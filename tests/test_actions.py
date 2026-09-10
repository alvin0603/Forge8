from __future__ import annotations

import json
import unittest

from forge8.actions import (
    ACTION_ENVELOPE_SCHEMA,
    MAX_CHECK_OUTPUT_CHARS,
    MAX_CHECK_TIMEOUT_SECONDS,
    MAX_CHECKS,
    MAX_FINISH_SUMMARY_CHARS,
    MAX_LLAMA_CPP_RATIONALE_CHARS,
    MAX_LINE_NUMBER,
    MAX_LIST_FILES,
    MAX_QUERY_CHARS,
    MAX_READ_LINES,
    MAX_RATIONALE_CHARS,
    MAX_SEARCH_RESULTS,
    MAX_WRITE_BYTES,
    ActionJSONError,
    ActionValidationError,
    FinishAction,
    ListFilesAction,
    ReadTextAction,
    ReplaceLinesAction,
    ReplaceTextAction,
    RunChecksAction,
    SearchTextAction,
    WriteTextAction,
    action_envelope_schema,
    llama_cpp_action_envelope_schema,
    parse_action_envelope,
    parse_llama_cpp_action_envelope,
    validate_action_envelope,
)


def document(action: dict[str, object], rationale: str = "Use the next bounded step.") -> str:
    return json.dumps(
        {"rationale": rationale, "action": action},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class ActionSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = action_envelope_schema()
        variants = self.schema["properties"]["action"]["oneOf"]
        self.variants = {
            variant["properties"]["kind"]["const"]: variant for variant in variants
        }

    def test_schema_is_deliberation_first_and_action_last(self) -> None:
        self.assertEqual(list(self.schema["properties"]), ["rationale", "action"])
        self.assertEqual(self.schema["required"], ["rationale", "action"])
        self.assertFalse(self.schema["additionalProperties"])

    def test_llama_cpp_projection_keeps_order_and_removes_unsupported_keywords(self) -> None:
        projected = llama_cpp_action_envelope_schema()
        self.assertEqual(list(projected["properties"]), ["rationale", "action"])
        self.assertEqual(
            projected["properties"]["rationale"]["minLength"],
            1,
        )
        self.assertEqual(
            projected["properties"]["rationale"]["maxLength"],
            MAX_LLAMA_CPP_RATIONALE_CHARS,
        )
        variants = projected["properties"]["action"]["oneOf"]
        self.assertEqual(
            [variant["properties"]["kind"]["enum"][0] for variant in variants],
            [
                "list_files",
                "read_text",
                "search_text",
                "replace_lines",
                "write_text",
                "replace_text",
                "run_checks",
                "finish",
            ],
        )
        wire_variants = {
            variant["properties"]["kind"]["enum"][0]: variant
            for variant in variants
        }
        self.assertEqual(
            list(wire_variants["read_text"]["properties"]),
            ["kind", "path", "start_line", "line_count"],
        )
        self.assertEqual(
            wire_variants["read_text"]["required"],
            ["kind", "path", "start_line", "line_count"],
        )
        self.assertNotIn("end_line", wire_variants["read_text"]["properties"])
        self.assertEqual(
            wire_variants["read_text"]["properties"]["start_line"]["minimum"],
            1,
        )
        self.assertEqual(
            wire_variants["read_text"]["properties"]["start_line"]["maximum"],
            MAX_LINE_NUMBER,
        )
        self.assertEqual(
            wire_variants["read_text"]["properties"]["line_count"]["minimum"],
            1,
        )
        self.assertEqual(
            wire_variants["read_text"]["properties"]["line_count"]["maximum"],
            MAX_READ_LINES,
        )
        self.assertEqual(
            list(wire_variants["replace_lines"]["properties"]),
            [
                "kind",
                "path",
                "expected_sha256",
                "start_line",
                "end_line",
                "new_text",
            ],
        )
        self.assertEqual(
            wire_variants["replace_lines"]["required"],
            list(wire_variants["replace_lines"]["properties"]),
        )
        self.assertEqual(
            wire_variants["replace_lines"]["properties"]["start_line"]["minimum"],
            1,
        )
        self.assertEqual(
            wire_variants["replace_lines"]["properties"]["end_line"]["maximum"],
            MAX_LINE_NUMBER,
        )
        self.assertNotIn(
            "maxLength",
            wire_variants["replace_lines"]["properties"]["new_text"],
        )
        self.assertEqual(
            list(wire_variants["write_text"]["properties"]),
            ["kind", "path", "expected_sha256", "content"],
        )
        self.assertEqual(
            list(wire_variants["replace_text"]["properties"]),
            ["kind", "path", "expected_sha256", "old_text", "new_text"],
        )
        self.assertNotIn(
            "maxLength",
            wire_variants["replace_text"]["properties"]["new_text"],
        )
        self.assertEqual(
            list(wire_variants["finish"]["properties"]),
            ["kind", "summary"],
        )

        unsupported = {
            "$schema",
            "title",
            "description",
            "const",
            "pattern",
            "minItems",
            "maxItems",
            "uniqueItems",
        }

        string_bound_paths = []

        def visit(value, path=()):
            if isinstance(value, dict):
                self.assertFalse(unsupported & set(value))
                if "minLength" in value or "maxLength" in value:
                    string_bound_paths.append(path)
                for key, child in value.items():
                    visit(child, path + (key,))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, path + (index,))

        visit(projected)
        self.assertEqual(string_bound_paths, [("properties", "rationale")])
        self.assertNotIn(
            "maxLength",
            wire_variants["write_text"]["properties"]["content"],
        )

    def test_action_variant_order_is_stable(self) -> None:
        kinds = [
            variant["properties"]["kind"]["const"]
            for variant in self.schema["properties"]["action"]["oneOf"]
        ]
        self.assertEqual(
            kinds,
            [
                "list_files",
                "read_text",
                "search_text",
                "replace_lines",
                "write_text",
                "replace_text",
                "run_checks",
                "finish",
            ],
        )

    def test_write_effect_content_is_last(self) -> None:
        write = self.variants["write_text"]
        self.assertEqual(
            list(write["properties"]),
            ["kind", "path", "expected_sha256", "content"],
        )
        self.assertEqual(write["required"], list(write["properties"]))

    def test_replace_lines_effect_new_text_is_last(self) -> None:
        replace = self.variants["replace_lines"]
        self.assertEqual(
            list(replace["properties"]),
            [
                "kind",
                "path",
                "expected_sha256",
                "start_line",
                "end_line",
                "new_text",
            ],
        )
        self.assertEqual(replace["required"], list(replace["properties"]))
        guard = replace["properties"]["expected_sha256"]
        self.assertEqual((guard["minLength"], guard["maxLength"]), (64, 64))
        self.assertEqual(replace["properties"]["start_line"]["minimum"], 1)
        self.assertEqual(
            replace["properties"]["end_line"]["maximum"], MAX_LINE_NUMBER
        )

    def test_replace_effect_new_text_is_last(self) -> None:
        replace = self.variants["replace_text"]
        self.assertEqual(
            list(replace["properties"]),
            ["kind", "path", "expected_sha256", "old_text", "new_text"],
        )
        self.assertEqual(replace["required"], list(replace["properties"]))
        guard = replace["properties"]["expected_sha256"]
        self.assertEqual((guard["minLength"], guard["maxLength"]), (64, 64))
        self.assertEqual(replace["properties"]["old_text"]["minLength"], 1)

    def test_finish_has_no_effect_fields(self) -> None:
        finish = self.variants["finish"]
        self.assertEqual(list(finish["properties"]), ["kind", "summary"])
        self.assertEqual(finish["required"], ["kind", "summary"])
        self.assertFalse(finish["additionalProperties"])
        for field in (
            "path",
            "content",
            "expected_sha256",
            "old_text",
            "new_text",
            "checks",
            "budget",
        ):
            self.assertNotIn(field, finish["properties"])

    def test_every_variant_is_closed_and_has_no_unused_common_fields(self) -> None:
        expected = {
            "list_files": ["kind", "path", "limit"],
            "read_text": ["kind", "path", "start_line", "end_line"],
            "search_text": ["kind", "query", "limit"],
            "replace_lines": [
                "kind",
                "path",
                "expected_sha256",
                "start_line",
                "end_line",
                "new_text",
            ],
            "write_text": ["kind", "path", "expected_sha256", "content"],
            "replace_text": [
                "kind",
                "path",
                "expected_sha256",
                "old_text",
                "new_text",
            ],
            "run_checks": ["kind", "checks", "budget"],
            "finish": ["kind", "summary"],
        }
        for kind, fields in expected.items():
            with self.subTest(kind=kind):
                variant = self.variants[kind]
                self.assertFalse(variant["additionalProperties"])
                self.assertEqual(list(variant["properties"]), fields)
                self.assertEqual(variant["required"], fields)

    def test_run_checks_budget_is_closed_and_bounded(self) -> None:
        budget = self.variants["run_checks"]["properties"]["budget"]
        self.assertFalse(budget["additionalProperties"])
        self.assertEqual(
            list(budget["properties"]),
            ["timeout_seconds", "max_output_chars"],
        )
        self.assertEqual(
            budget["properties"]["timeout_seconds"]["maximum"],
            MAX_CHECK_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            budget["properties"]["max_output_chars"]["maximum"],
            MAX_CHECK_OUTPUT_CHARS,
        )

    def test_schema_copy_cannot_mutate_exported_contract(self) -> None:
        self.schema["properties"].pop("rationale")
        self.assertEqual(
            list(ACTION_ENVELOPE_SCHEMA["properties"]),
            ["rationale", "action"],
        )


