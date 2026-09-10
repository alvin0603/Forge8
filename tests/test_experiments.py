from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
import warnings
import zipfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from forge8 import experiments
from forge8.runtime import IntegrityResult


class ExperimentAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "owned.py"
        self.inputs = self.root / "input.json"
        self.source.write_bytes(b"def entry(value):\n    return value\n")
        self.inputs.write_bytes(b'{"args":[1],"kwargs":{}}')

    def prepare(self):
        return experiments.prepare_request(self.source, "entry", self.inputs)

    def test_whole_module_stays_static_and_raw_identity_and_bigint_are_preserved(self) -> None:
        code = (
            b"raise RuntimeError('never execute during preparation')\r\n"
            b"LIMIT = 12\r\n\r\ndef entry(value):\r\n    return value + LIMIT\r\n"
        )
        large = 10**60 + 9007199254740993
        raw = (' {"args":[' + str(large) + '],"kwargs":{"label":"\u4f60"}}\n').encode("utf-8")
        self.source.write_bytes(code)
        self.inputs.write_bytes(raw)
        with patch("builtins.exec", side_effect=AssertionError("source executed")), \
                patch("builtins.eval", side_effect=AssertionError("source evaluated")):
            request, identity = self.prepare()
        self.assertEqual(request, {
            "source": code.decode("utf-8"), "entry": "entry",
            "input": {"args": [large], "kwargs": {"label": "\u4f60"}},
        })
        self.assertIs(type(request["input"]["args"][0]), int)
        self.assertEqual(identity, {
            "source_sha256": hashlib.sha256(code).hexdigest(),
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "source_bytes": len(code), "input_bytes": len(raw), "entry": "entry",
        })

    def test_source_limit_counts_exact_utf8_bytes(self) -> None:
        base = b"def entry(value):\n    return value\n#"
        remaining = experiments.MAX_SOURCE_BYTES - len(base)
        code = base + "\u4f60".encode("utf-8") * (remaining // 3) + b"x" * (remaining % 3)
        self.source.write_bytes(code)
        self.assertEqual(self.prepare()[1]["source_bytes"], 65_536)
        self.source.write_bytes(code + b"x")
        with self.assertRaisesRegex(ValueError, "exceeds 65536 bytes"):
            self.prepare()

    def test_input_limit_accepts_exact_size_and_rejects_one_extra_byte(self) -> None:
        prefix, suffix = b'{"args":["', b'"],"kwargs":{}}'
        raw = prefix + b"x" * (experiments.MAX_INPUT_BYTES - len(prefix) - len(suffix)) + suffix
        self.inputs.write_bytes(raw)
        self.assertEqual(self.prepare()[1]["input_bytes"], 16_384)
        self.inputs.write_bytes(raw + b" ")
        with self.assertRaisesRegex(ValueError, "exceeds 16384 bytes"):
            self.prepare()

    def test_input_requires_exact_args_list_and_kwargs_object(self) -> None:
        for raw in (
            b"[]", b"null", b"{}", b'{"args":[]}', b'{"kwargs":{}}',
            b'{"args":{},"kwargs":{}}', b'{"args":[],"kwargs":[]}',
            b'{"args":[],"kwargs":{},"extra":1}',
        ):
            with self.subTest(raw=raw):
                self.inputs.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "input must be exactly"):
                    self.prepare()

    def test_duplicate_keys_and_nonfinite_numbers_are_rejected_at_any_depth(self) -> None:
        invalid = [
            b'{"args":[],"args":[1],"kwargs":{}}',
            b'{"args":[{"x":1,"x":2}],"kwargs":{}}',
            b'{"args":[],"kwargs":{"x":1,"x":2}}',
        ] + [b'{"args":[{"value":' + value + b'}],"kwargs":{}}'
             for value in (b"NaN", b"Infinity", b"-Infinity", b"1e999", b"-1e999")]
        for raw in invalid:
            with self.subTest(raw=raw):
                self.inputs.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.prepare()

    def test_entry_requires_one_synchronous_top_level_definition(self) -> None:
        for code in (
            "async def entry(value):\n    return value\n",
            "def outer():\n    def entry(value):\n        return value\n",
            "class Owner:\n    def entry(self):\n        return 1\n",
            "if True:\n    def entry(value):\n        return value\n",
            "def entry():\n    pass\ndef entry():\n    pass\n",
            "entry = 3\n",
        ):
            with self.subTest(code=code):
                self.source.write_text(code, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "synchronous top-level function"):
                    self.prepare()
        for entry in ("", "owner.entry", "entry()", "__hidden"):
            with self.subTest(entry=entry), patch.object(experiments, "_file") as read:
                with self.assertRaises(ValueError):
                    experiments.prepare_request(self.source, entry, self.inputs)
                read.assert_not_called()

    def test_non_python_invalid_utf8_and_invalid_syntax_are_rejected(self) -> None:
        with patch.object(experiments, "_file") as read:
            with self.assertRaises(ValueError):
                experiments.prepare_request(self.source.with_suffix(".txt"), "entry", self.inputs)
            read.assert_not_called()
        for code, error in ((b"\xff", ValueError), (b"def entry(:\n", ValueError)):
            with self.subTest(code=code):
                self.source.write_bytes(code)
                with self.assertRaises(error):
                    self.prepare()

    def test_symlink_special_hardlinked_and_reparse_inputs_are_not_read(self) -> None:
        real_lstat = Path.lstat
        for mode, links, reparse in (
            (stat.S_IFLNK, 1, False), (stat.S_IFIFO, 1, False),
            (stat.S_IFDIR, 1, False), (stat.S_IFREG, 2, False),
            (stat.S_IFREG, 1, True),
        ):
            with self.subTest(mode=mode, links=links, reparse=reparse):
                metadata = SimpleNamespace(st_mode=mode | 0o600, st_nlink=links, st_size=1)

                def lstat(path, *args, **kwargs):
                    return metadata if path == self.source else real_lstat(path, *args, **kwargs)

                with patch.object(Path, "lstat", lstat), \
                        patch.object(experiments, "_is_reparse_point", return_value=reparse), \
                        patch.object(experiments, "_read_regular_file") as read:
                    with self.assertRaisesRegex(ValueError, "regular single-link file"):
                        experiments._file(self.source, experiments.MAX_SOURCE_BYTES)
                    read.assert_not_called()


class ExperimentArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.archive = self.root / "owned.zip"
        self.destination = self.root / "unpacked"

    def write_archive(self, members) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # Deliberately duplicate ZIP entries.
            with zipfile.ZipFile(self.archive, "w") as package:
                for name, data in members:
                    if isinstance(name, str):
                        # Encode the actual raw ZIP path: ZipInfo(name) silently
                        # normalizes backslashes on Windows before writing.
                        member = zipfile.ZipInfo()
                        member.filename = name
                        member.orig_filename = name
                    else:
                        member = name
                    package.writestr(member, data)

    def test_empty_regular_files_extract_and_receive_full_integrity_pins(self) -> None:
        self.write_archive([("package/", b""), ("package/__init__.py", b""), ("package/data.json", b"{}")])
        experiments._unpack(self.archive, self.destination)
        self.assertEqual((self.destination / "package/__init__.py").read_bytes(), b"")
        self.assertEqual(experiments._pins(self.destination), [
            {"filename": "package/__init__.py", "size_bytes": 0,
             "sha256": hashlib.sha256(b"").hexdigest()},
            {"filename": "package/data.json", "size_bytes": 2,
             "sha256": hashlib.sha256(b"{}").hexdigest()},
        ])

    def test_unsafe_archive_paths_fail_before_destination_creation(self) -> None:
        for name in ("../escape.py", "/absolute.py", "C:/escape.py", "a\\escape.py",
                     "a/../escape.py", "a//escape.py", "a/./escape.py", "a./file", "a /file"):
            with self.subTest(name=name):
                self.write_archive([(name, b"owned")])
                with self.assertRaises(ValueError):
                    experiments._unpack(self.archive, self.destination)
                self.assertFalse(self.destination.exists())

    def test_archive_symlink_and_special_file_modes_are_rejected(self) -> None:
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
            with self.subTest(mode=mode):
                member = zipfile.ZipInfo("unsafe")
                member.create_system = 3
                member.external_attr = (mode | 0o600) << 16
                self.write_archive([(member, b"target")])
                with self.assertRaisesRegex(ValueError, "unsafe or duplicate path"):
                    experiments._unpack(self.archive, self.destination)
                self.assertFalse(self.destination.exists())

    def test_duplicate_and_case_colliding_archive_names_are_rejected(self) -> None:
        for names in (("same.py", "same.py"), ("Case.py", "case.py"), ("folder/", "folder")):
            with self.subTest(names=names):
                self.write_archive([(name, b"") for name in names])
                with self.assertRaisesRegex(ValueError, "unsafe or duplicate path"):
                    experiments._unpack(self.archive, self.destination)
                self.assertFalse(self.destination.exists())

    def test_archive_count_and_expanded_size_limits_precede_extraction(self) -> None:
        for members in ([SimpleNamespace(file_size=0)] * 2501,
                        [SimpleNamespace(file_size=200 * 1024**2 + 1)]):
            with self.subTest(count=len(members)), patch.object(experiments.zipfile, "ZipFile") as opened:
                package = opened.return_value.__enter__.return_value
                package.infolist.return_value = members
                with self.assertRaisesRegex(ValueError, "extraction bounds"):
                    experiments._unpack(self.archive, self.destination)
                package.extractall.assert_not_called()
                self.assertFalse(self.destination.exists())


