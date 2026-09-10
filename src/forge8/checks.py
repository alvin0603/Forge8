"""Fixed, typed deterministic checks with bounded process evidence.

The model-facing API accepts a check ID only.  Command lines are immutable
registry data and are never assembled from model-provided argv or shell text.

This runner deliberately reports ``isolation=process_only`` and
``network_isolation_enforced=False``.  It sanitizes the child environment and
confines the working directory, but it is not an OS filesystem or network
sandbox.  Verifier directories are not registry discovery roots; callers must
also keep hidden verifier code outside the mounted workspace because project
code executed with process-only isolation can otherwise read mounted files.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, IO, Literal, Mapping

from .workspace import ArtifactRef, ArtifactStore, PolicyViolation, WorkspacePolicy


CheckStatus = Literal["passed", "failed", "timed_out", "launch_error", "unavailable"]
Isolation = Literal["process_only"]


class CheckCleanupError(RuntimeError):
    """An owned cancellable process may still be alive; do not release its slot."""

    def __init__(self, process_id: int) -> None:
        self.process_id = process_id
        super().__init__(f"Cleanup could not be confirmed for check process {process_id}")


_CHECK_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_WINDOWS_ABSOLUTE = re.compile(r"^[a-zA-Z]:[\\/]")
_FORBIDDEN_CONTROL_PARTS = frozenset({"verifier"})
_PREVIEW_MARKER = "\n…[preview truncated; use artifact handle]"
_UNITTEST_CHECK_ID = "python_unittest"
_UNITTEST_RESULT_BASE = 70
_UNITTEST_EVIDENCE_ERROR = 79
_PYTEST_CHECK_ID = "python_pytest"
_PYTEST_VERSION = "9.1.1"
_PYTEST_RESULT_BASE = 80
_PYTEST_BOOTSTRAP_ERROR = 90
_PYTEST_EVIDENCE_ERROR = 91
_PYTEST_ARGUMENTS = (
    "--disable-plugin-autoload",
    "-p",
    "no:cacheprovider",
    "-c",
    os.devnull,
    "--rootdir=.",
    "--confcutdir=.",
    "--color=no",
    "--tb=short",
    "-q",
    "tests",
)
_UNITTEST_BOOTSTRAP = f"""\
import os
import sys
import unittest as _unittest
_loader = _unittest.TestLoader()
_runner = _unittest.TextTestRunner(
    stream=sys.stderr,
    verbosity=2,
    warnings="default",
)
sys.path.insert(0, os.getcwd())
try:
    _suite = _loader.discover("tests")
    _expected_tests = _suite.countTestCases()
    _result = _runner.run(_suite)
