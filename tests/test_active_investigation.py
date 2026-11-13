import tempfile
from itertools import permutations
import unittest
from pathlib import Path

from backend.investigation.active_investigation import ActiveInvestigation, Plan, Query, OUTCOMES, run_reference
from backend.investigation.paired_evidence import mask_sources
from backend.investigation.snapshot_store import SnapshotStore
from test_paired_investigation import fixture


def plan(tool, window=None, trace_id=None):
    query = Query(tool=tool, window=window, trace_id=trace_id)
    key = ':'.join(x for x in (tool, window, trace_id) if x)
    return Plan(query=query, hypothesis_ids=['capacity', 'arrival'], gap=key,
                expected_outcomes=OUTCOMES[tool]).model_dump()


class ActiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SnapshotStore(Path(self.temp.name))

    def run_state(self, value=None, budget=6):
        return ActiveInvestigation(self.store, self.store.freeze(value or fixture()), budget)

    def test_unqueried_data_does_not_change_context_or_candidates(self):
        a, b = self.run_state(fixture('capacity')), self.run_state(fixture('arrival'))
        self.assertEqual(a.state(), b.state())
        self.assertEqual(a.candidates(), b.candidates())

    def test_projection_separates_config_and_rate(self):
        run = self.run_state()
        config = run.execute(plan('compare_runtime_config'))
        self.assertNotIn('rate', str(config))
        self.assertEqual(run.state()['hypotheses']['arrival']['status'], 'open')
        self.assertEqual(run.history[0]['effects']['arrival'], 'unresolved')
        rate = run.execute(plan('compare_arrival_rate'))
        self.assertNotIn('max_connections', str(rate))
        summary = run.execute(plan('get_window_summary', 'reference'))
        self.assertNotIn('max_connections', str(summary))
        self.assertNotIn('requests', summary['data'])

    def test_invalid_expected_outcome_is_atomic(self):
        run = self.run_state()
        before = run.report()
        action = plan('compare_runtime_config')
        action['expected_outcomes'] = ['increased', 'stable']
        with self.assertRaisesRegex(ValueError, 'query_expectation'):
            run.execute(action)
        self.assertEqual(run.report(), before)
        action = plan('compare_runtime_config')
        action['query']['window'] = 'reference'
        with self.assertRaisesRegex(ValueError, 'query_scope'):
            run.execute(action)

    def test_repetition_and_unknown_trace_do_not_consume_budget(self):
        run = self.run_state()
        run.execute(plan('get_window_summary', 'incident'))
        before = run.report()
        with self.assertRaisesRegex(ValueError, 'repeated_query'):
            run.execute(plan('get_window_summary', 'incident'))
        with self.assertRaisesRegex(ValueError, 'unobserved_trace'):
            run.execute(plan('get_request_trace', 'incident', 'f' * 32))
        self.assertEqual(run.report(), before)
        key = run.evidence[-1]['data']['drilldown_ids'][0]
        detail = run.execute(plan('get_request_trace', 'incident', key))
        self.assertNotIn('max_connections', detail['data']['request'])
        detail['data']['request']['orders_ms'] = -1
        self.assertGreater(run.evidence[-1]['data']['request']['orders_ms'], 0)

    def test_reference_policy_classes_and_early_stop(self):
        for kind, expected in [('stable', 'no_anomaly_observed'), ('capacity', 'capacity_change'),
                ('arrival', 'arrival_change'), ('downstream', 'downstream_change'), ('competing', 'competing_changes')]:
            report = run_reference(self.store, self.store.freeze(fixture(kind)))
            self.assertEqual(report['state']['code'], expected)
            self.assertEqual(report['state']['stop'], 'sufficient')
            self.assertEqual(len(report['evidence']), 3 if kind == 'stable' else 5)
            if kind == 'competing':
                for h in ('capacity', 'arrival'):
                    self.assertEqual(report['state']['hypotheses'][h]['status'], 'supported')
            for entry in report['history']:
                self.assertIn(entry['evidence_id'], [e['id'] for e in report['evidence']])

    def test_missing_sources_and_budget_stop(self):
        for source in ('trace', 'runtime'):
            report = run_reference(self.store, self.store.freeze(mask_sources(fixture(), **{source: False})))
            self.assertEqual(report['state']['stop'], 'source_missing')
            self.assertEqual(report['state']['code'], 'insufficient_evidence')
            self.assertEqual(len(report['evidence']), 2 if source == 'trace' else 4)
        run = self.run_state(budget=1)
        self.assertEqual(run.state()['stop'], 'budget_exhausted')
        self.assertEqual(run.candidates(), [])
        with self.assertRaisesRegex(ValueError, 'budget_exhausted'):
            run.execute(plan('compare_arrival_rate'))

    def test_refutation_not_inferred_from_missing(self):
        run = self.run_state(mask_sources(fixture(), runtime=False))
        run.execute(plan('compare_runtime_config'))
        self.assertEqual(run.state()['hypotheses']['capacity']['status'], 'open')
        self.assertEqual(run.history[-1]['resolved_gaps'], [])
        self.assertIn('compare_runtime_config', run.state()['hypotheses']['capacity']['missing'])
        run = self.run_state(fixture('arrival'))
        run.execute(plan('compare_runtime_config'))
        self.assertEqual(run.state()['hypotheses']['capacity']['status'], 'refuted')
        self.assertEqual(run.state()['hypotheses']['arrival']['status'], 'open')

    def test_terminal_state_rejects_extra_query(self):
        run = self.run_state(fixture('stable'))
        while run.candidates():
            run.execute(run.candidates()[0]['plan'])
        before = run.report()
        with self.assertRaisesRegex(ValueError, 'already_stopped'):
            run.execute(plan('compare_runtime_config'))
        self.assertEqual(run.report(), before)

    def test_compound_sufficiency_preserves_unchecked_factor_for_every_order(self):
        for factor in ('capacity', 'arrival', 'competing'):
            value = fixture(factor)
            for row in value['incident']['trace']['requests']:
                row.update(inventory_ms=350., orders_ms=700.)
            actions = [plan('get_window_summary', w) for w in ('reference', 'incident')]
            actions += [plan('compare_runtime_config'), plan('compare_arrival_rate')]
            for ordered in permutations(actions):
                run = self.run_state(value)
                for action in ordered:
                    if run.state()['stop'] != 'continue':
                        break
                    run.execute(action)
                state = run.state()
                self.assertEqual(state['code'], 'competing_changes')
                self.assertEqual(state['sufficiency']['scope'], 'multiple_changes_exist')
                self.assertGreaterEqual(sum(h['status'] == 'supported' for h in state['hypotheses'].values()), 2)
                for h in state['sufficiency']['unchecked_factors']:
                    self.assertEqual(state['hypotheses'][h]['status'], 'open')
                    self.assertTrue(state['hypotheses'][h]['missing'])
                self.assertEqual(state['sufficiency']['exhaustive'], not state['gaps'])

    def test_single_change_cannot_skip_other_factor_even_at_budget_limit(self):
        for kind, tool, unchecked in (('downstream', 'compare_runtime_config', 'arrival'),
                                     ('capacity', 'compare_runtime_config', 'arrival'),
                                     ('arrival', 'compare_arrival_rate', 'capacity')):
            run = self.run_state(fixture(kind), budget=4)
            for action in (plan('get_window_summary', 'reference'), plan('get_window_summary', 'incident'), plan(tool)):
                run.execute(action)
            self.assertEqual(run.state()['code'], 'insufficient_evidence')
            self.assertEqual(run.state()['stop'], 'budget_exhausted')
            self.assertIn(unchecked, run.state()['sufficiency']['unchecked_factors'])
            self.assertEqual(run.history[-1]['resolved_gaps'], [tool])
