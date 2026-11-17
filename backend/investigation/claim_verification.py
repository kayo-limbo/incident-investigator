"""Verify structured numeric claims against acquired active-tool evidence only.

No natural-language extraction, private snapshot access, or causal judgement.
P95 uses the summary tool's nearest-rank statistic over observed non-null values.
"""
from typing import Literal

from pydantic import Field, model_validator

from .bundle_evidence import Strict


class StatisticalClaim(Strict):
    snapshot_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence_id: str
    window: Literal['reference', 'incident']
    metric: Literal['orders_ms', 'inventory_ms', 'pre_send_ms']
    statistic: Literal['p95', 'request_value']
    population: Literal['observed_non_null', 'request', 'all_traffic']
    method: Literal['nearest_rank', 'none']
    trace_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    value: float = Field(ge=0)
    unit: Literal['ms']

    @model_validator(mode='after')
    def scope(self):
        if self.statistic == 'p95':
            if self.method != 'nearest_rank' or self.trace_id is not None or self.population == 'request':
                raise ValueError('invalid_p95_scope')
        elif self.method != 'none' or self.trace_id is None or self.population != 'request':
            raise ValueError('invalid_request_scope')
        return self


def verify_claim(claim, *, snapshot_id, evidence):
    """Evidence must come from the active executor, never from the claim author.

    Unsupported provenance/scope yields insufficient evidence, not a numeric
    contradiction. Values are compared at fixed 0.001 ms precision; callers
    cannot choose a tolerance that makes a wrong claim pass.
    """
    claim = StatisticalClaim.model_validate(claim)

    def result(status, reason, expected=None):
        return dict(protocol='statistical-claim-v1', claim=claim.model_dump(),
                    status=status, reason=reason, expected_value=expected,
                    precision_ms=0.001)

    if claim.snapshot_id != snapshot_id:
        return result('insufficient_evidence', 'snapshot_mismatch')
    matches = [e for e in evidence if e['id'] == claim.evidence_id]
    if len(matches) != 1:
        return result('insufficient_evidence', 'unobserved_reference')
    item = matches[0]
    if item.get('query', {}).get('window') != claim.window:
        return result('insufficient_evidence', 'window_mismatch')
    if claim.population == 'all_traffic':
        return result('insufficient_evidence', 'population_not_observed')
    data = item['data']
    if claim.statistic == 'p95':
        if item['tool'] != 'get_window_summary':
            return result('insufficient_evidence', 'aggregate_requires_summary')
        if data.get('quality') != 'observed' or not data.get('statistics'):
            return result('insufficient_evidence', 'statistic_unavailable')
        expected = data['statistics'].get(claim.metric)
    else:
        if item['tool'] != 'get_request_trace' or item['query']['trace_id'] != claim.trace_id:
            return result('insufficient_evidence', 'request_mismatch')
        expected = data.get('request', {}).get(claim.metric)
    if expected is None:
        return result('insufficient_evidence', 'metric_unavailable')
    supported = round(claim.value, 3) == round(expected, 3)
    return result('supported' if supported else 'contradicted',
                  'value_matches' if supported else 'value_mismatch', expected)