except BaseException as _error:
    print(
        "Forge8 unittest evidence gate aborted: "
        + type(_error).__name__ + ": " + str(_error),
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit({_UNITTEST_EVIDENCE_ERROR})
_strict_result = (
    _expected_tests > 0
    and not _result.shouldStop
    and _result.testsRun <= _expected_tests
    and not _result.skipped
    and not _result.expectedFailures
    and not _result.unexpectedSuccesses
)
if _strict_result and (_result.failures or _result.errors):
    raise SystemExit({_UNITTEST_RESULT_BASE + 1})
if (
    _strict_result
    and _result.testsRun == _expected_tests
    and _result.wasSuccessful()
):
    raise SystemExit({_UNITTEST_RESULT_BASE})
print(
    "Forge8 unittest evidence gate rejected zero, skipped, expected-failure, "
    "unexpected-success, or incomplete execution",
    file=sys.stderr,
    flush=True,
)
raise SystemExit({_UNITTEST_EVIDENCE_ERROR})
"""
_PYTEST_BOOTSTRAP = f"""\
import importlib
import importlib.metadata
import os
import sys
try:
    _names = ["iniconfig", "packaging", "pluggy", "pygments"]
    if sys.platform == "win32":
        _names.append("colorama")
    if sys.version_info < (3, 11):
        _names.extend(("exceptiongroup", "tomli", "typing_extensions"))
    for _name in _names:
        importlib.import_module(_name)
    _pytest = importlib.import_module("pytest")
    if (
        importlib.metadata.version("pytest") != {_PYTEST_VERSION!r}
        or getattr(_pytest, "__version__", None) != {_PYTEST_VERSION!r}
    ):
        raise RuntimeError("python_pytest requires pytest=={_PYTEST_VERSION}")
except BaseException as _error:
    print(
        "Forge8 pytest bootstrap failed; install exact forge8[pytest]: "
        + type(_error).__name__ + ": " + str(_error),
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit({_PYTEST_BOOTSTRAP_ERROR})
sys.path.insert(0, os.getcwd())

class _Forge8PytestEvidence:
    _PASSED_PHASES = (
        ("setup", "passed", False),
        ("call", "passed", False),
        ("teardown", "passed", False),
    )

    def __init__(self):
        self.collected_items = []
        self.final_items = None
        self.collection_invalid = False
        self.reports = dict()

    def pytest_itemcollected(self, item):
        self.collected_items.append((id(item), item.nodeid))

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            self.collection_invalid = True

    def pytest_runtest_logreport(self, report):
        self.reports.setdefault(report.nodeid, []).append(
            (
                report.when,
                report.outcome,
                getattr(report, "wasxfail", None) is not None,
            )
        )

    @_pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session, exitstatus):
        result = yield
        self.final_items = tuple((id(item), item.nodeid) for item in session.items)
        return result

    def _structurally_complete(self):
        if (
            self.collection_invalid
            or not self.collected_items
            or self.final_items is None
        ):
            return False
        collected = tuple(self.collected_items)
        if sorted(collected) != sorted(self.final_items):
            return False
        nodeids = tuple(nodeid for _identity, nodeid in collected)
        if len(set(nodeids)) != len(nodeids) or set(self.reports) != set(nodeids):
            return False
        return not any(
            outcome == "skipped" or wasxfail
            for reports in self.reports.values()
            for _when, outcome, wasxfail in reports
        )

    def passed(self):
        return self._structurally_complete() and all(
            tuple(self.reports[nodeid]) == self._PASSED_PHASES
            for _identity, nodeid in self.collected_items
        )

    def failed(self):
        if not self._structurally_complete():
            return False
        saw_failure = False
        for _identity, nodeid in self.collected_items:
            reports = tuple(self.reports[nodeid])
            if any(outcome == "failed" for _when, outcome, _wasxfail in reports):
                saw_failure = True
            elif reports != self._PASSED_PHASES:
                return False
        return saw_failure

_gate = _Forge8PytestEvidence()
_pytest_result = int(_pytest.main(list({_PYTEST_ARGUMENTS!r}), plugins=[_gate]))
if _pytest_result == 0 and _gate.passed():
    raise SystemExit({_PYTEST_RESULT_BASE})
if _pytest_result == 1 and _gate.failed():
    raise SystemExit({_PYTEST_RESULT_BASE + 1})
if _pytest_result in (2, 3, 4, 5):
    raise SystemExit({_PYTEST_RESULT_BASE} + _pytest_result)
print(
    "Forge8 pytest evidence gate rejected an incomplete, skipped, xfailed, "
    "deselected, or early-exit result",
    file=sys.stderr,
    flush=True,
)
raise SystemExit({_PYTEST_EVIDENCE_ERROR})
"""

_CREATE_SUSPENDED = 0x00000004
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_PROCESS_TERMINATE = 0x00000001
_PROCESS_SET_QUOTA = 0x00000100
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x00000002
_ERROR_NO_MORE_FILES = 18
_INVALID_DWORD = 0xFFFFFFFF
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


# Fixed-width Win32 types keep these structures testable on non-Windows hosts;
# ctypes.wintypes follows the host C ABI and has the wrong DWORD size on Linux.
_DWORD = ctypes.c_uint32
_LONG = ctypes.c_int32
_ULONG_PTR = ctypes.c_size_t
_SIZE_T = ctypes.c_size_t
_HANDLE = ctypes.c_void_p


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", _SIZE_T),
        ("MaximumWorkingSetSize", _SIZE_T),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", _ULONG_PTR),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    )


class _IoCounters(ctypes.Structure):
    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", _SIZE_T),
        ("JobMemoryLimit", _SIZE_T),
        ("PeakProcessMemoryUsed", _SIZE_T),
        ("PeakJobMemoryUsed", _SIZE_T),
    )


class _ThreadEntry32(ctypes.Structure):
    _fields_ = (
        ("dwSize", _DWORD),
        ("cntUsage", _DWORD),
        ("th32ThreadID", _DWORD),
        ("th32OwnerProcessID", _DWORD),
        ("tpBasePri", _LONG),
        ("tpDeltaPri", _LONG),
        ("dwFlags", _DWORD),
    )


class _WindowsJobError(RuntimeError):
    """A bounded Windows containment setup or cleanup failure."""


class _CtypesWindowsApi:
    """Small injectable Win32 surface used by the check Job controller."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise _WindowsJobError("Windows Job Objects are unavailable on this platform")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        kernel32.CreateJobObjectW.restype = _HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            _HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            _DWORD,
        )
        kernel32.SetInformationJobObject.restype = _LONG
        kernel32.OpenProcess.argtypes = (_DWORD, _LONG, _DWORD)
        kernel32.OpenProcess.restype = _HANDLE
        kernel32.AssignProcessToJobObject.argtypes = (_HANDLE, _HANDLE)
        kernel32.AssignProcessToJobObject.restype = _LONG
        kernel32.TerminateJobObject.argtypes = (_HANDLE, _DWORD)
        kernel32.TerminateJobObject.restype = _LONG
        kernel32.CloseHandle.argtypes = (_HANDLE,)
        kernel32.CloseHandle.restype = _LONG
        kernel32.CreateToolhelp32Snapshot.argtypes = (_DWORD, _DWORD)
        kernel32.CreateToolhelp32Snapshot.restype = _HANDLE
        kernel32.Thread32First.argtypes = (_HANDLE, ctypes.POINTER(_ThreadEntry32))
        kernel32.Thread32First.restype = _LONG
        kernel32.Thread32Next.argtypes = (_HANDLE, ctypes.POINTER(_ThreadEntry32))
        kernel32.Thread32Next.restype = _LONG
        kernel32.OpenThread.argtypes = (_DWORD, _LONG, _DWORD)
        kernel32.OpenThread.restype = _HANDLE
        kernel32.ResumeThread.argtypes = (_HANDLE,)
        kernel32.ResumeThread.restype = _DWORD
        self.kernel32 = kernel32

    @staticmethod
    def _error(operation: str, error_code: int | None = None) -> _WindowsJobError:
        code = ctypes.get_last_error() if error_code is None else error_code
        return _WindowsJobError(f"{operation} failed with Windows error {code}")

    def create_job(self) -> int:
        handle = self.kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise self._error("CreateJobObjectW")
        return int(handle)

    def set_kill_on_close(
        self, job_handle: int, *, process_memory_limit_bytes: int | None = None,
    ) -> None:
        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if process_memory_limit_bytes is not None:
            information.BasicLimitInformation.LimitFlags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            information.ProcessMemoryLimit = process_memory_limit_bytes
        if not self.kernel32.SetInformationJobObject(
            _HANDLE(job_handle),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise self._error("SetInformationJobObject")

    def open_process_for_job(self, process_id: int) -> int:
        handle = self.kernel32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_SET_QUOTA,
            False,
            process_id,
        )
        if not handle:
            raise self._error("OpenProcess")
        return int(handle)

    def assign_process(self, job_handle: int, process_handle: int) -> None:
        if not self.kernel32.AssignProcessToJobObject(
            _HANDLE(job_handle),
            _HANDLE(process_handle),
        ):
            raise self._error("AssignProcessToJobObject")

    def find_primary_thread(self, process_id: int) -> int:
        snapshot = self.kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        snapshot_value = None if snapshot is None else int(snapshot)
        if snapshot_value == _INVALID_HANDLE_VALUE:
            raise self._error("CreateToolhelp32Snapshot")

        assert snapshot_value is not None
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            if not self.kernel32.Thread32First(_HANDLE(snapshot_value), ctypes.byref(entry)):
                raise self._error("Thread32First")
            while True:
                if entry.th32OwnerProcessID == process_id:
                    return int(entry.th32ThreadID)
                if self.kernel32.Thread32Next(_HANDLE(snapshot_value), ctypes.byref(entry)):
                    continue
                error_code = ctypes.get_last_error()
                if error_code == _ERROR_NO_MORE_FILES:
                    break
                raise self._error("Thread32Next", error_code)
        finally:
            self.close_handle(snapshot_value)
        raise _WindowsJobError(f"No suspended thread found for process {process_id}")

    def resume_primary_thread(self, thread_id: int) -> None:
        handle = self.kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, thread_id)
        if not handle:
            raise self._error("OpenThread")
        handle_value = int(handle)
        try:
            previous_count = int(self.kernel32.ResumeThread(_HANDLE(handle_value)))
            if previous_count == _INVALID_DWORD:
                raise self._error("ResumeThread")
            if previous_count != 1:
                raise _WindowsJobError(
                    f"ResumeThread returned unexpected suspend count {previous_count}"
                )
        finally:
            self.close_handle(handle_value)

    def terminate_job(self, job_handle: int) -> None:
        if not self.kernel32.TerminateJobObject(_HANDLE(job_handle), 1):
            raise self._error("TerminateJobObject")

    def close_handle(self, handle: int) -> None:
        if not self.kernel32.CloseHandle(_HANDLE(handle)):
            raise self._error("CloseHandle")