class ExperimentControllerTests(unittest.TestCase):
    def test_expected_snapshot_hash_mismatch_prevents_runtime_and_worker_io(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source, inputs = root / "owned.py", root / "arguments.json"
            source.write_bytes(b"def entry():\n    return 1\n")
            inputs.write_bytes(b'{"args":[],"kwargs":{}}')
            with patch.object(experiments, "verify_experiment_runtime") as verify, \
                    patch.object(experiments, "_worker") as worker:
                with self.assertRaisesRegex(ValueError, "source version changed"):
                    experiments.run_experiment(root / "runtime", source, "entry", inputs, root / "run",
                        allow_execution=True, expected_source_sha256="0" * 64)
                verify.assert_not_called()
                worker.assert_not_called()
                self.assertFalse((root / "run").exists())

    def test_cancelled_controller_does_not_read_source_or_runtime(self) -> None:
        with patch.object(experiments, "prepare_request") as prepare, \
                patch.object(experiments, "verify_experiment_runtime") as verify, \
                patch.object(experiments, "_worker") as worker:
            with self.assertRaises(KeyboardInterrupt):
                experiments.run_experiment(Path("runtime"), Path("owned.py"), "entry", Path("args.json"),
                    Path("run"), allow_execution=True, cancel_requested=lambda: True)
            for operation in (prepare, verify, worker):
                operation.assert_not_called()

    def test_runtime_metadata_must_match_and_full_integrity_must_pass(self) -> None:
        with tempfile.TemporaryDirectory() as raw, \
                patch.object(experiments, "native_platform", return_value="linux"):
            root = Path(raw).resolve()
            path = root / "runtime.json"
            cache_pin = {"filename": "python.cwasm", "size_bytes": 5, "sha256": "1" * 64}
            manifest = {
                "schema_version": 2, "build": "owned", "commit": "owned",
                "install_dir": "installed",
                "assets": [{"filename": name, "sha256": digest}
                           for name, _, digest in (experiments.PYTHON_ARCHIVE, experiments.WHEELS["linux"])],
                "required_files": list(experiments.REQUIRED_FILES),
                "installed_files": [
                    {"filename": "guest/python.wasm", "size_bytes": 1, "sha256": "2" * 64},
                    {"filename": "guest/lib/python314.zip", "size_bytes": 1, "sha256": "4" * 64},
                    cache_pin,
                ],
                "experiment": deepcopy(experiments._metadata()),
            }
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(experiments, "verify_runtime", return_value=IntegrityResult(True, (), (), ())) as verify:
                self.assertEqual(experiments.verify_experiment_runtime(root), (root, cache_pin))
                verify.assert_called_once_with(path, root)
            with patch.object(experiments, "verify_runtime", return_value=IntegrityResult(False, (), (), ("changed",))):
                with self.assertRaisesRegex(ValueError, "integrity failed"):
                    experiments.verify_experiment_runtime(root)
            changed = []
            for key, value in (("platform", "windows"), ("python", "0"),
                               ("wasmtime", "0"), ("engine_options", {}), ("stdlib_layout", "loose")):
                item = deepcopy(manifest)
                item["experiment"][key] = value
                changed.append(item)
            wrong_asset = deepcopy(manifest)
            wrong_asset["assets"][0]["sha256"] = "3" * 64
            changed.append(wrong_asset)
            for index, item in enumerate(changed):
                with self.subTest(index=index), patch.object(experiments, "verify_runtime") as verify:
                    path.write_text(json.dumps(item), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "another platform, version or engine"):
                        experiments.verify_experiment_runtime(root)
                    verify.assert_not_called()

    def test_no_consent_precedes_source_runtime_worker_and_filesystem_io(self) -> None:
        with patch.object(experiments, "prepare_request") as prepare, \
                patch.object(experiments, "verify_experiment_runtime") as verify, \
                patch.object(experiments, "_worker") as worker, \
                patch.object(Path, "open") as opened, \
                patch.object(Path, "mkdir") as mkdir:
            for consent in (None, False):
                with self.subTest(consent=consent):
                    kwargs = {} if consent is None else {"allow_execution": consent}
                    with self.assertRaisesRegex(ValueError, "explicit --allow-execution"):
                        experiments.run_experiment(
                            Path("unopened-runtime"), Path("unopened.py"), "entry",
                            Path("unopened.json"), Path("uncreated-run"), **kwargs,
                        )
            for operation in (prepare, verify, worker, opened, mkdir):
                operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
