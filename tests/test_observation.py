"""Only dynamically created, trusted own fixtures are executed here."""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import py_compile
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch
from uuid import uuid4

from forge8.observation import capture_test, MAX_EVENTS, MAX_REPORT_BYTES


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.names = []
        self.addCleanup(lambda: [sys.modules.pop(name, None) for name in self.names])

    def fixture(self, body, prelude="", *, folder=""):
        module = "observe_fixture_" + uuid4().hex
        self.names.append(module)
        path = self.root / folder / (module + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        source = "import unittest\n" + textwrap.dedent(prelude) + "\nclass Case(unittest.TestCase):\n"
        source += textwrap.indent(textwrap.dedent(body), "    ")
        path.write_text(source, encoding="utf-8")
        importlib.invalidate_caches()
        return module + ".Case.test_case", (path.relative_to(self.root).as_posix(),)

    def observe(self, body, prelude="", **kwargs):
        selector, paths = self.fixture(body, prelude, **kwargs)
        return capture_test(self.root, selector, paths)

    def test_success_positions_parent_and_no_values_or_output_interception(self):
        helper = "observe_bridge_" + uuid4().hex
        self.names.append(helper)
        (self.root / (helper + ".py")).write_text("def bridge(fn):\n    return fn()\n")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            report = self.observe("""
                def test_case(self):
                    self.assertEqual(bridge(local), 'PRIVATE_ARGUMENT_VALUE')
                    print('ordinary test output')
            """, f"from {helper} import bridge\ndef local():\n    return 'PRIVATE_ARGUMENT_VALUE'\n", folder="tests")
        self.assertEqual(output.getvalue(), "ordinary test output\n")
        self.assertTrue(report["test_passed"])
        self.assertTrue(report["complete"])
        self.assertEqual(report["kind"], "forge8.observation")
        calls = [e for e in report["events"] if e["event"] == "call"]
        parent = next(e for e in calls if e["function"].split(".")[-1] == "test_case")
        child = next(e for e in calls if e["function"] == "local")
        self.assertEqual(child["parent_call_id"], parent["call_id"])
        self.assertEqual({e["path"] for e in report["events"]}, set(report["source_before"]))
        self.assertNotIn("PRIVATE_ARGUMENT_VALUE", json.dumps(report))
        for event in report["events"]:
            self.assertEqual(set(event), {"event", "call_id", "parent_call_id", "path", "function", "first_line", "line"})

    def test_failure_and_error_are_complete_but_never_passed_or_serialized(self):
        for statement, kind in (("self.fail('PRIVATE_ERROR_PAYLOAD')", "failed"),
                                ("raise RuntimeError('PRIVATE_ERROR_PAYLOAD')", "errors")):
            with self.subTest(kind=kind):
                report = self.observe("def test_case(self):\n    " + statement)
                self.assertFalse(report["test_passed"])
                self.assertTrue(report["complete"])
                self.assertEqual(report["counts"][kind], 1)
                self.assertNotIn("PRIVATE_ERROR_PAYLOAD", json.dumps(report))
                self.assertTrue(any(e["event"] == "exception" for e in report["events"]))

    def test_skip_and_expected_outcomes_do_not_falsely_pass(self):
        for decorator, body, kind in (
            ("@unittest.skip('PRIVATE_REASON')", "pass", "skipped"),
            ("@unittest.expectedFailure", "self.fail('PRIVATE_REASON')", "expected_failures"),
            ("@unittest.expectedFailure", "pass", "unexpected_successes"),
        ):
            with self.subTest(kind=kind):
                report = self.observe(decorator + "\ndef test_case(self):\n    " + body)
                self.assertFalse(report["test_passed"])
                self.assertTrue(report["complete"])
                self.assertEqual(report["counts"][kind], 1)
                self.assertNotIn("PRIVATE_REASON", json.dumps(report))

    def test_subtests_success_failure_error_and_skip_counts(self):
        report = self.observe("""
            def test_case(self):
                with self.subTest(secret='PRIVATE_SUBTEST'):
                    self.assertTrue(True)
                with self.subTest():
                    self.fail('PRIVATE_SUBTEST')
                with self.subTest():
                    raise RuntimeError('PRIVATE_SUBTEST')
                with self.subTest():
                    self.skipTest('PRIVATE_SUBTEST')
        """)
        self.assertTrue(report["complete"])
        self.assertFalse(report["test_passed"])
        for key in ("subtests_passed", "subtests_failed", "subtests_errors", "skipped"):
            self.assertEqual(report["counts"][key], 1)
        self.assertNotIn("PRIVATE_SUBTEST", json.dumps(report))
        passed = self.observe("def test_case(self):\n    with self.subTest():\n        self.assertTrue(True)")
        self.assertTrue(passed["test_passed"])

    def test_invalid_selector_or_missing_module_is_rejected_before_import(self):
        selector, paths = self.fixture("def test_case(self): pass")
        with patch("forge8.observation.importlib.import_module") as imported:
            for bad in ("", "x.factory", "x.Case.run", "x.Case.test_case()", "missing.Case.test_case"):
                report = capture_test(self.root, bad, paths)
                self.assertFalse(report["complete"])
                self.assertEqual(report["tests_run"], 0)
            for bad_paths in ((), paths * 5, list(paths), paths + paths):
                self.assertIsNotNone(capture_test(self.root, selector, bad_paths)["error"])
            imported.assert_not_called()

    def test_source_admission_precedes_import(self):
        selector, paths = self.fixture("def test_case(self): pass")
        for name, data in (("large.py", b"#" * (65536 + 1)), ("bad.py", b"\xff"),
                           ("zero.py", b"\0"), ("separator.py", "#\u2028".encode()),
                           (".env.py", b"# hidden credential file")):
            (self.root / name).write_bytes(data)
            with self.subTest(name=name), patch("forge8.observation.importlib.import_module") as imported:
                report = capture_test(self.root, selector, paths + (name,))
                self.assertFalse(report["complete"])
                self.assertIsNotNone(report["error"])
                imported.assert_not_called()
        (self.root / "directory.py").mkdir()
        for name in ("directory.py", "../escape.py", str(self.root / paths[0])):
            with patch("forge8.observation.importlib.import_module") as imported:
                self.assertIsNotNone(capture_test(self.root, selector, paths + (name,))["error"])
                imported.assert_not_called()

    def test_hardlink_rejected_before_import(self):
        selector, paths = self.fixture("def test_case(self): pass")
        try:
            os.link(self.root / paths[0], self.root / "alias.py")
        except OSError as exc:
            self.skipTest("Hard links unavailable: " + type(exc).__name__)
        with patch("forge8.observation.importlib.import_module") as imported:
            self.assertIsNotNone(capture_test(self.root, selector, paths)["error"])
            imported.assert_not_called()

    def test_symlink_rejected_before_import(self):
        selector, paths = self.fixture("def test_case(self): pass")
        try:
            (self.root / "alias.py").symlink_to(self.root / paths[0])
        except OSError as exc:
            self.skipTest("Symlink privilege unavailable: " + type(exc).__name__)
        with patch("forge8.observation.importlib.import_module") as imported:
            self.assertIsNotNone(capture_test(self.root, selector, paths + ("alias.py",))["error"])
            imported.assert_not_called()

    def test_imported_location_mismatch_never_runs_test_or_replaces_cache(self):
        selector, paths = self.fixture("def test_case(self): pass")
        module = selector.split(".")[0]
        wrong = types.ModuleType(module)
        wrong.__file__ = str(self.root / "other.py")
        sys.modules[module] = wrong
        report = capture_test(self.root, selector, paths)
        self.assertIn("not the selected source", report["error"])
        self.assertEqual(report["tests_run"], 0)
        self.assertIs(sys.modules[module], wrong)

    def test_custom_run_cannot_report_a_pass_without_selected_method_execution(self):
        report = self.observe("""
            def run(self, result):
                result.startTest(self)
                result.addSuccess(self)
                result.stopTest(self)
            def test_case(self):
                self.fail('this method was never called')
        """)
        self.assertEqual(report["counts"]["passed"], 1)
        self.assertFalse(report["test_passed"])
        self.assertFalse(report["complete"])
        self.assertFalse(report["method_observed"])

    def test_cached_same_path_old_code_is_not_pinned_to_new_source(self):
        selector, paths = self.fixture("def test_case(self):\n    self.assertTrue(True)\n")
        module = selector.split(".")[0]
        with patch.object(sys, "path", [str(self.root), *sys.path]):
            imported = importlib.import_module(module)
        path = self.root / paths[0]
        path.write_text(path.read_text().replace("self.assertTrue(True)", "self.assertTrue(False)"))
        report = capture_test(self.root, selector, paths)
        self.assertIs(sys.modules[module], imported)
        self.assertTrue(report["source_unchanged"])
        self.assertFalse(report["complete"])
        self.assertTrue(report["unmatched_source_code"])

    def test_matching_cached_dependency_and_nested_codes_are_accepted(self):
        dependency = "observe_dependency_" + uuid4().hex
        self.names.append(dependency)
        name = dependency + ".py"
        (self.root / name).write_text("def helper():\n    def inner():\n        return 7\n    return inner()\n")
        importlib.invalidate_caches()
        with patch.object(sys, "path", [str(self.root), *sys.path]):
            cached = importlib.import_module(dependency)
        selector, paths = self.fixture("def test_case(self):\n    self.assertEqual(helper(), 7)\n",
                                       f"from {dependency} import helper")
        report = capture_test(self.root, selector, paths + (name,))
        self.assertTrue(report["complete"])
        self.assertTrue(report["test_passed"])
        self.assertIs(sys.modules[dependency], cached)
        self.assertIn("inner", {e["function"].split(".")[-1] for e in report["events"]})

    def test_cached_filename_alias_keeps_events_and_rejects_stale_code(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                selector, paths = self.fixture("def test_case(self):\n    self.assertTrue(True)\n")
                module_name = selector.split(".")[0]
                with patch.object(sys, "path", [str(self.root), *sys.path]):
                    module = importlib.import_module(module_name)
                path = (self.root / paths[0]).resolve()
                alias = str(path.parent / "short-spelling" / path.name)
                method = module.Case.test_case
                method.__code__ = method.__code__.replace(co_filename=alias)
                if stale:
                    path.write_text(path.read_text().replace("self.assertTrue(True)", "self.assertTrue(False)"))
                realpath = os.path.realpath

                def resolve(value, **kwargs):
                    return str(path) if os.fspath(value) == alias else realpath(value, **kwargs)

                with patch.object(os.path, "realpath", side_effect=resolve):
                    report = capture_test(self.root, selector, paths)
                self.assertTrue(report["source_unchanged"])
                self.assertEqual(report["complete"], not stale)
                self.assertEqual(report["unmatched_source_code"], stale)
                self.assertEqual(report["method_observed"], not stale)
                self.assertEqual(bool(report["events"]), not stale)

    def test_compile_failure_and_code_object_cap_precede_import(self):
        selector, paths = self.fixture("def test_case(self): pass")
        name = "oversized_codes.py"
        for content in ("def invalid syntax", "\n".join(f"def f{i}(): pass" for i in range(1001))):
            (self.root / name).write_text(content)
            with patch("forge8.observation.importlib.import_module") as imported:
                report = capture_test(self.root, selector, paths + (name,))
                self.assertFalse(report["complete"])
                self.assertIsNotNone(report["error"])
                imported.assert_not_called()

    def test_stale_same_size_mtime_bytecode_cannot_claim_current_source(self):
        selector, paths = self.fixture("def test_case(self):\n    self.assertEqual(1, 1)\n")
        path = self.root / paths[0]
        original = path.stat()
        # Explicit creation also works under -B: automatic import caching is
        # not a fixture prerequisite. Pin timestamp mode, even with SOURCE_DATE_EPOCH.
        cached = Path(py_compile.compile(str(path), doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP))
        self.assertTrue(cached.is_file())
        header = cached.read_bytes()[:16]
        self.assertEqual(header[:4], importlib.util.MAGIC_NUMBER)
        self.assertEqual(int.from_bytes(header[4:8], "little"), 0)
        self.assertEqual(int.from_bytes(header[8:12], "little"), int(original.st_mtime) & 0xFFFFFFFF)
        self.assertEqual(int.from_bytes(header[12:16], "little"), original.st_size)
        path.write_text(path.read_text().replace("self.assertEqual(1, 1)", "self.assertEqual(1, 2)"))
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(path.stat().st_size, original.st_size)
        report = capture_test(self.root, selector, paths)
        # The stale 1==1 body really ran; the current 1==2 source would fail.
        self.assertEqual(report["counts"]["passed"], 1)
        self.assertEqual(report["counts"]["failed"], 0)
        self.assertTrue(report["source_unchanged"])
        self.assertFalse(report["complete"])
        self.assertTrue(report["unmatched_source_code"])

    def test_callable_factory_not_used_and_async_methods_not_run(self):
        selector, paths = self.fixture("def test_case(self): pass")
        (self.root / paths[0]).write_text("def Case(*args):\n    raise RuntimeError('FACTORY_CALLED')\n")
        report = capture_test(self.root, selector, paths)
        self.assertIn("unittest.TestCase", report["error"])
        self.assertFalse(report["aborted"])
        for body in ("async def test_case(self): pass", "def test_case(self): yield 1"):
            report = self.observe(body)
            self.assertTrue(report["unsupported"])
            self.assertEqual(report["tests_run"], 0)

    def test_import_source_drift_stops_before_test(self):
        report = self.observe("def test_case(self): pass", """
            from pathlib import Path
            p = Path(__file__)
            p.write_text(p.read_text() + '\\n# changed\\n')
        """)
        self.assertEqual(report["tests_run"], 0)
        self.assertFalse(report["source_unchanged"])
        self.assertIn("changed during imports", report["error"])

    def test_source_change_during_test_keeps_pass_separate_from_complete(self):
        report = self.observe("""
            def test_case(self):
                p = Path(__file__)
                p.write_text(p.read_text() + '\\n# changed\\n')
        """, "from pathlib import Path")
        self.assertTrue(report["test_passed"])
        self.assertFalse(report["complete"])
        self.assertFalse(report["source_unchanged"])

    def test_replaced_trace_and_previous_trace_restoration(self):
        previous = sys.gettrace()
        def prior(frame, event, arg):
            return prior
        old_path = sys.path[:]
        sys.settrace(prior)
        try:
            report = self.observe("def test_case(self):\n    sys.settrace(None)", "import sys")
            self.assertTrue(report["test_passed"])
            self.assertTrue(report["trace_replaced"])
            self.assertFalse(report["complete"])
            self.assertIs(sys.gettrace(), prior)
            self.assertEqual(sys.path, old_path)
            aborted = self.observe("def test_case(self): pass", "raise SystemExit('PRIVATE_ABORT')")
            self.assertTrue(aborted["aborted"])
            self.assertIs(sys.gettrace(), prior)
            self.assertNotIn("PRIVATE_ABORT", json.dumps(aborted))
        finally:
            sys.settrace(previous)

    def test_generator_helper_is_skipped_and_report_explicitly_limited(self):
        report = self.observe("def test_case(self):\n    self.assertEqual(next(items()), 1)",
                              "def items():\n    yield 1\n")
        self.assertTrue(report["test_passed"])
        self.assertTrue(report["unsupported"])
        self.assertGreater(report["unsupported_calls"], 0)
        self.assertFalse(report["complete"])
        self.assertNotIn("items", {e["function"] for e in report["events"]})

    def test_event_and_byte_limits_are_bounded_and_not_complete(self):
        body = "def test_case(self):\n    for i in range(2500):\n        value = i\n"
        report = self.observe(body)
        self.assertTrue(report["test_passed"])
        self.assertTrue(report["truncated"])
        self.assertFalse(report["complete"])
        self.assertLessEqual(len(report["events"]), MAX_EVENTS)
        self.assertLessEqual(len(json.dumps(report, separators=(",", ":")).encode()), MAX_REPORT_BYTES)
        with patch("forge8.observation.MAX_REPORT_BYTES", 16384 + 1024):
            limited = self.observe(body)
        self.assertTrue(limited["truncated"])
        self.assertLess(len(limited["events"]), MAX_EVENTS)
        self.assertLessEqual(len(json.dumps(limited, separators=(",", ":")).encode()), 16384 + 1024)

    def test_native_crlf_and_cr_map_to_physical_lines(self):
        for separator in ("\r\n", "\r"):
            selector, paths = self.fixture("def test_case(self):\n    self.assertTrue(True)\n", "# 中文")
            path = self.root / paths[0]
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", separator.encode()))
            report = capture_test(self.root, selector, paths)
            self.assertTrue(report["complete"])
            self.assertTrue(report["test_passed"])
            lines = [e["line"] for e in report["events"] if e["function"].split(".")[-1] == "test_case"]
            self.assertIn(5, lines)


if __name__ == "__main__":
    unittest.main()
