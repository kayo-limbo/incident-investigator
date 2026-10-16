import copy
import time
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from backend.investigation.baseline import investigate
from backend.investigation.models import Evidence
from backend.investigation.tools import EvidenceTools, EXPECTED, ToolFailure, project_trace

TRACE_ID = "a" * 32


def spans(slow=False, failed=False):
    result = [
        {"span_id": "1", "parents": [], "service": "orders", "operation": "GET /orders/preview",
         "start_us": time.time() * 1e6, "duration_ms": 510 if slow else 10,
         "tags": {"span.kind": "server", "http.response.status_code": 503 if failed else 200}},
        {"span_id": "2", "parents": ["1"], "service": "orders", "operation": "GET inventory",
         "start_us": time.time() * 1e6, "duration_ms": 505 if slow else 8,
         "tags": {"span.kind": "client", "error": failed, "server.address": "inventory",
                  "server.port": 8002 if failed else 8001}},
    ]
    if not failed:
        result.append({"span_id": "3", "parents": ["2"], "service": "inventory", "operation": "GET /inventory/{sku}",
                       "start_us": time.time() * 1e6, "duration_ms": 501 if slow else 2,
                       "tags": {"span.kind": "server", "http.response.status_code": 200}})
    return result


class FakeTools:
    def __init__(self, data, port=8002, probe_ok=True):
        self.data, self.port, self.probe_ok = data, port, probe_ok
        self.calls = []

    def result(self, name, data):
        self.calls.append(name)
        return Evidence(tool=name, source="test", data=data)

    def get_trace(self, trace_id):
        return self.result("get_trace", {"trace_id": trace_id, "spans": copy.deepcopy(self.data)})

    def get_service_topology(self):
        return self.result("get_service_topology", {"expected_endpoint": EXPECTED})

    def get_runtime_config(self):
        return self.result("get_runtime_config", {**EXPECTED, "port": self.port})

    def probe_inventory(self):
        return self.result("probe_inventory", {"business_ok": self.probe_ok})


class InvestigationTests(unittest.TestCase):
    def test_success_and_latency_are_limited_to_trace_evidence(self):
        for slow, code in ((False, "no_anomaly_observed"), (True, "inventory_latency")):
            tools = FakeTools(spans(slow=slow))
            report = investigate(TRACE_ID, tools)
            self.assertEqual(report.finding.code, code)
            self.assertEqual(tools.calls, ["get_trace"])
            self.assertEqual(report.finding.evidence_ids, [report.evidence[0].id])

    def test_mismatch_requires_all_four_sources(self):
        tools = FakeTools(spans(failed=True))
        result = investigate(TRACE_ID, tools)
        self.assertEqual(result.finding.code, "endpoint_mismatch")
        self.assertEqual(len(tools.calls), 4)
        self.assertEqual(set(result.finding.evidence_ids), {e.id for e in result.evidence})

    def test_probe_failure_cannot_confirm_mismatch(self):
        result = investigate(TRACE_ID, FakeTools(spans(failed=True), probe_ok=False))
        self.assertEqual(result.finding.code, "insufficient_evidence")

    def test_changed_config_and_correct_target_are_not_misdiagnosed(self):
        for actual_target in (8002, 8001):
            data = spans(failed=True)
            data[1]["tags"]["server.port"] = actual_target
            result = investigate(TRACE_ID, FakeTools(data, port=8001))
            self.assertEqual(result.finding.code, "insufficient_evidence")

    def test_partial_and_stale_traces_refuse(self):
        stale = spans(failed=True)
        stale[0]["start_us"] -= 180 * 1e6
        for data in (spans()[:2], spans()[1:], stale):
            tools = FakeTools(data)
            self.assertEqual(investigate(TRACE_ID, tools).finding.code, "insufficient_evidence")
            self.assertEqual(tools.calls, ["get_trace"])

    def test_invalid_id_never_reaches_data_source(self):
        tools = FakeTools([])
        with self.assertRaises(ValidationError):
            investigate("../../secrets", tools)
        self.assertEqual(tools.calls, [])

    def test_config_drops_credentials_query_and_path(self):
        tools = EvidenceTools()
        with patch.object(tools, "_docker", return_value="http://user:secret@inventory:8002/private?token=secret"):
            evidence = tools.get_runtime_config()
        self.assertEqual(evidence.data, {"scheme": "http", "host": "inventory", "port": 8002})
        self.assertNotIn("secret", evidence.model_dump_json())

    def test_source_unavailable_is_evidence_error(self):
        tools = EvidenceTools()
        with patch.object(tools, "_docker", side_effect=OSError("sensitive details")):
            evidence = tools.get_runtime_config()
        self.assertEqual(evidence.error_code, "source_unavailable")
        self.assertNotIn("sensitive", evidence.model_dump_json())

    def test_trace_projection_filters_secrets_and_rejects_oversize(self):
        span = {"spanID": "1", "traceID": TRACE_ID, "processID": "p", "references": [],
                "startTime": 1, "duration": 2, "operationName": "GET",
                "tags": [{"key": "password", "value": "secret"}, {"key": "span.kind", "value": "server"}]}
        raw = {"data": [{"traceID": TRACE_ID, "spans": [span],
                         "processes": {"p": {"serviceName": "orders", "tags": ["secret"]}}}]}
        self.assertNotIn("secret", str(project_trace(raw, TRACE_ID)))
        raw["data"][0]["spans"] = [span] * 51
        with self.assertRaisesRegex(ToolFailure, "too_large"):
            project_trace(raw, TRACE_ID)

    def test_missing_trace_never_becomes_no_anomaly(self):
        tools = FakeTools([])
        with patch.object(tools, "get_trace", return_value=Evidence(
            tool="get_trace", source="test", status="error", error_code="not_found",
        )):
            result = investigate(TRACE_ID, tools)
        self.assertEqual(result.finding.code, "insufficient_evidence")
        self.assertEqual(len(result.evidence), 1)


if __name__ == "__main__":
    unittest.main()
