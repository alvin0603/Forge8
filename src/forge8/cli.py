"""Forge8 command-line interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import shlex
import stat
import sys
import tempfile
import threading
from datetime import datetime, timezone
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Sequence

from . import __version__
from .checks import CHECK_REGISTRY
from .discovery import (
    SelectionRequired, _check_catalogue, _integrity as _discovery_integrity,
    plan_project_focus, prepare_discovery, run_discovery,
)
from .explain import (
    ExplanationError,
    _bounded_text,
    _file_state,
    _focus_actions,
    _inert_text,
    _next_explain_step,
    _reject_constant,
    _require_run_entry,
    _resident_completion,
    _strict_object,
    _was_interrupted,
    prepare_explanation,
    run_explanation,
)
from .inference import CancellableTransport, OpenAITransport
from .reading_preview import StructuredAnswerPreview
from .operator import AcceptanceGateResult, OperatorConfig, run_prepared_task
from .repair import BaselinePreflight, prepare_repository_repair
from .runtime import verify_model, verify_runtime
from .server import LocalServerSupervisor, prepare_server


def _load_json_object(path: Path, *, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {description} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{description} must contain a JSON object: {path}")
    return payload


_FIX_RUNTIME_MANIFEST = Path("config/runtimes/llama_cpp_b10621.json")
_FIX_MODEL_MANIFEST = Path("config/models/gemma4_e4b_qat_q4.json")
_FIX_PROFILE = Path("config/profiles/gemma4_e4b_text_8k.json")
_LINUX_RUNTIME_MANIFEST = Path("config/runtimes/llama_cpp_b10621_linux_cuda.json")
_LINUX_PROFILE = Path("config/profiles/gemma4_e4b_text_8k_linux.json")
_READ_MODEL_MANIFEST = Path("config/models/qwen35_9b_q4_k_m.json")
_READ_PROFILE = Path("config/profiles/qwen35_9b_read_8k.json")
_LINUX_READ_PROFILE = Path("config/profiles/qwen35_9b_read_8k_linux.json")
_GEMMA12_MODEL_MANIFEST = Path("config/models/gemma4_12b_qat_q4.json")
_GEMMA12_PROFILE = Path("config/profiles/gemma4_12b_read_8k.json")
_LINUX_GEMMA12_PROFILE = Path("config/profiles/gemma4_12b_read_8k_linux.json")
# A current directory may be the untrusted repository being inspected. Only the
# shipped configuration is eligible for implicit discovery from there; custom
# configurations require an explicit FORGE8_HOME or saved configure selection.
_CWD_ASSET_CONFIG_SHA256 = {
    _FIX_RUNTIME_MANIFEST: "05319c889111f14d1e43b3d520dbe74a7a0fa89db0f4241a8f309e03a10b6d3d",
    _FIX_MODEL_MANIFEST: "6a9f2a8beff4f58fe27a24ff91ee04856fbd7ae20f7e2a26e7787c0e60e799fb",
    _FIX_PROFILE: "5a7e5d5b214d57c91c7765fcb73eb488f43289e920131e379972cc19ccd0aafe",
    _LINUX_RUNTIME_MANIFEST: "d6ff12aa6e147c63803daac99b129a0f3519ee3a16dd6a2fc26a04dfffeddb34",
    _LINUX_PROFILE: "ac1e61ba4271d41b55fb75a6c536dca7aafbd5c5139d45e9bdb429a61ba24612",
    _READ_MODEL_MANIFEST: "8ce8e96162e4b1e02ea0f66368eca855596c24ca4c296dcd59c1f3383484b97b",
    _READ_PROFILE: "adf1180a799787c359074446d3df8de13b02e73c7f9eec928e73451620552749",
    _LINUX_READ_PROFILE: "e53720e0fbb80b6af41b48ec9ae0833bef1629077486c9e3485bcdb76b419441",
    _GEMMA12_MODEL_MANIFEST: "e0056790c8c80fff4bce69ef03ccd9eb017c17b248102208313c8289b09705cb",
    _GEMMA12_PROFILE: "71f353e731da6bc0ec4e15089650f47c661cbe47176acd23129d36d0079964a2",
    _LINUX_GEMMA12_PROFILE: "37c944d6c83c0c3e93606edd6448b9309bfdbfb93239f1236ee452053cc87d92",
}
_FIX_CHECK_TIMEOUT_SECONDS = 120.0
_FIX_CHECK_OUTPUT_BYTES = 512 * 1024

_OBSERVE_BOOTSTRAP = """\
import json, sys
from pathlib import Path
from forge8.observation import capture_test, MAX_REPORT_BYTES
request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if set(request) != {"root", "test", "sources", "output"} or not all(isinstance(request[k], str) for k in ("root", "test", "output")) or not isinstance(request["sources"], list) or not all(isinstance(p, str) for p in request["sources"]):
    raise ValueError("Invalid private observation input")
report = capture_test(Path(request["root"]), request["test"], tuple(request["sources"]))
data = json.dumps(report, separators=(",", ":")).encode("utf-8")
if len(data) > MAX_REPORT_BYTES:
    raise ValueError("Observation exceeds report limit")
with Path(request["output"]).open("xb") as handle:
    handle.write(data)
raise SystemExit(0 if report["complete"] else 2)
"""


def _write_observe_json(path: Path, value: dict) -> None:
    _require_run_entry(path.parent, directory=True, label="observation run directory")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
        json.dump(value, handle, separators=(",", ":"))


def _run_observe_cli(args: argparse.Namespace) -> int:
    run_root, before, after, code = None, {}, None, 2
    process = {"status": "not_started", "return_code": None, "timed_out": False, "output_truncated": False}
    report = {"schema_version": 1, "kind": "forge8.observation", "test": args.test,
        "complete": False, "test_passed": False, "tests_run": 0, "counts": {}, "events": [], "error": None}
    try:
        if not args.trust_project_execution:
            raise ValueError("observe executes project imports and a test; add --trust-project-execution only for trusted code")
        parts = args.test.split(".")
        if len(args.test.encode("utf-8")) > 512 or len(parts) < 3 or not all(part.isidentifier() for part in parts) or not parts[-1].startswith("test_"):
            report["test"] = None
            raise ValueError("observe --test must name one bounded module.Class.test_method")
        from .checks import CheckDefinition, CheckRunner
        from .observation import MAX_REPORT_BYTES, _source_pins
        from .repository import _read_regular_file, _strict_existing_directory
        from .workspace import ArtifactStore, WorkspacePolicy
        root = _strict_existing_directory(args.repo, "observation project")
        if not 1 <= len(args.source) <= 4:
            raise ValueError("observe requires one to four --source Python files")
        policy = WorkspacePolicy(root)
        before, _ = _source_pins(policy, tuple(args.source))
        assets = _resolve_fix_asset_root()
        if _paths_overlap(root, assets):
            raise ValueError("observation project and Forge8 assets must not overlap")
        run_root = _fix_runs_parent(assets, source_root=root) / _new_task_id("observe")
        run_root.mkdir(mode=0o700)
        raw = run_root / "capture.json"
        request = run_root / "input.json"
        _write_observe_json(request, {"root": str(root), "test": args.test, "sources": args.source, "output": str(raw)})
        executable = os.path.abspath(sys.executable)  # Preserve the active venv's executable symlink.
        bootstrap = f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n" + _OBSERVE_BOOTSTRAP
        definition = CheckDefinition("trusted_observation", "Trusted single unittest observation",
            "Selected-file positions only; process_only, not a filesystem or network sandbox",
            (executable, "-I", "-B", "-X", "utf8", "-c", bootstrap, str(request)), ".", args.source[0], 60.0)
        print(f"observe: {_inert_text(executable)}\nTest: {_inert_text(args.test)}\n"
            "Executing trusted project code with your user permissions (process_only); "
            "no filesystem/network sandbox; Forge8 does not start a model. Timeout: 60s. Ctrl+C cancels the child.", file=sys.stderr, flush=True)
        runner = CheckRunner(policy, ArtifactStore(run_root), max_output_bytes=128 * 1024, max_timeout_seconds=60.0)
        result = runner._run_definition(definition)
        process = {"status": result.status, "return_code": result.return_code, "timed_out": result.timed_out,
            "output_truncated": result.output_truncated}
        _write_observe_json(run_root / "check-result.json", result.as_dict())
        after, _ = _source_pins(policy, tuple(args.source))
        _require_run_entry(raw, directory=False, label="child observation report")
        metadata = raw.lstat()
        if metadata.st_size > MAX_REPORT_BYTES:
            raise ValueError("child observation report exceeds 128 KiB")
        data = _read_regular_file(raw, metadata, "capture.json")
        if len(data) > MAX_REPORT_BYTES:
            raise ValueError("child observation report exceeds 128 KiB")
        captured = json.loads(data, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
        if not isinstance(captured, dict) or type(captured.get("schema_version")) is not int or captured["schema_version"] != 1 or captured.get("kind") != "forge8.observation" or captured.get("test") != args.test:
            raise ValueError("child observation report has an invalid kind, schema or test selector")
        report = captured
        report["complete"] = (report.get("complete") is True and result.ok and not result.capture_errors
            and before == report.get("source_before") == after)
        if not report["complete"] and not report.get("error"):
            report["error"] = "Capture process, source consistency or event completeness did not pass"
        code = 0 if report["complete"] else 2
    except KeyboardInterrupt:
        process["status"] = "interrupted"
        report.update(complete=False, error="Observation interrupted; child cleanup was requested before returning")
        code = 130
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        report.update(complete=False, error=f"Observation unavailable: {_inert_text(exc)[:1000]}")
    report.update(source_before=before, source_after=after, source_unchanged=bool(before) and before == after, capture_process=process)
    if len(json.dumps(report, separators=(",", ":")).encode("utf-8")) > 128 * 1024:
        report.update(complete=False, truncated=True, events=[], error="Final observation exceeds 128 KiB")
        code = 2
    path = None
    if run_root is not None:
        try:
            path = run_root / "observation.json"
            _write_observe_json(path, report)
        except (OSError, ValueError) as exc:
            path, code = None, 2
            report.update(complete=False, error=f"Cannot save observation: {_inert_text(exc)[:1000]}")
    summary = {"status": "interrupted" if code == 130 else "complete" if report["complete"] else "incomplete",
        "complete": report["complete"], "test_passed": report.get("test_passed") is True,
        "counts": report.get("counts", {}), "capture_process": process, "run_root": str(run_root) if run_root else None,
        "observation": str(path) if path else None, "error": report.get("error")}
    if args.as_json:
        print(json.dumps(summary, ensure_ascii=True))
    else:
        print(f"Trace: {summary['status']}; test: {'passed' if summary['test_passed'] else 'not passed / unavailable'}")
        if run_root:
            print(f"Private run: {_inert_text(run_root)}\nObservation: {_inert_text(path) if path else 'not saved'}")
        if summary["error"]:
            print(_inert_text(summary["error"]), file=sys.stderr)
    return code


def _native_asset_paths() -> tuple[Path, Path]:
    """Select the native client's runtime and profile, including Linux in WSL."""

    system = platform.system()
    if system == "Windows":
        return _FIX_RUNTIME_MANIFEST, _FIX_PROFILE
    if system == "Linux":
        return _LINUX_RUNTIME_MANIFEST, _LINUX_PROFILE
    raise ValueError(
        f"no pinned native runtime for {system!r}; use native Windows Python in "
        "PowerShell or native Linux Python in WSL with its matching assets "
        "(see docs/PLATFORMS.md)"
    )


