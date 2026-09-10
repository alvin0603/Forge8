from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from forge8.actions import (
    ActionEnvelope,
    CheckBudget,
    FinishAction,
    RunChecksAction,
    WriteTextAction,
)
from forge8.capsule import FileChanges, VerificationResult, load_capsule
from forge8.checks import CheckRunner
from forge8.inference import ChatResponse
from forge8.operator import (
    AcceptanceGateResult,
    OperatorConfig,
    PreparedWorkspaceTask,
    _source_preservation_violations,
    run_capsule,
    run_prepared_task,
)


_BROKEN_APP = "def answer():\n    return 1\n"
_FIXED_APP = "def answer():\n    return 2\n"
_APP_TEST = """import unittest

from app import answer


class AnswerTests(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(answer(), 2)
"""


def _response(content: str, call: int) -> ChatResponse:
    return ChatResponse(
        content=content,
        finish_reason="stop",
        usage={"prompt_tokens": 40 + call, "completion_tokens": 10 + call},
        timings={"predicted_ms": 10.0},
    )


class _ScriptedBackend:
    def __init__(self, outputs: list[str | Exception]) -> None:
        self.outputs = outputs
        self.requests: list[Any] = []

    def chat(self, request: Any) -> ChatResponse:
        self.requests.append(request)
        if not self.outputs:
            raise AssertionError("operator requested an unscripted inference call")
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return _response(item, len(self.requests))


