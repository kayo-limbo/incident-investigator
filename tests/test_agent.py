import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from backend.investigation.agent import investigate_agent
from backend.investigation.models import Evidence
from backend.investigation.tools import EvidenceTools

TRACE_ID = "a" * 32


def finish(ids, code="no_anomaly_observed"):
    return {"kind": "finish", "finding": {"code": code, "summary": "测试结论",
            "evidence_ids": ids, "limitations": ["仅测试执行流程"]}}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tools = Mock(spec=EvidenceTools)
        self.observation = Evidence(tool="get_trace", source="test", data={"value": 1})
        self.tools.get_trace.return_value = self.observation
        self.model = Mock()

    def run_agent(self, budget=6):
        return investigate_agent(TRACE_ID, self.model, self.tools, budget)

    def test_finish_after_last_allowed_call(self):
        self.model.decide.side_effect = [
            {"kind": "tool", "tool": "get_trace"}, finish([self.observation.id])]
        report = self.run_agent(1)
        self.assertEqual(report.finding.code, "no_anomaly_observed")
        self.tools.get_trace.assert_called_once_with(TRACE_ID)
        self.assertEqual(self.model.decide.call_args.args[0].remaining_tool_calls, 0)
        self.assertEqual(report.strategy, "bounded_agent_v1")

    def test_repeated_queries_stop_at_budget(self):
        self.model.decide.return_value = {"kind": "tool", "tool": "get_trace"}
        self.tools.get_trace.side_effect = lambda _: Evidence(tool="get_trace", source="test")
        report = self.run_agent(2)
        self.assertEqual(self.tools.get_trace.call_count, 2)
        self.assertEqual(self.model.decide.call_count, 3)
        self.assertEqual(len(report.evidence), 2)
        self.assertEqual(report.finding.code, "insufficient_evidence")

    def test_unknown_tool_and_extra_parameters_never_execute(self):
        for action in ({"kind": "tool", "tool": "_docker"},
                       {"kind": "tool", "tool": "get_trace", "trace_id": "b" * 32},
                       {"kind": "tool", "tool": "probe_inventory", "url": "http://other"}):
            with self.subTest(action=action):
                self.model.decide.return_value = action
                self.assertEqual(self.run_agent().finding.code, "insufficient_evidence")
                self.assertEqual(self.tools.mock_calls, [])

    def test_fake_empty_and_duplicate_references_are_rejected(self):
        for ids in (["invented"], [], [self.observation.id] * 2):
            self.model.decide.side_effect = [
                {"kind": "tool", "tool": "get_trace"}, finish(ids)]
            self.assertEqual(self.run_agent().finding.code, "insufficient_evidence")

    def test_failed_observation_is_retained_but_cannot_support_diagnosis(self):
        self.observation.status = "error"
        self.observation.error_code = "not_found"
        self.model.decide.side_effect = [
            {"kind": "tool", "tool": "get_trace"}, finish([self.observation.id])]
        report = self.run_agent()
        self.assertEqual(report.finding.code, "insufficient_evidence")
        self.assertEqual(report.evidence[0].error_code, "not_found")

    def test_model_cannot_mutate_saved_evidence(self):
        def decide(context):
            if not context.evidence:
                return {"kind": "tool", "tool": "get_trace"}
            context.evidence[0].data["value"] = 999
            return finish([context.evidence[0].id])
        self.model.decide.side_effect = decide
        self.assertEqual(self.run_agent().evidence[0].data["value"], 1)

    def test_adapter_failure_is_sanitized(self):
        self.model.decide.side_effect = RuntimeError("secret-api-key")
        report = self.run_agent()
        self.assertEqual(report.finding.code, "insufficient_evidence")
        self.assertNotIn("secret-api-key", report.model_dump_json())

    def test_invalid_inputs_do_not_call_model(self):
        with self.assertRaises(ValidationError):
            investigate_agent("bad", self.model, self.tools)
        for budget in (-1, True, 1.5, 101):
            with self.assertRaises(ValueError):
                self.run_agent(budget)
        self.model.decide.assert_not_called()

    def test_zero_budget_allows_abstention_but_no_tool(self):
        self.model.decide.return_value = finish([], "insufficient_evidence")
        self.assertEqual(self.run_agent(0).finding.code, "insufficient_evidence")
        self.model.decide.return_value = {"kind": "tool", "tool": "get_trace"}
        self.assertEqual(self.run_agent(0).evidence, [])
        self.assertEqual(self.tools.mock_calls, [])

    def test_all_allowlisted_tools_are_dispatched(self):
        names = ["get_service_topology", "get_runtime_config", "probe_inventory"]
        for name in names:
            getattr(self.tools, name).return_value = Evidence(tool=name, source="test")
        self.model.decide.side_effect = [*({"kind": "tool", "tool": n} for n in names),
                                        finish([], "insufficient_evidence")]
        self.assertEqual([e.tool for e in self.run_agent().evidence], names)
        for name in names:
            getattr(self.tools, name).assert_called_once_with()
