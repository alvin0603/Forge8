"""Verified lifecycle supervision for a native local llama.cpp server.

Windows and Linux Python launch their corresponding pinned native server.
WSL-to-Windows process launch is intentionally refused: the child can start,
but WSL NAT does not provide a reliable localhost path back to that process.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .runtime import IntegrityResult, load_manifest, verify_model, verify_runtime
from .inference import _open_local_request


_PROFILE_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "runtime_manifest",
        "model_manifest",
        "executable",
        "model",
        "host",
        "port",
        "context_tokens",
        "parallel",
        "kv_cache",
        "flash_attention",
        "reasoning",
        "reasoning_budget",
        "reasoning_format",
        "batch_size",
        "ubatch_size",
        "flags",
        "experimental",
    }
)
_PROFILE_FLAGS = frozenset({"--no-mmproj", "--offline", "--jinja", "--metrics", "--slots"})
_REQUIRED_PROFILE_FLAGS = frozenset({"--no-mmproj", "--offline"})
_FAILURE_LOG_TAIL_BYTES = 8 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _detect_wsl() -> bool:
    if platform.system() != "Linux":
        return False
    release = platform.release().lower()
    if "microsoft" in release or "wsl" in release:
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _read_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {description} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{description} must contain a JSON object: {path}")
    return payload


def _inside_workspace(
    workspace: Path,
    value: str | os.PathLike[str],
    *,
    description: str,
    must_exist: bool,
) -> Path:
    raw = Path(value)
    candidate = raw if raw.is_absolute() else workspace / raw
    try:
        resolved = candidate.resolve(strict=must_exist)
        resolved.relative_to(workspace)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{description} must resolve inside the workspace: {candidate}") from exc
    return resolved


def _inside_root(
    root: Path,
    value: str,
    *,
    description: str,
    must_exist: bool = False,
) -> Path:
    if not value or "\x00" in value:
        raise ValueError(f"invalid {description} path")
    candidate = (root / value).resolve(strict=must_exist)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{description} escapes its artifact root: {value}") from exc
    return candidate


def _positive_int(value: Any, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"profile {name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"profile {name} must not exceed {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class ServerProfile:
    id: str
    runtime_manifest: str
    model_manifest: str
    executable: str
    model: str
    host: str
    port: int
    context_tokens: int
    parallel: int
    kv_cache: str
    flash_attention: str
    reasoning: str
    reasoning_budget: int
    flags: tuple[str, ...]
    experimental: bool
    reasoning_format: str = "none"
    batch_size: int | None = None
    ubatch_size: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "runtime_manifest": self.runtime_manifest,
            "model_manifest": self.model_manifest,
            "executable": self.executable,
            "model": self.model,
            "host": self.host,
            "port": self.port,
            "context_tokens": self.context_tokens,
            "parallel": self.parallel,
            "kv_cache": self.kv_cache,
            "flash_attention": self.flash_attention,
            "reasoning": self.reasoning,
            "reasoning_budget": self.reasoning_budget,
            "reasoning_format": self.reasoning_format,
            "flags": list(self.flags),
            "experimental": self.experimental,
            **({"batch_size": self.batch_size, "ubatch_size": self.ubatch_size}
                if self.batch_size is not None else {}),
        }


def load_server_profile(path: Path) -> ServerProfile:
    """Load the deployment profile without accepting pass-through arguments."""

    payload = _read_json_object(path, "server profile")
    unknown = sorted(set(payload) - _PROFILE_KEYS)
    missing = sorted(_PROFILE_KEYS - {"reasoning_format", "batch_size", "ubatch_size"} - set(payload))
    if unknown:
        raise ValueError(f"server profile contains unknown fields: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"server profile is missing fields: {', '.join(missing)}")
    if payload.get("schema_version") != 1:
        raise ValueError(f"unsupported server profile schema in {path}")
    for key in ("id", "runtime_manifest", "model_manifest", "executable", "model"):
        if not isinstance(payload[key], str) or not payload[key] or "\x00" in payload[key]:
            raise ValueError(f"profile {key} must be a non-empty string")
    if payload["host"] != "127.0.0.1":
        raise ValueError("production server profile host must be 127.0.0.1")
    port = _positive_int(payload["port"], "port", maximum=65535)
    context_tokens = _positive_int(payload["context_tokens"], "context_tokens")
    parallel = _positive_int(payload["parallel"], "parallel")
    if context_tokens != 8192:
        raise ValueError("production server profile must use exactly 8192 context tokens")
    batch_size = ubatch_size = None
    if "batch_size" in payload or "ubatch_size" in payload:
        if "batch_size" not in payload or "ubatch_size" not in payload:
            raise ValueError("profile batch_size and ubatch_size must be supplied together")
        batch_size, ubatch_size = payload["batch_size"], payload["ubatch_size"]
        if (type(batch_size) is not int or type(ubatch_size) is not int
                or not 1 <= ubatch_size <= batch_size <= context_tokens):
            raise ValueError(
                "profile batch_size and ubatch_size must be integers satisfying "
                "1 <= ubatch_size <= batch_size <= context_tokens"
            )
    if parallel != 1:
        raise ValueError("production server profile must use parallel=1")
    if payload["kv_cache"] != "q8_0":
        raise ValueError("production server profile must use q8_0 KV cache")
    if payload["flash_attention"] != "on":
        raise ValueError("production server profile must enable flash attention")
    reasoning_budget = payload["reasoning_budget"]
    reasoning_format = payload.get("reasoning_format", "none")
    if isinstance(reasoning_budget, bool) or not isinstance(reasoning_budget, int):
        raise ValueError("profile reasoning_budget must be an integer, not a boolean")
    if (payload["reasoning"], reasoning_budget, reasoning_format) not in (
        ("off", 0, "none"),
        ("on", 2048, "auto"),
    ):
        raise ValueError(
            "production server profile reasoning/budget/format must be "
            "off/0/none or on/2048/auto"
        )
    if not isinstance(payload["experimental"], bool) or payload["experimental"]:
        raise ValueError("production server profile must set experimental=false")
    raw_flags = payload["flags"]
    if not isinstance(raw_flags, list) or not all(
        isinstance(flag, str) and flag and "\x00" not in flag for flag in raw_flags
    ):
        raise ValueError("profile flags must be a list of non-empty strings")
    flags = tuple(raw_flags)
    if len(flags) != len(set(flags)):
        raise ValueError("profile flags must not contain duplicates")
    unsupported_flags = sorted(set(flags) - _PROFILE_FLAGS)
    if unsupported_flags:
        raise ValueError(f"server profile contains unsupported flags: {', '.join(unsupported_flags)}")
    missing_flags = sorted(_REQUIRED_PROFILE_FLAGS - set(flags))
    if missing_flags:
        raise ValueError(f"server profile is missing required flags: {', '.join(missing_flags)}")
    return ServerProfile(
        id=payload["id"],
        runtime_manifest=payload["runtime_manifest"],
        model_manifest=payload["model_manifest"],
        executable=payload["executable"],
        model=payload["model"],
        host=payload["host"],
        port=port,
        context_tokens=context_tokens,
        parallel=parallel,
        kv_cache=payload["kv_cache"],
        flash_attention=payload["flash_attention"],
        reasoning=payload["reasoning"],
        reasoning_budget=reasoning_budget,
        flags=flags,
        experimental=payload["experimental"],
        reasoning_format=reasoning_format,
        batch_size=batch_size,
        ubatch_size=ubatch_size,
    )


@dataclass(frozen=True, slots=True)
class ServerPlan:
    workspace: Path
    profile: ServerProfile
    executable_path: Path
    model_path: Path
    command: tuple[str, ...]
    working_directory: Path
    endpoint: str
    platform_shape: str
    runtime_manifest_path: Path
    model_manifest_path: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.as_dict(),
            "executable_path": str(self.executable_path),
            "model_path": str(self.model_path),
            "command": list(self.command),
            "working_directory": str(self.working_directory),
            "endpoint": self.endpoint,
            "platform_shape": self.platform_shape,
            "runtime_manifest_path": str(self.runtime_manifest_path),
            "model_manifest_path": str(self.model_manifest_path),
            "authentication": {
                "enabled": True,
                "delivery": "LLAMA_API_KEY environment variable",
                "secret_in_command": False,
            },
        }


@dataclass(frozen=True, slots=True)
class ServerPreparation:
    status: str
    errors: tuple[str, ...]
    plan: ServerPlan | None = None
    runtime_integrity: IntegrityResult | None = None
    model_integrity: IntegrityResult | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ready" and self.plan is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "errors": list(self.errors),
            "plan": None if self.plan is None else self.plan.as_dict(),
            "runtime_integrity": (
                None if self.runtime_integrity is None else self.runtime_integrity.as_dict()
            ),
            "model_integrity": (
                None if self.model_integrity is None else self.model_integrity.as_dict()
            ),
        }


def _target_model_path(model_manifest: Mapping[str, Any], model_root: Path) -> Path:
    files = model_manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("model manifest files must be a list")
    targets: list[Path] = []
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("filename"), str):
            raise ValueError("model manifest contains an invalid file entry")
        path = _inside_root(model_root, entry["filename"], description="model artifact")
        if entry.get("role") == "target":
            targets.append(path)
    if len(targets) != 1:
        raise ValueError("model manifest must pin exactly one file with role 'target'")
    return targets[0]


def _runtime_server_path(
    runtime_manifest: Mapping[str, Any], runtime_root: Path, profile_executable: Path
) -> Path:
    install_dir = runtime_manifest.get("install_dir")
    required_files = runtime_manifest.get("required_files")
    assets = runtime_manifest.get("assets")
    if not isinstance(install_dir, str) or not install_dir:
        raise ValueError("runtime manifest install_dir must be a non-empty string")
    if not isinstance(required_files, list) or not all(
        isinstance(item, str) and item for item in required_files
    ):
        raise ValueError("runtime manifest required_files must be a string list")
    if not isinstance(assets, list):
        raise ValueError("runtime manifest assets must be a list")
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("filename"), str):
            raise ValueError("runtime manifest contains an invalid asset entry")
        _inside_root(runtime_root, asset["filename"], description="runtime asset")
    install_root = _inside_root(runtime_root, install_dir, description="runtime install")
    for filename in required_files:
        _inside_root(install_root, filename, description="runtime required file")
    executable_name = profile_executable.name
    if executable_name not in required_files:
        raise ValueError(f"runtime manifest does not pin {executable_name}")
    pinned = _inside_root(install_root, executable_name, description="llama-server")
    if pinned != profile_executable:
        raise ValueError("profile executable does not match the runtime manifest install pin")
    return pinned


def _platform_shape(
    runtime_platform: Any,
    executable_path: Path,
    *,
    system_name: str,
    is_wsl: bool,
) -> tuple[str | None, str | None]:
    if not isinstance(runtime_platform, str) or not runtime_platform:
        return None, "runtime manifest platform must be a non-empty string"
    lowered = runtime_platform.lower()
    is_windows_runtime = lowered.startswith("windows") and executable_path.suffix.lower() == ".exe"
    is_linux_runtime = lowered.startswith("linux") and executable_path.suffix.lower() != ".exe"
    if is_wsl and is_windows_runtime:
        return (
            None,
            "Refusing to launch Windows llama-server from WSL: WSL NAT cannot reliably reach "
            "the Windows child's localhost endpoint. Run Forge8 with Windows Python from "
            "PowerShell instead (for example: forge8 fix ...).",
        )
    if system_name == "Windows" and is_windows_runtime and not is_wsl:
        return "windows-native", None
    if system_name == "Linux" and is_linux_runtime:
        return "linux-native", None
    return (
        None,
        f"runtime platform {runtime_platform!r} is not native to {system_name}; "
        "use a pinned native runtime/profile",
    )


def _server_command(executable: Path, model: Path, profile: ServerProfile) -> tuple[str, ...]:
    # --fit off prevents an apparently healthy server from silently reducing
    # GPU offload. A deployment that cannot honor full offload must fail.
    command = [
        str(executable),
        "--model",
        str(model),
        "--host",
        profile.host,
        "--port",
        str(profile.port),
        "--ctx-size",
        str(profile.context_tokens),
        "--parallel",
        str(profile.parallel),
        "--cache-type-k",
        profile.kv_cache,
        "--cache-type-v",
        profile.kv_cache,
        "--flash-attn",
        profile.flash_attention,
        "--gpu-layers",
        "all",
        "--fit",
        "off",
        "--reasoning",
        profile.reasoning,
        "--reasoning-format",
        profile.reasoning_format,
        "--reasoning-budget",
        str(profile.reasoning_budget),
        "--cache-ram",
        "512",
        "--no-webui",
        "--cors-origins",
        "localhost",
        "--log-colors",
        "off",
    ]
    # The profile flags have already been reduced to a strict allow-list.
    command.extend(profile.flags)
    if profile.batch_size is not None:
        command.extend(("--batch-size", str(profile.batch_size), "--ubatch-size", str(profile.ubatch_size)))
    return tuple(command)


def prepare_server(
    workspace: Path,
    *,
    runtime_manifest_path: Path = Path("config/runtimes/llama_cpp_b10621.json"),
    model_manifest_path: Path = Path("config/models/gemma4_e4b_qat_q4.json"),
    profile_path: Path = Path("config/profiles/gemma4_e4b_text_8k.json"),
    runtime_root: Path = Path("runtime"),
    model_root: Path = Path("models"),
    system_name: str | None = None,
    is_wsl: bool | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> ServerPreparation:
    """Validate all pins; cancellation raises KeyboardInterrupt, never a partial plan."""

    runtime_integrity: IntegrityResult | None = None
    model_integrity: IntegrityResult | None = None
    try:
        if cancel_requested is not None and cancel_requested():
            raise KeyboardInterrupt("server preparation cancelled")
        workspace_path = workspace.expanduser().resolve(strict=True)
        if not workspace_path.is_dir():
            raise ValueError(f"workspace is not a directory: {workspace_path}")
        runtime_manifest_file = _inside_workspace(
            workspace_path,
            runtime_manifest_path,
            description="runtime manifest",
            must_exist=True,
        )
        model_manifest_file = _inside_workspace(
            workspace_path,
            model_manifest_path,
            description="model manifest",
            must_exist=True,
        )
        profile_file = _inside_workspace(
            workspace_path, profile_path, description="server profile", must_exist=True
        )
        runtime_root_path = _inside_workspace(
            workspace_path, runtime_root, description="runtime root", must_exist=True
        )
        model_root_path = _inside_workspace(
            workspace_path, model_root, description="model root", must_exist=True
        )
        profile = load_server_profile(profile_file)
        profile_runtime_manifest = _inside_workspace(
            workspace_path,
            profile.runtime_manifest,
            description="profile runtime manifest",
            must_exist=True,
        )
        profile_model_manifest = _inside_workspace(
            workspace_path,
            profile.model_manifest,
            description="profile model manifest",
            must_exist=True,
        )
        if profile_runtime_manifest != runtime_manifest_file:
            raise ValueError("supplied runtime manifest does not match the profile pin")
        if profile_model_manifest != model_manifest_file:
            raise ValueError("supplied model manifest does not match the profile pin")

        runtime_manifest = load_manifest(runtime_manifest_file)
        model_manifest = _read_json_object(model_manifest_file, "model manifest")
        profile_executable = _inside_workspace(
            workspace_path,
            profile.executable,
            description="profile executable",
            must_exist=False,
        )
        executable_path = _runtime_server_path(
            runtime_manifest, runtime_root_path, profile_executable
        )
        target_model = _target_model_path(model_manifest, model_root_path)
        profile_model = _inside_workspace(
            workspace_path, profile.model, description="profile model", must_exist=False
        )
        if target_model != profile_model:
            raise ValueError("model manifest target does not match the profile pin")

        detected_system = platform.system() if system_name is None else system_name
        detected_wsl = _detect_wsl() if is_wsl is None else is_wsl
        shape, platform_error = _platform_shape(
            runtime_manifest.get("platform"),
            executable_path,
            system_name=detected_system,
            is_wsl=detected_wsl,
        )
        if platform_error is not None:
            return ServerPreparation("unsupported_platform", (platform_error,))

        verification_options = {"cancel_requested": cancel_requested} if cancel_requested is not None else {}
        runtime_integrity = verify_runtime(runtime_manifest_file, runtime_root_path, **verification_options)
        model_integrity = verify_model(model_manifest_file, model_root_path, **verification_options)
        if cancel_requested is not None and cancel_requested():
            raise KeyboardInterrupt("server preparation cancelled")
        integrity_errors: list[str] = []
        if not runtime_integrity.ok:
            integrity_errors.append("runtime integrity verification failed")
        if not model_integrity.ok:
            integrity_errors.append("model integrity verification failed")
        if integrity_errors:
            return ServerPreparation(
                "integrity_failed",
                tuple(integrity_errors),
                runtime_integrity=runtime_integrity,
                model_integrity=model_integrity,
            )
        # Strict existence is checked only after integrity verification so a
        # missing pinned artifact is reported as integrity failure, not config.
        executable_path = executable_path.resolve(strict=True)
        target_model = target_model.resolve(strict=True)
        if shape == "linux-native" and not os.access(executable_path, os.X_OK):
            raise ValueError(
                f"Native Linux llama-server lacks execute permission: {executable_path}. "
                "Restore execute permission for your account on this verified file, "
                "or move the verified runtime to a filesystem that permits execution "
                "if the current mount is noexec. Forge8 did not change permissions."
            )
        plan = ServerPlan(
            workspace=workspace_path,
            profile=profile,
            executable_path=executable_path,
            model_path=target_model,
            command=_server_command(executable_path, target_model, profile),
            working_directory=executable_path.parent,
            endpoint=f"http://{profile.host}:{profile.port}",
            platform_shape=shape or "unsupported",
            runtime_manifest_path=runtime_manifest_file,
            model_manifest_path=model_manifest_file,
        )
        return ServerPreparation(
            "ready",
            (),
            plan=plan,
            runtime_integrity=runtime_integrity,
            model_integrity=model_integrity,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return ServerPreparation(
            "configuration_error",
            (str(exc),),
            runtime_integrity=runtime_integrity,
            model_integrity=model_integrity,
        )


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None:
        ...

    def wait(self, timeout: float | None = None) -> int:
        ...

    def terminate(self) -> None:
        ...

    def kill(self) -> None:
        ...


ProcessFactory = Callable[..., ProcessHandle]
HealthProbe = Callable[[str, str, float], tuple[bool, str | None]]
PortProbe = Callable[[str, int], bool]


def _port_is_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if os.name == "posix":
                # Match llama-server's TIME_WAIT reuse without sharing a live
                # listener. On Windows REUSEADDR can steal an occupied port.
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
        return True
    except OSError:
        return False


def _health_probe(endpoint: str, api_key: str, timeout_seconds: float) -> tuple[bool, str | None]:
    request = urllib.request.Request(
        f"{endpoint}/health",
        headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with _open_local_request(request, timeout=timeout_seconds) as response:
            raw = response.read(65_536)
    except urllib.error.HTTPError as exc:
        return False, f"health endpoint returned HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"health endpoint unavailable: {exc}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False, "health endpoint returned non-JSON data"
    if isinstance(payload, dict) and str(payload.get("status", "")).lower() == "ok":
        return True, None
    return False, f"health endpoint not ready: {payload!r}"[:512]


def _log_tail(path: Path | None, limit: int = _FAILURE_LOG_TAIL_BYTES) -> str:
    if path is None:
        return ""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit), os.SEEK_SET)
            return stream.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


@dataclass(frozen=True, slots=True)
class ServerStartResult:
    status: str
    started_at: str
    ended_at: str
    endpoint: str
    command: tuple[str, ...]
    pid: int | None
    return_code: int | None
    health_attempts: int
    health_last_error: str | None
    stdout_log: str | None
    stderr_log: str | None
    stdout_tail: str
    stderr_tail: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ready"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "endpoint": self.endpoint,
            "command": list(self.command),
            "pid": self.pid,
            "return_code": self.return_code,
            "health_attempts": self.health_attempts,
            "health_last_error": self.health_last_error,
            "stdout_log": self.stdout_log,
            "stderr_log": self.stderr_log,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "error": self.error,
            "authentication": {
                "enabled": True,
                "secret_in_command": False,
                "secret_in_result": False,
            },
        }


@dataclass(frozen=True, slots=True)
class ServerShutdownResult:
    status: str
    return_code: int | None
    terminate_sent: bool
    kill_sent: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        if self.status == "not_started":
            return True
        return (
            self.status in {"already_exited", "terminated", "killed"}
            and self.return_code is not None
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "return_code": self.return_code,
            "terminate_sent": self.terminate_sent,
            "kill_sent": self.kill_sent,
            "error": self.error,
        }


class LocalServerSupervisor:
    """Own one llama-server process and its secret/log lifecycle.

    The caller may explicitly authorize a separate log root for its own run.
    Runtime and model paths retain the verified plan's workspace boundary.
    """

    def __init__(
        self,
        plan: ServerPlan,
        *,
        log_directory: Path = Path("logs/server"),
        log_root: Path | None = None,
        startup_timeout_seconds: float = 120.0,
        health_interval_seconds: float = 0.25,
        health_request_timeout_seconds: float = 1.0,
        shutdown_timeout_seconds: float = 5.0,
        kill_timeout_seconds: float = 2.0,
        process_factory: ProcessFactory = subprocess.Popen,
        health_probe: HealthProbe = _health_probe,
        port_probe: PortProbe = _port_is_available,
        api_key_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        for name, value in (
            ("startup_timeout_seconds", startup_timeout_seconds),
            ("health_interval_seconds", health_interval_seconds),
            ("health_request_timeout_seconds", health_request_timeout_seconds),
            ("shutdown_timeout_seconds", shutdown_timeout_seconds),
            ("kill_timeout_seconds", kill_timeout_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        self.plan = plan
        log_workspace = plan.workspace
        if log_root is not None:
            supplied_root = Path(log_root)
            if not supplied_root.is_absolute():
                raise ValueError("server log root must be an absolute directory")
            try:
                metadata = supplied_root.lstat()
                resolved_root = supplied_root.resolve(strict=True)
            except (OSError, ValueError) as exc:
                raise ValueError(f"cannot inspect server log root: {supplied_root}") from exc
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & reparse_flag
            ):
                raise ValueError("server log root must be a regular directory, not a link or reparse point")
            if resolved_root == Path(resolved_root.anchor):
                raise ValueError("server log root cannot be a filesystem root")
            log_workspace = resolved_root
        self.log_directory = _inside_workspace(
            log_workspace,
            log_directory,
            description="server log directory",
            must_exist=False,
        )
        self.startup_timeout_seconds = float(startup_timeout_seconds)
        self.health_interval_seconds = float(health_interval_seconds)
        self.health_request_timeout_seconds = float(health_request_timeout_seconds)
        self.shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self.kill_timeout_seconds = float(kill_timeout_seconds)
        self._process_factory = process_factory
        self._health_probe = health_probe
        self._port_probe = port_probe
        self._api_key_factory = api_key_factory
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._cancel_requested = cancel_requested
        self._process: ProcessHandle | None = None
        self._api_key: str | None = None
        self._stdout_stream: Any = None
        self._stderr_stream: Any = None
        self._stdout_path: Path | None = None
        self._stderr_path: Path | None = None
        self.start_result: ServerStartResult | None = None
        self.shutdown_result: ServerShutdownResult | None = None

    @property
    def api_key(self) -> str | None:
        """Return the in-memory run secret for direct application handoff."""

        return self._api_key

    def _open_logs(self) -> None:
        self.log_directory.mkdir(parents=True, exist_ok=True)
        run_id = secrets.token_hex(8)
        self._stdout_path = self.log_directory / f"llama-server-{run_id}.stdout.log"
        self._stderr_path = self.log_directory / f"llama-server-{run_id}.stderr.log"
        stdout_fd = os.open(self._stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            stderr_fd = os.open(self._stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except BaseException:
            os.close(stdout_fd)
            self._stdout_path.unlink(missing_ok=True)
            raise
        self._stdout_stream = os.fdopen(stdout_fd, "wb", buffering=0)
        self._stderr_stream = os.fdopen(stderr_fd, "wb", buffering=0)

    def _close_logs(self) -> None:
        for stream in (self._stdout_stream, self._stderr_stream):
            if stream is not None and not stream.closed:
                stream.close()

    def _tails(self) -> tuple[str, str]:
        for stream in (self._stdout_stream, self._stderr_stream):
            if stream is not None and not stream.closed:
                stream.flush()
        return _log_tail(self._stdout_path), _log_tail(self._stderr_path)

    def _redact(self, value: str | None) -> str | None:
        if value is None or self._api_key is None:
            return value
        return value.replace(self._api_key, "[REDACTED]")

    def _result(
        self,
        status: str,
        *,
        started_at: str,
        attempts: int,
        health_error: str | None = None,
        error: str | None = None,
    ) -> ServerStartResult:
        stdout_tail, stderr_tail = self._tails()
        stdout_tail = self._redact(stdout_tail) or ""
        stderr_tail = self._redact(stderr_tail) or ""
        return_code = None if self._process is None else self._process.poll()
        result = ServerStartResult(
            status=status,
            started_at=started_at,
            ended_at=_utc_now(),
            endpoint=self.plan.endpoint,
            command=self.plan.command,
            pid=None if self._process is None else self._process.pid,
            return_code=return_code,
            health_attempts=attempts,
            health_last_error=self._redact(health_error),
            stdout_log=None if self._stdout_path is None else str(self._stdout_path),
            stderr_log=None if self._stderr_path is None else str(self._stderr_path),
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            error=self._redact(error),
        )
        self.start_result = result
        return result

    def start(self) -> ServerStartResult:
        if self.start_result is not None:
            return self.start_result
        if self._process is not None:
            raise RuntimeError("server process exists without a start result")
        started_at = _utc_now()

        def cancelled(attempts: int, health_error: str | None = None) -> ServerStartResult | None:
            if self._cancel_requested is None or not self._cancel_requested():
                return None
            # Capture and redact the launch evidence before stop clears the
            # secret. Only this startup thread stops its owned child.
            result = self._result(
                "interrupted", started_at=started_at, attempts=attempts,
                health_error=health_error, error="server startup cancelled",
            )
            self.stop()
            return result

        if (result := cancelled(0)) is not None:
            return result
        if not self._port_probe(self.plan.profile.host, self.plan.profile.port):
            return self._result(
                "port_collision",
                started_at=started_at,
                attempts=0,
                error=f"port {self.plan.profile.host}:{self.plan.profile.port} is already in use",
            )

        self._api_key = self._api_key_factory()
        if not self._api_key or not isinstance(self._api_key, str):
            self._api_key = None
            return self._result(
                "launch_error",
                started_at=started_at,
                attempts=0,
                error="API key factory returned an invalid secret",
            )
        try:
            self._open_logs()
            environment = os.environ.copy()
            environment["LLAMA_API_KEY"] = self._api_key
            if (result := cancelled(0)) is not None:
                return result
            self._process = self._process_factory(
                list(self.plan.command),
                cwd=str(self.plan.working_directory),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=self._stdout_stream,
                stderr=self._stderr_stream,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            result = self._result(
                "launch_error",
                started_at=started_at,
                attempts=0,
                error=f"{type(exc).__name__}: {exc}",
            )
            self._api_key = None
            self._close_logs()
            return result

        deadline = self._monotonic() + self.startup_timeout_seconds
        attempts = 0
        health_error: str | None = None
        while True:
            if (result := cancelled(attempts, health_error)) is not None:
                return result
            return_code = self._process.poll()
            if return_code is not None:
                stdout_tail, stderr_tail = self._tails()
                combined = f"{stdout_tail}\n{stderr_tail}".lower()
                status_name = (
                    "port_collision"
                    if any(
                        marker in combined
                        for marker in ("address already in use", "wsaeaddrinuse", "bind failed")
                    )
                    else "early_exit"
                )
                result = self._result(
                    status_name,
                    started_at=started_at,
                    attempts=attempts,
                    health_error=health_error,
                    error=f"llama-server exited before readiness with code {return_code}",
                )
                self._api_key = None
                self._close_logs()
                return result

            now = self._monotonic()
            if now >= deadline:
                result = self._result(
                    "startup_timeout",
                    started_at=started_at,
                    attempts=attempts,
                    health_error=health_error,
                    error=f"health endpoint was not ready within {self.startup_timeout_seconds:g}s",
                )
                self.stop()
                return result

            attempts += 1
            request_timeout = min(self.health_request_timeout_seconds, max(0.001, deadline - now))
            ready, health_error = self._health_probe(
                self.plan.endpoint, self._api_key, request_timeout
            )
            if (result := cancelled(attempts, health_error)) is not None:
                return result
            if ready:
                return self._result("ready", started_at=started_at, attempts=attempts)
            remaining = deadline - self._monotonic()
            if remaining > 0:
                self._sleeper(min(self.health_interval_seconds, remaining))

    def stop(self) -> ServerShutdownResult:
        if self.shutdown_result is not None:
            return self.shutdown_result
        if self._process is None:
            self._api_key = None
            self._close_logs()
            result = ServerShutdownResult("not_started", None, False, False)
            self.shutdown_result = result
            return result

        process = self._process
        errors: list[str] = []

        def record_error(operation: str, exc: BaseException) -> None:
            errors.append(f"{operation}: {type(exc).__name__}: {exc}")

        def poll(operation: str) -> int | None:
            try:
                return process.poll()
            except OSError as exc:
                record_error(operation, exc)
                return None

        def finish(
            status: str,
            return_code: int | None,
            terminate_sent: bool,
            kill_sent: bool,
        ) -> ServerShutdownResult:
            # A reclaimed status is evidence-bearing only when wait/poll proved
            # an exit code.  Keep every process-API failure as redacted machine
            # evidence even when a later kill successfully reaps the process.
            if status in {"already_exited", "terminated", "killed"} and return_code is None:
                status = "shutdown_error"
                errors.append("shutdown did not produce a process exit code")
            secret = self._api_key

            def shutdown_result() -> ServerShutdownResult:
                error = "; ".join(errors) if errors else None
                if error is not None and secret is not None:
                    error = error.replace(secret, "[REDACTED]")
                return ServerShutdownResult(
                    status,
                    return_code,
                    terminate_sent,
                    kill_sent,
                    error,
                )

            # Publish a terminal result and clear the secret before touching
            # fallible log streams.  A close failure can then never make a
            # repeated stop() send process signals again.
            self._api_key = None
            self.shutdown_result = shutdown_result()
            for label, stream in (
                ("stdout log close failed", self._stdout_stream),
                ("stderr log close failed", self._stderr_stream),
            ):
                try:
                    if stream is not None and not stream.closed:
                        stream.close()
                except Exception as exc:
                    record_error(label, exc)
            self.shutdown_result = shutdown_result()
            return self.shutdown_result

        return_code = poll("initial poll failed")
        if return_code is not None:
            return finish("already_exited", return_code, False, False)

        terminate_sent = False
        kill_sent = False
        try:
            process.terminate()
            terminate_sent = True
        except OSError as exc:
            record_error("terminate failed", exc)

        if terminate_sent:
            try:
                return_code = process.wait(timeout=self.shutdown_timeout_seconds)
            except subprocess.TimeoutExpired:
                return_code = None
            except OSError as exc:
                record_error("wait after terminate failed", exc)
                return_code = None
            if return_code is not None:
                return finish("terminated", return_code, terminate_sent, kill_sent)

        # A terminate API error, graceful-wait API error, timeout, or a wait
        # without an exit code all take the same bounded escalation path.  Even
        # when kill() itself errors, wait once more: the signal may have raced
        # with process exit or have been delivered before the API reported it.
        try:
            process.kill()
            kill_sent = True
        except OSError as exc:
            record_error("kill failed", exc)

        kill_wait_timed_out = False
        try:
            return_code = process.wait(timeout=self.kill_timeout_seconds)
        except subprocess.TimeoutExpired:
            kill_wait_timed_out = True
            return_code = poll("poll after bounded kill wait failed")
        except OSError as exc:
            record_error("wait after kill failed", exc)
            return_code = poll("poll after kill wait failure failed")
        else:
            if return_code is None:
                errors.append("wait after kill returned no process exit code")
                return_code = poll("poll after empty kill wait failed")

        if return_code is not None:
            status_name = "killed" if kill_sent else "already_exited"
        elif kill_wait_timed_out:
            status_name = "kill_timeout"
            errors.append("process survived bounded kill wait")
        else:
            status_name = "shutdown_error"
        return finish(status_name, return_code, terminate_sent, kill_sent)

    def __enter__(self) -> "LocalServerSupervisor":
        try:
            self.start()
        except BaseException:
            self.stop()
            raise
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.stop()
