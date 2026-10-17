import io
import json
import os
import tempfile
from pathlib import Path
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import httpx

from backend.investigation.__main__ import main
from backend.investigation.agent import DecisionContext, ModelFailure, investigate_agent
from backend.investigation.model import ChatCompletionsModel, ModelConfig, ModelConfigError
from backend.investigation.models import Evidence

TRACE_ID = "a" * 32
TOOL = {"kind": "tool", "tool": "get_trace"}


def envelope(action=TOOL, reason="stop", **message_fields):
    return {"choices": [{"finish_reason": reason, "message": {
        "role": "assistant", "content": json.dumps(action), **message_fields}}]}


class ModelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env_file = Path(directory.name) / ".env"
        patcher = patch("backend.investigation.model.ENV_FILE", self.env_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_project_env_file_and_environment_precedence(self):
        self.env_file.write_text('DEEPSEEK_API_KEY="file-secret"\nINVESTIGATOR_MODEL=deepseek-flash\n',
                                 encoding="utf-8-sig")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ModelConfig.from_env().api_key, "file-secret")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-secret"}):
                self.assertEqual(ModelConfig.from_env().api_key, "env-secret")

    def test_invalid_env_file_does_not_echo_secret(self):
        self.env_file.write_text('DEEPSEEK_API_KEY="file-secret\n', encoding="utf-8")
        with self.assertRaises(ModelConfigError) as caught:
            ModelConfig.from_env()
        self.assertNotIn("file-secret", str(caught.exception))

    def config(self, **updates):
        return ModelConfig(**{"base_url": "https://api.example.test/v1", "model": "deepseek-flash",
                              "api_key": "test-secret", **updates})

    def context(self):
        return DecisionContext(trace_id=TRACE_ID, evidence=[], remaining_tool_calls=2)

    def test_request_contract_and_client_lifecycle(self):
        def handle(request):
            self.assertEqual(str(request.url), "https://api.example.test/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "deepseek-flash")
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertEqual(payload["max_tokens"], 2048)
            self.assertEqual(payload["thinking"], {"type": "disabled"})
            self.assertFalse(payload["stream"])
            self.assertEqual(json.loads(payload["messages"][1]["content"])["trace_id"], TRACE_ID)
            self.assertNotIn("test-secret", str(payload))
            self.assertIn("JSON", payload["messages"][0]["content"])
            self.assertEqual(request.extensions["timeout"]["read"], 30)
            return httpx.Response(200, json=envelope())
        with ChatCompletionsModel(self.config(), transport=httpx.MockTransport(handle)) as model:
            self.assertEqual(model.decide(self.context()), TOOL)
        self.assertTrue(model._client.is_closed)

    def test_http_failures_do_not_retry_or_leak_response(self):
        for status, code in ((401, "authentication_failed"), (403, "authentication_failed"),
                             (429, "rate_limited"), (500, "http_error"), (302, "http_error")):
            requests = []
            def handle(request):
                requests.append(request)
                return httpx.Response(status, text="private-server-message",
                                      headers={"Location": "https://other.test"})
            with self.subTest(status=status), ChatCompletionsModel(
                self.config(), transport=httpx.MockTransport(handle)
            ) as model:
                report = investigate_agent(TRACE_ID, model)
                self.assertIn(code, report.finding.summary)
                self.assertNotIn("private-server-message", report.model_dump_json())
                self.assertNotIn("test-secret", report.model_dump_json())
                self.assertEqual(len(requests), 1)

    def test_transport_failure_and_timeout_have_safe_codes(self):
        for error, code in ((httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "transport_error")):
            def handle(request):
                raise error("secret-network-details", request=request)
            with ChatCompletionsModel(self.config(), transport=httpx.MockTransport(handle)) as model:
                with self.assertRaisesRegex(ModelFailure, code):
                    model.decide(self.context())

    def test_malformed_truncated_and_refused_responses(self):
        cases = [(b"not-json", "invalid_response"),
                 (json.dumps({"choices": []}).encode(), "invalid_response"),
                 (json.dumps(envelope(reason="length")).encode(), "output_truncated"),
                 (json.dumps(envelope(refusal="sensitive-text")).encode(), "refused"),
                 (json.dumps(envelope(content="")).encode(), "invalid_response"),
                 (json.dumps(envelope(action=[])).encode(), "invalid_response"),
                 (json.dumps(envelope(tool_calls=[{}])).encode(), "invalid_response"),
                 (json.dumps(envelope(reason="content_filter")).encode(), "invalid_response")]
        for body, code in cases:
            with self.subTest(code=code), ChatCompletionsModel(
                self.config(), transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
            ) as model:
                with self.assertRaisesRegex(ModelFailure, code):
                    model.decide(self.context())

    def test_body_and_context_limits(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, content=b"x" * 100)
        with ChatCompletionsModel(self.config(), transport=httpx.MockTransport(handle)) as model:
            model.MAX_RESPONSE_BYTES = 99
            with self.assertRaisesRegex(ModelFailure, "response_too_large"):
                model.decide(self.context())
            model.MAX_CONTEXT_BYTES = 1
            with self.assertRaisesRegex(ModelFailure, "context_too_large"):
                model.decide(self.context())
        self.assertEqual(len(calls), 1)

    def test_config_validation_and_secret_repr(self):
        self.assertNotIn("test-secret", repr(self.config()))
        for updates in ({"base_url": "http://remote.test"}, {"base_url": "https://user:secret@host"},
                        {"base_url": "https://host?key=secret"}, {"base_url": "https://host:invalid"},
                        {"model": ""}, {"api_key": ""}, {"timeout_seconds": float("nan")},
                        {"timeout_seconds": 0}, {"max_output_tokens": True}):
            with self.subTest(updates=updates), self.assertRaises(ModelConfigError) as caught:
                self.config(**updates)
            self.assertNotIn("secret", str(caught.exception))
        self.config(base_url="http://127.0.0.1:9000/v1")

    def test_environment_defaults_and_numeric_validation(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-secret"}, clear=True):
            config = ModelConfig.from_env()
            self.assertEqual(config.model, "deepseek-flash")
            self.assertEqual(config.base_url, "https://api.deepseek.com")
            with patch.dict(os.environ, {"INVESTIGATOR_TIMEOUT_SECONDS": "secret-invalid"}):
                with self.assertRaises(ModelConfigError) as caught:
                    ModelConfig.from_env()
                self.assertNotIn("secret-invalid", str(caught.exception))

    def test_adapter_and_executor_complete_evidence_roundtrip(self):
        class Tools:
            def get_trace(self, trace_id):
                return Evidence(id="observed-id", tool="get_trace", source="test",
                                data={"trace_id": trace_id})
            def get_service_topology(self):
                raise AssertionError("unexpected tool")
            get_runtime_config = probe_inventory = get_service_topology
        def handle(request):
            context = json.loads(json.loads(request.content)["messages"][1]["content"])
            if not context["evidence"]:
                return httpx.Response(200, json=envelope())
            return httpx.Response(200, json=envelope({"kind": "finish", "finding": {
                "code": "insufficient_evidence", "summary": "链路信息不足", "limitations": [],
                "evidence_ids": [context["evidence"][0]["id"]]}}))
        with ChatCompletionsModel(self.config(), transport=httpx.MockTransport(handle)) as model:
            report = investigate_agent(TRACE_ID, model, Tools(), max_tool_calls=1)
        self.assertEqual(report.finding.evidence_ids, ["observed-id"])
        self.assertEqual(len(report.evidence), 1)

    def test_cli_rejects_invalid_trace_and_missing_configuration_before_network(self):
        for arguments in (["invalid", "--strategy", "agent"], [TRACE_ID, "--strategy", "agent"]):
            with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["investigate", *arguments]), \
                    patch("backend.investigation.__main__.ChatCompletionsModel") as adapter, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main()
            self.assertEqual(caught.exception.code, 2)
            adapter.assert_not_called()

    def test_cli_agent_selects_adapter_and_writes_json(self):
        finish = {"kind": "finish", "finding": {"code": "insufficient_evidence",
                  "summary": "没有证据", "limitations": [], "evidence_ids": []}}
        with patch("sys.argv", ["investigate", TRACE_ID, "--strategy", "agent", "--max-tool-calls", "0"]), \
                patch("backend.investigation.__main__.ModelConfig.from_env", return_value=self.config()), \
                patch("backend.investigation.__main__.ChatCompletionsModel") as adapter, \
                redirect_stdout(io.StringIO()) as output:
            adapter.return_value.__enter__.return_value.decide.return_value = finish
            main()
        self.assertEqual(json.loads(output.getvalue())["strategy"], "bounded_agent_v1")


if __name__ == "__main__":
    unittest.main()