class _WindowsJob:
    """Own one kill-on-close Job and resume a child only after assignment."""

    def __init__(self, api: Any, handle: int) -> None:
        self.api = api
        self.handle: int | None = handle

    @classmethod
    def create(
        cls, api: Any, *, process_memory_limit_bytes: int | None = None,
    ) -> _WindowsJob:
        handle = api.create_job()
        try:
            if process_memory_limit_bytes is None:
                api.set_kill_on_close(handle)
            else:
                api.set_kill_on_close(handle, process_memory_limit_bytes=process_memory_limit_bytes)
        except BaseException:
            try:
                api.close_handle(handle)
            except BaseException:
                pass
            raise
        return cls(api, handle)

    def assign_and_resume(self, process_id: int) -> None:
        if self.handle is None:
            raise _WindowsJobError("Windows Job handle is already closed")
        process_handle = self.api.open_process_for_job(process_id)
        try:
            self.api.assign_process(self.handle, process_handle)
        finally:
            self.api.close_handle(process_handle)
        thread_id = self.api.find_primary_thread(process_id)
        self.api.resume_primary_thread(thread_id)

    def close(self) -> str | None:
        """Close without an explicit terminate, used before any child exists."""

        if self.handle is None:
            return None
        handle, self.handle = self.handle, None
        try:
            self.api.close_handle(handle)
        except BaseException as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    def terminate_and_close(self) -> str | None:
        """Terminate all members and close the kill-on-close backstop handle."""

        if self.handle is None:
            return None
        handle, self.handle = self.handle, None
        errors: list[str] = []
        try:
            self.api.terminate_job(handle)
        except BaseException as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        try:
            self.api.close_handle(handle)
        except BaseException as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        return "; ".join(errors) or None


def _new_windows_job(*, process_memory_limit_bytes: int | None = None) -> _WindowsJob:
    api = _CtypesWindowsApi()
    if process_memory_limit_bytes is None:
        return _WindowsJob.create(api)
    return _WindowsJob.create(api, process_memory_limit_bytes=process_memory_limit_bytes)


def _validate_registry_path(value: str, *, allow_root: bool) -> None:
    if not value or "\x00" in value:
        raise ValueError("check paths must be non-empty and contain no NUL byte")
    normalized = value.replace("\\", "/")
    if allow_root and normalized == ".":
        return
    if normalized.startswith(("/", "//")) or _WINDOWS_ABSOLUTE.match(value):
        raise ValueError("check paths must be workspace-relative")
    pure = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("check paths cannot contain traversal or ambiguous parts")
    if any(part.casefold() in _FORBIDDEN_CONTROL_PARTS for part in pure.parts):
        raise ValueError("verifier paths cannot be exposed as deterministic checks")


@dataclass(frozen=True, slots=True)
class CheckDefinition:
    """Trusted registry definition for one deterministic check.

    ``argv`` is included in the machine-readable audit form, but discovery returns
    IDs only and :meth:`CheckRunner.run` never accepts argv from its caller.
    """

    id: str
    label: str
    description: str
    argv: tuple[str, ...]
    cwd: str
    discovery_path: str
    timeout_seconds: float
    isolation: Isolation = "process_only"
    network_isolation_enforced: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        if not _CHECK_ID.fullmatch(self.id):
            raise ValueError(f"invalid check id: {self.id!r}")
        if not self.label or not self.description:
            raise ValueError("check label and description are required")
        if not self.argv or any(not isinstance(item, str) or not item or "\x00" in item for item in self.argv):
            raise ValueError("check argv must contain non-empty strings without NUL bytes")
        if not Path(self.argv[0]).is_absolute():
            raise ValueError("check executable must be an absolute path")
        _validate_registry_path(self.cwd, allow_root=True)
        _validate_registry_path(self.discovery_path, allow_root=False)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("check timeout must be positive")
        if self.isolation != "process_only":
            raise ValueError("the deterministic runner currently supports process_only isolation")
        if self.network_isolation_enforced is not False:
            raise ValueError("the deterministic runner cannot claim network isolation")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "command": {"argv": list(self.argv), "shell": False},
            "cwd": self.cwd,
            "discovery_path": self.discovery_path,
            "timeout_seconds": float(self.timeout_seconds),
            "isolation": self.isolation,
            "network_isolation_enforced": self.network_isolation_enforced,
            "sealed": False,
        }


