import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from backend.investigation.evidence_models import ObservationRef, WindowQuery
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_queries import WindowEvidenceTools


def trace(letter="a", start=100, failed=False):
    trace_id = letter * 32
    def span(span_id, kind, service, operation, tags):
        return {"traceID": trace_id, "spanID": span_id, "processID": service,
                "operationName": operation, "startTime": start, "duration": 1000,
                "references": [], "tags": [{"key": "span.kind", "value": kind}] +
                [{"key": k, "value": v} for k, v in tags.items()]}
    spans = [span("1", "server", "orders", "GET /orders/preview",
                  {"http.response.status_code": 503 if failed else 200}),
             span("2", "client", "orders", "GET inventory",
                  {"client.max_connections": 4, "client.transport_ms": 250,
                   "client.request_headers_started": not failed,
                   **({"error.type": "PoolTimeout"} if failed else {"client.pre_send_ms": 8}),
                   "password": "secret", "fault.truth": "secret"})]
    if not failed:
        spans.append(span("3", "server", "inventory", "GET /inventory/{sku}", {}))
    return {"traceID": trace_id, "spans": spans,
            "processes": {s: {"serviceName": s} for s in ("orders", "inventory")}}


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SnapshotStore(Path(self.temp.name))
        self.tools = WindowEvidenceTools(self.store)
        self.query = WindowQuery(start_us=100, end_us=200)

    def collect(self, entries, **extra):
        with patch.object(self.tools, "_fetch_window", return_value={"data": entries, **extra}):
            return self.tools.query_window(self.query)

    def test_success_and_pool_timeout_retained_without_truth(self):
        evidence = self.collect([trace(), trace("b", failed=True)])
        summary = evidence.data["summary"]
        self.assertEqual(summary["http_status_counts"], {"200": 1, "503": 1})
        self.assertEqual(summary["client_error_counts"], {"PoolTimeout": 1})
        self.assertEqual(summary["measurements"]["pre_send_ms"]["count"], 1)
        payload = self.store.read(evidence.data["snapshot_id"])
        self.assertNotIn("secret", json.dumps(payload))
        self.assertEqual(payload["coverage"], "unknown")

    def test_window_is_half_open_and_excludes_other_requests(self):
        evidence = self.collect([trace(start=99), trace("b", start=100), trace("c", start=200)])
        self.assertEqual(evidence.data["summary"]["trace_ids"], ["b" * 32])
        self.assertEqual(evidence.data["excluded"], 2)

    def test_empty_partial_and_unavailable_are_distinct(self):
        self.assertEqual(self.collect([]).data["quality"], "empty")
        bad = trace("b")
        bad["spans"][0]["duration"] = -1
        partial = self.collect([trace(), bad])
        self.assertEqual(partial.data["quality"], "partial")
        self.assertEqual(partial.data["rejected"], 1)
        self.assertEqual(partial.data["summary"]["request_count"], 1)
        with patch.object(self.tools, "_fetch_window", side_effect=httpx.ConnectError("secret")):
            failed = self.tools.query_window(self.query)
        self.assertEqual(failed.status, "error")
        self.assertEqual(failed.data["quality"], "unavailable")
        self.assertNotIn("secret", failed.model_dump_json())
        self.assertEqual(failed.data["usage"]["tool_calls"], 1)

    def test_cap_and_source_errors_never_claim_complete(self):
        self.query = WindowQuery(start_us=100, end_us=200, limit=1)
        self.assertTrue(self.collect([trace()]).data["limit_reached"])
        self.assertEqual(self.collect([trace()]).data["quality"], "partial")
        self.assertEqual(self.collect([], errors=["secret"]).data["quality"], "partial")
        self.assertNotIn("secret", self.collect([], errors=["secret"]).model_dump_json())

    def test_late_arrival_creates_new_snapshot_old_drilldown_is_offline(self):
        first = self.collect([trace()]).data["snapshot_id"]
        second = self.collect([trace(), trace("b")]).data["snapshot_id"]
        self.assertNotEqual(first, second)
        with patch.object(self.tools, "_fetch_window", side_effect=AssertionError("must be offline")):
            self.assertEqual(self.tools.get_snapshot_trace(first, "b" * 32).error_code, "trace_not_in_snapshot")
            self.assertEqual(self.tools.get_snapshot_trace(second, "b" * 32).status, "ok")
        self.assertEqual(len(self.store.read(first)["traces"]), 1)

    def test_field_reference_integrity_and_copy_isolation(self):
        key = self.collect([trace()]).data["snapshot_id"]
        ref = ObservationRef(snapshot_id=key, trace_id="a" * 32, span_id="2", field="tags.client.max_connections")
        self.assertEqual(self.store.resolve(ref), 4)
        data = self.store.read(key)
        data["traces"].clear()
        self.assertEqual(self.store.resolve(ref), 4)
        with self.assertRaises(ValueError):
            self.store.resolve(ref.model_copy(update={"trace_id": "b" * 32}))
        with self.assertRaises(ValueError):
            self.store.resolve(ref.model_copy(update={"field": "tags.missing"}))
        path = Path(self.temp.name) / f"{key}.private.json"
        path.write_text("{}", encoding="utf-8")
        self.assertEqual(self.tools.get_snapshot_trace(key, "a" * 32).error_code, "snapshot_invalid")

    def test_store_idempotency_and_invalid_paths(self):
        data = {"x": [1, 2]}
        key = self.store.freeze(data)
        self.assertEqual(self.store.freeze(copy.deepcopy(data)), key)
        with self.assertRaises(ValueError):
            self.store.read("../anything")

    def test_bounds_and_nonfinite_observations(self):
        for kwargs in ({"start_us": 200, "end_us": 100}, {"start_us": 0, "end_us": 300_000_001},
                       {"start_us": True, "end_us": 100}, {"start_us": 0, "end_us": 100, "limit": 201}):
            with self.assertRaises(ValidationError):
                WindowQuery(**kwargs)
        bad = trace()
        bad["spans"][1]["tags"].append({"key": "client.pre_send_ms", "value": float("nan")})
        self.assertEqual(self.collect([bad]).data["quality"], "partial")

    def test_http_query_contract_and_errors(self):
        def handler(request):
            self.assertEqual(request.url.path, "/api/traces")
            self.assertEqual(dict(request.url.params), {"service": "orders", "operation": "GET /orders/preview",
                                                       "start": "100", "end": "200", "limit": "100"})
            return httpx.Response(200, json={"data": [trace()]})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch("backend.investigation.window_queries.httpx.Client", return_value=client):
            self.assertEqual(self.tools.query_window(self.query).data["summary"]["request_count"], 1)
        for response, code in ((httpx.Response(503), "source_unavailable"),
                               (httpx.Response(200, content="no json"), "invalid_response"),
                               (httpx.Response(200, content=b"x" * 8_000_001), "too_large")):
            client = httpx.Client(transport=httpx.MockTransport(lambda request: response))
            with patch("backend.investigation.window_queries.httpx.Client", return_value=client):
                self.assertEqual(self.tools.query_window(self.query).error_code, code)

    def test_disk_failure_does_not_report_success(self):
        with patch.object(self.store, "freeze", side_effect=OSError("secret")):
            evidence = self.collect([trace()])
        self.assertEqual(evidence.error_code, "snapshot_write_failed")
        self.assertNotIn("snapshot_id", evidence.data)


if __name__ == "__main__":
    unittest.main()
