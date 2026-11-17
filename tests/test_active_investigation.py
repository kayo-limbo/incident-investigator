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


def mixed_fixture(together):
    value = fixture('stable')
    rows = value['incident']['trace']['requests']
    for i in (0, 1):
        rows[i].update(inventory_ms=350., orders_ms=800. - i)
    for i in ((0, 1) if together else (2, 3)):
        rows[i].update(pre_send_ms=300., orders_ms=800. - i)
    return value


class ActiveTests(unittest.TestCase):
    def test_bound_claim_keeps_source_scope_precision_and_complete_sentence(self):
        import hashlib
        from backend.investigation.claim_verification import verify_bound_claim
        value = fixture('downstream')
        for row in value['incident']['trace']['requests']:
            row.update(orders_ms=362.079, inventory_ms=352.727)
        run = self.run_state(value)
        summary = run.execute(plan('get_window_summary', 'incident'))
        claim = dict(snapshot_id=run.snapshot_id, evidence_id=summary['id'], window='incident',
                     metric='orders_ms', statistic='p95', population='observed_non_null',
                     method='nearest_rank', value=362.08, unit='ms', approximation='约362.08ms')
        quote = '事件窗口采样订单 P95约362.08ms。'
        text = '调查摘要。\n' + quote + '\n未检查因果。'
        binding = dict(report_sha256=hashlib.sha256(text.encode()).hexdigest(),
                       start=text.index(quote), end=text.index(quote)+len(quote), quote=quote, claim=claim)
        before = run.report(), run.remaining
        def check(b=binding, report=text):
            return verify_bound_claim(b, report_text=report, snapshot_id=run.snapshot_id, evidence=run.evidence)
        self.assertEqual(check()['verification']['status'], 'supported')
        for changed in ({'metric':'inventory_ms'}, {'window':'reference'}, {'value':362.080,'approximation':'约362.080ms'},
                        {'approximation':None}, {'value':362.09,'approximation':'约362.09ms'},
                        {'statistic':'reported_p95','population':'alert_record','method':'reported'}):
            with self.subTest(changed=changed):
                self.assertEqual(check(dict(binding,claim=dict(claim,**changed)))['reason'], 'claim_text_mismatch')
        self.assertEqual(check(report=text+'改动')['reason'], 'report_hash_mismatch')
        self.assertEqual(check(dict(binding,start=binding['start']+1))['reason'], 'source_span_mismatch')
        self.assertEqual(check(dict(binding,quote=quote.replace('订单','库存')))['reason'], 'source_span_mismatch')
        for prefix in ('并非', '如果', '假设：'):
            modified = prefix+quote
            fragment = dict(binding,report_sha256=hashlib.sha256(modified.encode()).hexdigest(),
                            start=len(prefix),end=len(modified))
            self.assertEqual(check(fragment,modified)['reason'], 'incomplete_sentence')
        unsupported = '事件窗口采样订单P95低于250ms。'
        b = dict(binding,report_sha256=hashlib.sha256(unsupported.encode()).hexdigest(),
                 start=0,end=len(unsupported),quote=unsupported)
        self.assertEqual(check(b,unsupported)['reason'], 'unsupported_expression')
        wrong = '事件窗口采样库存P95为352.213ms。'
        b = dict(binding,report_sha256=hashlib.sha256(wrong.encode()).hexdigest(),start=0,end=len(wrong),quote=wrong,
                 claim=dict(claim,metric='inventory_ms',value=352.213,approximation=None))
        self.assertEqual(check(b,wrong)['verification']['status'], 'contradicted')
        alert = '事件告警记录客户端P95为'+str(value['alert']['client_p95_ms'])+'ms。'
        b = dict(binding,report_sha256=hashlib.sha256(alert.encode()).hexdigest(),start=0,end=len(alert),quote=alert,
                 claim=dict(claim,evidence_id='e0',metric='client_elapsed_ms',statistic='reported_p95',
                            population='alert_record',method='reported',value=value['alert']['client_p95_ms'],approximation=None))
        self.assertEqual(check(b,alert)['verification']['scope'], 'reported_alert_only')
        self.assertEqual((run.report(),run.remaining),before)

    def test_natural_report_rounding_regression_keeps_wrong_inventory_rejected(self):
        value = fixture('downstream')
        for row in value['incident']['trace']['requests']:
            row.update(orders_ms=362.079, inventory_ms=352.727)
        run = self.run_state(value)
        summary = run.execute(plan('get_window_summary', 'incident'))
        claim = dict(snapshot_id=run.snapshot_id, evidence_id=summary['id'], window='incident',
                     metric='orders_ms', statistic='p95', population='observed_non_null',
                     method='nearest_rank', value=362.08, unit='ms', approximation='约362.08ms')
        before = run.report(), run.remaining
        self.assertEqual(run.verify_claim(claim)['status'], 'supported')
        self.assertEqual(run.verify_claim(dict(claim, approximation=None))['status'], 'contradicted')
        self.assertEqual(run.verify_claim(dict(claim, value=362.07, approximation='约362.07ms'))['status'], 'contradicted')
        self.assertEqual(run.verify_claim(dict(claim, metric='inventory_ms', value=352.213,
                                              approximation=None))['status'], 'contradicted')
        for changes in ({'approximation': '约362.1ms'}, {'approximation': '362.08ms'},
                        {'approximation': '约362ms'}, {'decimal_places': 0}, {'tolerance': 10}):
            with self.assertRaises(ValueError):
                run.verify_claim(dict(claim, **changes))
        self.assertEqual((run.report(), run.remaining), before)

    def test_alert_attribution_without_trace_does_not_claim_trace_or_server_measurement(self):
        from backend.investigation.claim_verification import verify_claim
        value = mask_sources(fixture(), trace=False)
        value['alert']['client_p95_ms'] = 383.741
        run = self.run_state(value)
        claim = dict(snapshot_id=run.snapshot_id, evidence_id='e0', window='incident',
                     metric='client_elapsed_ms', statistic='reported_p95', population='alert_record',
                     method='reported', value=383.7, unit='ms', approximation='约383.7ms')
        before = run.report(), run.remaining
        result = run.verify_claim(claim)
        self.assertEqual((result['status'], result['scope']), ('supported', 'reported_alert_only'))
        self.assertEqual(run.verify_claim(dict(claim, metric='orders_ms'))['reason'], 'metric_unavailable')
        self.assertEqual(run.verify_claim(dict(claim, window='reference'))['status'], 'insufficient_evidence')
        self.assertEqual(run.verify_claim(dict(claim, evidence_id='missing'))['status'], 'insufficient_evidence')
        # Historical acquired alert envelope: attribution only, no synthetic summary.
        old = [dict(id='old-alert', tool='get_alert', data={'alert': {'orders_p95_ms': 383.741}})]
        old_claim = dict(claim, evidence_id='old-alert', metric='orders_ms')
        self.assertEqual(verify_claim(old_claim, snapshot_id=run.snapshot_id, evidence=old)['status'], 'supported')
        self.assertEqual(verify_claim(dict(old_claim, value=383.6, approximation='约383.6ms'),
                                     snapshot_id=run.snapshot_id, evidence=old)['status'], 'contradicted')
        for changes in ({'population': 'all_traffic'}, {'method': 'nearest_rank'}, {'metric': 'inventory_ms'}):
            with self.assertRaises(ValueError):
                run.verify_claim(dict(claim, **changes))
        self.assertEqual((run.report(), run.remaining), before)

    def test_statistical_claim_verification_and_scope(self):
        # Distinct requests own the two P95s; nearest-rank is not max at n=20.
        value = fixture('downstream')
        rows = value['incident']['trace']['requests']
        rows[0].update(orders_ms=900., inventory_ms=352.213)
        rows[1].update(orders_ms=800., inventory_ms=352.213)
        rows[2].update(orders_ms=500., inventory_ms=352.727)
        rows[3].update(orders_ms=450., inventory_ms=999.)
        run = self.run_state(value)
        summary = run.execute(plan('get_window_summary', 'incident'))
        detail = run.execute(plan('get_request_trace', 'incident', rows[1]['trace_id']))
        before = run.report(), run.remaining
        claim = dict(snapshot_id=run.snapshot_id, evidence_id=summary['id'],
                     window='incident', metric='inventory_ms', statistic='p95',
                     population='observed_non_null', method='nearest_rank', value=352.213, unit='ms')
        result = run.verify_claim(claim)
        self.assertEqual((result['status'], result['expected_value']), ('contradicted', 352.727))
        self.assertEqual(run.verify_claim(dict(claim, value=352.727))['status'], 'supported')
        self.assertEqual(run.verify_claim(dict(claim, metric='orders_ms', value=800.))['status'], 'supported')
        request = dict(claim, evidence_id=detail['id'], statistic='request_value',
                       method='none', population='request', trace_id=rows[1]['trace_id'])
        self.assertEqual(run.verify_claim(request)['status'], 'supported')
        for changed, reason in [({'evidence_id': detail['id']}, 'aggregate_requires_summary'),
                                ({'window': 'reference'}, 'window_mismatch'),
                                ({'snapshot_id': 'f'*64}, 'snapshot_mismatch'),
                                ({'evidence_id': 'e99'}, 'unobserved_reference'),
                                ({'population': 'all_traffic'}, 'population_not_observed')]:
            with self.subTest(reason=reason):
                result = run.verify_claim(dict(claim, **changed))
                self.assertEqual((result['status'], result['reason']), ('insufficient_evidence', reason))
        self.assertEqual(run.verify_claim(dict(request, trace_id=rows[0]['trace_id']))['status'], 'insufficient_evidence')
        self.assertEqual((run.report(), run.remaining), before)

    def test_claim_missing_evidence_and_invalid_claim_are_read_only(self):
        run = self.run_state(mask_sources(fixture(), trace=False))
        summary = run.execute(plan('get_window_summary', 'incident'))
        claim = dict(snapshot_id=run.snapshot_id, evidence_id=summary['id'], window='incident',
                     metric='inventory_ms', statistic='p95', population='observed_non_null',
                     method='nearest_rank', value=352.213, unit='ms')
        before = run.report(), run.remaining
        self.assertEqual(run.verify_claim(claim)['reason'], 'statistic_unavailable')
        for change in ({'value': float('nan')}, {'unit': 's'}, {'method': 'none'},
                       {'trace_id': 'a'*32}, {'tolerance': 100}):
            with self.assertRaises(ValueError):
                run.verify_claim(dict(claim, **change))
        self.assertEqual((run.report(), run.remaining), before)

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
                row.update(inventory_ms=350., pre_send_ms=0., orders_ms=700.)
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

    def test_mixed_summary_requires_targeted_localization_without_changing_population_stage(self):
        for together, expected in ((True, 'mixed'), (False, 'downstream')):
            run = self.run_state(mixed_fixture(together))
            for action in (plan('get_window_summary', 'reference'), plan('get_window_summary', 'incident'),
                           plan('compare_runtime_config'), plan('compare_arrival_rate')):
                run.execute(action)
            state = run.state()
            self.assertEqual(state['stage'], 'mixed')
            self.assertEqual(state['stop'], 'continue')
            self.assertFalse(state['sufficiency']['classification'])
            self.assertEqual(state['localization']['status'], 'pending')
            target = state['localization']['trace_id']
            candidate = run.candidates()[0]['plan']
            self.assertEqual(candidate['query']['trace_id'], target)
            run.execute(candidate)
            state = run.state()
            self.assertEqual(state['localization']['stage'], expected)
            self.assertEqual(state['stage'], 'mixed')
            self.assertEqual(state['code'], 'downstream_change')
            self.assertEqual(run.history[-1]['resolved_gaps'], ['get_request_trace:incident:' + target])
            self.assertEqual(len(state['localization']['references']), 2)

    def test_wrong_request_cannot_fill_local_gap_and_budget_cannot_bypass_it(self):
        run = self.run_state(mixed_fixture(False))
        for window in ('reference', 'incident'):
            run.execute(plan('get_window_summary', window))
        ids = run.evidence[-1]['data']['drilldown_ids']
        run.execute(plan('get_request_trace', 'incident', ids[1]))
        self.assertEqual(run.state()['localization']['status'], 'pending')
        self.assertEqual(run.history[-1]['resolved_gaps'], [])
        for tool in ('compare_runtime_config', 'compare_arrival_rate'):
            run.execute(plan(tool))
        self.assertEqual(run.state()['stop'], 'budget_exhausted')
        self.assertEqual(run.state()['code'], 'insufficient_evidence')