_PYTHON_EXECUTABLE = str(Path(sys.executable).resolve())

CHECK_REGISTRY: Mapping[str, CheckDefinition] = MappingProxyType(
    {
        _UNITTEST_CHECK_ID: CheckDefinition(
            id=_UNITTEST_CHECK_ID,
            label="Python unittest discovery",
            description="Run the workspace test suite discovered under tests/.",
            argv=(
                _PYTHON_EXECUTABLE,
                "-I",
                "-B",
                "-S",
                "-X",
                "utf8",
                "-c",
                _UNITTEST_BOOTSTRAP,
            ),
            cwd=".",
            discovery_path="tests",
            timeout_seconds=120.0,
        ),
        _PYTEST_CHECK_ID: CheckDefinition(
            id=_PYTEST_CHECK_ID,
            label="Python pytest",
            description=(
                "Run built-in pytest features with automatic plugin loading disabled."
            ),
            argv=(
                _PYTHON_EXECUTABLE,
                "-I",
                "-B",
                "-c",
                _PYTEST_BOOTSTRAP,
            ),
            cwd=".",
            discovery_path="tests",
            timeout_seconds=120.0,
        ),
    }
)


def _resolved_relative_parts(policy: WorkspacePolicy, path: Path) -> tuple[str, ...]:
    try:
        relative = path.relative_to(policy.root)
    except ValueError as exc:
        raise PolicyViolation("Check path escapes the workspace") from exc
    return tuple(part.casefold() for part in relative.parts)


def _definition_available(policy: WorkspacePolicy, definition: CheckDefinition) -> bool:
    """Check one exact discovery root without scanning verifier/oracle paths."""

    try:
        discovery = policy.resolve(definition.discovery_path)
        actual_parts = _resolved_relative_parts(policy, discovery)
    except (OSError, PolicyViolation):
        return False
    if any(part in _FORBIDDEN_CONTROL_PARTS for part in actual_parts):
        return False
    return discovery.is_dir()


def _status_for_return_code(check_id: str, return_code: int) -> CheckStatus:
    if check_id == _UNITTEST_CHECK_ID:
        if return_code == _UNITTEST_RESULT_BASE:
            return "passed"
        if return_code == _UNITTEST_RESULT_BASE + 1:
            return "failed"
        return "unavailable"
    if check_id == _PYTEST_CHECK_ID:
        if return_code == _PYTEST_RESULT_BASE:
            return "passed"
        if return_code == _PYTEST_RESULT_BASE + 1:
            return "failed"
        return "unavailable"
    if return_code == 0:
        return "passed"
    return "failed"


def discover_checks(policy: WorkspacePolicy) -> tuple[str, ...]:
    """Return only fixed registry IDs available in this workspace."""

    return tuple(
        check_id
        for check_id, definition in CHECK_REGISTRY.items()
        if _definition_available(policy, definition)
    )


def _resolve_cwd(policy: WorkspacePolicy, definition: CheckDefinition) -> Path:
    if definition.cwd == ".":
        cwd = policy.root.resolve(strict=True)
    else:
        cwd = policy.resolve(definition.cwd)
    actual_parts = _resolved_relative_parts(policy, cwd)
    if any(part in _FORBIDDEN_CONTROL_PARTS for part in actual_parts):
        raise PolicyViolation("Verifier paths cannot be used as a check working directory")
    if not cwd.is_dir():
        raise PolicyViolation("Check working directory is not a directory")
    return cwd


