"""Offline, finite hypothesis planner over paired-v2 evidence.

This is a deterministic reference policy, not a measured LLM improvement.
"""
import copy
from typing import Literal

from pydantic import Field

from .bundle_evidence import Strict
from .paired_evidence import PAIRED_BUNDLE
from .paired_baseline import statistics, stage


class Query(Strict):
    tool: Literal['get_window_summary', 'get_request_trace', 'compare_runtime_config', 'compare_arrival_rate']
    window: Literal['reference', 'incident'] | None = None
    trace_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')


class Plan(Strict):
    query: Query
    hypothesis_ids: list[Literal['downstream', 'capacity', 'arrival']] = Field(min_length=2, max_length=3)
    gap: str
    expected_outcomes: list[str]


OUTCOMES = {
    'get_window_summary': ['no_anomaly', 'downstream', 'pre_send', 'mixed', 'unknown', 'unavailable'],
    'compare_runtime_config': ['decreased', 'unchanged', 'other', 'unavailable'],
    'compare_arrival_rate': ['increased', 'stable', 'other', 'unavailable'],
    'get_request_trace': ['observed', 'unavailable'],
}
EFFECTS = {
    'compare_runtime_config': {'decreased': {'capacity': 'support_feature', 'arrival': 'unresolved'},
        'unchanged': {'capacity': 'refute_feature', 'arrival': 'unresolved'}},
    'compare_arrival_rate': {'increased': {'arrival': 'support_feature', 'capacity': 'unresolved'},
        'stable': {'arrival': 'refute_feature', 'capacity': 'unresolved'}},
}


def query_key(query):
    if query.tool in ('get_window_summary', 'get_request_trace'):
        if query.window is None or (query.tool == 'get_request_trace') != (query.trace_id is not None):
            raise ValueError('invalid_query_scope')
    elif query.window is not None or query.trace_id is not None:
        raise ValueError('invalid_query_scope')
    return ':'.join(x for x in (query.tool, query.window, query.trace_id) if x)


