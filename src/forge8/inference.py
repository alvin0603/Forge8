"""Backend-neutral inference contracts and an OpenAI-compatible transport.

"OpenAI-compatible" is treated as a wire starting point, not a capability
guarantee. Forge8 retains only the response fields consumed by its operator.
"""

from __future__ import annotations

import json
import http.client
import math
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Callable, Literal, Mapping


Role = Literal["system", "user", "assistant"]
_STREAM_LINE_BYTES = 65_536
_STREAM_EVENT_BYTES = 65_536
_STREAM_BODY_BYTES = 4 * 1024 * 1024
_STREAM_CONTENT_CHARS = 32_768
_STREAM_SECONDS = 180.0


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str | list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ChatRequest:
    model: str
    messages: tuple[Message, ...]
    temperature: float = 0.0
    max_tokens: int = 512
    seed: int | None = 1
    response_format: dict[str, Any] | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    repeat_penalty: float | None = None
    cache_prompt: bool | None = None
    # Requested per-thinking-block budget, not a total-token or time guarantee.
    # None preserves the server default; 4096 is Forge8's application ceiling.
    reasoning_budget_tokens: int | None = None
    # Narrow template-mode override, independent of the generation budget.
    enable_thinking: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.as_dict() for message in self.messages],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self.seed is not None:
            payload["seed"] = self.seed
        if self.response_format is not None:
            payload["response_format"] = self.response_format
        if self.cache_prompt is not None:
            if type(self.cache_prompt) is not bool:
                raise ValueError("cache_prompt must be boolean")
            payload["cache_prompt"] = self.cache_prompt
        if self.reasoning_budget_tokens is not None:
            if type(self.reasoning_budget_tokens) is not int or not 0 <= self.reasoning_budget_tokens <= 4096:
                raise ValueError("reasoning_budget_tokens must be an integer from 0 to 4096")
            payload["reasoning_budget_tokens"] = self.reasoning_budget_tokens
        if self.enable_thinking is not None:
            if type(self.enable_thinking) is not bool:
                raise ValueError("enable_thinking must be boolean")
            payload["chat_template_kwargs"] = {"enable_thinking": self.enable_thinking}
        for name, value in (
            ("top_p", self.top_p),
            ("top_k", self.top_k),
            ("min_p", self.min_p),
            ("presence_penalty", self.presence_penalty),
            ("repeat_penalty", self.repeat_penalty),
        ):
            if value is not None:
                payload[name] = value
        return payload


@dataclass(frozen=True, slots=True)
class ChatResponse:
    content: str
    finish_reason: str | None
    usage: dict[str, Any]
    timings: dict[str, Any]


def response_stats(response: ChatResponse) -> dict[str, Any]:
    """Bounded numeric telemetry only; never preserve arbitrary server text."""
    fields = {
        "usage": ("prompt_tokens", "completion_tokens", "total_tokens"),
        "timings": ("cache_n", "prompt_n", "prompt_ms", "prompt_per_token_ms", "prompt_per_second",
            "predicted_n", "predicted_ms", "predicted_per_token_ms", "predicted_per_second"),
    }
    result = {"finish_reason": response.finish_reason if type(response.finish_reason) is str
        and response.finish_reason in {"stop", "length"} else None}
    for section, names in fields.items():
        raw = getattr(response, section, None)
        result[section] = {name: raw[name] for name in names if type(raw) is dict and name in raw
            and type(raw[name]) in (int, float) and 0 <= raw[name] <= 10**15 and math.isfinite(raw[name])}
    return result


