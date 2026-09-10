from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from forge8 import experiments


ENTRY = (b"from .helper import OFFSET\r\n"
         b"raise RuntimeError('owned source must never execute in host tests')\r\n"
         b"def entry(value):\r\n    return value + OFFSET\r\n"
         b"def other(value):\r\n    return value\r\n")
HELPER = "# retained invisible character: \u200b\r\nOFFSET = 2\r\n".encode("utf-8")
FILES = {"ownedpkg/reader.py": ENTRY, "ownedpkg/__init__.py": b"", "ownedpkg/helper.py": HELPER}


class ModuleSetAdmissionTests(unittest.TestCase):
    def test_binary_archive_read_is_bounded_without_weakening_source_text_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "selected.zip"
            path.write_bytes(b"PK\x00\xff")
            self.assertEqual(experiments._file(path, 4, binary=True), b"PK\x00\xff")
            with self.assertRaises(ValueError):
                experiments._file(path, 3, binary=True)
            with self.assertRaises(ValueError):
                experiments._file(path, 4)

    def prepare(self, files=None, path="ownedpkg/reader.py", entry="entry"):
        return experiments.prepare_module_set(dict(FILES) if files is None else files, path, entry)

    def test_manifest_sorted_raw_bytes_canonical_hash_and_empty_init(self) -> None:
        with patch("builtins.exec", side_effect=AssertionError("never execute source")):
            result = self.prepare()
        self.assertEqual(set(result), {"import_root", "entry_path", "entry", "entry_module", "files", "sha256"})
        self.assertEqual((result["import_root"], result["entry_module"]), (".", "ownedpkg.reader"))
        self.assertEqual(result["files"], [{"path": path, "sha256": hashlib.sha256(code).hexdigest(),
                                          "size_bytes": len(code)} for path, code in sorted(FILES.items())])
        canonical = json.dumps({key: value for key, value in result.items() if key != "sha256"},
                               sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertEqual(result["sha256"], hashlib.sha256(canonical).hexdigest())
        reversed_files = dict(reversed(list(FILES.items())))
        self.assertEqual(self.prepare(reversed_files), result)

    def test_identity_binds_entry_file_function_all_members_and_raw_newlines(self) -> None:
        baseline = self.prepare()["sha256"]
        changes = [self.prepare(entry="other")]
        for changed in (HELPER.replace(b"\r\n", b"\n"), HELPER + b"# extra\n"):
            changes.append(self.prepare({**FILES, "ownedpkg/helper.py": changed}))
        renamed = dict(FILES)
        renamed["ownedpkg/renamed.py"] = renamed.pop("ownedpkg/reader.py")
        changes.append(self.prepare(renamed, path="ownedpkg/renamed.py"))
        self.assertTrue(all(item["sha256"] != baseline for item in changes))
        package = self.prepare({"ownedpkg/__init__.py": b"def entry():\n    return 1\n"},
                               path="ownedpkg/__init__.py")
        self.assertEqual(package["entry_module"], "ownedpkg")

    def test_one_to_four_files_share_the_existing_64k_source_budget(self) -> None:
        base = b"def entry():\n    return 1\n#"
        files = {"entrypoint.py": base + b"x" * (experiments.MAX_SOURCE_BYTES - len(base)),
                 "one.py": b"", "two.py": b"", "three.py": b""}
        result = self.prepare(files, path="entrypoint.py")
        self.assertEqual(sum(item["size_bytes"] for item in result["files"]), 65_536)
        with self.assertRaisesRegex(ValueError, "exceeds 65536 bytes in total"):
            self.prepare({**files, "one.py": b"#"}, path="entrypoint.py")
        for invalid in ({}, {**files, "four.py": b""}):
            with self.subTest(count=len(invalid)), self.assertRaisesRegex(ValueError, "one to four"):
                self.prepare(invalid, path="entrypoint.py")

    def test_noncanonical_and_non_ascii_paths_are_rejected(self) -> None:
        for path in ("/entry.py", "../entry.py", "./entry.py", "a//entry.py", "a/../entry.py",
                     "a\\entry.py", "C:entry.py", "a-b.py", "程式.py", "entry.PY", "a.pyi",
                     "entry.py/", ".py", "a" * 1022 + ".py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.prepare({path: ENTRY}, path=path)

    def test_missing_parent_init_case_collisions_and_module_package_collision_refuse(self) -> None:
        bad_sets = [
            {"ownedpkg/reader.py": ENTRY},
            {"outer/inner/reader.py": ENTRY, "outer/inner/__init__.py": b""},
            {"__init__.py": ENTRY},
            {"ownedpkg.py": ENTRY, "ownedpkg/__init__.py": ENTRY},
            {"Entry.py": ENTRY, "entry.py": ENTRY},
            {"Pkg/__init__.py": b"", "pkg/entry.py": ENTRY},
        ]
        for files in bad_sets:
            with self.subTest(paths=list(files)), self.assertRaises(ValueError):
                self.prepare(files, path=next(iter(files)))

    def test_every_source_is_statically_parsed_and_encoding_cannot_change_guest_meaning(self) -> None:
        for code in (b"def broken(:\n", b"\xff", "# coding: latin-1\nVALUE = 'é'\n".encode("utf-8"),
                     b"#!/usr/bin/python\n# coding=ascii\nVALUE=1\n", b"# coding: no_such_encoding\n"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.prepare({**FILES, "ownedpkg/helper.py": code})
        allowed = {**FILES, "ownedpkg/helper.py": b"# coding: utf-8\r\n" + HELPER}
        self.assertEqual(len(self.prepare(allowed)["files"]), 3)
        for files, path, entry in (({"module.py": "not raw bytes"}, "module.py", "entry"),
                                  (FILES, "missing.py", "entry"), (FILES, "ownedpkg/reader.py", "missing"),
                                  (FILES, "ownedpkg/reader.py", "__hidden")):
            with self.subTest(path=path, entry=entry), self.assertRaises(ValueError):
                self.prepare(files, path, entry)


class ModuleSetRunTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "selected-project"
        (self.project / "ownedpkg").mkdir(parents=True)
        self.files = dict(FILES)
        for path, data in self.files.items():
            (self.project / path).write_bytes(data)
        self.source = self.project / "ownedpkg/reader.py"
        self.inputs = self.root / "arguments.json"
        self.raw_input = b' {"args":[9007199254740993],"kwargs":{}}\n'
        self.inputs.write_bytes(self.raw_input)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.pin = {"filename": "python.cwasm", "size_bytes": 10, "sha256": "a" * 64}
        self.metadata = experiments.prepare_module_set(self.files, "ownedpkg/reader.py", "entry")
        self.guest = {"host_status": "exited", "detail": None, "output_limit": False,
                      "load_seconds": .01, "worker_seconds": .02,
                      "guest_output": {"stdout": 'FORGE8_GUEST_RESULT={"return":9007199254740995}\n',
                                       "stderr": ""}}

    def options(self):
        return {"allow_execution": True, "module_root": self.project,
                "module_files": list(self.files), "expected_module_set_sha256": self.metadata["sha256"],
                "expected_source_sha256": hashlib.sha256(ENTRY).hexdigest()}

    def invoke(self, name, *, after_worker=None, verify_effect=None):
        destination = self.root / name

        def worker(runtime, directory, operation, payload, **kwargs):
            self.assertEqual((runtime, directory, operation), (self.runtime, destination, "run"))
            self.assertEqual(set(payload), {"request", "cache_pin", "module_bundle"})
            self.assertEqual(payload["cache_pin"], self.pin)
            self.assertEqual(payload["request"], {"source": ENTRY.decode("utf-8"), "entry": "entry",
                                                  "input": {"args": [9007199254740993], "kwargs": {}}})
            bundle = payload["module_bundle"]
            self.assertEqual(set(bundle), {"entry_module", "top_levels", "sha256", "size_bytes"})
            self.assertEqual((bundle["entry_module"], bundle["top_levels"]), ("ownedpkg.reader", ["ownedpkg"]))
            self.assertEqual(list((directory / "modules").iterdir()), [directory / "modules/selected.zip"])
            archive_path = directory / "modules/selected.zip"
            data = archive_path.read_bytes()
            self.assertEqual((hashlib.sha256(data).hexdigest(), len(data)), (bundle["sha256"], bundle["size_bytes"]))
            self.assertLessEqual(len(data), experiments.MAX_MODULE_BUNDLE_BYTES)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.namelist(), sorted(self.files))
                for item in archive.infolist():
                    self.assertEqual(archive.read(item), self.files[item.filename])
                    self.assertEqual(item.compress_type, zipfile.ZIP_STORED)
                    self.assertEqual(item.date_time, (1980, 1, 1, 0, 0, 0))
                    self.assertEqual(stat.S_IFMT(item.external_attr >> 16), stat.S_IFREG)
            captured = json.loads((directory / "input.json").read_text(encoding="utf-8"))
            self.assertEqual(captured["identity"]["module_set"], self.metadata)
            self.assertEqual(captured["module_bundle"], bundle)
            if after_worker:
                after_worker(directory)
            return SimpleNamespace(status="passed", duration_seconds=.125), deepcopy(self.guest)

        with patch.object(experiments, "native_platform", return_value="linux"), \
                patch.object(experiments, "verify_experiment_runtime",
                             side_effect=verify_effect, return_value=(self.runtime, self.pin)) as verified, \
                patch.object(experiments, "_worker", side_effect=worker) as called, \
                patch("subprocess.Popen", side_effect=AssertionError("no worker or guest may start")):
            result = experiments.run_experiment(self.runtime, self.source, "entry", self.inputs,
                                                destination, **self.options())
        called.assert_called_once()
        self.assertEqual(verified.call_count, 2)
        self.assertEqual(json.loads((destination / "experiment.json").read_text(encoding="utf-8")), result)
        return result

    def test_deterministic_raw_zip_owned_pins_bigints_and_legacy_runtime_metadata(self) -> None:
        first = self.invoke("first")
        second = self.invoke("second")
        self.assertEqual(first["module_bundle"], second["module_bundle"])
        self.assertEqual(first["identity"]["module_set"], self.metadata)
        self.assertEqual(first["reported_result"], {"return": 9007199254740995})
        self.assertTrue(first["source_unchanged"])
        self.assertTrue(first["runtime_unchanged"])
        self.assertEqual(first["postcheck_errors"], [])
        self.assertEqual(first["runtime"]["cache"], self.pin)
        self.assertEqual(first["runtime"]["stdlib_layout"], "python314-zip-stored-v1")
        self.assertEqual(first["runtime"]["engine_options"], experiments.ENGINE_OPTIONS)
        self.assertEqual(self.source.read_bytes(), ENTRY)

    def test_argument_membership_duplicates_and_expected_hash_reject_before_runtime(self) -> None:
        invalid = [
            {"module_root": None}, {"module_files": None}, {"module_files": ()},
            {"module_files": list(self.files) + ["ownedpkg/helper.py"]},
            {"module_files": ["ownedpkg/__init__.py", "ownedpkg/helper.py"]},
            {"expected_module_set_sha256": "0" * 64}, {"expected_source_sha256": "0" * 64},
            {"allow_execution": False},
        ]
        with patch.object(experiments, "verify_experiment_runtime") as verify, \
                patch.object(experiments, "_worker") as worker:
            for changes in invalid:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    experiments.run_experiment(self.runtime, self.source, "entry", self.inputs,
                                               self.root / "not-created", **(self.options() | changes))
            with self.assertRaises(ValueError):
                experiments.run_experiment(self.runtime, self.root / "outside.py", "entry", self.inputs,
                                           self.root / "not-created", **self.options())
            verify.assert_not_called()
            worker.assert_not_called()
        self.assertFalse((self.root / "not-created").exists())

    def test_source_or_input_drift_during_runtime_check_refuses_before_zip_and_worker(self) -> None:
        for index, path in enumerate((self.project / "ownedpkg/helper.py", self.inputs)):
            with self.subTest(path=path):
                original = path.read_bytes()

                def verify(*args, **kwargs):
                    path.write_bytes(original + b" ")
                    return self.runtime, self.pin

                destination = self.root / f"pre-drift-{index}"
                with patch.object(experiments, "verify_experiment_runtime", side_effect=verify), \
                        patch.object(experiments, "_worker") as worker:
                    with self.assertRaisesRegex(ValueError, "changed before execution"):
                        experiments.run_experiment(self.runtime, self.source, "entry", self.inputs,
                                                   destination, **self.options())
                    worker.assert_not_called()
                self.assertFalse(destination.exists())
                path.write_bytes(original)

    def test_postrun_changed_deleted_or_unparseable_members_retain_original_report(self) -> None:
        target = self.project / "ownedpkg/helper.py"
        for index, mutate in enumerate((lambda: target.write_bytes(b"def broken(:\n"), target.unlink,
                                        lambda: self.inputs.write_bytes(b"{"))):
            with self.subTest(index=index):
                target.write_bytes(HELPER)
                self.inputs.write_bytes(self.raw_input)
                result = self.invoke(f"post-drift-{index}", after_worker=lambda directory: mutate())
                self.assertFalse(result["source_unchanged"])
                self.assertTrue(result["runtime_unchanged"])
                self.assertEqual(result["reported_result"], {"return": 9007199254740995})
                self.assertEqual(result["identity"]["module_set"], self.metadata)

    def test_zip_or_preopen_directory_drift_suppresses_result_without_mislabeling_runtime(self) -> None:
        changes = (
            lambda directory: (directory / "modules/selected.zip").write_bytes(b"changed"),
            lambda directory: (directory / "modules/selected.zip").unlink(),
            lambda directory: (directory / "modules/not-selected.py").write_bytes(b"raise AssertionError\n"),
        )
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                result = self.invoke(f"bundle-drift-{index}", after_worker=change)
                self.assertFalse(result["source_unchanged"])
                self.assertTrue(result["runtime_unchanged"])
                self.assertIsNone(result["reported_result"])
                self.assertTrue(any("bundle integrity" in error for error in result["postcheck_errors"]))

    def test_runtime_failure_prevents_bundle_creation_or_suppresses_postrun_result(self) -> None:
        destination = self.root / "runtime-refused"
        with patch.object(experiments, "verify_experiment_runtime", side_effect=ValueError("runtime changed")), \
                patch.object(experiments, "_worker") as worker:
            with self.assertRaisesRegex(ValueError, "runtime changed"):
                experiments.run_experiment(self.runtime, self.source, "entry", self.inputs,
                                           destination, **self.options())
            worker.assert_not_called()
        self.assertFalse(destination.exists())
        result = self.invoke("runtime-post", verify_effect=[(self.runtime, self.pin), ValueError("changed")])
        self.assertTrue(result["source_unchanged"])
        self.assertFalse(result["runtime_unchanged"])
        self.assertIsNone(result["reported_result"])

    def test_module_hardlink_special_or_redirected_parent_is_not_opened(self) -> None:
        target = self.project / "ownedpkg/helper.py"
        real_lstat = Path.lstat
        for mode, links in ((stat.S_IFIFO, 1), (stat.S_IFLNK, 1), (stat.S_IFREG, 2)):
            with self.subTest(mode=mode, links=links):
                metadata = SimpleNamespace(st_mode=mode | 0o600, st_nlink=links, st_size=len(HELPER))

                def lstat(path, *args, **kwargs):
                    return metadata if path == target else real_lstat(path, *args, **kwargs)

                with patch.object(Path, "lstat", lstat), \
                        patch.object(experiments, "verify_experiment_runtime") as verify, \
                        patch.object(experiments, "_worker") as worker:
                    with self.assertRaises(ValueError):
                        experiments.run_experiment(self.runtime, self.source, "entry", self.inputs,
                                                   self.root / "not-created", **self.options())
                    verify.assert_not_called()
                    worker.assert_not_called()
        actual = experiments._strict_existing_directory

        def redirected(path, label):
            return self.root if path == self.project / "ownedpkg" else actual(path, label)

        with patch.object(experiments, "_strict_existing_directory", side_effect=redirected):
            with self.assertRaisesRegex(ValueError, "redirected"):
                experiments._read_module_files(self.project, list(self.files))

    def test_pre_cancel_and_cancel_callback_preserve_existing_cleanup_contract(self) -> None:
        callback = lambda: True
        with patch.object(experiments, "verify_experiment_runtime") as verify, \
                patch.object(experiments, "_worker") as worker:
            with self.assertRaises(KeyboardInterrupt):
                experiments.run_experiment(self.runtime, self.source, "entry", self.inputs,
                                           self.root / "cancelled", cancel_requested=callback, **self.options())
            verify.assert_not_called()
            worker.assert_not_called()
        with patch.object(experiments, "verify_experiment_runtime", return_value=(self.runtime, self.pin)), \
                patch.object(experiments, "_worker", side_effect=KeyboardInterrupt) as worker:
            never_cancel = lambda: False
            with self.assertRaises(KeyboardInterrupt):
                experiments.run_experiment(self.runtime, self.source, "entry", self.inputs,
                                           self.root / "worker-cancel", cancel_requested=never_cancel, **self.options())
            self.assertIs(worker.call_args.kwargs["cancel_requested"], never_cancel)
        self.assertFalse((self.root / "worker-cancel/experiment.json").exists())


if __name__ == "__main__":
    unittest.main()
