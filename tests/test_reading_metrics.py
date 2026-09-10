"""Owned source and scripted responses only; no model or target execution."""
from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from forge8 import explain
from forge8.inference import ChatRequest, ChatResponse, Message, response_stats
from forge8.operator import AcceptanceGateResult
from forge8.trace import verify_trace
import test_explain as fixtures
from test_resident_reading import completion


class ReadingMetricsTests(unittest.TestCase):
    # Reuse only source setup/cleanup, not the existing vertical slice tests.
    setUp = fixtures.ExplanationVerticalSliceTests.setUp
    tearDown = fixtures.ExplanationVerticalSliceTests.tearDown

    def run_reading(self, name, response, *, resident=False):
        prepared = explain.prepare_explanation(self.source, self.root / name,
            fixtures.QUESTION, name, focus=("src/service.py:3-4",))
        backend = Mock(spec=["chat"])
        backend.chat.return_value = response
        options = {}
        gate = fixtures.passing_gate
        if resident:
            options["resident_session_id"] = "model-contract"
            gate = lambda: AcceptanceGateResult(True, None, completion())
        with patch("subprocess.Popen", side_effect=AssertionError("no native child allowed")):
            outcome = explain.run_explanation(prepared, backend, model="scripted-local-model",
                reader="qwen35", acceptance_gate=gate, **options)
        backend.chat.assert_called_once()
        self.assertTrue(outcome.ok, outcome.failure_reason)
        self.assertEqual((self.source / "src" / "service.py").read_text(encoding="utf-8"),
            fixtures.SERVICE_SOURCE)
        self.assertTrue(verify_trace(prepared.run_root / "trace.jsonl",
            prepared.run_root / "trace.seal.json").ok)
        return outcome, backend.chat.call_args.args[0], prepared.run_root

    def test_cache_prompt_is_optional_and_strictly_boolean(self):
        messages = (Message("user", "Read only."),)
        default = ChatRequest("scripted", messages).as_dict()
        self.assertNotIn("cache_prompt", default)
        for value in (True, False):
            with self.subTest(value=value):
                payload = ChatRequest("scripted", messages, cache_prompt=value).as_dict()
                self.assertIs(payload.pop("cache_prompt"), value)
                self.assertEqual(payload, default)
        for value in (0, 1, "true", "false", [], {}, object()):
            with self.subTest(invalid=repr(value)):
                with self.assertRaisesRegex(ValueError, "cache_prompt must be boolean"):
                    ChatRequest("scripted", messages, cache_prompt=value).as_dict()

    def test_response_stats_keep_only_bounded_numeric_allowlisted_fields(self):
        response = ChatResponse("ordinary answer", "private-server-text",
            {"prompt_tokens": 0, "completion_tokens": True, "total_tokens": "private-token",
                "unknown_counter": 12, "server_text": "private-token"},
            {"cache_n": 8, "prompt_n": -1, "prompt_ms": float("nan"),
                "prompt_per_token_ms": False, "prompt_per_second": {"secret": "private-token"},
                "predicted_n": 3, "predicted_ms": float("inf"),
                "predicted_per_token_ms": 1.25, "predicted_per_second": 10**15 + 1,
                "unknown_counter": 42, "reasoning_content": "private-token"})
        stats = response_stats(response)
        self.assertEqual(stats, {"finish_reason": None, "usage": {"prompt_tokens": 0},
            "timings": {"cache_n": 8, "predicted_n": 3, "predicted_per_token_ms": 1.25}})
        rendered = json.dumps(stats, allow_nan=False)
        self.assertNotIn("private", rendered)
        self.assertNotIn("ordinary answer", rendered)
        self.assertNotIn("unknown_counter", rendered)

    def test_response_stats_handle_missing_sections_and_preserve_numeric_boundaries(self):
        for invalid_finish in ([], {}, True, 1):
            self.assertIsNone(response_stats(ChatResponse("", invalid_finish, {}, {}))["finish_reason"])
        for invalid in (None, [], "server text", True):
            with self.subTest(section=invalid):
                response = ChatResponse("", None, invalid, invalid)
                self.assertEqual(response_stats(response),
                    {"finish_reason": None, "usage": {}, "timings": {}})
        for finish in ("stop", "length"):
            with self.subTest(finish=finish):
                response = ChatResponse("", finish,
                    {"prompt_tokens": 10**15, "completion_tokens": 0, "total_tokens": 10**15},
                    {"cache_n": 0, "prompt_n": 10**15, "prompt_ms": 0.0,
                        "predicted_per_second": 1.5})
                self.assertEqual(response_stats(response), {"finish_reason": finish,
                    "usage": response.usage, "timings": response.timings})

    def test_direct_engine_retains_filtered_metrics_in_artifact_and_sealed_trace(self):
        response = ChatResponse("Negative totals become zero. [E1:L3-L4]", "stop",
            {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                "server_message": "NEVER_RETAIN_SERVER_TEXT"},
            {"cache_n": 0, "prompt_n": 100, "prompt_ms": 12.5,
                "predicted_n": 20, "predicted_ms": 250.0, "predicted_per_second": 80.0,
                "unknown": "NEVER_RETAIN_SERVER_TEXT"})
        outcome, request, root = self.run_reading("metrics-one-shot", response)
        expected = {"call": 1, "finish_reason": "stop",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            "timings": {"cache_n": 0, "prompt_n": 100, "prompt_ms": 12.5,
                "predicted_n": 20, "predicted_ms": 250.0, "predicted_per_second": 80.0}}
        self.assertEqual(outcome.inference["responses"], [expected])
        self.assertIsNone(outcome.inference["cache_prompt"])
        self.assertNotIn("cache_prompt", request.as_dict())
        document = json.loads(Path(outcome.explanation_path).read_text(encoding="utf-8"))
        self.assertEqual(document["inference"], outcome.inference)
        self.assertIs(document["semantic_claims_verified"], False)
        self.assertIs(document["repository_code_executed"], False)
        raw_trace = (root / "trace.jsonl").read_text(encoding="utf-8")
        events = [json.loads(line) for line in raw_trace.splitlines()]
        self.assertEqual([event["payload"] for event in events
            if event["kind"] == "inference.metrics"], [expected])
        self.assertNotIn("NEVER_RETAIN_SERVER_TEXT", raw_trace)
        self.assertNotIn("NEVER_RETAIN_SERVER_TEXT", json.dumps(document))

    def test_resident_cache_flag_changes_neither_prompt_nor_sampling(self):
        response = ChatResponse("Negative totals become zero. [E1:L3-L4]", "stop",
            {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            {"cache_n": 92, "prompt_n": 8, "prompt_ms": 1.0, "predicted_n": 20})
        ordinary, cold_request, _ = self.run_reading("cache-ordinary", response)
        resident, warm_request, _ = self.run_reading("cache-resident", response, resident=True)
        cold_payload, warm_payload = cold_request.as_dict(), warm_request.as_dict()
        self.assertNotIn("cache_prompt", cold_payload)
        self.assertIs(warm_payload.pop("cache_prompt"), True)
        self.assertEqual(warm_payload, cold_payload)
        self.assertEqual(warm_request.messages, cold_request.messages)
        self.assertIs(resident.inference["cache_prompt"], True)
        self.assertIsNone(ordinary.inference["cache_prompt"])
        self.assertEqual(resident.inference["responses"][0]["timings"], response.timings)
        self.assertEqual(resident.request_completion["session_finalization"], "pending")
        # Synthetic cache counters are retained telemetry, not a measured speedup.
        self.assertEqual(resident.inference["responses"][0]["timings"]["cache_n"], 92)


if __name__ == "__main__":
    unittest.main()