class TransportError(RuntimeError):
    """Normalized network, HTTP, or provider response failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_local_request(request, *, timeout):
    # An explicit local endpoint must not inherit a corporate proxy or forward
    # its source/authentication to a redirected address. Never change globals.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request, timeout=timeout)


def _stream_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate stream JSON key")
        value[key] = item
    return value


def _stream_constant(_value):
    raise ValueError("non-finite stream JSON number")


def _stream_events(response, deadline):
    """Parse bounded b10621 SSE with public buffered reads, not socket internals.

    The deadline is checked between reads and events. One in-progress read can
    additionally take the socket timeout; this is not a hard wall-clock cutoff.
    read1 avoids waiting for a complete line while ordinary data keeps arriving.
    """
    pending = b""
    data = []
    event_bytes = total = 0
    while True:
        if time.monotonic() >= deadline:
            raise TransportError("Inference stream exceeded its elapsed-time budget")
        chunk = response.read1(4096)
        if time.monotonic() >= deadline:
            raise TransportError("Inference stream exceeded its elapsed-time budget")
        if not chunk:
            raise TransportError("Inference stream ended before [DONE]")
        total += len(chunk)
        if total > _STREAM_BODY_BYTES:
            raise TransportError("Inference stream exceeded its byte budget")
        pending += chunk
        while b"\n" in pending:
            if time.monotonic() >= deadline:
                raise TransportError("Inference stream exceeded its elapsed-time budget")
            line, pending = pending.split(b"\n", 1)
            if len(line) > _STREAM_LINE_BYTES:
                raise TransportError("Inference stream line is too large")
            line = line.removesuffix(b"\r")
            if not line:
                if data:
                    yield "\n".join(data)
                data, event_bytes = [], 0
            elif line.startswith(b":"):
                continue  # b10621 sends comment pings while no result is ready.
            elif line.startswith(b"data:"):
                event_bytes += len(line)
                if event_bytes > _STREAM_EVENT_BYTES:
                    raise TransportError("Inference stream event is too large")
                data.append(line[5:].removeprefix(b" ").decode("utf-8"))
            else:
                raise TransportError("Unexpected inference stream field")
        if len(pending) > _STREAM_LINE_BYTES:
            raise TransportError("Inference stream line is too large")


def _stream_stats(payload):
    usage, timings = payload.get("usage"), payload.get("timings")
    counts = ("prompt_tokens", "completion_tokens", "total_tokens")
    if type(usage) is not dict or any(
        type(usage.get(key)) is not int or usage[key] < 0 for key in counts
    ) or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
        raise TransportError("Malformed inference stream usage")
    if type(timings) is not dict or not timings or any(
        type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)) or value < 0
        for value in timings.values()
    ) or type(timings.get("predicted_n")) is not int or (
        timings["predicted_n"] != usage["completion_tokens"]
    ) or "predicted_per_second" not in timings:
        raise TransportError("Malformed inference stream timings")
    return usage, timings


def _is_loopback_or_private(hostname: str | None) -> bool:
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    if not hostname:
        return False
    try:
        address = socket.gethostbyname(hostname)
        first, second, *_ = (int(part) for part in address.split("."))
        return first == 10 or first == 127 or (first == 172 and 16 <= second <= 31) or (first == 192 and second == 168)
    except (OSError, ValueError):
        return False


class OpenAITransport:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        on_text: Callable[[str], None] | None = None,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "http":
            raise ValueError("Forge8 local transport currently requires an http:// endpoint")
        if not _is_loopback_or_private(parsed.hostname):
            raise ValueError(f"Refusing non-local inference endpoint: {base_url}")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        if on_text is not None and (
            not callable(on_text) or type(timeout_seconds) not in (int, float)
            or (type(timeout_seconds) is float and not math.isfinite(timeout_seconds)) or timeout_seconds <= 0
        ):
            raise ValueError("streaming requires a callable preview and finite positive timeout")
        self.on_text = on_text

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.on_text is not None:
            headers["Accept"] = "text/event-stream"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _redact(self, value: str) -> str:
        if self.api_key:
            return value.replace(self.api_key, "[REDACTED]")
        return value

    def _request(self, path: str, payload: Mapping[str, Any]):
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            timeout = self.timeout_seconds if self.on_text is None else min(self.timeout_seconds, _STREAM_SECONDS)
            return _open_local_request(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(16_384).decode("utf-8", errors="replace")
            raise TransportError(
                self._redact(
                    f"Inference endpoint returned HTTP {exc.code}: {detail}"
                )
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TransportError(
                self._redact(f"Inference endpoint request failed: {exc}")
            ) from exc

    def chat(self, request: ChatRequest) -> ChatResponse:
        if self.on_text is not None:
            return self._chat_stream(request)
        with self._request("/v1/chat/completions", request.as_dict()) as response:
            payload = json.load(response)
        try:
            choice = payload["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TransportError(
                self._redact(f"Malformed chat response: {payload!r}")
            ) from exc
        return ChatResponse(
            content=message.get("content") or "",
            finish_reason=choice.get("finish_reason"),
            usage=dict(payload.get("usage") or {}),
            timings=dict(payload.get("timings") or {}),
        )

    def _chat_stream(self, request: ChatRequest) -> ChatResponse:
        """Preview only text deltas; return a response only after complete SSE.

        The callback must be a short, nonblocking update. Preview text is not an
        accepted answer; the caller still owns parsing, citations and cleanup.
        Reasoning deltas are discarded, not returned or passed to the callback.
        Schema-constrained content remains raw wire text here; a caller must
        extract any structured preview and validate the complete final document.
        """
        payload = request.as_dict()
        payload.update(stream=True, stream_options={"include_usage": True})
        deadline = time.monotonic() + min(self.timeout_seconds, _STREAM_SECONDS)
        parts, size = [], 0
        identifier = finish = usage = timings = None
        try:
            with self._request("/v1/chat/completions", payload) as response:
                if response.headers.get_content_type() != "text/event-stream":
                    raise TransportError("Inference endpoint did not return an event stream")
                for event in _stream_events(response, deadline):
                    if event == "[DONE]":
                        if finish is None or usage is None:
                            raise TransportError("Inference stream ended without finish and usage")
                        return ChatResponse("".join(parts), finish, usage, timings)
                    item = json.loads(event, object_pairs_hook=_stream_object, parse_constant=_stream_constant)
                    if type(item) is not dict or "error" in item:
                        raise TransportError("Inference stream returned an error or malformed event")
                    current_id = item.get("id")
                    if item.get("object") != "chat.completion.chunk" or type(current_id) is not str or not current_id:
                        raise TransportError("Malformed inference stream identity")
                    if identifier is not None and current_id != identifier:
                        raise TransportError("Inference stream changed completion identity")
                    identifier = current_id
                    choices = item.get("choices")
                    if type(choices) is not list or len(choices) > 1:
                        raise TransportError("Inference stream requires exactly one completion")
                    if not choices:
                        if finish is None or usage is not None:
                            raise TransportError("Inference stream usage is out of order")
                        usage, timings = _stream_stats(item)
                        continue
                    if finish is not None or item.get("usage") is not None:
                        raise TransportError("Inference stream completion is out of order")
                    choice = choices[0]
                    if type(choice) is not dict or type(choice.get("index")) is not int or choice["index"] != 0:
                        raise TransportError("Malformed inference stream choice")
                    delta = choice.get("delta")
                    if type(delta) is not dict or set(delta) - {"role", "content", "reasoning_content"}:
                        raise TransportError("Inference stream is not text-only")
                    if "role" in delta and delta["role"] != "assistant":
                        raise TransportError("Unexpected inference stream role")
                    if any(value is not None and type(value) is not str for value in delta.values()):
                        raise TransportError("Malformed inference stream text")
                    if "finish_reason" not in choice or choice["finish_reason"] not in (None, "stop", "length"):
                        raise TransportError("Unexpected inference stream finish reason")
                    finish = choice["finish_reason"]
                    if finish is not None and delta:
                        raise TransportError("Inference stream finish contains an unexpected delta")
                    text = delta.get("content") or ""
                    if text:
                        text.encode("utf-8")  # Reject escaped lone surrogates before preview.
                        size += len(text)
                        if size > _STREAM_CONTENT_CHARS:
                            raise TransportError("Inference stream answer is too large")
                        parts.append(text)
                        try:
                            self.on_text(text)
                        except Exception:
                            raise TransportError("Inference stream preview callback failed") from None
        except (ValueError, TypeError, RecursionError, OSError, http.client.HTTPException):
            raise TransportError("Inference stream could not be read as a complete response") from None


class CancellableTransport(OpenAITransport):
    """Abort only this request's owned server; join before the caller's cleanup.

    The watcher exists only during chat, after server startup. It never injects
    an exception into another thread or leaves a stop racing the acceptance gate.
    """

    def __init__(self, *args, cancel_event: threading.Event, abort: Callable[[], object], **kwargs):
        super().__init__(*args, **kwargs)
        self.cancel_event = cancel_event
        self.abort = abort

    def chat(self, request: ChatRequest) -> ChatResponse:
        if self.cancel_event.is_set():
            raise KeyboardInterrupt("local inference cancelled")
        done = threading.Event()
        abort_failed = []

        def watch():
            while not done.wait(0.05):
                if self.cancel_event.is_set():
                    try:
                        self.abort()
                    except BaseException:
                        abort_failed.append(True)
                    return

        watcher = threading.Thread(target=watch, name="forge8-inference-cancel", daemon=True)
        watcher.start()
        try:
            response = super().chat(request)
        except BaseException:
            if self.cancel_event.is_set():
                raise KeyboardInterrupt("local inference cancelled") from None
            raise
        finally:
            done.set()
            watcher.join()
        if abort_failed:
            raise TransportError("local cancellation cleanup failed")
        if self.cancel_event.is_set():
            raise KeyboardInterrupt("local inference cancelled")
        return response
