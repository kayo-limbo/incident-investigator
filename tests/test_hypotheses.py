import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.investigation.agent import ModelFailure
from backend.investigation.evidence_models import WindowQuery
from backend.investigation.hypotheses import HypothesisLedger, HypothesisUpdate
from backend.investigation.hypothesis_agent import investigate_hypotheses
from backend.investigation.async_model import AsyncHypothesisChatModel
from backend.investigation.model import ModelConfig
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_cli import main
from backend.investigation.window_investigation import FrozenWindowSession
from backend.investigation.window_queries import WindowEvidenceTools
from evals.run_hypothesis_comparison import audit_history, replay
from test_window_investigation import observed, Scripted, finish


def update(key="slow", version=0, status="open", support=None, counter=None):
    return dict(id=key, expected_version=version, statement=key, affected_services=["inventory"],
                status=status, supporting_refs=support or [], counter_refs=counter or [],
                pending_checks=["检查其他样本"], change_reason="根据已采集字段更新")


def query(updates=None, letter="a", ids=None):
    return dict(kind="tool", tool="get_snapshot_trace", trace_id=letter * 32,
                updates=[{k: v for k, v in h.items() if k != "expected_version"} for h in updates or []],
                hypothesis_ids=ids or ["slow"], purpose="区分下游耗时和客户端等待")


class HypothesisTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SnapshotStore(Path(temp.name))
        tools = WindowEvidenceTools(self.store)
        with patch.object(tools, "_fetch_window", return_value={"data": [observed(slow=True), observed("b", failed=True)]}):
            self.key = tools.query_window(WindowQuery(start_us=100, end_us=200)).data["snapshot_id"]
        self.slow = dict(snapshot_id=self.key, trace_id="a" * 32, span_id="3", field="duration_ms")
        self.pool = dict(snapshot_id=self.key, trace_id="b" * 32, span_id="2", field="tags.error.type")

    def end(self, updates, ids=None, refs=None, code="inventory_latency"):
        return {**finish(self.key, code, refs if refs is not None else [self.slow]),
                "updates": [{k: v for k, v in h.items() if k != "expected_version"} for h in updates],
                "hypothesis_ids": ids if ids is not None else ["slow"]}

    def joint_model(self):
        return Scripted(query([update(), update("pool")], ids=["slow", "pool"]),
                        query([update(version=1, status="supported", support=[self.slow])], "b", ["pool"]),
                        self.end([update("pool", 1, "supported", [self.pool])], ["slow", "pool"],
                                 [self.slow, self.pool], "inventory_latency_with_pool_contention"))

    def test_joint_explanation_query_versions_and_independent_audit(self):
        model = self.joint_model()
        result = investigate_hypotheses(self.store, self.key, model)
        self.assertEqual(result.termination, "completed")
        self.assertEqual(result.claim_hypothesis_versions, {"slow": 2, "pool": 2})
        self.assertEqual([h.status for h in result.hypotheses], ["supported", "supported"])
        self.assertEqual(result.query_tasks[0].hypothesis_versions, {"slow": 1, "pool": 1})
        self.assertEqual(result.query_tasks[1].hypothesis_versions, {"pool": 1})
        self.assertEqual(result.usage_totals["tool_calls"], 3)
        self.assertEqual(result.usage_totals["model_calls"], 3)
        exposed = model.contexts[1].hypotheses[0].model_dump()
        self.assertNotIn("version", exposed)
        self.assertNotIn("expected_version", exposed)
        self.assertNotIn("turn", exposed)
        self.assertTrue(audit_history(result, self.store))
        for tamper in ("version", "evidence", "query"):
            changed = result.model_copy(deep=True)
            if tamper == "version":
                changed.hypothesis_history[-1].version = 8
            elif tamper == "evidence":
                changed.hypothesis_history[-1].evidence_count = 1
            else:
                changed.query_tasks[0].trace_id = "f" * 32
            self.assertFalse(audit_history(changed, self.store))

    def test_new_counter_evidence_can_reverse_status_without_erasing_history(self):
        session = FrozenWindowSession(self.store, self.key)
        session.drilldown("a" * 32)
        session.drilldown("b" * 32)
        ledger = HypothesisLedger().propose([HypothesisUpdate(**update(status="supported", support=[self.slow]))], session, 1)
        changed = ledger.propose([HypothesisUpdate(**update(version=1, status="refuted", counter=[self.pool]))], session, 2)
        self.assertEqual([h.status for h in changed.history], ["supported", "refuted"])
        self.assertEqual(ledger.current["slow"].status, "supported")
        exposed = changed.history
        exposed[0].supporting_refs.clear()
        self.assertEqual(len(changed.history[0].supporting_refs), 1)

    def test_batch_rejects_stale_version_and_unknown_id_without_partial_updates(self):
        session = FrozenWindowSession(self.store, self.key)
        session.drilldown("a" * 32)
        ledger = HypothesisLedger().propose([HypothesisUpdate(**update()), HypothesisUpdate(**update("pool"))], session, 1)
        with self.assertRaisesRegex(ValueError, "hypothesis_version_conflict"):
            ledger.propose([HypothesisUpdate(**update(version=1, status="supported", support=[self.slow])),
                            HypothesisUpdate(**update("pool", 9))], session, 2)
        self.assertEqual([h.version for h in ledger.current.values()], [1, 1])
        model = Scripted(query([update(), update("pool")]),
                         self.end([update(version=1, status="supported", support=[self.slow])], ["unknown"]))
        result = investigate_hypotheses(self.store, self.key, model)
        self.assertEqual(result.validation_error, "unknown_or_duplicate_hypothesis")
        self.assertEqual([h.version for h in result.hypotheses], [1, 1])
        self.assertEqual(result.finding.code, "insufficient_evidence")

    def test_unseen_cross_snapshot_missing_duplicate_and_conflicting_refs_rejected(self):
        for ref in (self.pool, {**self.slow, "snapshot_id": "f" * 64},
                    {**self.slow, "field": "tags.missing"}, {**self.slow, "span_id": "absent"}):
            model = Scripted(query([update()]), self.end([update(version=1, status="supported", support=[ref])]))
            self.assertEqual(investigate_hypotheses(self.store, self.key, model).validation_error, "invalid_reference")
        for support, counter in (([self.slow, self.slow], []), ([self.slow], [self.slow])):
            model = Scripted(query([update()]), self.end([update(version=1, status="supported", support=support, counter=counter)]))
            self.assertEqual(investigate_hypotheses(self.store, self.key, model).validation_error, "invalid_reference")

    def test_claim_cannot_bypass_hypotheses_or_use_unlinked_fields(self):
        status_ref = {**self.slow, "span_id": "1", "field": "tags.http.response.status_code"}
        for action in (self.end([], []), self.end([]),
                       self.end([update(version=1, status="supported", support=[self.slow])], refs=[status_ref])):
            result = investigate_hypotheses(self.store, self.key, Scripted(query([update()]), action))
            self.assertEqual(result.termination, "invalid_finding")
            self.assertEqual(result.hypotheses[0].version, 1)

    def test_hypothesis_identity_status_and_size_constraints(self):
        changed = update(version=1)
        changed["statement"] = "另一个命题"
        for item, error in ((changed, "hypothesis_identity_changed"),
                            (update(version=1, status="supported"), "support_required"),
                            (update(version=1, status="refuted"), "counter_evidence_required")):
            result = investigate_hypotheses(self.store, self.key, Scripted(query([update()]), self.end([item])))
            self.assertEqual(result.validation_error, error)
        result = investigate_hypotheses(self.store, self.key, Scripted(query([update(), update()])))
        self.assertEqual(result.validation_error, "hypothesis_limit_or_duplicate")
        first = [update("h" + str(i)) for i in range(6)]
        result = investigate_hypotheses(self.store, self.key, Scripted(query(first, ids=["h0"]), query([update()], ids=["slow"])))
        self.assertEqual(result.validation_error, "hypothesis_limit_or_duplicate")
        self.assertEqual(len(result.hypotheses), 6)

    def test_claim_records_refuted_alternative_and_schema_errors_are_safe(self):
        pre_send = {**self.slow, "span_id": "2", "field": "tags.client.pre_send_ms"}
        model = Scripted(query([update(), update("wait")]), self.end([
            update(version=1, status="supported", support=[self.slow]),
            update("wait", 1, "refuted", counter=[pre_send])], ["slow", "wait"], [self.slow, pre_send]))
        result = investigate_hypotheses(self.store, self.key, model)
        self.assertEqual(result.termination, "completed")
        self.assertEqual(result.claim_hypothesis_versions, {"slow": 2, "wait": 2})
        counter_only = self.end([update(version=1, status="supported", support=[self.slow], counter=[pre_send])], refs=[pre_send])
        result = investigate_hypotheses(self.store, self.key, Scripted(query([update()]), counter_only))
        self.assertEqual(result.validation_error, "claim_hypothesis_not_cited")
        broken = query([update()])
        broken["purpose"] = {"secret": "must_not_be_copied"}
        result = investigate_hypotheses(self.store, self.key, Scripted(broken))
        self.assertIn("tool.purpose:string_type", result.validation_issues)
        self.assertNotIn("must_not_be_copied", result.model_dump_json())
        broken = query([update()])
        broken["updates"][0]["version"] = 1
        result = investigate_hypotheses(self.store, self.key, Scripted(broken))
        self.assertIn("tool.updates.0.version:extra_forbidden", result.validation_issues)

    def test_rejected_query_cannot_commit_updates_and_context_is_isolated(self):
        for action in (query([update()], "f"), query([update(status="supported", support=[self.slow])])):
            result = investigate_hypotheses(self.store, self.key, Scripted(action))
            self.assertEqual(result.termination, "invalid_action")
            self.assertEqual(result.hypothesis_history, [])
            self.assertEqual(result.usage_totals["tool_calls"], 1)
        owner = self
        class Mutates(Scripted):
            def decide(self, context):
                if context.hypotheses:
                    context.hypotheses[0].statement = "tampered"
                    context.evidence.clear()
                    context.query_tasks.clear()
                return super().decide(context)
        model = Mutates(query([update()]), owner.end([update(version=1, status="supported", support=[self.slow])]))
        result = investigate_hypotheses(self.store, self.key, model)
        self.assertEqual(result.termination, "completed")
        self.assertEqual(result.hypotheses[0].statement, "slow")
        self.assertEqual(len(result.query_tasks), 1)

    def test_failed_and_unavailable_source_preserve_state_and_usage(self):
        class Fails(Scripted):
            def decide(self, context):
                if self.contexts:
                    raise ModelFailure("timeout")
                return super().decide(context)
        result = investigate_hypotheses(self.store, self.key, Fails(query([update()])))
        self.assertEqual(result.termination, "model_error")
        self.assertEqual(result.hypotheses[0].version, 1)
        self.assertEqual(result.usage_totals["model_calls"], 2)
        result = investigate_hypotheses(self.store, "f" * 64, Scripted())
        self.assertEqual(result.termination, "source_error")
        self.assertEqual(result.usage_totals["model_calls"], 0)

    def test_budget_keeps_last_finish_and_refusal_can_keep_open_hypotheses(self):
        refusal = self.end([], [], [], "insufficient_evidence")
        for last, termination in ((query(), "budget_exhausted"), (refusal, "completed")):
            result = investigate_hypotheses(self.store, self.key, Scripted(query([update()]), last), 2)
            self.assertEqual(result.termination, termination)
            self.assertEqual(result.usage_totals["tool_calls"], 2)
            self.assertEqual(result.hypotheses[0].status, "open")

    def test_healthy_finding_can_reference_refuted_abnormal_hypothesis(self):
        tools = WindowEvidenceTools(self.store)
        with patch.object(tools, "_fetch_window", return_value={"data": [observed()]}):
            self.key = tools.query_window(WindowQuery(start_us=100, end_us=200)).data["snapshot_id"]
        self.slow["snapshot_id"] = self.key
        action = self.end([update(version=1, status="refuted", counter=[self.slow])], code="no_anomaly_observed")
        result = investigate_hypotheses(self.store, self.key, Scripted(query([update()]), action))
        self.assertEqual(result.termination, "completed")

    def test_http_schema_cli_and_token_accounting(self):
        config = ModelConfig(base_url="https://example.test", model="test", api_key="secret")
        actions = iter([query([update()]), self.end([update(version=1, status="supported", support=[self.slow])])])
        def handler(request):
            body = json.loads(request.content)
            self.assertIn("supporting_refs", body["messages"][0]["content"])
            self.assertNotIn("expected", json.loads(body["messages"][1]["content"]))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(next(actions))}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})
        output = self.store.directory / "report.private.json"
        adapter = AsyncHypothesisChatModel(config, transport=httpx.MockTransport(handler))
        with patch("backend.investigation.window_cli.AsyncHypothesisChatModel", return_value=adapter), \
                patch("backend.investigation.window_cli.ModelConfig.from_env", return_value=config), \
                patch("sys.argv", ["window", "--store", str(self.store.directory), "investigate", self.key,
                                   "--strategy", "hypotheses", "--output", str(output)]), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result["usage_totals"]["total_tokens"], 60)

    def test_replay_does_not_pass_labels_to_models_and_preserves_source(self):
        source, output = self.store.directory / "source.private.json", self.store.directory / "out.private.json"
        source.write_text(json.dumps({"cases": [{"name": "secret_truth", "snapshot_id": self.key,
            "expected": "inventory_latency_with_pool_contention"}]}))
        original = source.read_bytes()
        model = self.joint_model()
        config = ModelConfig(base_url="https://example.test", model="test", api_key="secret")
        with patch("evals.run_hypothesis_comparison.HypothesisChatModel") as hypo, \
                patch("evals.run_hypothesis_comparison.WindowChatModel") as react, redirect_stdout(io.StringIO()):
            hypo.return_value.__enter__.return_value = model
            react.return_value.__enter__.return_value = Scripted(finish(self.key, "insufficient_evidence", []))
            self.assertEqual(replay(source, output, self.store, config), 1)
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(json.loads(output.read_text(encoding="utf-8"))["cases"][0]["passed"]["hypotheses"])
        for context in model.contexts:
            self.assertNotIn("secret_truth", context.model_dump_json())
        with self.assertRaises(ValueError):
            replay(source, output, self.store, config)


if __name__ == "__main__":
    unittest.main()
