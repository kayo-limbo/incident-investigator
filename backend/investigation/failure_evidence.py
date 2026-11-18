"""Request-level 503 evidence from content-addressed, frozen window spans."""


def sample_ids(rows, status=None):
    candidates = (r for r in rows if status is None or r['status'] == status)
    ranked = sorted(candidates, key=lambda r: -r['orders_ms'])
    return [r['trace_id'] for r in ranked[:3]]


def investigate_503(store, snapshot_id, *, budget, by_status=True):
    """One index read and at most one advertised trace, shared by both policies."""
    if type(budget) is not int or budget < 1:
        raise ValueError('positive_budget_required')
    payload = store.read(snapshot_id)
    if payload.get('projection_version') != 'pool-window-v1':
        raise ValueError('pool_window_snapshot_required')
    rows = []
    for trace_id, trace in payload['traces'].items():
        roots = [s for s in trace['spans'] if s['service'] == 'orders'
                 and s['operation'] == 'GET /orders/preview' and s['tags'].get('span.kind') == 'server']
        if len(roots) == 1:
            root = roots[0]
            rows.append(dict(trace_id=trace_id, orders_ms=root['duration_ms'],
                             status=root['tags'].get('http.response.status_code'), root=root))
    ids = sample_ids(rows, 503 if by_status else None)
    index = {r['trace_id']: r for r in rows}
    result = dict(snapshot_id=snapshot_id, tool_calls=1, code='no_visible_503',
                  candidates=ids, evidence=[], scope='sampled_request')
    trace_id = next((i for i in ids if index[i]['status'] == 503), None)
    if trace_id is None:
        return result
    if budget < 2:
        result['code'] = 'budget_exhausted'
        return result
    result.update(tool_calls=2, code='unconfirmed_failure_stage')
    root = index[trace_id]['root']
    def cite(span, field):
        return dict(snapshot_id=snapshot_id, trace_id=trace_id, span_id=span['span_id'], field=field)
    result['evidence'].append(cite(root, 'tags.http.response.status_code'))
    calls = [s for s in payload['traces'][trace_id]['spans']
             if root['span_id'] in s['parents'] and s['service'] == 'orders'
             and s['operation'] == 'GET inventory' and s['tags'].get('span.kind') == 'client']
    if len(calls) == 1:
        span = calls[0]
        if span['tags'].get('error.type') == 'PoolTimeout' and span['tags'].get('client.request_headers_started') is False:
            result.update(code='inspect_orders_connection_pool',
                          finding='该 503 请求调用库存时，在发出请求头前发生 PoolTimeout，首查订单客户端连接池。')
            result['evidence'].extend([cite(span, 'tags.error.type'), cite(span, 'tags.client.request_headers_started')])
    return result
