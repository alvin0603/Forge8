from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from forge8 import cli, explain
from forge8.trace import verify_trace
from test_explain import (
    ScriptedBackend,
    answer_document,
    insufficient_document,
    passing_gate,
    read_service,
)


class ExplanationFocusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-focus-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "repository"
        (self.source / "src").mkdir(parents=True)
        self.content = b'raise RuntimeError("must never execute")\r\n\r\ndef identity(value):\r\n    return value\r\n'
        (self.source / "src/service.py").write_bytes(self.content)

    def prepare(self, name: str = "focused", *, focus=("src/service.py:3-4",)):
        return explain.prepare_explanation(
            self.source, self.root / name, "Explain identity().", name, focus=focus
        )

    def run_prepared(self, prepared, outputs):
        backend = ScriptedBackend(outputs)
        gate = Mock(side_effect=passing_gate)
        outcome = explain.run_explanation(
            prepared, backend, model="scripted", acceptance_gate=gate
        )
        gate.assert_called_once()
        self.assertTrue(verify_trace(
            prepared.run_root / "trace.jsonl", prepared.run_root / "trace.seal.json"
        ).ok)
        return outcome, backend

    def test_explicit_ranges_are_retained_before_one_terminal_inference(self) -> None:
        prepared = self.prepare()
        self.assertEqual(sorted(path.name for path in prepared.run_root.iterdir()), ["input"])
        ingress = json.loads(prepared.ingress_path.read_text(encoding="utf-8"))
        self.assertEqual(prepared.focus, ("src/service.py:3-4",))
        self.assertEqual(ingress["focus"], list(prepared.focus))
        outcome, backend = self.run_prepared(
            prepared, [answer_document(inference="The function returns its argument.")]
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.inference, {"calls": 1, "actions": 1, "parse_failures": 0})
        self.assertEqual(outcome.coverage["observed"]["lines"], 2)
        self.assertEqual(outcome.answer["claims"][0]["text"], "    return value")
        request = backend.requests[0]
        self.assertEqual(request.response_format, explain._TERMINAL_RESPONSE_FORMAT)
        self.assertIn("USER-SELECTED RANGES", request.messages[1].content)
        self.assertNotIn("RECENT NAVIGATION", request.messages[1].content)
        self.assertNotIn("must never execute", request.messages[1].content)
        events = [json.loads(line) for line in (prepared.run_root / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        reads = [event for event in events if event["kind"] == "tool.completed"]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]["payload"]["origin"], "user_focus")
        self.assertTrue(reads[0]["payload"]["citable"])
        self.assertFalse(any(event["kind"].startswith("navigation.seed") for event in events))
        self.assertEqual((self.source / "src/service.py").read_bytes(), self.content)

    def test_two_files_and_overlapping_ranges_use_normal_evidence_coverage(self) -> None:
        (self.source / "other.py").write_text("value = 1\n", encoding="utf-8")
        prepared = self.prepare(focus=("src/service.py:3-4", "src/service.py:4-4", "other.py:1-1"))
        outcome, backend = self.run_prepared(prepared, [insufficient_document()])
        self.assertEqual(outcome.status, "insufficient_evidence")
        self.assertEqual(outcome.evidence_count, 2)
        self.assertEqual(outcome.coverage["observed"]["lines"], 3)
        self.assertEqual(outcome.coverage["observed"]["files"], 2)
        self.assertEqual(len(backend.requests), 1)

    def test_focus_case_aliases_are_native_windows_only_and_cite_canonical_paths(self) -> None:
        focus = ("SRC/SERVICE.PY:3-4",)
        if os.name != "nt":
            with self.assertRaises(explain.ExplanationError):
                self.prepare(focus=focus)
            return
        self.assertTrue((self.source / "SRC/SERVICE.PY").is_file())
        prepared = self.prepare(focus=focus)
        outcome, backend = self.run_prepared(prepared, [answer_document()])
        self.assertTrue(outcome.ok, outcome.failure_reason)
        self.assertEqual(prepared.focus, focus)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(
            outcome.answer["claims"][0]["citations"][0]["path"], "src/service.py"
        )

    def test_short_multiline_quote_is_copied_by_host_not_regenerated(self) -> None:
        prepared = self.prepare()
        document = json.loads(answer_document())
        document["action"]["claims"][0]["citation"].update(start_line=3, end_line=4)
        outcome, backend = self.run_prepared(prepared, [json.dumps(document)])
        self.assertTrue(outcome.ok, outcome.failure_reason)
        self.assertEqual(
            outcome.answer["claims"][0]["text"],
            "def identity(value):\n    return value",
        )
        self.assertEqual(len(backend.requests), 1)
        self.assertIn("EXACT SOURCE EXCERPT", Path(outcome.answer_path).read_text(encoding="utf-8"))

    def test_multiline_quote_still_rejects_oversize_or_unobserved_source(self) -> None:
        for index, size in enumerate((600, 601)):
            content = "#" + "x" * 299 + "\n#" + "x" * (size - 302)
            self.assertEqual(len(content), size)
            (self.source / "long.py").write_text(content + "\n", encoding="utf-8")
            prepared = self.prepare(f"quote-cap-{index}", focus=("long.py:1-2",))
            document = json.loads(answer_document(start_line=1, end_line=2))
            document["action"]["claims"][0]["citation"].update(start_line=1, end_line=2)
            outcome, backend = self.run_prepared(prepared, [json.dumps(document)])
            self.assertEqual(outcome.ok, index == 0)
            self.assertEqual(len(backend.requests), 1)
        prepared = self.prepare("quote-outside")
        document = json.loads(answer_document())
        document["action"]["claims"][0]["citation"].update(start_line=2, end_line=4)
        outcome, _ = self.run_prepared(prepared, [json.dumps(document)])
        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.answer)

    def test_focus_rejects_noncanonical_unsafe_or_duplicate_selectors(self) -> None:
        selectors = (
            ("../service.py:1-1",), ("/service.py:1-1",),
            ("C:/service.py:1-1",), ("src\\service.py:1-1",),
            ("src/./service.py:1-1",), ("src//service.py:1-1",),
            (".env:1-1",), ("src/.private/file.py:1-1",),
            ("src/service.py:01-2",), ("src/service.py:0-2",),
            ("src/service.py:3-2",), ("src/service.py:1-81",),
            ("src/service.py:1000001-1000001",), ("src/service.py:3",),
            ("src/service.py:3-4", "src/service.py:3-4"),
            tuple(f"src/service.py:{index}-{index}" for index in range(1, 5)),
            ["src/service.py:3-4"], (None,),
        )
        for index, focus in enumerate(selectors):
            with self.subTest(focus=focus), self.assertRaises(explain.ExplanationError):
                self.prepare(f"invalid-{index}", focus=focus)
            self.assertFalse((self.root / f"invalid-{index}").exists())

    def test_focus_rejects_missing_directory_excluded_and_past_eof_sources(self) -> None:
        (self.source / "empty.py").write_text("", encoding="utf-8")
        for index, selector in enumerate(("missing.py:1-1", "src:1-1", "empty.py:1-1", "src/service.py:3-5")):
            with self.subTest(selector=selector), self.assertRaises(explain.ExplanationError):
                self.prepare(f"bad-source-{index}", focus=(selector,))

    def test_changed_focus_cannot_reuse_the_original_ingress(self) -> None:
        prepared = replace(self.prepare(), focus=("src/service.py:4-4",))
        backend = ScriptedBackend([])
        with self.assertRaisesRegex(explain.ExplanationError, "ingress identity"):
            explain.run_explanation(prepared, backend, model="scripted", acceptance_gate=passing_gate)
        self.assertFalse(backend.requests)
        self.assertFalse((prepared.run_root / "trace.jsonl").exists())

    def test_oversized_and_pool_exceeding_focus_fail_with_zero_model_calls(self) -> None:
        for index, (width, count) in enumerate(((4100, 1), (3100, 3))):
            for number in range(count):
                (self.source / f"long{number}.py").write_text("#" + "x" * width + "\n", encoding="utf-8")
            # A forged/direct API preparation still faces the runner's guards;
            # the real preparation now rejects this before model acquisition.
            with patch.object(explain, "_preflight_focus"):
                prepared = self.prepare(f"budget-{index}", focus=tuple(f"long{number}.py:1-1" for number in range(count)))
            outcome, backend = self.run_prepared(prepared, [])
            self.assertEqual(outcome.status, "configuration_error")
            self.assertIn("focus could not be retained completely", outcome.failure_reason)
            self.assertEqual(outcome.inference["calls"], 0)
            self.assertFalse(backend.requests)

    def test_focus_budgets_fail_before_cli_asset_hashing_or_server_start(self) -> None:
        assets = self.root / "assets"
        assets.mkdir()
        cases = (
            ("read-cap", 4100, 1, 12000, "truncated"),
            ("pool-cap", 3100, 3, 12000, "citable characters"),
            ("prompt-cap", 100, 1, 500, "fixed prompt budget"),
        )
        for name, width, count, prompt_cap, reason in cases:
            for number in range(count):
                (self.source / f"long{number}.py").write_text(
                    "#" + "x" * width + "\n", encoding="utf-8"
                )
            selectors = [f"long{number}.py:1-1" for number in range(count)]
            argv = ["explain", str(self.source), "--question", "Explain these ranges.", "--json"]
            for selector in selectors:
                argv += ["--focus", selector]
            output = io.StringIO()
            with (
                self.subTest(case=name),
                patch.object(cli, "_load_deployment", return_value=None),
                patch.object(cli, "_resolve_fix_asset_root", return_value=assets),
                patch.object(cli, "_new_task_id", return_value=name),
                patch.object(cli, "prepare_server") as prepare_server,
                patch.object(cli, "LocalServerSupervisor") as supervisor,
                patch.object(explain, "_MAX_CONTEXT_CHARS", prompt_cap),
                redirect_stdout(output), redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(cli.main(argv), 2)
            prepare_server.assert_not_called()
            supervisor.assert_not_called()
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "configuration_error")
            self.assertIn(selectors[-1], result["error"])
            self.assertIn(reason, result["error"])
            self.assertIn("select a smaller complete range", result["error"])
            run_root = Path(result["run_root"])
            self.assertEqual(sorted(path.name for path in run_root.iterdir()), ["input"])
            self.assertEqual((self.source / "src/service.py").read_bytes(), self.content)

    def test_preflight_interruption_cleans_scratch_and_preserves_source(self) -> None:
        with patch.object(explain.WorkspaceTools, "read_text", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.prepare()
        self.assertEqual(
            sorted(path.name for path in (self.root / "focused").iterdir()), ["input"]
        )
        self.assertEqual((self.source / "src/service.py").read_bytes(), self.content)

    def test_a_clipped_read_is_not_silently_accepted(self) -> None:
        prepared = self.prepare()
        original = explain.WorkspaceTools.read_text

        def clipped(tools, path, *, start_line=1, end_line=None):
            return original(tools, path, start_line=start_line, end_line=end_line - 1)

        with patch.object(explain.WorkspaceTools, "read_text", clipped):
            outcome, backend = self.run_prepared(prepared, [])
        self.assertEqual(outcome.status, "configuration_error")
        self.assertEqual(outcome.evidence_count, 0)
        self.assertIn("exact requested range", outcome.failure_reason)
        self.assertFalse(backend.requests)

    def test_read_limits_are_inclusive_and_measured_with_source_line_prefixes(self) -> None:
        for index, (content, selector) in enumerate((
            ("#\n" * 80, "boundary.py:1-80"),
            ("#" + "x" * 3992 + "\n", "boundary.py:1-1"),
        )):
            (self.source / "boundary.py").write_text(content, encoding="utf-8")
            prepared = self.prepare(f"boundary-{index}", focus=(selector,))
            outcome, backend = self.run_prepared(prepared, [insufficient_document()])
            self.assertEqual(outcome.status, "insufficient_evidence")
            self.assertEqual(outcome.evidence_count, 1)
            self.assertEqual(len(backend.requests), 1)

    def test_terminal_failures_never_get_another_inference_or_navigation(self) -> None:
        for index, output in enumerate(("not json", read_service(), answer_document(evidence_id="E99"))):
            prepared = self.prepare(f"terminal-{index}")
            outcome, backend = self.run_prepared(prepared, [output])
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.status, "stalled")
            self.assertEqual(len(backend.requests), 1)
            self.assertIsNone(outcome.answer)

    def test_source_drift_still_fails_before_focused_reads_and_inference(self) -> None:
        prepared = self.prepare()
        (self.source / "src/service.py").write_text("changed\n", encoding="utf-8")
        outcome, backend = self.run_prepared(prepared, [])
        self.assertEqual(outcome.status, "source_drift")
        self.assertEqual(outcome.evidence_count, 0)
        self.assertFalse(backend.requests)

    def test_interrupted_focus_read_still_runs_cleanup_and_seals(self) -> None:
        prepared = self.prepare()
        with patch.object(explain.WorkspaceTools, "read_text", side_effect=KeyboardInterrupt):
            outcome, backend = self.run_prepared(prepared, [])
        self.assertEqual(outcome.status, "interrupted")
        self.assertFalse(backend.requests)


if __name__ == "__main__":
    unittest.main()
