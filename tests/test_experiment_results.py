from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from forge8 import experiments


def guest_envelope(stdout: str) -> dict:
    return {
        "host_status": "exited", "detail": None, "output_limit": False,
        "load_seconds": 0.01, "worker_seconds": 0.02,
        "guest_output": {"stdout": stdout, "stderr": ""},
    }


def marker(payload) -> str:
    return "FORGE8_GUEST_RESULT=" + json.dumps(payload) + "\n"


class ExperimentReportedResultTests(unittest.TestCase):
    def test_return_and_exception_remain_guest_reports_with_exact_large_integers(self) -> None:
        large = 10**60 + 9007199254740993
        returned = {"return": {"value": large, "label": "你好"}}
        host = guest_envelope("ordinary guest output\n" + marker(returned))
        original = deepcopy(host)
        self.assertEqual(experiments.reported_result(host), returned)
        self.assertIs(type(experiments.reported_result(host)["return"]["value"]), int)
        self.assertEqual(host, original)
        for phase in ("module_initialization", "call", "serialization"):
            with self.subTest(phase=phase):
                payload = {"exception": "ValueError", "message": "owned error", "phase": phase}
                self.assertEqual(experiments.reported_result(guest_envelope(marker(payload))), payload)

    def test_untrusted_phase_and_other_invalid_shapes_return_none_without_raising(self) -> None:
        invalid = [
            {"exception": "ValueError", "message": "owned", "phase": phase}
            for phase in ({}, [], None, True, 1, "unknown")
        ] + [
            [], {}, None, {"return": 1, "extra": True},
            {"exception": [], "message": "owned", "phase": "call"},
            {"exception": "ValueError", "message": {}, "phase": "call"},
            {"exception": "ValueError", "phase": "call"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertIsNone(experiments.reported_result(guest_envelope(marker(payload))))

    def test_missing_duplicate_nonterminal_and_malformed_markers_are_not_selected(self) -> None:
        valid = marker({"return": 3})
        for stdout in ("", "ordinary output\n", valid + valid,
                       valid + "later guest output\n", "FORGE8_GUEST_RESULT={\n"):
            with self.subTest(stdout=stdout):
                host = guest_envelope(stdout)
                original = deepcopy(host)
                self.assertIsNone(experiments.reported_result(host))
                self.assertEqual(host, original)

    def test_nonfinite_and_duplicate_key_guest_json_are_not_selected(self) -> None:
        invalid = ['{"return":1,"return":2}', '{"return":{"value":1,"value":2}}']
        invalid += ['{"return":' + number + '}' for number in
                    ("NaN", "Infinity", "-Infinity", "1e999", "-1e999")]
        for payload in invalid:
            with self.subTest(payload=payload):
                stdout = "FORGE8_GUEST_RESULT=" + payload + "\n"
                self.assertIsNone(experiments.reported_result(guest_envelope(stdout)))

    def test_output_limit_trap_and_nonzero_exit_do_not_promote_an_earlier_marker(self) -> None:
        self.assertIsNone(experiments.reported_result(None))
        for status, detail, limited in (("exited", None, True), ("trap", "INTERRUPT", False),
                                        ("guest_exit", 1, False), ("guest_exit", 0, True)):
            with self.subTest(status=status, detail=detail, limited=limited):
                host = guest_envelope(marker({"return": "not selected"}))
                host.update(host_status=status, detail=detail, output_limit=limited)
                self.assertIsNone(experiments.reported_result(host))


class ExperimentManifestAdmissionTests(unittest.TestCase):
    def test_special_hardlinked_and_oversized_manifest_is_rejected_before_opening(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "runtime.json"
            manifest.write_bytes(b"{}")
            real_lstat = Path.lstat
            for mode, links, size in (
                (stat.S_IFIFO, 1, 0), (stat.S_IFCHR, 1, 0), (stat.S_IFDIR, 1, 0),
                (stat.S_IFREG, 2, 2), (stat.S_IFREG, 1, 512 * 1024 + 1),
            ):
                with self.subTest(mode=mode, links=links, size=size):
                    metadata = SimpleNamespace(st_mode=mode | 0o600, st_nlink=links, st_size=size)

                    def lstat(path, *args, **kwargs):
                        return metadata if path == manifest else real_lstat(path, *args, **kwargs)

                    with patch.object(Path, "lstat", lstat), \
                            patch.object(experiments, "_artifact_path", return_value=manifest), \
                            patch.object(experiments, "load_manifest") as load, \
                            patch.object(experiments, "verify_runtime") as verify, \
                            patch.object(Path, "open") as opened:
                        with self.assertRaisesRegex(ValueError, "bounded regular single-link file"):
                            experiments.verify_experiment_runtime(root)
                        load.assert_not_called()
                        verify.assert_not_called()
                        opened.assert_not_called()


class ExperimentCompletedReportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "owned.py"
        self.inputs = self.root / "input.json"
        self.runtime = self.root / "trusted-runtime"
        self.runtime.mkdir()
        self.code = b"raise RuntimeError('source must never execute in these tests')\r\ndef entry(value):\r\n    return value\r\n"
        self.raw_input = b' {"args":[7],"kwargs":{}}\n'
        self.source.write_bytes(self.code)
        self.inputs.write_bytes(self.raw_input)
        self.pin = {"filename": "python.cwasm", "size_bytes": 5, "sha256": "1" * 64}
        self.host = guest_envelope(marker({"return": 7}))

    def run_mocked(self, name: str, *, after_worker=None, runtime_error=None) -> dict:
        run_root = self.root / name

        def worker(root, directory, operation, payload):
            self.assertEqual((root, directory, operation), (self.runtime, run_root, "run"))
            self.assertEqual(payload, {
                "request": {"source": self.code.decode("utf-8"), "entry": "entry",
                            "input": {"args": [7], "kwargs": {}}},
                "cache_pin": self.pin,
            })
            if after_worker is not None:
                after_worker()
            return SimpleNamespace(status="passed", duration_seconds=0.125), deepcopy(self.host)

        verified = (self.runtime, self.pin)
        checks = [verified, runtime_error if runtime_error is not None else verified]
        with patch.object(experiments, "native_platform", return_value="linux"), \
                patch.object(experiments, "verify_experiment_runtime", side_effect=checks) as verify, \
                patch.object(experiments, "_worker", side_effect=worker) as mocked_worker, \
                patch("subprocess.Popen", side_effect=AssertionError("no process may start")):
            report = experiments.run_experiment(
                self.runtime, self.source, "entry", self.inputs, run_root, allow_execution=True,
            )
        mocked_worker.assert_called_once()
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(json.loads((run_root / "experiment.json").read_text(encoding="utf-8")), report)
        captured = json.loads((run_root / "input.json").read_text(encoding="utf-8"))
        self.assertEqual(captured["identity"], report["identity"])
        self.assertEqual(captured["request"]["source"], self.code.decode("utf-8"))
        self.assertEqual(report["identity"]["source_sha256"], hashlib.sha256(self.code).hexdigest())
        self.assertEqual(report["identity"]["input_sha256"], hashlib.sha256(self.raw_input).hexdigest())
        self.assertEqual(report["runtime"]["cache"], self.pin)
        self.assertEqual(report["execution"], self.host)
        self.assertEqual(report["process_status"], "passed")
        return report

    def test_unchanged_files_preserve_original_identity_cache_and_guest_report(self) -> None:
        with patch.object(experiments.time, "monotonic", side_effect=[100.0, 102.5]):
            report = self.run_mocked("unchanged")
        self.assertTrue(report["source_unchanged"])
        self.assertTrue(report["runtime_unchanged"])
        self.assertEqual(report["postcheck_errors"], [])
        self.assertEqual(report["reported_result"], {"return": 7})
        self.assertEqual(report["elapsed_seconds"], 2.5)
        self.assertEqual(report["duration_seconds"], 0.125)

    def test_postrun_syntax_change_retains_completed_report_without_reparsing(self) -> None:
        report = self.run_mocked("syntax-change", after_worker=lambda: self.source.write_bytes(b"def entry(:\n"))
        self.assertFalse(report["source_unchanged"])
        self.assertTrue(report["runtime_unchanged"])
        self.assertEqual(report["reported_result"], {"return": 7})

    def test_postrun_deleted_source_or_input_retains_completed_report(self) -> None:
        for index, path in enumerate((self.source, self.inputs)):
            with self.subTest(path=path.name):
                self.source.write_bytes(self.code)
                self.inputs.write_bytes(self.raw_input)
                report = self.run_mocked(f"deleted-{index}", after_worker=path.unlink)
                self.assertFalse(report["source_unchanged"])
                self.assertTrue(report["runtime_unchanged"])
                self.assertEqual(report["reported_result"], {"return": 7})
                self.assertTrue(report["postcheck_errors"])

    def test_postrun_malformed_json_retains_completed_report_without_reparsing(self) -> None:
        report = self.run_mocked("invalid-json", after_worker=lambda: self.inputs.write_bytes(b'{"args":['))
        self.assertFalse(report["source_unchanged"])
        self.assertTrue(report["runtime_unchanged"])
        self.assertEqual(report["reported_result"], {"return": 7})

    def test_runtime_postcheck_failure_retains_raw_report_but_no_selected_result(self) -> None:
        report = self.run_mocked("runtime-changed", runtime_error=ValueError("owned runtime changed"))
        self.assertTrue(report["source_unchanged"])
        self.assertFalse(report["runtime_unchanged"])
        self.assertIsNone(report["reported_result"])
        self.assertEqual(report["postcheck_errors"], [
            "Runtime integrity could not be reconfirmed after execution",
        ])


if __name__ == "__main__":
    unittest.main()
