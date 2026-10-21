"""Answer transports. No adapter executes model-proposed tools or retries a turn."""
from __future__ import annotations

import json
import time
import uuid
from collections import deque
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class AdapterError(RuntimeError):
    """The remote outcome may be unknown; callers must never automatically retry."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AdapterError("MODEL_HTTP_REDIRECT_REJECTED")


def local_endpoint(base_url):
    parsed = urlparse(base_url)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise ValueError("P0 Ollama endpoint must be a plain loopback HTTP origin")
    return base_url.rstrip("/")


def post_json(url, payload, timeout, response_limit=2 * 1024 * 1024):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        # A loopback request must not be forwarded through an ambient HTTP proxy.
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(response_limit + 1)
        if len(raw) > response_limit:
            raise AdapterError("MODEL_RESPONSE_TOO_LARGE")
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("error"):
            raise AdapterError("MODEL_RESPONSE_INVALID")
        return result
    except HTTPError as exc:
        raise AdapterError("MODEL_HTTP_ERROR_%d" % exc.code) from exc
    except (URLError, OSError, ValueError) as exc:
        raise AdapterError("MODEL_TRANSPORT_OR_JSON_ERROR") from exc


class ReplayAdapter:
    """Deterministic test transport; every result is explicitly marked simulated."""
    name = "replay-simulated"
    simulated = True

    def __init__(self, responses):
        self.responses = deque(responses)
        self.captured = []

    def send(self, messages, request_id):
        if not self.responses:
            raise AdapterError("REPLAY_RESPONSES_EXHAUSTED")
        self.captured.append(json.loads(json.dumps(messages)))
        return {"text": self.responses.popleft(), "message_id": "replay-" + request_id,
                "remote_thread_id": "replay-clean-" + request_id,
                "usage": None, "latency_ms": 0, "simulated": True, "adapter": self.name}


class OllamaAdapter:
    """Stateless local /api/chat. No tools supplied and no tool executor exists."""
    name = "ollama-local"
    simulated = False

    def __init__(self, model, base_url="http://127.0.0.1:11434", timeout=45,
                 num_predict=256, transport=post_json):
        if not model or "cloud" in model.casefold():
            raise ValueError("P0 requires an explicitly selected installed local model")
        self.model = model
        self.base_url = local_endpoint(base_url)
        self.timeout = timeout
        self.num_predict = num_predict
        self.transport = transport

    def payload(self, messages):
        if not messages or any(m.get("role") not in ("system", "user", "assistant")
                               or not isinstance(m.get("content"), str) for m in messages):
            raise ValueError("Unsupported message role or content")
        return {"model": self.model, "messages": messages, "stream": False, "think": False,
                "keep_alive": 0,
                "options": {"temperature": 0, "num_predict": self.num_predict, "num_ctx": 8192}}

    def send(self, messages, request_id):
        started = time.monotonic()
        result = self.transport(self.base_url + "/api/chat", self.payload(messages), self.timeout)
        message = result.get("message", {})
        if message.get("tool_calls"):
            raise AdapterError("UNEXPECTED_TOOL_CALL_NO_EXECUTOR")
        text = message.get("content")
        if not isinstance(text, str) or not text.strip() or not result.get("done"):
            raise AdapterError("INCOMPLETE_MODEL_MESSAGE")
        usage = {k: result.get(k) for k in ("prompt_eval_count", "eval_count", "total_duration",
                                           "load_duration", "prompt_eval_duration", "eval_duration")}
        return {"text": text, "message_id": "ollama-" + uuid.uuid4().hex,
                "finish_reason": result.get("done_reason", "unknown"),
                "output_truncated": result.get("done_reason") == "length",
                "remote_thread_id": None, "request_mode": "stateless", "usage": usage,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "simulated": False, "adapter": self.name, "model": self.model}
