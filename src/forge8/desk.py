"""Loopback-only source reading desk. Browsing never starts the model."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import parse_qs, urlsplit

from .cli import _explain_gpu_state, _fix_runs_parent, _create_runs_parent, _absolute_deployment_path, _new_task_id, _paths_overlap, _resolve_fix_asset_root, _run_explain_cli, _run_locate_cli, _run_project_cli, _unverified_explain_prose, _insufficient_explain_reason, _reading_request_complete
from .checks import CheckCleanupError
from .comparison import prepare_comparison
from .discovery import _OUTLINE_FILE_BYTES, _OUTLINE_ITEMS, _OUTLINE_NODES, _outline_status, _python_outline
from .explain import _bounded_text, _context_supplement_actions, _focus_actions, _inert_text, _reject_constant, _require_run_entry, _snapshot_identity, _strict_object
from .repository import MAX_REPOSITORY_FILE_BYTES, _read_regular_file, prepare_repository_snapshot
from .source_context import selected_context
from .source_search import keyword_definitions
from .source_calls import ScanStopped, call_occurrences
from .import_sources import trace_import
from .residency import ResidentModel


_OUTLINE_CACHE_BYTES = 1024 * 1024
_OBSERVATION_BYTES = 128 * 1024
_HISTORY_ITEMS = 5
_HISTORY_BYTES = 512 * 1024
_TRACEBACK_FRAME = re.compile(r'  File "([^"\r\n]+)", line ([0-9]+)(?:, in (\S[^\r\n]*))?')
_CHANGE_QUESTION = (
    "Compare BEFORE (local HEAD) with AFTER (saved). The named caller is held "
    "FIXED against both; this is not historical caller behavior or proof of binding. "
    "Explain the changed condition, a distinguishing input, caller consequence and "
    "unknowns. Cite versions separately. Do not invent missing globals/helpers or "
    "test coverage. Source was not executed.\n"
)


def _retained_bytes(snapshot, fingerprint):
    """Read a captured snapshot member without relying on mutable desk state."""
    root = Path(snapshot.snapshot_root)
    path = root / fingerprint.path
    _require_run_entry(root, directory=True, label="browse snapshot")
    _require_run_entry(path, directory=False, label="browse snapshot file")
    if root.resolve(strict=True) != root or path.resolve(strict=True) != path:
        raise ValueError("browse snapshot is redirected")
    data = _read_regular_file(path, path.lstat(), "browse snapshot file")
    if len(data) != fingerprint.size_bytes or hashlib.sha256(data).hexdigest() != fingerprint.sha256:
        raise ValueError("browse snapshot changed; refresh before continuing")
    return path, data


def _paired_observation(report: dict) -> dict:
    """Bounded display projection; guest text is not an execution oracle."""
    from .experiments import compare_reported_results
    execution = report.get("execution") or {}
    if execution:
        output = execution.get("guest_output")
        if (type(output) is not dict or set(output) != {"stdout", "stderr"}
                or any(type(text) is not str for text in output.values())
                or sum(len(text.encode("utf-8")) for text in output.values()) > 3 * 65_536):
            raise ValueError("paired guest output is not bounded text")
    normal_exit = (execution.get("host_status") == "exited" and execution.get("detail") is None
        or execution.get("host_status") == "guest_exit" and type(execution.get("detail")) is int
        and execution["detail"] == 0)
    complete = (report.get("source_unchanged") is True and report.get("runtime_unchanged") is True
        and report.get("process_status") == "passed" and execution.get("output_limit") is False
        and normal_exit)
    value = {"complete": complete, "process_status": report.get("process_status", "unavailable"),
        "host_status": execution.get("host_status", "unavailable"),
        "source_unchanged": report.get("source_unchanged") is True,
        "runtime_unchanged": report.get("runtime_unchanged") is True}
    reported = report.get("reported_result")
    if compare_reported_results(reported, reported) != "unavailable":
        def display(indent):
            text = json.dumps(reported, ensure_ascii=False, allow_nan=False, indent=indent)
            return "".join(character if character.isprintable() or character == "\n"
                else json.dumps(character)[1:-1] for character in text)
        text = display(2)
        if len(text) > 6 * 65_536:
            text = display(None)
        if len(text) <= 6 * 65_536:
            value["result_text"] = text
    for name in ("stdout", "stderr"):
        text = execution.get("guest_output", {}).get(name, "")
        if "result_text" in value and name == "stdout":
            text = "\n".join(line for line in text.splitlines() if not line.startswith("FORGE8_GUEST_RESULT="))
        value[name] = text
    return value


def _valid_project_context(project: dict, focus: list) -> bool:
    if "context" not in project:
        return True  # Jobs retained before supplementation remain readable.
    context = project["context"]
    if type(context) is not dict or set(context) != {"added", "skipped"}:
        return False
    added, skipped = context["added"], context["skipped"]
    if (type(added) is not list or len(added) > 4 or type(skipped) is not dict
            or not set(skipped) <= {"ambiguous", "conditional", "limit", "unavailable", "capacity"}
            or any(type(count) is not int or not 1 <= count <= 384 for count in skipped.values())):
        return False
    covered = {(span["path"], line) for span in focus
        for line in range(span["start_line"], span["end_line"] + 1)}
    seen, total = set(), 0
    for span in added:
        if (type(span) is not dict or set(span) != {"path", "start_line", "end_line"}
                or type(span["path"]) is not str or type(span["start_line"]) is not int
                or type(span["end_line"]) is not int
                or not 1 <= span["start_line"] <= span["end_line"] < span["start_line"] + 40):
            return False
        key = (span["path"], span["start_line"], span["end_line"])
        total += span["end_line"] - span["start_line"] + 1
        if key in seen or total > 40 or any((span["path"], line) not in covered
                for line in range(span["start_line"], span["end_line"] + 1)):
            return False
        seen.add(key)
    return True


def _comparison_question(question: str, selectors: list[str]) -> str:
    # Evidence can merge an overlapping caller into AFTER. Explicit coordinates
    # survive that deduplication; positional descriptions like 'third range' do not.
    roles = "\n".join(f"{role}: {span}" for role, span in
        zip(("BEFORE", "AFTER", "FIXED CALLER"), selectors))
    return _bounded_text(_CHANGE_QUESTION + roles + "\nUser question:\n" + question,
        "comparison request (shorten the question if the source paths are long)", 2000, multiline=True)


def _traceback_path(reported: str, source: Path) -> tuple[str | None, str | None]:
    """Lexical translation only; never inspect a path supplied by a pasted log."""
    windows = PureWindowsPath(reported)
    if (reported.startswith(("<", "\\", "//")) or reported.endswith(("/", "\\"))
            or "//" in reported.replace("\\", "/") or windows.is_reserved()
            or ".." in re.split(r"[/\\]", reported)):
        return None, "unsupported_path"
    if os.name == "nt":
        if (windows.drive and (not re.fullmatch(r"[A-Za-z]:", windows.drive) or not windows.is_absolute())
                or windows.root and not windows.drive or ":" in reported[2:]
                or not windows.drive and ":" in reported):
            return None, "unsupported_path"
        path, root = windows, PureWindowsPath(source)
    else:
        if "\\" in reported or ":" in reported:
            return None, "unsupported_path"
        path, root = PurePosixPath(reported), PurePosixPath(source)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            return None, "outside_project"
    return path.as_posix(), None


def _load_observation(path):
    path = Path(path).expanduser().absolute()
    _require_run_entry(path, directory=False, label="observation report")
    if path.resolve(strict=True) != path or path.lstat().st_size > _OBSERVATION_BYTES:
        raise ValueError("observation report is redirected or exceeds 128 KiB")
    data = _read_regular_file(path, path.lstat(), "observation report")
    if len(data) > _OBSERVATION_BYTES:
        raise ValueError("observation report exceeds 128 KiB")
    report = json.loads(data, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    if type(report) is not dict:
        raise ValueError("observation report must be one JSON object")
    return report


def _observation_view(report, files, fingerprints, nonphysical):
    """Validate imported positions against cached source, not report authenticity."""
    if (type(report) is not dict or type(report.get("schema_version")) is not int
            or report["schema_version"] != 1 or report.get("kind") != "forge8.observation"):
        raise ValueError("invalid observation schema")
    if any(report.get(k) is not True for k in ("complete", "source_unchanged", "trace_restored")) or any(
            report.get(k) is not False for k in ("truncated", "unsupported", "observer_error", "aborted", "trace_replaced", "unmatched_source_code")):
        raise ValueError("observation is incomplete")
    if any(type(report.get(k)) is not int or report[k] != v for k, v in (("event_limit", 1000), ("report_byte_limit", _OBSERVATION_BYTES))):
        raise ValueError("invalid observation declared limits")
    process = report.get("capture_process")
    if (type(process) is not dict or process.get("status") != "passed"
            or type(process.get("return_code")) is not int or process["return_code"] != 0
            or process.get("timed_out") is not False or type(process.get("output_truncated")) is not bool
            or report.get("error") is not None):
        raise ValueError("observation capture did not finish cleanly")
    counts = report.get("counts")
    count_keys = {"passed", "failed", "errors", "skipped", "expected_failures",
        "unexpected_successes", "subtests_passed", "subtests_failed", "subtests_errors"}
    if (type(counts) is not dict or set(counts) != count_keys
            or any(type(v) is not int or not 0 <= v <= 2**31 - 1 for v in counts.values())
            or type(report.get("tests_run")) is not int or report["tests_run"] not in (0, 1)
            or type(report.get("test_passed")) is not bool
            or type(report.get("method_observed")) is not bool
            or type(report.get("unsupported_calls")) is not int or report["unsupported_calls"] != 0):
        raise ValueError("invalid observation outcome counts")
    if report["test_passed"] and (report["tests_run"] != 1 or counts["passed"] != 1
            or report.get("method_observed") is not True or any(v for k, v in counts.items() if k not in {"passed", "subtests_passed"})):
        raise ValueError("observation test outcome is inconsistent")
    before = report.get("source_before")
    current = {f["path"]: (f, pin) for f, pin in zip(files, fingerprints)}
    if type(before) is not dict or not 1 <= len(before) <= 4 or before != report.get("source_after"):
        raise ValueError("invalid observation source pins")
    for path, digest in before.items():
        if (path not in current or not path.endswith(".py") or current[path][1].size_bytes > 65536
                or digest != current[path][1].sha256
                or path in nonphysical):
            raise ValueError("observation source differs from the current snapshot")
    events, calls, stack = report.get("events"), [], []
    if type(events) is not list or len(events) > 1000:
        raise ValueError("invalid observation event limit")
    event_keys = {"event", "call_id", "parent_call_id", "path", "function", "first_line", "line"}
    for event in events:
        if type(event) is not dict or set(event) != event_keys or event["path"] not in before:
            raise ValueError("invalid observation event fields or source")
        kind, cid, parent = event["event"], event["call_id"], event["parent_call_id"]
        file = current[event["path"]][0]
        name = _bounded_text(event["function"], "observed function", 200)
        if (kind not in {"call", "line", "return", "exception"} or type(cid) is not int or not 1 <= cid <= 1000
                or (parent is not None and (type(parent) is not int or not 1 <= parent < cid))
                or type(event["first_line"]) is not int or not 1 <= event["first_line"] <= max(1, file["lines"])
                or type(event["line"]) is not int or not (0 if kind in {"call", "return"} else 1) <= event["line"] <= file["lines"]):
            raise ValueError("invalid observation source coordinates")
        if kind == "call":
            if cid != len(calls) + 1 or parent != (stack[-1] if stack else None):
                raise ValueError("invalid observation call ancestry")
            parent_visits = calls[parent - 1]["visits"] if parent is not None else []
            calls.append({"id": cid, "parent": parent, "path": event["path"], "file": file["id"],
                "parent_line": parent_visits[-1] if parent_visits else None,
                "function": name, "first_line": event["first_line"], "visits": [], "returned": False, "exceptions": 0})
            stack.append(cid)
        if not stack or stack[-1] != cid:
            raise ValueError("observation event is outside its synchronous call")
        row = calls[cid - 1]
        if (row["path"], row["function"], row["first_line"], row["parent"]) != (event["path"], name, event["first_line"], parent):
            raise ValueError("observation call identity changed")
        if kind == "line":
            row["visits"].append(event["line"])
        elif kind == "exception":
            row["exceptions"] += 1
        elif kind == "return":
            row["returned"] = True
            stack.pop()
    if stack:
        raise ValueError("observation has unclosed calls")
    return {"test": _bounded_text(report.get("test"), "observed test", 512),
        "python": _bounded_text(report.get("python"), "observed Python", 200), "counts": dict(counts),
        "test_passed": report["test_passed"], "event_count": len(events), "calls": calls,
        "scope": "Imported selected-source synchronous positions, not authenticated execution. "
        "No values, C calls, other threads/processes or OS-write audit; parent is nearest selected ancestor. "
        "Line precedes execution and return does not prove success."}


class ReadingDesk:
    def __init__(self, source: Path, *, observation: Path | None = None, reader: str = "qwen35",
                 allow_experiments: bool = False, idle_timeout: int = 0,
                 browse_only: bool = False, state_directory: Path | None = None):
        if type(browse_only) is not bool or type(allow_experiments) is not bool:
            raise ValueError("browse_only and allow_experiments must be explicit booleans")
        if browse_only:
            if allow_experiments or reader != "qwen35" or idle_timeout != 0:
                raise ValueError("browse-only has no model, resident timeout or isolated experiments")
            if not isinstance(state_directory, (str, Path)):
                raise ValueError("browse-only requires an explicit absolute state directory")
            state_directory = _absolute_deployment_path(str(state_directory), "browse-only state directory")
        elif state_directory is not None:
            raise ValueError("an explicit state directory is only available in browse-only mode")
        if reader not in ("qwen35", "gemma12b"):
            raise ValueError("reading desk reader must be qwen35 or gemma12b")
        if type(idle_timeout) is not int or not 0 <= idle_timeout <= 86400:
            raise ValueError("idle timeout must be an integer from 0 to 86400 seconds")
        self.browse_only = browse_only
        self.reader = None if browse_only else reader
        self.source = source.expanduser().resolve(strict=True)
        self.reading_source = self.source
        self.comparison = None
        assets = None if browse_only else _resolve_fix_asset_root(reader)
        self.assets = assets
        if assets is not None and _paths_overlap(self.source, assets):
            raise ValueError("project and Forge8 assets must not overlap")
        runs = (_create_runs_parent(None, str(state_directory), source_root=self.source) if browse_only
            else _fix_runs_parent(assets, source_root=self.source))
        self.root = runs / _new_task_id("read")
        self.root.mkdir(mode=0o700)
        self.model = None
        self.model_release_pending = False
        self.idle_timeout = idle_timeout
        self.lock = threading.RLock()
        self.call_scan_lock = threading.Lock()
        self.closed = False
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.allow_experiments = allow_experiments
        self.experiment_worker: threading.Thread | None = None
        self.experiment_cancel_event = threading.Event()
        self.experiment: dict = {"id": None, "status": "idle"}
        self._trial_candidate: bytes | None = None
        self._trial_baseline: bytes | None = None
        self._trial_revision = 0
        self.experiment_started = 0.0
        self.job: dict = {"id": None, "status": "idle", "phase": "", "elapsed_seconds": 0, "reader": self.reader}
        self._history: list[bytes] = []
        self._history_evicted = 0
        self.started = 0.0
        self.finished: float | None = None
        self.publishing = False
        self.project: dict = {}
        self.contents: dict[str, list[str]] = {}
        self.outlines: dict[str, dict] = {}
        self.observation, self.observation_error = None, None
        if observation is not None:
            try:
                self.observation = _load_observation(observation)
            except (ValueError, OSError, TypeError, RecursionError):
                self.observation_error = "觀測報告無法載入：需為完整、未重導且不超過 128 KiB 的 JSON。"
        self.refresh()
        if idle_timeout:
            self.model = ResidentModel(self.root, idle_timeout=idle_timeout, reader=reader)

    def _busy(self) -> bool:
        model_busy = self.model is not None and self.model.status()["state"] not in {"idle", "unloaded"}
        return self.model_release_pending or model_busy or self.experiment.get("cleanup_unknown") is True or any(worker is not None and worker.is_alive()
                   for worker in (self.worker, self.experiment_worker))

    def model_status(self) -> dict:
        return {"enabled": False} if self.model is None else {"enabled": True, **self.model.status()}

    def release_model(self, payload: dict) -> dict:
        if type(payload) is not dict or set(payload) != {"session_id"}:
            raise ValueError("expected the exact resident session id")
        identifier = _bounded_text(payload["session_id"], "resident session id", 200)
        with self.lock:
            if self.closed or self.model is None:
                raise ValueError("resident model controls are unavailable")
            if self._busy():
                raise ValueError("wait for the current operation before releasing the model")
            self.model_release_pending = True
        # Never hold the desk lock over native stop, hashes, or a receipt write.
        try:
            self.model.release(identifier)
            return self.model_status()
        finally:
            with self.lock:
                self.model_release_pending = False

    @staticmethod
    def _job_complete(job: dict) -> bool:
        result = job.get("result")
        if isinstance(result, dict) and result.get("kind") in {"forge8.explain.request", "forge8.locate.request"}:
            return (_reading_request_complete(result)
                and job.get("request_completion") == result.get("request_completion"))
        return job.get("gpu") == "released"

    def refresh(self, *, mode: str | None = None) -> dict:
        with self.lock:
            if self.closed:
                raise ValueError("reading desk is closing")
            if self._busy():
                raise ValueError("an operation is still running; wait for cleanup before refreshing")
            if mode is None:
                mode = "changes" if self.comparison is not None else "source"
            if mode not in ("source", "changes"):
                raise ValueError("unknown reading mode")
            if self.browse_only and mode != "source":
                raise ValueError("Git comparison is unavailable in browse-only mode; reopen normal read to use it")
            comparison = None
            if mode == "changes":
                # Synthetic reading sources cannot be descendants of state/runs:
                # the shared reader rejects that entire source/output overlap.
                parent = self.root.parent.parent / "comparison-sources"
                if _paths_overlap(parent, self.assets):
                    raise ValueError("change reading requires a state directory outside the assets; "
                        "configure --assets and a separate --state directory, then reopen the desk")
                parent.mkdir(mode=0o700, exist_ok=True)
                _require_run_entry(parent, directory=True, label="comparison sources")
                if parent.resolve(strict=True) != parent:
                    raise ValueError("comparison source directory is redirected")
                comparison = prepare_comparison(self.source, parent / _new_task_id("comparison"))
                snapshot = comparison.snapshot
            else:
                destination = self.root / _new_task_id("browse") / "source"
                destination.parent.mkdir(mode=0o700)
                snapshot = prepare_repository_snapshot(self.source, destination, for_explanation=True)
            version, counts = _snapshot_identity(snapshot)
            files, contents, outlines, nonphysical = [], {}, {}, set()
            outline_budget = _OUTLINE_CACHE_BYTES
            count_map = dict(counts)
            for index, fingerprint in enumerate(snapshot.fingerprints):
                path = Path(snapshot.snapshot_root) / fingerprint.path
                _require_run_entry(path, directory=False, label="browse snapshot file")
                if path.resolve(strict=True) != path:
                    raise ValueError("browse snapshot contains a redirected path")
                data = path.read_bytes()
                if len(data) != fingerprint.size_bytes or hashlib.sha256(data).hexdigest() != fingerprint.sha256:
                    raise ValueError("browse snapshot changed during admission")
                identifier = str(index)
                text = data.decode("utf-8")
                if any(c in text for c in "\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
                    nonphysical.add(fingerprint.path)
                outline = _python_outline(fingerprint.path, text)
                outline_size = len(json.dumps(outline, ensure_ascii=False).encode("utf-8"))
                # Reserve room for every remaining file's fixed fallback status.
                # Serialized metadata is bounded; Python object overhead remains.
                if outline_size + 128 * (len(snapshot.fingerprints) - index - 1) > outline_budget:
                    outline = _outline_status("limited", "metadata_limit")
                    outline_size = len(json.dumps(outline).encode("utf-8"))
                outline_budget -= outline_size
                outlines[identifier] = outline
                # Cache content from at most 16 MiB of admitted source (Python
                # objects add overhead). Never serve live paths through HTTP.
                contents[identifier] = [
                    "".join(c if c == "\t" or c.isprintable() else f"\\u{ord(c):04x}" for c in line)
                    for line in text.splitlines()
                ]
                files.append({"id": identifier, "path": fingerprint.path, "lines": count_map[fingerprint.path]})
            project = {"name": self.source.name, "version": version, "files": files,
                "excluded": list(snapshot.excluded), "reader": self.reader,
                "experiments_enabled": self.allow_experiments,
                "comparison": comparison.view() if comparison is not None else None,
                "observation": None}
            if self.browse_only:
                project["browse_only"] = True
            if self.idle_timeout:
                project["model_policy"] = {"mode": "resident", "idle_timeout_seconds": self.idle_timeout}
            if comparison is None and self.observation is not None:
                try:
                    project["observation"] = _observation_view(self.observation, files, snapshot.fingerprints, nonphysical)
                except (ValueError, KeyError, TypeError, RecursionError):
                    self.observation = None
                    self.observation_error = "觀測已撤下：報告不完整、事件無效或來源版本不符。請重新觀測並開啟閱讀桌。"
            project["observation_error"] = self.observation_error if comparison is None else None
            self.project = project
            self._trial_candidate = self._trial_baseline = None
            self._trial_revision += 1
            self.comparison = comparison
            self.reading_source = comparison.reading_source if comparison is not None else self.source
            self.browse_snapshot = snapshot
            self.contents = contents
            self.outlines = outlines
            self.nonphysical = nonphysical
            self.job = {"id": None, "status": "idle", "phase": "", "elapsed_seconds": 0, "reader": self.reader}
            self.experiment = {"id": None, "status": "idle"}
            self._history.clear()
            self._history_evicted = 0
            return self.project

    def source_view(self, file: str, version: str) -> dict:
        with self.lock:
            if version != self.project["version"] or file not in self.contents:
                raise ValueError("unknown file or stale source version; refresh the view")
            item = next(item for item in self.project["files"] if item["id"] == file)
            return {"path": item["path"], "version": version, "lines": self.contents[file], "outline": self.outlines[file]}

    def search(self, query: str) -> dict:
        _bounded_text(query, "literal search", 128)
        with self.lock:
            matches = []
            for item in self.project["files"]:
                for number, line in enumerate(self.contents[item["id"]], 1):
                    if query in line:
                        if len(matches) == 40:
                            return {"matches": matches, "truncated": True}
                        matches.append({"file": item["id"], "path": item["path"], "line": number, "text": line[:400]})
            return {"matches": matches, "truncated": False}

    def _retained_file(self, file: str, version: str):
        """Read raw fingerprint-bound bytes under the desk lock, never display text."""
        if self.closed:
            raise ValueError("reading desk is closing")
        if version != self.project["version"]:
            raise ValueError("stale source version; refresh the view")
        item = next((item for item in self.project["files"] if item["id"] == file), None)
        if item is None:
            raise ValueError("unknown source file")
        snapshot = self.browse_snapshot
        fingerprint = next(pin for pin in snapshot.fingerprints if pin.path == item["path"])
        path, data = _retained_bytes(snapshot, fingerprint)
        return item, fingerprint, path, data

    def context(self, payload: dict) -> dict:
        """Inspect retained same-file name origins, without an inference request."""
        if type(payload) is not dict or set(payload) != {"file", "version", "start", "end"}:
            raise ValueError("expected one source selection and version")
        with self.lock:
            item, _, _, data = self._retained_file(payload["file"], payload["version"])
            first, last = payload["start"], payload["end"]
            if type(first) is not int or type(last) is not int or not 1 <= first <= last <= item["lines"] or last - first >= 80:
                raise ValueError("select an inclusive range of at most 80 existing lines")
            return {"file": item["id"], "path": item["path"], "version": self.project["version"],
                "start": first, "end": last,
                **selected_context(item["path"], data.decode("utf-8"), first, last,
                    include_selected=True)}

    def import_source(self, payload: dict) -> dict:
        """Follow an explicitly chosen import declaration within this snapshot.

        The request cannot supply a module name or target path. Recompute C1 from
        the original retained selection, then inspect only same-side admitted
        paths. No host import machinery or project execution participates.
        """
        expected = {"file", "version", "start", "end", "binding_id",
            "declaration_start", "declaration_end"}
        if type(payload) is not dict or set(payload) != expected:
            raise ValueError("expected one selected import declaration and source version")
        if (type(payload["binding_id"]) is not str
                or type(payload["declaration_start"]) is not int
                or type(payload["declaration_end"]) is not int):
            raise ValueError("invalid selected import declaration")
        with self.lock:
            selection = {key: payload[key] for key in ("file", "version", "start", "end")}
            context = self.context(selection)
            candidates = [row for row in context["candidates"]
                if row["binding_id"] == payload["binding_id"]
                and row["start_line"] == payload["declaration_start"]
                and row["end_line"] == payload["declaration_end"]
                and row["kind"] in {"import", "from import"}]
            if len(candidates) != 1:
                raise ValueError("selected import is unavailable; inspect name sources again")
            item, _, _, data = self._retained_file(payload["file"], payload["version"])
            prefix = ""
            if self.comparison is not None:
                side, separator, _ = item["path"].partition("/")
                if separator != "/" or side not in {"before", "after"}:
                    raise ValueError("select a HEAD or current source file")
                prefix = side + "/"
            members = {row["path"][len(prefix):]: row for row in self.project["files"]
                if row["path"].startswith(prefix)}
            logical_path = item["path"][len(prefix):]
            touched = {logical_path}

            def read_text(path):
                if path not in members:
                    raise ValueError("import target is outside the retained source snapshot")
                target, _, _, raw = self._retained_file(members[path]["id"], payload["version"])
                touched.add(path)
                return raw.decode("utf-8")

            result = trace_import(list(members), logical_path, data.decode("utf-8"),
                candidates[0], read_text)
            # Detect ordinary retained-byte changes during a multi-file read;
            # this is not a hostile pathname-race lock or live-source refresh.
            for path in sorted(touched):
                self._retained_file(members[path]["id"], payload["version"])
            for route in result["routes"]:
                for step in route["steps"]:
                    target = members[step["path"]]
                    step.update(file=target["id"], path=target["path"])
            return {**payload, "path": item["path"], **result}

    def prepare_experiment_input(self, payload: dict) -> dict:
        """Convert a selected literal call, without preparing or resolving its target."""
        from .experiments import prepare_call_inputs
        if (type(payload) is not dict or set(payload) != {"file", "version", "start", "end", "entry"}
                or type(payload["file"]) is not str or type(payload["version"]) is not str):
            raise ValueError("expected one source selection, version and function name")
        with self.lock:
            if not self.allow_experiments:
                raise ValueError("isolated experiments are disabled; reopen with read --allow-experiments to opt in")
            if self.comparison is not None:
                raise ValueError("experiments require ordinary source mode, not a HEAD comparison")
            item, pin, _, data = self._retained_file(payload["file"], payload["version"])
            if not item["path"].endswith(".py") or item["path"] in self.nonphysical:
                raise ValueError("select one Python module with physical source lines")
            first, last = payload["start"], payload["end"]
            if type(first) is not int or type(last) is not int or not 1 <= first <= last <= item["lines"] or last - first >= 80:
                raise ValueError("select an inclusive range of at most 80 existing lines")
            entry = _bounded_text(payload["entry"], "function name", 200)
            return {**prepare_call_inputs(data, entry, first, last),
                "file": item["id"], "path": item["path"], "version": self.project["version"],
                "source_sha256": pin.sha256}

    def prepare_experiment(self, payload: dict) -> dict:
        from .experiments import RUNTIME_NAME, native_platform, prepare_module, prepare_module_set
        if type(payload) is dict and "mode" in payload:
            return self._prepare_paired_experiment(payload)
        expected = {"file", "version", "entry"}
        if type(payload) is not dict or set(payload) not in (expected, expected | {"modules"}):
            raise ValueError("expected a source file, version and function name")
        with self.lock:
            if not self.allow_experiments:
                raise ValueError("isolated experiments are disabled; reopen with read --allow-experiments to opt in")
            if self.comparison is not None:
                raise ValueError("experiments require ordinary source mode, not a HEAD comparison")
            item, pin, _, data = self._retained_file(payload["file"], payload["version"])
            if not item["path"].endswith(".py") or item["path"] in self.nonphysical:
                raise ValueError("select one Python module with physical source lines")
            entry = _bounded_text(payload["entry"], "function name", 200)
            prepare_module(data, entry)  # Static only; no import or function extraction.
            module_set = None
            if "modules" in payload:
                identifiers = payload["modules"]
                if (type(identifiers) is not list or not 1 <= len(identifiers) <= 4
                        or any(type(value) is not str for value in identifiers)
                        or len(set(identifiers)) != len(identifiers) or item["id"] not in identifiers):
                    raise ValueError("select 1–4 unique source files, including the entry module")
                selected = {}
                for identifier in identifiers:
                    member, _, _, raw = self._retained_file(identifier, payload["version"])
                    if not member["path"].endswith(".py") or member["path"] in self.nonphysical:
                        raise ValueError("module selection requires Python files with physical source lines")
                    selected[member["path"]] = raw
                module_set = prepare_module_set(selected, item["path"], entry)
            runtime = self.assets / f"experiment-{RUNTIME_NAME}-{native_platform()}"
            if not (runtime / "runtime.json").is_file():
                raise ValueError("optional experiment runtime is not installed; use forge8 experiment setup --archives <pinned-local-archives> first")
            target = {"file": item["id"], "path": item["path"], "version": self.project["version"],
                    "entry": entry, "source_sha256": pin.sha256, "source_bytes": pin.size_bytes}
            if module_set is not None:
                target["module_set"] = module_set
            return target

    def _prepare_paired_experiment(self, payload: dict) -> dict:
        from .experiments import RUNTIME_NAME, native_platform, prepare_input_search, prepare_module
        searching = "search" in payload
        expected = {"mode", "file", "version", "entry"} | ({"search", "input_text"} if searching else set())
        if (set(payload) != expected
                or payload["mode"] != "head_current"
                or any(type(payload[key]) is not str for key in payload)
                or searching and payload["search"] not in ("nearby-v1", "source-v1")):
            raise ValueError("expected an explicit HEAD/current mode and one current-version function")
        with self.lock:
            if not self.allow_experiments:
                raise ValueError("isolated experiments are disabled; reopen with read --allow-experiments to opt in")
            if self.comparison is None:
                raise ValueError("paired experiments require a retained HEAD/current comparison")
            item, pin, _, data = self._retained_file(payload["file"], payload["version"])
            if not item["path"].startswith("after/") or not item["path"].endswith(".py"):
                raise ValueError("select a current-version Python module (after/)")
            before_path = "before/" + item["path"][6:]
            before = next((member for member in self.project["files"] if member["path"] == before_path), None)
            if before is None:
                raise ValueError("no same-path HEAD module exists; renamed or added modules are not paired")
            prior, prior_pin, _, prior_data = self._retained_file(before["id"], payload["version"])
            if item["path"] in self.nonphysical or prior["path"] in self.nonphysical:
                raise ValueError("paired modules require physical source lines")
            entry = _bounded_text(payload["entry"], "function name", 200)
            for raw in (prior_data, data):
                prepare_module(raw, entry)  # Both complete modules, static only.
            runtime = self.assets / f"experiment-{RUNTIME_NAME}-{native_platform()}"
            if not (runtime / "runtime.json").is_file():
                raise ValueError("optional experiment runtime is not installed; use forge8 experiment setup --archives <pinned-local-archives> first")
            target = {"mode": "head_current", "file": item["id"], "path": item["path"],
                "version": self.project["version"], "entry": entry,
                "source_sha256": pin.sha256, "source_bytes": pin.size_bytes,
                "head": self.comparison.head, "current_snapshot_sha256": self.comparison.original_snapshot_sha256,
                "before": {"file": prior["id"], "path": prior["path"],
                    "source_sha256": prior_pin.sha256, "source_bytes": prior_pin.size_bytes}}
            if searching:
                source_options = {"sources": (prior_data, data), "entry": entry} if payload["search"] == "source-v1" else {}
                target["search_plan"] = prepare_input_search(payload["input_text"].encode("utf-8"), **source_options)
            return target

    def start_experiment(self, payload: dict) -> dict:
        from .experiments import RUNTIME_NAME, _file, _validate_watch_names, native_platform, prepare_inputs, retain_trial_result, run_experiment
        if type(payload) is dict and "mode" in payload:
            return self._start_paired_experiment(payload)
        expected = {"file", "version", "entry", "source_sha256", "input_text", "allow_execution"}
        shapes = (expected, expected | {"modules", "module_set_sha256"})
        if (type(payload) is not dict or set(payload) - {"trace_lines", "generator_steps", "watch_names"} not in shapes
                or payload["allow_execution"] is not True):
            raise ValueError("explicit whole-module execution consent and an exact target are required")
        trace_lines = payload.get("trace_lines", False)
        if type(trace_lines) is not bool or trace_lines and "modules" in payload:
            raise ValueError("trace_lines must be a boolean; line visits support single-file trials only")
        generator_steps = payload.get("generator_steps")
        if "generator_steps" in payload and (type(generator_steps) is not int or not 1 <= generator_steps <= 12):
            raise ValueError("generator_steps must be an integer from 1 to 12")
        if generator_steps is not None and (trace_lines or "modules" in payload):
            raise ValueError("generator steps require a single-file trial without line tracing")
        watch_names = ()
        if "watch_names" in payload:
            if type(payload["watch_names"]) is not list or not 1 <= len(payload["watch_names"]) <= 3:
                raise ValueError("watch_names must contain 1-3 local names")
            watch_names = tuple(payload["watch_names"])
            _validate_watch_names(watch_names)
            if not trace_lines or "modules" in payload or generator_steps is not None:
                raise ValueError("watched locals require single-file line tracing without generator steps")
        execution_options = {"trace_lines": True} if trace_lines else {}
        if watch_names:
            execution_options["watch_names"] = watch_names
        if generator_steps is not None:
            execution_options["generator_steps"] = generator_steps
        public_options = {**execution_options, **({"watch_names": list(watch_names)} if watch_names else {})}
        with self.lock:
            if self._busy():
                raise ValueError("only one model question or isolated experiment may run at a time")
            selection = {key: payload[key] for key in ("file", "version", "entry")}
            if "modules" in payload:
                selection["modules"] = payload["modules"]
            target = self.prepare_experiment(selection)
            if payload["source_sha256"] != target["source_sha256"]:
                raise ValueError("selected module changed; prepare the target again")
            module_options = {}
            if "module_set" in target:
                if payload["module_set_sha256"] != target["module_set"]["sha256"]:
                    raise ValueError("selected module set changed; prepare all files again")
                module_options = {"module_root": Path(self.browse_snapshot.snapshot_root),
                    "module_files": [member["path"] for member in target["module_set"]["files"]],
                    "expected_module_set_sha256": target["module_set"]["sha256"]}
            if type(payload["input_text"]) is not str:
                raise ValueError("input must be raw JSON text, not a browser-converted object")
            raw_input = payload["input_text"].encode("utf-8")
            prepare_inputs(raw_input)
            # Use the actual retained module; never reconstruct sanitized display
            # lines or replace it with the current working tree's source.
            source = Path(self.browse_snapshot.snapshot_root) / target["path"]
            identifier = _new_task_id("experiment")
            directory = self.root / identifier
            directory.mkdir(mode=0o700)
            inputs = directory / "arguments.json"
            with inputs.open("xb") as handle:
                handle.write(raw_input)
            runtime = self.assets / f"experiment-{RUNTIME_NAME}-{native_platform()}"
            self.experiment_cancel_event = threading.Event()
            cancellation = self.experiment_cancel_event
            started = time.monotonic()
            self.experiment_started = started
            self._trial_candidate = None
            self.experiment = {**target, "id": identifier, "status": "running",
                "input_text": payload["input_text"], "elapsed_seconds": 0.0, **public_options}

            def run():
                try:
                    manifest_before = None
                    if not module_options and generator_steps is None and not watch_names:
                        try:
                            manifest_before = _file(runtime / "runtime.json", 512 * 1024)
                        except (ValueError, OSError, TypeError):
                            pass  # Optional comparison cannot change the ordinary runner's gates.
                    report = run_experiment(runtime, source, target["entry"], inputs,
                        directory / "execution", allow_execution=True,
                        expected_source_sha256=target["source_sha256"], cancel_requested=cancellation.is_set,
                        **module_options, **execution_options,
                        **({"expected_input_sha256": hashlib.sha256(raw_input).hexdigest()}
                           if trace_lines or generator_steps is not None else {}))
                    execution = report.get("execution") or {}
                    complete = (report["source_unchanged"] and report["runtime_unchanged"]
                        and report["process_status"] == "passed" and not execution.get("output_limit", False)
                        and (execution.get("host_status"), execution.get("detail")) in (("exited", None), ("guest_exit", 0))
                        and (generator_steps is None or report.get("reported_result") is not None))
                    candidate = None
                    if complete and manifest_before is not None:
                        try:
                            if (_file(runtime / "runtime.json", 512 * 1024) != manifest_before
                                    or _file(inputs, 16_384) != raw_input):
                                raise ValueError("trial comparison runtime manifest or input changed")
                            candidate = retain_trial_result(target, identifier, payload["input_text"], report,
                                _file(directory / "execution" / "experiment.json", 2 * 1024**2),
                                manifest_before, _file(source, 65_536), directory / "execution", trace_lines=trace_lines)
                        except (ValueError, OSError, TypeError, KeyError, RecursionError, OverflowError):
                            pass  # Still show the original trial, but never pin or compare it.
                    with self.lock:
                        if not cancellation.is_set():
                            self._trial_candidate = candidate
                            self.experiment.update(status="completed" if complete else "incomplete",
                                process_status=report["process_status"], host_status=execution.get("host_status", "unavailable"),
                                source_unchanged=report["source_unchanged"], runtime_unchanged=report["runtime_unchanged"])
                            reported = report.get("reported_result")
                            if trace_lines:
                                # Separately parsed guest data; never a host observation,
                                # model fact, or authority to execute another call.
                                self.experiment["reported_trace"] = deepcopy(report.get("reported_trace")) if complete else None
                            if reported is not None:
                                # JSON as TEXT: passing a result object through a
                                # browser JSON decoder would round large integers.
                                text = json.dumps(reported, ensure_ascii=False, allow_nan=False, indent=2)
                                if generator_steps is not None and len(json.dumps(reported, ensure_ascii=True, indent=2)) > 128 * 1024:
                                    text = json.dumps(reported, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                                # Keep readable Unicode, but expose invisible/bidi
                                # controls and lone surrogates as JSON escapes.
                                self.experiment["result_text"] = "".join(character if character.isprintable() or character == "\n"
                                    else json.dumps(character)[1:-1] for character in text)
                            for name in ("stdout", "stderr"):
                                text = execution.get("guest_output", {}).get(name, "")
                                if reported is not None and name == "stdout":
                                    text = "\n".join(line for line in text.splitlines() if not line.startswith("FORGE8_GUEST_RESULT="))
                                if self.experiment.get("reported_trace") is not None and name == "stdout":
                                    text = "\n".join(line for line in text.splitlines() if not line.startswith("FORGE8_GUEST_TRACE="))
                                self.experiment[name] = text
                except CheckCleanupError as exc:
                    with self.lock:
                        self.experiment.update(status="incomplete", cleanup_unknown=True,
                            error=f"Owned process cleanup is unconfirmed; new work is blocked. {exc}"[:1000])
                except KeyboardInterrupt:
                    cancellation.set()
                except Exception as exc:
                    with self.lock:
                        self.experiment.update(status="incomplete", error=f"{type(exc).__name__}: {exc}"[:1000])
                finally:
                    with self.lock:
                        if cancellation.is_set() and not self.experiment.get("cleanup_unknown"):
                            self._trial_candidate = None
                            self.experiment = {**target, "id": identifier, "status": "cancelled",
                                "input_text": payload["input_text"], **public_options}
                        self.experiment["elapsed_seconds"] = round(time.monotonic() - started, 2)

            self.experiment_worker = threading.Thread(target=run, name="forge8-isolated-experiment")
            self.experiment_worker.start()
            return {"id": identifier}

    def _start_paired_experiment(self, payload: dict) -> dict:
        from .experiments import (RUNTIME_NAME, _file, _json, _write_json, compare_reported_results,
                                  native_platform, prepare_inputs, run_experiment)
        searching = "search" in payload
        expected = {"mode", "file", "version", "entry", "source_sha256", "before_sha256",
                    "head", "input_text", "allow_execution"} | ({"search", "search_plan_sha256"} if searching else set())
        if (set(payload) != expected or payload["allow_execution"] is not True
                or searching and payload["search"] not in ("nearby-v1", "source-v1")):
            raise ValueError("explicit consent for both complete modules and exact HEAD/current identities is required")
        with self.lock:
            if self._busy():
                raise ValueError("only one model question or isolated experiment may run at a time")
            selection = {key: payload[key] for key in ("mode", "file", "version", "entry")}
            if searching:
                selection.update(search=payload["search"], input_text=payload["input_text"])
            target = self._prepare_paired_experiment(selection)
            search_plan = target.pop("search_plan", None)
            if (payload["source_sha256"] != target["source_sha256"]
                    or payload["before_sha256"] != target["before"]["source_sha256"] or payload["head"] != target["head"]):
                raise ValueError("paired sources or HEAD changed; prepare both modules again")
            if type(payload["input_text"]) is not str:
                raise ValueError("input must be raw JSON text, not a browser-converted object")
            raw_input = payload["input_text"].encode("utf-8")
            prepare_inputs(raw_input)
            if searching and payload["search_plan_sha256"] != search_plan["sha256"]:
                raise ValueError("input search plan changed; preview and authorize the exact inputs again")
            input_sha = hashlib.sha256(raw_input).hexdigest()
            captured = self.comparison
            snapshot = Path(self.browse_snapshot.snapshot_root)
            runtime = self.assets / f"experiment-{RUNTIME_NAME}-{native_platform()}"
            identifier = _new_task_id("experiment")
            directory = self.root / identifier
            directory.mkdir(mode=0o700)
            inputs = directory / "arguments.json"
            with inputs.open("xb") as handle:
                handle.write(raw_input)
            cancellation = self.experiment_cancel_event = threading.Event()
            started = self.experiment_started = time.monotonic()
            self.experiment = {**target, "id": identifier, "status": "running", "phase": "checking",
                "input_text": payload["input_text"], "elapsed_seconds": 0.0, "observations": {},
                "comparison_result": "unavailable", "comparison_rule": "canonical-json-v1",
                "comparison_unchanged": False}
            if searching:
                self.experiment["search"] = {"strategy": search_plan["strategy"], "plan_sha256": search_plan["sha256"],
                    "total": len(search_plan["inputs"]), "completed": 0, "case_index": 1,
                    "input_text": payload["input_text"], "stop_reason": None}

            def run():
                guards, reports, report_pins = [], {}, {}
                runtime_sha = None
                runtime_identity = None
                completed_result = None
                deadline = started + search_plan["max_seconds"] if searching else None
                cases, retained = [], {"arguments.json": input_sha}

                def interrupted():
                    return cancellation.is_set() or deadline is not None and time.monotonic() >= deadline

                def check_sources():
                    if interrupted():
                        raise KeyboardInterrupt
                    gate = captured.guard()  # No desk lock: cancellation stays available.
                    guards.append({"ok": gate.ok, "reason": gate.reason, "evidence": gate.evidence})
                    if interrupted():
                        raise KeyboardInterrupt
                    if not gate.ok:
                        raise ValueError(gate.reason or "paired comparison source check failed")

                def run_pair(pair_directory, pair_inputs, pair_sha, case_record):
                    nonlocal guards, reports, report_pins, runtime_sha, runtime_identity
                    guards, reports, report_pins = [], {}, {}
                    def report_bytes(side):
                        return _file(pair_directory / side / "experiment.json", 2 * 1024**2)
                    check_sources()
                    manifest_sha = hashlib.sha256(_file(runtime / "runtime.json", 128 * 1024)).hexdigest()
                    if runtime_sha is not None and manifest_sha != runtime_sha:
                        raise ValueError("paired runtime manifest changed between inputs")
                    runtime_sha = manifest_sha
                    for side, member in (("before", target["before"]), ("after", target)):
                        if side == "after":
                            check_sources()
                        if (hashlib.sha256(_file(pair_inputs, 16_384)).hexdigest() != pair_sha
                                or hashlib.sha256(_file(runtime / "runtime.json", 128 * 1024)).hexdigest() != runtime_sha):
                            raise ValueError("paired input or runtime manifest changed before execution")
                        with self.lock:
                            if interrupted():
                                raise KeyboardInterrupt
                            self.experiment["phase"] = side
                        report = run_experiment(runtime, snapshot / member["path"], target["entry"], pair_inputs,
                            pair_directory / side, allow_execution=True, expected_source_sha256=member["source_sha256"],
                            expected_input_sha256=pair_sha, cancel_requested=interrupted)
                        raw_report = report_bytes(side)
                        report_sha = hashlib.sha256(raw_report).hexdigest()
                        if searching:
                            retained[(pair_directory / side / "experiment.json").relative_to(directory).as_posix()] = report_sha
                            case_record["child_reports"][side] = report_sha
                        # Bind exactly the controller's actual immutable report, not
                        # a fresh summary or a guest-supplied artifact path.
                        if (json.dumps(_json(raw_report), sort_keys=True, ensure_ascii=True, allow_nan=False)
                                != json.dumps(report, sort_keys=True, ensure_ascii=True, allow_nan=False)
                                or type(report.get("schema_version")) is not int or report["schema_version"] != 1
                                or report.get("kind") != "forge8.experiment"
                                or any(report.get("identity", {}).get(key) != value for key, value in
                                    (("source_sha256", member["source_sha256"]), ("input_sha256", pair_sha), ("entry", target["entry"])))):
                            raise ValueError("paired child report identity changed")
                        reports[side] = report
                        report_pins[side] = report_sha
                        observation = {**_paired_observation(report), "report_sha256": report_pins[side]}
                        with self.lock:
                            self.experiment["observations"][side] = observation
                        if not observation["complete"]:
                            raise ValueError(f"{side} trial did not complete with intact sources, runtime and cleanup; no comparison")
                        if searching and "result_text" not in observation:
                            raise ValueError(f"{side} guest result is unavailable; input search stopped")
                    with self.lock:
                        self.experiment["phase"] = "checking"
                    check_sources()
                    if (hashlib.sha256(_file(runtime / "runtime.json", 128 * 1024)).hexdigest() != runtime_sha
                            or json.dumps(reports["before"]["runtime"], sort_keys=True, allow_nan=False)
                                != json.dumps(reports["after"]["runtime"], sort_keys=True, allow_nan=False)
                            or hashlib.sha256(_file(pair_inputs, 16_384)).hexdigest() != pair_sha
                            or any(hashlib.sha256(report_bytes(side)).hexdigest() != pin for side, pin in report_pins.items())):
                        raise ValueError("paired runtime, input or child report changed")
                    if searching:
                        identity = json.dumps(reports["before"]["runtime"], sort_keys=True, allow_nan=False)
                        if runtime_identity is not None and identity != runtime_identity:
                            raise ValueError("paired reported runtime changed between inputs")
                        runtime_identity = identity
                    result = compare_reported_results(reports["before"].get("reported_result"), reports["after"].get("reported_result"))
                    return result

                try:
                    if not searching:
                        completed_result = run_pair(directory, inputs, input_sha, None)
                    else:
                        for index, item in enumerate(search_plan["inputs"], 1):
                            if interrupted():
                                raise KeyboardInterrupt
                            pair_directory = directory / f"case-{index:02}"
                            pair_directory.mkdir(mode=0o700)
                            pair_inputs = pair_directory / "arguments.json"
                            data = item["input_text"].encode("utf-8")
                            with pair_inputs.open("xb") as handle:
                                handle.write(data)
                            pair_sha = hashlib.sha256(data).hexdigest()
                            retained[pair_inputs.relative_to(directory).as_posix()] = pair_sha
                            case_record = {"index": index, "input_sha256": pair_sha,
                                "child_reports": {}, "receipt_sha256": None}
                            cases.append(case_record)
                            with self.lock:
                                if interrupted():
                                    raise KeyboardInterrupt
                                self.experiment.update(phase="checking", observations={})
                                self.experiment["search"].update(case_index=index, input_text=item["input_text"])
                            result = run_pair(pair_directory, pair_inputs, pair_sha, case_record)
                            if result == "unavailable":
                                raise ValueError("paired guest results are unavailable; input search stopped")
                            with self.lock:
                                if interrupted():
                                    raise KeyboardInterrupt
                                pair_final = deepcopy(self.experiment)
                                pair_final.pop("search")
                                pair_final.update(id=f"{identifier}-case-{index:02}", input_text=item["input_text"],
                                    status="completed", comparison_result=result, comparison_unchanged=True,
                                    elapsed_seconds=round(time.monotonic() - started, 2))
                            pair_receipt = {"schema_version": 1, "kind": "forge8.paired_experiment", "target": target,
                                "input_sha256": pair_sha, "runtime_manifest_sha256": runtime_sha,
                                "guards": guards, "child_reports": dict(report_pins), "result": pair_final,
                                "notice": "Two guest-reported JSON observations; not a trusted oracle, equivalence, causal proof or validation of an AI explanation."}
                            path = pair_directory / "comparison-experiment.json"
                            _write_json(path, pair_receipt)
                            case_record["receipt_sha256"] = hashlib.sha256(_file(path, 2 * 1024**2)).hexdigest()
                            retained[path.relative_to(directory).as_posix()] = case_record["receipt_sha256"]
                            with self.lock:
                                self.experiment["search"]["completed"] = index
                            if result == "different":
                                break
                        check_sources()
                        if (hashlib.sha256(_file(runtime / "runtime.json", 128 * 1024)).hexdigest() != runtime_sha
                                or any(hashlib.sha256(_file(directory / name, 2 * 1024**2)).hexdigest() != pin
                                    for name, pin in retained.items())):
                            raise ValueError("input search retained input, runtime or paired receipt/report changed")
                        completed_result = result
                except CheckCleanupError as exc:
                    with self.lock:
                        self.experiment.update(status="incomplete", cleanup_unknown=True,
                            error=f"Owned process cleanup is unconfirmed; new work is blocked. {exc}"[:1000])
                except KeyboardInterrupt:
                    if not (deadline is not None and time.monotonic() >= deadline):
                        cancellation.set()
                except Exception as exc:
                    with self.lock:
                        self.experiment.update(status="incomplete", error=f"{type(exc).__name__}: {exc}"[:1000])
                finally:
                    with self.lock:
                        final = deepcopy(self.experiment)
                        budget_exhausted = deadline is not None and time.monotonic() >= deadline
                        if cancellation.is_set() and not final.get("cleanup_unknown"):
                            final["status"] = "cancelled"
                        elif budget_exhausted:
                            final["status"] = "incomplete"
                        elif completed_result is not None:
                            final.update(status="completed", comparison_result=completed_result, comparison_unchanged=True)
                        if final["status"] != "completed":
                            final.update(comparison_result="unavailable", comparison_unchanged=False)
                        final["elapsed_seconds"] = round(time.monotonic() - started, 2)
                        receipt = {"schema_version": 1, "kind": "forge8.paired_experiment", "target": target,
                            "input_sha256": input_sha, "runtime_manifest_sha256": runtime_sha,
                            "guards": guards, "child_reports": report_pins, "result": final,
                            "notice": "Two guest-reported JSON observations; not a trusted oracle, equivalence, causal proof or validation of an AI explanation."}
                        if searching:
                            reason = ("cancelled" if final["status"] == "cancelled" else "incomplete"
                                if final.get("cleanup_unknown") else "budget" if budget_exhausted
                                else "different" if final["status"] == "completed" and completed_result == "different"
                                else "exhausted" if final["status"] == "completed" else "incomplete")
                            final["search"]["stop_reason"] = reason
                            receipt = {"schema_version": 1, "kind": "forge8.paired_input_search", "target": target,
                                "plan": search_plan, "cases": cases, "retained_sha256": retained,
                                "runtime_manifest_sha256": runtime_sha, "result": final,
                                "notice": "Bounded guest-reported differences only; no equivalence, causal proof or model validation."}
                        try:
                            _write_json(directory / "comparison-experiment.json", receipt)
                        except Exception as exc:
                            final.update(status="incomplete", comparison_result="unavailable", comparison_unchanged=False,
                                error=f"Paired receipt could not be written: {type(exc).__name__}"[:1000])
                            if searching:
                                final["search"]["stop_reason"] = "incomplete"
                        # One publication boundary: no completed result before its
                        # receipt; cancellation accepted before this lock wins.
                        self.experiment = final

            self.experiment_worker = threading.Thread(target=run, name="forge8-isolated-experiment")
            try:
                self.experiment_worker.start()
            except RuntimeError as exc:
                self.experiment_worker = None
                self.experiment.update(status="incomplete", error=f"Trial worker did not start: {exc}"[:1000])
                if searching:
                    self.experiment["search"]["stop_reason"] = "incomplete"
            return {"id": identifier}

    def experiment_status(self) -> dict:
        with self.lock:
            value = dict(self.experiment)
            if "module_set" in value:
                value["module_set"] = deepcopy(value["module_set"])
            for key in ("before", "observations", "search", "watch_names", "reported_trace"):
                if key in value:
                    value[key] = deepcopy(value[key])
            if value["status"] in {"running", "cancelling"}:
                value["elapsed_seconds"] = round(time.monotonic() - self.experiment_started, 2)
            if (self.allow_experiments and self.comparison is None and value.get("id") is not None
                    and "module_set" not in value):
                value["input_comparison"] = self._trial_comparison_status()
            return value

    def _trial_comparison_status(self) -> dict:
        """Pure projection under the desk lock; never read files or start work."""
        from .experiments import compare_trial_results, trial_result_view
        alive = self.experiment_worker is not None and self.experiment_worker.is_alive()
        candidate = self._trial_candidate
        current = trial_result_view(candidate) if candidate is not None else None
        eligible = (not self.closed and not alive and self.experiment.get("status") == "completed"
            and "generator_steps" not in self.experiment and not self.experiment.get("watch_names")
            and self.experiment.get("source_unchanged") is True and self.experiment.get("runtime_unchanged") is True
            and self.experiment.get("cleanup_unknown") is not True and current is not None
            and current["id"] == self.experiment["id"] and current["version"] == self.project["version"]
            and all(current[key] == self.experiment.get(key) for key in
                ("file", "path", "entry", "source_sha256", "source_bytes", "input_text"))
            and current["trace_lines"] is (self.experiment.get("trace_lines") is True))
        outcome, reason = compare_trial_results(self._trial_baseline, candidate if eligible else None)
        baseline = trial_result_view(self._trial_baseline) if self._trial_baseline is not None else None
        return {"revision": self._trial_revision, "baseline": baseline, "current_id": self.experiment.get("id"),
            "can_pin": bool(eligible and not self._busy() and (baseline is None or baseline["id"] != current["id"])),
            "settling": bool(alive and self.experiment.get("status") not in {"running", "cancelling"}),
            "current_report_sha256": current["report_sha256"] if eligible else None,
            "outcome": outcome, "reason": reason}

    def set_trial_baseline(self, payload: dict, *, clear: bool = False) -> dict:
        """Pin/clear one exact completed host-owned report, never caller data."""
        if (type(payload) is not dict or set(payload) != {"id", "version", "revision"}
                or type(payload["id"]) is not str or not 1 <= len(payload["id"]) <= 200
                or type(payload["version"]) is not str or type(payload["revision"]) is not int
                or not 0 <= payload["revision"] <= 2**53 - 1 or type(clear) is not bool):
            raise ValueError("expected one trial id, source version and comparison revision")
        with self.lock:
            if not self.allow_experiments or self.browse_only:
                raise ValueError("isolated experiments are disabled")
            if self.closed or self._busy():
                raise ValueError("wait for the current work and cleanup before changing a trial baseline")
            if (self.comparison is not None or payload["version"] != self.project["version"]
                    or payload["revision"] != self._trial_revision):
                raise ValueError("trial comparison changed; read its current state before trying again")
            view = self._trial_comparison_status()
            if clear:
                if view["baseline"] is None or view["baseline"]["id"] != payload["id"]:
                    raise ValueError("the selected trial baseline is unavailable")
                self._trial_baseline = None
            else:
                if not view["can_pin"] or view["current_id"] != payload["id"]:
                    raise ValueError("only the current complete single-file trial can become a baseline")
                self._trial_baseline = self._trial_candidate
            self._trial_revision += 1
            return self._trial_comparison_status()

    def cancel_experiment(self, payload: dict) -> dict:
        if type(payload) is not dict or set(payload) != {"id"}:
            raise ValueError("expected one experiment id")
        with self.lock:
            if payload["id"] is None or payload["id"] != self.experiment["id"]:
                raise ValueError("unknown isolated experiment")
            if (self.experiment_worker is not None and self.experiment_worker.is_alive()
                    and self.experiment["status"] in {"running", "cancelling"}):
                self.experiment_cancel_event.set()
                self.experiment["status"] = "cancelling"
            return {"status": self.experiment["status"]}

    def definitions(self, query: str, version: str, *, mode: str = "exact") -> dict:
        """Find lexical name candidates in cached outlines, not resolved calls."""
        if mode not in ("exact", "keywords", "calls"):
            raise ValueError("definition search mode must be exact, keywords or calls")
        _bounded_text(query, "definition name", 128)
        if mode == "calls":
            return self.call_sites(query, version)
        if mode == "exact" and not all(component.isidentifier() for component in query.split(".")):
            raise ValueError("definition name must contain dot-separated Python identifiers")
        with self.lock:
            if version != self.project["version"]:
                raise ValueError("stale source version; refresh the view")
            if mode == "keywords":
                if self.closed:
                    raise ValueError("reading desk is closing")
                result = {"version": version, "mode": "keywords", **keyword_definitions(
                    query, self.project["files"], self.contents, self.outlines)}
                if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 65_536:
                    raise ValueError("keyword result exceeds 64 KiB; narrow the query")
                return result
            matches, truncated, unavailable = [], False, 0
            for file in self.project["files"]:
                outline = self.outlines[file["id"]]
                if outline["status"] in {"unavailable", "limited"}:
                    if Path(file["path"]).suffix.lower() in {".py", ".pyi"}:
                        unavailable += 1
                    continue
                for item in outline["items"]:
                    if item["name"] == query or item["name"].endswith("." + query):
                        if len(matches) == 40:
                            truncated = True
                        else:
                            matches.append({"file": file["id"], "path": file["path"], **item})
            return {"version": version, "matches": matches, "truncated": truncated,
                "unavailable_files": unavailable}

    def call_sites(self, query: str, version: str) -> dict:
        """Explicit retained-source navigation; no inference context or execution."""
        if not self.call_scan_lock.acquire(blocking=False):
            raise ValueError("another call search is finishing; try again when it completes")
        try:
            with self.lock:
                project, snapshot = self.project, self.browse_snapshot
                if self.closed or version != project["version"]:
                    raise ValueError("closed desk or stale source version; refresh the view")
                files = [dict(item) for item in project["files"]]
            pins = {pin.path: pin for pin in snapshot.fingerprints}
            touched = {}
            deadline = time.monotonic() + 3.0

            def current():
                with self.lock:
                    if self.closed or self.project is not project:
                        raise ValueError("desk closed or snapshot refreshed during search; search again")

            def checkpoint():
                current()
                if time.monotonic() >= deadline:
                    raise ScanStopped("time_limit")

            def read(item):
                current()
                pin = pins[item["path"]]
                _, data = _retained_bytes(snapshot, pin)
                touched[pin.path] = pin
                return data, pin.sha256

            result = {"version": version, "mode": "calls",
                **call_occurrences(query, files, read, checkpoint)}
            # Observe every inspected member again before publication. This detects
            # ordinary retained-byte drift; neither scan nor recheck is an OS lock.
            # Verification must finish even when the cooperative scan budget ended.
            for pin in touched.values():
                current()
                _retained_bytes(snapshot, pin)
            if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 65_536:
                raise ValueError("call search result exceeds 64 KiB; use a narrower name")
            current()
            return result
        finally:
            self.call_scan_lock.release()

    def tracebacks(self, payload: dict) -> dict:
        """Map unverified classic traceback frames to the current cached source."""
        if type(payload) is not dict or set(payload) != {"text", "version"}:
            raise ValueError("expected traceback text and source version")
        text = _bounded_text(payload["text"], "traceback", 2000, multiline=True)
        with self.lock:
            if self.closed:
                raise ValueError("reading desk is closing")
            if self.comparison is not None:
                raise ValueError("traceback navigation requires ordinary source mode, not version aliases")
            if payload["version"] != self.project["version"]:
                raise ValueError("stale source version; refresh the view")
            files = {item["path"]: item for item in self.project["files"]}
            frames, header = [], False
            for line in text.splitlines():
                if line == "Traceback (most recent call last):":
                    header = True
                    continue
                if line.startswith("  [Previous line repeated "):
                    raise ValueError("compressed traceback repetitions are unsupported; no frames were discarded")
                if re.match(r"^[ \t|+]*[|+][ \t|+]*File\b", line):
                    raise ValueError("unsupported traceback frame format")
                if line.startswith("    "):
                    continue  # Classic source excerpts have four spaces, not two.
                match = _TRACEBACK_FRAME.fullmatch(line)
                if match is None:
                    if re.match(r"^[ \t]{0,3}File\b", line):
                        raise ValueError("unsupported traceback frame format")
                    continue
                if not header:
                    raise ValueError("expected classic Python Traceback header")
                if len(frames) == 32:
                    raise ValueError("traceback exceeds 32 frames; no frames were discarded")
                reported, number, function = match.groups()
                number = int(number)
                if number > 2**53 - 1:
                    raise ValueError("traceback line number exceeds the safe integer limit")
                path, reason = _traceback_path(reported, self.source)
                item = files.get(path)
                if reason is None:
                    reason = ("unknown_source" if item is None else
                        "line_separators" if path in self.nonphysical else
                        "line_out_of_range" if not 1 <= number <= item["lines"] else None)
                frames.append({"reported_path": reported, "line": number, "function": function or "",
                    "file": item["id"] if reason is None else None,
                    "path": path if reason is None else None, "reason": reason})
            if not frames:
                raise ValueError("no classic Python traceback frames found")
            return {"version": self.project["version"], "scope": "unverified_user_traceback",
                "matched": sum(item["reason"] is None for item in frames), "frames": frames}

    def _validated_reading_scope(self, scope: dict) -> dict:
        """Keep original windows, not merged coverage or inferred dependencies."""
        if (type(scope) is not dict or set(scope) != {"focus", "origin", "supplements"}
                or type(scope["focus"]) is not list or not scope["focus"]
                or type(scope["supplements"]) is not list):
            raise ValueError("this question has no reusable exact source scope")
        focus, supplements = tuple(scope["focus"]), tuple(scope["supplements"])
        actions = _focus_actions(focus, focus_origin=scope["origin"])
        _context_supplement_actions(focus, scope["origin"], supplements)
        files = {item["path"]: item for item in self.project["files"]}
        if any(action.path not in files or action.end_line > files[action.path]["lines"] for action in actions):
            raise ValueError("continued source ranges are absent from this snapshot")
        return {"focus": list(focus), "origin": scope["origin"], "supplements": list(supplements)}

    def start(self, payload: dict, *, kind: str = "explain") -> dict:
        if self.browse_only:
            raise ValueError("AI requests are disabled in browse-only mode; configure assets and reopen without --browse-only")
        if kind not in ("explain", "locate", "project", "continue"):
            raise ValueError("unknown reading operation")
        locating = kind == "locate"
        automatic = kind in ("locate", "project")
        continuing = kind == "continue"
        expected = {"question", "version", "parent"} if continuing else {"question", "version"} if automatic else {"question", "focus", "version"}
        if type(payload) is not dict or set(payload) != expected:
            raise ValueError("expected parent, question and source version" if continuing else
                "expected question and source version" if automatic else "expected question, focus and source version")
        question = _bounded_text(payload["question"], "question", 2000, multiline=True)
        with self.lock:
            if self.closed:
                raise ValueError("reading desk is closing")
            if self._busy():
                raise ValueError("only one question can run at a time")
            if payload["version"] != self.project["version"]:
                raise ValueError("stale source version; refresh before asking")
            comparison = self.comparison  # This job retains this exact comparison.
            continuation = None
            source_scope = {"focus": [], "origin": "user_focus", "supplements": []}
            if continuing:
                if comparison is not None:
                    raise ValueError("source continuation is unavailable in HEAD comparison mode")
                parent_id = _bounded_text(payload["parent"], "parent question id", 200)
                parent = next((entry for entry in self.history()["entries"] if entry.get("id") == parent_id), None)
                if (parent is None or parent.get("kind") in {"locate", "changes"} or "comparison" in parent
                        or parent.get("status") not in {"answered", "incomplete"}
                        or not self._job_complete(parent) or "error" in parent):
                    raise ValueError("parent question is unavailable, evicted or not eligible for continuation")
                source_scope = self._validated_reading_scope(parent.get("reading_scope"))
                continuation = {"parent_id": parent_id,
                    "parent_question": _bounded_text(parent.get("question"), "parent question", 2000, multiline=True)}
            if comparison is not None and automatic:
                raise ValueError("comparison reading requires explicit before, after and fixed-caller ranges")
            focus = [] if automatic or continuing else payload["focus"]
            if not automatic and not continuing and (not isinstance(focus, list) or not 1 <= len(focus) <= 3):
                raise ValueError("select 1-3 source ranges")
            paths = {item["id"]: item for item in self.project["files"]}
            selectors = list(source_scope["focus"])
            for span in focus:
                if not isinstance(span, dict) or set(span) != {"file", "start", "end"}:
                    raise ValueError("invalid source selection")
                if not isinstance(span["file"], str) or span["file"] not in paths:
                    raise ValueError("unknown source file")
                item = paths[span["file"]]
                first, last = span["start"], span["end"]
                if type(first) is not int or type(last) is not int or not 1 <= first <= last <= item["lines"] or last - first >= 80:
                    raise ValueError("select an inclusive range of at most 80 existing lines")
                selectors.append(f"{item['path']}:{first}-{last}")
            if len(set(selectors)) != len(selectors):
                raise ValueError("duplicate source selections")
            if not automatic and not continuing:
                source_scope["focus"] = list(selectors)
            model_question = question
            if comparison is not None:
                _bounded_text(question, "comparison question", 1300, multiline=True)
                if len(focus) != 3:
                    raise ValueError("select exactly three ranges: before, after, then one after caller held fixed")
                selected_paths = [paths[span["file"]]["path"] for span in focus]
                before, after, caller = selected_paths
                if (not before.startswith("before/") or after != "after/" + before[len("before/"):]
                        or not caller.startswith("after/")):
                    raise ValueError("select before and after of the same file, then an after caller")
                if any(path in self.nonphysical for path in selected_paths):
                    raise ValueError("comparison ranges require physical source line separators")
                changed = next((item for item in comparison.catalogue["files"]
                    if item["path"] == before[len("before/"):]), None)
                if changed is None or changed["status"] != "modified" or changed["line_endings_only"]:
                    raise ValueError("select a file with a retained before/after source change")
                model_question = _comparison_question(question, selectors)
            self.cancel_event = threading.Event()
            self.started, self.finished = time.monotonic(), None
            self.publishing = False
            identifier = _new_task_id("question")
            self.job = {"id": identifier, "status": "running", "phase": "checking source catalogue" if automatic else "checking selected source",
                "question": question, "version": self.project["version"], "files": self.project["files"], "reader": self.reader}
            if automatic or continuing:
                self.job["kind"] = kind
            if continuation is not None:
                self.job["continuation"] = continuation
            if comparison is not None:
                self.job["kind"] = "changes"
                self.job["comparison"] = {key: self.project["comparison"][key]
                    for key in ("head", "original_snapshot_sha256", "scope")}
                self.job["comparison"].update(caller_held_fixed=True, ranges=list(selectors))
            args = argparse.Namespace(repo=self.reading_source, question=model_question, focus=selectors, reader=self.reader, as_json=True)
            pending_result = None

            def progress(message):
                with self.lock:
                    self.job["phase"] = message

            def result(value):
                nonlocal pending_result
                with self.lock:
                    pending_result = value  # Publish all results only after final job gates.

            def preview(text):
                # A transient, bounded draft only. Final parsing and source /
                # lifecycle checks still consume the complete ChatResponse.
                with self.lock:
                    if self.job["status"] == "running" and not self.cancel_event.is_set():
                        current = self.job.get("preview", "")
                        self.job["preview"] = current + text[:max(0, 12_000 - len(current))]

            def run():
                try:
                    runner = _run_project_cli if kind == "project" else _run_locate_cli if locating else _run_explain_cli
                    runner(args, on_progress=progress, on_result=result,
                        **({} if locating else {"on_text": preview}),
                        **({"focus_origin": source_scope["origin"],
                            "context_supplements": tuple(source_scope["supplements"])} if continuing else {}),
                        **({"source_guard": comparison.guard} if comparison is not None else {}),
                        **({"resident_model": self.model} if self.model is not None else {}),
                        cancel_event=self.cancel_event, expected_snapshot=payload["version"])
                except BaseException as exc:
                    # No exception text from inference credentials enters HTTP.
                    with self.lock:
                        self.job["error"] = f"{type(exc).__name__}: reading job failed; inspect private run evidence"
                finally:
                    with self.lock:
                        # Publication wins only after this lock boundary. A
                        # cancellation already accepted must reclaim an idle
                        # resident generation before a terminal job is exposed.
                        self.publishing = True
                        closing_id = ((pending_result or {}).get("server") or {}).get("server_session_id")
                        cancel_at_publication = self.cancel_event.is_set()
                    cancelled_gpu = None
                    if cancel_at_publication and self.model is not None and closing_id:
                        try:
                            state = self.model.status()
                            if state["id"] == closing_id and state["state"] == "idle":
                                self.model.release(closing_id)
                            receipt = next((item for item in self.model.status()["receipts"] if item["id"] == closing_id), {})
                            cancelled_gpu = "released" if all(receipt.get(key) is True for key in (
                                "process_reclaimed", "supervisor_secret_cleared", "transport_secrets_cleared")) else "unknown"
                        except BaseException:
                            cancelled_gpu = "unknown"
                    with self.lock:
                        raw_preview = self.job.pop("preview", None)
                        value = pending_result or {}
                        self.job["gpu"] = _explain_gpu_state(value.get("server")) if value else "unknown"
                        if cancelled_gpu is not None:
                            self.job["gpu"] = cancelled_gpu
                        outcome = value.get("outcome")
                        if cancel_at_publication and value:
                            outcome = ({**outcome, "ok": False, "answer": None, "candidates": []}
                                if isinstance(outcome, dict) else outcome)
                            value = {**value, "ok": False, "status": "interrupted", "outcome": outcome}
                        request_complete = _reading_request_complete(value)
                        if request_complete and not cancel_at_publication and value.get("kind") in {"forge8.explain.request", "forge8.locate.request"}:
                            self.job["request_completion"] = value["request_completion"]
                        if not request_complete and value.get("kind") in {"forge8.explain.request", "forge8.locate.request"}:
                            outcome = ({**outcome, "ok": False, "answer": None} if isinstance(outcome, dict) else outcome)
                            value = {**value, "ok": False, "outcome": outcome}
                        cancelled = (value.get("status") == "interrupted" or self.cancel_event.is_set()) and self.job["gpu"] != "unknown"
                        valid_comparison = True
                        if comparison is not None:
                            acceptance = outcome.get("acceptance") if isinstance(outcome, dict) else None
                            evidence = acceptance.get("evidence") if isinstance(acceptance, dict) else None
                            original = evidence.get("original_source") if isinstance(evidence, dict) else None
                            pins = original.get("evidence") if isinstance(original, dict) else None
                            valid_comparison = (isinstance(acceptance, dict) and acceptance.get("ok") is True
                                and isinstance(original, dict) and original.get("ok") is True
                                and isinstance(pins, dict) and pins.get("head") == comparison.head
                                and pins.get("original_snapshot_sha256") == comparison.original_snapshot_sha256
                                and outcome.get("source_unchanged") is True and outcome.get("snapshot_unchanged") is True
                                and request_complete and not self.cancel_event.is_set() and "error" not in self.job)
                            if not valid_comparison and value:
                                outcome = ({**outcome, "ok": False, "answer": None} if isinstance(outcome, dict) else outcome)
                                value = {**value, "ok": False, "outcome": outcome}
                        if kind == "project":
                            project = value.get("project_reading")
                            discovery = project.get("discovery") if isinstance(project, dict) else None
                            focus = project.get("focus") if isinstance(project, dict) else None
                            valid_focus = (isinstance(focus, list) and 1 <= len(focus) <= 6
                                and all(isinstance(span, dict) and isinstance(span.get("path"), str)
                                    and type(span.get("start_line")) is int and type(span.get("end_line")) is int
                                    and 1 <= span["start_line"] <= span["end_line"] < span["start_line"] + 80
                                    and any(file["path"] == span["path"] and span["end_line"] <= file["lines"] for file in self.project["files"])
                                    for span in focus)
                                and len({(span["path"], line) for span in focus
                                    for line in range(span["start_line"], span["end_line"] + 1)}) <= 240)
                            valid_project = (value.get("kind") in {"forge8.explain", "forge8.explain.request"} and isinstance(discovery, dict) and discovery.get("ok") is True
                                and discovery.get("status") == "located" and discovery.get("snapshot_sha256") == payload["version"]
                                and all(discovery.get(key) is True for key in ("source_unchanged", "snapshot_unchanged", "ingress_unchanged"))
                                and isinstance(discovery.get("acceptance"), dict) and discovery["acceptance"].get("ok") is True
                                and request_complete and not self.cancel_event.is_set() and "error" not in self.job
                                and (valid_focus or focus == []) and _valid_project_context(project, focus)
                                and ((project.get("answer_attempted") is False and project.get("focus") == []
                                      and value.get("status") == "selection_required" and value.get("ok") is False and outcome is None)
                                    or (project.get("answer_attempted") is True and valid_focus and isinstance(outcome, dict)
                                        and outcome.get("source_unchanged") is True and outcome.get("snapshot_unchanged") is True
                                        and isinstance(outcome.get("acceptance"), dict) and outcome["acceptance"].get("ok") is True
                                        and ("insufficient_evidence" not in (value.get("status"), outcome.get("status"))
                                            or _insufficient_explain_reason(value) is not None))))
                            if not valid_project:
                                value = {key: item for key, item in value.items() if key != "project_reading"}
                                if value.get("ok"):
                                    outcome = {**outcome, "ok": False, "answer": None} if isinstance(outcome, dict) else outcome
                                    value = {**value, "ok": False, "outcome": outcome}
                        unverified = None if locating or not valid_comparison or self.cancel_event.is_set() or "error" in self.job or (kind == "project" and not valid_project) else _unverified_explain_prose(value)
                        if isinstance(outcome, dict) and "unverified_prose" in outcome and unverified is None:
                            outcome = {key: item for key, item in outcome.items() if key != "unverified_prose"}
                            value = {**value, "outcome": outcome}
                        if locating:
                            located = (value.get("ok") is True and value.get("status") == "located"
                                and isinstance(outcome, dict) and outcome.get("ok") is True and outcome.get("status") == "located"
                                and all(outcome.get(key) is True for key in ("source_unchanged", "snapshot_unchanged", "ingress_unchanged"))
                                and isinstance(outcome.get("acceptance"), dict) and outcome["acceptance"].get("ok") is True
                                and request_complete and not self.cancel_event.is_set() and "error" not in self.job)
                            self.job["status"] = "located" if located else "cancelled" if cancelled else "incomplete"
                            if value:
                                self.job["result"] = value if located else {**value, "ok": False,
                                    "outcome": {**outcome, "ok": False, "candidates": []} if isinstance(outcome, dict) else outcome}
                        else:
                            self.job["status"] = "answered" if value.get("ok") else "cancelled" if cancelled else "incomplete"
                            if value:
                                self.job["result"] = value
                        if (comparison is None and not locating and (kind != "project" or valid_project)
                                and isinstance(outcome, dict) and outcome.get("source_unchanged") is True
                                and outcome.get("snapshot_unchanged") is True
                                and isinstance(outcome.get("acceptance"), dict) and outcome["acceptance"].get("ok") is True
                                and request_complete and not self.cancel_event.is_set()
                                and "error" not in self.job and not value.get("error")
                                and (unverified is not None or _insufficient_explain_reason(value) is not None
                                    or (value.get("ok") is True and value.get("status") == "answered"
                                    and outcome.get("ok") is True and outcome.get("status") == "answered"
                                    and isinstance(outcome.get("answer"), dict)))):
                            scope = source_scope
                            if kind == "project":
                                scope = {"origin": "project_candidates",
                                    "focus": [f"{span['path']}:{span['start_line']}-{span['end_line']}" for span in project["focus"]],
                                    "supplements": [f"{span['path']}:{span['start_line']}-{span['end_line']}"
                                        for span in project.get("context", {}).get("added", [])]}
                            try:
                                self.job["reading_scope"] = self._validated_reading_scope(scope)
                            except ValueError:
                                pass  # Invalid metadata never becomes an executable continuation.
                        if (not locating and valid_comparison and (kind != "project" or valid_project) and unverified is None and value.get("ok") is False and value.get("status") == "stalled"
                                and isinstance(outcome, dict) and outcome.get("ok") is False
                                and outcome.get("status") == "stalled" and outcome.get("answer", {}) is None
                                and isinstance(outcome.get("acceptance"), dict)
                                and outcome["acceptance"].get("ok") is True
                                and outcome.get("source_unchanged") is True
                                and outcome.get("snapshot_unchanged") is True
                                and request_complete and not self.cancel_event.is_set()
                                and "error" not in self.job):
                            # An unaccepted RAM-only excerpt, never a final answer
                            # or a reason to guess citations or reread private traces.
                            try:
                                self.job["rejected_preview"] = _bounded_text(
                                    raw_preview, "unaccepted preview", 12_000, multiline=True)
                            except ValueError:
                                pass  # Discard unsafe/blank text; do not sanitize it.
                        self.finished = time.monotonic()
                        self._remember_finished_job()

            self.worker = threading.Thread(target=run, name="forge8-reading-job")
            self.worker.start()
            return {"id": identifier}

    def status(self) -> dict:
        with self.lock:
            return {**self.job, "elapsed_seconds": round((self.finished or time.monotonic()) - self.started, 1) if self.job["id"] else 0}

    def _remember_finished_job(self) -> None:
        """Called under the lifecycle lock, after final gates and the finish time."""
        if (self.finished is None or self.job.get("status") not in ("answered", "located", "incomplete", "cancelled")
                or self.job.get("version") != self.project["version"]):
            return
        # Only terminal public metadata and final gated results are retained.
        # Neither a streamed draft nor the transient rejected excerpt is history.
        entry = {key: self.job[key] for key in
            ("id", "question", "version", "kind", "reader", "status", "phase", "gpu", "error", "comparison", "continuation", "request_completion") if key in self.job}
        entry["elapsed_seconds"] = round(self.finished - self.started, 1)
        if (self._job_complete(self.job) and self.job["status"] != "cancelled"
                and not self.cancel_event.is_set() and "error" not in self.job):
            for key in ("files", "result", "reading_scope"):
                if key in self.job:
                    entry[key] = self.job[key]
        try:
            encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            self._history_evicted += 1
            return  # History must not invalidate an otherwise finished job.
        if len(encoded) + 2 > _HISTORY_BYTES:
            self._history_evicted += 1
            return  # Never shorten answer/source data to make an entry fit.
        self._history.append(encoded)
        # Include JSON array brackets and commas in the byte ceiling. Keeping
        # serialized owned bytes also prevents later live-result mutation.
        while (len(self._history) > _HISTORY_ITEMS
                or sum(map(len, self._history)) + len(self._history) + 1 > _HISTORY_BYTES):
            self._history.pop(0)
            self._history_evicted += 1

    def history(self) -> dict:
        with self.lock:
            version = self.project["version"]
            entries = [json.loads(item) for item in self._history]
            return {"version": version, "entries": [item for item in entries if item["version"] == version],
                "evicted": self._history_evicted}

    def cancel(self, identifier: str) -> dict:
        with self.lock:
            if identifier != self.job["id"]:
                raise ValueError("unknown reading job")
            if not self.publishing and self._busy() and self.job["status"] in {"running", "cancelling"}:
                self.cancel_event.set()
                self.job["status"] = "cancelling"
                self.job.pop("preview", None)
            return {"status": self.job["status"]}

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.cancel_event.set()
            self.experiment_cancel_event.set()
            workers = (self.worker, self.experiment_worker)
        for worker in workers:
            if worker is not None:
                worker.join()
        if self.model is not None:
            self.model.close()


def make_server(desk: ReadingDesk) -> tuple[ThreadingHTTPServer, str]:
    token = secrets.token_urlsafe(32)
    static = Path(__file__).with_name("web")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_args):
            pass  # Do not log private paths, queries or browser credentials.

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(data)

        def dispatch(self, mutation=False):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if self.headers.get_all("Host") != [origin[len("http://"):]]:
                return self.send(403, {"error": "invalid local host"})
            supplied_origin = self.headers.get_all("Origin")
            if (supplied_origin is not None and supplied_origin != [origin]) or (mutation and supplied_origin is None):
                return self.send(403, {"error": "invalid local origin"})
            parsed = urlsplit(self.path)
            assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                      "/reading-note.js": ("reading-note.js", "text/javascript"), "/app.css": ("app.css", "text/css")}
            if not mutation and parsed.path in assets:
                name, content_type = assets[parsed.path]
                return self.send(200, (static / name).read_bytes(), content_type + "; charset=utf-8")
            authorization = self.headers.get_all("Authorization")
            if authorization is None or len(authorization) != 1 or not secrets.compare_digest(authorization[0].encode("utf-8"), ("Bearer " + token).encode("utf-8")):
                return self.send(401, {"error": "open the private URL printed in your Forge8 terminal"})
            try:
                payload = None
                if mutation:
                    if self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding"):
                        raise ValueError("expected a bounded JSON request")
                    sizes = self.headers.get_all("Content-Length")
                    limit = 36 * 1024 if parsed.path in {"/api/experiment/run", "/api/experiment/prepare"} else 16_384
                    if sizes is None or len(sizes) != 1 or not sizes[0].isdigit() or not 1 <= int(sizes[0]) <= limit:
                        raise ValueError("invalid request size")
                    self.connection.settimeout(5)
                    raw = self.rfile.read(int(sizes[0])).decode("utf-8")
                    payload = json.loads(raw, **({"object_pairs_hook": _strict_object,
                        "parse_constant": _reject_constant} if parsed.path.startswith(("/api/experiment/", "/api/model/")) else {}))
                    if not isinstance(payload, dict):
                        raise ValueError("request must be an object")
                query = parse_qs(parsed.query)
                if not mutation and parsed.path == "/api/model":
                    return self.send(200, desk.model_status())
                if mutation and parsed.path == "/api/model/release":
                    return self.send(200, desk.release_model(payload))
                if not mutation and parsed.path == "/api/project":
                    with desk.lock:
                        project = desk.project
                    return self.send(200, project)
                if not mutation and parsed.path == "/api/source":
                    return self.send(200, desk.source_view(query.get("file", [""])[0], query.get("version", [""])[0]))
                if not mutation and parsed.path == "/api/search":
                    return self.send(200, desk.search(query.get("q", [""])[0]))
                if not mutation and parsed.path == "/api/definitions":
                    modes = query.get("mode", ["exact"])
                    if len(modes) != 1:
                        raise ValueError("expected one definition search mode")
                    if modes[0] == "calls" and any(len(query.get(key, [])) != 1 for key in ("q", "version")):
                        raise ValueError("expected one call name and source version")
                    return self.send(200, desk.definitions(query.get("q", [""])[0], query.get("version", [""])[0], mode=modes[0]))
                if not mutation and parsed.path == "/api/jobs/current":
                    return self.send(200, desk.status())
                if not mutation and parsed.path == "/api/history":
                    return self.send(200, desk.history())
                if not mutation and parsed.path == "/api/experiment/current":
                    return self.send(200, desk.experiment_status())
                if mutation and parsed.path == "/api/experiment/input":
                    return self.send(200, desk.prepare_experiment_input(payload))
                if mutation and parsed.path == "/api/experiment/prepare":
                    return self.send(200, desk.prepare_experiment(payload))
                if mutation and parsed.path == "/api/experiment/run":
                    return self.send(202, desk.start_experiment(payload))
                if mutation and parsed.path == "/api/experiment/cancel":
                    return self.send(202, desk.cancel_experiment(payload))
                if mutation and parsed.path == "/api/experiment/baseline":
                    return self.send(200, desk.set_trial_baseline(payload))
                if mutation and parsed.path == "/api/experiment/baseline/clear":
                    return self.send(200, desk.set_trial_baseline(payload, clear=True))
                if mutation and parsed.path == "/api/jobs":
                    return self.send(202, desk.start(payload))
                if mutation and parsed.path == "/api/locate":
                    return self.send(202, desk.start(payload, kind="locate"))
                if mutation and parsed.path == "/api/project-question":
                    return self.send(202, desk.start(payload, kind="project"))
                if mutation and parsed.path == "/api/continue-question":
                    return self.send(202, desk.start(payload, kind="continue"))
                if mutation and parsed.path == "/api/traceback":
                    return self.send(200, desk.tracebacks(payload))
                if mutation and parsed.path == "/api/context":
                    return self.send(200, desk.context(payload))
                if mutation and parsed.path == "/api/import-source":
                    return self.send(200, desk.import_source(payload))
                if mutation and parsed.path == "/api/refresh":
                    if set(payload) - {"mode"} or ("mode" in payload and payload["mode"] not in ("source", "changes")):
                        raise ValueError("expected an optional source or changes mode")
                    return self.send(200, desk.refresh(**payload))
                if mutation and parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/cancel"):
                    return self.send(202, desk.cancel(parsed.path[len("/api/jobs/"):-len("/cancel")]))
                return self.send(404, {"error": "unknown reading desk route"})
            except RecursionError:
                return self.send(400, {"error": "request JSON nesting exceeds the supported limit"})
            except (ValueError, OSError, TypeError) as exc:
                return self.send(400, {"error": str(exc)})

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch(mutation=True)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    return server, f"http://127.0.0.1:{server.server_port}/#token={token}"


def run_desk(source: Path, *, observation: Path | None = None, reader: str = "qwen35",
             allow_experiments: bool = False, idle_timeout: int = 600,
             browse_only: bool = False, state_directory: Path | None = None) -> int:
    if type(browse_only) is not bool or type(allow_experiments) is not bool:
        raise ValueError("browse_only and allow_experiments must be explicit booleans")
    options = {"allow_experiments": True} if allow_experiments else {}
    if browse_only:
        if type(idle_timeout) is not int or idle_timeout not in (0, 600):
            raise ValueError("browse-only has no resident model timeout")
        options.update(browse_only=True, state_directory=state_directory)
        idle_timeout = 0
    elif state_directory is not None:
        raise ValueError("an explicit state directory is only available in browse-only mode")
    desk = ReadingDesk(source, observation=observation, reader=reader, idle_timeout=idle_timeout, **options)
    server, url = make_server(desk)
    if browse_only:
        print("Forge8 純原碼瀏覽：不使用模型部署設定，不開放 AI、函式試跑或 Git 比較。")
        print("搜尋、名稱來源、匯入導航與 traceback 定位可用；不執行專案。Ctrl+C 關閉。")
        print("私人快照目錄：" + _inert_text(str(desk.root)))
    else:
        label = "Gemma 4 12B 實驗選用" if reader == "gemma12b" else "Qwen3.5 預覽"
        print(f"Forge8 本機程式閱讀桌（{label}；模型解釋仍可能錯誤）")
        print((f"瀏覽不啟動 GPU；首題載入後常駐，閒置 {idle_timeout} 秒自動卸載，也可手動釋放。" if idle_timeout else
            "瀏覽不使用 GPU；每題完成後釋放模型。") + "Ctrl+C 關閉並取消目前工作。")
    if allow_experiments:
        print("已啟用隔離函式實驗：仍需逐次明確確認完整模組執行；不使用 GPU，也不會自動執行。")
    print("請在本機瀏覽器開啟此私人網址，不要分享：\n" + url, flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("正在關閉閱讀桌，取消目前工作並等待程序清理…", flush=True)
    finally:
        server.server_close()
        desk.close()
    if desk.experiment.get("cleanup_unknown"):
        print("警告：隔離實驗的程序清理未確認；請檢查私人執行紀錄，勿視為已安全停止。", file=sys.stderr)
        return 2
    if desk.model is not None and (desk.model.status()["state"] == "cleanup_unknown" or desk.model.status()["validation_failed"]):
        print("警告：模型工作階段的程序清理或最終驗證未確認；請檢查私人執行紀錄。", file=sys.stderr)
        return 2
    return 0
