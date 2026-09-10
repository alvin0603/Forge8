from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from forge8 import explain
from forge8.inference import ChatResponse
from forge8.trace import verify_trace
from test_explain import passing_gate


class ReadingSeedTests(unittest.TestCase):
    """Owned inert source; one mocked model call per completed reading."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="forge8-reading-seed-")
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
            "Please explain identity().", name, focus=("service.py:3-4",))

    def run_reading(self, *, reader="qwen35", **options):
        prepared = self.prepare()
        structured = options.get("reading_format", "prose") == "structured_v1"
        raw = (json.dumps({"kind": "answer", "text": "It returns the input unchanged.",
            "citations": [{"evidence_id": "E1", "start_line": 3, "end_line": 4}]})
            if structured else "It returns the input unchanged. [E1:L3-L4]")
        backend = Mock(spec=["chat"])
        backend.chat.return_value = ChatResponse(raw, "stop",
            {"prompt_tokens": 100, "completion_tokens": 25}, {"predicted_n": 25})
        gate = Mock(side_effect=passing_gate)
        outcome = explain.run_explanation(prepared, backend, model="scripted",
            reader=reader, acceptance_gate=gate, **options)
        backend.chat.assert_called_once()
        gate.assert_called_once()
        self.assertTrue(outcome.ok, outcome.failure_reason)
        self.assertEqual(outcome.inference["calls"], 1)
        self.assertTrue(outcome.source_unchanged)
        self.assertTrue(outcome.snapshot_unchanged)
        self.assertEqual((self.source / "service.py").read_bytes(), self.source_bytes)
        self.assertTrue(verify_trace(prepared.run_root / "trace.jsonl",
            prepared.run_root / "trace.seal.json").ok)
        return outcome, backend.chat.call_args.args[0], prepared

    def assert_recipe_bound(self, outcome, prepared, recipe):
        document = json.loads(Path(outcome.explanation_path).read_bytes())
        events = [json.loads(line) for line in
            (prepared.run_root / "trace.jsonl").read_bytes().splitlines()]
        started = next(event["payload"] for event in events if event["kind"] == "explain.started")
        finished = next(event["payload"] for event in events if event["kind"] == "explain.finished")
        for container in (outcome.inference, document["inference"], started, finished["inference"]):
            if recipe is None:
                self.assertNotIn("reading_recipe", container)
            else:
                self.assertEqual(container["reading_recipe"], recipe)
        self.assertFalse(document["repository_code_executed"])
        self.assertFalse(document["semantic_claims_verified"])
        manifest = json.loads(Path(outcome.manifest_path).read_bytes())
        for name in ("explanation.json", "trace.jsonl", "trace.seal.json"):
            content = (prepared.run_root / name).read_bytes()
            state = next(item for item in manifest["files"] if item["path"] == name)
            self.assertEqual(state["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(state["size_bytes"], len(content))

    def test_default_and_explicit_one_preserve_prose_wire_and_metadata(self):
        for reader, temperature, top_k in (("qwen35", 0.6, 20), ("gemma12b", 1.0, 64)):
            with self.subTest(reader=reader):
                baseline, baseline_request, prepared = self.run_reading(reader=reader)
                explicit, explicit_request, explicit_prepared = self.run_reading(reader=reader, reading_seed=1)
                expected = {"model": "scripted", "messages": [
                    {"role": "system", "content": explain._READING_PROMPT},
                    {"role": "user", "content": baseline_request.messages[1].content}],
                    "temperature": temperature, "max_tokens": 4096, "stream": False, "seed": 1,
                    "top_p": 0.95, "top_k": top_k, "min_p": 0.0,
                    "presence_penalty": 0.0, "repeat_penalty": 1.0}
                self.assertEqual(baseline_request.as_dict(), expected)
                self.assertEqual(explicit_request.as_dict(), expected)
                self.assertEqual(explicit.inference, baseline.inference)
                self.assert_recipe_bound(baseline, prepared, None)
                self.assert_recipe_bound(explicit, explicit_prepared, None)

    def test_seeds_one_two_three_change_only_seed_and_bind_nondefaults(self):
        options = {"reading_format": "structured_v1", "reasoning_budget_tokens": 2048}
        recipe = {"format": "structured_v1", "reasoning_budget_tokens": 2048,
            "reasoning_budget_scope": "per_thinking_block"}
        qwen_wire = None
        for reader, temperature, top_k in (("qwen35", 0.6, 20), ("gemma12b", 1.0, 64)):
            baseline, baseline_request, prepared = self.run_reading(reader=reader, **options)
            baseline_wire = baseline_request.as_dict()
            if qwen_wire is None:
                qwen_wire = baseline_wire
            self.assertEqual(baseline_wire, qwen_wire | {"temperature": temperature, "top_k": top_k})
            self.assertEqual(baseline_request.messages[0].content, explain._STRUCTURED_READING_PROMPT)
            self.assertEqual(baseline_request.response_format, explain._STRUCTURED_READING_FORMAT)
            self.assertEqual(baseline_wire["reasoning_budget_tokens"], 2048)
            self.assertNotIn("chat_template_kwargs", baseline_wire)
            self.assert_recipe_bound(baseline, prepared, recipe)
            for seed in (1, 2, 3):
                with self.subTest(reader=reader, seed=seed):
                    outcome, request, candidate_prepared = self.run_reading(
                        reader=reader, reading_seed=seed, **options)
                    self.assertIs(type(request.seed), int)
                    self.assertEqual(request.as_dict(), baseline_wire | {"seed": seed})
                    expected_recipe = recipe | ({"seed": seed} if seed != 1 else {})
                    self.assertEqual(outcome.inference,
                        baseline.inference | {"reading_recipe": expected_recipe})
                    self.assert_recipe_bound(outcome, candidate_prepared, expected_recipe)

    def test_nondefault_preserves_existing_thinking_format_and_budget_fields(self):
        options = {"reading_format": "structured_v1", "reasoning_budget_tokens": 256,
            "enable_thinking": False}
        baseline, baseline_request, prepared = self.run_reading(**options)
        recipe = {"format": "structured_v1", "reasoning_budget_tokens": 256,
            "reasoning_budget_scope": "per_thinking_block", "enable_thinking": False}
        self.assert_recipe_bound(baseline, prepared, recipe)
        for seed in (2, 3):
            with self.subTest(seed=seed):
                outcome, request, candidate_prepared = self.run_reading(reading_seed=seed, **options)
                self.assertEqual(request.as_dict(), baseline_request.as_dict() | {"seed": seed})
                self.assert_recipe_bound(outcome, candidate_prepared, recipe | {"seed": seed})

    def test_nondefault_prose_and_upper_boundary_retain_seed_without_other_wire_options(self):
        _, baseline_request, _ = self.run_reading()
        for seed in (2, 3, 2147483647):
            with self.subTest(seed=seed):
                outcome, request, prepared = self.run_reading(reading_seed=seed)
                self.assertEqual(request.as_dict(), baseline_request.as_dict() | {"seed": seed})
                self.assert_recipe_bound(outcome, prepared, {"format": "prose",
                    "reasoning_budget_tokens": None, "reasoning_budget_scope": "per_thinking_block",
                    "seed": seed})

    def assert_rejected_before_artifacts(self, prepared, **options):
        def inventory():
            return {path.relative_to(prepared.run_root).as_posix():
                path.read_bytes() if path.is_file() else None
                for path in prepared.run_root.rglob("*")}

        before = inventory()
        backend, gate = Mock(spec=["chat"]), Mock()
        with self.assertRaises(explain.ExplanationError):
            explain.run_explanation(prepared, backend, model="scripted",
                acceptance_gate=gate, **({"reader": "qwen35"} | options))
        backend.chat.assert_not_called()
        gate.assert_not_called()
        self.assertEqual(inventory(), before)
        self.assertFalse((prepared.run_root / "artifacts").exists())
        self.assertFalse((prepared.run_root / "trace.jsonl").exists())

    def test_invalid_seeds_fail_before_artifacts_or_inference(self):
        class IntegerSubclass(int):
            pass

        prepared = self.prepare()
        for reader in ("qwen35", "gemma12b"):
            for seed in (True, False, None, 0, -1, 2147483648, 2**100,
                    1.0, float("nan"), float("inf"), "1", [], {}, IntegerSubclass(1)):
                with self.subTest(reader=reader, seed=seed):
                    self.assert_rejected_before_artifacts(prepared, reader=reader, reading_seed=seed)

    def test_nondefault_seeds_reject_gemma4_before_artifacts_or_inference(self):
        prepared = self.prepare()
        for seed in (2, 3, 2147483647):
            with self.subTest(seed=seed):
                self.assert_rejected_before_artifacts(prepared, reader="gemma4", reading_seed=seed)


if __name__ == "__main__":
    unittest.main()
