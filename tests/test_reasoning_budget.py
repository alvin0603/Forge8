from __future__ import annotations

import json
import unittest

from forge8.inference import ChatRequest, Message


class ReasoningBudgetTests(unittest.TestCase):
    def test_omitted_or_none_budget_preserves_baseline_wire(self) -> None:
        expected = {
            "model": "fake",
            "messages": [{"role": "user", "content": "read this code"}],
            "temperature": 0.0,
            "max_tokens": 512,
            "stream": False,
            "seed": 1,
        }
        for options in ({}, {"reasoning_budget_tokens": None}, {"enable_thinking": None},
                        {"reasoning_budget_tokens": None, "enable_thinking": None}):
            with self.subTest(options=options):
                request = ChatRequest("fake", (Message("user", "read this code"),), **options)
                self.assertEqual(json.loads(json.dumps(request.as_dict())), expected)

    def test_explicit_zero_candidate_and_upper_boundary_serialize_exactly(self) -> None:
        for budget in (0, 1, 256, 2048, 4096):
            with self.subTest(budget=budget):
                request = ChatRequest("fake", (), reasoning_budget_tokens=budget)
                payload = json.loads(json.dumps(request.as_dict()))
                self.assertIs(type(payload["reasoning_budget_tokens"]), int)
                self.assertEqual(payload["reasoning_budget_tokens"], budget)
                self.assertNotIn("reasoning_budget", payload)
                self.assertNotIn("thinking_budget_tokens", payload)

    def test_budget_rejects_non_integer_negative_and_overflow_values(self) -> None:
        class IntegerSubclass(int):
            pass

        invalid = (True, False, 0.0, 256.0, float("nan"), float("inf"),
                   -1, -4096, 4097, 2**100, "256", "", [], {}, IntegerSubclass(256))
        for budget in invalid:
            with self.subTest(budget=budget):
                request = ChatRequest("fake", (), reasoning_budget_tokens=budget)
                with self.assertRaisesRegex(ValueError, "reasoning_budget_tokens"):
                    request.as_dict()

    def test_budget_coexists_with_cache_schema_and_unchanged_output_limit(self) -> None:
        options = {
            "temperature": 0.6, "max_tokens": 4096, "seed": 1,
            "response_format": {"type": "json_object", "schema": {"type": "object"}},
            "cache_prompt": True, "top_p": 0.95, "top_k": 20,
            "min_p": 0.0, "presence_penalty": 0.0, "repeat_penalty": 1.0,
        }
        baseline = ChatRequest("fake", (), **options).as_dict()
        candidate = ChatRequest("fake", (), reasoning_budget_tokens=256, **options).as_dict()
        self.assertEqual(json.loads(json.dumps(candidate)), {**baseline, "reasoning_budget_tokens": 256})

    def test_explicit_thinking_mode_changes_only_its_exact_template_key(self) -> None:
        options = {
            "temperature": 0.6, "max_tokens": 4096, "seed": 1,
            "response_format": {"type": "json_object"}, "reasoning_budget_tokens": 256,
            "cache_prompt": True, "top_p": 0.95, "top_k": 20,
            "min_p": 0.0, "presence_penalty": 0.0, "repeat_penalty": 1.0,
        }
        baseline = ChatRequest("fake", (), **options).as_dict()
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                request = ChatRequest("fake", (), enable_thinking=enabled, **options)
                wire = json.loads(json.dumps(request.as_dict()))
                self.assertEqual(wire, {**baseline, "chat_template_kwargs": {"enable_thinking": enabled}})
                self.assertIs(wire["chat_template_kwargs"]["enable_thinking"], enabled)
                self.assertNotIn("enable_thinking", wire)

    def test_thinking_mode_rejects_boolean_lookalikes_and_nested_values(self) -> None:
        for enabled in (0, 1, -1, 0.0, 1.0, "false", "true", "", [], {},
                        {"enable_thinking": False}, {"other_template_key": True}):
            with self.subTest(enabled=enabled):
                request = ChatRequest("fake", (), enable_thinking=enabled)
                with self.assertRaisesRegex(ValueError, "enable_thinking"):
                    request.as_dict()

    def test_request_has_no_arbitrary_template_kwargs_passthrough(self) -> None:
        for supplied in ({"enable_thinking": False}, {"other_template_key": "value"}):
            with self.subTest(supplied=supplied), self.assertRaises(TypeError):
                ChatRequest("fake", (), chat_template_kwargs=supplied)


if __name__ == "__main__":
    unittest.main()
