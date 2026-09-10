from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from forge8 import explain
from forge8.inference import ChatRequest, ChatResponse, Message
from forge8.operator import AcceptanceGateResult
from forge8.trace import verify_trace
from test_explain import passing_gate


def citation(evidence_id="E1", start_line=3, end_line=4):
    return {"evidence_id": evidence_id, "start_line": start_line, "end_line": end_line}


def answer_document(text="It returns the input unchanged.", citations=None):
    return {"kind": "answer", "text": text,
        "citations": [citation()] if citations is None else citations}


class StructuredReadingTests(unittest.TestCase):
    """Owned inert source fixture; every inference response is supplied by a mock."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="forge8-structured-reading-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.source_bytes = b'raise RuntimeError("never execute")\n\ndef identity(value):\n    return value\n'
        (self.source / "service.py").write_bytes(self.source_bytes)
        self.sequence = 0

    def prepare(self):
        self.sequence += 1
        name = f"reading-{self.sequence}"
        return explain.prepare_explanation(self.source, self.root / name,
            "請解釋 identity().", name, focus=("service.py:3-4",))

    def run_reading(self, raw, *, finish="stop", prepared=None, gate=None, reader="qwen35", **options):
        prepared = self.prepare() if prepared is None else prepared
        backend = Mock(spec=["chat"])
        backend.chat.return_value = ChatResponse(raw, finish,
            {"prompt_tokens": 100, "completion_tokens": 25}, {"predicted_n": 25})
        acceptance = Mock(side_effect=passing_gate if gate is None else gate)
        outcome = explain.run_explanation(prepared, backend, model="scripted",
            reader=reader, acceptance_gate=acceptance,
            **({"reading_format": "structured_v1"} | options))
        backend.chat.assert_called_once()
        acceptance.assert_called_once()
        self.assertEqual(outcome.inference["calls"], 1)
        self.assertTrue(verify_trace(prepared.run_root / "trace.jsonl",
            prepared.run_root / "trace.seal.json").ok)
        return outcome, backend.chat.call_args.args[0], prepared

    def assert_no_public_answer(self, outcome):
        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.answer)
        self.assertNotIn("unverified_prose", outcome.as_dict())
        document = json.loads(Path(outcome.explanation_path).read_bytes())
        self.assertIsNone(document["answer"])
        self.assertFalse(document["answered"])
        self.assertFalse(document["semantic_claims_verified"])
        self.assertNotIn("unverified_prose", document)
        self.assertEqual(outcome.coverage["cited"]["lines"], 0)
        self.assertNotIn("UNVERIFIED MODEL OUTPUT",
            Path(outcome.answer_path).read_text(encoding="utf-8"))

    def test_exact_prose_and_separate_coordinates_bind_retained_bytes(self):
        prose = "  回傳原本的值。\r\n\n```python\nidentity([1, 2])\n```\n\t不改寫引數。  "
        values = [citation(end_line=3), citation(start_line=4)]
        raw = json.dumps(answer_document(prose, values), ensure_ascii=False)
        outcome, request, prepared = self.run_reading(raw)
        self.assertTrue(outcome.ok, outcome.failure_reason)
        self.assertEqual(outcome.answer["format"], "cited_prose")
        self.assertEqual(outcome.answer["citation_scope"], "document_references_only")
        claim = outcome.answer["claims"][0]
        self.assertEqual(claim["text"], prose)
        self.assertEqual(len(outcome.answer["claims"]), 1)
        document = json.loads(Path(outcome.explanation_path).read_bytes())
        evidence = document["evidence"][0]
        artifact = evidence["artifact"]
        artifact_bytes = (prepared.run_root / artifact["relative_path"]).read_bytes()
        self.assertEqual(hashlib.sha256(artifact_bytes).hexdigest(), artifact["sha256"])
        for selected, resolved in zip(values, claim["citations"]):
            self.assertEqual(resolved, selected | {"path": "service.py",
                "file_sha256": hashlib.sha256(self.source_bytes).hexdigest(),
                "file_size_bytes": len(self.source_bytes),
                "snapshot_inventory_sha256": prepared.snapshot_sha256,
                "artifact_sha256": artifact["sha256"]})
        self.assertEqual(outcome.coverage["cited"]["lines"], 2)
        self.assertFalse(document["semantic_claims_verified"])
        self.assertFalse(document["repository_code_executed"])
        self.assertTrue(outcome.source_unchanged)
        self.assertTrue(outcome.snapshot_unchanged)
        self.assertEqual(request.messages[0].content, explain._STRUCTURED_READING_PROMPT)
        self.assertEqual(request.response_format, explain._STRUCTURED_READING_FORMAT)
        self.assertNotIn("never execute", request.messages[1].content)
        self.assertIn(prose.encode("utf-8"), Path(outcome.answer_path).read_bytes())
        self.assertNotIn(raw.encode("utf-8"), Path(outcome.answer_path).read_bytes())
        self.assertEqual((self.source / "service.py").read_bytes(), self.source_bytes)

    def test_insufficient_is_a_single_completed_nonanswer(self):
        reason = "Missing caller input.\n請補上呼叫端。"
        raw = json.dumps({"kind": "insufficient", "reason": reason})
        _, parsed = explain._parse_structured_reading(raw)
        self.assertEqual(parsed, {"kind": "insufficient", "reason": reason})
        outcome, _, _ = self.run_reading(raw)
        self.assertEqual(outcome.status, "insufficient_evidence")
        self.assertEqual(outcome.failure_reason, reason)
        self.assertEqual(outcome.inference["parse_failures"], 0)
        self.assert_no_public_answer(outcome)

    def test_parser_rejects_duplicate_keys_and_nonfinite_or_nonobject_json(self):
        raw = json.dumps(answer_document())
        invalid = ["", " ", "[]", "null", "42", '"answer"', raw + raw,
            "```json\n" + raw + "\n```", raw + " trailing prose",
            raw.replace('"kind": "answer"', '"kind": "answer", "kind": "answer"'),
            raw.replace('"start_line": 3', '"start_line": 3, "start_line": 3'),
            "x" * 32769]
        invalid += [raw.replace('"start_line": 3', '"start_line": ' + constant)
            for constant in ("NaN", "Infinity", "-Infinity")]
        for value in invalid:
            with self.subTest(raw=repr(value[:100])):
                with self.assertRaises(explain.ExplanationError):
                    explain._parse_structured_reading(value)

    def test_parser_rejects_missing_unknown_keys_and_wrong_types(self):
        valid = answer_document()
        invalid = [valid | {"unknown": True}, valid | {"kind": "other"}]
        invalid += [{key: value for key, value in valid.items() if key != missing}
            for missing in valid]
        invalid += [valid | {key: value} for key, values in {
            "kind": [None, True, [], {}], "text": [None, True, 7, [], {}],
            "citations": [None, True, "E1:L3-L4", {}, []]}.items() for value in values]
        invalid += [answer_document(citations=[value]) for value in
            (None, True, "E1:L3-L4", [], citation() | {"path": "service.py"},
                {"evidence_id": "E1", "start_line": 3})]
        invalid += [{"kind": "insufficient"},
            {"kind": "insufficient", "reason": "missing", "text": "answer"}]
        invalid += [{"kind": "insufficient", "reason": value} for value in (None, True, [], {})]
        for value in invalid:
            with self.subTest(document=value):
                with self.assertRaises(explain.ExplanationError):
                    explain._parse_structured_reading(json.dumps(value))

    def test_text_and_insufficient_reason_remain_bounded_and_control_safe(self):
        unsafe = ["", " \r\n\t", "bad\x00text", "bad\x1btext", "bad\vtext",
            "bad\u202etext", "bad\x85text", "bad\u2028text", "bad\u2029text", "bad\ud800text"]
        for kind, maximum in (("answer", 12000), ("insufficient", 1000)):
            for value in unsafe + ["x" * (maximum + 1)]:
                document = (answer_document(value) if kind == "answer"
                    else {"kind": kind, "reason": value})
                with self.subTest(kind=kind, text=repr(value[:30])):
                    with self.assertRaises(explain.ExplanationError):
                        explain._parse_structured_reading(json.dumps(document))
            boundary = (answer_document("x" * maximum) if kind == "answer"
                else {"kind": kind, "reason": "x" * maximum})
            _, parsed = explain._parse_structured_reading(json.dumps(boundary))
            self.assertEqual(parsed["kind"], kind)

    def test_reference_count_line_range_and_schema_boundaries(self):
        for references in ([citation()] * 32, [citation(start_line=1, end_line=80)],
                [citation(start_line=1000000, end_line=1000000)]):
            _, parsed = explain._parse_structured_reading(json.dumps(answer_document(citations=references)))
            self.assertEqual(parsed["claims"][0]["citations"], references)
        invalid = [[citation()] * 33, [citation(start_line=1, end_line=81)],
            [citation(start_line=4, end_line=3)]]
        invalid += [[citation(**{field: value})] for field in ("start_line", "end_line")
            for value in (True, False, 3.0, "3", None, 0, -1, 1000001)]
        invalid += [[citation(evidence_id=value)] for value in
            (None, True, 1, "E0", "E01", "e1", "E10000", "E1 ", "E1\n")]
        for references in invalid:
            with self.subTest(references=references):
                with self.assertRaises(explain.ExplanationError):
                    explain._parse_structured_reading(json.dumps(answer_document(citations=references)))
        schema = explain._STRUCTURED_READING_FORMAT["json_schema"]
        self.assertIs(schema["strict"], True)
        variants = schema["schema"]["oneOf"]
        answer_schema = next(item for item in variants if item["properties"]["kind"]["const"] == "answer")
        references = answer_schema["properties"]["citations"]
        self.assertEqual((references["minItems"], references["maxItems"]), (1, 32))
        for field in ("start_line", "end_line"):
            self.assertEqual(references["items"]["properties"][field]["maximum"], 1000000)
        self.assertIs(answer_schema["additionalProperties"], False)
        self.assertIs(references["items"]["additionalProperties"], False)

    def test_unknown_or_outside_coordinates_never_become_an_answer(self):
        for reader, selected in ((reader, selected) for reader in ("qwen35", "gemma12b")
                for selected in (citation(evidence_id="E2"), citation(start_line=2), citation(end_line=5))):
            with self.subTest(reader=reader, citation=selected):
                # Valid prose references do not rescue invalid separate coordinates.
                raw = json.dumps(answer_document("Correct prose reference [E1:L3-L4].", [selected]))
                outcome, _, _ = self.run_reading(raw, reader=reader)
                self.assertEqual(outcome.status, "stalled")
                self.assertEqual(outcome.inference["parse_failures"], 1)
                self.assert_no_public_answer(outcome)

    def test_malformed_and_incomplete_responses_are_not_retried_or_shown_as_json(self):
        raw = json.dumps(answer_document("DO NOT DISPLAY PARTIAL MODEL TEXT"))
        cases = [(raw, finish) for finish in ("length", None, "content_filter")]
        cases += [(value, "stop") for value in (raw[:-1], raw + " trailing", "[E1:L3-L4] plain prose",
            json.dumps(answer_document("DO NOT DISPLAY PARTIAL MODEL TEXT", [])),
            raw.replace('"start_line": 3', '"start_line": ' + "9" * 5000))]
        for value, finish in cases:
            with self.subTest(finish=finish, raw=value[:80]):
                outcome, _, _ = self.run_reading(value, finish=finish)
                self.assertEqual(outcome.status, "stalled")
                self.assert_no_public_answer(outcome)
                self.assertNotIn("DO NOT DISPLAY PARTIAL MODEL TEXT",
                    Path(outcome.answer_path).read_text(encoding="utf-8"))
                self.assertNotIn(value, Path(outcome.answer_path).read_text(encoding="utf-8"))

    def test_default_prose_wire_and_sampling_remain_unchanged(self):
        prose = "It returns its input. [E1:L3-L4]"
        prepared = self.prepare()
        backend = Mock(spec=["chat"])
        backend.chat.return_value = ChatResponse(prose, "stop", {}, {})
        outcome = explain.run_explanation(prepared, backend, model="scripted",
            reader="qwen35", acceptance_gate=passing_gate)
        backend.chat.assert_called_once()
        request = backend.chat.call_args.args[0]
        self.assertTrue(outcome.ok, outcome.failure_reason)
        self.assertEqual(outcome.answer["claims"][0]["text"], prose)
        expected = {"model": "scripted", "messages": [
            {"role": "system", "content": explain._READING_PROMPT},
            {"role": "user", "content": request.messages[1].content}],
            "temperature": 0.6, "max_tokens": 4096, "stream": False, "seed": 1,
            "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0, "repeat_penalty": 1.0}
        self.assertEqual(request.as_dict(), expected)
        self.assertTrue(request.messages[1].content.endswith("\n\nUSER QUESTION:\n" + prepared.question))
        self.assertNotIn("reading_recipe", outcome.inference)
        events = [json.loads(line) for line in (prepared.run_root / "trace.jsonl").read_bytes().splitlines()]
        started = next(event["payload"] for event in events if event["kind"] == "explain.started")
        self.assertNotIn("reading_recipe", started)
        explicit, explicit_request, _ = self.run_reading(prose, reading_format="prose")
        self.assertTrue(explicit.ok)
        self.assertEqual(explicit_request.as_dict(), expected)

    def test_explicit_recipe_is_bound_to_request_trace_and_inference(self):
        for reading_format, budget in (("structured_v1", None), ("structured_v1", 0),
                ("structured_v1", 512), ("prose", 0), ("prose", 4096)):
            with self.subTest(format=reading_format, budget=budget):
                raw = (json.dumps(answer_document()) if reading_format == "structured_v1"
                    else "It returns its input. [E1:L3-L4]")
                outcome, request, prepared = self.run_reading(raw, reading_format=reading_format,
                    reasoning_budget_tokens=budget)
                self.assertTrue(outcome.ok, outcome.failure_reason)
                self.assertEqual(request.reasoning_budget_tokens, budget)
                self.assertEqual("reasoning_budget_tokens" in request.as_dict(), budget is not None)
                recipe = {"format": reading_format, "reasoning_budget_tokens": budget,
                    "reasoning_budget_scope": "per_thinking_block"}
                self.assertEqual(outcome.inference["reading_recipe"], recipe)
                document = json.loads(Path(outcome.explanation_path).read_bytes())
                self.assertEqual(document["inference"]["reading_recipe"], recipe)
                events = [json.loads(line) for line in (prepared.run_root / "trace.jsonl").read_bytes().splitlines()]
                for kind in ("explain.started", "explain.finished"):
                    payload = next(event["payload"] for event in events if event["kind"] == kind)
                    self.assertEqual((payload if kind == "explain.started" else payload["inference"])["reading_recipe"], recipe)

    def test_invalid_experimental_options_fail_before_inference_or_trace(self):
        prepared = self.prepare()
        before = {path.relative_to(prepared.run_root).as_posix():
            path.read_bytes() if path.is_file() else None for path in prepared.run_root.rglob("*")}
        invalid = [{"reading_format": value} for value in (None, True, 1, [], "json", "structured_v2")]
        invalid += [{"reasoning_budget_tokens": value} for value in
            (True, False, -1, 4097, 512.0, "512", float("nan"), float("inf"), [], {})]
        invalid += [{"enable_thinking": value} for value in
            (0, 1, 0.0, 1.0, "false", "true", "", [], {}, float("nan"))]
        invalid = [{"reader": reader, **options} for reader in ("qwen35", "gemma12b")
            for options in invalid]
        invalid += [{"reader": "gemma4", **options}
            for options in ({"reading_format": "structured_v1"}, {"reasoning_budget_tokens": 0},
                {"reading_seed": 2},
                {"enable_thinking": False}, {"enable_thinking": True})]
        invalid += [{"reader": "gemma12b", "enable_thinking": enabled, **options}
            for enabled in (False, True) for options in ({},
                {"reading_format": "structured_v1", "reasoning_budget_tokens": 2048, "reading_seed": 2})]
        for options in invalid:
            with self.subTest(options=options):
                backend, gate = Mock(spec=["chat"]), Mock()
                with self.assertRaises(explain.ExplanationError):
                    explain.run_explanation(prepared, backend, model="scripted", acceptance_gate=gate,
                        **({"reader": "qwen35"} | options))
                backend.chat.assert_not_called()
                gate.assert_not_called()
                self.assertEqual({path.relative_to(prepared.run_root).as_posix():
                    path.read_bytes() if path.is_file() else None
                    for path in prepared.run_root.rglob("*")}, before)
                self.assertFalse((prepared.run_root / "artifacts").exists())
                self.assertFalse((prepared.run_root / "trace.jsonl").exists())

    def test_thinking_toggle_only_changes_template_flag_and_records_explicit_recipe(self):
        for reading_format, budget in (("prose", None), ("structured_v1", 256)):
            raw = (json.dumps(answer_document()) if reading_format == "structured_v1"
                else "It returns its input. [E1:L3-L4]")
            baseline, baseline_request, _ = self.run_reading(raw, reading_format=reading_format,
                reasoning_budget_tokens=budget, enable_thinking=None)
            self.assertTrue(baseline.ok, baseline.failure_reason)
            baseline_wire = baseline_request.as_dict()
            self.assertNotIn("chat_template_kwargs", baseline_wire)
            self.assertNotIn("enable_thinking", baseline.inference.get("reading_recipe", {}))
            for enabled in (False, True):
                with self.subTest(format=reading_format, thinking=enabled):
                    outcome, request, prepared = self.run_reading(raw, reading_format=reading_format,
                        reasoning_budget_tokens=budget, enable_thinking=enabled)
                    self.assertTrue(outcome.ok, outcome.failure_reason)
                    self.assertIs(request.enable_thinking, enabled)
                    self.assertEqual(request.as_dict(), baseline_wire | {
                        "chat_template_kwargs": {"enable_thinking": enabled}})
                    self.assertEqual(outcome.answer["claims"][0]["text"],
                        baseline.answer["claims"][0]["text"])
                    self.assertTrue(outcome.source_unchanged)
                    self.assertTrue(outcome.snapshot_unchanged)
                    self.assertEqual((self.source / "service.py").read_bytes(), self.source_bytes)
                    document = json.loads(Path(outcome.explanation_path).read_bytes())
                    artifact = document["evidence"][0]["artifact"]
                    resolved = outcome.answer["claims"][0]["citations"][0]
                    self.assertEqual(resolved, citation() | {"path": "service.py",
                        "file_sha256": hashlib.sha256(self.source_bytes).hexdigest(),
                        "file_size_bytes": len(self.source_bytes),
                        "snapshot_inventory_sha256": prepared.snapshot_sha256,
                        "artifact_sha256": artifact["sha256"]})
                    self.assertEqual(hashlib.sha256(
                        (prepared.run_root / artifact["relative_path"]).read_bytes()).hexdigest(),
                        artifact["sha256"])
                    self.assertFalse(document["semantic_claims_verified"])
                    self.assertFalse(document["repository_code_executed"])
                    recipe = {"format": reading_format, "reasoning_budget_tokens": budget,
                        "reasoning_budget_scope": "per_thinking_block", "enable_thinking": enabled}
                    self.assertEqual(outcome.inference["reading_recipe"], recipe)
                    self.assertEqual(document["inference"]["reading_recipe"], recipe)
                    events = [json.loads(line) for line in
                        (prepared.run_root / "trace.jsonl").read_bytes().splitlines()]
                    for kind in ("explain.started", "explain.finished"):
                        payload = next(event["payload"] for event in events if event["kind"] == kind)
                        self.assertEqual((payload if kind == "explain.started"
                            else payload["inference"])["reading_recipe"], recipe)

    def test_thinking_toggle_never_bypasses_citation_source_or_cleanup_gates(self):
        for enabled in (False, True):
            for fault in ("citation", "source", "cleanup"):
                with self.subTest(thinking=enabled, fault=fault):
                    (self.source / "service.py").write_bytes(self.source_bytes)
                    prepared = self.prepare()
                    references = [citation(evidence_id="E2" if fault == "citation" else "E1")]

                    def gate():
                        if fault == "source":
                            (self.source / "service.py").write_bytes(b"changed\n")
                        elif fault == "cleanup":
                            return AcceptanceGateResult(False, "synthetic cleanup failure", passing_gate().evidence)
                        return passing_gate()

                    outcome, request, _ = self.run_reading(json.dumps(answer_document(citations=references)),
                        prepared=prepared, gate=gate, enable_thinking=enabled)
                    self.assertIs(request.enable_thinking, enabled)
                    self.assert_no_public_answer(outcome)

    def test_reasoning_budget_wire_omits_default_and_requires_bounded_integer(self):
        request = ChatRequest(model="scripted", messages=(Message("user", "Read."),))
        self.assertNotIn("reasoning_budget_tokens", request.as_dict())
        for budget in (0, 512, 4096):
            self.assertEqual(replace(request, reasoning_budget_tokens=budget).as_dict()["reasoning_budget_tokens"], budget)
        for budget in (True, False, -1, 4097, 0.0, "512", float("nan"), float("inf"), [], {}):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                replace(request, reasoning_budget_tokens=budget).as_dict()

    def test_each_final_integrity_gate_still_removes_a_well_formed_answer(self):
        for reader, fault in ((reader, fault) for reader in ("qwen35", "gemma12b")
                for fault in ("source", "snapshot", "ingress", "evidence", "artifact", "cleanup")):
            with self.subTest(reader=reader, fault=fault):
                (self.source / "service.py").write_bytes(self.source_bytes)
                prepared = self.prepare()

                def gate():
                    paths = {"source": self.source / "service.py",
                        "snapshot": Path(prepared.snapshot.snapshot_root) / "service.py",
                        "ingress": prepared.ingress_path,
                        "artifact": prepared.run_root / "unreferenced.txt"}
                    if fault in paths:
                        paths[fault].write_bytes(b"changed\n")
                    elif fault == "evidence":
                        objects = [path for path in (prepared.run_root / "artifacts" / "objects").rglob("*") if path.is_file()]
                        self.assertTrue(objects)
                        for path in objects:
                            path.write_bytes(b"changed\n")
                    else:
                        return AcceptanceGateResult(False, "synthetic cleanup failure", passing_gate().evidence)
                    return passing_gate()

                outcome, _, _ = self.run_reading(json.dumps(answer_document("WITHHOLD ON INTEGRITY FAILURE")),
                    prepared=prepared, gate=gate, reader=reader)
                self.assert_no_public_answer(outcome)
                self.assertNotIn("WITHHOLD ON INTEGRITY FAILURE",
                    Path(outcome.answer_path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