class ValidActionParsingTests(unittest.TestCase):
    def test_decoded_payload_can_be_validated_without_execution(self) -> None:
        envelope = validate_action_envelope(
            {
                "rationale": "The task contract has been satisfied.",
                "action": {"kind": "finish", "summary": "Verified complete."},
            }
        )
        self.assertIsInstance(envelope.action, FinishAction)

    def test_action_kind_cannot_be_overridden_during_construction(self) -> None:
        with self.assertRaises(TypeError):
            FinishAction(summary="done", kind="write_text")  # type: ignore[call-arg]

    def test_list_files(self) -> None:
        envelope = parse_action_envelope(
            document({"kind": "list_files", "path": ".", "limit": 200})
        )
        self.assertIsInstance(envelope.action, ListFilesAction)
        self.assertEqual(envelope.action.path, ".")
        self.assertEqual(envelope.action.limit, 200)

    def test_read_text(self) -> None:
        envelope = parse_action_envelope(
            document(
                {
                    "kind": "read_text",
                    "path": "src/forge8/actions.py",
                    "start_line": 20,
                    "end_line": 80,
                }
            )
        )
        self.assertIsInstance(envelope.action, ReadTextAction)
        self.assertEqual(envelope.action.end_line, 80)

    def test_search_text(self) -> None:
        envelope = parse_action_envelope(
            document({"kind": "search_text", "query": "ActionEnvelope", "limit": 25})
        )
        self.assertIsInstance(envelope.action, SearchTextAction)
        self.assertEqual(envelope.action.query, "ActionEnvelope")

    def test_write_text_accepts_missing_and_normalizes_hex_case(self) -> None:
        create = parse_action_envelope(
            document(
                {
                    "kind": "write_text",
                    "path": "src/new_file.py",
                    "expected_sha256": "missing",
                    "content": "value = 1\n",
                }
            )
        )
        update = parse_action_envelope(
            document(
                {
                    "kind": "write_text",
                    "path": "src/existing.py",
                    "expected_sha256": "AB" * 32,
                    "content": "",
                }
            )
        )
        self.assertIsInstance(create.action, WriteTextAction)
        self.assertEqual(create.action.expected_sha256, "missing")
        self.assertEqual(update.action.expected_sha256, "ab" * 32)
        self.assertEqual(update.action.content, "")

    def test_replace_text_is_a_typed_proposal_with_effect_last(self) -> None:
        envelope = parse_action_envelope(
            document(
                {
                    "kind": "replace_text",
                    "path": "src/existing.py",
                    "expected_sha256": "AB" * 32,
                    "old_text": "shutdown = 72\n",
                    "new_text": "shutdown = 30\n",
                }
            )
        )
        self.assertIsInstance(envelope.action, ReplaceTextAction)
        self.assertEqual(envelope.action.expected_sha256, "ab" * 32)
        self.assertEqual(envelope.action.old_text, "shutdown = 72\n")
        self.assertEqual(envelope.action.new_text, "shutdown = 30\n")
        self.assertEqual(
            list(envelope.as_dict()["action"]),
            ["kind", "path", "expected_sha256", "old_text", "new_text"],
        )
        rendered = envelope.as_json()
        self.assertLess(rendered.index('"old_text"'), rendered.index('"new_text"'))

    def test_replace_lines_is_a_typed_proposal_with_effect_last(self) -> None:
        envelope = parse_action_envelope(
            document(
                {
                    "kind": "replace_lines",
                    "path": "src/existing.py",
                    "expected_sha256": "AB" * 32,
                    "start_line": 20,
                    "end_line": 24,
                    "new_text": "def repaired():\n    return True\n",
                }
            )
        )
        self.assertIsInstance(envelope.action, ReplaceLinesAction)
        self.assertEqual(envelope.action.expected_sha256, "ab" * 32)
        self.assertEqual((envelope.action.start_line, envelope.action.end_line), (20, 24))
        self.assertEqual(envelope.action.new_text, "def repaired():\n    return True\n")
        self.assertEqual(
            list(envelope.as_dict()["action"]),
            [
                "kind",
                "path",
                "expected_sha256",
                "start_line",
                "end_line",
                "new_text",
            ],
        )
        rendered = envelope.as_json()
        self.assertLess(rendered.index('"end_line"'), rendered.index('"new_text"'))

    def test_run_checks_uses_named_checks_and_explicit_budget(self) -> None:
        envelope = parse_action_envelope(
            document(
                {
                    "kind": "run_checks",
                    "checks": ["python_unittest", "capsule_verify"],
                    "budget": {"timeout_seconds": 120, "max_output_chars": 16_000},
                }
            )
        )
        self.assertIsInstance(envelope.action, RunChecksAction)
        self.assertEqual(envelope.action.checks, ("python_unittest", "capsule_verify"))
        self.assertEqual(envelope.action.budget.timeout_seconds, 120)

    def test_finish_carries_only_summary(self) -> None:
        envelope = parse_action_envelope(
            document({"kind": "finish", "summary": "All declared checks passed."})
        )
        self.assertIsInstance(envelope.action, FinishAction)
        self.assertEqual(envelope.action.summary, "All declared checks passed.")

    def test_serialization_preserves_deliberation_and_effect_order(self) -> None:
        envelope = parse_action_envelope(
            document(
                {
                    "kind": "write_text",
                    "path": "config/runtime.ini",
                    "expected_sha256": "0" * 64,
                    "content": "shutdown = 30\n",
                },
                rationale="The observed upper bound is 30 seconds.",
            )
        )
        payload = envelope.as_dict()
        self.assertEqual(list(payload), ["rationale", "action"])
        self.assertEqual(
            list(payload["action"]),
            ["kind", "path", "expected_sha256", "content"],
        )
        rendered = envelope.as_json()
        self.assertLess(rendered.index('"rationale"'), rendered.index('"action"'))
        self.assertLess(rendered.index('"expected_sha256"'), rendered.index('"content"'))

    def test_json_whitespace_is_allowed_but_no_non_json_text(self) -> None:
        raw = document({"kind": "finish", "summary": "done"})
        envelope = parse_action_envelope(f"\n  {raw}\t")
        self.assertEqual(envelope.action.kind, "finish")


