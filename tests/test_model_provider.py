"""Bounded Qwen transport tests; no provider or credential is used."""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from economic_machine.values import MachineError
from finance_service.model_provider import (ModelProviderError,
    OpenAICompatibleQwenProvider, QwenProviderConfig)


class Response:
    def __init__(self, body, headers=None):
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, maximum):
        return self.body


class QueueOpener:
    def __init__(self, *items):
        self.items = list(items)
        self.calls = 0

    def __call__(self, request, timeout):
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def config(**changes):
    base = dict(base_url="https://kiln.invalid/v1", api_key="secret",
                model_id="qwen3-32b", max_retries=1)
    base.update(changes)
    return QwenProviderConfig(**base)


class ModelProviderTests(unittest.TestCase):
    def test_provider_fractional_cost_preserves_exact_response_hash(self):
        import hashlib
        body = b'{"choices":[{"message":{"content":"{}"}}],"usage":{"prompt_tokens":9,"completion_tokens":5,"cost":0.00021}}'
        result = OpenAICompatibleQwenProvider(config(), opener=QueueOpener(Response(body))).complete([{"role":"user","content":"test"}])
        self.assertEqual(result["response_sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(result["input_tokens"], 9)

    def test_success_preserves_nullable_usage_and_request_identity(self):
        body = json.dumps({"id": "request-one", "choices": [{"message": {
            "content": "{}"}}]}).encode()
        provider = OpenAICompatibleQwenProvider(config(),
            opener=QueueOpener(Response(body, {"x-model-revision": "revision-one"})))
        result = provider.complete([{"role": "user", "content": "안녕"}])
        self.assertEqual((result["request_id"], result["model_revision"]),
                         ("request-one", "revision-one"))
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["output_tokens"])

        malformed_usage = json.dumps({"choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": "12", "completion_tokens": -1}}).encode()
        result = OpenAICompatibleQwenProvider(config(),
            opener=QueueOpener(Response(malformed_usage))).complete(
                [{"role": "user", "content": "안녕"}])
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (None, None))

    def test_429_and_timeout_stop_at_retry_budget(self):
        rate = urllib.error.HTTPError("https://kiln.invalid", 429, "rate", {}, io.BytesIO())
        opener = QueueOpener(rate, rate)
        provider = OpenAICompatibleQwenProvider(config(), opener=opener)
        with self.assertRaisesRegex(ModelProviderError, "RATE_LIMITED") as caught:
            provider.complete([{"role": "user", "content": "a"}])
        self.assertEqual((opener.calls, caught.exception.metadata["attempts"]), (2, 2))
        timeout = QueueOpener(TimeoutError(), TimeoutError())
        with self.assertRaisesRegex(ModelProviderError, "TIMEOUT_OR_NETWORK"):
            OpenAICompatibleQwenProvider(config(), opener=timeout).complete(
                [{"role": "user", "content": "a"}])
        self.assertEqual(timeout.calls, 2)

    def test_http_failures_are_specific_and_only_transient_statuses_retry(self):
        for status, code, calls in ((400, "BAD_REQUEST", 1),
                                    (401, "AUTHENTICATION_FAILED", 1),
                                    (402, "PAYMENT_REQUIRED", 1),
                                    (403, "PROVIDER_FORBIDDEN_OR_SUSPENDED", 1),
                                    (404, "MODEL_OR_ENDPOINT_NOT_FOUND", 1),
                                    (503, "PROVIDER_UNAVAILABLE", 2)):
            errors = [urllib.error.HTTPError("https://kiln.invalid", status, "failure", {},
                                            io.BytesIO()) for _ in range(calls)]
            opener = QueueOpener(*errors)
            with self.subTest(status=status), self.assertRaisesRegex(ModelProviderError, code):
                OpenAICompatibleQwenProvider(config(), opener=opener).complete(
                    [{"role": "user", "content": "a"}])
            self.assertEqual(opener.calls, calls)

    def test_environment_supports_owner_only_secret_file_without_key_in_env(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "qwen.key"
            path.write_text("file-secret\n")
            path.chmod(0o600)
            environment = {"GWDC_QWEN_BASE_URL": "https://api.bricksum.com/v1",
                "GWDC_QWEN_MODEL_ID": "qwen3-32b", "GWDC_QWEN_API_KEY_FILE": str(path)}
            with patch.dict(os.environ, environment, clear=True):
                loaded = QwenProviderConfig.from_environment()
            self.assertEqual(loaded.api_key, "file-secret")
            with patch.dict(os.environ, {**environment, "GWDC_QWEN_API_KEY": "duplicate"},
                             clear=True), self.assertRaisesRegex(ModelProviderError,
                                                                 "NOT_CONFIGURED"):
                QwenProviderConfig.from_environment()

    def test_tool_call_malformed_json_and_wrong_model_fail_closed(self):
        tools = json.dumps({"choices": [{"message": {"content": "{}",
            "tool_calls": [{"function": {"name": "send"}}]}}]}).encode()
        with self.assertRaisesRegex(ModelProviderError, "TOOL_CALL_REJECTED"):
            OpenAICompatibleQwenProvider(config(), opener=QueueOpener(Response(tools))).complete(
                [{"role": "user", "content": "a"}])
        with self.assertRaisesRegex(ModelProviderError, "MALFORMED_RESPONSE"):
            OpenAICompatibleQwenProvider(config(), opener=QueueOpener(Response(b"{"))).complete(
                [{"role": "user", "content": "a"}])
        with self.assertRaisesRegex(MachineError, "not Qwen3 32B"):
            OpenAICompatibleQwenProvider(config(model_id="qwen2.5-32b"))


if __name__ == "__main__":
    unittest.main()
