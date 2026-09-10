"""Line-report protocol tests only; never execute bootstrap or supplied source."""
from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from forge8 import _experiment_worker as worker, experiments
import test_experiment_results as result_fixtures
import test_experiment_worker_modules as worker_fixtures
from test_experiment_results import guest_envelope, marker


TRACE = {"line_events": [3, 2, 3, 3], "truncated": False, "hook_intact": True}
SOURCE = "# whole module\r\ndef entry(value):\r\n    return value\r\n"


def trace_marker(value=TRACE):
    return "FORGE8_GUEST_TRACE=" + json.dumps(value) + "\n"


class TraceProtocolTests(unittest.TestCase):
    def test_order_repetition_bound_and_partial_flags_are_preserved(self):
        for value in (TRACE, {**TRACE, "line_events": []},
                      {**TRACE, "line_events": [2] * 1000, "truncated": True},
                      {**TRACE, "hook_intact": False}):
            with self.subTest(value=value):
                host = guest_envelope("ordinary output\n" + trace_marker(value) + "\n" + marker({"return": 7}))
                original = deepcopy(host)
                self.assertEqual(experiments._reported_trace(host, SOURCE), value)
                self.assertEqual(experiments.reported_result(host), {"return": 7})
                self.assertEqual(host, original)

    def test_only_physical_lf_crlf_cr_lines_define_coordinates(self):
        for separator in ("\n", "\r\n", "\r"):
            for terminal in ("", separator):
                source = separator.join(("def entry():", '    return "a\u2028b\u0085c"')) + terminal
                with self.subTest(separator=separator, terminal=terminal):
                    for line, accepted in ((1, True), (2, True), (3, False)):
                        payload = {**TRACE, "line_events": [line]}
                        host = guest_envelope(trace_marker(payload) + marker({"return": None}))
                        self.assertEqual(experiments._reported_trace(host, source), payload if accepted else None)

    def test_strict_schema_numbers_and_1000_event_limit(self):
        invalid = [None, [], {}, {**TRACE, "values": {}}, {**TRACE, "truncated": True},
                   {key: value for key, value in TRACE.items() if key != "truncated"}]
        invalid += [{**TRACE, "line_events": events} for events in
                    (None, {}, "2", [True], [False], [0], [-1], [4], [2.0], ["2"],
                     [None], [[2]], [2] * 1001)]
        invalid += [{**TRACE, key: value} for key in ("truncated", "hook_intact")
                    for value in (None, 0, 1, "false", [])]
        raws = [json.dumps(value) for value in invalid]
        raws += ['{"line_events":[2],"line_events":[3],"truncated":false,"hook_intact":true}',
                 '{"line_events":[NaN],"truncated":false,"hook_intact":true}',
                 '{"line_events":[' + "9" * 5000 + '],"truncated":false,"hook_intact":true}',
                 '{"line_events":' + "[" * 1500 + "2" + "]" * 1500
                 + ',"truncated":false,"hook_intact":true}']
        for raw in raws:
            with self.subTest(raw=raw[:100]):
                host = guest_envelope("FORGE8_GUEST_TRACE=" + raw + "\n" + marker({"return": 7}))
                self.assertIsNone(experiments._reported_trace(host, SOURCE))
                self.assertEqual(experiments.reported_result(host), {"return": 7})

    def test_marker_must_be_unique_and_immediately_before_valid_terminal_result(self):
        valid = trace_marker()
        result = marker({"return": 7})
        for stdout in (result, valid + valid + result, valid + "later output\n" + result,
                       "FORGE8_GUEST_TRACE={\n" + result, valid, valid + result + result,
                       valid + result + "later output\n", valid + "FORGE8_GUEST_RESULT={}\n"):
            with self.subTest(stdout=stdout):
                self.assertIsNone(experiments._reported_trace(guest_envelope(stdout), SOURCE))
        self.assertIsNone(experiments._reported_trace(None, SOURCE))
        for status, detail, limited in (("trap", "INTERRUPT", False),
                                       ("guest_exit", 1, False), ("exited", None, True)):
            host = guest_envelope(valid + result)
            host.update(host_status=status, detail=detail, output_limit=limited)
            with self.subTest(status=status, limited=limited):
                self.assertIsNone(experiments._reported_trace(host, SOURCE))
        for phase in ("call", "serialization"):
            payload = {"exception": "ValueError", "message": "owned", "phase": phase}
            self.assertEqual(experiments._reported_trace(
                guest_envelope(valid + marker(payload)), SOURCE), TRACE)
        initialization = {"exception": "ValueError", "message": "owned", "phase": "module_initialization"}
        host = guest_envelope(valid + marker(initialization))
        self.assertIsNone(experiments._reported_trace(host, SOURCE))
        self.assertEqual(experiments.reported_result(host), initialization)


