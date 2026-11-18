"""Render a human-reviewed cross-request mechanism in a historical window summary."""
from hashlib import sha256


def render_observational_hypothesis(summary, *, summary_sha256, start, end, quote):
    """Qualify one reviewed assertion that slow inventory caused other pool timeouts.

    The caller establishes the relation and the observational-only evidence scope.
    The digest and offsets bind the edit to the reviewed text, not its interpretation.
    This is an offline rendering operation, not a free-text claim detector.
    """
    if sha256(summary.encode("utf-8")).hexdigest() != summary_sha256:
        raise ValueError("summary_version_mismatch")
    if not (0 <= start < end <= len(summary)) or summary[start:end] != quote:
        raise ValueError("reviewed_span_mismatch")
    if start and summary[start - 1] not in "。：\n":
        raise ValueError("reviewed_span_starts_inside_clause")
    if not quote.endswith("。") or "。" in quote[:-1]:
        raise ValueError("expected_single_statement")
    return (summary[:start] + "待验证的跨请求解释为：“" + quote
            + "”现有观测尚无法确认这一因果关系。" + summary[end:])
