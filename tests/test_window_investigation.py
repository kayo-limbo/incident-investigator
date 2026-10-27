import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from contextlib import redirect_stdout

import httpx

from backend.investigation.evidence_models import WindowQuery
from backend.investigation.model import ModelConfig
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.usage import CallUsage, Usage, token_usage
from backend.investigation.window_baseline import investigate_window_fixed
from backend.investigation.window_investigation import FrozenWindowSession, investigate_window_agent
from backend.investigation.window_model import WindowChatModel
from backend.investigation.window_queries import WindowEvidenceTools
from backend.investigation.window_cli import main as cli_main
from evals.run_window_comparison import replay, score
from test_window_evidence import trace


def observed(letter="a", slow=False, wait=False, failed=False):
    raw = trace(letter, failed=failed)
    client = raw["spans"][1]
    client["references"] = [{"refType": "CHILD_OF", "traceID": letter * 32, "spanID": "1"}]
    client["tags"].append({"key": "client.pool_timeout_ms", "value": 250})
    if not failed:
        raw["spans"][2]["references"] = [{"refType": "CHILD_OF", "traceID": letter * 32, "spanID": "2"}]
        raw["spans"][2]["duration"] = 500_000 if slow else 80_000
        for t in client["tags"]:
            if t["key"] == "client.pre_send_ms":
                t["value"] = 550 if wait else 5
    raw["spans"][0]["duration"] = 600_000 if slow or wait else 90_000
    return raw


def finish(key, code="no_anomaly_observed", refs=None):
    return {"kind": "finish", "finding": {"code": code, "summary": "窗口观测结论", "limitations": [],
            "references": refs if refs is not None else [{"snapshot_id": key, "trace_id": "a" * 32,
                                                          "span_id": "1", "field": "tags.http.response.status_code"}]}}


class Scripted:
    def __init__(self, *actions):
        self.actions = iter(actions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context.model_copy(deep=True))
        return next(self.actions)


class WindowInvestigationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SnapshotStore(Path(temp.name))

    def freeze(self, entries=None, limit=100):
        tools = WindowEvidenceTools(self.store)
        with patch.object(tools, "_fetch_window", return_value={"data": entries if entries is not None else [observed()]}):
            return tools.query_window(WindowQuery(start_us=100, end_us=200, limit=limit)).data["snapshot_id"]

    def test_rule_classifies_four_patterns_and_validates_references(self):
        for entries, expected in (([observed()], "no_anomaly_observed"),
                                  ([observed(wait=True)], "pre_send_delay"),
                                  ([observed(slow=True)], "inventory_latency"),
                                  ([observed(slow=True), observed("b", failed=True)], "inventory_latency_with_pool_contention")):
            key = self.freeze(entries)
            report = investigate_window_fixed(self.store, key)
            self.assertEqual(report.finding.code, expected)
            self.assertEqual(report.termination, "completed")
            self.assertTrue(report.finding.references)
            for ref in report.finding.references:
                self.store.resolve(ref)
            self.assertLessEqual(report.usage_totals["tool_calls"], 6)
            self.assertEqual(report.usage_totals["model_calls"], 0)
            self.assertEqual(report.usage_totals["total_tokens"], 0)

    def test_partial_empty_missing_spans_and_small_pool_do_not_invent_cause(self):
        missing = observed()
        missing["spans"].pop()
        for key in (self.freeze([]), self.freeze(limit=1), self.freeze([missing])):
            self.assertEqual(investigate_window_fixed(self.store, key).finding.code, "insufficient_evidence")
        small = observed()
        for tag in small["spans"][1]["tags"]:
            if tag["key"] == "client.max_connections":
                tag["value"] = 1
        self.assertEqual(investigate_window_fixed(self.store, self.freeze([small])).finding.code, "no_anomaly_observed")

    def test_model_roundtrip_same_summary_as_rule_and_usage(self):
        key = self.freeze()
        model = Scripted({"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32}, finish(key))
        agent = investigate_window_agent(self.store, key, model)
        rule = investigate_window_fixed(self.store, key)
        self.assertEqual(agent.termination, "completed")
        self.assertEqual(agent.evidence[0].data, rule.evidence[0].data)
        self.assertEqual(agent.usage_totals["model_calls"], 2)
        self.assertEqual(agent.usage_totals["tool_calls"], 2)
        self.assertEqual(agent.usage_totals["drilldown_calls"], 1)
        self.assertIsNone(agent.usage_totals["total_tokens"])

    def test_outside_trace_and_tool_or_snapshot_injection_are_rejected(self):
        key = self.freeze()
        for action in ({"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "b" * 32},
                       {"kind": "tool", "tool": "get_trace", "trace_id": "a" * 32},
                       {"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32, "snapshot_id": "b" * 64}):
            report = investigate_window_agent(self.store, key, Scripted(action))
            self.assertEqual(report.termination, "invalid_action")
            self.assertEqual(report.usage_totals["drilldown_calls"], 0)

    def test_reference_paths_are_explicit_and_bare_tag_is_rejected(self):
        key = self.freeze()
        session = FrozenWindowSession(self.store, key)
        detail = session.drilldown("a" * 32)
        self.assertIn("tags.client.pre_send_ms", detail.data["spans"][1]["reference_fields"])
        action = finish(key)
        action["finding"]["references"][0]["field"] = "client.pre_send_ms"
        model = Scripted({"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32}, action)
        self.assertEqual(investigate_window_agent(self.store, key, model).termination, "invalid_action")

    def test_finding_must_reference_seen_fields_in_bound_snapshot(self):
        key = self.freeze()
        bad_refs = []
        base = finish(key)["finding"]["references"][0]
        for updates in ({"snapshot_id": "b" * 64}, {"trace_id": "b" * 32},
                        {"span_id": "999"}, {"field": "tags.missing"}):
            bad_refs.append([{**base, **updates}])
        bad_refs.extend(([base, base], []))
        for refs in bad_refs:
            model = Scripted({"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32}, finish(key, refs=refs))
            self.assertEqual(investigate_window_agent(self.store, key, model).termination, "invalid_finding")
        self.assertEqual(investigate_window_agent(self.store, key, Scripted(finish(key))).termination, "invalid_finding")
        rejected = investigate_window_agent(self.store, key, Scripted(finish(key)))
        self.assertEqual(rejected.rejected_finding.code, "no_anomaly_observed")
        self.assertEqual(rejected.finding.code, "insufficient_evidence")

    def test_partial_snapshot_cannot_support_definite_model_finding(self):
        key = self.freeze(limit=1)
        model = Scripted({"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32}, finish(key))
        self.assertEqual(investigate_window_agent(self.store, key, model).termination, "invalid_finding")
        report = investigate_window_agent(self.store, key, Scripted(finish(key, "insufficient_evidence", [])))
        self.assertEqual(report.termination, "completed")

    def test_budget_counts_summary_and_repeats_and_preserves_final_turn(self):
        key = self.freeze()
        drill = {"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32}
        report = investigate_window_agent(self.store, key, Scripted(drill, drill), max_tool_calls=2)
        self.assertEqual(report.termination, "budget_exhausted")
        self.assertEqual(report.usage_totals["tool_calls"], 2)
        report = investigate_window_agent(self.store, key, Scripted(drill, finish(key)), max_tool_calls=2)
        self.assertEqual(report.termination, "completed")
        self.assertEqual(investigate_window_fixed(self.store, key, 1).termination, "budget_exhausted")

    def test_context_and_disk_mutations_do_not_change_loaded_session(self):
        key = self.freeze()
        session = FrozenWindowSession(self.store, key)
        context = session.context()
        context.evidence[0].data.clear()
        self.assertIn("summary", session.context().evidence[0].data)
        (self.store.directory / f"{key}.private.json").write_text("{}")
        self.assertEqual(session.drilldown("a" * 32).data["trace_id"], "a" * 32)
        self.assertEqual(FrozenWindowSession(self.store, key).evidence[0].status, "error")

    def test_snapshot_failure_does_not_call_model(self):
        model = Scripted()
        report = investigate_window_agent(self.store, "b" * 64, model)
        self.assertEqual(report.termination, "source_error")
        self.assertEqual(report.usage_totals["model_calls"], 0)
        self.assertEqual(report.usage_totals["tool_calls"], 1)

    def test_http_adapter_usage_on_success_truncation_and_failure(self):
        key = self.freeze()
        config = ModelConfig(base_url="https://example.test", model="test", api_key="secret")
        usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
        def response(reason="stop", with_usage=True):
            return httpx.Response(200, json={"choices": [{"finish_reason": reason,
                "message": {"content": json.dumps(finish(key, "insufficient_evidence", []))}}],
                **({"usage": usage} if with_usage else {})})
        responses = iter([response(), response("length"), httpx.Response(500), response(with_usage=False)])
        def handle(request):
            payload = json.loads(request.content)
            self.assertIn("get_snapshot_trace", payload["messages"][0]["content"])
            return next(responses)
        with WindowChatModel(config, transport=httpx.MockTransport(handle)) as model:
            first = investigate_window_agent(self.store, key, model)
            self.assertEqual(first.usage_totals["total_tokens"], 30)
            truncated = investigate_window_agent(self.store, key, model)
            self.assertEqual(truncated.termination, "model_error")
            self.assertEqual(truncated.usage_totals["total_tokens"], 30)
            failed = investigate_window_agent(self.store, key, model)
            self.assertIsNone(failed.usage_totals["total_tokens"])
            self.assertEqual(failed.usage_totals["model_calls"], 1)
            missing = investigate_window_agent(self.store, key, model)
            self.assertIsNone(missing.usage_totals["total_tokens"])

    def test_usage_rejects_bad_numbers_and_does_not_fill_unknown_as_zero(self):
        self.assertIsNone(token_usage({"total_tokens": True})["total_tokens"])
        self.assertIsNone(token_usage({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 3})["total_tokens"])
        ledger = Usage(calls=[CallUsage(kind="model", name="decide", status="ok", elapsed_ms=1,
                                       prompt_tokens=10, completion_tokens=2, total_tokens=12),
                             CallUsage(kind="model", name="decide", status="error", elapsed_ms=1)])
        self.assertIsNone(ledger.totals()["total_tokens"])
        self.assertEqual(ledger.totals()["unknown_usage_calls"], 1)

    def test_evaluator_rejects_failed_or_invalid_evidence_even_if_label_matches(self):
        key = self.freeze()
        report = investigate_window_fixed(self.store, key)
        self.assertTrue(score(report, "no_anomaly_observed", key, self.store))
        report.termination = "model_error"
        self.assertFalse(score(report, "no_anomaly_observed", key, self.store))
        report.termination = "completed"
        report.finding.references[0] = report.finding.references[0].model_copy(update={"field": "tags.invented"})
        self.assertFalse(score(report, "no_anomaly_observed", key, self.store))

    def test_cli_fixed_and_agent_share_snapshot_and_write_report(self):
        key = self.freeze()
        for strategy in ("fixed", "agent"):
            output = self.store.directory / strategy / "report.private.json"
            model = Scripted({"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32}, finish(key))
            with patch("sys.argv", ["window", "--store", str(self.store.directory), "investigate", key,
                                    "--strategy", strategy, "--output", str(output)]), \
                    patch("backend.investigation.window_cli.ModelConfig.from_env"), \
                    patch("backend.investigation.window_cli.AsyncWindowChatModel") as adapter, \
                    redirect_stdout(io.StringIO()):
                model.decide = AsyncMock(side_effect=model.decide)
                adapter.return_value.__aenter__.return_value = model
                self.assertEqual(cli_main(), 0)
                self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["snapshot_id"], key)
                if strategy == "fixed":
                    adapter.assert_not_called()

    def test_replay_uses_existing_snapshot_without_environment_mutations(self):
        key = self.freeze()
        source = self.store.directory / "source.private.json"
        output = self.store.directory / "replay.private.json"
        source.write_text(json.dumps({"cases": [{"name": "healthy", "snapshot_id": key,
                                                "expected": "no_anomaly_observed"}]}))
        original = source.read_bytes()
        with patch("evals.run_window_comparison.compose") as compose, redirect_stdout(io.StringIO()):
            self.assertEqual(replay(source, output, self.store, None, 6), 0)
            compose.assert_not_called()
        self.assertEqual(source.read_bytes(), original)
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(result["environment_modified"])
        self.assertTrue(result["cases"][0]["passed"]["fixed"])


if __name__ == "__main__":
    unittest.main()