class LlamaCppWireActionTests(unittest.TestCase):
    def parse(self, action: dict[str, object], rationale: str = "Use bounded evidence."):
        return parse_llama_cpp_action_envelope(document(action, rationale))

    def assert_invalid(self, action: dict[str, object]) -> None:
        with self.assertRaises(ActionValidationError):
            self.parse(action)

    def test_wire_rationale_limit_is_stricter_than_authoritative_api(self) -> None:
        action = {"kind": "finish", "summary": "done"}
        boundary = "r" * MAX_LLAMA_CPP_RATIONALE_CHARS
        wire = self.parse(action, rationale=boundary)
        self.assertEqual(wire.rationale, boundary)

        wire_too_long = boundary + "r"
        with self.assertRaises(ActionValidationError):
            self.parse(action, rationale=wire_too_long)

        authoritative = parse_action_envelope(
            document(action, rationale=wire_too_long)
        )
        self.assertEqual(len(authoritative.rationale), MAX_LLAMA_CPP_RATIONALE_CHARS + 1)
        self.assertLess(MAX_LLAMA_CPP_RATIONALE_CHARS, MAX_RATIONALE_CHARS)

    def test_read_line_count_compiles_to_authoritative_inclusive_end_line(self) -> None:
        one_line = self.parse(
            {
                "kind": "read_text",
                "path": "src/forge8/actions.py",
                "start_line": 1,
                "line_count": 1,
            }
        )
        window = self.parse(
            {
                "kind": "read_text",
                "path": "src/forge8/actions.py",
                "start_line": 20,
                "line_count": MAX_READ_LINES,
            }
        )

        self.assertIsInstance(one_line.action, ReadTextAction)
        self.assertEqual((one_line.action.start_line, one_line.action.end_line), (1, 1))
        self.assertEqual(window.action.end_line, 20 + MAX_READ_LINES - 1)
        self.assertEqual(
            list(window.as_dict()["action"]),
            ["kind", "path", "start_line", "end_line"],
        )
        self.assertNotIn("line_count", window.as_dict()["action"])

    def test_read_wire_fields_are_exact_and_authoritative_shape_is_unchanged(self) -> None:
        self.assert_invalid(
            {
                "kind": "read_text",
                "path": "file.txt",
                "start_line": 1,
                "end_line": 10,
            }
        )
        self.assert_invalid(
            {
                "kind": "read_text",
                "path": "file.txt",
                "start_line": 1,
                "line_count": 10,
                "end_line": 10,
            }
        )
        self.assert_invalid(
            {"kind": "read_text", "path": "file.txt", "start_line": 1}
        )

        authoritative = parse_action_envelope(
            document(
                {
                    "kind": "read_text",
                    "path": "file.txt",
                    "start_line": 1,
                    "end_line": 10,
                }
            )
        )
        self.assertEqual(authoritative.action.end_line, 10)
        with self.assertRaises(ActionValidationError):
            parse_action_envelope(
                document(
                    {
                        "kind": "read_text",
                        "path": "file.txt",
                        "start_line": 1,
                        "line_count": 10,
                    }
                )
            )

    def test_read_wire_integer_bounds_and_compiled_overflow_are_rejected(self) -> None:
        invalid_pairs = [
            (0, 1),
            (MAX_LINE_NUMBER + 1, 1),
            (1, 0),
            (1, MAX_READ_LINES + 1),
            (True, 1),
            (1, False),
            (MAX_LINE_NUMBER, 2),
        ]
        for start_line, line_count in invalid_pairs:
            with self.subTest(start_line=start_line, line_count=line_count):
                self.assert_invalid(
                    {
                        "kind": "read_text",
                        "path": "file.txt",
                        "start_line": start_line,
                        "line_count": line_count,
                    }
                )

        edge = self.parse(
            {
                "kind": "read_text",
                "path": "file.txt",
                "start_line": MAX_LINE_NUMBER,
                "line_count": 1,
            }
        )
        self.assertEqual(edge.action.end_line, MAX_LINE_NUMBER)

    def test_replace_lines_wire_uses_authoritative_range_without_conversion(self) -> None:
        replacement = {
            "kind": "replace_lines",
            "path": "eventspool/follower.py",
            "expected_sha256": "0" * 64,
            "start_line": 40,
            "end_line": 43,
            "new_text": "first = b'\\n'\nsecond = b'\\r\\n'\n",
        }
        envelope = self.parse(replacement)

        self.assertIsInstance(envelope.action, ReplaceLinesAction)
        self.assertEqual(envelope.as_dict()["action"], replacement)
        self.assertEqual(envelope.action.new_text, replacement["new_text"])
        self.assert_invalid(
            {
                "kind": "replace_lines",
                "path": "eventspool/follower.py",
                "expected_sha256": "0" * 64,
                "start_line": 40,
                "line_count": 4,
                "new_text": "effect",
            }
        )
        oversized_window = dict(replacement)
        oversized_window["start_line"] = 1
        oversized_window["end_line"] = MAX_READ_LINES + 1
        self.assert_invalid(oversized_window)

    def test_non_read_actions_use_authoritative_validation_unchanged(self) -> None:
        actions = [
            {"kind": "list_files", "path": ".", "limit": 3},
            {"kind": "search_text", "query": "needle", "limit": 2},
            {
                "kind": "replace_lines",
                "path": "config/runtime.ini",
                "expected_sha256": "0" * 64,
                "start_line": 2,
                "end_line": 3,
                "new_text": "shutdown = 30\n",
            },
            {
                "kind": "replace_text",
                "path": "config/runtime.ini",
                "expected_sha256": "0" * 64,
                "old_text": "shutdown = 72\n",
                "new_text": "shutdown = 30\n",
            },
            {
                "kind": "run_checks",
                "checks": ["unit"],
                "budget": {"timeout_seconds": 10, "max_output_chars": 1000},
            },
            {"kind": "finish", "summary": "Verified."},
        ]
        for action in actions:
            with self.subTest(kind=action["kind"]):
                self.assertEqual(self.parse(action).action.kind, action["kind"])

        write = self.parse(
            {
                "kind": "write_text",
                "path": "config/runtime.ini",
                "expected_sha256": "missing",
                "content": "shutdown = 72\n",
            },
            rationale="The correct shutdown value is 30.",
        )
        self.assertEqual(write.action.content, "shutdown = 72\n")

        self.assert_invalid(
            {
                "kind": "write_text",
                "path": "config/runtime.ini",
                "expected_sha256": "not-a-guard",
                "content": "shutdown = 30\n",
            }
        )
        self.assert_invalid(
            {"kind": "finish", "summary": "done", "content": "effect"}
        )

    def test_wire_parser_uses_the_same_strict_json_decoder(self) -> None:
        valid = document({"kind": "finish", "summary": "done"})
        rejected = [
            f"```json\n{valid}\n```",
            valid + "\nDone",
            '{"rationale":"one","rationale":"two",'
            '"action":{"kind":"finish","summary":"done"}}',
            '{"rationale":"ok","action":{"kind":"read_text",'
            '"path":"a.txt","start_line":1,"line_count":NaN}}',
        ]
        for raw in rejected:
            with self.subTest(raw=raw[:30]), self.assertRaises(ActionJSONError):
                parse_llama_cpp_action_envelope(raw)

    def test_wire_parser_strips_only_one_known_llama_control_prefix(self) -> None:
        valid = document({"kind": "finish", "summary": "done"})
        marker = "<|channel>thought<channel|>"

        parsed = parse_llama_cpp_action_envelope("  " + marker + valid + "\n")
        self.assertEqual(parsed.action.kind, "finish")

        rejected = (
            marker + marker + valid,
            "model prose" + marker + valid,
            "<|channel>final<channel|>" + valid,
            marker + valid + " trailing prose",
        )
        for raw in rejected:
            with self.subTest(raw=raw[:40]), self.assertRaises(ActionJSONError):
                parse_llama_cpp_action_envelope(raw)
        with self.assertRaises(ActionJSONError):
            parse_action_envelope(marker + valid)


