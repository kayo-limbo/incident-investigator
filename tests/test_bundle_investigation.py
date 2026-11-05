import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx
from fastapi import FastAPI

from backend.investigation.async_investigation import InvestigationCancelled
from backend.investigation.bundle_agent import (
    BundleChatModel, BundleHypothesisModel, BundleDomain, BundleFinding, investigate_bundle,
)
from backend.investigation.bundle_baseline import investigate_bundle_fixed
from backend.investigation.bundle_evidence import BundleSession, BundleReference, EvidenceBundle, build_bundle
from backend.investigation.evidence_models import WindowQuery
from backend.investigation.model import ModelConfig
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_queries import WindowEvidenceTools
from evals.run_bundle_comparison import freeze, validate_plan, score, collect, evaluate
from evals.summarize_bundle_comparison import summarize as summarize_run
from lab.orders.runtime_observation import initialize, install
from test_window_investigation import observed


def reading(at, finished=0):
    return dict(process_id="f" * 32, started_us=1, observed_us=at,
        max_connections=1, pool_timeout_ms=1000, requests_started=finished,
        requests_finished=finished, in_flight=0, peak_in_flight=1 if finished else 0)


def finish(key, code="inventory_latency", hypotheses=False):
    ref = dict(snapshot_id=key, source="trace_window", field="requests.0.inventory_ms")
    result = dict(kind="finish", finding=dict(code=code, summary="库存处理耗时升高。",
        references=[ref] if code != "insufficient_evidence" else [], limitations=[]))
    if hypotheses:
        result.update(hypothesis_ids=["slow"] if code != "insufficient_evidence" else [], updates=[
            hypothesis(ref)] if code != "insufficient_evidence" else [])
    return result


def hypothesis(ref=None):
    return dict(id="slow", statement="库存处理耗时升高", affected_services=["inventory"],
        status="supported" if ref else "open", supporting_refs=[ref] if ref else [],
        counter_refs=[], pending_checks=[] if ref else ["查询请求分段"], change_reason="检查证据")


class Scripted:
    last_usage = dict(prompt_tokens=10, completion_tokens=10, total_tokens=20)

    def __init__(self, *actions):
        self.actions = iter(actions)
        self.contexts = []

    async def decide(self, context):
        self.contexts.append(context.model_copy(deep=True))
        return next(self.actions)


class BundleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SnapshotStore(Path(self.temp.name))

    def payload(self, *, slow=True, wait=False, missing=False):
        tools = WindowEvidenceTools(self.store)
        with patch.object(tools, "_fetch_window", return_value={"data": [observed(slow=slow, wait=wait)]}):
            key = tools.query_window(WindowQuery(start_us=100, end_us=200)).data["snapshot_id"]
        raw = self.store.read(key)
        raw["truth"] = "must never enter"
        return build_bundle(raw, reading(90), reading(210, 1), traces_available=not missing)

    def key(self, **kwargs):
        return self.store.freeze(self.payload(**kwargs))

    def test_projection_is_strict_and_mask_has_no_backdoor(self):
        payload = self.payload(missing=True)
        self.assertEqual(payload["trace_window"]["requests"], [])
        self.assertNotIn("truth", json.dumps(payload))
        self.assertNotIn("original_snapshot", json.dumps(payload))
        for path in ("truth", "delay"):
            extra = copy.deepcopy(payload)
            extra[path] = 350
            with self.assertRaises(ValueError):
                EvidenceBundle.model_validate(extra)
        bad = copy.deepcopy(payload)
        bad["client_runtime"]["after"]["process_id"] = "e" * 32
        with self.assertRaises(ValueError):
            EvidenceBundle.model_validate(bad)
        bad = copy.deepcopy(payload)
        bad["client_runtime"]["after"]["observed_us"] = 150
        with self.assertRaises(ValueError):
            EvidenceBundle.model_validate(bad)

    def test_query_copies_budget_repeat_and_reference_scope(self):
        key = self.key()
        session = BundleSession(self.store, key, 3)
        ref = BundleReference(snapshot_id=key, source="trace_window", field="requests.0.inventory_ms")
        with self.assertRaisesRegex(ValueError, "invalid_reference"):
            session.validate_references([ref])
        evidence = session.query("trace_window")
        evidence.data["requests"][0]["inventory_ms"] = 0
        self.assertEqual(session.resolve(ref), 500)
        with self.assertRaisesRegex(ValueError, "repeated_query"):
            session.query("trace_window")
        for changed in (ref.model_copy(update={"snapshot_id": "0" * 64}),
                        ref.model_copy(update={"field": "requests.0.truth"})):
            with self.assertRaises(ValueError):
                session.validate_references([changed])
        with self.assertRaises(ValueError):
            session.validate_references([ref, ref])
        session.query("client_runtime")
        with self.assertRaisesRegex(ValueError, "budget_exhausted"):
            session.query("client_runtime")

    def test_fixed_control_wait_slow_and_missing(self):
        for options, code in ((dict(slow=False), "no_anomaly_observed"),
                              (dict(slow=False, wait=True), "pre_send_delay"),
                              (dict(slow=True), "inventory_latency"),
                              (dict(missing=True), "insufficient_evidence")):
            key = self.key(**options)
            result = investigate_bundle_fixed(self.store, key)
            self.assertEqual(result.finding.code, code)
            self.assertEqual(result.usage_totals["tool_calls"], 3)
            self.assertTrue(score(result, dict(snapshot_id=key, expected=code), self.store))
            tampered = result.model_copy(deep=True)
            tampered.evidence[1].data["quality"] = "invented"
            self.assertFalse(score(tampered, dict(snapshot_id=key, expected=code), self.store))

    async def test_three_strategies_share_initial_input_and_multisource_refs(self):
        key = self.key()
        fixed = investigate_bundle_fixed(self.store, key)
        for strategy in ("agent", "hypotheses"):
            query = dict(kind="tool", tool="trace_window")
            runtime = dict(kind="tool", tool="client_runtime")
            if strategy == "hypotheses":
                query.update(updates=[hypothesis()], hypothesis_ids=["slow"], purpose="区分下游与调用方")
                runtime.update(updates=[], hypothesis_ids=["slow"], purpose="核对配置与请求压力")
            final = finish(key, hypotheses=strategy == "hypotheses")
            model = Scripted(query, runtime, final)
            report = await investigate_bundle(self.store, key, model, strategy)
            self.assertEqual(report.termination, "completed")
            self.assertEqual(report.evidence[0].data, fixed.evidence[0].data)
            self.assertEqual(report.usage_totals["model_calls"], 3)
            self.assertEqual(report.usage_totals["tool_calls"], 3)
            if strategy == "hypotheses":
                self.assertEqual([h.version for h in report.hypothesis_history], [1, 2])

    async def test_unknown_tool_or_unseen_ref_does_not_execute(self):
        key = self.key()
        for action, termination in ((dict(kind="tool", tool="shell"), "invalid_action"),
                                     (finish(key), "invalid_finding")):
            report = await investigate_bundle(self.store, key, Scripted(action))
            self.assertEqual(report.termination, termination)
            self.assertEqual(report.usage_totals["tool_calls"], 1)

    async def test_missing_trace_cannot_be_replaced_by_runtime_config(self):
        key = self.key(missing=True)
        action = finish(key)
        action["finding"]["references"] = [dict(snapshot_id=key, source="client_runtime", field="after.max_connections")]
        report = await investigate_bundle(self.store, key,
            Scripted(dict(kind="tool", tool="client_runtime"), action))
        self.assertEqual(report.termination, "invalid_finding")
        self.assertEqual(report.validation_error, "insufficient_observations")
        good = await investigate_bundle(self.store, key, Scripted(finish(key, "insufficient_evidence")))
        self.assertEqual(good.termination, "completed")

    async def test_repeat_rejects_attached_hypothesis_update(self):
        key = self.key()
        query = dict(kind="tool", tool="trace_window", purpose="调查", hypothesis_ids=["slow"], updates=[hypothesis()])
        ref = dict(snapshot_id=key, source="trace_window", field="requests.0.inventory_ms")
        second = {**query, "updates": [hypothesis(ref)]}
        report = await investigate_bundle(self.store, key, Scripted(query, second), "hypotheses")
        self.assertEqual(report.termination, "repeated_query")
        self.assertEqual(len(report.hypothesis_history), 1)
        self.assertEqual(report.hypothesis_history[0].status, "open")

    async def test_token_unknown_and_exhausted_reject_actions(self):
        key = self.key()
        query = dict(kind="tool", tool="trace_window")
        model = Scripted(query)
        report = await investigate_bundle(self.store, key, model, max_total_tokens=20)
        self.assertEqual(report.termination, "token_budget_exhausted")
        self.assertEqual(report.usage_totals["tool_calls"], 1)
        model = Scripted(query)
        model.last_usage = None
        report = await investigate_bundle(self.store, key, model)
        self.assertEqual(report.termination, "usage_unavailable")

    async def test_deadline_and_cancel_keep_partial_usage(self):
        key = self.key()
        entered = asyncio.Event()
        class Slow:
            last_usage = None
            async def decide(self, context):
                entered.set()
                await asyncio.Event().wait()
        report = await investigate_bundle(self.store, key, Slow(), timeout_seconds=.05)
        self.assertEqual(report.termination, "deadline_exceeded")
        self.assertEqual(report.usage_totals["tool_calls"], 1)
        entered.clear()
        task = asyncio.create_task(investigate_bundle(self.store, key, Slow()))
        await entered.wait()
        task.cancel()
        with self.assertRaises(InvestigationCancelled) as caught:
            await task
        self.assertEqual(caught.exception.report.usage_totals["model_calls"], 1)
        self.assertEqual(caught.exception.report.usage_totals["unknown_usage_calls"], 1)

    async def test_real_adapter_mock_http_uses_new_schema_and_records_usage(self):
        key = self.key()
        config = ModelConfig("http://localhost", "test", "test-key")
        for adapter in (BundleChatModel, BundleHypothesisModel):
            requests = []
            def handler(request):
                requests.append(json.loads(request.content))
                return httpx.Response(200, json=dict(
                    choices=[dict(finish_reason="stop", message=dict(content=json.dumps(finish(key, "insufficient_evidence",
                        hypotheses=adapter is BundleHypothesisModel))))],
                    usage=dict(prompt_tokens=10, completion_tokens=10, total_tokens=20)))
            async with adapter(config, transport=httpx.MockTransport(handler)) as model:
                report = await investigate_bundle(self.store, key, model,
                    "hypotheses" if adapter is BundleHypothesisModel else "agent")
            self.assertEqual(report.termination, "completed")
            self.assertIn("client_runtime", requests[0]["messages"][0]["content"])
            self.assertNotIn("inventory_ms", requests[0]["messages"][1]["content"])
            self.assertEqual(report.usage_totals["total_tokens"], 20)

    def test_protocol_freeze_refuses_changes_and_overwrite(self):
        directory = Path(self.temp.name) / "run"
        config = ModelConfig("http://localhost", "test", "test-key")
        freeze(directory, config)
        validate_plan(directory, config)
        with self.assertRaises(FileExistsError):
            freeze(directory, config)
        with patch("evals.run_bundle_comparison.BUDGET", {"max_tool_calls": 100}):
            with self.assertRaisesRegex(ValueError, "frozen_protocol_changed"):
                validate_plan(directory, config)

    def test_failed_collection_preserves_failure_and_runs_recovery(self):
        directory = Path(self.temp.name) / "collection"
        freeze(directory, ModelConfig("http://localhost", "test", "test-key"))
        response = httpx.Response(200, json={"sku": "demo", "can_order": True})
        runtime = httpx.Response(200, json={"max_connections": 100, "pool_timeout_ms": 2000})
        client = MagicMock()
        client.__enter__.return_value.get.side_effect = [response, runtime]
        with patch("evals.run_bundle_comparison.compose", side_effect=[RuntimeError("injected"), ""]) as compose:
            with patch("evals.run_bundle_comparison.httpx.Client", return_value=client):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    collect(directory, self.store)
        saved = json.loads((directory / "collection.private.json").read_text())
        self.assertFalse(saved["completed"])
        self.assertEqual(saved["error"], "RuntimeError")
        self.assertTrue(saved["recovery_verified"])
        self.assertEqual(compose.call_count, 2)
        with self.assertRaises(FileExistsError):
            collect(directory, self.store)

    async def test_evaluation_preserves_reports_and_refuses_overwrite(self):
        key = self.key()
        directory = Path(self.temp.name) / "evaluation"
        directory.mkdir()
        manifest = dict(cases=[dict(name="test", snapshot_id=key, expected="inventory_latency")])
        with patch("evals.run_bundle_comparison.validate_collection", return_value=manifest):
            with patch("evals.run_bundle_comparison.validate_plan"):
                result = await evaluate(directory, self.store)
                self.assertTrue(result["completed"])
                self.assertTrue(result["runs"][0]["passed"])
                with patch("evals.summarize_bundle_comparison.validate_collection", return_value=manifest):
                    summary = summarize_run(directory, self.store, fixed_only=True)
                    self.assertEqual(summary["groups"]["fixed"]["label_protocol_passed"], 1)
                    result["runs"][0]["passed"] = False
                    (directory / "fixed-reports/result.private.json").write_text(json.dumps(result), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "saved_result_mismatch"):
                        summarize_run(directory, self.store, fixed_only=True)
                with self.assertRaises(FileExistsError):
                    await evaluate(directory, self.store)

    async def test_runtime_counters_are_observed_not_injection_values(self):
        app = FastAPI()
        initialize(app, 2, 800)
        install(app)
        ready, release = asyncio.Event(), asyncio.Event()
        @app.get("/orders/preview")
        async def endpoint():
            ready.set()
            await release.wait()
            return {"ok": True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            before = (await client.get("/observations/client")).json()
            task = asyncio.create_task(client.get("/orders/preview"))
            await ready.wait()
            during = (await client.get("/observations/client")).json()
            self.assertEqual(during["in_flight"], 1)
            release.set()
            await task
            after = (await client.get("/observations/client")).json()
        self.assertEqual(after["requests_finished"] - before["requests_finished"], 1)
        self.assertEqual(after["in_flight"], 0)
        self.assertEqual(after["max_connections"], 2)
        self.assertFalse({"delay", "scenario", "truth"} & after.keys())
