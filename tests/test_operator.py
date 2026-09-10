from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from forge8.actions import (
    ActionEnvelope,
    ReplaceLinesAction,
    ReplaceTextAction,
    WriteTextAction,
)
from forge8.capsule import load_capsule
from forge8.inference import ChatResponse
from forge8.operator import OperatorConfig, run_capsule


REPAIRED_PRODUCTION = """[runtime]
environment = production
workers = 6
shutdown_grace_seconds = 30

[delivery]
url = https://collector.internal.example/v2/events
request_timeout_seconds = 12
max_inflight = 256

[security]
verify_tls = true
token = ${STREAMGATE_TOKEN}

[storage]
spool_dir = /var/lib/streamgate/spool
"""


def response(content: str, call: int) -> ChatResponse:
    return ChatResponse(
        content=content,
        finish_reason="stop",
        usage={"prompt_tokens": 100 + call, "completion_tokens": 20 + call},
        timings={"predicted_ms": 250.0 + call},
    )


class ScriptedBackend:
    def __init__(self, outputs: list[str | Exception]) -> None:
        self.outputs = outputs
        self.requests = []

    def chat(self, request):
        self.requests.append(request)
        if not self.outputs:
            raise AssertionError("operator requested more scripted inference calls")
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return response(item, len(self.requests))


class OperatorVerticalSliceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repository = Path(__file__).resolve().parents[1]
        cls.capsule_root = (
            repository
            / "tests"
            / "fixtures"
            / "capsules"
            / "production-config-recovery"
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-operator-test-")
        self.root = Path(self.temporary.name)
        self.manifest = load_capsule(self.capsule_root)
        original = self.manifest.workspace_path / "config" / "production.ini"
        self.original_text = original.read_text(encoding="utf-8")
        self.original_sha = hashlib.sha256(original.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def read_action(self) -> str:
        return json.dumps(
            {
                "rationale": (
                    "Inspect the only allowed file before proposing a guarded repair."
                ),
                "action": {
                    "kind": "read_text",
                    "path": "config/production.ini",
                    "start_line": 1,
                    "line_count": 200,
                },
            },
            separators=(",", ":"),
        )

    def write_action(self, *, path: str = "config/production.ini", guard: str | None = None) -> str:
        return ActionEnvelope(
            rationale=(
                "Set grace to 30 (>12 and <=60), inflight to 256 (>=256 and <=384), "
                "enable TLS, preserve the environment token, and use the required absolute spool."
            ),
            action=WriteTextAction(
                path=path,
                expected_sha256=guard or self.original_sha,
                content=REPAIRED_PRODUCTION,
            ),
        ).as_json()

    def test_two_action_local_repair_produces_verified_delivery(self) -> None:
        backend = ScriptedBackend([self.read_action(), self.write_action()])

        outcome = run_capsule(
            self.manifest,
            self.root / "run",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=4, max_actions=4),
        )

        self.assertTrue(outcome.verified, outcome.verification.stderr)
        self.assertEqual(outcome.status, "verified")
        self.assertEqual(outcome.actions, 2)
        self.assertEqual(outcome.inference.calls, 2)
        self.assertTrue(all(check.ok for check in outcome.last_checks))
        self.assertEqual(
            (self.manifest.workspace_path / "config" / "production.ini").read_text(
                encoding="utf-8"
            ),
            self.original_text,
            "the source fixture must remain immutable",
        )
        candidate = Path(outcome.candidate_workspace) / "config" / "production.ini"
        self.assertEqual(candidate.read_text(encoding="utf-8"), REPAIRED_PRODUCTION)
        patch = Path(outcome.delivery.patch_path).read_text(encoding="utf-8")
        self.assertIn("-shutdown_grace_seconds = 10", patch)
        self.assertIn("+shutdown_grace_seconds = 30", patch)
        self.assertIn("+verify_tls = true", patch)
        handoff = Path(outcome.delivery.handoff_path).read_text(encoding="utf-8")
        self.assertIn("VERIFIED", handoff)
        manifest = json.loads(Path(outcome.delivery.manifest_path).read_text(encoding="utf-8"))
        self.assertTrue(manifest["verified"])
        self.assertEqual(manifest["run"]["model"], "scripted-local-model")

        request_schema = backend.requests[0].response_format["json_schema"]["schema"]
        self.assertEqual(list(request_schema["properties"]), ["rationale", "action"])

    def test_stale_failure_then_read_and_material_effect_can_verify(self) -> None:
        backend = ScriptedBackend(
            [
                self.write_action(guard="0" * 64),
                self.read_action(),
                self.write_action(),
            ]
        )

        outcome = run_capsule(
            self.manifest,
            self.root / "stale-read-material-recovery",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=4, max_actions=4),
        )

        self.assertTrue(outcome.verified, outcome.verification.stderr)
        self.assertEqual(outcome.status, "verified")
        self.assertEqual(outcome.actions, 3)
        candidate = Path(outcome.candidate_workspace) / "config" / "production.ini"
        self.assertEqual(candidate.read_text(encoding="utf-8"), REPAIRED_PRODUCTION)

    def test_guarded_exact_replacement_runs_checks_and_verifier(self) -> None:
        replacement = ActionEnvelope(
            rationale="Replace the uniquely observed overlay while preserving guarded state.",
            action=ReplaceTextAction(
                path="config/production.ini",
                expected_sha256=self.original_sha,
                old_text=self.original_text,
                new_text=REPAIRED_PRODUCTION,
            ),
        ).as_json()
        outcome = run_capsule(
            self.manifest,
            self.root / "exact-replacement",
            ScriptedBackend([replacement]),
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
        )

        self.assertTrue(outcome.verified)
        self.assertEqual(outcome.actions, 1)
        candidate = Path(outcome.candidate_workspace) / "config" / "production.ini"
        self.assertEqual(candidate.read_text(encoding="utf-8"), REPAIRED_PRODUCTION)
        trace = Path(outcome.run_root, "trace.jsonl").read_text(encoding="utf-8")
        self.assertIn('"kind":"replace_text"', trace)

    def test_guarded_line_range_replacement_runs_checks_and_verifier(self) -> None:
        replacement = ActionEnvelope(
            rationale=(
                "Use the numbered whole-file range and its observed guard without "
                "copying a long preimage."
            ),
            action=ReplaceLinesAction(
                path="config/production.ini",
                expected_sha256=self.original_sha,
                start_line=1,
                end_line=len(self.original_text.splitlines()),
                new_text=REPAIRED_PRODUCTION.rstrip("\n"),
            ),
        ).as_json()
        outcome = run_capsule(
            self.manifest,
            self.root / "line-range-replacement",
            ScriptedBackend([replacement]),
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
        )

        self.assertTrue(outcome.verified)
        self.assertEqual(outcome.actions, 1)
        candidate = Path(outcome.candidate_workspace) / "config" / "production.ini"
        self.assertEqual(candidate.read_text(encoding="utf-8"), REPAIRED_PRODUCTION)
        trace = Path(outcome.run_root, "trace.jsonl").read_text(encoding="utf-8")
        self.assertIn('"kind":"replace_lines"', trace)
        self.assertIn('"terminal_eol_preserved":true', trace)

    def test_schema_error_is_repaired_without_executing_malformed_output(self) -> None:
        backend = ScriptedBackend(
            [
                "```json\n{}\n```",
                self.read_action(),
                self.write_action(),
            ]
        )

        outcome = run_capsule(
            self.manifest,
            self.root / "parse-repair",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=5, max_actions=4),
        )

        self.assertTrue(outcome.verified)
        self.assertEqual(outcome.parse_failures, 1)
        self.assertEqual(outcome.actions, 2)
        second_prompt = backend.requests[1].messages[1].content
        self.assertIn("action_schema_error", second_prompt)

    def test_capsule_write_allowlist_denies_control_file_then_recovers(self) -> None:
        base = self.manifest.workspace_path / "config" / "base.ini"
        base_guard = hashlib.sha256(base.read_bytes()).hexdigest()
        denied = self.write_action(path="config/base.ini", guard=base_guard)
        backend = ScriptedBackend(
            [self.read_action(), denied, self.write_action()]
        )

        outcome = run_capsule(
            self.manifest,
            self.root / "policy-repair",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=5, max_actions=5),
        )

        self.assertTrue(outcome.verified)
        candidate_base = Path(outcome.candidate_workspace) / "config" / "base.ini"
        self.assertEqual(candidate_base.read_bytes(), base.read_bytes())
        third_prompt = backend.requests[2].messages[1].content
        self.assertIn("policy_denial", third_prompt)
        self.assertIn("config/base.ini", third_prompt)

    def test_backend_failure_produces_honest_nonverified_bundle(self) -> None:
        backend = ScriptedBackend([RuntimeError("local server disappeared")])

        outcome = run_capsule(
            self.manifest,
            self.root / "backend-failure",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
        )

        self.assertFalse(outcome.verified)
        self.assertEqual(outcome.status, "backend_error")
        self.assertIn("server disappeared", outcome.failure_reason or "")
        handoff = Path(outcome.delivery.handoff_path).read_text(encoding="utf-8")
        self.assertIn("NOT VERIFIED", handoff)
        self.assertIn("Do not apply", handoff)

    def test_backend_failure_cannot_be_promoted_when_final_verifier_passes(self) -> None:
        capsule_root = self.root / "already-valid-capsule"
        shutil.copytree(self.capsule_root, capsule_root)
        (capsule_root / "workspace" / "config" / "production.ini").write_text(
            REPAIRED_PRODUCTION,
            encoding="utf-8",
        )
        passing_manifest = load_capsule(capsule_root)

        outcome = run_capsule(
            passing_manifest,
            self.root / "backend-failure-verifier-pass",
            ScriptedBackend([RuntimeError("local server disappeared")]),
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=2, max_actions=2),
        )

        self.assertEqual(outcome.status, "backend_error")
        self.assertTrue(outcome.verification.ok)
        self.assertFalse(outcome.verified)
        self.assertFalse(outcome.delivery.verified)
        delivery_manifest = json.loads(
            Path(outcome.delivery.manifest_path).read_text(encoding="utf-8")
        )
        self.assertFalse(delivery_manifest["verified"])
        handoff = Path(outcome.delivery.handoff_path).read_text(encoding="utf-8")
        self.assertIn("NOT VERIFIED", handoff)
        self.assertIn("Do not apply", handoff)

    def test_public_check_regression_is_rolled_back_to_passing_checkpoint(self) -> None:
        jsonl_root = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "fixtures"
            / "capsules"
            / "jsonl-checkpoint-recovery"
        )
        manifest = load_capsule(jsonl_root)
        follower = manifest.workspace_path / "eventspool" / "follower.py"
        original = follower.read_text(encoding="utf-8")
        guard = hashlib.sha256(follower.read_bytes()).hexdigest()
        regressing_write = ActionEnvelope(
            rationale="Propose a syntactically broken edit to exercise transactional rollback.",
            action=WriteTextAction(
                path="eventspool/follower.py",
                expected_sha256=guard,
                content="this is not valid python !!!\n",
            ),
        ).as_json()
        backend = ScriptedBackend(
            [regressing_write, RuntimeError("stop after observing rollback")]
        )

        outcome = run_capsule(
            manifest,
            self.root / "transactional-rollback",
            backend,
            model="scripted-local-model",
            config=OperatorConfig(max_inference_calls=3, max_actions=3),
        )

        self.assertEqual(outcome.status, "backend_error")
        candidate = Path(outcome.candidate_workspace) / "eventspool" / "follower.py"
        self.assertEqual(candidate.read_text(encoding="utf-8"), original)
        second_prompt = backend.requests[1].messages[1].content
        self.assertIn("transaction_rollback", second_prompt)
        trace = Path(outcome.run_root, "trace.jsonl").read_text(encoding="utf-8")
        self.assertIn('"kind":"transaction.rolled_back"', trace)


if __name__ == "__main__":
    unittest.main()
