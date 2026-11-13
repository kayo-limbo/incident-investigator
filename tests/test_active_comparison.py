import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.investigation.model import ModelConfig
from backend.investigation.paired_evidence import PairedBundleV2, mask_sources
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.active_policies import wrap_query, investigate_policy
from test_active_policies import Scripted, QUERIES
from evals.run_active_comparison import freeze, validate, evaluate, summarize, score, CASES
from test_paired_investigation import fixture


class ComparisonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        self.destination = Path(self.temp.name) / 'run'
        self.config = ModelConfig('http://localhost', 'test', 'secret-test-key')
        source = SnapshotStore(self.root / 'snapshots')
        self.payloads = {name: fixture('competing' if name == 'compound' else 'stable' if name == 'restart' else name) for name in CASES}
        cases = []
        for name, payload in self.payloads.items():
            cases.append(dict(name=name, repeat=0, valid=True, feasibility={'ready': True},
                windows={'fixture': name}, snapshot_id=source.freeze(payload), source_ablations={
                    'trace_only': source.freeze(mask_sources(payload, runtime=False)),
                    'runtime_only': source.freeze(mask_sources(payload, trace=False))}))
        (self.root / 'plan.private.json').write_text(json.dumps(dict(split='development', limits={})), encoding='utf-8')
        (self.root / 'collection.private.json').write_text(json.dumps(dict(completed=True,
            recovery={'verified': True}, cases=cases)), encoding='utf-8')

    def freeze(self):
        with patch('evals.run_active_comparison.rebuild_pair', side_effect=lambda w, l: PairedBundleV2.model_validate(self.payloads[w['fixture']])):
            return freeze(self.root, self.destination, self.config)

    async def test_freeze_fixed_replay_audit_and_no_overwrite(self):
        plan = self.freeze()
        self.assertEqual(len(plan['samples']), 8)
        self.assertNotIn('secret-test-key', (self.destination / 'plan.private.json').read_text())
        result = await evaluate(self.destination)
        self.assertEqual(sum(r.get('accepted', False) for r in result['rows']), 8)
        self.assertEqual(sum(r['status'] == 'not_started' for r in result['rows']), 24)
        before = (self.destination / 'fixed-reports/0-fixed.json').read_bytes()
        await evaluate(self.destination)
        self.assertEqual(before, (self.destination / 'fixed-reports/0-fixed.json').read_bytes())
        with self.assertRaises(FileExistsError):
            self.freeze()

    async def test_alternative_path_sufficiency_replays_into_comparison_metrics(self):
        # Synthetic mechanism check, not a measured model policy improvement.
        value = fixture('arrival')
        for row in value['incident']['trace']['requests']:
            row.update(inventory_ms=350., orders_ms=700.)
        store = SnapshotStore(self.root / 'snapshots')
        key = store.freeze(value)
        sample = dict(snapshot_id=key, expected='competing_changes')
        fixed = await investigate_policy(store, key)
        actions = QUERIES[:2] + [QUERIES[3]]
        for strategy in ('react', 'active'):
            model = Scripted([wrap_query(q).model_dump() for q in actions] if strategy == 'active' else actions)
            report = await investigate_policy(store, key, model, strategy=strategy)
            metrics = score(report, sample, store, strategy)
            self.assertTrue(metrics['accepted'])
            self.assertEqual(metrics['tool_calls'], 4)
            self.assertFalse(metrics['sufficiency']['exhaustive'])
            self.assertEqual(report['state']['sufficiency']['unchecked_factors'], ['capacity'])
        self.assertTrue(score(fixed, sample, store, 'fixed')['accepted'])
        self.assertEqual(fixed['tool_calls'], 5)

    def test_split_membership_provenance_and_plan_tampering(self):
        p = self.root / 'plan.private.json'
        p.write_text('{"split":"heldout"}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'development'):
            self.freeze()
        p.write_text('{"split":"development","limits":{}}', encoding='utf-8')
        self.freeze()
        p = self.destination / 'plan.private.json'
        value = json.loads(p.read_text())
        value['budget']['budget'] = 100
        p.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'digest'):
            validate(self.destination)

    def test_source_and_model_changes_block(self):
        self.freeze()
        with self.assertRaisesRegex(ValueError, 'protocol_changed'):
            validate(self.destination, ModelConfig('http://localhost', 'changed', 'key'))
        with patch('evals.run_active_comparison.sources', return_value={}):
            with self.assertRaisesRegex(ValueError, 'protocol_changed'):
                validate(self.destination)

    async def test_uncertain_started_attempt_never_reissued(self):
        self.freeze()
        folder = self.destination / 'model-reports'
        folder.mkdir()
        (folder / '0-react.started.json').write_text('{}')
        with patch('evals.run_active_comparison.ReactQueryModel', side_effect=AssertionError('must not call')):
            with self.assertRaises(FileExistsError):
                await evaluate(self.destination, self.config)
        rows = summarize(self.destination)['rows']
        self.assertEqual(next(r for r in rows if r['sample'] == 0 and r['variant'] == 'react')['status'], 'unknown_interrupted')

    async def test_report_state_evidence_and_usage_tampering(self):
        plan = self.freeze()
        await evaluate(self.destination)
        report = json.loads((self.destination / 'fixed-reports/0-fixed.json').read_text(encoding='utf-8'))
        store = SnapshotStore(self.destination / 'snapshots')
        for change in (lambda r: r['state'].update(code='arrival_change'),
                       lambda r: r['evidence'][0]['data'].update(request_count=999),
                       lambda r: r.update(model_calls=1),
                       lambda r: r['token_budget'].update(known_total_tokens=999)):
            changed = copy.deepcopy(report)
            change(changed)
            with self.assertRaises(ValueError):
                score(changed, plan['samples'][0], store, 'fixed')

    async def test_scripted_model_batch_does_not_receive_labels_and_failures_retained(self):
        self.freeze()
        contexts = []
        class Model:
            def __init__(self, config): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def decide(self, ctx):
                contexts.append(ctx.model_dump())
                self.last_usage = dict(prompt_tokens=10, completion_tokens=10, total_tokens=20)
                return {'unexpected': 'private'}
        with patch('evals.run_active_comparison.ReactQueryModel', Model), patch('evals.run_active_comparison.ActivePlanModel', Model):
            result = await evaluate(self.destination, self.config)
            count = len(contexts)
            await evaluate(self.destination, self.config)
        self.assertEqual(count, 24)
        self.assertEqual(len(contexts), 24)
        self.assertTrue(all('expected' not in c and 'name' not in c for c in contexts))
        self.assertEqual(sum(r.get('invalid_action', False) for r in result['rows']), 24)
