"""Desk integration with owned source bytes and a mocked local Git boundary.

No source is imported/executed, and every inference operation is mocked. Native
Git and capsule admission have separate targeted tests.
"""
from __future__ import annotations

import copy
import os
from unittest.mock import patch

from forge8 import cli, comparison as comparison_module, desk as desk_module
from forge8.git_source import GitBaseline
from test_desk import DeskFixture


class ChangeDeskTests(DeskFixture):
    def setUp(self):
        super().setUp()
        # Match deployment: comparison sources must be outside runtime/assets.
        environment = patch.dict(os.environ, {"FORGE8_STATE_HOME": str(self.root / "separate-state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.desk.close()
        self.desk = desk_module.ReadingDesk(self.source)
        self.addCleanup(self.desk.close)
        self.before = {
            "main.py": b"def answer():\n    return 41\n",
            "caller.py": b"from main import answer\n\ndef show():\n    return str(answer())\n",
        }
        for name, data in self.before.items():
            (self.source / name).write_bytes(data)
        (self.source / "main.py").write_bytes(b"def answer():\n    return 42\n")
        self.head = "a" * 40
        for name, value in (("read_head", GitBaseline(self.head, self.before, ())),
                            ("head_identity", self.head)):
            boundary = patch.object(comparison_module, name, return_value=value)
            boundary.start()
            self.addCleanup(boundary.stop)
        self.desk.refresh(mode="changes")

    def question(self):
        ids = {item["path"]: item["id"] for item in self.desk.project["files"]}
        return {"question": "What changes for this caller?", "version": self.desk.project["version"],
            "focus": [{"file": ids["before/main.py"], "start": 1, "end": 2},
                      {"file": ids["after/main.py"], "start": 1, "end": 2},
                      {"file": ids["after/caller.py"], "start": 1, "end": 4}]}

    def result(self):
        return {"ok": True, "status": "answered", "outcome": {
            "ok": True, "status": "answered", "answer": {"claims": []},
            "source_unchanged": True, "snapshot_unchanged": True,
            "acceptance": {"ok": True, "evidence": {"original_source": {
                "ok": True, "reason": None, "evidence": {
                    "head": self.head,
                    "original_snapshot_sha256": self.desk.comparison.original_snapshot_sha256}}}},
        }, "server": {"start": {"ok": True, "pid": 123},
            "shutdown": {"ok": True, "status": "terminated", "return_code": 0},
            "supervisor_secret_cleared": True, "transport_secret_cleared": True}}

    def test_pair_is_bound_to_original_but_reading_source_is_outside_runs(self):
        self.assertEqual(self.desk.source, self.source)
        self.assertNotEqual(self.desk.reading_source, self.source)
        self.assertNotIn(self.desk.root.parent, self.desk.reading_source.parents)
        self.assertEqual(self.desk.project["comparison"]["head"], self.head)
        self.assertTrue(self.desk.comparison.guard().ok)
        versions = {}
        for file in self.desk.project["files"]:
            if file["path"] in ("before/main.py", "after/main.py"):
                versions[file["path"]] = self.desk.source_view(file["id"], self.desk.project["version"])["lines"]
        self.assertIn("    return 41", versions["before/main.py"])
        self.assertIn("    return 42", versions["after/main.py"])
        self.assertIsNone(self.desk.worker)

    def test_job_passes_captured_guard_and_role_prompt_and_keeps_history_provenance(self):
        comparison = self.desk.comparison
        question = self.question()
        value = self.result()
        def run(args, **hooks):
            self.assertEqual(args.repo, comparison.reading_source)
            self.assertEqual(hooks["source_guard"].__self__, comparison)
            self.assertEqual(hooks["expected_snapshot"], question["version"])
            self.assertEqual(args.focus, ["before/main.py:1-2", "after/main.py:1-2", "after/caller.py:1-4"])
            self.assertTrue(args.question.startswith(desk_module._CHANGE_QUESTION))
            self.assertIn("held FIXED", args.question)
            self.assertTrue(args.question.endswith(question["question"]))
            hooks["on_result"](value)
        with patch.object(desk_module, "_run_explain_cli", side_effect=run) as runner:
            self.desk.start(question)
            self.desk.worker.join(5)
        runner.assert_called_once()
        self.assertFalse(self.desk.worker.is_alive())
        job = self.desk.status()
        self.assertEqual((job["status"], job["kind"]), ("answered", "changes"))
        self.assertEqual(job["question"], question["question"])
        saved = self.desk.history()["entries"][0]
        self.assertEqual(saved["comparison"], job["comparison"])
        self.assertTrue(saved["comparison"]["caller_held_fixed"])
        self.assertNotIn("catalogue", saved["comparison"])
        value["outcome"]["answer"]["claims"].append({"text": "later mutation"})
        self.assertEqual(self.desk.history()["entries"][0]["result"]["outcome"]["answer"]["claims"], [])

    def test_live_original_drift_refuses_before_model_even_though_capsule_is_unchanged(self):
        (self.source / "main.py").write_bytes(b"def answer():\n    return 99\n")
        with patch.object(cli, "prepare_server") as server:
            self.desk.start(self.question())
            self.desk.worker.join(5)
        self.assertFalse(self.desk.worker.is_alive())
        server.assert_not_called()
        job = self.desk.status()
        self.assertEqual(job["status"], "incomplete")
        self.assertEqual(job["result"]["status"], "source_guard_failed")
        self.assertNotIn("rejected_preview", job)

    def test_missing_or_wrong_original_proof_cannot_publish_answer_or_draft(self):
        for failure in ("missing", "head", "source", "release", "cancel"):
            with self.subTest(failure=failure):
                value = self.result()
                original = value["outcome"]["acceptance"]["evidence"]["original_source"]
                if failure == "missing": value["outcome"]["acceptance"]["evidence"].clear()
                if failure == "head": original["evidence"]["head"] = "b" * 40
                if failure == "source": original["evidence"]["original_snapshot_sha256"] = "stale"
                if failure == "release": value["server"]["shutdown"]["return_code"] = None
                def run(_args, **hooks):
                    hooks["on_text"]("ordinary unverified draft")
                    hooks["on_result"](value)
                    if failure == "cancel": hooks["cancel_event"].set()
                with patch.object(desk_module, "_run_explain_cli", side_effect=run):
                    self.desk.start(self.question())
                    self.desk.worker.join(5)
                self.assertFalse(self.desk.worker.is_alive())
                job = self.desk.status()
                self.assertNotEqual(job["status"], "answered")
                self.assertFalse(job["result"]["ok"])
                self.assertIsNone(job["result"]["outcome"]["answer"])
                self.assertNotIn("preview", job)
                self.assertNotIn("rejected_preview", job)

    def test_invalid_roles_and_limits_start_no_worker(self):
        valid = self.question()
        ids = {item["path"]: item["id"] for item in self.desk.project["files"]}
        invalid = []
        invalid.append({**valid, "focus": valid["focus"][:2]})
        reversed_pair = copy.deepcopy(valid)
        reversed_pair["focus"][:2] = list(reversed(reversed_pair["focus"][:2]))
        invalid.append(reversed_pair)
        for slot, path in ((1, "after/caller.py"), (2, "before/caller.py")):
            changed = copy.deepcopy(valid)
            changed["focus"][slot]["file"] = ids[path]
            invalid.append(changed)
        unchanged = copy.deepcopy(valid)
        unchanged["focus"][0]["file"] = ids["before/caller.py"]
        unchanged["focus"][1]["file"] = ids["after/caller.py"]
        invalid.append(unchanged)
        invalid.append({**valid, "question": "x" * 1301})
        with patch.object(desk_module, "_run_explain_cli") as runner:
            for payload in invalid:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    self.desk.start(payload)
        runner.assert_not_called()
        self.assertIsNone(self.desk.worker)

    def test_automatic_and_traceback_modes_require_ordinary_source(self):
        payload = {key: value for key, value in self.question().items() if key != "focus"}
        for kind in ("locate", "project"):
            with self.assertRaisesRegex(ValueError, "explicit before"):
                self.desk.start(payload, kind=kind)
        with self.assertRaisesRegex(ValueError, "ordinary source mode"):
            self.desk.tracebacks({"text": "Traceback (most recent call last):\n  File \"main.py\", line 1\n",
                                  "version": payload["version"]})
        self.assertIsNone(self.desk.worker)

    def test_failed_refresh_preserves_comparison_job_history_and_old_source(self):
        value = self.result()
        with patch.object(desk_module, "_run_explain_cli", side_effect=lambda _args, **hooks: hooks["on_result"](value)):
            self.desk.start(self.question())
            self.desk.worker.join(5)
        project, comparison = self.desk.project, self.desk.comparison
        history, job = self.desk.history(), self.desk.status()
        with patch.object(desk_module, "prepare_comparison", side_effect=ValueError("owned capture failure")):
            with self.assertRaises(ValueError): self.desk.refresh()
        self.assertIs(self.desk.project, project)
        self.assertIs(self.desk.comparison, comparison)
        self.assertEqual(self.desk.history(), history)
        self.assertEqual(self.desk.status(), job)
        self.assertEqual(self.desk.reading_source, comparison.reading_source)
        self.desk.refresh(mode="source")
        self.assertIsNone(self.desk.comparison)
        self.assertIsNone(self.desk.project["comparison"])
        self.assertEqual(self.desk.reading_source, self.source)
        self.assertEqual(self.desk.history()["entries"], [])
        self.assertEqual(self.desk.status()["status"], "idle")

    def test_comparison_hides_but_does_not_discard_ordinary_observation(self):
        observation = {"owned": "observation placeholder"}
        self.desk.observation = observation
        with patch.object(desk_module, "_observation_view", return_value={"owned": "view"}) as view:
            self.desk.refresh()
            view.assert_not_called()
            self.assertIsNone(self.desk.project["observation"])
            self.assertIs(self.desk.observation, observation)
            self.desk.refresh(mode="source")
            view.assert_called_once()
        self.assertEqual(self.desk.project["observation"], {"owned": "view"})

    def test_legacy_state_inside_assets_is_rejected_before_publishing_comparison(self):
        with patch.dict(os.environ, {}, clear=True):
            desk = desk_module.ReadingDesk(self.source)
            try:
                project = desk.project
                with patch.object(desk_module, "prepare_comparison") as prepare:
                    with self.assertRaisesRegex(ValueError, "state directory outside the assets"):
                        desk.refresh(mode="changes")
                prepare.assert_not_called()
                self.assertIs(desk.project, project)
                self.assertFalse((desk.root.parent.parent / "comparison-sources").exists())
            finally:
                desk.close()
