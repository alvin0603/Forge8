from __future__ import annotations

import hashlib
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from typing import Any
from unittest.mock import patch

import forge8.checks as checks_module
from forge8.actions import ActionEnvelope, WriteTextAction
from forge8.inference import ChatResponse
from forge8.operator import OperatorConfig, run_prepared_task
from forge8.repair import (
    MAX_REPAIR_GOAL_CHARS,
    RepositoryRepairError,
    prepare_repository_repair,
)
from forge8.repository import prepare_repository_snapshot
from forge8.workspace import ArtifactStore, WorkspacePolicy


_BROKEN_APP = "def answer():\n    return 1\n"
_FIXED_APP = "def answer():\n    return 2\n"
_WRONG_APP = "def answer():\n    return 3\n"
_APP_TEST = '''from pathlib import Path
import unittest

from app import answer


class AnswerTests(unittest.TestCase):
    def test_answer(self):
        marker = Path("check-marker.txt")
        marker.write_text("the check ran in this cwd", encoding="utf-8")
        try:
            self.assertEqual(answer(), 2)
        finally:
            marker.unlink(missing_ok=True)
'''
_MUTATING_TEST = '''from pathlib import Path
import unittest


class MutationTests(unittest.TestCase):
    def test_mutation(self):
        Path("persistent-check-marker.txt").write_text("mutated", encoding="utf-8")
        self.assertTrue(True)
'''


def _has_exact_pytest_runtime() -> bool:
    try:
        return metadata.version("pytest") == "9.1.1"
    except metadata.PackageNotFoundError:
        return False


def _response(content: str, call: int) -> ChatResponse:
    return ChatResponse(
        content=content,
        finish_reason="stop",
        usage={"prompt_tokens": 40 + call, "completion_tokens": 10 + call},
        timings={"predicted_ms": 10.0},
    )


class _ScriptedBackend:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.requests: list[Any] = []

    def chat(self, request: Any) -> ChatResponse:
        self.requests.append(request)
        if not self.outputs:
            raise AssertionError("operator requested an unscripted inference call")
        return _response(self.outputs.pop(0), len(self.requests))


