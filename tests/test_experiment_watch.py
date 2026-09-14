"""Watch protocol and mocked-worker tests; never execute guest or supplied source."""
from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from forge8 import _experiment_worker as worker, experiments
import test_experiment_trace as trace_fixtures
import test_experiment_worker_modules as worker_fixtures
from test_experiment_results import guest_envelope, marker


NAMES = ("value",)


def observation(state=None, *, event="line", call_id=1, line=3):
    return {"event": event, "line": line, "call_id": call_id,
            "values": {"value": deepcopy(state if state is not None else {"state": "value", "json": "7"})}}


def watched(events=None):
    return {**deepcopy(trace_fixtures.TRACE),
            "watch": {"names": ["value"], "events": deepcopy(events if events is not None else [observation()]),
                      "truncated": False}}


def envelope(trace, result=None):
    return guest_envelope(trace_fixtures.trace_marker(trace)
                          + marker(result if result is not None else {"return": 7}))


class WatchProtocolTests(unittest.TestCase):
    def parse(self, trace, *, names=NAMES, result=None):
        return experiments._reported_trace(envelope(trace, result), trace_fixtures.SOURCE, watch_names=names)

    def test_states_numeric_text_and_order_survive_without_mutating_guest_bytes(self):
        states = [{"state": state} for state in ("unbound", "unsupported", "limited")]
        states += [{"state": "value", "json": text} for text in
                   ("null", "true", "9007199254740993", "-9007199254740993", "0.125",
                    '{"a":[null,9007199254740993],"z":"\\u754c"}')]
        trace = watched([observation(state) for state in states])
        host = envelope(trace)
        original = deepcopy(host)
        self.assertEqual(experiments._reported_trace(host, trace_fixtures.SOURCE, watch_names=NAMES), trace)
        self.assertEqual(experiments.reported_result(host), {"return": 7})
        self.assertEqual(host, original)
        self.assertEqual(trace["watch"]["events"][4]["values"]["value"]["json"], "true")

    def test_only_requested_names_and_exact_schema_are_admitted(self):
        valid = watched()
        mutations = [
            lambda t: t.update(unexpected=True),
            lambda t: t["watch"].update(unexpected=True),
            lambda t: t["watch"].update(names=["other"]),
            lambda t: t["watch"].update(truncated=1),
            lambda t: t["watch"].update(events={}),
            lambda t: t["watch"]["events"][0].update(event="call"),
            lambda t: t["watch"]["events"][0].update(extra=None),
            lambda t: t["watch"]["events"][0].update(values={"other": {"state": "unbound"}}),
        ]
        mutations += [lambda t, key=key, value=value: t["watch"]["events"][0].update({key: value})
                      for key, value in (("line", True), ("line", 3.0), ("line", 4),
                                         ("call_id", True), ("call_id", 0), ("call_id", 129))]
        for mutate in mutations:
            trace = deepcopy(valid)
            mutate(trace)
            with self.subTest(trace=trace):
                self.assertIsNone(self.parse(trace))
        self.assertIsNone(self.parse(valid, names=()))
        self.assertIsNone(self.parse(valid, names=["value"]))
        self.assertIsNone(self.parse(trace_fixtures.TRACE))
        trace = watched()
        trace["watch"]["names"] = ["value", "other"]
        trace["watch"]["events"][0]["values"]["other"] = {"state": "unbound"}
        self.assertEqual(self.parse(trace, names=("value", "other")), trace)
        trace["watch"]["events"][0]["values"] = dict(reversed(list(trace["watch"]["events"][0]["values"].items())))
        self.assertIsNone(self.parse(trace, names=("value", "other")))

    def test_value_json_is_strict_canonical_ascii_and_never_repaired(self):
        invalid = [{"state": "value"}, {"state": "value", "json": 7},
                   {"state": "value", "json": "7", "extra": None},
                   {"state": "unbound", "json": "null"}, {"state": "unknown"}, {"state": True}]
        invalid += [{"state": "value", "json": text} for text in
                    ("", "NaN", "1e999", '{"a":1,"a":2}', '{"b":1,"a":2}',
                     " 7", "1e2", '"界"', '"\\u754C"', '"\\n"\n')]
        for state in invalid:
            with self.subTest(state=state):
                trace = watched([observation(state)])
                self.assertIsNone(self.parse(trace))
                self.assertEqual(experiments.reported_result(envelope(trace)), {"return": 7})

    def test_value_depth_node_and_ascii_byte_bounds_include_dictionary_keys(self):
        text_pairs = [
            ("[" * 6 + "0" + "]" * 6, "[" * 7 + "0" + "]" * 7),
            ("[" + ",".join(["0"] * 127) + "]", "[" + ",".join(["0"] * 128) + "]"),
            (json.dumps({str(n): 0 for n in range(63)}, sort_keys=True, separators=(",", ":")),
             json.dumps({str(n): 0 for n in range(64)}, sort_keys=True, separators=(",", ":"))),
            ('"' + "x" * 2046 + '"', '"' + "x" * 2047 + '"'),
            ('"' + "\\u754c" * 341 + '"', '"' + "\\u754c" * 342 + '"'),
        ]
        for good, bad in text_pairs:
            with self.subTest(good_length=len(good), bad_length=len(bad)):
                trace = watched([observation({"state": "value", "json": good})])
                self.assertEqual(self.parse(trace), trace)
                self.assertIsNone(self.parse(watched([observation({"state": "value", "json": bad})])))

    def test_event_cap_and_outer_escaped_watch_byte_budget_are_independent(self):
        trace = watched([observation({"state": "unbound"}) for _ in range(128)])
        trace["watch"]["truncated"] = True
        self.assertEqual(self.parse(trace), trace)
        trace["watch"]["events"].append(observation())
        self.assertIsNone(self.parse(trace))
        # Each inner JSON value fits; the escaped outer report is the tighter bound.
        event = observation({"state": "value", "json": json.dumps("\\" * 512)})
        trace = watched([])
        for _ in range(128):
            trace["watch"]["events"].append(deepcopy(event))
            if len(json.dumps(trace["watch"], ensure_ascii=True, separators=(",", ":"))) > 24 * 1024:
                break
        self.assertLess(len(trace["watch"]["events"]), 128)
        self.assertIsNone(self.parse(trace))
        trace["watch"]["events"].pop()
        trace["watch"]["truncated"] = True
        self.assertEqual(self.parse(trace), trace)

    def test_recursive_ids_and_exception_return_boundaries_do_not_claim_success(self):
        events = [observation(call_id=1), observation(call_id=2), observation(call_id=3),
                  observation(call_id=3, event="exception"),
                  observation(call_id=3, event="return"),
                  observation(call_id=2, event="exception"),
                  observation(call_id=2, event="return"),
                  observation(call_id=1, event="exception"),
                  observation(call_id=1, event="return")]
        trace = watched(events)
        for phase in ("call", "serialization"):
            failure = {"exception": "ValueError", "message": "owned", "phase": phase}
            self.assertEqual(self.parse(trace, result=failure), trace)
            self.assertEqual(experiments.reported_result(envelope(trace, failure)), failure)
        trace = watched([observation({"state": "value", "json": "null"}, event="return")])
        self.assertEqual(self.parse(trace, result={"return": None}), trace)
        # An intact final hook is not proof of continuous or complete tracing.
        trace = watched([observation()])
        self.assertEqual(self.parse(trace), trace)

    def test_skipped_reused_closed_and_non_top_invocation_ids_are_refused(self):
        invalid = [
            [observation(call_id=2)],
            [observation(), observation(call_id=3)],
            [observation(), observation(call_id=2), observation(call_id=1)],
            [observation(event="return"), observation()],
            [observation(event="return"), observation(call_id=2)],
            [observation(), observation(call_id=2, event="return"), observation(call_id=2)],
        ]
        for events in invalid:
            with self.subTest(events=events):
                self.assertIsNone(self.parse(watched(events)))

    def test_watch_still_requires_original_valid_terminal_result_and_normal_guest_exit(self):
        trace = watched()
        initialization = {"exception": "ValueError", "message": "owned", "phase": "module_initialization"}
        self.assertIsNone(self.parse(trace, result=initialization))
        for result in ({}, {"return": 7, "extra": True}):
            self.assertIsNone(self.parse(trace, result=result))
        for changes in ({"host_status": "trap", "detail": "INTERRUPT"},
                        {"host_status": "guest_exit", "detail": 1}, {"output_limit": True}):
            host = envelope(trace)
            host.update(changes)
            self.assertIsNone(experiments._reported_trace(host, trace_fixtures.SOURCE, watch_names=NAMES))
        stdout = trace_fixtures.trace_marker(trace)
        self.assertIsNone(experiments._reported_trace(
            guest_envelope(stdout + stdout + marker({"return": 7})), trace_fixtures.SOURCE, watch_names=NAMES))


