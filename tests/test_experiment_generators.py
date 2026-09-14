"""Generator protocol/admission tests; never execute guest or supplied source."""
from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from forge8 import _experiment_worker as worker, experiments
import test_experiment_trace as trace_fixtures
from test_experiment_results import guest_envelope, marker


def generator_result(**changes):
    report = {"limit": 3, "next_calls": 3, "yields": [1, 2],
              "iteration": {"status": "exhausted"}, "serialization": {"status": "ok"},
              "close": {"status": "closed"}, "return_value": 9}
    report.update(changes)
    return {"generator": report}


def generator_host(value):
    return guest_envelope("FORGE8_GUEST_RESULT=" + json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n")


class GeneratorProtocolTests(unittest.TestCase):
    def test_exact_expected_limit_and_each_completed_state(self):
        base = generator_result()["generator"]
        variants = [base, {**base, "return_value": None},
            {**base, "next_calls": 1, "yields": [], "return_value": 7},
            {**base, "next_calls": 1, "yields": [], "return_value": 10**60 + 1}]
        no_return = {key: value for key, value in base.items() if key != "return_value"}
        variants += [
            {**no_return, "next_calls": 3, "yields": [1, 2, 3],
             "iteration": {"status": "limit_reached"}},
            {**no_return, "iteration": {"status": "exception", "exception": "LookupError"}},
            {**no_return, "next_calls": 1, "yields": [],
             "iteration": {"status": "exception", "exception": "LookupError"},
             "serialization": {"status": "not_attempted"}},
            {**no_return, "next_calls": 0, "yields": [],
             "iteration": {"status": "type_error", "exception": "TypeError"},
             "serialization": {"status": "not_attempted"}, "close": {"status": "not_attempted"}},
            {**no_return, "serialization": {"status": "exception", "phase": "return",
                                          "exception": "TypeError"}},
            {**no_return, "iteration": {"status": "stopped_for_serialization"},
             "serialization": {"status": "exception", "phase": "yield", "yield_index": 3,
                               "exception": "TypeError"},
             "close": {"status": "exception", "exception": "LookupError"}},
        ]
        for report in variants:
            value = {"generator": report}
            host, original = generator_host(value), deepcopy(value)
            with self.subTest(report=report):
                self.assertEqual(experiments.reported_result(host, generator_steps=3), value)
                self.assertIsNone(experiments.reported_result(host))
                self.assertIsNone(experiments.reported_result(host, generator_steps=2))
                self.assertEqual(experiments.compare_reported_results(value, value), "unavailable")
                self.assertEqual(value, original)

    def test_wrong_shapes_counters_and_cross_state_claims_are_unavailable(self):
        base = generator_result()["generator"]
        invalid = [None, [], {}, {**base, "extra": 1}]
        invalid += [{key: value for key, value in base.items() if key != missing}
                    for missing in base]
        invalid += [{**base, key: value} for key, values in (
            ("limit", (True, 0, 4, 3.0, "3")),
            ("next_calls", (True, 0, 2, 4, 3.0, "3")),
            ("yields", (None, {}, [1, 2, 3], [0] * 13)),
            ("iteration", ({}, {"status": "not_started"}, {"status": "exhausted", "extra": 1},
                           {"status": "limit_reached"}, {"status": "type_error", "exception": "TypeError"},
                           {"status": "exception", "exception": "LookupError"})),
            ("serialization", ({"status": "not_attempted"}, {"status": "ok", "extra": 1},
                {"status": "exception", "phase": "return", "exception": "TypeError"},
                {"status": "exception", "phase": "yield", "yield_index": 3, "exception": "TypeError"})),
            ("close", (None, {"status": "not_attempted"}, {"status": "closed", "extra": 1},
                       {"status": "exception"}, {"status": "exception", "exception": ""},
                       {"status": "exception", "exception": "x" * 81})),
        ) for value in values]
        for report in invalid:
            with self.subTest(report=report):
                self.assertIsNone(experiments.reported_result(generator_host(
                    {"generator": report}), generator_steps=3))
        valid = generator_result()
        for expected in (True, 0, 13, 3.0, "3", [], {}):
            with self.subTest(expected=expected):
                self.assertIsNone(experiments.reported_result(generator_host(valid),
                                                             generator_steps=expected))
        for raw in ('{"generator":{},"generator":{}}',
                    json.dumps(valid).replace('"return_value": 9', '"return_value": NaN'),
                    json.dumps(valid).replace('"return_value": 9', '"return_value": 1e999')):
            self.assertIsNone(experiments.reported_result(
                guest_envelope("FORGE8_GUEST_RESULT=" + raw + "\n"), generator_steps=3))

    def test_only_initialization_and_call_failures_keep_legacy_exception_shape(self):
        for phase in ("module_initialization", "call", "serialization"):
            value = {"exception": "ValueError", "message": "owned", "phase": phase}
            host = guest_envelope(marker(value))
            self.assertEqual(experiments.reported_result(host), value)
            self.assertEqual(experiments.reported_result(host, generator_steps=3),
                             None if phase == "serialization" else value)
        self.assertIsNone(experiments.reported_result(
            guest_envelope(marker({"return": 7})), generator_steps=3))

    def test_capture_markers_and_retained_compact_value_budget(self):
        value = generator_result()
        host = generator_host(value)
        original = host["guest_output"]["stdout"]
        for text in (original + original, original + "later\n", "FORGE8_GUEST_RESULT={\n"):
            self.assertIsNone(experiments.reported_result(guest_envelope(text), generator_steps=3))
        for status, detail, limited in (("trap", "INTERRUPT", False),
                                       ("guest_exit", 1, False), ("guest_exit", False, False),
                                       ("exited", None, True)):
            changed = deepcopy(host)
            changed.update(host_status=status, detail=detail, output_limit=limited)
            self.assertIsNone(experiments.reported_result(changed, generator_steps=3))
        for size, accepted in ((60 * 1024 - 2, True), (60 * 1024 - 1, False)):
            bounded = generator_result(limit=1, next_calls=1, yields=["x" * size],
                                       iteration={"status": "limit_reached"})
            del bounded["generator"]["return_value"]
            result = experiments.reported_result(generator_host(bounded), generator_steps=1)
            self.assertEqual(result, bounded if accepted else None)
        large = generator_result(limit=1, next_calls=1, yields=[[0] * 30_000],
                                 iteration={"status": "limit_reached"})
        del large["generator"]["return_value"]
        self.assertEqual(experiments.reported_result(generator_host(large), generator_steps=1), large)
        self.assertIsNone(experiments.reported_result(guest_envelope(marker(large)), generator_steps=1))


class GeneratorControllerTests(unittest.TestCase):
    setUp = trace_fixtures.TraceControllerTests.setUp
    run_fake = trace_fixtures.TraceControllerTests.run_fake

    def test_request_identity_and_default_wire_are_not_widened(self):
        request, identity = experiments.prepare_request(self.source, "entry", self.inputs)
        for name, options in (("omitted", {}), ("none", {"generator_steps": None}),
                              ("enabled", {"generator_steps": 3})):
            enabled = options.get("generator_steps") is not None
            self.host = generator_host(generator_result()) if enabled else guest_envelope(marker({"return": 7}))
            report, sent, captured = self.run_fake(name, trace_options=options)
            extra = {"generator_steps": 3} if enabled else {}
            self.assertEqual(sent, {"request": {**request, **extra}, "cache_pin": self.pin})
            self.assertEqual(captured, {"request": {**request, **extra}, "identity": {**identity, **extra}})
            self.assertEqual(report["identity"], {**identity, **extra})
            self.assertEqual(report["reported_result"], generator_result() if enabled else {"return": 7})
            self.assertNotIn("reported_trace", report)
        target = {"file": "owned", "path": self.source.name, "version": "snapshot",
                  "entry": "entry", "source_sha256": identity["source_sha256"],
                  "source_bytes": len(self.code)}
        with self.assertRaisesRegex(ValueError, "does not bind"):
            experiments.retain_trial_result(target, "enabled", self.raw_input.decode(), report,
                json.dumps(report).encode(), b"{}", self.code, self.root / "enabled")
        failure = {"exception": "ValueError", "message": "owned", "phase": "call"}
        self.host = guest_envelope(marker(failure))
        failed, _, _ = self.run_fake("call-failed", trace_options={"generator_steps": 3})
        self.assertEqual(failed["reported_result"], failure)
        with self.assertRaisesRegex(ValueError, "does not bind"):
            experiments.retain_trial_result(target, "call-failed", self.raw_input.decode(), failed,
                json.dumps(failed).encode(), b"{}", self.code, self.root / "call-failed")

    def test_invalid_modes_reject_before_source_runtime_or_worker(self):
        options = [{"generator_steps": value} for value in (True, 0, 13, 3.0, "3", [], {})]
        options += [{"generator_steps": 3, key: value} for key, value in (
            ("trace_lines", True), ("module_root", self.root), ("module_files", []),
            ("expected_module_set_sha256", "0" * 64))]
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

    def test_changed_source_input_or_incomplete_process_does_not_publish_generator(self):
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
            self.host = generator_host(generator_result())
            report, _, _ = self.run_fake(f"bad-{index}", trace_options={"generator_steps": 3}, **options)
            self.assertIsNone(report["reported_result"])
            self.assertEqual(report["execution"], self.host)
        cancelled = []
        self.source.write_bytes(self.code)
        self.inputs.write_bytes(self.raw_input)
        with self.assertRaises(KeyboardInterrupt):
            self.run_fake("cancelled", trace_options={"generator_steps": 3},
                after_worker=lambda: cancelled.append(True), cancel_requested=lambda: bool(cancelled))
        self.assertFalse((self.root / "cancelled/experiment.json").exists())


class GeneratorWorkerTests(unittest.TestCase):
    def test_worker_independently_refuses_null_invalid_and_combined_options(self):
        requests = [{"generator_steps": value} for value in (None, True, 0, 13, 3.0, "3", [], {})]
        requests += [{"generator_steps": 3, "trace_lines": True},
                     {"generator_steps": 3, "module_bundle": {}}]
        with patch.object(worker, "_runtime") as runtime, \
                patch.object(worker, "_module_bundle_directory") as modules:
            for request in requests:
                with self.subTest(request=request), self.assertRaises(ValueError):
                    worker.execute(Path("unread"), request, {})
            with self.assertRaises(ValueError):
                worker.execute(Path("unread"), {"generator_steps": 3}, {}, module_bundle={})
            runtime.assert_not_called()
            modules.assert_not_called()

    def test_guest_helper_is_only_bootstrap_text_and_keeps_bounded_next_and_finally(self):
        self.assertFalse(hasattr(worker, "consume_generator"))
        tree = ast.parse(worker.GUEST_BOOTSTRAP)  # Static parsing only.
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == "consume_generator")
        advances = [node for node in ast.walk(helper) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == "next"]
        self.assertEqual(len(advances), 1)
        loop = next(node for node in ast.walk(helper) if isinstance(node, ast.For)
                    and isinstance(node.iter, ast.Call) and ast.unparse(node.iter.func) == "range")
        self.assertEqual(ast.unparse(loop.iter), "range(limit)")
        outer = next(node for node in helper.body if isinstance(node, ast.Try))
        self.assertIn("result.close()", ast.unparse(outer.finalbody[0]))
        text = ast.unparse(tree)
        self.assertIn("types.GeneratorType", text)
        self.assertIn("allow_nan=False", text)
        self.assertIn("object_pairs_hook=unique_object", text)
        self.assertIn("{'separators': (',', ':')}", text)
        self.assertEqual(worker.GUEST_SECONDS, 5.0)
        self.assertEqual(worker.GUEST_MEMORY_BYTES, 128 * 1024**2)
        self.assertNotIn("consume_fuel", worker.ENGINE_OPTIONS)
