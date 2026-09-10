"""Bounded positions from one explicitly trusted test, in an isolated child.

Never used by read/explain. Imports and the test execute with the child's user
permissions: this is not a sandbox, syscall audit or semantic verifier.
"""
from __future__ import annotations

import importlib
import inspect
import json
import os
from pathlib import Path
import stat
import sys
import unittest
from hashlib import sha256
from types import CodeType

from .repository import (RepositoryError, _is_reparse_point, _read_regular_file,
                         _strict_existing_directory, _validate_portable_component,
                         _validate_text)
from .workspace import WorkspacePolicy

MAX_EVENTS = 1000
MAX_REPORT_BYTES = 128 * 1024
MAX_SOURCE_BYTES = 64 * 1024
_ASYNC_FLAGS = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR


class _Invalid(ValueError):
    """Only constant, observer-authored validation messages are serialized."""


class _Counts(unittest.TestResult):
    """Retain outcome counts only, never formatted exceptions or test values."""
    def __init__(self):
        super().__init__()
        self.counts = dict.fromkeys(("passed", "failed", "errors", "skipped",
            "expected_failures", "unexpected_successes", "subtests_passed",
            "subtests_failed", "subtests_errors"), 0)

    def addSuccess(self, test):
        self.counts["passed"] += 1

    def addFailure(self, test, err):
        self.counts["failed"] += 1

    def addError(self, test, err):
        self.counts["errors"] += 1

    def addSkip(self, test, reason):
        self.counts["skipped"] += 1

    def addExpectedFailure(self, test, err):
        self.counts["expected_failures"] += 1

    def addUnexpectedSuccess(self, test):
        self.counts["unexpected_successes"] += 1

    def addSubTest(self, test, subtest, err):
        kind = "passed" if err is None else (
            "failed" if issubclass(err[0], test.failureException) else "errors")
        self.counts["subtests_" + kind] += 1