class _NoInferenceBackend:
    """Fail if an admission or infrastructure path unexpectedly reaches inference."""

    def __init__(self, reason: str) -> None:
        self.reason = reason[:1_000]

    def chat(self, _request: Any) -> Any:
        raise RuntimeError(self.reason)


def _fix_operator_config() -> OperatorConfig:
    """Return the non-user-tunable request budget derived from the pinned 8K profile."""

    return OperatorConfig(
        max_inference_calls=16,
        max_actions=14,
        max_parse_failures=3,
        max_verifier_attempts=5,
        max_repeated_actions=3,
        max_context_chars=10_000,
        max_observation_chars=5_000,
        max_tokens_per_action=1_536,
        check_timeout_seconds=_FIX_CHECK_TIMEOUT_SECONDS,
        check_output_bytes=_FIX_CHECK_OUTPUT_BYTES,
        temperature=0.0,
        seed=1,
    )


def _fix_asset_anchors(root: Path, reader: str = "gemma4") -> tuple[Path, ...]:
    if reader not in ("gemma4", "qwen35", "gemma12b"):
        raise ValueError(f"unsupported reader: {reader!r}")
    runtime_manifest, profile = _native_asset_paths()
    model_manifest = _FIX_MODEL_MANIFEST
    if reader == "qwen35":
        model_manifest = _READ_MODEL_MANIFEST
        profile = _LINUX_READ_PROFILE if platform.system() == "Linux" else _READ_PROFILE
    elif reader == "gemma12b":
        model_manifest = _GEMMA12_MODEL_MANIFEST
        profile = _LINUX_GEMMA12_PROFILE if platform.system() == "Linux" else _GEMMA12_PROFILE
    return (
        root / runtime_manifest,
        root / model_manifest,
        root / profile,
        root / "runtime",
        root / "models",
    )


def _is_fix_asset_root(root: Path, reader: str = "gemma4") -> bool:
    runtime_manifest, model_manifest, profile, runtime_root, model_root = (
        _fix_asset_anchors(root, reader)
    )
    return (
        runtime_manifest.is_file()
        and model_manifest.is_file()
        and profile.is_file()
        and runtime_root.is_dir()
        and model_root.is_dir()
    )


def _resolve_fix_asset_root(reader: str = "gemma4") -> Path:
    """Find the trusted Forge8 checkout without accepting a CLI path override."""

    runtime_manifest, model_manifest, profile, _, _ = _fix_asset_anchors(Path(), reader)
    configurations = (runtime_manifest, model_manifest, profile)
    configured = os.environ.get("FORGE8_HOME")
    origin = "FORGE8_HOME"
    if configured is None:
        saved = _load_deployment()
        if saved is not None:
            configured, origin = saved["assets"], "saved deployment assets"
    if configured is not None:
        if not configured.strip() or "\x00" in configured:
            raise ValueError("FORGE8_HOME must name a non-empty local directory")
        candidates = (Path(configured).expanduser(),)
        explicit = True
    else:
        module = Path(__file__).resolve(strict=True)
        candidates = tuple(list(module.parents)[:5])
        explicit = False

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            root = candidate.resolve(strict=True)
        except (OSError, ValueError):
            continue
        if root in seen:
            continue
        seen.add(root)
        if root.is_dir() and _is_fix_asset_root(root, reader):
            return root
    if not explicit:
        try:
            current = Path.cwd().resolve(strict=True)
            if _is_fix_asset_root(current, reader):
                for relative in configurations:
                    expected = _CWD_ASSET_CONFIG_SHA256.get(relative)
                    with (current / relative).open("rb") as handle:
                        content = handle.read(65_537)
                    # Git on Windows may check out these text files with CRLF.
                    normalized = content.replace(b"\r\n", b"\n")
                    if (
                        len(content) > 65_536
                        or hashlib.sha256(normalized).hexdigest() != expected
                    ):
                        break
                else:
                    return current
        except (OSError, ValueError):
            pass
    native_hint = (
        f"; native {platform.system()} requires {runtime_manifest.as_posix()}, {model_manifest.as_posix()} and "
        f"{profile.as_posix()} with the matching installed runtime. "
        "See docs/PLATFORMS.md; Windows executables are not a native WSL runtime"
    )
    if explicit:
        raise ValueError(
            f"{origin} does not contain the pinned config, runtime, and model assets"
            + native_hint
        )
    raise ValueError(
        "cannot locate Forge8 assets; run forge8 configure --assets ABS --state ABS, "
        "run from the checkout, or set FORGE8_HOME"
        + native_hint
    )


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


_DEPLOYMENT_MAX_BYTES = 8_192


def _absolute_deployment_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{label} must name an absolute non-root directory")
    path = Path(value)
    if not path.is_absolute() or path == Path(path.anchor) or ".." in path.parts:
        raise ValueError(f"{label} must name an absolute non-root directory without '..'")
    return path


def _deployment_config_path() -> Path:
    if platform.system() == "Linux":
        variable, fallback, directory = "XDG_CONFIG_HOME", ".config", "forge8"
    elif platform.system() == "Windows":
        variable, fallback, directory = "LOCALAPPDATA", "AppData/Local", "Forge8"
    else:
        raise ValueError("deployment settings support native Windows or Linux only")
    base = os.environ.get(variable)
    if base is None:
        try:
            base = str(Path.home() / fallback)
        except RuntimeError as exc:
            raise ValueError(f"cannot locate home directory; set an absolute {variable}") from exc
    return _absolute_deployment_path(base, variable) / directory / "deployment.json"


def _read_deployment_bytes(path: Path) -> bytes | None:
    for parent in reversed(path.parents):
        if os.path.lexists(parent):
            _require_run_entry(parent, directory=True, label="deployment configuration parent")
    if not os.path.lexists(path):
        return None
    _require_run_entry(path, directory=False, label="deployment configuration")
    before = path.lstat()
    if before.st_size > _DEPLOYMENT_MAX_BYTES:
        raise ValueError("deployment configuration exceeds 8192 bytes")
    with path.open("rb") as handle:
        if not os.path.samestat(before, os.fstat(handle.fileno())):
            raise ValueError("deployment configuration changed while opening")
        data = handle.read(_DEPLOYMENT_MAX_BYTES + 1)
        after = os.fstat(handle.fileno())
    _require_run_entry(path, directory=False, label="deployment configuration")
    if (len(data) > _DEPLOYMENT_MAX_BYTES or before.st_size != len(data)
            or before.st_mtime_ns != after.st_mtime_ns
            or not os.path.samestat(before, path.lstat())):
        raise ValueError("deployment configuration changed while reading")
    return data


def _decode_deployment(data: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("deployment configuration contains duplicate keys")
            result[key] = value
        return result

    try:
        record = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except RecursionError as exc:
        raise ValueError("deployment configuration is too deeply nested") from exc
    if (not isinstance(record, dict) or set(record) != {"schema_version", "assets", "state"}
            or type(record["schema_version"]) is not int or record["schema_version"] != 1):
        raise ValueError("deployment configuration requires schema_version 1, assets and state only")
    for field in ("assets", "state"):
        _absolute_deployment_path(record[field], f"saved deployment {field}")
    return record


def _load_deployment() -> dict | None:
    data = _read_deployment_bytes(_deployment_config_path())
    return None if data is None else _decode_deployment(data)


def _state_directory_plan(asset_root: Path | None, configured: str | None):
    """Validate existing state ancestors without creating directories."""
    if configured is None:
        if asset_root is None:
            raise ValueError("an explicit absolute state directory is required without assets")
        state_root = asset_root / ".forge8"
        candidates = [state_root, state_root / "runs"]
    else:
        state_root = Path(configured)
        if (
            not configured.strip()
            or "\x00" in configured
            or not state_root.is_absolute()
            or state_root == Path(state_root.anchor)
            or ".." in state_root.parts
        ):
            raise ValueError("FORGE8_STATE_HOME must be an absolute non-root directory")
        current = Path(state_root.anchor)
        candidates = []
        for name in (*state_root.parts[1:], "runs"):
            current = current / name
            candidates.append(current)

    # Inspect every supplied ancestor before creating anything. In particular,
    # a state path inside the source must not add directories before rejection.
    missing = []
    for candidate in candidates:
        if os.path.lexists(os.fspath(candidate)):
            metadata = candidate.lstat()
            if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"Forge8 evidence directory is not a regular directory: {candidate}")
        else:
            missing.append(candidate)
    return state_root, candidates, missing


def _fix_runs_parent(asset_root: Path, *, source_root: Path) -> Path:
    """Create host-selected private evidence storage, never inside the source."""

    configured = os.environ.get("FORGE8_STATE_HOME")
    if configured is None:
        saved = _load_deployment()
        configured = saved["state"] if saved is not None else None
    return _create_runs_parent(asset_root, configured, source_root=source_root)


def _create_runs_parent(asset_root: Path | None, configured: str | None, *, source_root: Path) -> Path:
    """Shared state admission; callers decide explicit versus saved path selection."""
    state_root, candidates, missing = _state_directory_plan(asset_root, configured)
    proposed_runs = candidates[-1].resolve(strict=False)
    if _paths_overlap(source_root, proposed_runs):
        raise ValueError("target repository and Forge8 evidence directory must not overlap")
    for candidate in missing:
        candidate.mkdir(mode=0o700, exist_ok=False)
    resolved = candidates[-1].resolve(strict=True)
    try:
        resolved.relative_to(state_root)
    except ValueError as exc:
        raise ValueError("Forge8 evidence directory escapes the selected state root") from exc
    return resolved


