from __future__ import annotations

import json
import io
import threading
import unittest
import urllib.error
from unittest.mock import Mock, patch

from forge8.inference import (
    ChatRequest,
    ChatResponse,
    CancellableTransport,
    Message,
    OpenAITransport,
    TransportError,
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ChatRequestTests(unittest.TestCase):
    def test_default_sampler_omission_preserves_legacy_wire_request(self) -> None:
        request = ChatRequest(model="fake", messages=(Message("user", "hello"),))
        self.assertEqual(request.as_dict(), {
            "model": "fake",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.0,
            "max_tokens": 512,
            "stream": False,
            "seed": 1,
        })

    def test_unspecified_sampler_preserves_legacy_optional_fields(self) -> None:
        request = ChatRequest(
            model="fake", messages=(), temperature=0.25, max_tokens=32,
            seed=None, response_format={"type": "json_object"},
        )
        self.assertEqual(request.as_dict(), {
            "model": "fake",
            "messages": [],
            "temperature": 0.25,
            "max_tokens": 32,
            "stream": False,
            "response_format": {"type": "json_object"},
        })

    def test_precise_coding_sampler_serializes_explicit_zero_values(self) -> None:
        request = ChatRequest(
            model="fake", messages=(Message("user", "explain this code"),),
            temperature=0.6, max_tokens=4096, top_p=0.95, top_k=20,
            min_p=0.0, presence_penalty=0.0, repeat_penalty=1.0,
        )
        self.assertEqual(json.loads(json.dumps(request.as_dict())), {
            "model": "fake",
            "messages": [{"role": "user", "content": "explain this code"}],
            "temperature": 0.6,
            "max_tokens": 4096,
            "stream": False,
            "seed": 1,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "repeat_penalty": 1.0,
        })


class TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = OpenAITransport("http://127.0.0.1:8080")

    def test_chat(self) -> None:
        chat_payload = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            "timings": {"predicted_per_second": 42.0},
        }
        with patch("forge8.inference._open_local_request", return_value=FakeResponse(json.dumps(chat_payload).encode())):
            request = ChatRequest(model="fake", messages=(Message("user", "hello"),))
            response = self.transport.chat(request)
        self.assertEqual(response.content, "ok")
        self.assertEqual(response.timings["predicted_per_second"], 42.0)

    def test_remote_endpoint_rejected_by_default(self) -> None:
        with self.assertRaises(ValueError):
            OpenAITransport("https://api.example.com")

    def test_http_and_malformed_response_errors_redact_in_memory_api_key(self) -> None:
        secret = "local-secret-must-not-enter-evidence"
        transport = OpenAITransport("http://127.0.0.1:8080", api_key=secret)
        http_error = urllib.error.HTTPError(
            "http://127.0.0.1:8080/v1/chat/completions",
            401,
            "unauthorized",
            {},
            FakeResponse(f"reflected Authorization: Bearer {secret}".encode()),
        )
        with (
            patch("forge8.inference._open_local_request", side_effect=http_error),
            self.assertRaises(TransportError) as caught,
        ):
            transport.chat(
                ChatRequest(model="fake", messages=(Message("user", "hello"),))
            )
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

        malformed = FakeResponse(
            json.dumps({"reflected_authorization": secret}).encode("utf-8")
        )
        request = ChatRequest(model="fake", messages=(Message("user", "hello"),))
        with (
            patch("forge8.inference._open_local_request", return_value=malformed),
            self.assertRaises(TransportError) as malformed_error,
        ):
            transport.chat(request)
        self.assertNotIn(secret, str(malformed_error.exception))
        self.assertIn("[REDACTED]", str(malformed_error.exception))


class CancellableTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = ChatRequest(model="fake", messages=(Message("user", "hello"),))
        self.response = ChatResponse("answer", "stop", {"completion_tokens": 1}, {})
        self.cancel = threading.Event()
        self.abort = Mock()
        self.transport = CancellableTransport(
            "http://127.0.0.1:8080", cancel_event=self.cancel, abort=self.abort,
        )
        self.thread_type = threading.Thread
        self.watchers: list[threading.Thread] = []

    def make_watcher(self, *args, **kwargs):
        watcher = self.thread_type(*args, **kwargs)
        self.watchers.append(watcher)
        return watcher

    def assert_watcher_joined(self) -> None:
        self.assertEqual(len(self.watchers), 1)
        self.assertFalse(self.watchers[0].is_alive())

    def test_pre_cancelled_request_never_calls_base_transport_or_abort(self) -> None:
        self.cancel.set()
        with (
            patch.object(OpenAITransport, "chat", autospec=True) as chat,
            patch("forge8.inference.threading.Thread") as watcher,
            self.assertRaisesRegex(KeyboardInterrupt, "cancelled"),
        ):
            self.transport.chat(self.request)
        chat.assert_not_called()
        watcher.assert_not_called()
        self.abort.assert_not_called()

    def test_normal_response_is_preserved_without_abort_or_surviving_watcher(self) -> None:
        with (
            patch.object(OpenAITransport, "chat", autospec=True, return_value=self.response) as chat,
            patch("forge8.inference.threading.Thread", side_effect=self.make_watcher),
        ):
            response = self.transport.chat(self.request)
            self.assert_watcher_joined()
        self.assertIs(response, self.response)
        chat.assert_called_once_with(self.transport, self.request)
        self.abort.assert_not_called()

    def test_uncancelled_transport_error_is_preserved_after_watcher_join(self) -> None:
        failure = TransportError("original local transport failure")
        with (
            patch.object(OpenAITransport, "chat", autospec=True, side_effect=failure),
            patch("forge8.inference.threading.Thread", side_effect=self.make_watcher),
            self.assertRaises(TransportError) as caught,
        ):
            self.transport.chat(self.request)
        self.assertIs(caught.exception, failure)
        self.assert_watcher_joined()
        self.abort.assert_not_called()

    def test_cancel_during_blocked_request_aborts_owned_server_and_raises_interrupt(self) -> None:
        aborted = threading.Event()
        self.abort.side_effect = aborted.set

        def blocked_chat(transport, request):
            self.assertIs(transport, self.transport)
            self.assertIs(request, self.request)
            self.cancel.set()  # Already inside the base request, not pre-cancelled.
            if not aborted.wait(2):
                raise AssertionError("owned abort did not unblock the fake request")
            raise TransportError("connection closed by owned server stop")

        with (
            patch.object(OpenAITransport, "chat", autospec=True, side_effect=blocked_chat) as chat,
            patch("forge8.inference.threading.Thread", side_effect=self.make_watcher),
            self.assertRaisesRegex(KeyboardInterrupt, "cancelled"),
        ):
            self.transport.chat(self.request)
        chat.assert_called_once_with(self.transport, self.request)
        self.abort.assert_called_once_with()
        self.assertTrue(aborted.is_set())
        self.assert_watcher_joined()

    def test_abort_finishes_and_watcher_joins_before_control_returns_to_caller(self) -> None:
        abort_started = threading.Event()
        release_abort = threading.Event()
        abort_finished = threading.Event()
        join_started = threading.Event()
        join_finished = threading.Event()
        chat_finished = threading.Event()
        outcome: dict[str, object] = {}

        def slow_abort():
            abort_started.set()
            if not release_abort.wait(2):
                raise AssertionError("test did not release the fake owned stop")
            abort_finished.set()

        def completed_chat(_transport, _request):
            self.cancel.set()
            if not abort_started.wait(2):
                raise AssertionError("cancel watcher did not enter the owned stop")
            return self.response

        def observed_watcher(*args, **kwargs):
            watcher = self.make_watcher(*args, **kwargs)
            original_join = watcher.join

            def observed_join(*join_args, **join_kwargs):
                join_started.set()
                original_join(*join_args, **join_kwargs)
                join_finished.set()

            watcher.join = observed_join
            return watcher

        def caller():
            try:
                outcome["response"] = self.transport.chat(self.request)
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                outcome["abort_complete_before_return"] = abort_finished.is_set()
                outcome["join_complete_before_return"] = join_finished.is_set()
                chat_finished.set()

        self.abort.side_effect = slow_abort
        driver = self.thread_type(target=caller, daemon=True)
        with (
            patch.object(OpenAITransport, "chat", autospec=True, side_effect=completed_chat),
            patch("forge8.inference.threading.Thread", side_effect=observed_watcher),
        ):
            driver.start()
            try:
                self.assertTrue(join_started.wait(2), "chat did not wait for its cancel watcher")
                self.assertTrue(abort_started.is_set())
                self.assertFalse(abort_finished.is_set())
                self.assertFalse(chat_finished.is_set())
            finally:
                release_abort.set()
                driver.join(2)
        self.assertFalse(driver.is_alive())
        self.assertTrue(chat_finished.is_set())
        self.assertNotIn("response", outcome)
        self.assertIsInstance(outcome.get("error"), KeyboardInterrupt)
        self.assertTrue(outcome["abort_complete_before_return"])
        self.assertTrue(outcome["join_complete_before_return"])
        self.abort.assert_called_once_with()
        self.assert_watcher_joined()


if __name__ == "__main__":
    unittest.main()