def _source_pins(policy, paths):
    pins, selected = {}, {}
    for name in paths:
        if not isinstance(name, str) or len(name.encode("utf-8")) > 512 or "\\" in name:
            raise _Invalid("Source paths must be bounded relative POSIX paths")
        path = policy.resolve(name)
        if path.suffix != ".py":
            raise _Invalid("Observed sources must be Python files")
        current = policy.root
        for part in name.split("/"):
            _validate_portable_component(part, name)
            current /= part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
                raise _Invalid("Observed source contains a link or reparse point")
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_SOURCE_BYTES:
            raise _Invalid("Observed source must be a regular file of at most 64 KiB")
        content = _read_regular_file(path, metadata, name)
        _validate_text(content, name)
        if len(content) > MAX_SOURCE_BYTES:
            raise _Invalid("Observed source exceeds 64 KiB")
        text = content.decode("utf-8")
        if any(c in text for c in "\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
            raise _Invalid("Observed source uses unsupported physical line separators")
        try:
            pending = [compile(content, str(path), "exec", dont_inherit=True)]
        except (SyntaxError, ValueError, RecursionError):
            raise _Invalid("Observed source cannot compile in this interpreter") from None
        codes, visited = set(), 0
        while pending:
            code = pending.pop()
            visited += 1
            if visited > 1000:
                raise _Invalid("Observed source exceeds 1000 nested code objects")
            codes.add(code)
            pending.extend(c for c in code.co_consts if isinstance(c, CodeType))
        pins[name] = sha256(content).hexdigest()
        selected[os.path.normcase(str(path))] = (name, codes)
    if len(selected) != len(paths):
        raise _Invalid("Observed source paths must be distinct")
    return pins, selected


def capture_test(root: Path, selector: str, source_paths: tuple[str, ...]) -> dict:
    """Execute one trusted unittest selector; return metadata, never write it.

    The caller must obtain explicit trust and provide process timeout/cleanup.
    Only root and the selected module's import base are added to sys.path;
    other dependencies must already be available in this native interpreter.
    Code equality checks source consistency, not hostile-code authenticity.
    """
    result = _Counts()
    report = {"schema_version": 1, "kind": "forge8.observation", "test": None, "python": sys.version[:200],
        "source_before": {}, "source_after": None, "source_unchanged": False,
        "tests_run": 0, "counts": result.counts, "test_passed": False,
        "complete": False, "truncated": False, "unsupported": False, "unsupported_calls": 0,
        "unmatched_source_code": False, "method_observed": False,
        "observer_error": False, "aborted": False, "trace_replaced": False,
        "trace_restored": False, "error": None, "events": [],
        "event_limit": MAX_EVENTS, "report_byte_limit": MAX_REPORT_BYTES,
        "scope": "Selected-file synchronous Python positions during TestSuite.run; "
        "Earlier test-loading imports and test construction, values, C calls, other threads/processes unobserved. "
        "Parent is nearest selected ancestor; line precedes execution; return is not success."}
    previous, old_path = sys.gettrace(), sys.path[:]
    events, active, byte_count, next_call = report["events"], {}, 0, 1
    selected, policy, method_code = {}, None, None

    def trace(frame, event, arg):
        nonlocal byte_count, next_call
        try:
            filename = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
            if filename not in selected:
                return None
            if frame.f_code not in selected[filename][1]:
                report["unmatched_source_code"] = True
                return None
            if frame.f_code.co_flags & _ASYNC_FLAGS:
                report["unsupported"] = True
                report["unsupported_calls"] += 1
                return None
            if event not in {"call", "line", "return", "exception"}:
                return trace
            identity = id(frame)
            if event == "call":
                report["method_observed"] |= frame.f_code is method_code
                parent = frame.f_back
                while parent is not None and id(parent) not in active:
                    parent = parent.f_back
                active[identity] = (next_call, None if parent is None else active[id(parent)][0])
                next_call += 1
            cid, parent_id = active[identity]
            item = {"event": event, "call_id": cid, "parent_call_id": parent_id,
                "path": selected[filename][0],
                "function": getattr(frame.f_code, "co_qualname", frame.f_code.co_name)[:200],
                "first_line": frame.f_code.co_firstlineno, "line": frame.f_lineno}
            size = len(json.dumps(item, separators=(",", ":")).encode("utf-8")) + 1
            if len(events) >= MAX_EVENTS or byte_count + size > MAX_REPORT_BYTES - 16384:
                report["truncated"] = True
                sys.settrace(None)
                return None
            events.append(item)
            byte_count += size
            if event == "return":
                active.pop(identity)
            return trace
        except Exception:
            report["observer_error"] = True
            sys.settrace(None)
            return None

    try:
        if not isinstance(selector, str) or len(selector.encode("utf-8")) > 512:
            raise _Invalid("Selector must be a bounded module.Class.test_method")
        parts = selector.split(".")
        if len(parts) < 3 or not all(p.isidentifier() for p in parts) or not parts[-1].startswith("test_"):
            raise _Invalid("Selector must name exactly module.Class.test_method")
        if not isinstance(source_paths, tuple) or not 1 <= len(source_paths) <= 4:
            raise _Invalid("Select one to four source paths")
        policy = WorkspacePolicy(_strict_existing_directory(root, "observation root"), max_read_bytes=MAX_SOURCE_BYTES)
        report["source_before"], selected = _source_pins(policy, source_paths)
        report["test"] = selector
        module_name, class_name, method_name = ".".join(parts[:-2]), parts[-2], parts[-1]
        suffix = "/".join(parts[:-2]) + ".py"
        matches = [p for p in source_paths if p == suffix or p.endswith("/" + suffix)]
        if len(matches) != 1:
            raise _Invalid("Selected sources must include exactly one matching test module")
        test_path = policy.resolve(matches[0])
        sys.path[:0] = [str(test_path.parents[len(parts) - 3]), str(policy.root)]
        module = importlib.import_module(module_name)
        if not getattr(module, "__file__", None) or Path(module.__file__).resolve() != test_path:
            raise _Invalid("Imported test module is not the selected source")
        cls = vars(module).get(class_name)
        if not isinstance(cls, type) or not issubclass(cls, unittest.TestCase):
            raise _Invalid("Selector class must be a unittest.TestCase")
        method = inspect.getattr_static(cls, method_name, None)
        if inspect.isfunction(method):
            method = inspect.unwrap(method)
        if not inspect.isfunction(method) or method.__code__.co_flags & _ASYNC_FLAGS:
            report["unsupported"] = True
            raise _Invalid("Test method must be an ordinary synchronous function")
        if os.path.normcase(os.path.abspath(method.__code__.co_filename)) not in selected:
            raise _Invalid("Test method implementation is outside selected sources")
        method_code = method.__code__
        suite = unittest.TestSuite([cls(method_name)])
        if _source_pins(policy, source_paths)[0] != report["source_before"]:
            raise _Invalid("Selected source changed during imports or construction")
        report["trace_replaced"] = sys.gettrace() is not previous
        sys.settrace(trace)
        try:
            suite.run(result)
        finally:
            expected = None if report["truncated"] or report["observer_error"] else trace
            report["trace_replaced"] |= sys.gettrace() is not expected
    except _Invalid as exc:
        report["error"] = str(exc)
    except (ValueError, OSError, RepositoryError):
        report["error"] = "Source or selector unavailable, invalid UTF-8 or rejected by workspace policy"
    except BaseException:
        report["aborted"] = True
        report["error"] = "Import or test execution aborted; exception payload omitted"
    finally:
        sys.settrace(previous)
        sys.path[:] = old_path
        report["trace_restored"] = sys.gettrace() is previous
        if policy is not None and report["source_before"]:
            try:
                report["source_after"] = _source_pins(policy, source_paths)[0]
            except Exception:
                report["error"] = "Selected sources unavailable or invalid after execution"
        report["source_unchanged"] = bool(report["source_before"]) and report["source_before"] == report["source_after"]
    report["tests_run"] = result.testsRun
    if result.counts["passed"] and not report["method_observed"]:
        report["error"] = "Selected test method was not observed"
    report["test_passed"] = (result.testsRun == 1 and result.counts["passed"] == 1
        and report["method_observed"]
        and not any(v for k, v in result.counts.items() if k not in {"passed", "subtests_passed"}))
    report["complete"] = (report["source_unchanged"] and report["trace_restored"]
        and not active and not any(report[k] for k in (
            "error", "truncated", "unsupported", "observer_error", "aborted", "trace_replaced", "unmatched_source_code")))
    if len(json.dumps(report, separators=(",", ":")).encode("utf-8")) > MAX_REPORT_BYTES:
        report.update(complete=False, truncated=True, events=[])
    return report
