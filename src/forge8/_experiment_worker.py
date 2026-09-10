"""Fixed native WASI worker. Never import supplied Python into the host.

Called only by the explicit experiment controller after runtime verification.
Guest output is untrusted data, not a verification of any explanation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time


ENGINE_OPTIONS = {
    "epoch_interruption": True,
    "memory_reservation": 0,
    "memory_reservation_for_growth": 0,
    "memory_guard_size": 65_536,
    "max_wasm_stack": 524_288,
    "parallel_compilation": False,
    "cranelift_opt_level": "speed",
}
OUTPUT_BYTES = 65_536
GUEST_MEMORY_BYTES = 128 * 1024**2
GUEST_SECONDS = 5.0
GUEST_BOOTSTRAP = r'''
import json, sys, types
request = json.loads(sys.argv[1])
trace_enabled = request.get("trace_lines", False)
trace_report = None
if trace_enabled:
    # Keep original hooks and strong references before module initialization.
    # This is diagnostic guest reporting, not protection from guest tampering.
    trace_set, trace_get = sys.settrace, sys.gettrace
    trace_codes, trace_ids = [], set()
if "module_bundle" not in request:
    module = types.ModuleType("forge8_experiment")
    module.__file__ = "experiment.py"
    sys.modules[module.__name__] = module
    scope = module.__dict__
phase = "module_initialization"
try:
    if "module_bundle" in request:
        import importlib.util
        bundle = request["module_bundle"]
        reserved = (sys.stdlib_module_names | set(sys.builtin_module_names)
                    | {name.partition(".")[0] for name in sys.modules})
        # Frozen test modules are not all in stdlib_module_names. Resolve only
        # top-level names BEFORE exposing project bytes; no selected code runs.
        if any(name in reserved or importlib.util.find_spec(name) is not None
               for name in bundle["top_levels"]):
            raise ImportError("Selected module name conflicts with the interpreter")
        sys.path.append("/modules/selected.zip")
        module = importlib.import_module(bundle["entry_module"])
        scope = module.__dict__
    else:
        compiled = compile(request["source"], "experiment.py", "exec")
        if trace_enabled:
            pending = [compiled]
            while pending:
                code = pending.pop()
                trace_codes.append(code)
                trace_ids.add(id(code))
                pending.extend(item for item in code.co_consts if type(item) is types.CodeType)
        exec(compiled, scope)
    phase = "call"
    if trace_enabled:
        trace_report = {"line_events": [], "truncated": False, "hook_intact": False}
        trace_active = True

        def trace_call(frame, event, arg):
            # Code objects compare structurally: use identities, not equality
            # or a guest-controlled filename. Do not retain frame/arg/locals.
            if not trace_active or id(frame.f_code) not in trace_ids:
                return None
            if event == "line":
                if len(trace_report["line_events"]) < 1000:
                    trace_report["line_events"].append(frame.f_lineno)
                else:
                    trace_report["truncated"] = True
            return trace_call

        try:
            trace_set(trace_call)
            result = scope[request["entry"]](*request["input"]["args"], **request["input"]["kwargs"])
        finally:
            # Even an audit hook refusing settrace(None) must not extend
            # capture into exception formatting or result serialization.
            trace_active = False
            try:
                trace_report["hook_intact"] = trace_get() is trace_call
            finally:
                trace_set(None)
    else:
        result = scope[request["entry"]](*request["input"]["args"], **request["input"]["kwargs"])
    phase = "serialization"
    encoded_result = json.dumps({"return": result}, ensure_ascii=True, allow_nan=False)
except BaseException as error:
    encoded_result = json.dumps({"exception": type(error).__name__, "message": str(error), "phase": phase}, ensure_ascii=True)
if trace_report is not None:
    print("\nFORGE8_GUEST_TRACE=" + json.dumps(trace_report, ensure_ascii=True, allow_nan=False))
print("\nFORGE8_GUEST_RESULT=" + encoded_result)
'''


def _module_bundle_directory(request_path: Path | None, bundle: dict) -> Path:
    """Admit only the controller's adjacent, hash-bound archive, never a supplied mount path."""
    if (type(bundle) is not dict or set(bundle) != {"entry_module", "top_levels", "sha256", "size_bytes"}
            or type(bundle["entry_module"]) is not str
            or not all(part.isascii() and part.isidentifier() for part in bundle["entry_module"].split("."))
            or type(bundle["top_levels"]) is not list or not 1 <= len(bundle["top_levels"]) <= 4
            or not all(type(name) is str and name.isascii() and name.isidentifier() for name in bundle["top_levels"])
            or bundle["top_levels"] != sorted(set(bundle["top_levels"]))
            or bundle["entry_module"].partition(".")[0] not in bundle["top_levels"]
            or type(bundle["size_bytes"]) is not int or not 0 < bundle["size_bytes"] <= 128 * 1024
            or type(bundle["sha256"]) is not str or len(bundle["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in bundle["sha256"])):
        raise ValueError("invalid selected module bundle")
    if request_path is None or not request_path.is_absolute():
        raise ValueError("module bundle requires the actual absolute worker request path")
    directory = request_path.parent / "modules"
    archive = directory / "selected.zip"
    for path in (request_path, archive):
        if path.resolve(strict=True) != path:
            raise ValueError("module bundle paths must not be redirected")
    for path in (directory, *directory.parents):
        metadata = path.lstat()
        if (not stat.S_ISDIR(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & 0x400):
            raise ValueError("module bundle parents must be ordinary directories")
    for path in (request_path, archive):
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or getattr(metadata, "st_file_attributes", 0) & 0x400):
            raise ValueError("module bundle inputs must be regular single-link files")
    if {path.name for path in directory.iterdir()} != {"selected.zip"}:
        raise ValueError("module bundle directory must contain only selected.zip")
    before = archive.lstat()
    if before.st_size != bundle["size_bytes"]:
        raise ValueError("selected module archive size changed")
    # Windows 3.12's lstat/fstat ctime fields may have different meanings.
    # Bind by birth time there, but retain each API's independent ctime check.
    timestamp = "st_birthtime_ns" if os.name == "nt" and hasattr(before, "st_birthtime_ns") else "st_ctime_ns"
    fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", timestamp)
    expected = tuple(getattr(before, field) for field in fields)
    with archive.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if tuple(getattr(opened, field) for field in fields) != expected:
            raise ValueError("selected module archive changed before reading")
        data = handle.read(128 * 1024 + 1)
        after = os.fstat(handle.fileno())
    current = archive.lstat()
    if (len(data) != bundle["size_bytes"] or hashlib.sha256(data).hexdigest() != bundle["sha256"]
            or tuple(getattr(after, field) for field in fields) != expected
            or tuple(getattr(current, field) for field in fields) != expected
            or after.st_ctime_ns != opened.st_ctime_ns
            or current.st_ctime_ns != before.st_ctime_ns):
        raise ValueError("selected module archive integrity failed")
    return directory


def _runtime(root: Path, *, building: bool):
    # Windows limits are applied by the parent Job before this process resumes.
    if os.name != "nt":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
        cpu = 60 if building else 12
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    else:
        # Only x86-64 hosts are admitted by the controller. Python <=3.12 needs
        # this host-only variable for platform.machine; it is NOT passed to WASI.
        os.environ["PROCESSOR_ARCHITECTURE"] = "AMD64"
    sys.path.insert(0, str(root / "installed/host"))
    import wasmtime
    config = wasmtime.Config()
    for key, value in ENGINE_OPTIONS.items():
        setattr(config, key, value)
    return wasmtime, config


def compile_runtime(root: Path) -> dict:
    wasmtime, config = _runtime(root, building=True)
    started = time.monotonic()
    with wasmtime.Engine(config) as engine, wasmtime.Module.from_file(
        engine, str(root / "installed/guest/python.wasm")
    ) as module:
        if any(item.module != "wasi_snapshot_preview1" for item in module.imports):
            raise ValueError("interpreter imports unsupported host capabilities")
        data = module.serialize()
        if len(data) > 64 * 1024**2:
            raise ValueError("compiled interpreter exceeds 64 MiB")
        with (root / "installed/python.cwasm").open("xb") as handle:
            handle.write(data)
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "compile_seconds": time.monotonic() - started}


def execute(root: Path, request: dict, cache_pin: dict, *,
            module_bundle: dict | None = None, request_path: Path | None = None) -> dict:
    trace_lines = request.get("trace_lines", False)
    if type(trace_lines) is not bool:
        raise ValueError("trace_lines must be a boolean")
    if trace_lines and (module_bundle is not None or "module_bundle" in request):
        raise ValueError("line tracing supports single-file experiments only")
    modules = None if module_bundle is None else _module_bundle_directory(request_path, module_bundle)
    if modules is not None:
        request = {**request, "module_bundle": {key: module_bundle[key] for key in ("entry_module", "top_levels")}}
    wasmtime, config = _runtime(root, building=False)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    exceeded = False
    started = time.monotonic()
    with wasmtime.Engine(config) as engine:
        def capture(stream: str, data: bytes) -> int:
            nonlocal exceeded
            available = OUTPUT_BYTES - sum(map(len, output.values()))
            output[stream].extend(data[:available])
            if len(data) > available:
                exceeded = True
                engine.increment_epoch()
                return -27  # EFBIG. Never raise/log private guest bytes in callback.
            return len(data)

        with (root / "installed/python.cwasm").open("rb") as handle:
            data = handle.read(64 * 1024**2 + 1)
        if (len(data) > 64 * 1024**2 or len(data) != cache_pin["size_bytes"]
                or hashlib.sha256(data).hexdigest() != cache_pin["sha256"]):
            raise ValueError("compiled interpreter changed before loading")
        # deserialize is a native-code trust boundary: only our verified local
        # setup output, never a project/model/guest-supplied serialized module.
        with wasmtime.Module.deserialize(engine, data) as module:
            if any(item.module != "wasi_snapshot_preview1" for item in module.imports):
                raise ValueError("interpreter imports unsupported host capabilities")
            loaded = time.monotonic() - started
            with wasmtime.Store(engine) as store, wasmtime.Linker(engine) as linker:
                store.set_limits(memory_size=GUEST_MEMORY_BYTES, table_elements=100_000,
                                 instances=1, memories=1, tables=1)
                store.set_epoch_deadline(1)
                wasi = wasmtime.WasiConfig()
                wasi.argv = ["python", "-I", "-S", "-B", "-c", GUEST_BOOTSTRAP, json.dumps(request)]
                wasi.env = []
                wasi.preopen_dir(str(root / "installed/guest/lib"), "/lib", fs_mutable=False)
                if modules is not None:
                    _module_bundle_directory(request_path, module_bundle)
                    wasi.preopen_dir(str(modules), "/modules", fs_mutable=False)
                wasi.stdout_custom = lambda data: capture("stdout", data)
                wasi.stderr_custom = lambda data: capture("stderr", data)
                store.set_wasi(wasi)
                linker.define_wasi()
                status, detail = "exited", None
                timer = threading.Timer(GUEST_SECONDS, engine.increment_epoch)
                timer.start()
                try:
                    instance = linker.instantiate(store, module)
                    instance.exports(store)["_start"](store)
                except wasmtime.ExitTrap as exc:
                    status, detail = "guest_exit", exc.code
                except wasmtime.Trap as exc:
                    status, detail = "trap", str(exc.trap_code)
                finally:
                    timer.cancel()
                    timer.join()
    return {"host_status": status, "detail": detail, "output_limit": exceeded,
            "load_seconds": loaded, "worker_seconds": time.monotonic() - started,
            "guest_output": {key: bytes(value).decode("utf-8", "replace") for key, value in output.items()}}


def main() -> None:
    operation, directory, request_path = sys.argv[1:]
    root = Path(directory)
    if operation == "compile":
        result = compile_runtime(root)
    elif operation == "run":
        with Path(request_path).open("rb") as handle:
            raw = handle.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ValueError("worker input exceeds limit")
        payload = json.loads(raw)
        options = {}
        if "module_bundle" in payload:
            if type(payload["module_bundle"]) is not dict:
                raise ValueError("invalid selected module bundle")
            options = {"module_bundle": payload["module_bundle"], "request_path": Path(request_path)}
        result = execute(root, payload["request"], payload["cache_pin"], **options)
    else:
        raise ValueError("unknown experiment operation")
    print(json.dumps(result, ensure_ascii=True, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
