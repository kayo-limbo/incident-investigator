"""Verify structured numeric claims against acquired active-tool evidence only.

No natural-language extraction, private snapshot access, or causal judgement.
P95 uses the summary tool's nearest-rank statistic over observed non-null values.
"""
from typing import Literal
from decimal import Decimal, ROUND_HALF_UP
import re
import hashlib

from pydantic import Field, model_validator

from .bundle_evidence import Strict


class StatisticalClaim(Strict):
    snapshot_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence_id: str
    window: Literal['reference', 'incident']
    metric: Literal['orders_ms', 'inventory_ms', 'pre_send_ms', 'client_elapsed_ms']
    statistic: Literal['p95', 'request_value', 'reported_p95']
    population: Literal['observed_non_null', 'request', 'all_traffic', 'alert_record']
    method: Literal['nearest_rank', 'none', 'reported']
    trace_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    value: float = Field(ge=0)
    unit: Literal['ms']
    approximation: str | None = None

    @model_validator(mode='after')
    def scope(self):
        if self.approximation is not None:
            match = re.fullmatch(r'约\s*((?:0|[1-9][0-9]*)\.([0-9]{1,3}))\s*ms', self.approximation)
            if not match or Decimal(match[1]) != Decimal(str(self.value)):
                raise ValueError('invalid_approximation_literal')
        if self.statistic == 'reported_p95':
            if (self.method != 'reported' or self.population != 'alert_record' or
                    self.trace_id is not None or self.metric not in ('orders_ms', 'client_elapsed_ms')):
                raise ValueError('invalid_alert_scope')
            return self
        if self.population == 'alert_record' or self.metric == 'client_elapsed_ms':
            raise ValueError('invalid_observation_scope')
        if self.statistic == 'p95':
            if self.method != 'nearest_rank' or self.trace_id is not None or self.population == 'request':
                raise ValueError('invalid_p95_scope')
        elif self.method != 'none' or self.trace_id is None or self.population != 'request':
            raise ValueError('invalid_request_scope')
        return self


def verify_claim(claim, *, snapshot_id, evidence):
    """Evidence must come from the active executor, never from the claim author.

    Unsupported provenance/scope yields insufficient evidence, not a numeric
    contradiction. An explicit approximate literal is rounded at its written
    precision. The caller must preserve it from the source text; this function
    does not authenticate natural-language extraction. No arbitrary tolerance.
    """
    claim = StatisticalClaim.model_validate(claim)
    digits = len(re.fullmatch(r'约\s*(?:0|[1-9][0-9]*)\.([0-9]{1,3})\s*ms', claim.approximation)[1]) if claim.approximation else 3

    def result(status, reason, expected=None):
        return dict(protocol='statistical-claim-v2', claim=claim.model_dump(),
                    status=status, reason=reason, expected_value=expected,
                    precision_ms=10 ** -digits,
                    scope='reported_alert_only' if claim.statistic == 'reported_p95' else 'observed_evidence')

    if claim.snapshot_id != snapshot_id:
        return result('insufficient_evidence', 'snapshot_mismatch')
    matches = [e for e in evidence if e['id'] == claim.evidence_id]
    if len(matches) != 1:
        return result('insufficient_evidence', 'unobserved_reference')
    item = matches[0]
    if claim.statistic == 'reported_p95':
        if item['tool'] != 'get_alert' or claim.window != 'incident':
            return result('insufficient_evidence', 'alert_scope_mismatch')
        # Active alerts contain driver-side client_p95_ms, NOT server orders_ms.
        # Older acquired alert envelopes explicitly name orders_p95_ms.
        data = item['data'].get('alert', item['data'])
        field = 'client_p95_ms' if claim.metric == 'client_elapsed_ms' else 'orders_p95_ms'
        expected = data.get(field)
        if expected is None:
            return result('insufficient_evidence', 'metric_unavailable')
        return result('supported' if _matches(claim, expected, digits) else 'contradicted',
                      'reported_value_matches' if _matches(claim, expected, digits) else 'reported_value_mismatch', expected)
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
    supported = _matches(claim, expected, digits)
    return result('supported' if supported else 'contradicted',
                  'value_matches' if supported else 'value_mismatch', expected)


def _matches(claim, expected, digits):
    if claim.approximation is None:
        return round(claim.value, 3) == round(expected, 3)
    quantum = Decimal(1).scaleb(-digits)
    return Decimal(str(expected)).quantize(quantum, rounding=ROUND_HALF_UP) == Decimal(str(claim.value))


class ClaimBinding(Strict):
    """Character offsets in the exact UTF-8-hashed report supplied by the caller."""
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=1000)
    claim: StatisticalClaim


_BOUND_P95 = re.compile(
    r'(?P<source>事件窗口采样|参考窗口采样|事件告警记录)'
    r'(?P<metric>订单|库存|发送前|客户端)\s*P95\s*'
    r'(?P<relation>约|为|=)\s*(?P<number>(?:0|[1-9][0-9]*)(?:\.[0-9]{1,3})?)\s*ms[。.!！]?')
_BOUND_METRICS = dict(订单='orders_ms', 库存='inventory_ms',
                      发送前='pre_send_ms', 客户端='client_elapsed_ms')


def verify_bound_claim(binding, *, report_text, snapshot_id, evidence):
    """Conservative controlled-sentence binding followed by evidence verification.

    The trusted caller supplies report_text and acquired evidence, not the
    candidate. Unsupported prose is NOT a false statement or an accepted report.
    This API checks one complete standalone sentence, not report completeness.
    """
    binding = ClaimBinding.model_validate(binding)

    def rejected(reason):
        return dict(protocol='claim-binding-v1', binding_status='unverified', reason=reason,
                    verification=None)

    if hashlib.sha256(report_text.encode('utf-8')).hexdigest() != binding.report_sha256:
        return rejected('report_hash_mismatch')
    if binding.end > len(report_text) or report_text[binding.start:binding.end] != binding.quote:
        return rejected('source_span_mismatch')
    # Do not take a positive fragment out of a negation, condition or comparison.
    # Only line boundaries and explicit Chinese sentence delimiters are accepted.
    before = report_text[:binding.start].rstrip(' \t')
    after = report_text[binding.end:].lstrip(' \t')
    if before and before[-1] not in '\n。！？':
        return rejected('incomplete_sentence')
    if after and after[0] != '\n' and binding.quote[-1] not in '。！？':
        return rejected('incomplete_sentence')
    match = _BOUND_P95.fullmatch(binding.quote)
    if match is None:
        return rejected('unsupported_expression')
    source = match['source']
    alert = source == '事件告警记录'
    expected = dict(
        window='reference' if source == '参考窗口采样' else 'incident',
        metric=_BOUND_METRICS[match['metric']],
        statistic='reported_p95' if alert else 'p95',
        population='alert_record' if alert else 'observed_non_null',
        method='reported' if alert else 'nearest_rank', trace_id=None,
        approximation=('约'+match['number']+'ms') if match['relation']=='约' else None)
    claim = binding.claim
    # Compare precision lexically as well as magnitude. 362.080 != "约362.08".
    actual = claim.model_dump()
    if (any(actual[k] != v for k,v in expected.items()) or
            Decimal(str(claim.value)) != Decimal(match['number'])):
        return rejected('claim_text_mismatch')
    checked = verify_claim(claim, snapshot_id=snapshot_id, evidence=evidence)
    return dict(protocol='claim-binding-v1', binding_status='bound', reason='literal_fields_match',
                report_sha256=binding.report_sha256, start=binding.start, end=binding.end,
                quote=binding.quote, verification=checked)