def _run_configure_cli(args: argparse.Namespace) -> int:
    try:
        path = _deployment_config_path()
        previous = _read_deployment_bytes(path)
        if args.assets is None and args.state is None and not args.replace:
            saved = None if previous is None else _decode_deployment(previous)
            status = "not_configured" if saved is None else "saved_record"
        else:
            if args.assets is None or args.state is None:
                raise ValueError("provide both --assets and --state; omit both to show saved settings")
            assets = _absolute_deployment_path(args.assets, "--assets")
            state = _absolute_deployment_path(args.state, "--state")
            _require_run_entry(assets, directory=True, label="deployment assets")
            if not all(_is_fix_asset_root(assets, reader) for reader in ("gemma4", "qwen35")):
                raise ValueError("--assets needs both Gemma and Qwen native config/profile anchors, runtime and models directories")
            _state_directory_plan(assets, str(state))
            if state == path or path in state.parents:
                raise ValueError("--state cannot be inside the deployment configuration file")
            saved = {"schema_version": 1, "assets": str(assets.resolve(strict=True)), "state": str(state.resolve(strict=False))}
            data = (json.dumps(saved, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if len(data) > _DEPLOYMENT_MAX_BYTES:
                raise ValueError("deployment configuration exceeds 8192 bytes")
            try:
                old = None if previous is None else _decode_deployment(previous)
            except ValueError:
                if not args.replace:
                    raise
                old = None  # Explicit replacement may repair a safe but invalid record.
            if old == saved:
                status = "unchanged"
            else:
                if previous is not None and not args.replace:
                    raise ValueError("saved deployment differs; use --replace to replace this app's settings")
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _read_deployment_bytes(path)  # Recheck newly created ancestors before publishing.
                descriptor, temporary_name = tempfile.mkstemp(prefix=".deployment-", suffix=".json", dir=path.parent)
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if previous is None:
                        os.link(temporary, path)  # Atomic create, never overwrite a concurrent setup.
                        temporary.unlink()
                    else:
                        os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
                status = "saved"
        print(json.dumps({"config_path": str(path), "status": status, "saved": saved,
            "environment_overrides": {key: os.environ.get(key) for key in ("FORGE8_HOME", "FORGE8_STATE_HOME")},
            "note": "Per-field precedence: environment > saved record > checkout defaults. Setup checks native asset anchors only; runtime/model hashes remain verified when used."}, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"Deployment configuration ERROR: {_inert_text(exc)}", file=sys.stderr)
        return 2


def _new_task_id(kind: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{kind}-{timestamp}-{secrets.token_hex(4)}"


def _baseline_payload(preflight: BaselinePreflight) -> dict[str, Any]:
    return {
        "status": preflight.status,
        "repairable": preflight.repairable,
        "reason": preflight.reason,
        "checks": [check.as_dict() for check in preflight.checks],
    }


def _fix_model_id(model_manifest_path: Path) -> str:
    payload = _load_json_object(model_manifest_path, description="model manifest")
    model_id = payload.get("id")
    if not isinstance(model_id, str) or not model_id.strip() or "\x00" in model_id:
        raise ValueError("model manifest id must be a non-empty string")
    return model_id


def _server_acceptance_gate(
    supervisor: LocalServerSupervisor,
    transport: OpenAITransport,
) -> AcceptanceGateResult:
    try:
        shutdown = supervisor.stop()
    finally:
        transport.api_key = None
    supervisor_secret_cleared = supervisor.api_key is None
    transport_secret_cleared = transport.api_key is None
    reclaim_status = shutdown.status in {"already_exited", "terminated", "killed"}
    evidence = {
        "shutdown_status": shutdown.status,
        "return_code_observed": shutdown.return_code is not None,
        "supervisor_secret_cleared": supervisor_secret_cleared,
        "transport_secret_cleared": transport_secret_cleared,
    }
    if (
        shutdown.ok
        and reclaim_status
        and shutdown.return_code is not None
        and supervisor_secret_cleared
        and transport_secret_cleared
    ):
        return AcceptanceGateResult(ok=True, reason=None, evidence=evidence)
    return AcceptanceGateResult(
        ok=False,
        reason="local inference server shutdown or secret cleanup was not proven",
        evidence=evidence,
    )


def _server_lifecycle_payload(
    preparation: Any,
    supervisor: Any,
    transport: Any,
) -> dict[str, Any] | None:
    if preparation is None:
        return None
    start_result = None if supervisor is None else supervisor.start_result
    shutdown_result = None if supervisor is None else supervisor.shutdown_result
    return {
        "preparation": preparation.as_dict(),
        "start": None if start_result is None else start_result.as_dict(),
        "shutdown": None if shutdown_result is None else shutdown_result.as_dict(),
        "supervisor_secret_cleared": (
            supervisor is None or supervisor.api_key is None
        ),
        "transport_secret_cleared": transport is None or transport.api_key is None,
    }


def _fix_payload(
    *,
    status: str,
    ok: bool,
    task_id: str | None,
    source_repo: Path,
    run_root: Path | None,
    preflight: BaselinePreflight | None = None,
    server: dict[str, Any] | None = None,
    outcome: Any = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "forge8.fix",
        "status": status,
        "ok": ok,
        "task_id": task_id,
        "source_repo": str(source_repo),
        "run_root": None if run_root is None else str(run_root),
        "source_write_attempted": False,
        "source_unchanged": True if ok else None,
        "patch_applied": False,
        "baseline": None if preflight is None else _baseline_payload(preflight),
        "server": server,
        "outcome": None if outcome is None else outcome.as_dict(),
        "error": error,
    }


def _emit_fix_result(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    outcome = payload.get("outcome") or {}
    delivery = outcome.get("delivery") or {}
    if payload["ok"]:
        print("Forge8 fix VERIFIED - Forge8 did not apply the patch.")
        print("  Source fingerprint matched the initial snapshot at final verification.")
        print(f"  Handoff:      {delivery.get('handoff_path')}")
        print(f"  Manifest:     {delivery.get('manifest_path')}")
        print(f"  Verification: {delivery.get('verification_path')}")
        print(f"  Patch:        {delivery.get('patch_path')}")
        print(f"  Run evidence: {payload['run_root']}")
        print("  Review the handoff, manifest, and patch before applying it yourself.")
        print("  Safety: project tests ran as your user (process_only); use trusted code only.")
        return
    print(f"Forge8 fix NOT VERIFIED ({payload['status']})", file=sys.stderr)
    if payload.get("error"):
        print(f"  {payload['error']}", file=sys.stderr)
    elif outcome.get("failure_reason"):
        print(f"  {outcome['failure_reason']}", file=sys.stderr)
    if delivery:
        print(f"  Handoff:  {delivery.get('handoff_path')}", file=sys.stderr)
        print(f"  Manifest: {delivery.get('manifest_path')}", file=sys.stderr)
        print("  Do not apply a patch from a NOT VERIFIED run.", file=sys.stderr)
    print(f"  Evidence: {payload.get('run_root') or 'not created'}", file=sys.stderr)


def _progress(args: argparse.Namespace, message: str) -> None:
    if args.as_json:
        return
    rendered = f"Forge8: {_inert_text(message)}"
    if sys.stderr.encoding:
        rendered = rendered.encode(
            sys.stderr.encoding, errors="backslashreplace"
        ).decode(sys.stderr.encoding)
    print(rendered, file=sys.stderr, flush=True)


def _run_fix_cli(args: argparse.Namespace) -> int:
    source_argument = Path(args.repo).expanduser()
    task_id: str | None = None
    run_root: Path | None = None
    preflight: BaselinePreflight | None = None
    secret_was_issued = False
    preparation: Any = None
    supervisor: Any = None
    transport: OpenAITransport | None = None
    try:
        source_resolved = source_argument.resolve(strict=True)
        if not source_resolved.is_dir():
            raise ValueError(f"repository is not a directory: {source_argument}")
        asset_root = _resolve_fix_asset_root()
        if _paths_overlap(source_resolved, asset_root):
            raise ValueError(
                "target repository and Forge8 asset root must not overlap"
            )

        _progress(args, "sanitizing and fingerprinting the repository")
        runs_parent = _fix_runs_parent(asset_root, source_root=source_resolved)
        task_id = _new_task_id("fix")
        run_root = runs_parent / task_id
        if _paths_overlap(source_resolved, run_root):
            raise ValueError("target repository and repair run root must not overlap")

        prepared = prepare_repository_repair(
            source_argument,
            run_root,
            args.goal,
            args.allow_write,
            args.check,
            task_id,
            check_timeout_seconds=_FIX_CHECK_TIMEOUT_SECONDS,
            check_output_bytes=_FIX_CHECK_OUTPUT_BYTES,
        )
        _progress(args, "running the fixed baseline check without loading the model")
        preflight = prepared.baseline_preflight()
        config = _fix_operator_config()

        if not preflight.repairable:
            outcome = run_prepared_task(
                prepared.task,
                prepared.run_root,
                _NoInferenceBackend("baseline admission changed unexpectedly"),
                model="not-started",
                config=config,
            )
            payload = _fix_payload(
                status=preflight.status,
                ok=False,
                task_id=task_id,
                source_repo=source_resolved,
                run_root=prepared.run_root,
                preflight=preflight,
                outcome=outcome,
                error=preflight.reason,
            )
            _emit_fix_result(payload, as_json=args.as_json)
            return 1 if preflight.status == "already_passing" else 2

        _progress(
            args,
            "baseline failure confirmed; hashing about 7 GB of pinned local assets",
        )
        runtime_manifest, profile = _native_asset_paths()
        preparation = prepare_server(
            asset_root, runtime_manifest_path=runtime_manifest, profile_path=profile,
        )
        if not preparation.ok:
            outcome = run_prepared_task(
                prepared.task,
                prepared.run_root,
                _NoInferenceBackend(
                    "local inference server preparation failed: " + preparation.status
                ),
                model="not-started",
                config=config,
            )
            payload = _fix_payload(
                status=preparation.status,
                ok=False,
                task_id=task_id,
                source_repo=source_resolved,
                run_root=prepared.run_root,
                preflight=preflight,
                server={"preparation": preparation.as_dict()},
                outcome=outcome,
                error="; ".join(preparation.errors) or preparation.status,
            )
            _emit_fix_result(payload, as_json=args.as_json)
            return 2

        assert preparation.plan is not None
        plan = preparation.plan
        model_id = _fix_model_id(plan.model_manifest_path)
        _progress(args, "starting the verified local model")
        supervisor = LocalServerSupervisor(
            plan,
            log_directory=prepared.run_root / "server-logs",
            log_root=prepared.run_root,
        )
        outcome = None
        try:
            with supervisor:
                start_result = supervisor.start_result
                assert start_result is not None
                if start_result.ok:
                    if not isinstance(supervisor.api_key, str) or not supervisor.api_key:
                        raise RuntimeError("ready inference server has no in-memory API key")
                    secret_was_issued = True
                    transport = OpenAITransport(
                        plan.endpoint,
                        api_key=supervisor.api_key,
                        timeout_seconds=180.0,
                    )
                    _progress(
                        args,
                        "repairing only the staging copy; final checks will replay cleanly",
                    )

                    def acceptance_gate() -> AcceptanceGateResult:
                        assert transport is not None
                        return _server_acceptance_gate(supervisor, transport)

                    outcome = run_prepared_task(
                        prepared.task,
                        prepared.run_root,
                        transport,
                        model=model_id,
                        config=config,
                        acceptance_gate=acceptance_gate,
                    )
        finally:
            if transport is not None:
                transport.api_key = None

        start_result = supervisor.start_result
        assert start_result is not None
        shutdown_result = supervisor.shutdown_result
        server_payload = _server_lifecycle_payload(
            preparation,
            supervisor,
            transport,
        )
        assert server_payload is not None
        if not start_result.ok:
            outcome = run_prepared_task(
                prepared.task,
                prepared.run_root,
                _NoInferenceBackend(
                    "local inference server startup failed: " + start_result.status
                ),
                model=model_id,
                config=config,
            )
            payload = _fix_payload(
                status=start_result.status,
                ok=False,
                task_id=task_id,
                source_repo=source_resolved,
                run_root=prepared.run_root,
                preflight=preflight,
                server=server_payload,
                outcome=outcome,
                error=start_result.error or start_result.health_last_error,
            )
            _emit_fix_result(payload, as_json=args.as_json)
            return 2

        assert outcome is not None
        lifecycle_ok = (
            shutdown_result is not None
            and shutdown_result.ok
            and shutdown_result.status in {"already_exited", "terminated", "killed"}
            and shutdown_result.return_code is not None
            and supervisor.api_key is None
            and (transport is None or transport.api_key is None)
        )
        overall_ok = outcome.verified and lifecycle_ok
        status = outcome.status if lifecycle_ok else "server_shutdown_failed"
        payload = _fix_payload(
            status=status,
            ok=overall_ok,
            task_id=task_id,
            source_repo=source_resolved,
            run_root=prepared.run_root,
            preflight=preflight,
            server=server_payload,
            outcome=outcome,
            error=(
                None
                if lifecycle_ok
                else "local inference server shutdown or secret cleanup was not proven"
            ),
        )
        _emit_fix_result(payload, as_json=args.as_json)
        if overall_ok:
            return 0
        if not lifecycle_ok or outcome.status in {
            "backend_error",
            "acceptance_gate_failed",
        }:
            return 2
        return 1
    except KeyboardInterrupt:
        payload = _fix_payload(
            status="interrupted",
            ok=False,
            task_id=task_id,
            source_repo=source_argument,
            run_root=run_root,
            preflight=preflight,
            error="interrupted; any owned server cleanup was attempted",
        )
        _emit_fix_result(payload, as_json=args.as_json)
        return 130
    except Exception as exc:
        error = (
            f"{type(exc).__name__}: operation failed after local secret issuance"
            if secret_was_issued
            else f"{type(exc).__name__}: {exc}"
        )
        payload = _fix_payload(
            status="runtime_error" if secret_was_issued else "configuration_error",
            ok=False,
            task_id=task_id,
            source_repo=source_argument,
            run_root=run_root,
            preflight=preflight,
            server=_server_lifecycle_payload(preparation, supervisor, transport),
            error=error,
        )
        _emit_fix_result(payload, as_json=args.as_json)
        return 2


def _finish_explain(
    args: argparse.Namespace, status: str, source_repo: Path,
    task_id: str | None, run_root: Path | None, *,
    server: dict[str, Any] | None = None, outcome: Any = None,
    error: str | None = None, code: int | None = None,
    emit: Callable[[dict[str, Any]], None] | None = None,
    kind: str = "explain",
    project_reading: dict[str, Any] | None = None,
) -> int:
    outcome_payload = None if outcome is None else outcome.as_dict()
    result = {
        "schema_version": 1, "kind": f"forge8.{kind}", "status": status,
        "ok": False if outcome is None else outcome.ok, "task_id": task_id,
        "question": (
            outcome_payload.get("question")
            if outcome_payload and outcome_payload.get("question") is not None
            else getattr(args, "question", None)
        ),
        "source_repo": str(source_repo),
        "run_root": None if run_root is None else str(run_root),
        "repository_code_executed": False, "source_write_attempted": False,
        "semantic_claims_verified": False, "server": server,
        "outcome": outcome_payload, "error": error,
    }
    if project_reading is not None:
        result["project_reading"] = project_reading
    if isinstance(server, dict) and server.get("scope") == "resident_request":
        result["kind"] += ".request"
        result["request_completion"] = server.get("request_completion")
    if outcome_payload and "unverified_prose" in outcome_payload and _unverified_explain_prose(result) is None:
        result["outcome"] = {key: value for key, value in outcome_payload.items() if key != "unverified_prose"}
    if emit is not None:
        emit(result)
    elif args.as_json:
        print(json.dumps(result, indent=2))
    elif kind == "locate":
        _emit_locate_human(result)
    else:
        _emit_explain_human(result)
    if code is not None or result["ok"]:
        return code or 0
    if status == "interrupted":
        return 130
    return 2 if status in {
        "acceptance_gate_failed", "artifact_drift", "backend_error", "evidence_drift",
        "ingress_drift", "source_drift", "snapshot_drift", "tool_error",
    } else 1


def _explain_integrity_error(
    preparation: Any, reader: str, assets: Path,
    runtime_manifest: Path, model_manifest: Path,
) -> str:
    """Describe existing pre-start checks; never read assets or execute commands."""
    lines = [f"Asset verification failed for reader {reader}."]
    system = platform.system()
    for kind, integrity, manifest, directory in (
        ("runtime", preparation.runtime_integrity, runtime_manifest, "runtime"),
        ("model", preparation.model_integrity, model_manifest, "models"),
    ):
        if integrity is None or integrity.ok:
            continue
        lines.append(f"{kind}: {len(integrity.missing)} missing, {len(integrity.mismatched)} mismatched.")
        details = [("missing", value) for value in integrity.missing] + [
            ("mismatch", value) for value in integrity.mismatched
        ]
        for label, value in details[:3]:
            text = _inert_text(value)
            lines.append(f"{label}: {text[:240]}" + (" [truncated]" if len(text) > 240 else ""))
        if len(details) > 3:
            lines.append(f"{len(details) - 3} more items omitted; use --json for complete preparation details.")
        # sys.executable is already native and absolute. Do not resolve away a
        # venv symlink; -I also avoids importing from the user's current project.
        argv = [sys.executable, "-I", "-m", "forge8", kind, "verify",
                "--manifest", str(assets / manifest), "--root", str(assets / directory)]
        command = ("& " + " ".join("'" + value.replace("'", "''") + "'" for value in argv)
                   if system == "Windows" else shlex.join(argv))
        printable = len(command) <= 2000 and all(value.isprintable() for value in argv)
        if system == "Windows" and any(character in command for character in "\u2018\u2019\u201c\u201d"):
            printable = False  # PowerShell treats smart quotes as delimiters too.
        try:
            command.encode(sys.stderr.encoding or "utf-8")
        except (UnicodeError, LookupError):
            printable = False
        if printable:
            lines.extend((f"Verify manually in {'PowerShell' if system == 'Windows' else 'WSL/Linux Bash'} (no downloads):", command))
        else:
            lines.append("Copyable command omitted: paths cannot be displayed exactly within the limit. Follow docs/USAGE.md for this reader's native asset verification.")
    return "\n".join(lines)


def _unverified_explain_prose(result: dict[str, Any]) -> str | None:
    """Gate the engine's complete ordinary text; never promote a streamed excerpt."""
    outcome = result.get("outcome")
    if (result.get("status") != "stalled" or result.get("ok") is not False or result.get("error")
            or not isinstance(outcome, dict) or outcome.get("status") != "stalled"
            or outcome.get("ok") is not False or outcome.get("answer", {}) is not None
            or outcome.get("source_unchanged") is not True or outcome.get("snapshot_unchanged") is not True
            or not isinstance(outcome.get("acceptance"), dict) or outcome["acceptance"].get("ok") is not True
            or not _reading_request_complete(result)):
        return None
    try:
        return _bounded_text(outcome.get("unverified_prose"), "unverified model text", 12_000, multiline=True)
    except ValueError:
        return None


def _insufficient_explain_reason(result: dict[str, Any]) -> str | None:
    """Recognize a clean abstention, not proof that source is actually missing."""
    outcome = result.get("outcome")
    if (result.get("kind") not in {"forge8.explain", "forge8.explain.request"}
            or result.get("status") != "insufficient_evidence" or result.get("ok") is not False
            or result.get("error") or not isinstance(outcome, dict)
            or outcome.get("status") != "insufficient_evidence" or outcome.get("ok") is not False
            or outcome.get("answer", {}) is not None
            or outcome.get("source_unchanged") is not True or outcome.get("snapshot_unchanged") is not True
            or not isinstance(outcome.get("acceptance"), dict) or outcome["acceptance"].get("ok") is not True
            or not _reading_request_complete(result)):
        return None
    try:
        return _bounded_text(outcome.get("failure_reason"), "model's insufficient-source reason", 1000, multiline=True)
    except ValueError:
        return None


def _emit_explain_human(result: dict[str, Any]) -> None:
    detail = result["outcome"] or {}
    lines: list[str] = []
    question = detail.get("question") or result.get("question") or "not available"
    gpu_status = _explain_gpu_status(result.get("server"))
    if result["ok"]:
        coverage = detail.get("coverage") or {}
        stream = sys.stdout
        lines.extend(
            (
                "Forge8 explanation ANSWERED",
                "Question:",
                f"  {_inert_text(question)}",
            )
        )
        claims = sorted(
            (detail.get("answer") or {}).get("claims", []),
            key=lambda claim: claim.get("type") == "source_quote",
        )
        previous_quote = None
        for index, claim in enumerate(claims, start=1):
            if (detail.get("answer") or {}).get("format") == "cited_prose":
                lines.append("")
                lines.extend(_inert_text(line) for line in claim["text"].split("\n"))
                lines.extend(("", "Source references (range-checked; not semantic verification):"))
                lines.extend(
                    f"  [{item['evidence_id']}:L{item['start_line']}-L{item['end_line']}] "
                    f"{_inert_text(item['path'])}:{item['start_line']}-{item['end_line']}"
                    for item in claim["citations"]
                )
                continue
            is_quote = claim.get("type") == "source_quote"
            if is_quote != previous_quote:
                lines.append("Supporting source lines:" if is_quote else "Interpretation:")
                previous_quote = is_quote
            label = "Source quote" if is_quote else "Inference"
            citations = ", ".join(
                f"{_inert_text(item.get('path'))}:L{item.get('start_line')}"
                + (
                    ""
                    if item.get("end_line") == item.get("start_line")
                    else f"-L{item.get('end_line')}"
                )
                + f" ({_inert_text(item.get('evidence_id', '?'))})"
                for item in claim.get("citations", [])
            )
            lines.append(f"{index}. {label}")
            if is_quote:
                lines.extend(
                    (
                        "   Source excerpt (terminal-safe representation):",
                        "\n".join(
                            f"   | {_inert_text(line)}"
                            for line in str(claim.get("text", "")).split("\n")
                        ),
                    )
                )
            else:
                lines.append(f"   {_inert_text(claim.get('text'))}")
            lines.append(f"   {'Source' if is_quote else 'Based on'}: {citations}")
        observed = coverage.get("observed", {})
        admitted = coverage.get("admitted", {})
        cited = coverage.get("cited", {})
        lines.extend(
            (
                f"Coverage: {observed.get('files', 0)}/{admitted.get('files', 0)} files "
                f"and {observed.get('lines', 0)}/{admitted.get('lines', 0)} lines observed; "
                f"{cited.get('files', 0)} "
                f"{'file' if cited.get('files', 0) == 1 else 'files'} and "
                f"{cited.get('lines', 0)} "
                f"{'line' if cited.get('lines', 0) == 1 else 'lines'} cited.",
                gpu_status,
                "Artifacts:",
                f"  Answer:      {_inert_text(detail.get('answer_path') or 'not created')}",
                f"  Explanation: {_inert_text(detail.get('explanation_path') or 'not created')}",
                f"  Manifest:    {_inert_text(detail.get('manifest_path') or 'not created')}",
                f"  Evidence:    {_inert_text(result.get('run_root') or 'not created')}",
                "Safety: repository code was not executed or modified.",
                "Citations prove provenance, not the truth of model inferences.",
            )
        )
    else:
        stream = sys.stderr
        lines.extend(
            (
                f"Forge8 explanation INCOMPLETE ({_inert_text(result['status'])})",
                gpu_status,
                "Question:",
                f"  {_inert_text(question)}",
            )
        )
        reason = result["error"] or detail.get("failure_reason")
        if reason:
            if result["status"] == "integrity_failed":
                lines.append("Reason:")
                lines.extend(f"  {_inert_text(line)}" for line in str(reason).split("\n"))
            else:
                lines.append(f"Reason: {_inert_text(reason)}")
        unverified = _unverified_explain_prose(result)
        if unverified is not None:
            lines.extend(("", "Model text generated; references UNVERIFIED; semantics UNVERIFIED."))
            lines.extend(_inert_text(line) for line in unverified.split("\n"))
            lines.extend(("", "Source retained for this question (not answer citations):"))
            for row in (detail.get("coverage") or {}).get("observed", {}).get("ranges", []):
                for span in row["ranges"]:
                    lines.append(f"  {_inert_text(row['path'])}:{span['start_line']}-{span['end_line']}")
            lines.append("Next: compare the generated text with the retained source; no interpretation has been verified.")
        else:
            lines.append(f"Next: {_next_explain_step(str(result['status']))}")
        if detail:
            lines.append("Diagnostics:")
            for label, key in (("Diagnostic bundle", "answer_path"), ("Manifest", "manifest_path")):
                lines.append(f"  {label}: {_inert_text(detail.get(key) or 'not created')}")
        lines.append(f"  Evidence: {_inert_text(result.get('run_root') or 'not created')}")
        lines.append("Safety: Forge8 did not execute or attempt to write repository code.")

    excluded = (detail.get("coverage") or {}).get("excluded")
    project = result.get("project_reading")
    if project is not None:
        discovery = project["discovery"]
        lines.extend(("", "Project question: model-located candidates; relevance and completeness are NOT verified.",
            f"  Discovery evidence: {_inert_text(discovery['run_root'])}"))
        if project["answer_attempted"]:
            lines.append("  Automatically planned source ranges (actual reads are recorded in answer coverage):")
            for row in project["focus"]:
                lines.append(f"    {_inert_text(row['path'])}:{row['start_line']}-{row['end_line']}")
            if "context" in project:
                context = project["context"]
                lines.append(f"  Same-file supplements: {len(context['added'])}; one-hop lexical matches, NOT verified bindings or complete dependencies.")
                for row in context["added"]:
                    lines.append(f"    {_inert_text(row['path'])}:{row['start_line']}-{row['end_line']}")
                if context["skipped"]:
                    lines.append("  Optional context skipped: " + ", ".join(
                        f"{reason}={count}" for reason, count in context["skipped"].items()))
        else:
            lines.append("  No answer model was started. Inspect these candidates and select source in forge8 read:")
            for row in discovery["candidates"]:
                lines.append(f"    {_inert_text(row['path'])}:{row['start_line']}-{row['end_line']} {_inert_text(row['name'])}")
        if discovery.get("scope"):
            lines.append("  Locator considered definitions only in these model-selected files; other files may matter:")
            lines.extend(f"    {_inert_text(path)}" for path in discovery["scope"]["files"])
    if excluded is not None:
        paths = excluded.get("paths", [])
        lines.append(
            f"Excluded from inspection: {excluded.get('entries', 0)} entries "
            "(not sent to model or source-verified; directories include their descendants)."
        )
        lines.extend(f"  {_inert_text(path)}" for path in paths[:5])
        if len(paths) > 5:
            explanation_path = detail.get("explanation_path") or "explanation.json"
            lines.append(f"  Full excluded list: {_inert_text(explanation_path)}")

    rendered = "\n".join(lines)
    if stream.encoding:
        rendered = rendered.encode(stream.encoding, errors="backslashreplace").decode(
            stream.encoding
        )
    print(rendered, file=stream)


def _explain_gpu_state(server: object) -> str:
    if not isinstance(server, dict):
        return "not_acquired"
    if server.get("scope") == "resident_request":
        return "resident" if _resident_completion(server.get("request_completion")) else "unknown"
    start = server.get("start")
    shutdown = server.get("shutdown")
    released = (
        isinstance(shutdown, dict)
        and shutdown.get("ok") is True
        and shutdown.get("status") in {"already_exited", "terminated", "killed"}
        and shutdown.get("return_code") is not None
        and server.get("supervisor_secret_cleared") is True
        and server.get("transport_secret_cleared") is True
    )
    if released:
        return "released"
    if not isinstance(start, dict):
        if not isinstance(shutdown, dict) or shutdown.get("status") == "not_started":
            return "not_acquired"
        return "unknown"
    if start.get("ok") is not True and start.get("pid") is None and (
        not isinstance(shutdown, dict) or shutdown.get("status") == "not_started"
    ):
        return "not_acquired"
    return "unknown"


def _explain_gpu_status(server: object) -> str:
    state = _explain_gpu_state(server)
    if state == "released":
        return f"GPU/server: released ({_inert_text(server['shutdown']['status'])}; secrets cleared)."
    if state == "not_acquired":
        return "GPU/server: not acquired."
    if state == "resident":
        return "GPU/server: resident at request completion; session cleanup pending."
    return "GPU/server: release NOT PROVEN; inspect running processes before continuing."


def _emit_locate_human(result: dict[str, Any]) -> None:
    outcome = result.get("outcome") or {}
    scope = outcome.get("scope")
    lines = [
        "Forge8 source candidates (review before answering)" if result["ok"] else f"Forge8 source discovery INCOMPLETE ({_inert_text(result['status'])})",
        _explain_gpu_status(result.get("server")), "Question:",
        *[f"  {_inert_text(line)}" for line in str(result.get("question") or "").split("\n")],
    ]
    if result["ok"]:
        for item in outcome.get("candidates", []):
            lines.append(f"  {_inert_text(item['name'])} · {_inert_text(item['path'])}:{item['start_line']}-{item['end_line']}")
        if not outcome.get("candidates"):
            lines.append("  No candidate IDs were selected.")
        lines.extend((
            f"Snapshot: {_inert_text(outcome.get('snapshot_sha256'))}",
            "Discovery limit: at most 6 candidates from the selected files; 12,000 user-context characters per request."
            if scope else "Discovery limit: at most 6 candidates from a complete small Python catalogue; 12,000 user-context characters.",
            "Scope: file inventory, Python definition names and a bounded README excerpt; not function bodies or a resolved call graph.",
            "Review candidates, then select up to 3 ranges of at most 80 lines for explain/read. Character limits also apply; nothing was automatically added or answered.",
        ))
    else:
        reason = result.get("error") or outcome.get("failure_reason") or result["status"]
        lines.extend(f"  {_inert_text(line)}" for line in str(reason).split("\n"))
    if scope:
        lines.extend((
            f"Selected-file scope: {len(scope['files'])}/{scope['total_files']} files; "
            f"{scope['selected_functions']}/{scope['total_functions']} Python function definitions.",
            "Large catalogue: at most 2 requests in one server session; choose up to 3 files, then definitions from only those files.",
            "This model-selected scope may miss relevant files. It contains names and a README excerpt, not function bodies; no retries or automatic explanation.",
        ))
        lines.extend(f"  {_inert_text(path)}" for path in scope["files"])
    lines.append(f"Evidence: {_inert_text(result.get('run_root') or 'not created')}")
    lines.append("Repository code was not executed or modified. Candidate relevance is a model judgment, not verified behavior.")
    stream = sys.stdout if result["ok"] else sys.stderr
    rendered = "\n".join(lines)
    if stream.encoding:
        rendered = rendered.encode(stream.encoding, errors="backslashreplace").decode(stream.encoding)
    print(rendered, file=stream)


def _reading_request_complete(result: dict[str, Any]) -> bool:
    """Separate legacy shutdown acceptance from immutable resident request evidence."""
    if result.get("kind") not in {"forge8.explain.request", "forge8.locate.request"}:
        return _explain_gpu_state(result.get("server")) == "released"
    completion = _resident_completion(result.get("request_completion"))
    if completion is None or result.get("request_completion") != completion:
        return False
    detail = result.get("outcome")
    project = result.get("project_reading")
    if detail is None and isinstance(project, dict) and project.get("answer_attempted") is False:
        detail = project.get("discovery")
    if not isinstance(detail, dict) or detail.get("request_completion") != completion:
        return False
    acceptance = detail.get("acceptance")
    return (isinstance(acceptance, dict) and acceptance.get("ok") is True
        and _resident_completion(acceptance.get("evidence")) == completion
        and isinstance(result.get("server"), dict)
        and result["server"].get("scope") == "resident_request"
        and result["server"].get("server_session_id") == completion["server_session_id"]
        and result["server"].get("request_completion") == completion)


class _ResidentTransport(CancellableTransport):
    """Record only completion after the response and cancellation watcher close."""

    completed = False

    def chat(self, request):
        self.completed = False
        response = super().chat(request)
        self.completed = True
        return response


def _run_explain_cli(
    args: argparse.Namespace, *,
    on_progress: Callable[[str], None] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    on_text: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    expected_snapshot: str | None = None,
    focus_origin: str = "user_focus",
    context_supplements: tuple[str, ...] = (),
    source_guard: Callable[[], AcceptanceGateResult] | None = None,
    resident_model: Any = None,
) -> int:
    return _run_reading_cli(args, on_progress=on_progress, on_result=on_result,
        on_text=on_text, cancel_event=cancel_event, expected_snapshot=expected_snapshot,
        focus_origin=focus_origin,
        **({"context_supplements": context_supplements} if context_supplements else {}),
        **({"source_guard": source_guard} if source_guard is not None else {}),
        **({"resident_model": resident_model} if resident_model is not None else {}))


def _run_locate_cli(
    args: argparse.Namespace, *,
    on_progress: Callable[[str], None] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    cancel_event: threading.Event | None = None,
    expected_snapshot: str | None = None,
    resident_model: Any = None,
) -> int:
    return _run_reading_cli(args, kind="locate", on_progress=on_progress,
        on_result=on_result, cancel_event=cancel_event, expected_snapshot=expected_snapshot,
        **({"resident_model": resident_model} if resident_model is not None else {}))


def _run_project_cli(
    args: argparse.Namespace, *,
    on_progress: Callable[[str], None] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    on_text: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    expected_snapshot: str | None = None,
    resident_model: Any = None,
) -> int:
    return _run_reading_cli(args, kind="project", on_progress=on_progress,
        on_result=on_result, on_text=on_text, cancel_event=cancel_event,
        expected_snapshot=expected_snapshot,
        **({"resident_model": resident_model} if resident_model is not None else {}))


def _run_reading_cli(
    args: argparse.Namespace, *, kind: str = "explain",
    on_progress: Callable[[str], None] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    on_text: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    expected_snapshot: str | None = None,
    focus_origin: str = "user_focus",
    context_supplements: tuple[str, ...] = (),
    source_guard: Callable[[], AcceptanceGateResult] | None = None,
    resident_model: Any = None,
) -> int:
    source_repo = Path(args.repo).expanduser()
    task_id = run_root = None
    secret_was_issued = False
    preparation = supervisor = transport = None
    lease = None
    checkpoint = None
    lease_finished = False
    guard_callback = source_guard  # One captured original-source identity per job.

    def guarded_source() -> AcceptanceGateResult:
        try:
            assert guard_callback is not None
            result = guard_callback()
            if not isinstance(result, AcceptanceGateResult):
                raise TypeError("invalid original-source gate result")
            # Revalidate even an instance whose mutable evidence was changed.
            return AcceptanceGateResult(result.ok, result.reason, result.evidence)
        except BaseException:
            # Git/source errors can contain private paths or content. Never let
            # their text escape, or interrupt the existing server cleanup gate.
            return AcceptanceGateResult(False, "original source verification failed", {})

    def finish(status: str, **values: Any) -> int:
        return _finish_explain(args, status, source_repo, task_id, run_root,
            emit=on_result, kind="explain" if kind == "project" else kind, **values)

    def progress(message: str) -> None:
        if on_progress is not None:
            on_progress(message)
        else:
            _progress(args, message)

    def check_cancel() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt("explanation cancelled")

    def close_unfinished_lease() -> None:
        nonlocal lease_finished
        if lease is not None and not lease_finished:
            if transport is not None:
                transport.api_key = None
            try:
                lease.checkpoint(transport_completed=False, transport_secret_cleared=True)
            finally:
                lease.finish(None, source_checks_ok=False)
                lease_finished = True

    def close_failed_request() -> None:
        close_unfinished_lease()
        if lease is not None and lease_finished:
            state = resident_model.status()
            if state["state"] == "idle" and state["id"] == lease.session_id:
                try:
                    resident_model.release(lease.session_id)
                except ValueError:
                    pass  # The idle timer may already own this generation's close.

    try:
        check_cancel()
        if kind not in ("explain", "locate", "project"):
            raise ValueError("unknown reading operation")
        if guard_callback is not None and kind != "explain":
            raise ValueError("original source guards require a direct explanation")
        locating = kind in ("locate", "project")
        if kind == "project":
            progress("Project question: locating source before answering.")
            check_cancel()
        reader = getattr(args, "reader", "qwen35" if locating else "gemma4")
        selected_reader = reader in ("qwen35", "gemma12b")
        if locating and not selected_reader:
            raise ValueError("source discovery requires the qwen35 or gemma12b reader")
        if not locating and selected_reader and not getattr(args, "focus", None):
            raise ValueError(f"the {reader} preview reader requires --focus PATH:START-END; automatic navigation is not enabled")
        source_repo = source_repo.resolve(strict=True)
        if not source_repo.is_dir():
            raise ValueError(f"repository is not a directory: {source_repo}")
        asset_root = _resolve_fix_asset_root() if reader == "gemma4" else _resolve_fix_asset_root(reader)
        if _paths_overlap(source_repo, asset_root):
            raise ValueError("target repository and Forge8 asset root must not overlap")
        task_id = _new_task_id(kind)
        run_root = _fix_runs_parent(asset_root, source_root=source_repo) / task_id
        if _paths_overlap(source_repo, run_root):
            raise ValueError("target repository and explanation run root must not overlap")

        progress("sanitizing and fingerprinting a read-only source snapshot")
        prepared = prepare_explanation(
            source_repo, run_root, args.question, task_id,
            focus=() if locating else tuple(getattr(args, "focus", None) or ()),
            **({"focus_origin": focus_origin} if focus_origin != "user_focus" else {}),
            **({"context_supplements": context_supplements} if context_supplements else {}),
        )
        if expected_snapshot is not None and prepared.snapshot_sha256 != expected_snapshot:
            raise ValueError("source changed since browsing; refresh the source snapshot before asking again")
        if guard_callback is not None and not guarded_source().ok:
            return finish("source_guard_failed",
                error="original source verification failed; refresh before asking again", code=2)
        catalogue = None
        if locating:
            progress("building a complete bounded Python definition catalogue")
            catalogue = prepare_discovery(prepared)
            if catalogue.files is not None:
                progress("Large catalogue: at most 2 requests; choose up to 3 files, then definitions only within those files; relevant files may be missed.")
        check_cancel()
        runtime_manifest, model_manifest, profile = _fix_asset_anchors(Path(), reader)[:3]
        server_options = {"model_manifest_path": model_manifest} if selected_reader else {}
        if cancel_event is not None:
            server_options["cancel_requested"] = cancel_event.is_set
        if reader == "qwen35":
            progress("Qwen3.5 preview reader: one bounded thinking pass; answer may take about 1-2 minutes")
        elif reader == "gemma12b":
            progress("Gemma4 12B preview reader: one bounded thinking pass; larger model may take longer")
        if resident_model is None:
            progress("hashing the pinned local runtime and model")
        if resident_model is not None:
            from .residency import ResidentPreparationError
            try:
                lease = resident_model.acquire(asset_root, runtime_manifest_path=runtime_manifest,
                    model_manifest_path=model_manifest, profile_path=profile,
                    cancel_event=cancel_event if cancel_event is not None else threading.Event(), progress=progress)
            except ResidentPreparationError as exc:
                # Only a rejected pre-secret preflight crosses this typed boundary.
                # Reuse the ordinary failure formatter; never retry or create a server.
                check_cancel()
                preparation = exc.preparation
            else:
                supervisor = lease.supervisor
                secret_was_issued = True
                preparation = lease.preparation
        else:
            preparation = prepare_server(
                asset_root, runtime_manifest_path=runtime_manifest, profile_path=profile,
                **server_options,
            )
        if not preparation.ok:
            return finish(
                preparation.status,
                server={"preparation": preparation.as_dict()},
                error=(
                    _explain_integrity_error(preparation, reader, asset_root, runtime_manifest, model_manifest)
                    if preparation.status == "integrity_failed"
                    else "; ".join(preparation.errors) or preparation.status
                ),
                code=2,
            )

        assert preparation.plan is not None
        check_cancel()
        plan = preparation.plan
        model_id = lease.model_id if lease else _fix_model_id(plan.model_manifest_path)
        progress("resident model acquired; preparing the source request" if lease else "starting the verified local model")
        supervisor = lease.supervisor if lease else LocalServerSupervisor(
            plan,
            log_directory=prepared.run_root / "server-logs",
            log_root=prepared.run_root,
            **({"cancel_requested": cancel_event.is_set} if cancel_event is not None else {}),
        )
        outcome = None
        try:
            with nullcontext(supervisor) if lease else supervisor:
                start_result = supervisor.start_result
                assert start_result is not None
                if start_result.ok:
                    if not isinstance(supervisor.api_key, str) or not supervisor.api_key:
                        raise RuntimeError("ready inference server has no in-memory API key")
                    secret_was_issued = True
                    transport_type = _ResidentTransport if lease else (OpenAITransport if cancel_event is None else CancellableTransport)
                    # Decode only the answer's unverified text for live display.
                    # The engine still validates the untouched complete response.
                    preview = (StructuredAnswerPreview(on_text)
                        if reader == "qwen35" and not locating and on_text is not None else on_text)
                    transport = transport_type(
                        plan.endpoint,
                        api_key=supervisor.api_key,
                        timeout_seconds=180.0,
                        **({"cancel_event": lease.cancel_event, "abort": lease.abort} if lease else
                            ({"cancel_event": cancel_event, "abort": supervisor.stop} if cancel_event is not None else {})),
                        **({"on_text": preview} if not locating and selected_reader and preview is not None else {}),
                    )
                    progress("selecting source candidates from the catalogue; not answering" if locating else "reading only the sanitized snapshot and building source citations")

                    def acceptance_gate() -> AcceptanceGateResult:
                        nonlocal checkpoint
                        assert transport is not None
                        if lease:
                            transport.api_key = None
                            checkpoint = gate = lease.checkpoint(transport_completed=transport.completed,
                                transport_secret_cleared=transport.api_key is None)
                            evidence = dict(gate.evidence)
                        else:
                            gate = _server_acceptance_gate(supervisor, transport)
                            lifecycle = _server_lifecycle_payload(preparation, supervisor, transport)
                            assert lifecycle is not None
                            evidence = {
                                **gate.evidence, "lifecycle": lifecycle,
                                "server_logs": [
                                    _file_state(run_root, path)
                                    for path in sorted((run_root / "server-logs").glob("*"))
                                ],
                            }
                        if guard_callback is not None:
                            original = guarded_source()
                            evidence["original_source"] = {
                                "ok": original.ok, "reason": original.reason,
                                "evidence": original.evidence,
                            }
                            return AcceptanceGateResult(
                                gate.ok and original.ok,
                                gate.reason if not gate.ok else original.reason,
                                evidence,
                            )
                        return AcceptanceGateResult(gate.ok, gate.reason, evidence)

                    if locating:
                        outcome = run_discovery(prepared, catalogue, transport, model=model_id,
                            reader=reader, acceptance_gate=acceptance_gate, progress=progress,
                            **({"resident_session_id": lease.session_id} if lease else {}))
                    else:
                        outcome = run_explanation(
                            prepared,
                            transport,
                            model=model_id,
                            acceptance_gate=acceptance_gate,
                            progress=progress,
                            **({"reader": reader} if selected_reader else {}),
                            **({"reading_format": "structured_v1"} if reader == "qwen35" else {}),
                            **({"resident_session_id": lease.session_id} if lease else {}),
                        )
        finally:
            if transport is not None:
                transport.api_key = None
            if lease is not None:
                if checkpoint is None:
                    checkpoint = lease.checkpoint(transport_completed=False, transport_secret_cleared=True)
                clean = (outcome is not None and outcome.source_unchanged and outcome.snapshot_unchanged
                    and outcome.acceptance.get("ok") is True and outcome.status in {
                        "answered", "located", "stalled", "insufficient_evidence", "parse_budget_exhausted",
                        "action_budget_exhausted", "context_budget_exhausted", "inference_budget_exhausted"})
                artifact = ((prepared.run_root / "discovery.json") if locating else
                    (prepared.run_root / "manifest.json")) if outcome is not None else None
                registered = lease.finish(artifact, source_checks_ok=bool(clean))
                lease_finished = True

        assert start_result is not None
        server_payload = ({"scope": "resident_request", "server_session_id": lease.session_id,
            "request_completion": getattr(outcome, "request_completion", None)} if lease else
            _server_lifecycle_payload(preparation, supervisor, transport))
        assert server_payload is not None
        if lease and not registered:
            cancelled = cancel_event is not None and cancel_event.is_set()
            return finish("interrupted" if cancelled else "acceptance_gate_failed",
                server=_server_lifecycle_payload(preparation, supervisor, transport),
                error="resident request cancelled" if cancelled else "resident request could not be registered with its session",
                code=130 if cancelled else 2)
        if not start_result.ok:
            return finish(
                start_result.status,
                server=server_payload,
                error=start_result.error or start_result.health_last_error,
                code=130 if start_result.status == "interrupted" else 2,
            )

        assert outcome is not None
        if locating or selected_reader:
            check_cancel()
        if kind == "project" and outcome.ok:
            if (outcome.status != "located" or not outcome.source_unchanged
                    or not outcome.snapshot_unchanged or not outcome.ingress_unchanged
                    or not outcome.acceptance.get("ok")
                    or outcome.snapshot_sha256 != prepared.snapshot_sha256
                    or (not lease and _explain_gpu_state(server_payload) != "released")
                    or (lease and _resident_completion(outcome.request_completion, lease.session_id) is None)):
                return finish("acceptance_gate_failed", server=server_payload,
                    error="source discovery did not establish safe source and model release", code=2)
            progress("Project question: preparing complete candidate source.")
            check_cancel()
            project = {"discovery": outcome.as_dict(), "focus": [], "answer_attempted": False}
            try:
                source_plan = plan_project_focus(prepared, catalogue, outcome.candidates)
            except SelectionRequired as exc:
                check_cancel()
                return finish("selection_required", server=server_payload,
                    error=str(exc), project_reading=project, code=1)
            selectors = source_plan.focus
            actions = _focus_actions(selectors, focus_origin="project_candidates")
            project["focus"] = [{"path": action.path, "start_line": action.start_line,
                "end_line": action.end_line} for action in actions]
            project["context"] = {"added": [{"path": action.path,
                "start_line": action.start_line, "end_line": action.end_line}
                for action in _focus_actions(source_plan.supplements, focus_origin="project_candidates")],
                "skipped": dict(source_plan.skipped)}
            progress("Project question: answering from the automatically selected source.")
            check_cancel()
            _check_catalogue(prepared, catalogue)
            checks = _discovery_integrity(prepared)
            if any(_was_interrupted(error) for _, error in checks):
                raise KeyboardInterrupt()
            if not all(ok for ok, _ in checks):
                raise ExplanationError("source discovery changed before automatic reading")

            def answer_result(result: dict[str, Any]) -> None:
                # Failed preparation/drift/cleanup is not a usable candidate fallback.
                # Retained source and accepted references still belong to the actual
                # second-stage explanation, never to a guessed location or UI focus.
                detail = result.get("outcome") or {}
                if ((result.get("status") in {"answered", "stalled"}
                        or _insufficient_explain_reason(result) is not None) and not result.get("error")
                        and not (cancel_event is not None and cancel_event.is_set())
                        and detail.get("source_unchanged") is True
                        and detail.get("snapshot_unchanged") is True
                        and (detail.get("acceptance") or {}).get("ok") is True
                        and _reading_request_complete(result)):
                    result["project_reading"] = {**project, "answer_attempted": True}
                if on_result is not None:
                    on_result(result)
                elif args.as_json:
                    print(json.dumps(result, indent=2))
                else:
                    _emit_explain_human(result)

            answer_args = argparse.Namespace(**{**vars(args), "focus": list(selectors), "reader": reader})
            return _run_explain_cli(answer_args, on_progress=on_progress,
                on_result=answer_result, on_text=on_text, cancel_event=cancel_event,
                expected_snapshot=prepared.snapshot_sha256, focus_origin="project_candidates",
                context_supplements=source_plan.supplements,
                **({"resident_model": resident_model} if resident_model is not None else {}))
        return finish(outcome.status, server=server_payload, outcome=outcome)
    except KeyboardInterrupt:
        close_failed_request()
        return finish(
            "interrupted",
            server=_server_lifecycle_payload(preparation, supervisor, transport),
            error="interrupted; any owned server cleanup was attempted",
            code=130,
        )
    except Exception as exc:
        close_failed_request()
        error = (
            f"{type(exc).__name__}: operation failed after local secret issuance"
            if secret_was_issued
            else f"{type(exc).__name__}: {exc}"
        )
        return finish(
            "runtime_error" if secret_was_issued else "configuration_error",
            server=_server_lifecycle_payload(preparation, supervisor, transport),
            error=error,
            code=2,
        )


    finally:
        close_unfinished_lease()


def _run_experiment_cli(args: argparse.Namespace) -> int:
    """Explicit CPU-only workflow, separate from reading and model check tools."""
    from .experiments import RUNTIME_NAME, _validate_watch_names, native_platform, run_experiment, setup_runtime
    run_root = None
    try:
        if args.experiment_action == "run" and not args.allow_execution:
            raise ValueError("executes the WHOLE module, including initialization; add --allow-execution explicitly")
        if args.runtime is not None:
            runtime = _absolute_deployment_path(str(args.runtime), "experiment runtime")
        else:
            configured = os.environ.get("FORGE8_HOME")
            if configured is None:
                saved = _load_deployment()
                configured = saved["assets"] if saved else None
            if configured is None:
                raise ValueError("choose an absolute --runtime directory, or use existing Forge8 asset settings")
            assets = _absolute_deployment_path(configured, "Forge8 assets")
            runtime = assets / f"experiment-{RUNTIME_NAME}-{native_platform()}"
        if args.experiment_action == "setup":
            print("Verifying local archives and compiling the fixed interpreter once; no network or GPU.", file=sys.stderr, flush=True)
            result = setup_runtime(runtime, args.archives)
            code = 0
        else:
            from .experiments import prepare_request
            # Reject input errors before creating a private run directory.
            prepare_request(args.source, args.entry, args.input)
            module_options = {}
            if args.module_root is not None or args.module_file is not None:
                if args.module_root is None or args.module_file is None:
                    raise ValueError("bundle mode requires --module-root and every --module-file, including the entry")
                module_options = {"module_root": args.module_root, "module_files": args.module_file}
            if args.trace_lines:
                if module_options:
                    raise ValueError("--trace-lines supports single-file trials only")
                module_options["trace_lines"] = True
            if args.generator_steps is not None:
                if module_options:
                    raise ValueError("--generator-steps requires a single-file trial without --trace-lines")
                module_options["generator_steps"] = args.generator_steps
            watch_names = tuple(getattr(args, "watch_local", None) or ())
            _validate_watch_names(watch_names)
            if watch_names:
                if not args.trace_lines or args.generator_steps is not None or args.module_root is not None or args.module_file is not None:
                    raise ValueError("--watch-local requires --trace-lines, one source file and no generator steps")
                module_options["watch_names"] = watch_names
            run_root = _fix_runs_parent(runtime.parent, source_root=args.source.parent.resolve()) / _new_task_id("experiment")
            print("Executing the complete selected module(s), including package initialization, in CPython/WASI, then one JSON call; "
                "no host project mount, no GPU. 5s guest / 10s worker wait, plus setup/cleanup. Ctrl+C cancels.", file=sys.stderr, flush=True)
            if args.generator_steps is not None:
                print(f"Advance the returned generator at most {args.generator_steps} times, then explicitly close it; "
                    "both next() and close() may execute code. Reaching the step limit does not prove exhaustion.",
                    file=sys.stderr, flush=True)
            result = run_experiment(runtime, args.source, args.entry, args.input, run_root, allow_execution=True, **module_options)
            execution = result.get("execution") or {}
            code = 0 if (result["source_unchanged"] and result["runtime_unchanged"] and not execution.get("output_limit", False)
                and (execution.get("host_status"), execution.get("detail")) in (("exited", None), ("guest_exit", 0))
                and (args.generator_steps is None or
                    result["process_status"] == "passed" and result.get("reported_result") is not None)) else 2
        if args.as_json:
            print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        elif args.experiment_action == "setup":
            print(f"Experiment runtime ready: {_inert_text(result['runtime'])}\n"
                "CPython 3.14.7 / Wasmtime 48.0.0. Run is offline and does not compile or download.")
        else:
            execution = result.get("execution") or {}
            print(f"Worker: {_inert_text(result['process_status'])}; "
                f"guest: {_inert_text(execution.get('host_status', 'no completed output'))}; "
                f"worker {result['duration_seconds']:.2f}s; "
                f"including verification {result['elapsed_seconds']:.2f}s")
            reported = result.get("reported_result")
            if reported is not None:
                print("Guest-reported result (separate WASI environment, not a verified explanation):")
                for line in json.dumps(reported, ensure_ascii=False, allow_nan=False, indent=2).splitlines():
                    print(_inert_text(line))
            trace = result.get("reported_trace") if args.trace_lines else None
            if args.trace_lines:
                print("Guest-reported call line visits (untrusted; lines precede execution, not model validation):")
                if trace is None:
                    print("Unavailable; this is not an empty or complete execution path.")
                else:
                    print(json.dumps(trace["line_events"]))
                    if trace["truncated"]:
                        print("Only the first 1000 visits were retained; the path is incomplete.")
                    if not trace["hook_intact"]:
                        print("The guest trace hook changed; the path is incomplete.")
                    if not trace["line_events"]:
                        print("No selected-source visits reported; this does not prove no work occurred.")
                    if "watch" in trace:
                        watch = trace["watch"]
                        print("Selected entry locals: untrusted snapshots before lines; return events may be unwinding.")
                        for event in watch["events"]:
                            values = ", ".join(name + "=" + (value["json"] if value["state"] == "value" else "[" + value["state"] + "]")
                                for name, value in event["values"].items())
                            print(_inert_text(f'Call #{event["call_id"]}, {event["event"]} at L{event["line"]}: {values}'))
                        if watch["truncated"]:
                            print("Watched snapshots reached an event or byte limit; later values are unavailable.")
            for name, text in execution.get("guest_output", {}).items():
                if reported is not None and name == "stdout":
                    text = "\n".join(line for line in text.splitlines() if not line.startswith("FORGE8_GUEST_RESULT="))
                if trace is not None and name == "stdout":
                    text = "\n".join(line for line in text.splitlines() if not line.startswith("FORGE8_GUEST_TRACE="))
                if text:
                    print(f"Guest {name} (untrusted, not a verified explanation):")
                    for line in text.splitlines():
                        print(_inert_text(line))
            print(f"Saved: {_inert_text(str(run_root / 'experiment.json'))}")
            if not result["source_unchanged"]:
                print("Source or input changed; result is not bound to the current files.")
            if not result["runtime_unchanged"]:
                print("Runtime integrity could not be reconfirmed; do not rely on the guest output.")
        return code
    except KeyboardInterrupt:
        print("Experiment interrupted; owned process cleanup requested. No automatic retry.", file=sys.stderr)
        return 130
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        message = _inert_text(exc)
        if args.as_json:
            print(json.dumps({"status": "unavailable", "error": message,
                "run_root": str(run_root) if run_root else None}, ensure_ascii=True))
        else:
            print(f"Experiment unavailable: {message}", file=sys.stderr)
            if run_root is not None:
                print(f"Any partial evidence: {_inert_text(run_root)}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge8",
        description="Local code repair and explanation for 8 GB GPUs; native Windows/Linux assets",
    )
    parser.add_argument("--version", action="version", version=f"forge8 {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser("configure", help="save native asset/state paths for new shells; no paths shows saved settings")
    configure.add_argument("--assets", metavar="ABS", help="absolute native asset directory with both reader configurations")
    configure.add_argument("--state", metavar="ABS", help="absolute private state directory; use the Linux filesystem in WSL")
    configure.add_argument("--replace", action="store_true", help="explicitly replace a different saved deployment")

    doctor = subparsers.add_parser("doctor", help="diagnose selected-reader deployment with full asset hashes; no model start or settings changes")
    doctor.add_argument("--reader", choices=("gemma4", "qwen35", "gemma12b"), default="qwen35",
        help="reader to check; defaults to the reading desk's Qwen3.5, not Gemma repair")
    doctor.add_argument("--json", action="store_true", dest="as_json", help="machine-readable diagnostic; not a GPU/inference readiness guarantee")

    experiment = subparsers.add_parser("experiment", help="explicit offline Python function experiments; no model or host project execution")
    experiment_actions = experiment.add_subparsers(dest="experiment_action", required=True)
    experiment_setup = experiment_actions.add_parser("setup", help="install from pinned local archives and precompile once; no network")
    experiment_setup.add_argument("--archives", type=Path, required=True, help="directory containing the pinned CPython zip and native Wasmtime wheel")
    experiment_run = experiment_actions.add_parser("run", help="explicitly execute a whole self-contained module and one named function in WASI")
    experiment_run.add_argument("source", type=Path, help="one complete UTF-8 .py module, at most 64 KiB; initialization also executes")
    experiment_run.add_argument("--entry", required=True, help="one synchronous top-level function name")
    experiment_run.add_argument("--input", type=Path, required=True, help='UTF-8 JSON file, at most 16 KiB: {"args": [...], "kwargs": {...}}')
    experiment_run.add_argument("--module-root", type=Path, help="explicit import root for selected-module mode; no guessing of src layout")
    experiment_run.add_argument("--module-file", action="append", metavar="PATH", help="repeat for ALL selected relative .py paths, including entry and required __init__.py; at most 4 files / 64 KiB total")
    experiment_run.add_argument("--allow-execution", action="store_true", help="explicitly authorize selected module/package initialization and one function call in the separate WASI environment")
    experiment_run.add_argument("--trace-lines", action="store_true", help="opt in to at most 1000 guest-reported call-phase source line visits; single-file only, not model validation")
    experiment_run.add_argument("--watch-local", action="append", metavar="NAME", help="with --trace-lines, watch up to 3 declared entry locals; repeat once per name; untrusted bounded snapshots")
    experiment_run.add_argument("--generator-steps", type=int, choices=range(1, 13), metavar="N",
        help="advance a returned generator at most N times (1-12), then close; single-file only, no tracing; yields are untrusted JSON snapshots")
    for command in (experiment_setup, experiment_run):
        command.add_argument("--runtime", type=Path, help="absolute trusted runtime directory; default uses saved assets, never CWD")
        command.add_argument("--json", action="store_true", dest="as_json", help="machine-readable host status and separate untrusted guest output")

    fix = subparsers.add_parser(
        "fix",
        help="repair a sanitized repository and emit a verified patch without applying it",
        description=(
            "Repair a small UTF-8 Python repository entirely on this machine. "
            "Forge8 edits a staging copy and emits evidence plus a patch; it does "
            "not apply the patch automatically. Project tests run with your user "
            "privileges (process_only), without a filesystem or network sandbox. "
            "Use only trusted repositories or a disposable copy."
        ),
        epilog=(
            "example:\n"
            "  forge8 fix C:\\work\\project --goal \"Make the failing test pass\" "
            "--allow-write src/app.py --check python_unittest"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fix.add_argument(
        "repo",
        type=Path,
        help="source repository; generated patches are not applied automatically",
    )
    fix.add_argument(
        "--goal",
        required=True,
        help="bounded repair guidance; deterministic checks remain authoritative",
    )
    fix.add_argument(
        "--allow-write",
        action="append",
        required=True,
        help="existing repository-relative file or directory; repeat for more",
    )
    fix.add_argument(
        "--check",
        action="append",
        required=True,
        choices=tuple(sorted(CHECK_REGISTRY)),
        help=(
            "fixed strict-outcome check ID; python_pytest ignores repository pytest config "
            "and requires the pinned forge8[pytest] extra; repeat for more"
        ),
    )
    fix.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit one machine-readable result on stdout",
    )

    explain = subparsers.add_parser(
        "explain",
        help="answer a bounded code question from exact read-only source citations",
        description="Answer locally without editing or executing repository code.",
    )
    explain.add_argument("repo", type=Path, help="repository to snapshot read-only")
    explain.add_argument("--question", required=True,
        help="single bounded code-understanding question (maximum 2000 characters)",
    )
    explain.add_argument(
        "--focus", action="append", metavar="PATH:START-END",
        help=(
            "explain selected source directly, without model navigation; repository-relative "
            "path and inclusive lines, at most 80 lines per range; repeat up to 3 times"
        ),
    )
    explain.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    explain.add_argument(
        "--reader", choices=("gemma4", "qwen35", "gemma12b"), default="gemma4",
        help="qwen35/gemma12b: selected-code prose (requires --focus and separately provisioned model); gemma12b is experimental; gemma4: existing reader",
    )

    locate = subparsers.add_parser("locate", help="suggest source definitions for a question; inspect before selecting code to explain",
        description="Small Python catalogues use one request. Large catalogues use at most 2 requests: choose up to 3 files, then definitions from only those files. This may miss relevant files. Reads names and a README excerpt, not function bodies; no automatic explanation or retries.")
    locate.add_argument("repo", type=Path, help="project to snapshot without executing its code")
    locate.add_argument("--question", required=True, help="project question (maximum 2000 characters); no source ranges required")
    locate.add_argument("--reader", choices=("qwen35", "gemma12b"), default="qwen35",
        help="separately provisioned local reader; bounded catalogue selection, not an answer")
    locate.add_argument("--json", action="store_true", dest="as_json", help="emit versioned candidates and source/cleanup checks")

    ask = subparsers.add_parser("ask", help="locate and read source for a project question without choosing files or lines",
        description="Locate Python definitions, release the model, then read ALL returned candidate definitions in one bounded answer pass. Two model loads; 2 requests for small catalogues, at most 3 for large catalogues. If complete source cannot fit, stop for manual selection; no clipping, retries or repository code execution. Relevant implementations may still be missed.")
    ask.add_argument("repo", type=Path, help="project to snapshot read-only")
    ask.add_argument("--question", required=True, help="project question (maximum 2000 characters); no source ranges required")
    ask.add_argument("--reader", choices=("qwen35", "gemma12b"), default="qwen35",
        help="separately provisioned native reader; gemma12b is experimental")
    ask.add_argument("--json", action="store_true", dest="as_json", help="emit one explanation result with discovery and actual source scope")

    read = subparsers.add_parser("read", help="open a private local source-reading desk (Qwen3.5 by default)")
    read.add_argument("repo", type=Path, help="project to browse without executing its code")
    read.add_argument("--browse-only", action="store_true",
        help="static source navigation only; no asset configuration, AI, guest trials or Git comparison; requires --state")
    read.add_argument("--state", type=Path, metavar="ABSOLUTE_DIRECTORY",
        help="private snapshot storage for --browse-only only; outside the project; WSL: use Linux ext4, not /mnt/<drive>")
    read.add_argument("--reader", choices=("qwen35", "gemma12b"), default="qwen35",
        help="gemma12b: experimental opt-in; requires separately provisioned native assets, never downloads automatically")
    read.add_argument("--observation", type=Path, help="read-only import of an existing complete observation; never executes its test")
    read.add_argument("--allow-experiments", action="store_true",
        help="enable separately confirmed whole-module WASI function experiments; never automatic, requires optional runtime")
    read.add_argument("--idle-timeout", type=int, default=600, metavar="SECONDS",
        help="keep the model resident between questions; unload after 600 idle seconds by default; 0 restores one-shot mode (maximum 86400)")

    observe = subparsers.add_parser("observe", help="explicitly execute one trusted unittest and record source positions; no Forge8 model startup")
    observe.add_argument("repo", type=Path, help="trusted project; imports and tests run unsandboxed with your user permissions")
    observe.add_argument("--test", required=True, help="one exact module.Class.test_method, not a command")
    observe.add_argument("--source", required=True, action="append", help="relative .py file, including the test module; repeat 1-4 times")
    observe.add_argument("--trust-project-execution", action="store_true", help="explicitly authorize project imports/setup/test/teardown (process_only, no filesystem/network sandbox)")
    observe.add_argument("--json", action="store_true", dest="as_json", help="separate capture process, trace completeness and test outcome")

    runtime = subparsers.add_parser(
        "runtime", help="verify the exact pinned inference runtime"
    )
    runtime.add_argument("action", choices=("verify",))
    runtime.add_argument(
        "--root",
        type=Path,
        help="override runtime directory; default: FORGE8_HOME/runtime",
    )
    runtime.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help=(
            "override runtime manifest; native Windows default: "
            "FORGE8_HOME/config/runtimes/llama_cpp_b10621.json; "
            "native Linux/WSL default: "
            "FORGE8_HOME/config/runtimes/llama_cpp_b10621_linux_cuda.json"
        ),
    )
    runtime.add_argument("--json", action="store_true", dest="as_json")

    model = subparsers.add_parser("model", help="verify the exact pinned model weights")
    model.add_argument("action", choices=("verify",))
    model.add_argument(
        "--root",
        type=Path,
        help="override model directory; default: FORGE8_HOME/models",
    )
    model.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help=(
            "override model manifest; default: "
            "FORGE8_HOME/config/models/gemma4_e4b_qat_q4.json"
        ),
    )
    model.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "configure":
        return _run_configure_cli(args)
    if args.command == "doctor":
        from .doctor import run_doctor
        return run_doctor(args)
    if args.command == "observe":
        return _run_observe_cli(args)
    if args.command == "experiment":
        return _run_experiment_cli(args)
    if args.command == "read":
        from .desk import run_desk
        try:
            if args.browse_only:
                if args.state is None:
                    raise ValueError("--browse-only requires --state ABSOLUTE_DIRECTORY outside the project (WSL: Linux ext4)")
                if args.allow_experiments or args.reader != "qwen35" or args.idle_timeout not in (0, 600):
                    raise ValueError("--browse-only cannot select a model, enable experiments or set a resident timeout")
            elif args.state is not None:
                raise ValueError("--state is only for --browse-only; normal reading uses forge8 configure or FORGE8_STATE_HOME")
            options = {"reader": args.reader} if args.reader != "qwen35" else {}
            if args.browse_only:
                options.update(browse_only=True, state_directory=args.state)
            if args.observation is not None:
                options["observation"] = args.observation
            if args.allow_experiments:
                options["allow_experiments"] = True
            if args.idle_timeout != 600:
                options["idle_timeout"] = args.idle_timeout
            return run_desk(args.repo, **options)
        except (OSError, ValueError) as exc:
            print(f"Reading desk could not start: {_inert_text(exc)}", file=sys.stderr)
            return 2
    if args.command == "fix":
        return _run_fix_cli(args)
    if args.command == "explain":
        return _run_explain_cli(args)
    if args.command == "locate":
        return _run_locate_cli(args)
    if args.command == "ask":
        return _run_project_cli(args)
    if args.command == "runtime":
        try:
            runtime_root = args.root
            runtime_manifest = args.manifest
            if runtime_root is None or runtime_manifest is None:
                asset_root = _resolve_fix_asset_root()
                runtime_root = runtime_root or asset_root / "runtime"
                runtime_manifest = runtime_manifest or asset_root / _native_asset_paths()[0]
            result = verify_runtime(runtime_manifest, runtime_root)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if args.as_json:
                print(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "forge8.runtime.verify",
                            "status": "configuration_error",
                            "ok": False,
                            "error": error,
                        },
                        indent=2,
                    )
                )
            else:
                print(f"Runtime verification ERROR: {error}", file=sys.stderr)
            return 2
        if args.as_json:
            print(json.dumps(result.as_dict(), indent=2))
        elif result.ok:
            count = len(result.checked)
            print(
                f"Runtime integrity OK ({count} "
                f"{'file' if count == 1 else 'files'} checked)"
            )
        else:
            print("Runtime integrity FAILED")
            for item in result.missing:
                print(f"  missing: {item}")
            for item in result.mismatched:
                print(f"  mismatch: {item}")
        return 0 if result.ok else 1
    if args.command == "model":
        try:
            model_root = args.root
            model_manifest = args.manifest
            if model_root is None or model_manifest is None:
                asset_root = _resolve_fix_asset_root()
                model_root = model_root or asset_root / "models"
                model_manifest = model_manifest or asset_root / _FIX_MODEL_MANIFEST
            result = verify_model(model_manifest, model_root)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if args.as_json:
                print(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "forge8.model.verify",
                            "status": "configuration_error",
                            "ok": False,
                            "error": error,
                        },
                        indent=2,
                    )
                )
            else:
                print(f"Model verification ERROR: {error}", file=sys.stderr)
            return 2
        if args.as_json:
            print(json.dumps(result.as_dict(), indent=2))
        elif result.ok:
            count = len(result.checked)
            print(
                f"Model integrity OK ({count} "
                f"{'file' if count == 1 else 'files'} checked)"
            )
        else:
            print("Model integrity FAILED")
            for item in result.missing:
                print(f"  missing: {item}")
            for item in result.mismatched:
                print(f"  mismatch: {item}")
        return 0 if result.ok else 1
    return 2
