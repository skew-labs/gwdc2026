"""Bounded OpenAI-compatible Qwen transport for the hosted service.

The provider returns text and transport metadata.  It never interprets the
text, updates a mandate, calls a tool, or grants execution authority.
"""

from dataclasses import dataclass
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from economic_machine.values import MachineError, canonical


MAX_RESPONSE_BYTES = 128_000
MAX_MESSAGES_BYTES = 65_536
SUPPORTED_MODEL = re.compile(r"(^|[/_.-])qwen3[/_.-]?32b($|[/_.:-])", re.IGNORECASE)


class ModelProviderError(RuntimeError):
    """A bounded provider failure with safe, non-secret trace metadata."""

    def __init__(self, code: str, metadata: dict):
        super().__init__(code)
        self.code = code
        self.metadata = metadata


@dataclass(frozen=True)
class QwenProviderConfig:
    base_url: str
    api_key: str
    model_id: str
    provider: str = "kiln"
    timeout_seconds: int = 30
    max_output_tokens: int = 800
    max_retries: int = 1

    @classmethod
    def from_environment(cls):
        required = ("GWDC_QWEN_BASE_URL", "GWDC_QWEN_API_KEY", "GWDC_QWEN_MODEL_ID")
        if any(not os.environ.get(key) for key in required):
            raise ModelProviderError("NOT_CONFIGURED", {
                "provider": "kiln", "model_id": os.environ.get("GWDC_QWEN_MODEL_ID"),
                "attempts": 0, "latency_ms": 0, "request_id": None,
                "model_revision": None, "input_tokens": None,
                "output_tokens": None, "response_sha256": None,
            })
        return cls(os.environ["GWDC_QWEN_BASE_URL"], os.environ["GWDC_QWEN_API_KEY"],
                   os.environ["GWDC_QWEN_MODEL_ID"])

    def validate(self):
        parsed = urlparse(self.base_url.rstrip("/"))
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise MachineError("Qwen service base URL must be credential-free HTTPS")
        if not self.api_key:
            raise MachineError("Qwen service credential is required")
        if SUPPORTED_MODEL.search(self.model_id) is None:
            raise MachineError("configured model is not Qwen3 32B")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:/-]{0,79}", self.provider) is None:
            raise MachineError("invalid model provider name")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 35:
            raise MachineError("model timeout outside service bound")
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 1600:
            raise MachineError("model output budget outside service bound")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 1:
            raise MachineError("model retry budget outside service bound")


class OpenAICompatibleQwenProvider:
    def __init__(self, config: QwenProviderConfig, *, opener=None, monotonic=time.monotonic):
        config.validate()
        self.config = config
        self._open = opener or urllib.request.urlopen
        self._monotonic = monotonic

    @staticmethod
    def _messages(messages):
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 8
                or any(not isinstance(item, dict) or set(item) != {"role", "content"}
                       for item in messages)):
            raise MachineError("model messages require a bounded role/content list")
        for item in messages:
            if item["role"] not in {"system", "user"} or not isinstance(item["content"], str):
                raise MachineError("unsupported model message")
            if not 1 <= len(item["content"]) <= 32_768:
                raise MachineError("model message outside size bound")
        if len(canonical(messages)) > MAX_MESSAGES_BYTES:
            raise MachineError("model messages exceed request bound")
        return messages

    def _metadata(self, attempts, started, body=None, response=None):
        usage = body.get("usage") if isinstance(body, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        request_id = None
        revision = None
        if response is not None:
            request_id = response.headers.get("x-request-id")
            revision = response.headers.get("x-model-revision")
        if isinstance(body, dict):
            request_id = request_id or body.get("id")
            revision = revision or body.get("system_fingerprint")
        def provider_text(value):
            if (not isinstance(value, str) or not 1 <= len(value) <= 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                return None
            return value
        request_id, revision = provider_text(request_id), provider_text(revision)
        raw = canonical(body) if isinstance(body, dict) else None
        def counter(name):
            value = usage.get(name)
            return value if type(value) is int and value >= 0 else None

        return {
            "provider": self.config.provider, "model_id": self.config.model_id,
            "model_revision": revision, "request_id": request_id,
            "input_tokens": counter("prompt_tokens"),
            "output_tokens": counter("completion_tokens"),
            "attempts": attempts,
            "latency_ms": max(0, round((self._monotonic() - started) * 1000)),
            "response_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
            "energy": {"kind": "UNMEASURED", "joules": None,
                       "measurement_source": None},
        }

    def complete(self, messages: list[dict]) -> dict:
        messages = self._messages(messages)
        payload = {"model": self.config.model_id, "messages": messages,
                   "temperature": 0, "max_tokens": self.config.max_output_tokens,
                   "stream": False}
        encoded = canonical(payload)
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/chat/completions", data=encoded,
            headers={"Authorization": "Bearer " + self.config.api_key,
                     "Content-Type": "application/json"}, method="POST")
        started = self._monotonic()
        attempts = 0
        while True:
            attempts += 1
            body = response = None
            try:
                with self._open(request, timeout=self.config.timeout_seconds) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ModelProviderError("RESPONSE_TOO_LARGE",
                            self._metadata(attempts, started, response=response))
                    body = json.loads(raw.decode("utf-8"))
                choices = body.get("choices")
                if not isinstance(choices, list) or len(choices) != 1:
                    raise ValueError("choices")
                message = choices[0].get("message")
                if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                    raise ValueError("content")
                if message.get("tool_calls") not in (None, []):
                    raise ModelProviderError("TOOL_CALL_REJECTED",
                        self._metadata(attempts, started, body, response))
                return {"content": message["content"],
                        **self._metadata(attempts, started, body, response)}
            except ModelProviderError:
                raise
            except urllib.error.HTTPError as exc:
                code = "RATE_LIMITED" if exc.code == 429 else "HTTP_ERROR"
                if exc.code == 429 and attempts <= self.config.max_retries:
                    continue
                raise ModelProviderError(code,
                    self._metadata(attempts, started, body, response)) from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                if attempts <= self.config.max_retries:
                    continue
                raise ModelProviderError("TIMEOUT_OR_NETWORK",
                    self._metadata(attempts, started, body, response)) from exc
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
                raise ModelProviderError("MALFORMED_RESPONSE",
                    self._metadata(attempts, started, body, response)) from exc
