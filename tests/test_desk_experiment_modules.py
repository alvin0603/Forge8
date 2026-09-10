"""Explicit module-set desk admission; all owned source is static test data."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import threading
from unittest.mock import patch

from forge8 import desk as desk_module, experiments
import test_desk as fixtures
import test_desk_experiments as experiment_fixtures


class DeskExperimentModuleTests(fixtures.DeskFixture):
    # Borrow helpers, not the existing TestCase's test methods or setUp/super chain.
    report = experiment_fixtures.DeskExperimentTests.report
    join_experiment = experiment_fixtures.DeskExperimentTests.join_experiment
    reading_state = experiment_fixtures.DeskExperimentTests.reading_state
    assert_no_operations = experiment_fixtures.DeskExperimentTests.assert_no_operations

    def setUp(self):
        super().setUp()
        self.desk.close()
        self.modules = {
            "owned_pkg/__init__.py": b"",
            "owned_pkg/helper.py": (
                '# Owned data, never import.\r\nTAG = "x\u200by"\r\n'
                "raise RuntimeError('must not execute helper')\r\n"
            ).encode("utf-8"),
            "owned_pkg/main.py": (
                "from .helper import TAG\r\n"
                "def entry(value, *, suffix=''):\r\n"
                "    return {'value': value, 'tag': TAG + suffix}\r\n"
            ).encode("utf-8"),
        }
        for name, data in self.modules.items():
            destination = self.source / name
            destination.parent.mkdir(exist_ok=True)
            destination.write_bytes(data)
        self.legacy = b"def entry(value):\n    return value\n"
        (self.source / "main.py").write_bytes(self.legacy)
        (self.source / "notes.md").write_text("Owned non-Python fixture.\n", encoding="utf-8")
        (self.source / "nonphysical.py").write_text('TEXT = "a\u2028b"\n', encoding="utf-8")
        for mock in (
            patch.dict(os.environ, {"FORGE8_STATE_HOME": str(self.root / "state")}),
            patch.object(experiments, "native_platform", return_value="linux"),
            patch("subprocess.Popen", side_effect=AssertionError("no subprocess allowed")),
            patch.object(experiments, "_worker", side_effect=AssertionError("no native worker allowed")),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        self.guards = []
        for owner, name in ((experiments, "run_experiment"),
                            (desk_module, "_run_explain_cli"),
                            (desk_module, "_run_locate_cli"),
                            (desk_module, "_run_project_cli")):
            guard = patch.object(owner, name, side_effect=AssertionError("unmocked operation"))
            self.guards.append(guard.start())
            self.addCleanup(guard.stop)
        self.runtime = self.assets / f"experiment-{experiments.RUNTIME_NAME}-linux"
        self.runtime.mkdir()
        (self.runtime / "runtime.json").write_bytes(b"{}")  # Availability only.
        self.desk = desk_module.ReadingDesk(self.source, allow_experiments=True)
        self.addCleanup(self.desk.close)
        self.ids = {item["path"]: item["id"] for item in self.desk.project["files"]}

    def target(self):
        return {"file": self.ids["owned_pkg/main.py"], "version": self.desk.project["version"],
                "entry": "entry", "modules": [self.ids[name] for name in reversed(self.modules)]}

    def payload(self):
        target = self.target()
        prepared = self.desk.prepare_experiment(target)
        return {**target, "source_sha256": prepared["source_sha256"],
                "module_set_sha256": prepared["module_set"]["sha256"],
                "input_text": ' {"args":[9007199254740993],"kwargs":{"suffix":"!"}}\n',
                "allow_execution": True}, prepared["module_set"]

    def test_prepare_pins_every_raw_module_and_keeps_legacy_target_unchanged(self):
        before = self.reading_state()
        with patch.object(experiments, "prepare_module_set", wraps=experiments.prepare_module_set) as prepare:
            result = self.desk.prepare_experiment(self.target())
        prepare.assert_called_once_with(self.modules, "owned_pkg/main.py", "entry")
        self.assertEqual(set(result), {"file", "path", "version", "entry", "source_sha256", "source_bytes", "module_set"})
        metadata = result["module_set"]
        self.assertEqual(set(metadata), {"import_root", "entry_path", "entry", "entry_module", "files", "sha256"})
        self.assertEqual((metadata["import_root"], metadata["entry_path"], metadata["entry"], metadata["entry_module"]),
                         (".", "owned_pkg/main.py", "entry", "owned_pkg.main"))
        self.assertEqual(metadata["files"], [{"path": name, "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data)} for name, data in sorted(self.modules.items())])
        self.assertRegex(metadata["sha256"], r"^[0-9a-f]{64}$")
        reordered = {**self.target(), "modules": list(reversed(self.target()["modules"]))}
        self.assertEqual(self.desk.prepare_experiment(reordered), result)
        view = self.desk.source_view(self.ids["owned_pkg/helper.py"], result["version"])
        self.assertIn('TAG = "x\\u200by"', view["lines"])
        self.assertNotEqual("\n".join(view["lines"]).encode("utf-8"), self.modules["owned_pkg/helper.py"])
        legacy = {"file": self.ids["main.py"], "version": result["version"], "entry": "entry"}
        self.assertEqual(self.desk.prepare_experiment(legacy), {**legacy, "path": "main.py",
            "source_sha256": hashlib.sha256(self.legacy).hexdigest(), "source_bytes": len(self.legacy)})
        self.assertEqual(self.reading_state(), before)
        self.assert_no_operations()

    def test_explicit_ids_and_parent_package_are_required_before_any_operation(self):
        target = self.target()
        invalid_sets = [None, (), "main.py", [], [True], ["unknown"],
            [self.ids["main.py"]], target["modules"] * 2,
            target["modules"] + [target["file"]],
            [self.ids["owned_pkg/main.py"], self.ids["owned_pkg/helper.py"]],
            target["modules"] + [self.ids["notes.md"]],
            target["modules"] + [self.ids["nonphysical.py"]],
            target["modules"] + ["unknown"]]
        before = set(self.desk.root.iterdir())
        for modules in invalid_sets:
            with self.subTest(modules=modules), self.assertRaises(ValueError):
                self.desk.prepare_experiment({**target, "modules": modules})
        with self.assertRaises(ValueError):
            self.desk.prepare_experiment({**target, "module_set_sha256": "0" * 64})
        self.assertEqual(set(self.desk.root.iterdir()), before)
        self.assert_no_operations()

    def test_run_requires_both_module_fields_and_exact_source_set_digest(self):
        valid, _ = self.payload()
        invalid = [
            {key: value for key, value in valid.items() if key != "modules"},
            {key: value for key, value in valid.items() if key != "module_set_sha256"},
            {**valid, "module_set_sha256": "0" * 64},
            {**valid, "source_sha256": "0" * 64},
            {**valid, "modules": valid["modules"] + [self.ids["main.py"]]},
            {**valid, "module_root": str(self.source)},
            {**valid, "module_set": {}},
        ]
        before = set(self.desk.root.iterdir())
        for payload in invalid:
            with self.subTest(keys=sorted(payload)), self.assertRaises(ValueError):
                self.desk.start_experiment(payload)
        self.assertEqual(set(self.desk.root.iterdir()), before)
        self.assertEqual(self.desk.experiment_status(), {"id": None, "status": "idle"})
        self.assert_no_operations()

    def test_run_uses_retained_module_set_despite_live_edits_and_preserves_reading(self):
        payload, metadata = self.payload()
        self.desk.job = {"id": "prior-reading", "status": "answered", "question": "Keep this question",
            "reader": "qwen35", "version": payload["version"], "gpu": "released",
            "result": {"focus": [{"path": "owned_pkg/main.py", "start_line": 2, "end_line": 3}],
                       "answer": "Owned prior answer"}}
        self.desk.started, self.desk.finished = 1.0, 2.0
        with self.desk.lock:
            self.desk._remember_finished_job()
        before = self.reading_state()
        for name in self.modules:
            (self.source / name).write_bytes(b"raise RuntimeError('different live bytes; never execute')\n")

        def run(runtime, source, entry, inputs, destination, **options):
            retained = Path(self.desk.browse_snapshot.snapshot_root)
            self.assertEqual((runtime, source, entry), (self.runtime, retained / "owned_pkg/main.py", "entry"))
            self.assertEqual({name: (retained / name).read_bytes() for name in self.modules}, self.modules)
            self.assertEqual(inputs.read_bytes(), payload["input_text"].encode("utf-8"))
            self.assertEqual(destination, inputs.parent / "execution")
            self.assertEqual(set(options), {"allow_execution", "expected_source_sha256", "cancel_requested",
                "module_root", "module_files", "expected_module_set_sha256"})
            self.assertIs(options["allow_execution"], True)
            self.assertEqual(options["expected_source_sha256"], payload["source_sha256"])
            self.assertEqual(options["module_root"], retained)
            self.assertEqual(options["module_files"], sorted(self.modules))
            self.assertEqual(options["expected_module_set_sha256"], metadata["sha256"])
            self.assertFalse(options["cancel_requested"]())
            return self.report()

        with patch.object(experiments, "run_experiment", side_effect=run) as runner:
            identifier = self.desk.start_experiment(payload)["id"]
            self.join_experiment()
        runner.assert_called_once()
        result = self.desk.experiment_status()
        self.assertEqual((result["id"], result["status"], result["module_set"]), (identifier, "completed", metadata))
        self.assertEqual(result["input_text"], payload["input_text"])
        self.assertIn("9007199254740993", result["result_text"])
        self.assertEqual(self.reading_state(), before)
        for guard in self.guards:
            guard.assert_not_called()

    def test_changed_retained_dependency_refuses_before_worker_or_run_directory(self):
        payload, _ = self.payload()
        retained = Path(self.desk.browse_snapshot.snapshot_root) / "owned_pkg/helper.py"
        retained.chmod(stat.S_IREAD | stat.S_IWRITE)
        retained.write_bytes(self.modules["owned_pkg/helper.py"].replace(b"TAG", b"NEW"))
        before = set(self.desk.root.iterdir())
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            self.desk.start_experiment(payload)
        self.assertEqual(set(self.desk.root.iterdir()), before)
        self.assertEqual(self.desk.experiment_status(), {"id": None, "status": "idle"})
        self.assert_no_operations()

    def test_current_and_cancel_preserve_the_complete_module_set_identity(self):
        payload, metadata = self.payload()
        before = self.reading_state()
        entered, release = threading.Event(), threading.Event()

        def run(*args, **options):
            entered.set()
            self.assertTrue(release.wait(3), "test did not release mock experiment")
            self.assertTrue(options["cancel_requested"]())
            return self.report("late result must be discarded")

        with patch.object(experiments, "run_experiment", side_effect=run) as runner:
            identifier = self.desk.start_experiment(payload)["id"]
            try:
                self.assertTrue(entered.wait(3))
                running = self.desk.experiment_status()
                self.assertEqual((running["status"], running["module_set"]), ("running", metadata))
                self.desk.cancel_experiment({"id": identifier})
                cancelling = self.desk.experiment_status()
                self.assertEqual((cancelling["status"], cancelling["module_set"]), ("cancelling", metadata))
            finally:
                release.set()
                self.join_experiment()
        runner.assert_called_once()
        result = self.desk.experiment_status()
        self.assertEqual((result["id"], result["status"], result["module_set"]), (identifier, "cancelled", metadata))
        self.assertEqual(result["input_text"], payload["input_text"])
        self.assertNotIn("result_text", result)
        self.assertFalse(self.desk._busy())
        self.assertEqual(self.reading_state(), before)