class ActiveInvestigation:
    def __init__(self, store, key, budget=6):
        if type(budget) is not int or not 1 <= budget <= 100:
            raise ValueError('invalid_budget')
        self._bundle = PAIRED_BUNDLE.validate_python(store.read(key)).model_dump(mode='json')
        if self._bundle['projection_version'] != 'paired-bundle-v2':
            raise ValueError('unsupported_projection')
        self.snapshot_id, self.budget = key, budget
        self.evidence = [dict(id='e0', tool='get_alert', data=copy.deepcopy(self._bundle['alert']), cost=1)]
        self.observed, self.history = {}, []

    @property
    def remaining(self):
        return self.budget - len(self.evidence)

    def _read(self, query):
        """Only this tool boundary reads private frozen payloads."""
        b = self._bundle
        if query.tool == 'get_window_summary':
            trace = b[query.window]['trace']
            stats = statistics(trace)
            # Summary does not reveal runtime configuration or per-request timings.
            ids = [r['trace_id'] for r in sorted(trace['requests'], key=lambda r: r['orders_ms'], reverse=True)[:3]]
            return dict(quality=trace['quality'], outcome=stage(stats) if stats else 'unavailable',
                sample_count=len(trace['requests']), statistics=stats, drilldown_ids=ids)
        if query.tool == 'get_request_trace':
            row = next(r for r in b[query.window]['trace']['requests'] if r['trace_id'] == query.trace_id)
            fields = ('trace_id', 'status', 'orders_ms', 'inventory_ms', 'pre_send_ms', 'error_type')
            return dict(outcome='observed', request={k: row[k] for k in fields})
        a, c = (b[w]['runtime'] for w in ('reference', 'incident'))
        if a is None or c is None:
            return dict(outcome='unavailable', missing=[w for w in ('reference', 'incident') if b[w]['runtime'] is None])
        if query.tool == 'compare_runtime_config':
            values = {w: {k: b[w]['runtime']['after'][k] for k in ('max_connections', 'pool_timeout_ms')}
                      for w in ('reference', 'incident')}
            x, y = a['after']['max_connections'], c['after']['max_connections']
            return dict(outcome='decreased' if y < x else 'unchanged' if y == x else 'other', **values)
        values = {}
        for w, raw in (('reference', a), ('incident', c)):
            start, end = raw['before'], raw['send_end']
            count = end['requests_started'] - start['requests_started']
            elapsed = end['monotonic_us'] - start['monotonic_us']
            values[w] = dict(count=count, elapsed_us=elapsed, rate=count * 1e6 / elapsed,
                             send_end_in_flight=end['in_flight'])
        ratio = values['incident']['rate'] / values['reference']['rate'] if values['reference']['rate'] else None
        return dict(outcome='increased' if ratio is not None and ratio >= 1.5 else
                    'stable' if ratio is not None and .9 <= ratio <= 1.1 else 'other', ratio=ratio, **values)

    def execute(self, plan):
        plan = Plan.model_validate(plan)
        query = plan.query
        key = query_key(query)
        if self.remaining <= 0:
            raise ValueError('budget_exhausted')
        if key in self.observed:
            raise ValueError('repeated_query')
        if plan.gap != key or plan.expected_outcomes != OUTCOMES[query.tool]:
            raise ValueError('query_expectation_mismatch')
        ids = plan.hypothesis_ids
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate_hypotheses')
        if query.tool.startswith('compare_') and set(ids) != {'capacity', 'arrival'}:
            raise ValueError('hypothesis_query_mismatch')
        if query.tool == 'get_request_trace':
            summary = self.observed.get('get_window_summary:' + query.window)
            if not summary or query.trace_id not in summary['data']['drilldown_ids']:
                raise ValueError('unobserved_trace')
        before = self.state()
        if before['stop'] != 'continue':
            raise ValueError('investigation_already_stopped')
        data = self._read(query)
        evidence = dict(id='e' + str(len(self.evidence)), tool=query.tool, query=query.model_dump(),
                        data=data, cost=1)
        self.evidence.append(evidence)
        self.observed[key] = evidence
        after = self.state()
        self.history.append(dict(plan=plan.model_dump(), evidence_id=evidence['id'], outcome=data['outcome'],
            effects=EFFECTS.get(query.tool, {}).get(data['outcome'], {}),
            resolved_gaps=sorted(({k for h in before['hypotheses'].values() for k in h['missing']} |
                                  set(before['localization']['missing'])) -
                                 ({k for h in after['hypotheses'].values() for k in h['missing']} |
                                  set(after['localization']['missing']))),
            before=before['hypotheses'], after=after['hypotheses']))
        return copy.deepcopy(evidence)

    def state(self):
        """Compute state only from acquired evidence, never from unqueried payloads."""
        def get(key):
            return self.observed.get(key, {}).get('data')
        a, b = (get('get_window_summary:' + w) for w in ('reference', 'incident'))
        config, arrival = get('compare_runtime_config'), get('compare_arrival_rate')
        keys = ['get_window_summary:reference', 'get_window_summary:incident',
                'compare_runtime_config', 'compare_arrival_rate']
        gaps = [k for k in keys if k not in self.observed]
        refs = [e['id'] for e in self.observed.values()]
        hypotheses = {h: dict(status='open', scope='observed_change_not_unique_cause',
                             supporting_evidence=[], counter_evidence=[], missing=list(gaps))
                      for h in ('downstream', 'capacity', 'arrival')}
        code, stop = 'insufficient_evidence', 'continue'
        phase = b['outcome'] if b and b['outcome'] != 'unavailable' else 'unknown'
        valid_pair = bool(a and b and a['outcome'] == 'no_anomaly' and b['statistics'])
        slow = bool(valid_pair and b['statistics']['inventory_ms'] >= 250 and
                    b['statistics']['inventory_ms'] - a['statistics']['inventory_ms'] >= 100)
        capacity = bool(config and config['outcome'] == 'decreased')
        increased = bool(arrival and arrival['outcome'] == 'increased')
        if any(v and v['outcome'] == 'unavailable' for v in (a, b)):
            stop = 'source_missing'
        elif a and b:
            if a['outcome'] != 'no_anomaly':
                stop = 'unsupported_reference'
            elif phase == 'no_anomaly' and self.evidence[0]['data']['failed_requests'] == 0:
                code, stop = 'no_anomaly_observed', 'sufficient'
            elif any(v and v['outcome'] == 'unavailable' for v in (config, arrival)):
                stop = 'source_missing'
            elif phase != 'unknown' and sum((slow, capacity, increased)) >= 2:
                # Existence of multiple changes is monotone: checking another
                # factor cannot make this a single-change diagnosis. This does
                # not certify that all simultaneous changes have been found.
                code, stop = 'competing_changes', 'sufficient'
            elif config and arrival:
                if 'unavailable' in (config['outcome'], arrival['outcome']):
                    stop = 'source_missing'
                else:
                    sa, sb = a['statistics'], b['statistics']
                    slow = sb['inventory_ms'] >= 250 and sb['inventory_ms'] - sa['inventory_ms'] >= 100
                    capacity, increased = config['outcome'] == 'decreased', arrival['outcome'] == 'increased'
                    same_timeout = config['reference']['pool_timeout_ms'] == config['incident']['pool_timeout_ms']
                    if phase != 'unknown' and sum((slow, capacity, increased)) >= 2:
                        code = 'competing_changes'
                    elif slow:
                        code = 'downstream_change'
                    elif phase == 'pre_send' and sb['inventory_ms'] < 150 and sb['orders_ms'] - sa['orders_ms'] >= 100 and same_timeout:
                        if capacity and arrival['outcome'] == 'stable':
                            code = 'capacity_change'
                        elif increased and config['outcome'] == 'unchanged':
                            code = 'arrival_change'
                    stop = 'sufficient' if code != 'insufficient_evidence' else 'conditions_not_met'
        # Each feature has its own evidence requirement. Missing data never
        # becomes a refutation, including when classification can stop early.
        requirements = dict(downstream=keys[:2], capacity=[keys[2]], arrival=[keys[3]])
        for h, supported in (('downstream', slow), ('capacity', capacity), ('arrival', increased)):
            used = requirements[h]
            missing = [k for k in used if not get(k) or get(k)['outcome'] == 'unavailable']
            hypotheses[h]['missing'] = missing
            if supported:
                hypotheses[h].update(status='supported',
                    supporting_evidence=[self.observed[k]['id'] for k in used])
            elif not missing:
                hypotheses[h]['status'] = 'inconclusive'
        if valid_pair and not slow:
            hypotheses['downstream'].update(status='refuted',
                counter_evidence=[self.observed[k]['id'] for k in keys[:2]])
        # Feature-level refutations do not refute simultaneous alternative changes.
        for h, value, key, positive, negative in (
                ('capacity', config, 'compare_runtime_config', 'decreased', 'unchanged'),
                ('arrival', arrival, 'compare_arrival_rate', 'increased', 'stable')):
            if value and value['outcome'] == negative:
                hypotheses[h].update(status='refuted', counter_evidence=[self.observed[key]['id']])
        localization = dict(scope='slowest_sampled_incident_request', status='not_required',
                            stage=None, trace_id=None, missing=[], references=[])
        if valid_pair and phase == 'mixed':
            trace_id = b['drilldown_ids'][0]
            detail_key = 'get_request_trace:incident:' + trace_id
            detail = get(detail_key)
            localization.update(trace_id=trace_id, status='pending', missing=[detail_key])
            if detail:
                row = detail['request']
                if all(row[k] is not None for k in ('inventory_ms', 'pre_send_ms', 'orders_ms')):
                    request_stage = stage(dict(inventory_ms=row['inventory_ms'], pre_send_ms=row['pre_send_ms'],
                        orders_ms=row['orders_ms'], pool_timeout=row['error_type'] == 'PoolTimeout',
                        failed=row['status'] != 200))
                    localization.update(status='observed', stage=request_stage, missing=[],
                        references=[self.observed['get_window_summary:incident']['id'], self.observed[detail_key]['id']])
                else:
                    localization['status'] = 'unavailable'
            elif detail_key not in gaps:
                gaps.append(detail_key)
            if stop == 'sufficient' and localization['missing']:
                code = 'insufficient_evidence'
                stop = 'source_missing' if detail else 'continue'
        if stop == 'continue' and self.remaining <= 0:
            stop = 'budget_exhausted'
        if stop != 'continue' and code == 'insufficient_evidence':
            for h in hypotheses.values():
                if h['status'] == 'open':
                    h['status'] = 'inconclusive'
        sufficiency = dict(stage=phase != 'unknown', reference=valid_pair,
            classification=stop == 'sufficient',
            scope='multiple_changes_exist' if code == 'competing_changes' else
                  'sampled_no_anomaly' if code == 'no_anomaly_observed' else 'checked_change_conditions',
            unchecked_factors=[h for h, value in hypotheses.items() if value['missing']],
            exhaustive=valid_pair and not any(value['missing'] for value in hypotheses.values()))
        return dict(code=code, stage=phase, stop=stop, hypotheses=hypotheses, gaps=gaps,
                    sufficiency=sufficiency, localization=localization,
                    remaining=self.remaining, references=refs)

    def candidates(self):
        state = self.state()
        if state['stop'] != 'continue':
            return []
        result = []
        for gap in state['gaps']:
            parts = gap.split(':')
            tool, window, trace_id = (parts + [None, None])[:3]
            ids = ['capacity', 'arrival'] if tool.startswith('compare_') else ['downstream', 'capacity', 'arrival']
            # Reproducible heuristic: affected unresolved hypotheses + filled gap - cost.
            unresolved = sum(gap in state['hypotheses'][h]['missing'] for h in ids)
            unresolved += gap in state['localization']['missing']
            plan = Plan(query=Query(tool=tool, window=window, trace_id=trace_id), hypothesis_ids=ids,
                        gap=gap, expected_outcomes=OUTCOMES[tool])
            result.append(dict(plan=plan.model_dump(), score=unresolved + 1 - 1, cost=1,
                               effects=copy.deepcopy(EFFECTS.get(tool, {}))))
        return sorted(result, key=lambda item: -item['score'])

    def report(self):
        return copy.deepcopy(dict(protocol='active-reference-v3', snapshot_id=self.snapshot_id,
            state=self.state(), evidence=self.evidence, history=self.history,
            limitations=['有限规则假设与确定性参考策略；不是LLM收益或通用语义验证。',
                         '同一工具单位费用不等于相同token或实际查询成本。',
                         '支持观测变化不证明唯一因果，样本摘要不代表全量。']))


def run_reference(store, key, budget=6):
    run = ActiveInvestigation(store, key, budget)
    while run.state()['stop'] == 'continue':
        run.execute(run.candidates()[0]['plan'])
    return run.report()