def _fingerprints(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _events(run_root: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in Path(run_root, "trace.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]


class PreparedWorkspaceTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-task-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_workspaces(
        self,
        name: str,
        *,
        passing: bool = False,
    ) -> tuple[Path, Path, Path]:
        run_root = self.root / name
        source = run_root / "caller-input" / "source"
        candidate = run_root / "caller-input" / "candidate"
        (source / "tests").mkdir(parents=True)
        (source / "app.py").write_text(
            _FIXED_APP if passing else _BROKEN_APP,
            encoding="utf-8",
        )
        (source / "tests" / "test_app.py").write_text(_APP_TEST, encoding="utf-8")
        shutil.copytree(source, candidate)
        return run_root, source, candidate

    def preservation_workspaces(self, name: str) -> tuple[Path, Path]:
        root = self.root / name
        source = root / "source"
        candidate = root / "candidate"
        source.mkdir(parents=True)
        candidate.mkdir(parents=True)
        return source, candidate

    def test_source_preservation_rejects_only_new_python_trailing_whitespace(
        self,
    ) -> None:
        source, candidate = self.preservation_workspaces("trailing-whitespace")
        (source / "app.py").write_bytes(
            b'"""Module."""\n'
            b"LEGACY = 1 \t\n"
            b'TEMPLATE = """alpha\n'
            b"beta\n"
            b'"""\n'
            b"VALUE = 1\n"
            b"OTHER = 2\n"
        )
        (candidate / "app.py").write_bytes(
            b'"""Module."""\n'
            b"LEGACY = 2 \t\n"
            b'TEMPLATE = """alpha\n'
            b"beta  \n"
            b'"""\n'
            b"VALUE = 2 \n"
            b"OTHER = 3\t\n"
        )

        violations = _source_preservation_violations(
            source,
            candidate,
            ("app.py",),
        )

        self.assertEqual(len(violations), 2, violations)
        self.assertFalse(any("app.py:2:" in message for message in violations))
        self.assertTrue(any("app.py:6:" in message for message in violations))
        self.assertTrue(any("app.py:7:" in message for message in violations))

    def test_source_preservation_does_not_align_unequal_replace_blocks(self) -> None:
        source, candidate = self.preservation_workspaces("unequal-replace")
        (source / "app.py").write_bytes(
            b"OLD = 1 \n"
            b"KEEP = 1\n"
        )
        (candidate / "app.py").write_bytes(
            b"INSERT = 0 \n"
            b"OLD = 2\n"
            b"KEEP = 1\n"
        )

        violations = _source_preservation_violations(
            source,
            candidate,
            ("app.py",),
        )

        self.assertEqual(len(violations), 1, violations)
        self.assertIn("app.py:1:", violations[0])
        self.assertIn("trailing space", violations[0])

    def test_source_preservation_fails_closed_when_python_cannot_be_inspected(
        self,
    ) -> None:
        cases = (
            (
                "token-error.py",
                b'"""Docs."""\nVALUE = 1\n',
                b'VALUE = 2 \nTEXT = """unterminated\n',
                ("cannot be tokenized", "module docstring"),
            ),
            (
                "parse-error.py",
                b'"""Docs."""\nVALUE = 1\n',
                b'"""Docs."""\nif:\n    pass\n',
                ("cannot be parsed", "module docstring"),
            ),
        )
        for path, before, after, expected_fragments in cases:
            with self.subTest(path=path):
                source, candidate = self.preservation_workspaces(path)
                (source / path).write_bytes(before)
                (candidate / path).write_bytes(after)

                violations = _source_preservation_violations(
                    source,
                    candidate,
                    (path,),
                )

                combined = "\n".join(violations)
                for fragment in expected_fragments:
                    self.assertIn(fragment, combined)

    def test_source_preservation_skips_non_python_and_markdown_hard_breaks(
        self,
    ) -> None:
        source, candidate = self.preservation_workspaces("non-python")
        (source / "README.md").write_bytes(b"first line\nsecond line\n")
        (candidate / "README.md").write_bytes(b"first line  \nsecond line\n")
        (source / "notes.txt").write_bytes(b"plain text\n")
        (candidate / "notes.txt").write_bytes(b"plain text\t\n")

        self.assertEqual(
            _source_preservation_violations(
                source,
                candidate,
                ("README.md", "notes.txt"),
            ),
            (),
        )

    def test_source_preservation_handles_fstrings_and_lf_physical_lines(
        self,
    ) -> None:
        cases = (
            (
                "multiline-fstring.py",
                b'VALUE = f"""alpha\nbeta\n"""\n',
                b'VALUE = f"""alpha\nbeta  \n"""\n',
            ),
            (
                "cr-only.py",
                b'TEXT = """alpha\rbeta\r"""\rVALUE = 1\r',
                b'TEXT = """alpha\rbeta  \r"""\rVALUE = 2\r',
            ),
        )
        for path, before, after in cases:
            with self.subTest(path=path):
                source, candidate = self.preservation_workspaces(path)
                (source / path).write_bytes(before)
                (candidate / path).write_bytes(after)

                self.assertEqual(
                    _source_preservation_violations(source, candidate, (path,)),
                    (),
                )

    def test_source_preservation_rejects_only_lost_terminal_newline(
        self,
    ) -> None:
        cases = (
            ("lf-lost.py", b"VALUE = 1\n", b"VALUE = 2", True),
            ("crlf-lost.py", b"VALUE = 1\r\n", b"VALUE = 2", True),
            ("crlf-to-lf.py", b"VALUE = 1\r\n", b"VALUE = 2\n", False),
            ("already-none.py", b"VALUE = 1", b"VALUE = 2", False),
        )
        for path, before, after, rejected in cases:
            with self.subTest(path=path):
                source, candidate = self.preservation_workspaces(path)
                (source / path).write_bytes(before)
                (candidate / path).write_bytes(after)

                violations = _source_preservation_violations(
                    source,
                    candidate,
                    (path,),
                )

                self.assertEqual(bool(violations), rejected, violations)
                if rejected:
                    self.assertIn(path, violations[0])

    def test_source_preservation_tracks_module_docstrings_without_overreach(
        self,
    ) -> None:
        header = b"#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"
        cases = (
            (
                "deleted.py",
                header + b'"""Old docs."""\nVALUE = 1\n',
                header + b"VALUE = 2\n",
                True,
            ),
            (
                "modified.py",
                header + b'"""Old docs."""\nVALUE = 1\n',
                header + b'"""New docs."""\nVALUE = 2\n',
                False,
            ),
            (
                "never-had-one.py",
                b"VALUE = 1\n",
                b"VALUE = 2\n",
                False,
            ),
            (
                "bom-deleted.py",
                b'\xef\xbb\xbf"""Old docs."""\nVALUE = 1\n',
                b"\xef\xbb\xbfVALUE = 2\n",
                True,
            ),
        )
        for path, before, after, rejected in cases:
            with self.subTest(path=path):
                source, candidate = self.preservation_workspaces(path)
                (source / path).write_bytes(before)
                (candidate / path).write_bytes(after)

                violations = _source_preservation_violations(
                    source,
                    candidate,
                    (path,),
                )

                self.assertEqual(bool(violations), rejected, violations)
                if rejected:
                    self.assertIn(path, violations[0])
                    self.assertIn("docstring", violations[0])

    def test_source_preservation_skips_added_and_removed_paths(self) -> None:
        source, candidate = self.preservation_workspaces("path-existence")
        (source / "removed.py").write_bytes(b'"""Docs."""\nVALUE = 1\n')
        (candidate / "added.py").write_bytes(b"VALUE = 2 \t")

        self.assertEqual(
            _source_preservation_violations(
                source,
                candidate,
                ("removed.py", "added.py"),
            ),
            (),
        )

    def verification(
        self,
        task_id: str,
        source: Path,
        candidate: Path,
        *,
        force_empty_changes: bool = False,
    ) -> VerificationResult:
        passed = (candidate / "app.py").read_text(encoding="utf-8") == _FIXED_APP
        changed = (source / "app.py").read_bytes() != (candidate / "app.py").read_bytes()
        changes = FileChanges(
            added=(),
            modified=() if force_empty_changes or not changed else ("app.py",),
            removed=(),
        )
        summary = {"task": task_id, "passed": passed}
        return VerificationResult(
            capsule_id=task_id,
            status="passed" if passed else "failed",
            ok=passed,
            started_at="2026-08-31T00:00:00Z",
            ended_at="2026-08-31T00:00:00Z",
            duration_seconds=0.0,
            return_code=0 if passed else 1,
            timed_out=False,
            launch_error=None,
            changes=changes,
            policy_violations=(),
            required_artifacts_missing=(),
            stdout=json.dumps(summary) + "\n",
            stderr="",
            output_truncated=False,
            verifier_summary=summary,
            candidate_fingerprints=_fingerprints(candidate),
        )

    def task(
        self,
        task_id: str,
        source: Path,
        candidate: Path,
        *,
        require_failing_baseline: bool = True,
        require_nonempty_changes: bool = True,
        force_empty_changes: bool = False,
    ) -> PreparedWorkspaceTask:
        return PreparedWorkspaceTask(
            task_id=task_id,
            prompt_text="Repair app.answer so the fixed unittest passes.",
            source_workspace=source,
            candidate_workspace=candidate,
            source_fingerprints=_fingerprints(source),
            allowed_write_roots=("app.py",),
            selected_check_ids=("python_unittest",),
            write_enabled=True,
            policy_metadata={"network": "deny", "isolation": "process_only"},
            verify=lambda workspace: self.verification(
                task_id,
                source,
                workspace,
                force_empty_changes=force_empty_changes,
            ),
            require_failing_baseline=require_failing_baseline,
            require_nonempty_changes=require_nonempty_changes,
        )

    @staticmethod
    def write_action(candidate: Path) -> str:
        guard = hashlib.sha256((candidate / "app.py").read_bytes()).hexdigest()
        return ActionEnvelope(
            rationale="Apply the smallest guarded repair observed from the failing test.",
            action=WriteTextAction(
                path="app.py",
                expected_sha256=guard,
                content=_FIXED_APP,
            ),
        ).as_json()

    def test_generic_vertical_path_preserves_source_and_packages_nonempty_patch(
        self,
    ) -> None:
        run_root, source, candidate = self.make_workspaces("generic-verified")
        backend = _ScriptedBackend([self.write_action(candidate)])

        outcome = run_prepared_task(
            self.task("generic.answer-repair", source, candidate),
            run_root,
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
        )

        self.assertTrue(outcome.verified, outcome.failure_reason)
        self.assertEqual(outcome.status, "verified")
        self.assertEqual(outcome.capsule_id, "generic.answer-repair")
        self.assertEqual(Path(outcome.run_root), run_root.resolve())
        self.assertEqual((source / "app.py").read_text(encoding="utf-8"), _BROKEN_APP)
        self.assertEqual((candidate / "app.py").read_text(encoding="utf-8"), _FIXED_APP)
        self.assertTrue((run_root / "caller-input").is_dir())
        self.assertEqual([change.path for change in outcome.delivery.changes], ["app.py"])
        self.assertIn(
            "+    return 2",
            Path(outcome.delivery.patch_path).read_text(encoding="utf-8"),
        )

    def test_source_preservation_feedback_is_repairable_in_the_same_run(
        self,
    ) -> None:
        run_root, source, candidate = self.make_workspaces("preservation-repair")
        original = '"""Application behavior."""\n\ndef answer():\n    return 1\n'
        regressed = "def answer():\n    return 2 \n"
        repaired = '"""Application behavior."""\n\ndef answer():\n    return 2\n'
        (source / "app.py").write_bytes(original.encode("utf-8"))
        (candidate / "app.py").write_bytes(original.encode("utf-8"))

        def behavior_verifier(workspace: Path) -> VerificationResult:
            namespace: dict[str, Any] = {}
            try:
                exec(
                    compile(
                        (workspace / "app.py").read_text(encoding="utf-8"),
                        "app.py",
                        "exec",
                    ),
                    namespace,
                )
                passed = namespace["answer"]() == 2
            except Exception:
                passed = False
            changed = (source / "app.py").read_bytes() != (
                workspace / "app.py"
            ).read_bytes()
            summary = {"answer_is_two": passed}
            return VerificationResult(
                capsule_id="generic.preservation-repair",
                status="passed" if passed else "failed",
                ok=passed,
                started_at="2026-08-31T00:00:00Z",
                ended_at="2026-08-31T00:00:00Z",
                duration_seconds=0.0,
                return_code=0 if passed else 1,
                timed_out=False,
                launch_error=None,
                changes=FileChanges(
                    added=(),
                    modified=("app.py",) if changed else (),
                    removed=(),
                ),
                policy_violations=(),
                required_artifacts_missing=(),
                stdout=json.dumps(summary) + "\n",
                stderr="",
                output_truncated=False,
                verifier_summary=summary,
                candidate_fingerprints=_fingerprints(workspace),
            )

        first = ActionEnvelope(
            rationale="Make the behavior pass, but accidentally damage source hygiene.",
            action=WriteTextAction(
                path="app.py",
                expected_sha256=hashlib.sha256(original.encode("utf-8")).hexdigest(),
                content=regressed,
            ),
        ).as_json()
        second = ActionEnvelope(
            rationale="Restore the documented, whitespace-clean localized result.",
            action=WriteTextAction(
                path="app.py",
                expected_sha256=hashlib.sha256(regressed.encode("utf-8")).hexdigest(),
                content=repaired,
            ),
        ).as_json()
        backend = _ScriptedBackend([first, second])
        task = replace(
            self.task("generic.preservation-repair", source, candidate),
            verify=behavior_verifier,
        )

        outcome = run_prepared_task(
            task,
            run_root,
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
        )

        self.assertTrue(outcome.verified, outcome.failure_reason)
        self.assertEqual(len(backend.requests), 2)
        repair_context = backend.requests[1].messages[1].content
        self.assertIn("policy_violations", repair_context)
        self.assertIn("app.py:2:", repair_context)
        self.assertIn("module docstring", repair_context)
        self.assertEqual((candidate / "app.py").read_text(encoding="utf-8"), repaired)

    def test_acceptance_gate_runs_once_before_trace_seal_and_promotion(self) -> None:
        run_root, source, candidate = self.make_workspaces("acceptance-gate-pass")
        calls: list[str] = []

        def gate() -> AcceptanceGateResult:
            calls.append("gate")
            self.assertFalse((run_root / "trace.seal.json").exists())
            self.assertFalse((run_root / "delivery").exists())
            return AcceptanceGateResult(
                ok=True,
                reason=None,
                evidence={"shutdown_status": "terminated", "secret_cleared": True},
            )

        outcome = run_prepared_task(
            self.task("generic.acceptance-gate-pass", source, candidate),
            run_root,
            _ScriptedBackend([self.write_action(candidate)]),
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
            acceptance_gate=gate,
        )

        self.assertTrue(outcome.verified)
        self.assertEqual(calls, ["gate"])
        kinds = [event["kind"] for event in _events(outcome.run_root)]
        self.assertLess(
            kinds.index("acceptance_gate.passed"),
            kinds.index("verifier.post_acceptance_gate"),
        )
        self.assertLess(
            kinds.index("verifier.post_acceptance_gate"),
            kinds.index("run.finished"),
        )

    def test_post_gate_candidate_mutation_cannot_promote_stale_verification(self) -> None:
        run_root, source, candidate = self.make_workspaces("acceptance-gate-race")

        def gate() -> AcceptanceGateResult:
            (candidate / "app.py").write_text(
                "def answer():\n    return 999\n",
                encoding="utf-8",
            )
            return AcceptanceGateResult(
                ok=True,
                reason=None,
                evidence={"shutdown_status": "terminated", "secret_cleared": True},
            )

        outcome = run_prepared_task(
            self.task("generic.acceptance-gate-race", source, candidate),
            run_root,
            _ScriptedBackend([self.write_action(candidate)]),
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
            acceptance_gate=gate,
        )

        self.assertEqual(outcome.status, "verification_failed")
        self.assertFalse(outcome.verified)
        self.assertFalse(outcome.delivery.verified)
        self.assertEqual(
            outcome.failure_reason,
            "post-acceptance-gate verification failed",
        )

    def test_failed_acceptance_gate_keeps_passing_verifier_non_promotable(self) -> None:
        run_root, source, candidate = self.make_workspaces("acceptance-gate-fail")

        outcome = run_prepared_task(
            self.task("generic.acceptance-gate-fail", source, candidate),
            run_root,
            _ScriptedBackend([self.write_action(candidate)]),
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
            acceptance_gate=lambda: AcceptanceGateResult(
                ok=False,
                reason="local inference process tree was not reclaimed",
                evidence={"shutdown_status": "error", "secret_cleared": False},
            ),
        )

        self.assertEqual(outcome.status, "acceptance_gate_failed")
        self.assertFalse(outcome.verified)
        self.assertTrue(outcome.verification.ok)
        self.assertFalse(outcome.delivery.verified)
        self.assertIn("not reclaimed", outcome.failure_reason or "")
        manifest = json.loads(
            Path(outcome.delivery.manifest_path).read_text(encoding="utf-8")
        )
        self.assertFalse(manifest["verified"])
        failed = [
            event
            for event in _events(outcome.run_root)
            if event["kind"] == "acceptance_gate.failed"
        ]
        self.assertEqual(
            failed[0]["payload"]["evidence"]["shutdown_status"],
            "error",
        )

    def test_raising_acceptance_gate_fails_closed(self) -> None:
        run_root, source, candidate = self.make_workspaces("acceptance-gate-raises")

        def gate() -> AcceptanceGateResult:
            raise RuntimeError("shutdown evidence unavailable")

        outcome = run_prepared_task(
            self.task("generic.acceptance-gate-raises", source, candidate),
            run_root,
            _ScriptedBackend([self.write_action(candidate)]),
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
            acceptance_gate=gate,
        )

        self.assertEqual(outcome.status, "acceptance_gate_failed")
        self.assertFalse(outcome.verified)
        self.assertFalse(outcome.delivery.verified)
        self.assertIn("RuntimeError", outcome.failure_reason or "")

    def test_only_selected_checks_execute_and_unselected_action_is_denied(self) -> None:
        run_root, source, candidate = self.make_workspaces("exact-check-subset")
        calls: list[str] = []

        def executor_factory(policy, artifacts):
            delegate = CheckRunner(policy, artifacts)

            class RecordingExecutor:
                def run(self, check_id: str):
                    calls.append(check_id)
                    return delegate.run(check_id)

            return RecordingExecutor()

        selected_task = replace(
            self.task("generic.exact-checks", source, candidate),
            check_executor_factory=executor_factory,
        )
        denied = ActionEnvelope(
            rationale="Try an available but unselected check to prove task confinement.",
            action=RunChecksAction(
                checks=("extra_check",),
                budget=CheckBudget(timeout_seconds=10, max_output_chars=1000),
            ),
        ).as_json()
        backend = _ScriptedBackend([denied, self.write_action(candidate)])

        with patch(
            "forge8.operator.discover_checks",
            return_value=("python_unittest", "extra_check"),
        ):
            outcome = run_prepared_task(
                selected_task,
                run_root,
                backend,
                model="scripted-local-model",
                config=OperatorConfig(max_inference_calls=3, max_actions=3),
            )

        self.assertTrue(outcome.verified)
        self.assertTrue(calls)
        self.assertEqual(set(calls), {"python_unittest"})
        self.assertNotIn("extra_check", backend.requests[0].messages[1].content)
        self.assertIn("checks are not selected", backend.requests[1].messages[1].content)
        completed_ids = {
            event["payload"]["check_id"]
            for event in _events(outcome.run_root)
            if event["kind"] == "check.completed"
        }
        self.assertEqual(completed_ids, {"python_unittest"})

    def test_already_passing_required_baseline_makes_zero_backend_calls(self) -> None:
        run_root, source, candidate = self.make_workspaces(
            "already-passing",
            passing=True,
        )
        backend = _ScriptedBackend([])

        outcome = run_prepared_task(
            self.task(
                "generic.already-passing",
                source,
                candidate,
                require_nonempty_changes=False,
            ),
            run_root,
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
        )

        self.assertFalse(outcome.verified)
        self.assertFalse(outcome.delivery.verified)
        self.assertEqual(outcome.status, "verification_failed")
        self.assertEqual(outcome.inference.calls, 0)
        self.assertEqual(outcome.actions, 0)
        self.assertEqual(backend.requests, [])
        self.assertEqual(
            outcome.failure_reason,
            "selected checks already pass at baseline",
        )
        self.assertFalse(
            any(event["kind"].startswith("inference.") for event in _events(outcome.run_root))
        )

    def test_source_drift_is_rejected_before_inference_on_a_failing_baseline(
        self,
    ) -> None:
        run_root, source, candidate = self.make_workspaces("source-drift")
        task = self.task("generic.source-drift", source, candidate)
        (source / "app.py").write_text(
            "def answer():\n    return 0\n",
            encoding="utf-8",
        )
        backend = _ScriptedBackend([])

        outcome = run_prepared_task(
            task,
            run_root,
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
        )

        reason = (
            "source bytes do not match the prepared ingress fingerprints "
            "before inference"
        )
        self.assertEqual(outcome.status, "verification_failed")
        self.assertFalse(outcome.verified)
        self.assertEqual(outcome.failure_reason, reason)
        self.assertEqual(outcome.inference.calls, 0)
        self.assertEqual(backend.requests, [])
        rejected = [
            event
            for event in _events(outcome.run_root)
            if event["kind"] == "workspace.admission_rejected"
        ]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["payload"]["reason"], reason)
        self.assertFalse(rejected[0]["payload"]["source_matches"])
        self.assertTrue(rejected[0]["payload"]["candidate_matches"])

    def test_candidate_drift_is_rejected_before_inference_on_a_failing_baseline(
        self,
    ) -> None:
        run_root, source, candidate = self.make_workspaces("candidate-drift")
        task = self.task("generic.candidate-drift", source, candidate)
        (candidate / "app.py").write_text(
            "def answer():\n    return 0\n",
            encoding="utf-8",
        )
        backend = _ScriptedBackend([])

        outcome = run_prepared_task(
            task,
            run_root,
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
        )

        reason = (
            "candidate bytes do not match the prepared ingress fingerprints "
            "before inference"
        )
        self.assertEqual(outcome.status, "verification_failed")
        self.assertFalse(outcome.verified)
        self.assertEqual(outcome.failure_reason, reason)
        self.assertEqual(outcome.inference.calls, 0)
        self.assertEqual(backend.requests, [])
        rejected = [
            event
            for event in _events(outcome.run_root)
            if event["kind"] == "workspace.admission_rejected"
        ]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["payload"]["reason"], reason)
        self.assertTrue(rejected[0]["payload"]["source_matches"])
        self.assertFalse(rejected[0]["payload"]["candidate_matches"])

    def test_passing_callback_with_empty_changes_cannot_promote(self) -> None:
        run_root, source, candidate = self.make_workspaces("empty-callback-changes")
        finish = ActionEnvelope(
            rationale="Ask for acceptance after the public check passes.",
            action=FinishAction(summary="The repair is complete."),
        ).as_json()
        backend = _ScriptedBackend([self.write_action(candidate), finish])

        outcome = run_prepared_task(
            self.task(
                "generic.empty-changes",
                source,
                candidate,
                force_empty_changes=True,
            ),
            run_root,
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
        )

        self.assertFalse(outcome.verified)
        self.assertFalse(outcome.delivery.verified)
        self.assertEqual(outcome.verification.status, "policy_failed")
        self.assertFalse(outcome.verification.ok)
        manifest = json.loads(
            Path(outcome.delivery.manifest_path).read_text(encoding="utf-8")
        )
        self.assertFalse(manifest["verified"])
        self.assertFalse(
            [
                event
                for event in _events(outcome.run_root)
                if event["kind"] == "run.finished"
            ][0]["payload"]["verified"]
        )

    def test_reserved_operator_outputs_are_rejected_but_caller_input_is_allowed(
        self,
    ) -> None:
        for index, reserved in enumerate(
            ("artifacts", "trace.jsonl", "trace.seal.json", "delivery")
        ):
            with self.subTest(reserved=reserved):
                run_root, source, candidate = self.make_workspaces(
                    f"reserved-{index}"
                )
                collision = run_root / reserved
                if reserved in {"artifacts", "delivery"}:
                    collision.mkdir()
                else:
                    collision.write_text("reserved", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "reserved operator outputs"):
                    run_prepared_task(
                        self.task(f"generic.reserved-{index}", source, candidate),
                        run_root,
                        _ScriptedBackend([]),
                        model="scripted-local-model",
                        config=OperatorConfig(max_inference_calls=2, max_actions=2),
                    )

    def test_legacy_capsule_adapter_delegates_to_shared_prepared_loop(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        manifest = load_capsule(
            repository
            / "tests"
            / "fixtures"
            / "capsules"
            / "production-config-recovery"
        )
        backend = _ScriptedBackend([RuntimeError("stop after adapter delegation")])

        with patch(
            "forge8.operator.run_prepared_task",
            wraps=run_prepared_task,
        ) as shared_loop:
            outcome = run_capsule(
                manifest,
                self.root / "legacy-adapter",
                backend,
                model="scripted-local-model",
                config=OperatorConfig(max_inference_calls=2, max_actions=2),
            )

        shared_loop.assert_called_once()
        adapted_task = shared_loop.call_args.args[0]
        self.assertIsInstance(adapted_task, PreparedWorkspaceTask)
        self.assertEqual(adapted_task.task_id, manifest.capsule_id)
        self.assertEqual(outcome.capsule_id, manifest.capsule_id)


if __name__ == "__main__":
    unittest.main()
