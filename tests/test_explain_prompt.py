from __future__ import annotations

import json
import unittest

from forge8 import explain


class ExplanationPromptTests(unittest.TestCase):
    def test_wire_terminal_bounds_match_the_host_parser(self):
        variants = explain.explain_action_envelope_schema()["properties"]["action"]["oneOf"]
        answer = next(v for v in variants if v["properties"]["kind"]["enum"] == ["answer"])
        claims = answer["properties"]["claims"]
        self.assertEqual((claims["minItems"], claims["maxItems"]), (2, 10))
        inference = next(v for v in claims["items"]["oneOf"] if "text" in v["properties"])
        citations = inference["properties"]["citations"]
        self.assertEqual((citations["minItems"], citations["maxItems"]), (1, 3))
        self.assertEqual(inference["properties"]["text"]["maxLength"], 600)

    def setUp(self) -> None:
        self.examples = [
            json.loads(line)
            for line in explain._SYSTEM_PROMPT.splitlines()
            if line.startswith('{"rationale":')
        ]

    def test_visible_examples_cover_every_read_only_wire_action(self) -> None:
        schema = explain.explain_action_envelope_schema()
        variants = {
            variant["properties"]["kind"]["enum"][0]: variant
            for variant in schema["properties"]["action"]["oneOf"]
        }
        self.assertEqual(
            {example["action"]["kind"] for example in self.examples},
            set(variants),
        )
        self.assertEqual(len(self.examples), len(variants))
        for example in self.examples:
            with self.subTest(kind=example["action"]["kind"]):
                self.assertEqual(set(example), set(schema["required"]))
                self.assertEqual(
                    set(example["action"]),
                    set(variants[example["action"]["kind"]]["required"]),
                )
                rationale, action = explain.parse_explain_action_envelope(
                    json.dumps(example)
                )
                kind = action["kind"] if isinstance(action, dict) else action.kind
                self.assertEqual(kind, example["action"]["kind"])
                self.assertLess(len(rationale), 100)
                self.assertNotIn("\n", rationale)

    def test_answer_example_does_not_grant_evidence(self) -> None:
        answer = next(
            example for example in self.examples
            if example["action"]["kind"] == "answer"
        )
        _, parsed = explain.parse_explain_action_envelope(json.dumps(answer))
        self.assertEqual(
            {claim["type"] for claim in parsed["claims"]},
            {"source_quote", "inference"},
        )
        self.assertNotIn("text", answer["action"]["claims"][0])
        with self.assertRaisesRegex(explain.ExplanationError, "non-retained evidence"):
            explain._validate_answer(parsed["claims"], [])

    def test_prompt_separates_decision_from_answer_and_examples_from_evidence(self) -> None:
        prompt = explain._SYSTEM_PROMPT
        self.assertIn("one short sentence describing the next decision, not the", prompt)
        self.assertIn("Put the explanation in action.claims.", prompt)
        self.assertIn("WIRE EXAMPLES (syntax only, not task evidence)", prompt)
        self.assertIn("actually retained in CITABLE EVIDENCE", prompt)
        self.assertIn("choose answer instead of rereading", prompt)


if __name__ == "__main__":
    unittest.main()