def _sanitized_environment(check_id: str, workspace_root: Path) -> dict[str, str]:
    """Construct a minimal environment instead of filtering an inherited copy."""

    python_dir = str(Path(_PYTHON_EXECUTABLE).parent)
    path_entries = [python_dir]
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if system_root:
            path_entries.extend((str(Path(system_root) / "System32"), system_root))
    else:
        path_entries.extend(("/usr/local/bin", "/usr/bin", "/bin"))

    deduplicated_path = tuple(dict.fromkeys(path_entries))
    environment = {
        "PATH": os.pathsep.join(deduplicated_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "FORGE8_CHECK_ID": check_id,
        "FORGE8_ISOLATION": "process_only",
        "FORGE8_NETWORK_ISOLATION_ENFORCED": "0",
    }
    if check_id == _UNITTEST_CHECK_ID:
        # The -I bootstrap ignores this while importing stdlib unittest, then
        # adds the workspace explicitly. Tests that spawn Python retain the
        # prior workspace-import behavior without inheriting ambient paths.
        environment["PYTHONPATH"] = str(workspace_root)
    if os.name == "nt":
        for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
    else:
        environment["LANG"] = "C.UTF-8"
        environment["LC_ALL"] = "C.UTF-8"
    return environment


@dataclass(frozen=True, slots=True)
class _OutputSnapshot:
    stdout: bytes
    stderr: bytes
    stdout_observed_bytes: int
    stderr_observed_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    capture_errors: tuple[str, ...]

    @property
    def truncated(self) -> bool:
        return self.stdout_truncated or self.stderr_truncated


class _OutputCollector:
    """Drain both pipes while retaining at most one shared byte budget."""

    def __init__(self, limit_bytes: int) -> None:
        self.limit_bytes = limit_bytes
        self._lock = threading.Lock()
        self._buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self._observed = {"stdout": 0, "stderr": 0}
        self._truncated = {"stdout": False, "stderr": False}
        self._capture_errors: list[str] = []

    def add(self, stream_name: Literal["stdout", "stderr"], data: bytes) -> None:
        with self._lock:
            self._observed[stream_name] += len(data)
            retained = len(self._buffers["stdout"]) + len(self._buffers["stderr"])
            available = max(0, self.limit_bytes - retained)
            selected = data[:available]
            self._buffers[stream_name].extend(selected)
            if len(selected) != len(data):
                self._truncated[stream_name] = True

    def capture_error(self, stream_name: str, error: OSError) -> None:
        with self._lock:
            self._capture_errors.append(f"{stream_name}: {type(error).__name__}: {error}")

    def snapshot(self) -> _OutputSnapshot:
        with self._lock:
            return _OutputSnapshot(
                stdout=bytes(self._buffers["stdout"]),
                stderr=bytes(self._buffers["stderr"]),
                stdout_observed_bytes=self._observed["stdout"],
                stderr_observed_bytes=self._observed["stderr"],
                stdout_truncated=self._truncated["stdout"],
                stderr_truncated=self._truncated["stderr"],
                capture_errors=tuple(self._capture_errors),
            )


def _drain_pipe(
    stream: IO[bytes],
    stream_name: Literal["stdout", "stderr"],
    collector: _OutputCollector,
) -> None:
    try:
        while True:
            read = getattr(stream, "read1", stream.read)
            chunk = read(8192)
            if not chunk:
                return
            collector.add(stream_name, chunk)
    except OSError as exc:
        collector.capture_error(stream_name, exc)


def _truncate_preview(text: str, budget: int) -> tuple[str, bool]:
    if len(text) <= budget:
        return text, False
    if budget <= 0:
        return "", True
    if budget <= len(_PREVIEW_MARKER):
        return _PREVIEW_MARKER[-budget:], True
    return text[: budget - len(_PREVIEW_MARKER)] + _PREVIEW_MARKER, True


def _bounded_previews(stdout: bytes, stderr: bytes, limit_chars: int) -> tuple[str, str, bool, bool]:
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    if not stdout_text:
        stderr_preview, stderr_truncated = _truncate_preview(stderr_text, limit_chars)
        return "", stderr_preview, False, stderr_truncated
    if not stderr_text:
        stdout_preview, stdout_truncated = _truncate_preview(stdout_text, limit_chars)
        return stdout_preview, "", stdout_truncated, False

    stderr_budget = limit_chars // 2
    stdout_budget = limit_chars - stderr_budget
    if len(stdout_text) < stdout_budget:
        stderr_budget += stdout_budget - len(stdout_text)
        stdout_budget = len(stdout_text)
    elif len(stderr_text) < stderr_budget:
        stdout_budget += stderr_budget - len(stderr_text)
        stderr_budget = len(stderr_text)
    stdout_preview, stdout_truncated = _truncate_preview(stdout_text, stdout_budget)
    stderr_preview, stderr_truncated = _truncate_preview(stderr_text, stderr_budget)
    return stdout_preview, stderr_preview, stdout_truncated, stderr_truncated


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Machine-readable process result and immutable output handles."""

    check_id: str
    status: CheckStatus
    ok: bool
    return_code: int | None
    timed_out: bool
    duration_seconds: float
    command: tuple[str, ...]
    cwd: str | None
    environment_keys: tuple[str, ...]
    stdout_artifact: ArtifactRef
    stderr_artifact: ArtifactRef
    stdout_preview: str
    stderr_preview: str
    stdout_observed_bytes: int
    stderr_observed_bytes: int
    stdout_captured_bytes: int
    stderr_captured_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_preview_truncated: bool
    stderr_preview_truncated: bool
    output_limit_bytes: int
    partial_output: bool
    capture_errors: tuple[str, ...]
    error: str | None
    isolation: Isolation = "process_only"
    network_isolation_enforced: bool = False
    sealed: bool = False

    def __post_init__(self) -> None:
        if self.isolation != "process_only":
            raise ValueError("check results cannot claim stronger than process_only isolation")
        if self.network_isolation_enforced is not False:
            raise ValueError("check results cannot claim network isolation")
        if self.sealed is not False:
            raise ValueError("process-only check results cannot claim to be sealed")

    @property
    def output_truncated(self) -> bool:
        return self.stdout_truncated or self.stderr_truncated

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "check_id": self.check_id,
            "status": self.status,
            "ok": self.ok,
            "return_code": self.return_code,
            "timed_out": self.timed_out,
            "duration_seconds": self.duration_seconds,
            "command": {"argv": list(self.command), "shell": False},
            "cwd": self.cwd,
            "environment_keys": list(self.environment_keys),
            "stdout": {
                "artifact": self.stdout_artifact.as_dict(),
                "preview": self.stdout_preview,
                "observed_bytes": self.stdout_observed_bytes,
                "captured_bytes": self.stdout_captured_bytes,
                "truncated": self.stdout_truncated,
                "preview_truncated": self.stdout_preview_truncated,
            },
            "stderr": {
                "artifact": self.stderr_artifact.as_dict(),
                "preview": self.stderr_preview,
                "observed_bytes": self.stderr_observed_bytes,
                "captured_bytes": self.stderr_captured_bytes,
                "truncated": self.stderr_truncated,
                "preview_truncated": self.stderr_preview_truncated,
            },
            "output_limit_bytes": self.output_limit_bytes,
            "output_truncated": self.output_truncated,
            "partial_output": self.partial_output,
            "capture_errors": list(self.capture_errors),
            "error": self.error,
            "isolation": self.isolation,
            "network_isolation_enforced": self.network_isolation_enforced,
            "sealed": self.sealed,
        }

    def model_view(self) -> str:
        """Return bounded previews and handles, never argv or inherited state."""

        payload = {
            "check_id": self.check_id,
            "status": self.status,
            "ok": self.ok,
            "return_code": self.return_code,
            "timed_out": self.timed_out,
            "duration_seconds": self.duration_seconds,
            "stdout": {
                "preview": self.stdout_preview,
                "artifact": self.stdout_artifact.as_dict(),
                "observed_bytes": self.stdout_observed_bytes,
                "captured_bytes": self.stdout_captured_bytes,
                "truncated": self.stdout_truncated,
                "preview_truncated": self.stdout_preview_truncated,
            },
            "stderr": {
                "preview": self.stderr_preview,
                "artifact": self.stderr_artifact.as_dict(),
                "observed_bytes": self.stderr_observed_bytes,
                "captured_bytes": self.stderr_captured_bytes,
                "truncated": self.stderr_truncated,
                "preview_truncated": self.stderr_preview_truncated,
            },
            "output_limit_bytes": self.output_limit_bytes,
            "output_truncated": self.output_truncated,
            "partial_output": self.partial_output,
            "error": self.error,
            "isolation": self.isolation,
            "network_isolation_enforced": self.network_isolation_enforced,
            "sealed": self.sealed,
        }
        return json.dumps(payload, ensure_ascii=False)


class CheckRunner:
    """Execute fixed registry checks selected only by ID.

    Optional Windows process memory limits bound committed virtual memory, not
    resident memory or host capabilities. Other platforms must enforce their own
    limits; this option does not change the process-only isolation contract.
    """

    def __init__(
        self,
        policy: WorkspacePolicy,
        artifacts: ArtifactStore,
        *,
        max_output_bytes: int = 1024 * 1024,
        max_timeout_seconds: float = 120.0,
        process_memory_limit_bytes: int | None = None,
    ) -> None:
        if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes < 1:
            raise ValueError("max_output_bytes must be a positive integer")
        if (
            isinstance(max_timeout_seconds, bool)
            or not isinstance(max_timeout_seconds, (int, float))
            or max_timeout_seconds <= 0
        ):
            raise ValueError("max_timeout_seconds must be positive")
        if process_memory_limit_bytes is not None:
            if (
                isinstance(process_memory_limit_bytes, bool)
                or not isinstance(process_memory_limit_bytes, int)
                or not 1 <= process_memory_limit_bytes <= _SIZE_T(-1).value
            ):
                raise ValueError("process_memory_limit_bytes must be a positive integer fitting SIZE_T")
            if os.name != "nt":
                raise ValueError("process_memory_limit_bytes is supported only on Windows")
        self.policy = policy
        self.artifacts = artifacts
        self.max_output_bytes = max_output_bytes
        self.max_timeout_seconds = float(max_timeout_seconds)
        self.process_memory_limit_bytes = process_memory_limit_bytes

    def _finish(
        self,
        *,
        check_id: str,
        status: CheckStatus,
        return_code: int | None,
        timed_out: bool,
        started: float,
        command: tuple[str, ...],
        cwd: str | None,
        environment_keys: tuple[str, ...],
        collector: _OutputCollector,
        error: str | None,
    ) -> CheckResult:
        snapshot = collector.snapshot()
        stdout_artifact = self.artifacts.put_bytes(snapshot.stdout)
        stderr_artifact = self.artifacts.put_bytes(snapshot.stderr)
        stdout_preview, stderr_preview, stdout_preview_truncated, stderr_preview_truncated = (
            _bounded_previews(
                snapshot.stdout,
                snapshot.stderr,
                self.policy.max_output_chars,
            )
        )
        return CheckResult(
            check_id=check_id,
            status=status,
            ok=status == "passed",
            return_code=return_code,
            timed_out=timed_out,
            duration_seconds=max(0.0, time.monotonic() - started),
            command=command,
            cwd=cwd,
            environment_keys=environment_keys,
            stdout_artifact=stdout_artifact,
            stderr_artifact=stderr_artifact,
            stdout_preview=stdout_preview,
            stderr_preview=stderr_preview,
            stdout_observed_bytes=snapshot.stdout_observed_bytes,
            stderr_observed_bytes=snapshot.stderr_observed_bytes,
            stdout_captured_bytes=len(snapshot.stdout),
            stderr_captured_bytes=len(snapshot.stderr),
            stdout_truncated=snapshot.stdout_truncated,
            stderr_truncated=snapshot.stderr_truncated,
            stdout_preview_truncated=stdout_preview_truncated,
            stderr_preview_truncated=stderr_preview_truncated,
            output_limit_bytes=self.max_output_bytes,
            partial_output=timed_out or snapshot.truncated or bool(snapshot.capture_errors),
            capture_errors=snapshot.capture_errors,
            error=error,
        )

    @staticmethod
    def _kill(process: subprocess.Popen[bytes]) -> None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except ProcessLookupError:
                pass
            except PermissionError:
                if process.returncode is not None:
                    raise  # Killing a reaped leader cannot clean its remaining group.
        try:
            process.kill()
        except ProcessLookupError:
            pass

    def run(self, check_id: str) -> CheckResult:
        """Run an available check.  No argv, shell, or cwd input is accepted."""

        started = time.monotonic()
        collector = _OutputCollector(self.max_output_bytes)
        if not isinstance(check_id, str) or not _CHECK_ID.fullmatch(check_id):
            return self._finish(
                check_id=str(check_id),
                status="unavailable",
                return_code=None,
                timed_out=False,
                started=started,
                command=(),
                cwd=None,
                environment_keys=(),
                collector=collector,
                error="Unknown or invalid check id",
            )

        definition = CHECK_REGISTRY.get(check_id)
        if definition is None or not _definition_available(self.policy, definition):
            return self._finish(
                check_id=check_id,
                status="unavailable",
                return_code=None,
                timed_out=False,
                started=started,
                command=(),
                cwd=None,
                environment_keys=(),
                collector=collector,
                error="Check id is not available in this workspace",
            )

        return self._run_definition(definition)

    def _run_definition(
        self, definition: CheckDefinition, *, cancel_requested: Callable[[], bool] | None = None,
    ) -> CheckResult:
        """Execute an owned definition, never a model command.

        With cancellation enabled, uncertain cleanup raises CheckCleanupError;
        confirmed cleanup preserves the initiating cancellation or callback error.
        """

        check_id = definition.id
        started = time.monotonic()
        if cancel_requested is not None and cancel_requested():
            raise KeyboardInterrupt("Check cancelled")
        collector = _OutputCollector(self.max_output_bytes)
        try:
            cwd = _resolve_cwd(self.policy, definition)
        except (OSError, PolicyViolation) as exc:
            return self._finish(
                check_id=check_id,
                status="unavailable",
                return_code=None,
                timed_out=False,
                started=started,
                command=(),
                cwd=None,
                environment_keys=(),
                collector=collector,
                error=str(exc),
            )

        environment = _sanitized_environment(check_id, self.policy.root)
        popen_options: dict[str, Any] = {}
        windows_job: _WindowsJob | None = None
        if os.name == "posix":
            popen_options["start_new_session"] = True
        elif os.name == "nt":
            try:
                if self.process_memory_limit_bytes is None:
                    windows_job = _new_windows_job()
                else:
                    windows_job = _new_windows_job(
                        process_memory_limit_bytes=self.process_memory_limit_bytes,
                    )
            except (OSError, _WindowsJobError) as exc:
                return self._finish(
                    check_id=check_id,
                    status="launch_error",
                    return_code=None,
                    timed_out=False,
                    started=started,
                    command=definition.argv,
                    cwd=definition.cwd,
                    environment_keys=tuple(sorted(environment)),
                    collector=collector,
                    error=f"Windows containment setup failed: {type(exc).__name__}: {exc}",
                )
            popen_options["creationflags"] = _CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED

        if cancel_requested is not None:
            try:
                if cancel_requested():
                    raise KeyboardInterrupt("Check cancelled")
            except BaseException:
                if windows_job is not None:
                    windows_job.close()
                raise

        try:
            process = subprocess.Popen(
                list(definition.argv),
                cwd=str(cwd),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=False,
                bufsize=0,
                **popen_options,
            )
        except OSError as exc:
            cleanup_error = windows_job.close() if windows_job is not None else None
            error = f"{type(exc).__name__}: {exc}"
            if cleanup_error:
                error += f"; Windows containment cleanup failed: {cleanup_error}"
            return self._finish(
                check_id=check_id,
                status="launch_error",
                return_code=None,
                timed_out=False,
                started=started,
                command=definition.argv,
                cwd=definition.cwd,
                environment_keys=tuple(sorted(environment)),
                collector=collector,
                error=error,
            )

        assert process.stdout is not None
        assert process.stderr is not None
        if windows_job is not None:
            checking_cancel = cancel_requested is not None
            try:
                if cancel_requested is not None and cancel_requested():
                    raise KeyboardInterrupt("Check cancelled")
                checking_cancel = False
                windows_job.assign_and_resume(process.pid)
            except BaseException as exc:
                reraisable = checking_cancel or not isinstance(exc, Exception)
                try:
                    cleanup_errors = [
                        value
                        for value in (windows_job.terminate_and_close(),)
                        if value is not None
                    ]
                except BaseException:
                    if cancel_requested is None:
                        raise
                    cleanup_errors = ["job cleanup failed"]
                try:
                    process.kill()
                except BaseException as cleanup_exc:
                    if cancel_requested is None and not isinstance(cleanup_exc, OSError):
                        raise
                    cleanup_errors.append(f"{type(cleanup_exc).__name__}: {cleanup_exc}")
                try:
                    process.wait(timeout=5.0)
                except BaseException as cleanup_exc:
                    if cancel_requested is None and not isinstance(cleanup_exc, (OSError, subprocess.TimeoutExpired)):
                        raise
                    cleanup_errors.append(f"{type(cleanup_exc).__name__}: {cleanup_exc}")
                for stream in (process.stdout, process.stderr):
                    try:
                        stream.close()
                    except BaseException as cleanup_exc:
                        if cancel_requested is None:
                            if not isinstance(cleanup_exc, OSError):
                                raise
                        else:
                            cleanup_errors.append("pipe cleanup failed")
                if cancel_requested is not None and cleanup_errors:
                    raise CheckCleanupError(process.pid) from exc
                if reraisable:
                    raise
                error = f"Windows containment setup failed: {type(exc).__name__}: {exc}"
                if cleanup_errors:
                    error += "; cleanup: " + "; ".join(cleanup_errors)
                return self._finish(
                    check_id=check_id,
                    status="launch_error",
                    return_code=None,
                    timed_out=False,
                    started=started,
                    command=definition.argv,
                    cwd=definition.cwd,
                    environment_keys=tuple(sorted(environment)),
                    collector=collector,
                    error=error,
                )

        readers: tuple[threading.Thread, ...] = ()
        timeout = min(float(definition.timeout_seconds), self.max_timeout_seconds)
        timed_out = False
        interrupted = False
        cleanup_error: str | None = None
        cancellable_cleanup_failed = False
        cleanup_cause: BaseException | None = None
        try:
            if cancel_requested is not None and cancel_requested():
                raise KeyboardInterrupt("Check cancelled")
            readers = (
                threading.Thread(
                    target=_drain_pipe,
                    args=(process.stdout, "stdout", collector),
                    daemon=True,
                    name=f"forge8-{check_id}-stdout",
                ),
                threading.Thread(
                    target=_drain_pipe,
                    args=(process.stderr, "stderr", collector),
                    daemon=True,
                    name=f"forge8-{check_id}-stderr",
                ),
            )
            for reader in readers:
                reader.start()
            try:
                if cancel_requested is None:
                    process.wait(timeout=timeout)
                else:
                    deadline = started + timeout
                    while True:
                        if cancel_requested():
                            raise KeyboardInterrupt("Check cancelled")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(definition.argv, timeout)
                        try:
                            process.wait(timeout=min(0.1, remaining))
                        except subprocess.TimeoutExpired:
                            continue
                        if cancel_requested():
                            raise KeyboardInterrupt("Check cancelled")
                        break
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                cleanup_cause = exc
                try:
                    if windows_job is not None:
                        cleanup_error = windows_job.terminate_and_close()
                    else:
                        self._kill(process)
                except BaseException:
                    if cancel_requested is None:
                        raise
                    cancellable_cleanup_failed = True
                try:
                    process.wait(timeout=5.0)
                except BaseException as cleanup_exc:
                    if cancel_requested is None:
                        if not isinstance(cleanup_exc, subprocess.TimeoutExpired):
                            raise
                        self._kill(process)
                    else:
                        cancellable_cleanup_failed = True
        except BaseException as exc:
            interrupted = True
            cleanup_cause = exc
            # Own the child before starting readers: interruption at either stage
            # must release containment and attempt bounded reaping before pipe close.
            try:
                if windows_job is not None:
                    failure = windows_job.terminate_and_close()
                    if cancel_requested is not None and failure:
                        cancellable_cleanup_failed = True
                else:
                    self._kill(process)
            except BaseException:
                if cancel_requested is not None:
                    cancellable_cleanup_failed = True
            try:
                process.wait(timeout=5.0)
            except BaseException:
                if cancel_requested is not None:
                    cancellable_cleanup_failed = True
            raise
        finally:
            if os.name == "posix" and not interrupted and not timed_out:
                # A completed leader may leave same-group children holding pipes.
                # This cannot contain descendants that create a separate session.
                try:
                    self._kill(process)
                except BaseException as exc:
                    if cancel_requested is None and not isinstance(exc, OSError):
                        raise
                    cleanup_error = f"{type(exc).__name__}: {exc}"
                    if cleanup_cause is None:
                        cleanup_cause = exc
            if windows_job is not None and windows_job.handle is not None:
                try:
                    final_cleanup_error = windows_job.terminate_and_close()
                except BaseException as exc:
                    if cancel_requested is None:
                        raise
                    cancellable_cleanup_failed = True
                    if cleanup_cause is None:
                        cleanup_cause = exc
                    final_cleanup_error = None
                if final_cleanup_error:
                    cleanup_error = "; ".join(
                        value for value in (cleanup_error, final_cleanup_error) if value
                    )
            for reader in readers:
                if reader.ident is not None:
                    try:
                        reader.join(timeout=1.0)
                    except BaseException:
                        if cancel_requested is not None:
                            cancellable_cleanup_failed = True
                        elif not interrupted:
                            raise
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except BaseException as exc:
                    if cancel_requested is not None:
                        cancellable_cleanup_failed = True
                    elif not interrupted and not isinstance(exc, OSError):
                        raise
            for reader in readers:
                if reader.ident is not None:
                    try:
                        reader.join(timeout=0.1)
                    except BaseException:
                        if cancel_requested is not None:
                            cancellable_cleanup_failed = True
                        elif not interrupted:
                            raise
            if cancel_requested is not None and (cancellable_cleanup_failed or cleanup_error):
                raise CheckCleanupError(process.pid) from cleanup_cause

        if cleanup_error:
            status: CheckStatus = "launch_error"
            return_code = None
            containment = "Windows" if os.name == "nt" else "POSIX"
            error = f"{containment} containment cleanup failed: {cleanup_error}"
        elif timed_out:
            status: CheckStatus = "timed_out"
            return_code = None
            error = f"Check exceeded its {timeout:g}-second timeout"
        else:
            return_code = process.returncode
            status = _status_for_return_code(check_id, return_code)
            if status == "passed":
                error = None
            elif status == "failed":
                error = f"Check exited with status {return_code}"
            elif check_id == _PYTEST_CHECK_ID and return_code == _PYTEST_BOOTSTRAP_ERROR:
                error = (
                    f"python_pytest could not bootstrap pytest=={_PYTEST_VERSION} "
                    "before repository imports; install the exact forge8[pytest] "
                    "optional extra"
                )
            elif check_id == _UNITTEST_CHECK_ID:
                error = (
                    "python_unittest exited outside its attested pass/fail wrapper "
                    f"statuses ({return_code}); zero tests, skipped tests, expected "
                    "failures, unexpected successes, incomplete execution, or an "
                    "observed non-wrapper early exit is non-repairable"
                )
            else:
                error = (
                    "python_pytest exited outside its attested pass/fail wrapper "
                    f"statuses ({return_code}); result is non-repairable"
                )

        return self._finish(
            check_id=check_id,
            status=status,
            return_code=return_code,
            timed_out=timed_out,
            started=started,
            command=definition.argv,
            cwd=definition.cwd,
            environment_keys=tuple(sorted(environment)),
            collector=collector,
            error=error,
        )


__all__ = [
    "CHECK_REGISTRY",
    "CheckDefinition",
    "CheckResult",
    "CheckRunner",
    "discover_checks",
]
