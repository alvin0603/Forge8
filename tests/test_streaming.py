from __future__ import annotations

import io
import json
import threading
import unittest
from email.message import Message as Headers
from unittest.mock import Mock, patch

from forge8.inference import ChatRequest, CancellableTransport, OpenAITransport, TransportError


def chunk(delta=None, *, finish=None, **extra):
    return {
        "object": "chat.completion.chunk", "id": "chat-test",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
        **extra,
    }


def statistics(**extra):
    return chunk(choices=[], usage={"prompt_tokens": 3, "completion_tokens": 2,
        "total_tokens": 5, "prompt_tokens_details": {"cached_tokens": 0}},
        timings={"predicted_n": 2, "predicted_per_second": 40.5, "prompt_ms": 1.0}, **extra)


def event(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ("data: " + text + "\n\n").encode("utf-8")


def complete(*deltas, finish="stop"):
    return b"".join(event(value) for value in (
        chunk({"role": "assistant", "content": None}), *deltas,
        chunk(finish=finish), statistics(), "[DONE]"))


class StreamResponse(io.BytesIO):
    def __init__(self, body, *, width=4096, content_type="text/event-stream"):
        super().__init__(body)
        self.width = width
        self.headers = Headers()
        self.headers["Content-Type"] = content_type

    def read1(self, size=-1):
        return super().read1(min(size, self.width))


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.request = ChatRequest(model="fake", messages=())
        self.preview = []
        self.transport = OpenAITransport("http://127.0.0.1:8080", on_text=self.preview.append)

    def chat(self, body, **response_options):
        response = StreamResponse(body, **response_options)
        with patch("forge8.inference._open_local_request", return_value=response):
            try:
                return self.transport.chat(self.request)
            finally:
                self.assertTrue(response.closed)

    def test_wire_utf8_reasoning_ignored_and_final_usage_retained(self):
        body = complete(chunk({"reasoning_content": "PRIVATE THOUGHT"}),
            chunk({"content": "繁體🙂"}), chunk({"content": "程式"}))
        response = StreamResponse(body, width=1)

        def preview(text):
            self.assertFalse(response.closed)
            self.assertLess(response.tell(), len(body))
            self.preview.append(text)

        self.transport.on_text = preview
        with patch("forge8.inference._open_local_request", return_value=response) as opened:
            result = self.transport.chat(self.request)
        sent = opened.call_args.args[0]
        expected = {**self.request.as_dict(), "stream": True, "stream_options": {"include_usage": True}}
        self.assertEqual(json.loads(sent.data), expected)
        self.assertEqual(sent.get_header("Accept"), "text/event-stream")
        self.assertFalse(self.request.as_dict()["stream"])
        self.assertEqual(result.content, "繁體🙂程式")
        self.assertEqual(self.preview, ["繁體🙂", "程式"])
        self.assertEqual(result.usage, statistics()["usage"])
        self.assertEqual(result.timings, statistics()["timings"])
        self.assertEqual(result.finish_reason, "stop")
        self.assertTrue(response.closed)
        self.assertNotIn("PRIVATE THOUGHT", repr(result))

    def test_comment_crlf_and_multiline_data_events(self):
        first = json.dumps(chunk({"content": "answer"})).replace(', "id"', ',\n"id"')
        multiline = ("data: " + first.replace("\n", "\ndata: ") + "\n\n").encode()
        body = b":\n\n" + multiline + event(chunk(finish="stop")) + event(statistics()) + event("[DONE]")
        result = self.chat(body.replace(b"\n", b"\r\n"), width=7)
        self.assertEqual(result.content, "answer")

    def test_length_finish_is_preserved_for_existing_acceptance_gate(self):
        result = self.chat(complete(chunk({"content": "partial"}), finish="length"))
        self.assertEqual(result.finish_reason, "length")

    def test_incomplete_endings_are_never_success(self):
        prefix = event(chunk({"content": "preview"}))
        for ending in (
            b"", event("[DONE]"), event(chunk(finish="stop")),
            event(chunk(finish="stop")) + event("[DONE]"),
            event(chunk(finish="stop")) + event(statistics()),
            event(chunk(finish="stop")) + event(statistics()) + b"data: [DONE]\n",
        ):
            with self.subTest(ending=ending), self.assertRaises(TransportError):
                self.chat(prefix + ending)

    def test_invalid_events_and_nontext_choices_fail_closed(self):
        bad = (
            {"error": {"message": "local-secret"}}, [], "not-json",
            chunk(choices={}), chunk(choices=[{}, {}]), chunk(choices=[{"index": True}]),
            chunk({"tool_calls": []}), chunk({"function_call": {}}),
            chunk({"content": ["not text"]}), chunk({"reasoning_content": 5}),
            chunk({"role": "user"}), chunk(finish="tool_calls"),
            chunk({"content": "last"}, finish="stop"),
            chunk(id=""), chunk(object="chat.completion"),
            chunk({"content": "\ud800"}),
            '{"id":"a","id":"b"}', '{"id":NaN}',
        )
        for value in bad:
            with self.subTest(value=repr(value)), self.assertRaises(TransportError):
                body = ("data: " + json.dumps(value) + "\n\n").encode() if isinstance(value, dict) else event(value)
                self.chat(body)

    def test_identity_order_and_duplicate_finish_or_usage_are_rejected(self):
        for values in (
            [chunk(id="a"), chunk(id="b")], [statistics()],
            [chunk(finish="stop"), chunk({"content": "late"})],
            [chunk(finish="stop"), chunk(finish="stop")],
            [chunk(finish="stop"), statistics(), statistics()],
        ):
            with self.subTest(values=values), self.assertRaises(TransportError):
                self.chat(b"".join(event(value) for value in values))

    def test_malformed_usage_or_timings_are_rejected(self):
        for field, value in (
            ("usage", None), ("usage", {}),
            ("usage", {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3}),
            ("usage", {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 6}),
            ("timings", None), ("timings", {}),
            ("timings", {"predicted_n": 1, "predicted_per_second": 40}),
            ("timings", {"predicted_n": True, "predicted_per_second": 40}),
            ("timings", {"predicted_n": 2, "predicted_per_second": -1}),
            ("timings", {"predicted_n": 2, "predicted_per_second": float("inf")}),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(TransportError):
                stats = statistics()
                stats[field] = value
                self.chat(event(chunk(finish="stop")) + event(stats) + event("[DONE]"))

    def test_bad_framing_encoding_and_content_type_are_rejected(self):
        for body in (b"event: token\n\n", b"data: \xff\n\n"):
            with self.subTest(body=body), self.assertRaises(TransportError):
                self.chat(body)
        with self.assertRaises(TransportError):
            self.chat(complete(), content_type="application/json")

    def test_line_event_body_and_content_limits(self):
        cases = (
            ("_STREAM_LINE_BYTES", 8, b"data: " + b"x" * 9),
            ("_STREAM_EVENT_BYTES", 12, b"data: 1\ndata: 2\n\n"),
            ("_STREAM_BODY_BYTES", 9, b":\n\n" * 4),
            ("_STREAM_CONTENT_CHARS", 3, complete(chunk({"content": "ab"}), chunk({"content": "cd"}))),
        )
        for name, limit, body in cases:
            self.preview.clear()
            with self.subTest(name=name), patch("forge8.inference." + name, limit), self.assertRaises(TransportError):
                self.chat(body, width=1)
            if name == "_STREAM_CONTENT_CHARS":
                self.assertEqual(self.preview, ["ab"])

    def test_continuous_pings_and_partial_lines_do_not_extend_deadline(self):
        for body in (b":\n\n" * 50, b"data: " + b"x" * 50):
            now = [0.0]
            response = StreamResponse(body, width=1)
            read = response.read1

            def advancing_read(size):
                now[0] += 1
                return read(size)

            response.read1 = advancing_read
            with (
                self.subTest(body=body),
                patch("forge8.inference.time.monotonic", side_effect=lambda: now[0]),
                patch("forge8.inference._open_local_request", return_value=response),
                self.assertRaisesRegex(TransportError, "elapsed-time"),
            ):
                OpenAITransport("http://127.0.0.1:8080", on_text=Mock(), timeout_seconds=4).chat(self.request)
            self.assertEqual(now[0], 4)
            self.assertTrue(response.closed)

    def test_stream_only_timeout_validation_and_cap(self):
        for timeout in (None, True, 0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                OpenAITransport("http://127.0.0.1:8080", on_text=Mock(), timeout_seconds=timeout)
        with self.assertRaises(ValueError):
            OpenAITransport("http://127.0.0.1:8080", on_text="not callable")
        with patch("forge8.inference._open_local_request", return_value=StreamResponse(complete())) as opened:
            OpenAITransport("http://127.0.0.1:8080", on_text=Mock(), timeout_seconds=1000).chat(self.request)
        self.assertEqual(opened.call_args.kwargs["timeout"], 180)

    def test_schema_stream_forwards_format_and_preserves_raw_json_content(self):
        response_format = {"type": "json_schema", "json_schema": {
            "name": "reading_example", "strict": True,
            "schema": {"type": "object", "properties": {"text": {"type": "string"}},
                       "required": ["text"], "additionalProperties": False},
        }}
        request = ChatRequest(model="fake", messages=(), response_format=response_format,
            cache_prompt=True, reasoning_budget_tokens=256)
        raw = json.dumps({"text": '繁體🙂 "quoted"\nnext'}, ensure_ascii=True)
        pieces = (raw[:12], raw[12:25], raw[25:])
        body = complete(chunk({"reasoning_content": "PRIVATE THOUGHT"}),
            *(chunk({"content": piece}) for piece in pieces))
        response = StreamResponse(body, width=1)
        with patch("forge8.inference._open_local_request", return_value=response) as opened:
            result = self.transport.chat(request)
        sent = json.loads(opened.call_args.args[0].data)
        self.assertEqual(sent, {**request.as_dict(), "stream": True,
                                "stream_options": {"include_usage": True}})
        self.assertEqual(sent["response_format"], response_format)
        self.assertEqual(result.content, raw)  # Final parser, not transport, owns JSON validation.
        self.assertEqual(self.preview, list(pieces))  # Caller receives partial JSON wire text.
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.usage, statistics()["usage"])
        self.assertEqual(result.timings, statistics()["timings"])
        self.assertNotIn("PRIVATE THOUGHT", repr(result))
        self.assertNotIn("PRIVATE THOUGHT", repr(self.preview))
        self.assertFalse(request.as_dict()["stream"])
        self.assertTrue(response.closed)

    def test_callback_and_stream_io_errors_are_inert(self):
        self.transport.on_text = Mock(side_effect=ValueError("secret callback error"))
        with self.assertRaises(TransportError) as caught:
            self.chat(complete(chunk({"content": "text"})))
        self.assertNotIn("secret", str(caught.exception))
        response = StreamResponse(b"")
        response.read1 = Mock(side_effect=OSError("secret socket error"))
        with patch("forge8.inference._open_local_request", return_value=response), self.assertRaises(TransportError) as caught:
            self.transport.chat(self.request)
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(response.closed)


class StreamingCancellationTests(unittest.TestCase):
    def test_cancel_unblocks_stream_and_joins_watcher_before_return(self):
        cancel = threading.Event()
        abort_started, release_abort, join_started = (threading.Event() for _ in range(3))
        finished, failures, watchers = threading.Event(), [], []
        real_thread = threading.Thread

        class Watcher(real_thread):
            def join(self, timeout=None):
                join_started.set()
                return super().join(timeout)

        def make_watcher(*args, **kwargs):
            watcher = Watcher(*args, **kwargs)
            watchers.append(watcher)
            return watcher

        def abort():
            abort_started.set()
            if not release_abort.wait(2):
                raise AssertionError("test did not release owned abort")

        response = StreamResponse(b"")

        def blocked_read(_size):
            cancel.set()
            if not abort_started.wait(2):
                raise AssertionError("owned abort did not start")
            raise OSError("server stopped")

        response.read1 = blocked_read
        stop = Mock(side_effect=abort)
        transport = CancellableTransport("http://127.0.0.1:8080", on_text=Mock(),
            cancel_event=cancel, abort=stop)

        def run():
            try:
                transport.chat(ChatRequest(model="fake", messages=()))
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()

        worker = real_thread(target=run)
        with patch("forge8.inference._open_local_request", return_value=response), patch("forge8.inference.threading.Thread", side_effect=make_watcher):
            worker.start()
            try:
                self.assertTrue(join_started.wait(2))
                self.assertFalse(finished.is_set())
                self.assertTrue(watchers[0].is_alive())
            finally:
                release_abort.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], KeyboardInterrupt)
        self.assertFalse(watchers[0].is_alive())
        self.assertTrue(response.closed)
        stop.assert_called_once_with()

    def test_normal_stream_returns_after_watcher_join_without_abort(self):
        stop, preview, watchers = Mock(), [], []
        real_thread = threading.Thread

        def make_watcher(*args, **kwargs):
            watcher = real_thread(*args, **kwargs)
            watchers.append(watcher)
            return watcher

        transport = CancellableTransport("http://127.0.0.1:8080", on_text=preview.append,
            cancel_event=threading.Event(), abort=stop)
        with patch("forge8.inference._open_local_request", return_value=StreamResponse(complete(chunk({"content": "answer"})))), patch("forge8.inference.threading.Thread", side_effect=make_watcher):
            result = transport.chat(ChatRequest(model="fake", messages=()))
            self.assertFalse(watchers[0].is_alive())
        self.assertEqual(result.content, "answer")
        self.assertEqual(preview, ["answer"])
        stop.assert_not_called()