class TraceControllerTests(unittest.TestCase):
    # Reuse owned inert bytes only, without inheriting or rerunning other tests.
    setUp = result_fixtures.ExperimentCompletedReportTests.setUp

    def run_fake(self, name, *, trace_options=None, after_worker=None,
                 runtime_error=None, process_changes=None, cancel_requested=None):
        run_root = self.root / name
        captured = {}
        process = SimpleNamespace(status="passed", ok=True, duration_seconds=.125,
                                  output_truncated=False, capture_errors=())
        for key, value in (process_changes or {}).items():
            setattr(process, key, value)

        def fake_worker(root, directory, operation, payload, **options):
            self.assertEqual((root, directory, operation), (self.runtime, run_root, "run"))
            captured.update(deepcopy(payload))
            if after_worker:
                after_worker()
            return process, deepcopy(self.host)

        verified = (self.runtime, self.pin)
        with patch.object(experiments, "native_platform", return_value="linux"), \
                patch.object(experiments, "verify_experiment_runtime", side_effect=[
                    verified, runtime_error if runtime_error is not None else verified]) as verify, \
                patch.object(experiments, "_worker", side_effect=fake_worker) as mocked_worker, \
                patch("subprocess.Popen", side_effect=AssertionError("no process may start")), \
                patch("builtins.exec", side_effect=AssertionError("no source may execute")), \
                patch("builtins.eval", side_effect=AssertionError("no source may evaluate")):
            report = experiments.run_experiment(self.runtime, self.source, "entry", self.inputs, run_root,
                allow_execution=True, cancel_requested=cancel_requested, **(trace_options or {}))
        mocked_worker.assert_called_once()
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(json.loads((run_root / "experiment.json").read_text(encoding="utf-8")), report)
        return report, captured, json.loads((run_root / "input.json").read_text(encoding="utf-8"))

    def test_true_only_forwarding_and_exact_legacy_shape_for_omitted_false(self):
        request, identity = experiments.prepare_request(self.source, "entry", self.inputs)
        self.host = guest_envelope(trace_marker() + marker({"return": 7}))
        reports = []
        for name, options in (("omitted", {}), ("false", {"trace_lines": False}),
                              ("true", {"trace_lines": True})):
            with self.subTest(name=name):
                report, sent, captured = self.run_fake(name, trace_options=options)
                tracing = options.get("trace_lines", False)
                expected_request = {**request, **({"trace_lines": True} if tracing else {})}
                expected_identity = {**identity, **({"trace_lines": True} if tracing else {})}
                self.assertEqual(sent, {"request": expected_request, "cache_pin": self.pin})
                self.assertEqual(captured, {"request": expected_request, "identity": expected_identity})
                self.assertEqual(report["identity"], expected_identity)
                self.assertEqual(report["reported_result"], {"return": 7})
                if tracing:
                    self.assertEqual(report["reported_trace"], TRACE)
                else:
                    self.assertNotIn("reported_trace", report)
                reports.append(report)
        legacy_keys = {"schema_version", "kind", "identity", "runtime", "source_unchanged",
                       "runtime_unchanged", "postcheck_errors", "process_status", "duration_seconds",
                       "execution", "run_root", "notice", "reported_result", "elapsed_seconds"}
        self.assertEqual(set(reports[0]), legacy_keys)
        self.assertEqual(set(reports[1]), legacy_keys)
        self.assertEqual(set(reports[2]), legacy_keys | {"reported_trace"})
        self.assertEqual(experiments.compare_reported_results(
            reports[0]["reported_result"], reports[2]["reported_result"]), "same")

    def test_nonboolean_or_module_mode_rejects_before_io_or_worker(self):
        options = [{"trace_lines": value} for value in (None, 0, 1, "true", [], {})]
        options += [{"trace_lines": True, key: value} for key, value in
                    (("module_root", self.root), ("module_files", []),
                     ("expected_module_set_sha256", "0" * 64))]
        with patch.object(experiments, "prepare_request") as prepare, \
                patch.object(experiments, "_read_module_files") as modules, \
                patch.object(experiments, "verify_experiment_runtime") as verify, \
                patch.object(experiments, "_worker") as execute, \
                patch.object(Path, "mkdir") as mkdir:
            for option in options:
                with self.subTest(option=option), self.assertRaises(ValueError):
                    experiments.run_experiment(self.runtime, self.source, "entry", self.inputs,
                        self.root / "never-created", allow_execution=True, **option)
            for operation in (prepare, modules, verify, execute, mkdir):
                operation.assert_not_called()

    def test_incomplete_or_changed_execution_never_publishes_trace(self):
        valid = guest_envelope(trace_marker() + marker({"return": 7}))
        variants = [
            {"after_worker": lambda: self.source.write_bytes(b"def changed(:\n")},
            {"after_worker": lambda: self.inputs.write_bytes(b"{}")},
            {"runtime_error": ValueError("owned runtime drift")},
            {"process_changes": {"status": "failed", "ok": False}},
            {"process_changes": {"output_truncated": True}},
            {"process_changes": {"capture_errors": ("owned capture failure",)}},
        ]
        for index, options in enumerate(variants):
            with self.subTest(index=index):
                self.source.write_bytes(self.code)
                self.inputs.write_bytes(self.raw_input)
                self.host = deepcopy(valid)
                report, _, _ = self.run_fake(f"bad-{index}", trace_options={"trace_lines": True}, **options)
                self.assertIsNone(report["reported_trace"])
        for index, host in enumerate((None, guest_envelope(marker({"return": 7})),
                                     guest_envelope(trace_marker() + "FORGE8_GUEST_RESULT={}\n"))):
            with self.subTest(host=index):
                self.source.write_bytes(self.code)
                self.inputs.write_bytes(self.raw_input)
                self.host = host
                report, _, _ = self.run_fake(f"absent-{index}", trace_options={"trace_lines": True})
                self.assertIsNone(report["reported_trace"])

    def test_cancellation_never_leaves_a_completed_trace_report(self):
        self.host = guest_envelope(trace_marker() + marker({"return": 7}))
        cancelled = []
        with self.assertRaises(KeyboardInterrupt):
            self.run_fake("cancelled", trace_options={"trace_lines": True},
                after_worker=lambda: cancelled.append(True), cancel_requested=lambda: bool(cancelled))
        self.assertFalse((self.root / "cancelled/experiment.json").exists())


