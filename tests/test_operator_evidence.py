from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from forge8.actions import (
    ActionEnvelope,
    ReplaceTextAction,
    WriteTextAction,
)
from forge8.capsule import load_capsule
from forge8.inference import ChatResponse
from forge8.operator import OperatorConfig, run_capsule


_OBSERVATION_HEADER = "BOUNDED OBSERVATION LEDGER (oldest retained first)\n"
_OBSERVATION_FOOTER = "\n\nChoose the next smallest action."


def _response(content: str, call: int) -> ChatResponse:
    return ChatResponse(
        content=content,
        finish_reason="stop",
        usage={"prompt_tokens": 100 + call, "completion_tokens": 20 + call},
        timings={"predicted_ms": 250.0 + call},
    )


class _ScriptedBackend:
    def __init__(self, outputs: list[str | Exception]) -> None:
        self.outputs = outputs
        self.requests: list[Any] = []

    def chat(self, request: Any) -> ChatResponse:
        self.requests.append(request)
        if not self.outputs:
            raise AssertionError("operator requested more scripted inference calls")
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return _response(item, len(self.requests))


def _request_observations(request: Any) -> list[dict[str, Any]]:
    content = request.messages[1].content
    if _OBSERVATION_HEADER not in content or _OBSERVATION_FOOTER not in content:
        raise AssertionError("model request is missing its bounded observation ledger")
    rendered = content.split(_OBSERVATION_HEADER, 1)[1].split(
        _OBSERVATION_FOOTER, 1
    )[0]
    observations = [json.loads(line) for line in rendered.splitlines() if line]
    if not all(isinstance(item, dict) for item in observations):
        raise AssertionError("observation ledger contains a non-object entry")
    return observations


def _only_observation(request: Any, kind: str) -> dict[str, Any]:
    matches = [
        observation
        for observation in _request_observations(request)
        if observation.get("kind") == kind
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one {kind!r} observation, found {len(matches)}"
        )
    return matches[0]


def _trace_events(run_root: str) -> list[dict[str, Any]]:
    trace_path = Path(run_root) / "trace.jsonl"
    return [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]


class OperatorEvidenceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repository = Path(__file__).resolve().parents[1]
        cls.production_capsule = (
            repository / "tests" / "fixtures" / "capsules" / "production-config-recovery"
        )
        cls.jsonl_capsule = (
            repository / "tests" / "fixtures" / "capsules" / "jsonl-checkpoint-recovery"
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="forge8-operator-evidence-test-"
        )
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_first_request_contains_baseline_acceptance_failure_summary(self) -> None:
        manifest = load_capsule(self.production_capsule)
        backend = _ScriptedBackend([RuntimeError("stop after first request")])

        outcome = run_capsule(
            manifest,
            self.root / "baseline-acceptance",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
        )

        self.assertEqual(len(backend.requests), 1)
        observation = _only_observation(
            backend.requests[0], "baseline_acceptance"
        )
        evidence = json.loads(observation["body"])
        self.assertFalse(evidence["ok"])
        self.assertEqual(evidence["status"], "failed")
        summary = evidence["summary"]
        self.assertEqual(
            summary["capsule"], "repository.production-config-recovery"
        )
        self.assertFalse(summary["passed"])
        self.assertGreater(summary["failures"] + summary["errors"], 0)
        self.assertEqual(summary, outcome.verification.verifier_summary)
        self.assertNotIn(
            "stdout",
            evidence,
            "parsed verifier stdout must not be echoed beside its summary",
        )

    def test_three_semantically_identical_stale_writes_stall_without_fourth_call(
        self,
    ) -> None:
        manifest = load_capsule(self.production_capsule)
        production = manifest.workspace_path / "config" / "production.ini"
        actual_sha = hashlib.sha256(production.read_bytes()).hexdigest()
        stale_sha = "0" * 64
        attempts = [
            ActionEnvelope(
                rationale=f"Try distinct repair proposal {index} with the stale guard.",
                action=WriteTextAction(
                    path="config/production.ini",
                    expected_sha256=stale_sha,
                    content=f"distinct generated payload {index}\n",
                ),
            ).as_json()
            for index in range(1, 4)
        ]
        backend = _ScriptedBackend(attempts)

        outcome = run_capsule(
            manifest,
            self.root / "semantic-stale-write-stall",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=4, max_actions=4),
        )

        self.assertEqual(outcome.status, "stalled")
        self.assertEqual(outcome.actions, 3)
        self.assertEqual(outcome.inference.calls, 3)
        self.assertEqual(len(backend.requests), 3)
        self.assertIn("normalized effect failure", outcome.failure_reason or "")
        stalled = [
            event
            for event in _trace_events(outcome.run_root)
            if event["kind"] == "run.stalled"
        ]
        self.assertEqual(len(stalled), 1)
        self.assertEqual(
            stalled[0]["payload"],
            {
                "reason": "same normalized effect failure repeated 3 times",
                "count": 3,
                "tool": "write_text",
                "path": "config/production.ini",
                "error": "Write guard does not match the current file",
                "actual_current_sha256": actual_sha,
            },
        )

    def test_same_byte_effect_failure_survives_reads_and_stalls(self) -> None:
        manifest = load_capsule(self.production_capsule)
        production = manifest.workspace_path / "config" / "production.ini"
        original = production.read_text(encoding="utf-8")
        guard = hashlib.sha256(production.read_bytes()).hexdigest()
        identical_write = ActionEnvelope(
            rationale="Propose a byte-identical guarded effect.",
            action=WriteTextAction(
                path="config/production.ini",
                expected_sha256=guard,
                content=original,
            ),
        ).as_json()
        reread = json.dumps(
            {
                "rationale": "Re-read the same unchanged file.",
                "action": {
                    "kind": "read_text",
                    "path": "config/production.ini",
                    "start_line": 1,
                    "line_count": 40,
                },
            },
            separators=(",", ":"),
        )
        backend = _ScriptedBackend(
            [
                identical_write,
                reread,
                identical_write,
                reread,
                identical_write,
                RuntimeError("must stall before this call"),
            ]
        )

        with patch(
            "forge8.operator.discover_checks",
            return_value=("python_unittest", "python_pytest"),
        ):
            outcome = run_capsule(
                manifest,
                self.root / "same-byte-read-write-stall",
                backend,
                model="scripted-local-model",
                config=OperatorConfig(max_inference_calls=6, max_actions=6),
            )

        self.assertEqual(outcome.status, "stalled")
        self.assertEqual(outcome.actions, 5)
        self.assertEqual(outcome.inference.calls, 5)
        self.assertEqual(len(backend.requests), 5)
        self.assertIn("normalized effect failure", outcome.failure_reason or "")
        candidate = Path(outcome.candidate_workspace) / "config" / "production.ini"
        self.assertEqual(candidate.read_text(encoding="utf-8"), original)
        stalled = [
            event
            for event in _trace_events(outcome.run_root)
            if event["kind"] == "run.stalled"
        ]
        self.assertEqual(len(stalled), 1)
        self.assertEqual(stalled[0]["payload"]["count"], 3)
        self.assertEqual(
            stalled[0]["payload"]["error"],
            "Write content is byte-identical to the current file",
        )
        check_events = [
            event
            for event in _trace_events(outcome.run_root)
            if event["kind"] == "check.completed"
        ]
        self.assertEqual(len(check_events), 1)

    def test_second_exact_read_warns_before_the_third_decision(self) -> None:
        manifest = load_capsule(self.production_capsule)
        repeated_read = json.dumps(
            {
                "rationale": "Inspect the same bounded file window.",
                "action": {
                    "kind": "read_text",
                    "path": "config/production.ini",
                    "start_line": 1,
                    "line_count": 40,
                },
            },
            separators=(",", ":"),
        )
        backend = _ScriptedBackend(
            [repeated_read, repeated_read, RuntimeError("stop after warning")]
        )

        outcome = run_capsule(
            manifest,
            self.root / "repeated-read-warning",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=3, max_actions=3),
        )

        self.assertEqual(outcome.status, "backend_error")
        self.assertEqual(len(backend.requests), 3)
        warning = _only_observation(backend.requests[2], "progress_warning")
        self.assertIn("next action must differ", warning["body"])

    def test_successful_effect_observation_omits_generated_content_and_artifact_handle(
        self,
    ) -> None:
        manifest = load_capsule(self.jsonl_capsule)
        follower = manifest.workspace_path / "eventspool" / "follower.py"
        guard = hashlib.sha256(follower.read_bytes()).hexdigest()
        sentinel = "GENERATED_EFFECT_SENTINEL_MUST_NOT_BE_ECHOED"
        proposal = ActionEnvelope(
            rationale="Make a harmless guarded edit so the next request sees tool evidence.",
            action=ReplaceTextAction(
                path="eventspool/follower.py",
                expected_sha256=guard,
                old_text='"""Checkpointed JSON Lines follower."""',
                new_text=(
                    '"""Checkpointed JSON Lines follower."""\n'
                    f"# {sentinel}"
                ),
            ),
        ).as_json()
        backend = _ScriptedBackend(
            [proposal, RuntimeError("stop after compact effect evidence")]
        )

        outcome = run_capsule(
            manifest,
            self.root / "compact-effect-observation",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=3, max_actions=3),
        )

        self.assertEqual(len(backend.requests), 2)
        second_prompt = backend.requests[1].messages[1].content
        self.assertNotIn(sentinel, second_prompt)
        observation = _only_observation(backend.requests[1], "tool_result")
        model_view = json.loads(observation["body"])
        self.assertEqual(set(model_view), {"ok", "metadata", "error"})
        self.assertTrue(model_view["ok"])
        self.assertIsNone(model_view["error"])
        self.assertNotIn("preview", model_view)
        self.assertNotIn("artifact", model_view)
        metadata = model_view["metadata"]
        self.assertIn("after_sha256", metadata)
        self.assertEqual(metadata["matches"], 1)
        for hidden in (
            "before_sha256",
            "old_text_sha256",
            "matched_preimage_sha256",
            "new_text_sha256",
        ):
            self.assertNotIn(hidden, metadata)

        trace = Path(outcome.run_root, "trace.jsonl").read_text(encoding="utf-8")
        self.assertIn(sentinel, trace)
        artifact_bytes = [
            path.read_bytes()
            for path in (Path(outcome.run_root) / "artifacts" / "objects").rglob("*")
            if path.is_file()
        ]
        self.assertTrue(any(sentinel.encode("utf-8") in item for item in artifact_bytes))

    def test_rollback_observation_supplies_restored_current_guard(self) -> None:
        manifest = load_capsule(self.jsonl_capsule)
        follower = manifest.workspace_path / "eventspool" / "follower.py"
        original = follower.read_text(encoding="utf-8")
        original_sha = hashlib.sha256(follower.read_bytes()).hexdigest()
        regressed_content = "this is not valid python !!!\n"
        regressed_sha = hashlib.sha256(regressed_content.encode("utf-8")).hexdigest()
        regression = ActionEnvelope(
            rationale="Make a deliberately regressing edit to exercise rollback evidence.",
            action=WriteTextAction(
                path="eventspool/follower.py",
                expected_sha256=original_sha,
                content=regressed_content,
            ),
        ).as_json()
        backend = _ScriptedBackend(
            [regression, RuntimeError("stop after receiving rollback evidence")]
        )

        outcome = run_capsule(
            manifest,
            self.root / "rollback-current-guard",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=3, max_actions=3),
        )

        self.assertEqual(len(backend.requests), 2)
        observation = _only_observation(
            backend.requests[1], "transaction_rollback"
        )
        self.assertIn(
            f"current expected_sha256 is '{original_sha}'", observation["body"]
        )
        self.assertNotIn(regressed_sha, observation["body"])
        candidate = Path(outcome.candidate_workspace) / "eventspool" / "follower.py"
        self.assertEqual(candidate.read_text(encoding="utf-8"), original)

        rollback_events = [
            event
            for event in _trace_events(outcome.run_root)
            if event["kind"] == "transaction.rolled_back"
        ]
        self.assertEqual(len(rollback_events), 1)
        self.assertEqual(
            rollback_events[0]["payload"]["restored_current_sha256"],
            original_sha,
        )

    def test_failed_tool_completed_trace_event_carries_error(self) -> None:
        manifest = load_capsule(self.production_capsule)
        production = manifest.workspace_path / "config" / "production.ini"
        actual_sha = hashlib.sha256(production.read_bytes()).hexdigest()
        stale_sha = "0" * 64
        stale_write = ActionEnvelope(
            rationale="Exercise the failed guarded-write evidence contract.",
            action=WriteTextAction(
                path="config/production.ini",
                expected_sha256=stale_sha,
                content=production.read_text(encoding="utf-8"),
            ),
        ).as_json()
        backend = _ScriptedBackend(
            [stale_write, RuntimeError("stop after failed tool result")]
        )

        outcome = run_capsule(
            manifest,
            self.root / "failed-tool-trace",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=3, max_actions=3),
        )

        failed_tool_events = [
            event
            for event in _trace_events(outcome.run_root)
            if event["kind"] == "tool.completed"
            and event["payload"].get("ok") is False
        ]
        self.assertEqual(len(failed_tool_events), 1)
        payload = failed_tool_events[0]["payload"]
        self.assertIn("error", payload)
        self.assertEqual(
            payload["error"], "Write guard does not match the current file"
        )
        self.assertEqual(payload["expected_sha256"], stale_sha)
        self.assertEqual(payload["actual_sha256"], actual_sha)

    def test_failed_replace_text_directs_the_next_decision_to_line_replacement(
        self,
    ) -> None:
        manifest = load_capsule(self.production_capsule)
        production = manifest.workspace_path / "config" / "production.ini"
        guard = hashlib.sha256(production.read_bytes()).hexdigest()
        missed_literal = ActionEnvelope(
            rationale="Exercise recovery after an exact literal is absent.",
            action=ReplaceTextAction(
                path="config/production.ini",
                expected_sha256=guard,
                old_text="this literal is not in the file",
                new_text="replacement",
            ),
        ).as_json()
        backend = _ScriptedBackend(
            [missed_literal, RuntimeError("stop after recovery guidance")]
        )

        run_capsule(
            manifest,
            self.root / "replace-text-recovery",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=3, max_actions=3),
        )

        self.assertEqual(len(backend.requests), 2)
        observation = _only_observation(backend.requests[1], "tool_result")
        model_view = json.loads(observation["body"])
        self.assertFalse(model_view["ok"])
        self.assertIn("replace_lines", model_view["recovery"])
        self.assertIn("Do not retry replace_text", model_view["recovery"])
        self.assertNotIn("replacement", observation["body"])

    def test_byte_identical_replace_text_does_not_claim_a_match_failure(self) -> None:
        manifest = load_capsule(self.production_capsule)
        production = manifest.workspace_path / "config" / "production.ini"
        original = production.read_text(encoding="utf-8")
        guard = hashlib.sha256(production.read_bytes()).hexdigest()
        no_op = ActionEnvelope(
            rationale="Exercise byte-identical replacement evidence.",
            action=ReplaceTextAction(
                path="config/production.ini",
                expected_sha256=guard,
                old_text=original,
                new_text=original,
            ),
        ).as_json()
        backend = _ScriptedBackend(
            [no_op, RuntimeError("stop after byte-identical evidence")]
        )

        run_capsule(
            manifest,
            self.root / "replace-text-no-op-evidence",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=3, max_actions=3),
        )

        observation = _only_observation(backend.requests[1], "tool_result")
        model_view = json.loads(observation["body"])
        self.assertFalse(model_view["ok"])
        self.assertIn("byte-identical", model_view["error"])
        self.assertNotIn("recovery", model_view)


if __name__ == "__main__":
    unittest.main()
