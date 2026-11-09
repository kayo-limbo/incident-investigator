import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.paired_plan import CollectionPlan, disjoint
from evals.collect_paired_windows import collect, assess_case, sources, audit
from evals.replay_paired_fixed import replay
from backend.investigation.paired_evidence import PairedBundleV2
from test_paired_investigation import fixture
from test_paired_evidence import reading


class PairedPlanTests(unittest.TestCase):
    def plans(self):
        return [CollectionPlan.model_validate_json(Path('evals/plans/' + name + '.json').read_text(encoding='utf-8'))
                for name in ('paired-development-v1', 'paired-heldout-v1')]

    def test_split_parameters_disjoint_and_strict(self):
        dev, held = self.plans()
        disjoint(dev, held)
        with self.assertRaisesRegex(ValueError, 'overlapping'):
            disjoint(dev, dev.model_copy(update={'split': 'heldout'}))
        for change in (lambda p: p['cases'].update(compound=[[60, 3, 10], [60, 1, 10]]),
                       lambda p: p['cases']['capacity'][0].__setitem__(2, True),
                       lambda p: p.update(duration=31),
                       lambda p: p['cases']['stable'][0].__setitem__(2, 100),
                       lambda p: p.update(url='https://example.com')):
            raw = dev.model_dump(mode='json')
            change(raw)
            with self.assertRaises(ValueError):
                CollectionPlan.model_validate(raw)

    def test_explicit_plan_saved_and_failures_retained(self):
        dev, _ = self.plans()
        raw = dev.model_dump(mode='json')
        raw.update(cases={'compound': raw['cases']['compound']}, repeats=1)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'run'
            with patch('evals.collect_paired_windows.compose', side_effect=RuntimeError('fixture')), \
                 patch('evals.collect_paired_windows.recover', return_value={
                     'verified': True, 'runtime': {**reading(5_000_000), 'max_connections': 100,
                         'pool_timeout_ms': 2000}, 'status': 200,
                     'business': {'sku': 'demo', 'can_order': True}}):
                result = collect(target, explicit_plan=raw)
            plan = json.loads((target / 'plan.private.json').read_text(encoding='utf-8'))
            self.assertEqual(plan['input_plan'], raw)
            self.assertEqual(plan['cases'], raw['cases'])
            self.assertEqual(result['cases'][0]['name'], 'compound')
            self.assertFalse(result['all_valid'])
            self.assertEqual(audit(target)['valid'], 0)
            plan['duration'] += 1
            (target / 'plan.private.json').write_text(json.dumps(plan), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'input_plan_mismatch'):
                audit(target)
        self.assertIn('evals/paired_plan.py', sources())

    def test_compound_requires_both_observed_changes(self):
        bundle = PairedBundleV2.model_validate(fixture('competing'))
        windows = {w: {'load': {'samples': [{'business_ok': True, 'client_ms': ms}]}}
                   for w, ms in [('reference', 100.), ('incident', 400.)]}
        specs = ((60, 2, 20), (60, 1, 40))
        self.assertTrue(assess_case('compound', windows, bundle, specs)['ready'])
        for field in ('send_end', 'after'):
            getattr(bundle.incident.runtime, field).requests_started = 60
            getattr(bundle.incident.runtime, field).requests_finished = 60
        self.assertIn('arrival_change_not_observed', assess_case('compound', windows, bundle, specs)['failures'])

    def test_development_replay_refuses_heldout_before_reading_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'plan.private.json').write_text('{"split":"heldout"}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'heldout_replay'):
                replay(root)