class TraceWorkerTests(unittest.TestCase):
    setUp = worker_fixtures.ExperimentWorkerModulesTests.setUp

    def test_invalid_trace_rejected_before_bundle_read_or_runtime_loading(self):
        with patch.object(worker, "_runtime") as runtime, \
                patch.object(worker, "_module_bundle_directory") as modules:
            for value in (None, 0, 1, "true", []):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    worker.execute(self.root, {**self.request, "trace_lines": value}, self.cache_pin)
            for options, added in (({"module_bundle": self.bundle}, {}),
                                   ({}, {"module_bundle": {}})):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    worker.execute(self.root, {**self.request, **added, "trace_lines": True},
                                   self.cache_pin, **options)
            runtime.assert_not_called()
            modules.assert_not_called()

    def test_opt_in_reaches_guest_argv_without_new_mounts_limits_or_output_fields(self):
        runtime = MagicMock()
        runtime.ExitTrap = type("OwnedExitTrap", (Exception,), {})
        runtime.Trap = type("OwnedTrap", (Exception,), {})
        runtime.Module.deserialize.return_value.__enter__.return_value.imports = []
        start = MagicMock()  # Do not execute the supplied bootstrap or source.
        runtime.Linker.return_value.__enter__.return_value.instantiate.return_value.exports.return_value = {"_start": start}
        request = {**self.request, "trace_lines": True}
        original = deepcopy(request)
        with patch.object(worker, "_runtime", return_value=(runtime, object())), \
                patch.object(worker.threading, "Timer") as timer:
            report = worker.execute(self.root, request, self.cache_pin)
        wasi = runtime.WasiConfig.return_value
        self.assertEqual(wasi.argv[:6], ["python", "-I", "-S", "-B", "-c", worker.GUEST_BOOTSTRAP])
        self.assertEqual(json.loads(wasi.argv[6]), request)
        self.assertEqual(request, original)
        self.assertEqual(wasi.env, [])
        wasi.preopen_dir.assert_called_once_with(str(self.root / "installed/guest/lib"), "/lib", fs_mutable=False)
        runtime.Store.return_value.__enter__.return_value.set_limits.assert_called_once_with(
            memory_size=128 * 1024**2, table_elements=100_000, instances=1, memories=1, tables=1)
        timer.assert_called_once_with(5.0, runtime.Engine.return_value.__enter__.return_value.increment_epoch)
        self.assertEqual(set(report), {"host_status", "detail", "output_limit", "load_seconds",
                                       "worker_seconds", "guest_output"})

    def test_bootstrap_static_identity_filter_and_call_only_finally_contract(self):
        # This static contract check does not execute the guest or prove runtime tracing.
        tree = ast.parse(worker.GUEST_BOOTSTRAP)
        text = ast.unparse(tree)
        self.assertIn("trace_codes.append(code)", text)
        self.assertIn("trace_ids.add(id(code))", text)
        self.assertIn("type(item) is types.CodeType", text)
        self.assertIn("if not trace_active or id(frame.f_code) not in trace_ids:", text)
        self.assertNotIn("frame.f_code.co_filename", text)
        self.assertNotIn("f_locals", text)
        self.assertNotIn("f_back", text)
        callback = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "trace_call")
        self.assertIn("len(trace_report['line_events']) < 1000", ast.unparse(callback))
        self.assertIn("trace_report['truncated'] = True", ast.unparse(callback))
        outer = next(node for node in tree.body if isinstance(node, ast.Try))
        call_branch = next(node for node in outer.body if isinstance(node, ast.If)
                           and ast.unparse(node.test) == "trace_enabled")
        guarded = next(node for node in call_branch.body if isinstance(node, ast.Try))
        self.assertEqual(ast.unparse(guarded.body[0]), "trace_set(trace_call)")
        self.assertIn("result = scope[request['entry']]", ast.unparse(guarded.body[1]))
        self.assertEqual(ast.unparse(guarded.finalbody[0]), "trace_active = False")
        cleanup = guarded.finalbody[1]
        self.assertIsInstance(cleanup, ast.Try)
        self.assertEqual(ast.unparse(cleanup.body[0]), "trace_report['hook_intact'] = trace_get() is trace_call")
        self.assertEqual(ast.unparse(cleanup.finalbody[0]), "trace_set(None)")
        serialization = next(node for node in outer.body if isinstance(node, ast.Assign)
                             and any(isinstance(target, ast.Name) and target.id == "encoded_result"
                                     for target in node.targets))
        self.assertLess(guarded.end_lineno, serialization.lineno)
        self.assertLess(guarded.end_lineno, outer.handlers[0].lineno)
        hooks = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                     and isinstance(node.targets[0], ast.Tuple)
                     and [getattr(item, "id", None) for item in node.targets[0].elts] == ["trace_set", "trace_get"])
        self.assertEqual(ast.dump(hooks.value), ast.dump(ast.parse("(sys.settrace, sys.gettrace)", mode="eval").body))
        initialization = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                              and isinstance(node.func, ast.Name) and node.func.id == "exec")
        self.assertLess(hooks.lineno, initialization.lineno)
        self.assertLess(text.index("FORGE8_GUEST_TRACE="), text.index("FORGE8_GUEST_RESULT="))


if __name__ == "__main__":
    unittest.main()