class JSONStrictnessTests(unittest.TestCase):
    def assert_json_rejected(self, raw: object) -> None:
        with self.assertRaises(ActionJSONError):
            parse_action_envelope(raw)  # type: ignore[arg-type]

    def test_markdown_and_trailing_prose_are_rejected(self) -> None:
        raw = document({"kind": "finish", "summary": "done"})
        self.assert_json_rejected(f"```json\n{raw}\n```")
        self.assert_json_rejected(raw + "\nDone")

    def test_non_string_and_empty_output_are_json_errors(self) -> None:
        self.assert_json_rejected(None)
        self.assert_json_rejected("")

    def test_valid_json_that_is_not_an_envelope_is_a_validation_error(self) -> None:
        for raw in ("null", "[]"):
            with self.subTest(raw=raw), self.assertRaises(ActionValidationError):
                parse_action_envelope(raw)

    def test_duplicate_keys_are_rejected_at_any_depth(self) -> None:
        self.assert_json_rejected(
            '{"rationale":"first","rationale":"second",'
            '"action":{"kind":"finish","summary":"done"}}'
        )
        self.assert_json_rejected(
            '{"rationale":"ok","action":{"kind":"finish",'
            '"summary":"one","summary":"two"}}'
        )

    def test_non_finite_json_numbers_are_rejected(self) -> None:
        self.assert_json_rejected(
            '{"rationale":"ok","action":{"kind":"list_files",'
            '"path":".","limit":NaN}}'
        )


