"""Pure retained-trial fixtures. No file, subprocess, model or target execution."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from forge8 import experiments


def encoded(value, **options):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, **options).encode("utf-8")


def sha(value):
    return hashlib.sha256(value).hexdigest()


class TrialComparisonTests(unittest.TestCase):
    def setUp(self):
        native = patch.object(experiments, "native_platform", return_value="linux")
        native.start()
        self.addCleanup(native.stop)

    def fixture(self, result=None, *, identifier="trial-a", trace=False):
        source = b"def probe(value=None):\n    return value\nraise RuntimeError('NEVER EXECUTE TARGET')\n"
        input_text = '{\n  "args": [9007199254740993], "kwargs": {}\n}\n'
        run_root = Path(__file__).absolute().parent / "uncreated-trial-fixture" / identifier
        target = {"file": "0", "path": "main.py", "version": "a" * 64, "entry": "probe",
            "source_sha256": sha(source), "source_bytes": len(source)}
        cache = {"filename": "python.cwasm", "size_bytes": 123, "sha256": "c" * 64}
        metadata = deepcopy(experiments._metadata())
        manifest = {"schema_version": 2, "build": "CPython3.14.7-Wasmtime48",
            "commit": "823f0323ee6ec1402088b73bce1a38473cac36dc", "install_dir": "installed",
            "assets": [{"filename": name, "sha256": digest} for name, _, digest in
                (experiments.PYTHON_ARCHIVE, experiments.WHEELS["linux"])],
            "required_files": list(experiments.REQUIRED_FILES), "experiment": metadata,
            "installed_files": [{"filename": "guest/python.wasm", "size_bytes": 456, "sha256": "d" * 64},
                {"filename": "guest/lib/python314.zip", "size_bytes": 789, "sha256": "e" * 64}, cache]}
        result = {"return": 9007199254740993} if result is None else deepcopy(result)
        identity = {"source_sha256": target["source_sha256"], "source_bytes": len(source),
            "input_sha256": sha(input_text.encode("utf-8")), "input_bytes": len(input_text.encode("utf-8")), "entry": "probe"}
        if trace:
            identity["trace_lines"] = True
        report = {"schema_version": 1, "kind": "forge8.experiment", "identity": identity,
            "runtime": {**deepcopy(metadata), "cache": deepcopy(cache)},
            "source_unchanged": True, "runtime_unchanged": True, "postcheck_errors": [],
            "process_status": "passed", "duration_seconds": 0.1, "elapsed_seconds": 0.2,
            "execution": {"host_status": "exited", "detail": None, "output_limit": False,
                "load_seconds": 0.05, "worker_seconds": 0.1,
                "guest_output": {"stdout": "PRIVATE_DIAGNOSTIC\nFORGE8_GUEST_RESULT=" + encoded(result).decode("utf-8") + "\n", "stderr": "PRIVATE_STDERR"}},
            "reported_result": result, "run_root": str(run_root), "notice": "PRIVATE_REPORT_NOTICE"}
        if trace:
            report["reported_trace"] = None  # Trace absence does not invalidate an ordinary result comparison.
        return {"target": target, "identifier": identifier, "input_text": input_text, "report": report,
            "manifest": manifest, "source": source, "run_root": run_root, "trace": trace}

    def retain(self, fixture, *, report_bytes=None, runtime_manifest=None):
        with patch.object(experiments, "_file", side_effect=AssertionError("pure capture read a file")), \
                patch.object(experiments, "_worker", side_effect=AssertionError("pure capture started a worker")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("pure capture read a path")):
            return experiments.retain_trial_result(fixture["target"], fixture["identifier"], fixture["input_text"],
                fixture["report"], encoded(fixture["report"]) if report_bytes is None else report_bytes,
                encoded(fixture["manifest"]) if runtime_manifest is None else runtime_manifest,
                fixture["source"], fixture["run_root"], trace_lines=fixture["trace"])

    def test_public_view_preserves_exact_input_identity_and_detaches_all_mutable_data(self):
        fixture = self.fixture()
        original = deepcopy(fixture)
        record = self.retain(fixture)
        self.assertIs(type(record), bytes)
        self.assertLessEqual(len(record), 256 * 1024)
        view = experiments.trial_result_view(record)
        self.assertEqual(set(view), {"id", "version", "file", "path", "entry", "source_sha256", "source_bytes",
            "input_text", "input_sha256", "runtime_sha256", "report_sha256", "result_text", "trace_lines"})
        self.assertEqual(view["input_text"], fixture["input_text"])
        self.assertEqual(view["input_sha256"], sha(fixture["input_text"].encode("utf-8")))
        self.assertEqual(view["source_sha256"], sha(fixture["source"]))
        self.assertEqual(view["runtime_sha256"], sha(encoded(fixture["manifest"])))
        self.assertEqual(view["report_sha256"], sha(encoded(fixture["report"])))
        self.assertIn("9007199254740993", view["result_text"])
        self.assertIs(view["trace_lines"], False)
        self.assertLessEqual(len(encoded(view)), 128 * 1024)
        for private in ("PRIVATE_DIAGNOSTIC", "PRIVATE_STDERR", "PRIVATE_REPORT_NOTICE", str(fixture["run_root"])):
            self.assertNotIn(private, json.dumps(view))
        self.assertEqual(fixture, original)
        expected = deepcopy(view)
        fixture["target"]["entry"] = "other"
        fixture["report"]["reported_result"]["return"] = False
        fixture["manifest"]["installed_files"].clear()
        view["input_text"] = "NEW_DRAFT"
        self.assertEqual(experiments.trial_result_view(record), expected)

    def test_typed_json_comparison_preserves_numbers_container_order_and_exceptions(self):
        exception = {"exception": "ValueError", "message": "owned", "phase": "call"}
        cases = [(True, 1, "different"), (1, 1.0, "different"), (False, 0.0, "different"),
            (9007199254740993, 9007199254740992, "different"), (10**300, 10**300, "same"),
            ({"b": [1, True], "a": 2}, {"a": 2, "b": [1, True]}, "same"),
            ([1, 2], [2, 1], "different"), ({"v": None}, {"v": None}, "same")]
        for left, right, expected in cases:
            with self.subTest(left=left, right=right):
                baseline = self.retain(self.fixture({"return": left}))
                current = self.retain(self.fixture({"return": right}, identifier="trial-b"))
                self.assertEqual(experiments.compare_trial_results(baseline, current), (expected, None))
        for right, expected in ((exception, "same"), ({**exception, "phase": "serialization"}, "different"),
                ({**exception, "message": "other"}, "different"), ({"return": "owned"}, "different")):
            with self.subTest(right=right):
                self.assertEqual(experiments.compare_trial_results(self.retain(self.fixture(exception)),
                    self.retain(self.fixture(right, identifier="trial-b"))), (expected, None))

    def test_input_changes_are_allowed_but_the_submitted_bytes_remain_distinct(self):
        first, second = self.fixture(), self.fixture(identifier="trial-b")
        second["input_text"] = '{"args":[7],"kwargs":{}}'
        raw = second["input_text"].encode("utf-8")
        second["report"]["identity"].update(input_sha256=sha(raw), input_bytes=len(raw))
        baseline, current = self.retain(first), self.retain(second)
        self.assertNotEqual(experiments.trial_result_view(baseline)["input_sha256"], experiments.trial_result_view(current)["input_sha256"])
        self.assertEqual(experiments.compare_trial_results(baseline, current), ("same", None), "different inputs can report the same value; this is not equivalence")

    def test_unavailable_reasons_distinguish_absence_same_run_source_runtime_and_trace(self):
        baseline = self.retain(self.fixture())
        self.assertEqual(experiments.compare_trial_results(None, None), ("unavailable", "no_baseline"))
        self.assertEqual(experiments.compare_trial_results(baseline, None), ("unavailable", "current_unavailable"))
        self.assertEqual(experiments.compare_trial_results(baseline, baseline), ("unavailable", "same_run"))
        for field, value in (("file", "1"), ("path", "other.py"), ("version", "b" * 64), ("entry", "other")):
            changed = self.fixture(identifier="trial-b")
            changed["target"][field] = value
            if field == "entry":
                changed["report"]["identity"]["entry"] = value
            with self.subTest(field=field):
                self.assertEqual(experiments.compare_trial_results(baseline, self.retain(changed)), ("unavailable", "source_changed"))
        changed = self.fixture(identifier="trial-b")
        changed["source"] += b"# another captured source\n"
        for value in (changed["target"], changed["report"]["identity"]):
            value.update(source_sha256=sha(changed["source"]), source_bytes=len(changed["source"]))
        self.assertEqual(experiments.compare_trial_results(baseline, self.retain(changed)), ("unavailable", "source_changed"))
        runtime = self.fixture(identifier="trial-b")
        runtime["manifest"]["installed_files"][-1]["sha256"] = "f" * 64
        runtime["report"]["runtime"]["cache"]["sha256"] = "f" * 64
        self.assertEqual(experiments.compare_trial_results(baseline, self.retain(runtime)), ("unavailable", "runtime_changed"))
        self.assertEqual(experiments.compare_trial_results(baseline, self.retain(self.fixture(identifier="trial-b", trace=True))),
            ("unavailable", "trace_mode_changed"))

    def test_returned_report_and_actual_report_bytes_must_agree_with_json_types(self):
        fixture = self.fixture({"return": 1})
        raw = encoded(fixture["report"])
        fixture["report"]["reported_result"] = {"return": True}
        with self.assertRaises(ValueError):
            self.retain(fixture, report_bytes=raw)
        fixture = self.fixture()
        with self.assertRaises(ValueError):
            self.retain(fixture, report_bytes=b'{"schema_version":1,' + encoded(fixture["report"])[1:])
        with self.assertRaises(ValueError):
            self.retain(fixture, report_bytes=b"{malformed")
        reordered = dict(reversed(list(fixture["report"].items())))
        self.assertEqual(experiments.trial_result_view(self.retain(fixture, report_bytes=encoded(reordered)))["report_sha256"], sha(encoded(reordered)))

    def test_truthy_flags_bad_completion_and_missing_schema_fields_are_rejected(self):
        changes = [("schema_version", True), ("schema_version", 1.0), ("kind", "forge8.paired_experiment"),
            ("source_unchanged", 1), ("runtime_unchanged", "true"), ("postcheck_errors", None),
            ("postcheck_errors", ["uncertain"]), ("process_status", "timed_out")]
        for field, value in changes:
            fixture = self.fixture()
            fixture["report"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.retain(fixture)
        for change in ({"output_limit": 0}, {"output_limit": True}, {"host_status": "trap"},
                {"host_status": "guest_exit", "detail": False}, {"host_status": "guest_exit", "detail": 1}):
            fixture = self.fixture()
            fixture["report"]["execution"].update(change)
            with self.subTest(execution=change), self.assertRaises(ValueError):
                self.retain(fixture)
        fixture = self.fixture()
        fixture["report"]["execution"].update(host_status="guest_exit", detail=0)
        self.assertIsInstance(self.retain(fixture), bytes)
        for field in ("identity", "runtime", "execution", "reported_result"):
            fixture = self.fixture()
            del fixture["report"][field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                self.retain(fixture)

    def test_exact_source_input_target_trace_and_run_identity_are_required(self):
        changes = [lambda f: f["target"].update(source_bytes=True), lambda f: f["target"].update(source_sha256="f" * 64),
            lambda f: f["target"].update(module_set={}), lambda f: f.update(source=f["source"] + b"# drift"),
            lambda f: f.update(input_text=f["input_text"] + " "), lambda f: f.update(identifier="../bad"),
            lambda f: f.update(identifier="trial\n"), lambda f: f["report"].update(run_root="different-run"),
            lambda f: f["report"]["identity"].update(input_bytes=1.0), lambda f: f["report"]["identity"].update(entry="other"),
            lambda f: f["report"]["identity"].update(trace_lines=False), lambda f: f.update(trace=True)]
        for index, change in enumerate(changes):
            fixture = self.fixture()
            change(fixture)
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.retain(fixture)
        for value in (None, 0, 1, "false"):
            fixture = self.fixture()
            fixture["trace"] = value
            with self.subTest(trace=value), self.assertRaises(ValueError):
                self.retain(fixture)
        fixture = self.fixture(trace=True)
        fixture["report"]["identity"]["trace_lines"] = 1
        with self.assertRaises(ValueError):
            self.retain(fixture)
        for input_text in ('{"args":[],"args":[],"kwargs":{}}', '{"args":[NaN],"kwargs":{}}',
                '{"args":{},"kwargs":{}}', '{"args":[],"kwargs":{},"extra":true}'):
            fixture = self.fixture()
            fixture["input_text"] = input_text
            raw_input = input_text.encode("utf-8")
            fixture["report"]["identity"].update(input_sha256=sha(raw_input), input_bytes=len(raw_input))
            with self.subTest(input_text=input_text), self.assertRaises(ValueError):
                self.retain(fixture)

    def test_manifest_and_cache_must_match_without_duplicate_metadata_keys(self):
        changes = [lambda f: f["manifest"].update(install_dir="other"), lambda f: f["manifest"].update(experiment={}),
            lambda f: f["manifest"]["experiment"].update(protocol=True),
            lambda f: f["manifest"].update(installed_files=[]),
            lambda f: f["manifest"]["installed_files"].append(deepcopy(f["manifest"]["installed_files"][-1])),
            lambda f: f["report"]["runtime"]["cache"].update(sha256="f" * 64),
            lambda f: f["report"]["runtime"].update(protocol=True)]
        for index, change in enumerate(changes):
            fixture = self.fixture()
            change(fixture)
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.retain(fixture)
        fixture = self.fixture()
        with self.assertRaises(ValueError):
            self.retain(fixture, runtime_manifest=b'{"install_dir":"installed",' + encoded(fixture["manifest"])[1:])

    def test_missing_malformed_and_oversized_guest_output_are_value_errors(self):
        outputs = [None, [], {}, {"stdout": ""}, {"stdout": "", "stderr": None},
            {"stdout": 1, "stderr": ""}, {"stdout": "", "stderr": "", "extra": ""},
            {"stdout": "x" * (3 * experiments.OUTPUT_BYTES), "stderr": "x"}]
        for output in outputs:
            fixture = self.fixture()
            fixture["report"]["execution"]["guest_output"] = output
            with self.subTest(output_type=type(output).__name__), self.assertRaises(ValueError):
                self.retain(fixture)
        fixture = self.fixture()
        del fixture["report"]["execution"]["guest_output"]
        with self.assertRaises(ValueError):
            self.retain(fixture)

    def test_guest_result_is_reparsed_not_trusted_from_the_report_field(self):
        for stdout in ("", "FORGE8_GUEST_RESULT={}\n", "FORGE8_GUEST_RESULT={malformed\n",
                "FORGE8_GUEST_RESULT={\"return\":1}\nFORGE8_GUEST_RESULT={\"return\":1}\n",
                "FORGE8_GUEST_RESULT={\"return\":1}\ntrailing output\n", "FORGE8_GUEST_RESULT={\"return\":NaN}\n"):
            fixture = self.fixture({"return": 1})
            fixture["report"]["execution"]["guest_output"]["stdout"] = stdout
            with self.subTest(stdout=stdout), self.assertRaises(ValueError):
                self.retain(fixture)
        fixture = self.fixture({"return": 1})
        fixture["report"]["reported_result"] = {"return": True}
        with self.assertRaises(ValueError):
            self.retain(fixture)
        for result in (None, {}, {"return": 1, "exception": "ValueError"}, {"exception": "ValueError", "message": "x", "phase": "unknown"}):
            fixture = self.fixture()
            fixture["report"]["reported_result"] = result
            with self.subTest(result=result), self.assertRaises(ValueError):
                self.retain(fixture)

    def test_record_and_view_limits_fail_whole_and_invisible_text_stays_inert(self):
        fixture = self.fixture()
        fixture["report"]["reported_result"] = {"return": "中文\u202e\x1b\ud800"}
        # Encode the synthetic report with escaped surrogates, as the real private JSON writer does.
        fixture["report"]["execution"]["guest_output"]["stdout"] = "FORGE8_GUEST_RESULT=" + json.dumps(fixture["report"]["reported_result"]) + "\n"
        raw = json.dumps(fixture["report"], ensure_ascii=True).encode("ascii")
        text = experiments.trial_result_view(self.retain(fixture, report_bytes=raw))["result_text"]
        self.assertIn("中文", text)
        for escape in ("\\u202e", "\\u001b", "\\ud800"):
            self.assertIn(escape, text)
        with self.assertRaisesRegex(ValueError, "display exceeds 128 KiB"):
            self.retain(self.fixture({"return": "x" * (128 * 1024)}))
        with self.assertRaisesRegex(ValueError, "capture exceeds 256 KiB"):
            self.retain(self.fixture({"return": "界" * 24000}))
        fixture = self.fixture()
        for options in ({"report_bytes": b" " * (2 * 1024**2 + 1)}, {"runtime_manifest": b" " * (512 * 1024 + 1)}):
            with self.subTest(options=list(options)), self.assertRaises(ValueError):
                self.retain(fixture, **options)
        fixture["source"] = b"x" * (65536 + 1)
        with self.assertRaises(ValueError):
            self.retain(fixture)