class WatchControllerTests(unittest.TestCase):
    setUp = trace_fixtures.TraceControllerTests.setUp
    run_fake = trace_fixtures.TraceControllerTests.run_fake

    def test_default_omitted_empty_and_enabled_options_bind_exact_request_identity(self):
        request, identity = experiments.prepare_request(self.source, "entry", self.inputs)
        for name, options in (("omitted", {}), ("empty", {"watch_names": ()}),
                              ("traced-empty", {"trace_lines": True, "watch_names": ()}),
                              ("watched", {"trace_lines": True, "watch_names": NAMES})):
            tracing, watching = options.get("trace_lines", False), bool(options.get("watch_names"))
            trace = watched() if watching else trace_fixtures.TRACE
            self.host = envelope(trace) if tracing else guest_envelope(marker({"return": 7}))
            with self.subTest(name=name):
                report, sent, captured = self.run_fake(name, trace_options=options)
                added = {"trace_lines": True} if tracing else {}
                if watching:
                    added["watch_names"] = list(NAMES)
                self.assertEqual(sent, {"request": {**request, **added}, "cache_pin": self.pin})
                self.assertEqual(captured, {"request": {**request, **added}, "identity": {**identity, **added}})
                self.assertEqual(report["identity"], {**identity, **added})
                self.assertEqual(report["reported_result"], {"return": 7})
                if tracing:
                    self.assertEqual(report["reported_trace"], trace)
                else:
                    self.assertNotIn("reported_trace", report)

    def test_invalid_names_and_mode_combinations_reject_before_io_or_worker(self):
        invalid_names = (None, [], ["value"], "value", ("value", "value"), ("a", "b", "c", "d"),
                         (True,), ("",), ("x" * 65,), ("a.b",), ("x[0]",), ("class",), ("名稱",))
        options = [{"trace_lines": True, "watch_names": names} for names in invalid_names]
        options += [{"watch_names": NAMES}, {"watch_names": NAMES, "trace_lines": True, "generator_steps": 1}]
        options += [{"watch_names": NAMES, "trace_lines": True, key: value} for key, value in
                    (("module_root", self.root), ("module_files", []), ("expected_module_set_sha256", "0" * 64))]
        with patch.object(experiments, "prepare_request") as prepare, \
                patch.object(experiments, "_read_module_files") as modules, \
                patch.object(experiments, "verify_experiment_runtime") as verify, \
                patch.object(experiments, "_worker") as execute, patch.object(Path, "mkdir") as mkdir:
            for option in options:
                with self.subTest(option=option), self.assertRaises(ValueError):
                    experiments.run_experiment(self.runtime, self.source, "entry", self.inputs,
                        self.root / "never-created", allow_execution=True, **option)
            for operation in (prepare, modules, verify, execute, mkdir):
                operation.assert_not_called()
        for names in ((), ("_", "__local", "x" * 64)):
            experiments._validate_watch_names(names)

    def test_decorated_source_and_stale_source_or_input_refuse_before_runtime(self):
        with patch.object(experiments, "verify_experiment_runtime") as verify, \
                patch.object(experiments, "_worker") as execute, patch.object(Path, "mkdir") as mkdir:
            for options in ({"expected_source_sha256": "0" * 64}, {"expected_input_sha256": "0" * 64}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    experiments.run_experiment(self.runtime, self.source, "entry", self.inputs,
                        self.root / "never-created", allow_execution=True, trace_lines=True,
                        watch_names=NAMES, **options)
            self.source.write_bytes(b"@owned_decorator\ndef entry(value):\n    return value\n")
            with self.assertRaises(ValueError):
                experiments.run_experiment(self.runtime, self.source, "entry", self.inputs,
                    self.root / "never-created", allow_execution=True, trace_lines=True, watch_names=NAMES)
            for operation in (verify, execute, mkdir):
                operation.assert_not_called()

    def test_changed_or_incomplete_execution_retains_raw_report_but_no_watch_publication(self):
        valid = envelope(watched())
        variants = [
            {"after_worker": lambda: self.source.write_bytes(b"def changed(:\n")},
            {"after_worker": lambda: self.inputs.write_bytes(b"{}")},
            {"runtime_error": ValueError("owned runtime drift")},
            {"process_changes": {"status": "failed", "ok": False}},
            {"process_changes": {"output_truncated": True}},
            {"process_changes": {"capture_errors": ("owned capture failure",)}},
        ]
        for index, options in enumerate(variants):
            self.source.write_bytes(self.code)
            self.inputs.write_bytes(self.raw_input)
            self.host = deepcopy(valid)
            with self.subTest(index=index):
                report, _, _ = self.run_fake(f"bad-{index}",
                    trace_options={"trace_lines": True, "watch_names": NAMES}, **options)
                self.assertIsNone(report["reported_trace"])
                self.assertEqual(report["execution"], valid)

    def test_invalid_watch_keeps_ordinary_result_and_cancellation_never_publishes(self):
        self.host = envelope(trace_fixtures.TRACE)
        report, _, _ = self.run_fake("missing-watch", trace_options={"trace_lines": True, "watch_names": NAMES})
        self.assertIsNone(report["reported_trace"])
        self.assertEqual(report["reported_result"], {"return": 7})
        self.host = envelope(watched())
        cancelled = []
        with self.assertRaises(KeyboardInterrupt):
            self.run_fake("cancelled", trace_options={"trace_lines": True, "watch_names": NAMES},
                after_worker=lambda: cancelled.append(True), cancel_requested=lambda: bool(cancelled))
        self.assertFalse((self.root / "cancelled/experiment.json").exists())


class WatchWorkerTests(unittest.TestCase):
    setUp = worker_fixtures.ExperimentWorkerModulesTests.setUp

    def test_worker_independently_rejects_invalid_options_before_runtime_or_bundle_read(self):
        request = {**self.request, "source": trace_fixtures.SOURCE, "trace_lines": True, "watch_names": ["value"]}
        invalid = [{**request, "watch_names": names} for names in (None, [], ("value",), ["value", "value"], ["a.b"])]
        invalid += [{**request, key: value} for key, value in
                    (("trace_lines", False), ("generator_steps", 1), ("module_bundle", {}))]
        invalid += [{**request, "source": source} for source in
                    ("async def entry(value):\n    return value\n",
                     "@owned\ndef entry(value):\n    return value\n",
                     "def entry(value): pass\ndef entry(value): pass\n",
                     "def other():\n    def entry(value): pass\n")]
        with patch.object(worker, "_runtime") as runtime, patch.object(worker, "_module_bundle_directory") as modules:
            for value in invalid:
                with self.subTest(request=value), self.assertRaises(ValueError):
                    worker.execute(self.root, value, self.cache_pin)
            with self.assertRaises(ValueError):
                worker.execute(self.root, request, self.cache_pin, module_bundle=self.bundle)
            runtime.assert_not_called()
            modules.assert_not_called()

    def test_watch_reaches_mock_guest_without_new_mounts_environment_or_limits(self):
        runtime = MagicMock()
        runtime.ExitTrap = type("OwnedExitTrap", (Exception,), {})
        runtime.Trap = type("OwnedTrap", (Exception,), {})
        runtime.Module.deserialize.return_value.__enter__.return_value.imports = []
        start = MagicMock()  # No bootstrap or fixture execution.
        runtime.Linker.return_value.__enter__.return_value.instantiate.return_value.exports.return_value = {"_start": start}
        request = {**self.request, "source": trace_fixtures.SOURCE, "trace_lines": True, "watch_names": ["value"]}
        original = deepcopy(request)
        with patch.object(worker, "_runtime", return_value=(runtime, object())), \
                patch.object(worker.threading, "Timer") as timer:
            report = worker.execute(self.root, request, self.cache_pin)
        wasi = runtime.WasiConfig.return_value
        self.assertEqual(wasi.argv[:6], ["python", "-I", "-S", "-B", "-c", worker.GUEST_BOOTSTRAP])
        self.assertEqual(json.loads(wasi.argv[6]), original)
        self.assertEqual(request, original)
        self.assertEqual(wasi.env, [])
        wasi.preopen_dir.assert_called_once_with(str(self.root / "installed/guest/lib"), "/lib", fs_mutable=False)
        store = runtime.Store.return_value.__enter__.return_value
        store.set_limits.assert_called_once_with(memory_size=128 * 1024**2, table_elements=100_000,
                                                 instances=1, memories=1, tables=1)
        timer.assert_called_once_with(5.0, runtime.Engine.return_value.__enter__.return_value.increment_epoch)
        start.assert_called_once_with(store)
        self.assertEqual(report["guest_output"], {"stdout": "", "stderr": ""})

    def test_static_original_entry_guard_precedes_hook_and_does_not_inspect_event_argument(self):
        tree = ast.parse(worker.GUEST_BOOTSTRAP)  # Parse only, never compile/exec/eval.
        outer = next(node for node in tree.body if isinstance(node, ast.Try))
        branch = next(node for node in outer.body if isinstance(node, ast.If)
                      and ast.unparse(node.test) == "trace_enabled")
        guard = branch.body[0]
        self.assertIsInstance(guard, ast.If)
        self.assertEqual(ast.unparse(guard.test), "watch_names")
        guard_text = ast.unparse(guard)
        for fragment in ("watch_type(selected) is not watch_function", "selected.__code__ is not watch_code",
                         "watch_code.co_flags", "watch_code.co_varnames + watch_code.co_cellvars"):
            self.assertIn(fragment, guard_text)
        self.assertTrue(any(isinstance(node, ast.Raise) for node in ast.walk(guard)))
        capture = next(node for node in ast.walk(branch)
                       if isinstance(node, ast.FunctionDef) and node.name == "watch_event")
        self.assertEqual([argument.arg for argument in capture.args.args], ["frame", "event"])
        self.assertIn("frame.f_code is not watch_code", ast.unparse(capture))
        self.assertNotIn("repr(", ast.unparse(capture))
        call = next(node for node in branch.body if isinstance(node, ast.Try))
        self.assertLess(guard.end_lineno, call.lineno)


if __name__ == "__main__":
    unittest.main()