class ExactFieldTests(unittest.TestCase):
    def assert_invalid(self, action: dict[str, object], rationale: str = "bounded step") -> None:
        with self.assertRaises(ActionValidationError):
            parse_action_envelope(document(action, rationale))

    def test_envelope_rejects_missing_and_unknown_fields(self) -> None:
        with self.assertRaises(ActionValidationError):
            parse_action_envelope(json.dumps({"action": {"kind": "finish", "summary": "x"}}))
        with self.assertRaises(ActionValidationError):
            parse_action_envelope(
                json.dumps(
                    {
                        "rationale": "done",
                        "action": {"kind": "finish", "summary": "x"},
                        "confidence": 1,
                    }
                )
            )

    def test_unknown_kind_and_non_object_action_are_rejected(self) -> None:
        self.assert_invalid({"kind": "delete_tree"})
        with self.assertRaises(ActionValidationError):
            parse_action_envelope(json.dumps({"rationale": "x", "action": "finish"}))

    def test_each_kind_rejects_unused_fields(self) -> None:
        examples = [
            {"kind": "list_files", "path": ".", "limit": 1, "query": "unused"},
            {
                "kind": "read_text",
                "path": "a.txt",
                "start_line": 1,
                "end_line": 1,
                "content": "unused",
            },
            {"kind": "search_text", "query": "x", "limit": 1, "path": "unused"},
            {
                "kind": "write_text",
                "path": "a.txt",
                "expected_sha256": "missing",
                "content": "x",
                "limit": 1,
            },
            {
                "kind": "replace_lines",
                "path": "a.txt",
                "expected_sha256": "0" * 64,
                "start_line": 1,
                "end_line": 1,
                "new_text": "after",
                "old_text": "unused",
            },
            {
                "kind": "replace_text",
                "path": "a.txt",
                "expected_sha256": "0" * 64,
                "old_text": "before",
                "new_text": "after",
                "content": "unused",
            },
            {
                "kind": "run_checks",
                "checks": ["unit"],
                "budget": {"timeout_seconds": 1, "max_output_chars": 1},
                "content": "unused",
            },
        ]
        for action in examples:
            with self.subTest(kind=action["kind"]):
                self.assert_invalid(action)

    def test_finish_cannot_smuggle_an_effect(self) -> None:
        effect_fields: dict[str, object] = {
            "path": "config.ini",
            "content": "shutdown = 72\n",
            "expected_sha256": "missing",
            "old_text": "before",
            "new_text": "after",
            "checks": ["unit"],
            "budget": {"timeout_seconds": 30, "max_output_chars": 1000},
        }
        for field, value in effect_fields.items():
            with self.subTest(field=field):
                self.assert_invalid(
                    {"kind": "finish", "summary": "done", field: value}
                )

    def test_kind_specific_required_fields_are_enforced(self) -> None:
        examples = [
            {"kind": "list_files", "path": "."},
            {"kind": "read_text", "path": "a", "start_line": 1},
            {"kind": "search_text", "limit": 1},
            {
                "kind": "write_text",
                "path": "a",
                "expected_sha256": "missing",
            },
            {
                "kind": "replace_lines",
                "path": "a",
                "expected_sha256": "0" * 64,
                "start_line": 1,
                "end_line": 1,
            },
            {
                "kind": "replace_text",
                "path": "a",
                "expected_sha256": "0" * 64,
                "old_text": "before",
            },
            {"kind": "run_checks", "checks": ["unit"]},
            {"kind": "finish"},
        ]
        for action in examples:
            with self.subTest(kind=action["kind"]):
                self.assert_invalid(action)