def _fingerprints(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


class RepositoryRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-repair-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_repository(
        self,
        name: str,
        *,
        app: str = _BROKEN_APP,
        test_text: str = _APP_TEST,
    ) -> Path:
        repository = self.root / name / "repository"
        (repository / "tests").mkdir(parents=True)
        (repository / "app.py").write_text(app, encoding="utf-8")
        if test_text:
            (repository / "tests" / "test_app.py").write_text(
                test_text, encoding="utf-8"
            )
        (repository / "README.md").write_text("# tiny project\n", encoding="utf-8")
        return repository

    def prepare(
        self,
        name: str,
        *,
        app: str = _BROKEN_APP,
        test_text: str = _APP_TEST,
        allowed: tuple[str, ...] = ("app.py",),
    ):
        repository = self.make_repository(name, app=app, test_text=test_text)
        prepared = prepare_repository_repair(
            repository,
            self.root / name / "run",
            "Make app.answer return the value required by the repository tests.",
            allowed,
            ("python_unittest",),
            f"generic.{name}",
            check_timeout_seconds=20,
            check_output_bytes=64 * 1024,
        )
        return repository, prepared

    @staticmethod
    def repair_action(candidate: Path) -> str:
        guard = hashlib.sha256((candidate / "app.py").read_bytes()).hexdigest()
        return ActionEnvelope(
            rationale="Apply the smallest guarded edit supported by the failing test.",
            action=WriteTextAction(
                path="app.py",
                expected_sha256=guard,
                content=_FIXED_APP,
            ),
        ).as_json()

    def test_preparation_builds_two_equal_sanitized_snapshots(self) -> None:
        repository, prepared = self.prepare("prepared")

        self.assertEqual(prepared.run_root, (self.root / "prepared" / "run").resolve())
        self.assertEqual(
            prepared.task.source_workspace,
            (prepared.run_root / "input" / "source").resolve(),
        )
        self.assertEqual(
            prepared.task.candidate_workspace,
            (prepared.run_root / "input" / "workspace").resolve(),
        )
        self.assertEqual(prepared.allowed_write_roots, ("app.py",))
        self.assertEqual(prepared.task.allowed_write_roots, ("app.py",))
        self.assertEqual(prepared.task.selected_check_ids, ("python_unittest",))
        self.assertTrue(prepared.task.require_failing_baseline)
        self.assertTrue(prepared.task.require_nonempty_changes)
        self.assertEqual(prepared.task.policy_metadata["isolation"], "process_only")
        self.assertFalse(
            prepared.task.policy_metadata["network_isolation_enforced"]
        )
        self.assertEqual(
            prepared.task.source_fingerprints,
            {item.path: item.sha256 for item in prepared.ingress.fingerprints},
        )
        self.assertEqual(
            _fingerprints(prepared.task.source_workspace),
            _fingerprints(prepared.task.candidate_workspace),
        )
        self.assertEqual(Path(prepared.ingress.source_root), repository.resolve())

    def test_server_free_baseline_requires_a_real_failure_and_is_non_mutating(self) -> None:
        repository, prepared = self.prepare("baseline")
        original = _fingerprints(repository)

        baseline = prepared.baseline_preflight()

        self.assertTrue(baseline.repairable, baseline.reason)
        self.assertEqual(baseline.status, "repairable")
        self.assertEqual([check.status for check in baseline.checks], ["failed"])
        self.assertEqual(_fingerprints(repository), original)
        self.assertEqual(_fingerprints(prepared.task.source_workspace), original)
        self.assertEqual(_fingerprints(prepared.task.candidate_workspace), original)
        self.assertFalse((repository / "check-marker.txt").exists())
        self.assertFalse(
            (prepared.task.candidate_workspace / "check-marker.txt").exists()
        )

    def test_real_generic_operator_vertical_delivers_verified_nonempty_patch(self) -> None:
        repository, prepared = self.prepare("vertical")
        source_before = _fingerprints(repository)
        tests_before = (repository / "tests" / "test_app.py").read_bytes()
        backend = _ScriptedBackend(
            [self.repair_action(prepared.task.candidate_workspace)]
        )

        outcome = run_prepared_task(
            prepared.task,
            prepared.run_root,
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
        )

        self.assertTrue(outcome.verified, outcome.failure_reason)
        self.assertEqual(outcome.status, "verified")
        self.assertEqual(outcome.verification.status, "passed")
        self.assertEqual(outcome.verification.changes.modified, ("app.py",))
        self.assertEqual(
            outcome.verification.candidate_fingerprints,
            _fingerprints(prepared.task.candidate_workspace),
        )
        self.assertEqual([change.path for change in outcome.delivery.changes], ["app.py"])
        self.assertEqual((repository / "app.py").read_text(encoding="utf-8"), _BROKEN_APP)
        self.assertEqual(_fingerprints(repository), source_before)
        self.assertEqual(
            (repository / "tests" / "test_app.py").read_bytes(), tests_before
        )
        self.assertEqual(
            (prepared.task.source_workspace / "app.py").read_text(encoding="utf-8"),
            _BROKEN_APP,
        )
        self.assertEqual(
            (prepared.task.candidate_workspace / "app.py").read_text(encoding="utf-8"),
            _FIXED_APP,
        )
        self.assertFalse(
            (prepared.task.candidate_workspace / "check-marker.txt").exists()
        )

    def test_gitignore_is_fingerprinted_but_model_invisible_and_unchanged(self) -> None:
        repository = self.make_repository("gitignore-vertical")
        gitignore = repository / ".gitignore"
        gitignore_content = b"__pycache__/\n*.pyc\n"
        gitignore.write_bytes(gitignore_content)
        source_before = _fingerprints(repository)
        prepared = prepare_repository_repair(
            repository,
            self.root / "gitignore-vertical" / "run",
            "Make app.answer return the value required by the repository tests.",
            ("app.py",),
            ("python_unittest",),
            "generic.control-file-vertical",
            check_timeout_seconds=20,
            check_output_bytes=64 * 1024,
        )
        hidden_probe = (
            '{"rationale":"Check whether a conventional hidden control file is '
            'readable.","action":{"kind":"read_text","path":".gitignore",'
            '"start_line":1,"line_count":20}}'
        )
        backend = _ScriptedBackend(
            [hidden_probe, self.repair_action(prepared.task.candidate_workspace)]
        )

        outcome = run_prepared_task(
            prepared.task,
            prepared.run_root,
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=3, max_actions=3),
        )

        self.assertTrue(outcome.verified, outcome.failure_reason)
        self.assertEqual(outcome.actions, 2)
        self.assertIn(".gitignore", prepared.task.source_fingerprints)
        self.assertEqual(outcome.verification.changes.modified, ("app.py",))
        self.assertEqual([change.path for change in outcome.delivery.changes], ["app.py"])
        self.assertTrue(backend.requests)
        for request in backend.requests:
            self.assertNotIn(".gitignore", request.messages[1].content)
        self.assertIn("Hidden paths", backend.requests[1].messages[1].content)
        self.assertEqual(gitignore.read_bytes(), gitignore_content)
        self.assertEqual(
            (prepared.task.source_workspace / ".gitignore").read_bytes(),
            gitignore_content,
        )
        self.assertEqual(
            (prepared.task.candidate_workspace / ".gitignore").read_bytes(),
            gitignore_content,
        )
        self.assertEqual(_fingerprints(repository), source_before)

    def test_already_passing_preflight_and_operator_make_no_model_calls(self) -> None:
        _, prepared = self.prepare("already-passing", app=_FIXED_APP)
        baseline = prepared.baseline_preflight()
        backend = _ScriptedBackend([])

        outcome = run_prepared_task(
            prepared.task,
            prepared.run_root,
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
        )

        self.assertEqual(baseline.status, "already_passing")
        self.assertFalse(baseline.repairable)
        self.assertEqual([check.status for check in baseline.checks], ["passed"])
        self.assertFalse(outcome.verified)
        self.assertEqual(outcome.inference.calls, 0)
        self.assertEqual(backend.requests, [])
        self.assertEqual(outcome.failure_reason, "selected checks already pass at baseline")

    def test_goal_task_check_and_limit_inputs_are_strict(self) -> None:
        repository = self.make_repository("validation")
        (repository / "conftest.py").write_text("# pytest control file\n", encoding="utf-8")
        cases = (
            ({"goal": "   "}, "goal"),
            ({"goal": "bad\x00goal"}, "goal"),
            ({"goal": "x" * (MAX_REPAIR_GOAL_CHARS + 1)}, "goal"),
            ({"goal": "\ud800"}, "UTF-8"),
            ({"task_id": " bad "}, "task_id"),
            ({"task_id": "\ud800"}, "UTF-8"),
            ({"check_ids": ()}, "check"),
            ({"check_ids": ("unknown",)}, "unknown check"),
            ({"check_ids": ("python_unittest", "python_unittest")}, "duplicates"),
            ({"check_ids": (["not-hashable"],)}, "check IDs"),
            ({"check_timeout_seconds": 0}, "timeout"),
            ({"check_timeout_seconds": float("nan")}, "timeout"),
            ({"check_timeout_seconds": float("inf")}, "timeout"),
            ({"check_output_bytes": 0}, "output"),
            (
                {
                    "allowed_write_paths": ("conftest.py",),
                    "check_ids": ("python_pytest",),
                },
                r"conftest\.py.*writ",
            ),
        )
        defaults: dict[str, Any] = {
            "goal": "repair it",
            "allowed_write_paths": ("app.py",),
            "check_ids": ("python_unittest",),
            "task_id": "generic.validation",
            "check_timeout_seconds": 20,
            "check_output_bytes": 4096,
        }
        for index, (override, message) in enumerate(cases):
            with self.subTest(override=override):
                arguments = defaults | override
                with self.assertRaisesRegex(RepositoryRepairError, message):
                    prepare_repository_repair(
                        repository,
                        self.root / "validation" / f"run-{index}",
                        **arguments,
                    )

    def test_run_root_must_not_overlap_original_repository_either_way(self) -> None:
        repository = self.make_repository("overlap-inside")
        with self.assertRaisesRegex(RepositoryRepairError, "must not overlap"):
            prepare_repository_repair(
                repository,
                repository / "run",
                "repair it",
                ("app.py",),
                ("python_unittest",),
                "generic.overlap-inside",
            )

        outer = self.root / "overlap-outside" / "run"
        nested_repository = outer / "repository"
        (nested_repository / "tests").mkdir(parents=True)
        (nested_repository / "app.py").write_text(_BROKEN_APP, encoding="utf-8")
        (nested_repository / "tests" / "test_app.py").write_text(
            _APP_TEST, encoding="utf-8"
        )
        with self.assertRaisesRegex(RepositoryRepairError, "must not overlap"):
            prepare_repository_repair(
                nested_repository,
                outer,
                "repair it",
                ("app.py",),
                ("python_unittest",),
                "generic.overlap-outside",
            )

    def test_verifier_rejects_late_live_source_mutation(self) -> None:
        repository, prepared = self.prepare("live-mutation")
        (repository / "app.py").write_text("def answer():\n    return 9\n", encoding="utf-8")

        result = prepared.task.verify(prepared.task.candidate_workspace)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "policy_failed")
        self.assertEqual(result.verifier_summary["stage"], "live_source_before")

    def test_verifier_rejects_immutable_source_mutation(self) -> None:
        _, prepared = self.prepare("snapshot-mutation")
        (prepared.task.source_workspace / "app.py").write_text(
            "def answer():\n    return 9\n", encoding="utf-8"
        )

        result = prepared.task.verify(prepared.task.candidate_workspace)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "policy_failed")
        self.assertEqual(result.verifier_summary["stage"], "immutable_source_before")

    def test_verifier_rejects_changes_outside_canonical_allow_roots(self) -> None:
        _, prepared = self.prepare("outside-allow")
        (prepared.task.candidate_workspace / "README.md").write_text(
            "changed outside allowlist\n", encoding="utf-8"
        )

        result = prepared.task.verify(prepared.task.candidate_workspace)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "policy_failed")
        self.assertEqual(result.verifier_summary["stage"], "candidate_diff")
        self.assertIn("README.md", result.stderr)

    def test_verifier_rejects_candidate_excluded_and_link_mutations(self) -> None:
        for name, mutation in (
            (
                "excluded",
                lambda root: (
                    (root / "__pycache__").mkdir(),
                    (root / "__pycache__" / "app.pyc").write_bytes(b"cache"),
                ),
            ),
            (
                "link",
                lambda root: (root / "alias.py").symlink_to(root / "app.py"),
            ),
        ):
            with self.subTest(mutation=name):
                _, prepared = self.prepare(f"candidate-{name}")
                try:
                    mutation(prepared.task.candidate_workspace)
                except (OSError, NotImplementedError) as exc:
                    if name == "link":
                        self.skipTest(f"symlinks unavailable: {exc}")
                    raise

                result = prepared.task.verify(prepared.task.candidate_workspace)

                self.assertFalse(result.ok)
                self.assertEqual(result.status, "policy_failed")
                self.assertIn(
                    result.verifier_summary["stage"],
                    {"candidate_sanitation", "verification_error"},
                )

    def test_check_failure_is_bounded_and_keeps_candidate_fingerprint_evidence(self) -> None:
        _, prepared = self.prepare("check-failure")
        candidate = prepared.task.candidate_workspace
        (candidate / "app.py").write_text(_WRONG_APP, encoding="utf-8")

        result = prepared.task.verify(candidate)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.verifier_summary["stage"], "checks")
        self.assertEqual(result.candidate_fingerprints, _fingerprints(candidate))
        self.assertEqual(result.verifier_summary["checks"][0]["status"], "failed")
        self.assertLessEqual(len(result.stdout), 8_000)
        self.assertLessEqual(len(result.stderr), 8_000)

    def test_guarded_replay_and_final_fingerprint_are_exact_and_attempts_isolated(self) -> None:
        _, prepared = self.prepare("replay-evidence")
        candidate = prepared.task.candidate_workspace
        (candidate / "app.py").write_text(_FIXED_APP, encoding="utf-8")

        first = prepared.task.verify(candidate)
        second = prepared.task.verify(candidate)

        self.assertTrue(first.ok, first.stderr)
        self.assertTrue(second.ok, second.stderr)
        self.assertEqual(first.changes.modified, ("app.py",))
        self.assertEqual(first.candidate_fingerprints, _fingerprints(candidate))
        attempts = sorted(
            path.name
            for path in (prepared.run_root / "input" / "verification-artifacts").iterdir()
            if path.is_dir()
        )
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(set(attempts)), 2)

        with patch("forge8.repair._replay_changes", return_value=None):
            replay_failure = prepared.task.verify(candidate)
        self.assertFalse(replay_failure.ok)
        self.assertEqual(replay_failure.verifier_summary["stage"], "guarded_replay")

    def test_check_executor_returns_unavailable_instead_of_raising_on_bad_candidate(self) -> None:
        _, prepared = self.prepare("executor-sanitize")
        candidate = prepared.task.candidate_workspace
        try:
            (candidate / "alias.py").symlink_to(candidate / "app.py")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        artifacts = ArtifactStore(self.root / "executor-artifacts")
        factory = prepared.task.check_executor_factory
        self.assertIsNotNone(factory)
        executor = factory(WorkspacePolicy(candidate, allow_write=True), artifacts)

        result = executor.run("python_unittest")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("sanitation failed", result.error)

    def test_mutating_and_zero_test_checks_fail_closed(self) -> None:
        scenarios = (
            ("mutating", _MUTATING_TEST, "mutated"),
            ("zero", "", "zero tests"),
        )
        for name, test_text, expected in scenarios:
            with self.subTest(scenario=name):
                repository = self.make_repository(
                    f"infrastructure-{name}", test_text=test_text
                )
                prepared = prepare_repository_repair(
                    repository,
                    self.root / f"infrastructure-{name}" / "run",
                    "repair it",
                    ("app.py",),
                    ("python_unittest",),
                    f"generic.infrastructure-{name}",
                    check_timeout_seconds=20,
                    check_output_bytes=64 * 1024,
                )

                baseline = prepared.baseline_preflight()

                self.assertEqual(baseline.status, "invalid")
                self.assertFalse(baseline.repairable)
                self.assertEqual(baseline.checks[0].status, "unavailable")
                self.assertIn(expected, baseline.checks[0].error)
                self.assertFalse(
                    (prepared.task.candidate_workspace / "persistent-check-marker.txt").exists()
                )

    def test_unittest_bootstrap_precedes_same_named_workspace_path(self) -> None:
        repository = self.make_repository("unittest-shadow")
        (repository / "unittest.py").write_text(
            "raise SystemExit('workspace unittest imported')\n",
            encoding="utf-8",
        )
        prepared = prepare_repository_repair(
            repository,
            self.root / "unittest-shadow" / "run",
            "repair it",
            ("app.py",),
            ("python_unittest",),
            "generic.unittest-shadow",
            check_timeout_seconds=20,
            check_output_bytes=64 * 1024,
        )

        baseline = prepared.baseline_preflight()

        self.assertEqual(baseline.status, "repairable")
        self.assertTrue(baseline.repairable)
        self.assertEqual(baseline.checks[0].status, "failed")
        self.assertEqual(baseline.checks[0].return_code, 71)

    def test_unittest_nonexecution_outcomes_cannot_verify_a_candidate(self) -> None:
        plain_test = '''import unittest

from app import answer


class AnswerTests(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(answer(), 2)
'''
        expected_failure_test = '''import unittest

from app import answer


class AnswerTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_answer(self):
        self.assertEqual(answer(), 2)
'''
        scenarios = (
            (
                "expected-failure",
                _FIXED_APP,
                expected_failure_test,
                _BROKEN_APP,
                "invalid",
                79,
                "expected",
            ),
            (
                "stderr-spoof",
                _BROKEN_APP,
                plain_test,
                "import os\n\ndef answer():\n"
                "    os.write(2, b'\\n' + b'-' * 70 + "
                "b'\\nRan 1 test in 0.001s\\n\\nOK\\n')\n"
                "    os._exit(0)\n",
                "repairable",
                0,
                "attested",
            ),
            (
                "skip-atexit-spoof",
                _BROKEN_APP,
                plain_test,
                "import atexit\nimport os\nimport unittest\n\n"
                "def _forge_summary():\n"
                "    os.write(2, b'\\n' + b'-' * 70 + "
                "b'\\nRan 1 test in 0.001s\\n\\nOK\\n')\n\n"
                "atexit.register(_forge_summary)\n\n"
                "def answer():\n"
                "    raise unittest.SkipTest('candidate skipped execution')\n",
                "repairable",
                79,
                "skipped",
            ),
        )
        for (
            name,
            app,
            test_text,
            candidate_text,
            baseline_status,
            return_code,
            evidence,
        ) in scenarios:
            with self.subTest(outcome=name):
                _, prepared = self.prepare(
                    f"unittest-{name}",
                    app=app,
                    test_text=test_text,
                )

                baseline = prepared.baseline_preflight()
                (prepared.task.candidate_workspace / "app.py").write_text(
                    candidate_text,
                    encoding="utf-8",
                )
                result = prepared.task.verify(prepared.task.candidate_workspace)

                self.assertEqual(baseline.status, baseline_status)
                self.assertFalse(result.ok)
                self.assertEqual(result.verifier_summary["stage"], "checks")
                self.assertEqual(
                    result.verifier_summary["checks"][0]["status"],
                    "unavailable",
                )
                self.assertEqual(
                    result.verifier_summary["checks"][0]["return_code"],
                    return_code,
                )
                self.assertIn(
                    evidence,
                    result.verifier_summary["checks"][0]["error"].casefold(),
                )
                if "spoof" in name:
                    self.assertIn(
                        "Ran 1 test in 0.001s",
                        result.verifier_summary["checks"][0]["stderr_preview"],
                    )

    @unittest.skipUnless(
        _has_exact_pytest_runtime(),
        "requires the exact pytest optional runtime",
    )
    def test_pytest_bootstrap_precedes_same_named_workspace_path(self) -> None:
        for name, is_directory in (("pytest.py", False), ("pytest", True)):
            with self.subTest(name=name):
                repository = self.make_repository(f"pytest-shadow-{name}")
                shadow = repository / name
                if is_directory:
                    shadow.mkdir()
                else:
                    shadow.write_text("raise SystemExit(0)\n", encoding="utf-8")
                prepared = prepare_repository_repair(
                    repository,
                    self.root / f"pytest-shadow-{name}" / "run",
                    "repair it",
                    ("app.py",),
                    ("python_pytest",),
                    f"generic.pytest-shadow-{is_directory}",
                    check_timeout_seconds=20,
                    check_output_bytes=64 * 1024,
                )

                baseline = prepared.baseline_preflight()

                self.assertEqual(baseline.status, "repairable")
                self.assertTrue(baseline.repairable)
                self.assertEqual(baseline.checks[0].status, "failed")
                self.assertEqual(baseline.checks[0].return_code, 81)
                self.assertFalse(prepared.task.policy_metadata["stdlib_only"])

    @unittest.skipUnless(
        _has_exact_pytest_runtime(),
        "requires the exact pytest optional runtime",
    )
    def test_candidate_pytest_config_cannot_hide_a_failing_test(self) -> None:
        repository = self.make_repository(
            "pytest-config-control",
            test_text="def test_failure():\n    assert False\n",
        )
        (repository / "tests" / "test_pass.py").write_text(
            "def test_pass():\n    assert True\n",
            encoding="utf-8",
        )
        (repository / "pytest.ini").write_text(
            "[pytest]\naddopts = --collect-only\n",
            encoding="utf-8",
        )
        prepared = prepare_repository_repair(
            repository,
            self.root / "pytest-config-control" / "run",
            "Repair the failing behavior without changing test selection.",
            ("pytest.ini",),
            ("python_pytest",),
            "generic.pytest-config-control",
            check_timeout_seconds=20,
            check_output_bytes=64 * 1024,
        )

        initial = (prepared.task.candidate_workspace / "pytest.ini").read_bytes()
        candidate_config = "[pytest]\npython_files = test_pass.py\n"
        backend = _ScriptedBackend(
            [
                ActionEnvelope(
                    rationale="Attempt to hide the failing test through collection config.",
                    action=WriteTextAction(
                        path="pytest.ini",
                        expected_sha256=hashlib.sha256(initial).hexdigest(),
                        content=candidate_config,
                    ),
                ).as_json()
            ]
        )
        with patch.dict(
            checks_module.os.environ,
            {"PYTEST_ADDOPTS": "--invalid-ambient-option"},
        ):
            baseline = prepared.baseline_preflight()
            outcome = run_prepared_task(
                prepared.task,
                prepared.run_root,
                backend,
                model="scripted-local-model",
                config=OperatorConfig(max_inference_calls=1, max_actions=1),
            )

        self.assertEqual(baseline.status, "repairable")
        self.assertEqual(baseline.checks[0].status, "failed")
        self.assertEqual(baseline.checks[0].return_code, 81)
        self.assertFalse(outcome.verified)
        self.assertFalse(outcome.delivery.verified)
        self.assertEqual(outcome.verification.status, "failed")
        self.assertEqual(outcome.verification.verifier_summary["stage"], "checks")
        self.assertEqual(
            outcome.verification.verifier_summary["checks"][0]["status"],
            "failed",
        )
        self.assertEqual(
            outcome.verification.verifier_summary["checks"][0]["return_code"],
            81,
        )
        self.assertEqual(
            [change.path for change in outcome.delivery.changes],
            ["pytest.ini"],
        )

    def test_clean_snapshot_evidence_matches_repository_boundary(self) -> None:
        _, prepared = self.prepare("snapshot-evidence")
        candidate = prepared.task.candidate_workspace
        (candidate / "app.py").write_text(_FIXED_APP, encoding="utf-8")
        evidence_root = self.root / "captured-candidate"

        snapshot = prepare_repository_snapshot(candidate, evidence_root)
        result = prepared.task.verify(candidate)

        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(
            result.candidate_fingerprints,
            {item.path: item.sha256 for item in snapshot.fingerprints},
        )


if __name__ == "__main__":
    unittest.main()
