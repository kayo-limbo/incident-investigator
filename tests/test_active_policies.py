import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.investigation.active_policies import (investigate_policy, wrap_query, context,
    ReactQueryModel, ActivePlanModel, PolicyCancelled)
from backend.investigation.active_investigation import ActiveInvestigation
from backend.investigation.model import ModelConfig
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.active_cli import main
from test_paired_investigation import fixture


QUERIES = [dict(tool='get_window_summary', window=w) for w in ('reference', 'incident')] + [
    dict(tool='compare_runtime_config'), dict(tool='compare_arrival_rate')]


class Scripted:
    def __init__(self, actions, usage=True):
        self.actions, self.known, self.contexts = iter(actions), usage, []

    async def decide(self, ctx):
        self.contexts.append(ctx.model_copy(deep=True))
        self.last_usage = dict(prompt_tokens=10, completion_tokens=10, total_tokens=20) if self.known else None
        return next(self.actions)


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SnapshotStore(Path(self.temp.name))
        self.key = self.store.freeze(fixture('competing'))

    async def test_same_actions_same_evidence_and_stopping(self):
        fixed = await investigate_policy(self.store, self.key)
        for policy in ('react', 'active'):
            model = Scripted([wrap_query(q).model_dump() for q in QUERIES] if policy == 'active' else QUERIES)
            result = await investigate_policy(self.store, self.key, model, strategy=policy)
            self.assertEqual(result['state'], fixed['state'])
            self.assertEqual(result['evidence'], fixed['evidence'])
            self.assertEqual(result['model_calls'], 4)
            self.assertEqual(result['model_usage']['calls'][0]['total_tokens'], 20)
            self.assertEqual(result['state']['code'], 'competing_changes')

    def test_context_visibility_and_scoring_ablation(self):
        run = ActiveInvestigation(self.store, self.key)
        plain = context(run, 'react')
        self.assertIsNone(plain.hypotheses)
        self.assertIsNone(plain.candidates)
        full, ablated = context(run, 'active'), context(run, 'active', False)
        self.assertEqual(full.evidence, ablated.evidence)
        self.assertNotIn('score', ablated.candidates[0])
        self.assertIn('score', full.candidates[0])
        full.evidence[0]['data'].clear()
        self.assertTrue(run.evidence[0]['data'])

    async def test_token_unknown_and_exhausted_discard_action(self):
        for known, limit, reason in ((False, 100, 'token_unknown'), (True, 20, 'token_exhausted')):
            result = await investigate_policy(self.store, self.key, Scripted(QUERIES, known),
                strategy='react', max_total_tokens=limit)
            self.assertEqual(result['termination'], reason)
            self.assertEqual(result['tool_calls'], 1)

    async def test_repeat_bad_plan_and_schema_stop_without_retry(self):
        for actions, policy, reason in (([QUERIES[0], QUERIES[0]], 'react', 'repeated_query'),
                ([{'url': 'private'}], 'react', 'invalid_schema'),
                ([dict(wrap_query(QUERIES[2]).model_dump(), expected_outcomes=['increased'])],
                 'active', 'query_expectation_mismatch')):
            result = await investigate_policy(self.store, self.key, Scripted(actions), strategy=policy)
            self.assertEqual(result['failure'], reason)
            self.assertNotIn('private', json.dumps(result))
            self.assertEqual(len(result['history']), len(actions) - 1)

    async def test_rejected_http_actions_are_bounded_and_do_not_commit(self):
        mismatch = wrap_query(QUERIES[2]).model_dump()
        mismatch['hypothesis_ids'] = ['downstream', 'capacity']
        expectation = wrap_query(QUERIES[2]).model_dump()
        expectation.update(gap='private-text' * 1000, expected_outcomes=['private-text' * 1000])
        cases = [(ActivePlanModel, 'active', mismatch, 'hypothesis_query_mismatch'),
                 (ReactQueryModel, 'react', dict(tool='compare_runtime_config', window='incident'), 'invalid_query_scope'),
                 (ActivePlanModel, 'active', expectation, 'query_expectation_mismatch')]
        initial = ActiveInvestigation(self.store, self.key).report()
        for adapter, policy, candidate, reason in cases:
            with self.subTest(reason=reason):
                def handler(request):
                    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                        'message': {'content': json.dumps(candidate)}}],
                        'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}})
                async with adapter(ModelConfig('http://localhost', 'test', 'test-key'),
                                   transport=httpx.MockTransport(handler)) as model:
                    result = await investigate_policy(self.store, self.key, model, strategy=policy)
                self.assertEqual(result['failure'], reason)
                self.assertEqual(result['evidence'], initial['evidence'])
                self.assertEqual(result['state'], initial['state'])
                self.assertEqual(result['history'], [])
                self.assertEqual(result['tool_calls'], 1)
                self.assertEqual(result['model_calls'], 1)
                self.assertEqual(result['model_usage']['calls'][0]['total_tokens'], 20)
                diagnostic = result['rejected_action']
                self.assertTrue(diagnostic['candidate_available'])
                self.assertEqual(diagnostic['reason'], reason)
                self.assertEqual(diagnostic['query']['tool'], 'compare_runtime_config')
                self.assertLess(len(json.dumps(diagnostic)), 700)
                self.assertNotIn('private-text', json.dumps(result))
                if reason == 'hypothesis_query_mismatch':
                    self.assertEqual(diagnostic['hypothesis_ids'], ['downstream', 'capacity'])
                if reason == 'invalid_query_scope':
                    self.assertEqual(diagnostic['query']['window'], 'incident')

    async def test_timeout_and_cancel_retain_partial_report(self):
        entered = asyncio.Event()
        class Waiting:
            async def decide(self, ctx):
                entered.set()
                await asyncio.sleep(10)
        result = await investigate_policy(self.store, self.key, Waiting(), strategy='react', timeout_seconds=.2)
        self.assertEqual(result['termination'], 'deadline_exceeded')
        self.assertEqual(result['model_calls'], 1)
        entered.clear()
        task = asyncio.create_task(investigate_policy(self.store, self.key, Waiting(), strategy='active'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(PolicyCancelled) as caught:
            await task
        self.assertEqual(caught.exception.report['termination'], 'cancelled')
        self.assertEqual(caught.exception.report['tool_calls'], 1)

    async def test_budget_one_never_calls_model(self):
        result = await investigate_policy(self.store, self.key, Scripted([]), strategy='react', budget=1)
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(result['state']['stop'], 'budget_exhausted')

    async def test_cli_default_fixed_never_loads_credentials_or_overwrites(self):
        output = Path(self.temp.name) / 'report.json'
        args = [self.key, '--store', self.temp.name, '--output', str(output)]
        with patch('backend.investigation.active_cli.ModelConfig.from_env', side_effect=AssertionError):
            self.assertEqual(await asyncio.to_thread(main, args), 0)
            with self.assertRaises(FileExistsError):
                await asyncio.to_thread(main, args)

    async def test_http_adapters_use_correct_schema_and_usage(self):
        config = ModelConfig('http://localhost', 'test', 'test-key')
        for adapter, policy in ((ReactQueryModel, 'react'), (ActivePlanModel, 'active')):
            actions = iter([wrap_query(q).model_dump() for q in QUERIES] if policy == 'active' else QUERIES)
            requests = []
            def handler(request):
                requests.append(json.loads(request.content))
                return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(next(actions))}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}})
            async with adapter(config, transport=httpx.MockTransport(handler)) as model:
                report = await investigate_policy(self.store, self.key, model, strategy=policy)
            self.assertEqual(report['termination'], 'completed')
            self.assertEqual(report['model_calls'], 4)
            self.assertTrue(requests)
            self.assertNotIn('competing_changes', requests[0]['messages'][-1]['content'])
