import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.investigation.agent import ModelFailure
from backend.investigation.async_investigation import (
    InvestigationCancelled, investigate_hypotheses_async, investigate_window_async,
)
from backend.investigation.async_model import AsyncHypothesisChatModel, AsyncWindowChatModel
from backend.investigation.budget import ObservedTokenBudget
from backend.investigation.model import ModelConfig
from backend.investigation.usage import CallUsage, Usage, token_usage
from backend.investigation.window_cli import main
from test_async_hypotheses import setup_snapshot
from test_hypotheses import query, update
from test_window_investigation import finish


class MeteredModel:
    def __init__(self, *rounds):
        self.rounds = iter(rounds)
        self.calls = 0
        self.last_usage = None
        self.entered = asyncio.Event()

    async def decide(self, context):
        self.calls += 1
        action, self.last_usage = next(self.rounds)
        if action == "wait":
            self.entered.set()
            await asyncio.Event().wait()
        if isinstance(action, Exception):
            raise action
        return action


class TokenBudgetUnitTests(unittest.TestCase):
    def test_validation_disabled_and_empty_ledger(self):
        for limit in (0, -1, True, "10", 1.5):
            with self.assertRaises(ValueError):
                ObservedTokenBudget(limit)
        self.assertIsNone(ObservedTokenBudget().snapshot(Usage()))
        snapshot = ObservedTokenBudget(10).snapshot(Usage())
        self.assertEqual(snapshot.total_tokens, 0)
        self.assertEqual(snapshot.remaining_tokens, 10)

    def test_known_subtotal_is_not_a_complete_total(self):
        usage = Usage(calls=[CallUsage(kind="model", name="decide", status="ok", elapsed_ms=1, total_tokens=4),
                             CallUsage(kind="model", name="decide", status="error", elapsed_ms=1)])
        snapshot = ObservedTokenBudget(10).snapshot(usage)
        self.assertEqual(snapshot.known_total_tokens, 4)
        self.assertEqual(snapshot.unknown_usage_calls, 1)
        self.assertIsNone(snapshot.total_tokens)
        self.assertIsNone(snapshot.remaining_tokens)
        self.assertIsNone(snapshot.exceeded_by)
        self.assertEqual(snapshot.status, "unknown")

    def test_inconsistent_partial_usage_is_unknown(self):
        for raw in ({"total_tokens": 2, "prompt_tokens": 100},
                    {"total_tokens": 2, "completion_tokens": 3},
                    {"total_tokens": 10, "prompt_tokens": 4, "completion_tokens": 5}):
            self.assertIsNone(token_usage(raw)["total_tokens"])
        self.assertEqual(token_usage({"total_tokens": 10})["total_tokens"], 10)


class TokenBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store, self.key = setup_snapshot(temp.name)
        self.variants = ((investigate_window_async,
                          {"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32},
                          finish(self.key, "insufficient_evidence", [])),
                         (investigate_hypotheses_async, query([update()]),
                          {**finish(self.key, "insufficient_evidence", []), "updates": [], "hypothesis_ids": []}))

    async def test_under_threshold_completes_for_both_strategies(self):
        for runner, tool, done in self.variants:
            model = MeteredModel((tool, {"total_tokens": 4}), (done, {"total_tokens": 5}))
            result = await runner(self.store, self.key, model, max_total_tokens=10)
            self.assertEqual(result.termination, "completed")
            self.assertEqual(result.token_budget.total_tokens, 9)
            self.assertEqual(result.token_budget.remaining_tokens, 1)
            self.assertEqual(result.token_budget.status, "available")
            self.assertEqual(model.calls, 2)

    async def test_equal_or_over_threshold_rejects_action_and_stops_calls(self):
        for runner, tool, done in self.variants:
            for tokens in (10, 14):
                for action in (tool, done):
                    model = MeteredModel((action, {"total_tokens": tokens}))
                    result = await runner(self.store, self.key, model, max_total_tokens=10)
                    self.assertEqual(result.termination, "token_budget_exhausted")
                    self.assertEqual(result.token_budget.exceeded_by, tokens - 10)
                    self.assertEqual(result.usage_totals["drilldown_calls"], 0)
                    self.assertEqual(result.token_budget.remaining_tokens, 0)
                    self.assertEqual(model.calls, 1)
                    if hasattr(result, "hypotheses"):
                        self.assertEqual(result.hypotheses, [])

    async def test_threshold_across_rounds_keeps_previous_evidence_and_hypotheses(self):
        for runner, tool, done in self.variants:
            model = MeteredModel((tool, {"total_tokens": 6}), (done, {"total_tokens": 5}))
            result = await runner(self.store, self.key, model, max_total_tokens=10)
            self.assertEqual(result.termination, "token_budget_exhausted")
            self.assertEqual(result.token_budget.total_tokens, 11)
            self.assertEqual(result.usage_totals["drilldown_calls"], 1)
            if hasattr(result, "hypotheses"):
                self.assertEqual(result.hypotheses[0].version, 1)

    async def test_missing_or_malformed_usage_stops_before_action(self):
        for runner, tool, done in self.variants:
            for usage in (None, {}, {"total_tokens": True}, {"total_tokens": -1},
                          {"total_tokens": 2, "prompt_tokens": 10}):
                model = MeteredModel((tool, usage))
                result = await runner(self.store, self.key, model, max_total_tokens=10)
                self.assertEqual(result.termination, "usage_unavailable")
                self.assertEqual(result.token_budget.status, "unknown")
                self.assertEqual(result.usage_totals["drilldown_calls"], 0)

    async def test_unknown_later_usage_retains_known_subtotal(self):
        runner, tool, done = self.variants[0]
        model = MeteredModel((tool, {"total_tokens": 4}), (done, None))
        result = await runner(self.store, self.key, model, max_total_tokens=10)
        self.assertEqual(result.termination, "usage_unavailable")
        self.assertEqual(result.token_budget.known_total_tokens, 4)
        self.assertIsNone(result.token_budget.total_tokens)

    async def test_disabled_budget_keeps_existing_unknown_usage_behavior(self):
        for runner, tool, done in self.variants:
            result = await runner(self.store, self.key, MeteredModel((done, None)))
            self.assertEqual(result.termination, "completed")
            self.assertIsNone(result.token_budget)

    async def test_model_error_wins_but_failed_usage_still_counts(self):
        for usage, status in (({"total_tokens": 20}, "exhausted"), (None, "unknown")):
            result = await investigate_window_async(
                self.store, self.key, MeteredModel((ModelFailure("output_truncated"), usage)), max_total_tokens=10)
            self.assertEqual(result.termination, "model_error")
            self.assertEqual(result.usage.calls[-1].error_code, "output_truncated")
            self.assertEqual(result.token_budget.status, status)

    async def test_cancel_and_deadline_keep_budget_metadata(self):
        for runner, tool, done in self.variants:
            model = MeteredModel((tool, {"total_tokens": 4}), ("wait", None))
            task = asyncio.create_task(runner(self.store, self.key, model, max_total_tokens=10))
            await asyncio.wait_for(model.entered.wait(), 2)
            task.cancel()
            with self.assertRaises(InvestigationCancelled) as caught:
                await task
            result = caught.exception.report
            self.assertEqual(result.termination, "cancelled")
            self.assertEqual(result.token_budget.known_total_tokens, 4)
            self.assertEqual(result.token_budget.status, "unknown")
            model = MeteredModel(("wait", None))
            result = await runner(self.store, self.key, model, max_total_tokens=10, timeout_seconds=0.05)
            self.assertEqual(result.termination, "deadline_exceeded")
            self.assertEqual(result.token_budget.unknown_usage_calls, 1)

    async def test_invalid_limit_rejected_before_source_access(self):
        for runner, tool, done in self.variants:
            with patch.object(self.store, "read") as read:
                with self.assertRaises(ValueError):
                    await runner(self.store, self.key, MeteredModel(), max_total_tokens=0)
                read.assert_not_called()


class TokenBudgetCliTests(unittest.TestCase):
    def test_rejects_invalid_or_unsupported_limit_before_model_configuration(self):
        for strategy, limit in (("fixed", "10"), ("agent", "0"), ("hypotheses", "-1"), ("agent", "1.5")):
            with patch("sys.argv", ["window", "investigate", "a" * 64, "--strategy", strategy,
                                    "--max-total-tokens", limit]), \
                    patch("backend.investigation.window_cli.ModelConfig.from_env") as config, \
                    redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main()
                self.assertEqual(caught.exception.code, 2)
                config.assert_not_called()

    def test_cli_limit_and_report_survive_real_adapter_decoding(self):
        with tempfile.TemporaryDirectory() as directory:
            store, key = setup_snapshot(directory)
            config = ModelConfig(base_url="https://example.test", model="test", api_key="test-secret")
            for strategy, adapter_type in (("agent", AsyncWindowChatModel), ("hypotheses", AsyncHypothesisChatModel)):
                action = finish(key, "insufficient_evidence", [])
                if strategy == "hypotheses":
                    action.update(updates=[], hypothesis_ids=[])
                def handler(request):
                    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                        "content": json.dumps(action)}}], "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}})
                adapter = adapter_type(config, transport=httpx.MockTransport(handler))
                output = Path(directory) / (strategy + '.private.json')
                with patch("sys.argv", ["window", "--store", directory, "investigate", key, "--strategy", strategy,
                                        "--max-total-tokens", "10", "--output", str(output)]), \
                        patch("backend.investigation.window_cli.ModelConfig.from_env", return_value=config), \
                        patch("backend.investigation.window_cli." + adapter_type.__name__, return_value=adapter), \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(main(), 1)
                report = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(report["termination"], "token_budget_exhausted")
                self.assertEqual(report["token_budget"]["exceeded_by"], 2)
                self.assertTrue(adapter._client.is_closed)