class PathAndBoundTests(unittest.TestCase):
    def assert_invalid(self, action: dict[str, object], rationale: str = "bounded step") -> None:
        with self.assertRaises(ActionValidationError):
            parse_action_envelope(document(action, rationale))

    def test_paths_must_be_normalized_workspace_relative_posix_paths(self) -> None:
        invalid_paths = [
            "",
            ".",
            "/etc/passwd",
            "//server/share",
            "../outside",
            "src/../outside",
            "src//main.py",
            "src/./main.py",
            "src/main.py/",
            "C:/Windows/system.ini",
            "C:\\Windows\\system.ini",
            "src\\main.py",
            "src/line\nbreak.py",
            "segment/" + "x" * 256,
            "bad\x00name",
        ]
        for path in invalid_paths:
            with self.subTest(path=repr(path)):
                self.assert_invalid(
                    {
                        "kind": "read_text",
                        "path": path,
                        "start_line": 1,
                        "end_line": 1,
                    }
                )

    def test_list_files_alone_may_address_workspace_root(self) -> None:
        root = parse_action_envelope(
            document({"kind": "list_files", "path": ".", "limit": 1})
        )
        self.assertEqual(root.action.path, ".")
        self.assert_invalid(
            {
                "kind": "write_text",
                "path": ".",
                "expected_sha256": "missing",
                "content": "x",
            }
        )

    def test_list_files_compiles_one_terminal_directory_marker(self) -> None:
        bare = parse_action_envelope(
            document({"kind": "list_files", "path": "src/forge8", "limit": 10})
        )
        marked = parse_action_envelope(
            document({"kind": "list_files", "path": "src/forge8/", "limit": 10})
        )
        wire = parse_llama_cpp_action_envelope(
            document({"kind": "list_files", "path": "src/forge8/", "limit": 10})
        )

        self.assertEqual(marked.action, bare.action)
        self.assertEqual(wire.action, bare.action)
        self.assertEqual(marked.action.path, "src/forge8")

    def test_list_directory_marker_does_not_relax_path_boundaries(self) -> None:
        invalid_paths = (
            "/",
            "//",
            "./",
            "../",
            "src//",
            "src/./",
            "src/../",
            "C:/",
            "C:\\Windows\\",
            "src\\forge8\\",
            "src/line\nbreak/",
        )
        for path in invalid_paths:
            with self.subTest(path=repr(path)):
                self.assert_invalid(
                    {"kind": "list_files", "path": path, "limit": 10}
                )

        self.assert_invalid(
            {
                "kind": "write_text",
                "path": "src/forge8/",
                "expected_sha256": "missing",
                "content": "x",
            }
        )

    def test_list_and_search_limits_reject_bools_and_out_of_range_values(self) -> None:
        for value in (True, 0, MAX_LIST_FILES + 1):
            with self.subTest(list_limit=value):
                self.assert_invalid({"kind": "list_files", "path": ".", "limit": value})
        for value in (False, 0, MAX_SEARCH_RESULTS + 1):
            with self.subTest(search_limit=value):
                self.assert_invalid({"kind": "search_text", "query": "x", "limit": value})

    def test_read_line_numbers_and_window_are_bounded(self) -> None:
        invalid_ranges = [
            (0, 1),
            (2, 1),
            (1, MAX_LINE_NUMBER + 1),
            (1, MAX_READ_LINES + 1),
            (True, 2),
        ]
        for start, end in invalid_ranges:
            with self.subTest(start=start, end=end):
                self.assert_invalid(
                    {
                        "kind": "read_text",
                        "path": "file.txt",
                        "start_line": start,
                        "end_line": end,
                    }
                )
        accepted = parse_action_envelope(
            document(
                {
                    "kind": "read_text",
                    "path": "file.txt",
                    "start_line": 9,
                    "end_line": 9 + MAX_READ_LINES - 1,
                }
            )
        )
        self.assertEqual(accepted.action.end_line, 9 + MAX_READ_LINES - 1)

    def test_query_is_non_blank_nul_free_and_bounded(self) -> None:
        for query in ("", "   \n", "bad\x00query", "q" * (MAX_QUERY_CHARS + 1)):
            with self.subTest(length=len(query)):
                self.assert_invalid({"kind": "search_text", "query": query, "limit": 1})
        accepted = parse_action_envelope(
            document({"kind": "search_text", "query": "q" * MAX_QUERY_CHARS, "limit": 1})
        )
        self.assertEqual(len(accepted.action.query), MAX_QUERY_CHARS)

    def test_write_guard_is_exactly_missing_or_64_hex(self) -> None:
        invalid_guards = ["", "missing ", "0" * 63, "0" * 65, "g" * 64, 123]
        for guard in invalid_guards:
            with self.subTest(guard=guard):
                self.assert_invalid(
                    {
                        "kind": "write_text",
                        "path": "file.txt",
                        "expected_sha256": guard,
                        "content": "x",
                    }
                )

    def test_write_content_uses_utf8_byte_budget_and_rejects_nul(self) -> None:
        exact = "é" * (MAX_WRITE_BYTES // 2)
        accepted = parse_action_envelope(
            document(
                {
                    "kind": "write_text",
                    "path": "file.txt",
                    "expected_sha256": "missing",
                    "content": exact,
                }
            )
        )
        self.assertEqual(len(accepted.action.content.encode("utf-8")), MAX_WRITE_BYTES)
        self.assert_invalid(
            {
                "kind": "write_text",
                "path": "file.txt",
                "expected_sha256": "missing",
                "content": exact + "é",
            }
        )
        self.assert_invalid(
            {
                "kind": "write_text",
                "path": "file.txt",
                "expected_sha256": "missing",
                "content": "before\x00after",
            }
        )

    def test_replace_requires_existing_hash_and_bounded_utf8_text(self) -> None:
        base: dict[str, object] = {
            "kind": "replace_text",
            "path": "file.txt",
            "expected_sha256": "A0" * 32,
            "old_text": "before",
            "new_text": "after",
        }
        accepted = parse_action_envelope(document(base))
        self.assertIsInstance(accepted.action, ReplaceTextAction)
        self.assertEqual(accepted.action.expected_sha256, "a0" * 32)

        for guard in ("missing", "0" * 63, "0" * 65, "g" * 64, 123):
            with self.subTest(guard=guard):
                action = dict(base)
                action["expected_sha256"] = guard
                self.assert_invalid(action)

        invalid_text = [
            ("old_text", ""),
            ("old_text", "before\x00after"),
            ("new_text", "before\x00after"),
        ]
        for field, value in invalid_text:
            with self.subTest(field=field, value=repr(value)):
                action = dict(base)
                action[field] = value
                self.assert_invalid(action)

        deletion = dict(base)
        deletion["old_text"] = " "
        deletion["new_text"] = ""
        self.assertEqual(
            parse_action_envelope(document(deletion)).action.new_text,
            "",
        )

        exact = "é" * (MAX_WRITE_BYTES // 2)
        for field in ("old_text", "new_text"):
            with self.subTest(field=field, boundary="accepted"):
                action = dict(base)
                action[field] = exact
                parsed = parse_action_envelope(document(action))
                self.assertEqual(
                    len(getattr(parsed.action, field).encode("utf-8")),
                    MAX_WRITE_BYTES,
                )
            with self.subTest(field=field, boundary="rejected"):
                action = dict(base)
                action[field] = exact + "é"
                self.assert_invalid(action)

    def test_replace_lines_requires_existing_hash_bounded_range_and_utf8_effect(self) -> None:
        base: dict[str, object] = {
            "kind": "replace_lines",
            "path": "file.txt",
            "expected_sha256": "A0" * 32,
            "start_line": 9,
            "end_line": 9 + MAX_READ_LINES - 1,
            "new_text": "replacement\n",
        }
        accepted = parse_action_envelope(document(base))
        self.assertIsInstance(accepted.action, ReplaceLinesAction)
        self.assertEqual(accepted.action.expected_sha256, "a0" * 32)
        self.assertEqual(accepted.action.end_line, 9 + MAX_READ_LINES - 1)

        for guard in ("missing", "0" * 63, "0" * 65, "g" * 64, 123):
            with self.subTest(guard=guard):
                action = dict(base)
                action["expected_sha256"] = guard
                self.assert_invalid(action)

        invalid_ranges = [
            (0, 1),
            (2, 1),
            (1, MAX_READ_LINES + 1),
            (1, MAX_LINE_NUMBER + 1),
            (True, 2),
            (1, False),
        ]
        for start_line, end_line in invalid_ranges:
            with self.subTest(start_line=start_line, end_line=end_line):
                action = dict(base)
                action["start_line"] = start_line
                action["end_line"] = end_line
                self.assert_invalid(action)

        deletion = dict(base)
        deletion["start_line"] = 1
        deletion["end_line"] = 1
        deletion["new_text"] = ""
        self.assertEqual(
            parse_action_envelope(document(deletion)).action.new_text,
            "",
        )

        exact = "é" * (MAX_WRITE_BYTES // 2)
        boundary = dict(deletion)
        boundary["new_text"] = exact
        parsed = parse_action_envelope(document(boundary))
        self.assertEqual(len(parsed.action.new_text.encode("utf-8")), MAX_WRITE_BYTES)

        oversized = dict(boundary)
        oversized["new_text"] = exact + "é"
        self.assert_invalid(oversized)

        nul = dict(deletion)
        nul["new_text"] = "before\x00after"
        self.assert_invalid(nul)

    def test_rationale_and_finish_summary_are_non_blank_and_bounded(self) -> None:
        self.assert_invalid({"kind": "finish", "summary": "done"}, rationale=" ")
        self.assert_invalid(
            {"kind": "finish", "summary": "done"},
            rationale="r" * (MAX_RATIONALE_CHARS + 1),
        )
        for summary in ("", "  \n", "s" * (MAX_FINISH_SUMMARY_CHARS + 1)):
            with self.subTest(summary_length=len(summary)):
                self.assert_invalid({"kind": "finish", "summary": summary})

    def test_run_checks_identifiers_and_budget_are_strict(self) -> None:
        base: dict[str, object] = {
            "kind": "run_checks",
            "checks": ["unit"],
            "budget": {"timeout_seconds": 60, "max_output_chars": 1000},
        }
        invalid_checks = [
            [],
            ["unit"] * (MAX_CHECKS + 1),
            ["unit", "unit"],
            ["Unit"],
            ["unit test"],
            ["unit;rm"],
            ["unit-test"],
            ["unit.test"],
            [1],
        ]
        for checks in invalid_checks:
            with self.subTest(checks=checks):
                action = dict(base)
                action["checks"] = checks
                self.assert_invalid(action)

        invalid_budgets = [
            {"timeout_seconds": 0, "max_output_chars": 1000},
            {
                "timeout_seconds": MAX_CHECK_TIMEOUT_SECONDS + 1,
                "max_output_chars": 1000,
            },
            {"timeout_seconds": True, "max_output_chars": 1000},
            {"timeout_seconds": 60, "max_output_chars": 0},
            {
                "timeout_seconds": 60,
                "max_output_chars": MAX_CHECK_OUTPUT_CHARS + 1,
            },
            {"timeout_seconds": 60, "max_output_chars": 1000, "processes": 4},
        ]
        for budget in invalid_budgets:
            with self.subTest(budget=budget):
                action = dict(base)
                action["budget"] = budget
                self.assert_invalid(action)


if __name__ == "__main__":
    unittest.main()
