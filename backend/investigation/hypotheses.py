"""版本化假设账本。版本与引用约束不等同于语义或因果正确性。"""

import copy
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .window_investigation import WindowReference

HypothesisId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
ShortText = Annotated[str, Field(min_length=1, max_length=300)]


class HypothesisState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: HypothesisId
    statement: ShortText
    affected_services: list[Literal["orders", "inventory"]] = Field(min_length=1, max_length=2)
    status: Literal["open", "supported", "refuted", "inconclusive"]
    supporting_refs: list[WindowReference] = Field(max_length=6)
    counter_refs: list[WindowReference] = Field(max_length=6)
    pending_checks: list[ShortText] = Field(max_length=4)
    change_reason: ShortText


class HypothesisUpdate(HypothesisState):
    expected_version: int = Field(ge=0)


class HypothesisRevision(HypothesisUpdate):
    version: int
    turn: int
    evidence_count: int


def reference_key(ref):
    return ref.snapshot_id, ref.trace_id, ref.span_id, ref.field


class HypothesisLedger:
    """单调查内存账本；批量校验后一次替换，不是数据库并发控制。"""

    def __init__(self):
        self._history = []
        self._current = {}

    @property
    def history(self):
        return copy.deepcopy(self._history)

    @property
    def current(self):
        return copy.deepcopy(self._current)

    def propose(self, updates, session, turn):
        """返回独立候选账本；整个动作验证成功后由执行器接纳。"""
        candidate = copy.deepcopy(self)
        ids = [update.id for update in updates]
        if len(ids) != len(set(ids)) or len(set(ids) | self._current.keys()) > 6:
            raise ValueError("hypothesis_limit_or_duplicate")
        for update in updates:
            old = self._current.get(update.id)
            if update.expected_version != (old.version if old else 0):
                raise ValueError("hypothesis_version_conflict")
            if old and (old.statement != update.statement or old.affected_services != update.affected_services):
                raise ValueError("hypothesis_identity_changed")
            # 合并校验也禁止同一版本中一个字段同时充当支持与反证。
            session.validate_references(update.supporting_refs + update.counter_refs)
            if update.status == "supported" and not update.supporting_refs:
                raise ValueError("support_required")
            if update.status == "refuted" and not update.counter_refs:
                raise ValueError("counter_evidence_required")
            revision = HypothesisRevision(**update.model_dump(), version=update.expected_version + 1,
                                          turn=turn, evidence_count=len(session.evidence))
            candidate._current[update.id] = revision
            candidate._history.append(revision)
        return candidate

    def versions(self, ids):
        if len(ids) != len(set(ids)) or any(i not in self._current for i in ids):
            raise ValueError("unknown_or_duplicate_hypothesis")
        return {i: self._current[i].version for i in ids}

    def validate_claim(self, finding, ids):
        versions = self.versions(ids)
        if finding.code == "insufficient_evidence":
            return versions
        if not ids:
            raise ValueError("claim_hypothesis_required")
        eligible = set()
        supported = False
        for key in ids:
            hypothesis = self._current[key]
            if hypothesis.status not in ("supported", "refuted"):
                raise ValueError("claim_hypothesis_unresolved")
            supported |= hypothesis.status == "supported"
            eligible.update(map(reference_key, hypothesis.supporting_refs + hypothesis.counter_refs))
        if finding.code != "no_anomaly_observed" and not supported:
            raise ValueError("claim_support_required")
        refs = set(map(reference_key, finding.references))
        if not refs <= eligible:
            raise ValueError("claim_reference_not_linked")
        # 每个入选解释都须出现在结论的具体字段引用中。
        for key in ids:
            h = self._current[key]
            used = h.supporting_refs if h.status == "supported" else h.counter_refs
            if not refs.intersection(map(reference_key, used)):
                raise ValueError("claim_hypothesis_not_cited")
        return versions
