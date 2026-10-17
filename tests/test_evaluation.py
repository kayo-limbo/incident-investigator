import unittest
import io
import json
import tempfile
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from backend.investigation.agent import ModelFailure, investigate_agent
from backend.investigation.models import Evidence, Finding, Investigation
from evals.run_baseline import main, score


class EvaluationTests(unittest.TestCase):
    def report(self):
        return Investigation(trace_id="a" * 32,
            evidence=[Evidence(id="trace", tool="get_trace", source="test")],
            finding=Finding(code="insufficient_evidence", summary="无法区分网络与服务停止",
                            evidence_ids=["trace"], limitations=[])).model_dump(mode="json")

    def test_completed_abstention_with_observation_passes(self):
        self.assertTrue(score(self.report(), "insufficient_evidence"))

    def test_execution_errors_never_pass_as_abstention(self):
        for termination in ("model_error", "budget_exhausted", "invalid_action", "invalid_finding"):
            report = self.report()
            report["termination"] = termination
            self.assertFalse(score(report, "insufficient_evidence"))

    def test_missing_failed_or_invented_evidence_cannot_pass(self):
        for change in ("empty", "failed", "invented", "wrong_code"):
            report = self.report()
            if change == "empty":
                report["finding"]["evidence_ids"] = []
            elif change == "failed":
                report["evidence"][0]["status"] = "error"
            elif change == "invented":
                report["finding"]["evidence_ids"] = ["fake"]
            else:
                report["finding"]["code"] = "inventory_latency"
            self.assertFalse(score(report, "insufficient_evidence"))

    def test_executor_marks_model_error_and_budget(self):
        model = Mock()
        model.decide.side_effect = ModelFailure("timeout")
        self.assertEqual(investigate_agent("a" * 32, model).termination, "model_error")
        model.decide.side_effect = None
        model.decide.return_value = {"kind": "tool", "tool": "get_trace"}
        self.assertEqual(investigate_agent("a" * 32, model, max_tool_calls=0).termination,
                         "budget_exhausted")

    def test_failed_diagnoses_continue_and_restore_business(self):
        responses = []
        for status in (200, 200, 503, 503, 200, 200):
            response = Mock(status_code=status, headers={"X-Trace-ID": "a" * 32})
            response.json.return_value = {"can_order": True}
            responses.append(response)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            with patch("sys.argv", ["eval", "--output", str(output)]), \
                    patch("evals.run_baseline.compose") as compose, \
                    patch("evals.run_baseline.wait_for_trace"), \
                    patch("evals.run_baseline.httpx.Client") as client, \
                    patch("evals.run_baseline.subprocess.run") as run, redirect_stdout(io.StringIO()):
                client.return_value.__enter__.return_value.get.side_effect = responses
                run.return_value.stdout = json.dumps(self.report())
                with self.assertRaises(SystemExit) as caught:
                    main()
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(len(report["cases"]), 5)
        self.assertTrue(report["recovery_verified"])
        self.assertTrue(report["completed"])
        compose.assert_called_with("up", "-d", "--wait", delay=0)
        for call in run.call_args_list:
            self.assertNotIn("INVENTORY_DELAY_MS", call.kwargs["env"])
            self.assertNotIn("INVENTORY_BASE_URL", call.kwargs["env"])
            self.assertNotIn("stopped_inventory", call.args[0])
