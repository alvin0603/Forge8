"""Fixed native WASI worker. Never import supplied Python into the host.

Called only by the explicit experiment controller after runtime verification.
Guest output is untrusted data, not a verification of any explanation.
"""
from __future__ import annotations

import ast
import hashlib
import json
import keyword
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


def consume_generator(result, limit):
    """Advance at most limit times, snapshot yields, then explicitly close.

    Invoke only inside the existing bounded WASI guest. This function supplies
    no independent timeout or sandbox and does not serialize arbitrary objects.
    """
    import json
    import types

    if type(limit) is not int or not 1 <= limit <= 12:
        raise ValueError("generator limit must be an integer from 1 to 12")
    report = {"limit": limit, "next_calls": 0, "yields": [],
              "iteration": {"status": "not_started"},
              "serialization": {"status": "not_attempted"},
              "close": {"status": "not_attempted"}}
    if type(result) is not types.GeneratorType:
        report["iteration"] = {"status": "type_error", "exception": "TypeError"}
        return {"generator": report}

    def exception_name(error):
        return type(error).__name__[:80]

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate normalized JSON object key")
            value[key] = item
        return value

    remaining = 60 * 1024  # Leave room for the envelope within outer capture.

    def snapshot(value, phase, index=None):
        nonlocal remaining
        try:
            encoded = json.dumps(value, ensure_ascii=True, allow_nan=False,
                                 separators=(",", ":"))
            if len(encoded) > remaining:
                raise ValueError("generator retained JSON exceeds 60 KiB")
            detached = json.loads(encoded, object_pairs_hook=unique_object)
        except BaseException as error:
            report["serialization"] = {"status": "exception", "phase": phase,
                                       "exception": exception_name(error)}
            if index is not None:
                report["serialization"]["yield_index"] = index
            return False, None
        remaining -= len(encoded)
        report["serialization"] = {"status": "ok"}
        return True, detached

    try:
        for _ in range(limit):
            report["next_calls"] += 1
            try:
                value = next(result)
            except StopIteration as stopped:
                report["iteration"] = {"status": "exhausted"}
                ok, detached = snapshot(stopped.value, "return")
                if ok:
                    report["return_value"] = detached
                break
            except BaseException as error:
                report["iteration"] = {"status": "exception",
                                       "exception": exception_name(error)}
                break
            ok, detached = snapshot(value, "yield", len(report["yields"]) + 1)
            if not ok:
                report["iteration"] = {"status": "stopped_for_serialization"}
                break
            report["yields"].append(detached)
        else:
            report["iteration"] = {"status": "limit_reached"}
    finally:
        try:
            result.close()
        except BaseException as error:
            report["close"] = {"status": "exception",
                               "exception": exception_name(error)}
        else:
            report["close"] = {"status": "closed"}
    return {"generator": report}


request = json.loads(sys.argv[1])
trace_enabled = request.get("trace_lines", False)
trace_report = None
watch_names = request.get("watch_names", [])
if watch_names:
    import math
    watch_type, watch_id, watch_len = type, id, len
    watch_types = (type(None), bool, int, float, str, list, dict)
    watch_dump, watch_finite = json.dumps, math.isfinite
    watch_function = types.FunctionType

    class WatchUnsupported(Exception):
        pass

    class WatchLimited(Exception):
        pass

    def watch_snapshot(value):
        # Emit detached JSON text, never repr, object attributes or user hooks.
        nodes, remaining, active = 0, 2048, set()

        def encode(item, depth=0):
            nonlocal nodes, remaining
            nodes += 1
            if nodes > 128:
                raise WatchLimited
            kind = watch_type(item)
            if not any(kind is allowed for allowed in watch_types):
                raise WatchUnsupported
            if kind is float and not watch_finite(item):
                raise WatchUnsupported
            if ((kind is str and watch_len(item) > 2048)
                    or (kind is int and int.bit_length(item) > 7000)):
                raise WatchLimited
            if kind is list or kind is dict:
                if depth >= 6 or watch_len(item) > 128:
                    raise WatchLimited
                identity = watch_id(item)
                if identity in active:
                    raise WatchUnsupported
                active.add(identity)
                remaining -= 2 + max(0, watch_len(item) - 1)
                if remaining < 0:
                    raise WatchLimited
                if kind is dict:
                    keys = list(item)
                    if any(watch_type(key) is not str for key in keys):
                        raise WatchUnsupported
                    keys.sort()
                    remaining -= watch_len(keys)
                    pieces = [encode(key, depth + 1) + ":" + encode(item[key], depth + 1) for key in keys]
                    text = "{" + ",".join(pieces) + "}"
                else:
                    text = "[" + ",".join(encode(part, depth + 1) for part in item) + "]"
                active.remove(identity)
                return text
            text = watch_dump(item, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            remaining -= watch_len(text)
            if remaining < 0:
                raise WatchLimited
            return text

        try:
            return {"state": "value", "json": encode(value)}
        except WatchUnsupported:
            return {"state": "unsupported"}
        except WatchLimited:
            return {"state": "limited"}

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
        if watch_names:
            selected = [code for code in compiled.co_consts
                        if watch_type(code) is types.CodeType and code.co_name == request["entry"]]
            if len(selected) != 1:
                raise ValueError("watched entry has no unique original code object")
            watch_code = selected[0]
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
        if watch_names:
            selected = scope[request["entry"]]
            if (watch_type(selected) is not watch_function or selected.__code__ is not watch_code
                    or watch_code.co_flags & (0x20 | 0x80 | 0x200)
                    or any(name not in watch_code.co_varnames + watch_code.co_cellvars for name in watch_names)):
                raise ValueError("watching requires the original synchronous entry and selected local names")
        trace_report = {"line_events": [], "truncated": False, "hook_intact": False}
        trace_active = True
        if watch_names:
            watch = {"names": watch_names, "events": [], "truncated": False}
            trace_report["watch"] = watch
            watch_frames, watch_next = {}, 0
            watch_size = len(watch_dump(watch, ensure_ascii=True, separators=(",", ":")))

            def watch_event(frame, event):
                global watch_next, watch_size
                if frame.f_code is not watch_code or watch["truncated"]:
                    return
                frame_id = watch_id(frame)
                if event == "call":
                    watch_next += 1
                    if watch_next > 128:
                        watch["truncated"] = True
                    else:
                        watch_frames[frame_id] = watch_next
                    return
                if event not in ("line", "return", "exception") or frame_id not in watch_frames:
                    return
                values, local = {}, frame.f_locals
                for name in watch_names:
                    try:
                        value = local[name]
                    except KeyError:
                        values[name] = {"state": "unbound"}
                    else:
                        values[name] = watch_snapshot(value)
                observation = {"event": event, "line": frame.f_lineno,
                               "call_id": watch_frames[frame_id], "values": values}
                size = len(watch_dump(observation, ensure_ascii=True, separators=(",", ":"))) + bool(watch["events"])
                if len(watch["events"]) >= 128 or watch_size + size > 24 * 1024:
                    watch["truncated"] = True
                else:
                    watch["events"].append(observation)
                    watch_size += size
                if event == "return":
                    del watch_frames[frame_id]

        def trace_call(frame, event, arg):
            # Code objects compare structurally: use identities, not equality
            # or a guest-controlled filename. Do not retain frame/arg/locals.
            if not trace_active or id(frame.f_code) not in trace_ids:
                return None
            if watch_names:
                watch_event(frame, event)
            if event == "line":
                if len(trace_report["line_events"]) < 1000:
                    trace_report["line_events"].append(frame.f_lineno)
                else:
                    trace_report["truncated"] = True
            return trace_call

        try:
            trace_set(trace_call)
            result = (selected if watch_names else scope[request["entry"]])(*request["input"]["args"], **request["input"]["kwargs"])
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
    encoded_result = json.dumps(
        consume_generator(result, request["generator_steps"])
        if "generator_steps" in request else {"return": result},
        ensure_ascii=True, allow_nan=False,
        **({"separators": (",", ":")} if "generator_steps" in request else {}))
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


def _validate_generator_steps(value) -> None:
    if type(value) is not int or not 1 <= value <= 12:
        raise ValueError("generator_steps must be an integer from 1 to 12")


def _validate_watch_names(value: tuple[str, ...]) -> None:
    if (type(value) is not tuple or len(value) > 3
            or any(type(name) is not str or not 1 <= len(name) <= 64
                   or not name.isascii() or not name.isidentifier() or keyword.iskeyword(name) for name in value)
            or len(set(value)) != len(value)):
        raise ValueError("watch_names must be up to three unique ASCII local names, each at most 64 characters")


def _validate_watch_source(source: str, entry: str) -> None:
    selected = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == entry]
    if len(selected) != 1 or selected[0].decorator_list:
        raise ValueError("watching requires one undecorated synchronous top-level function")


def execute(root: Path, request: dict, cache_pin: dict, *,
            module_bundle: dict | None = None, request_path: Path | None = None) -> dict:
    trace_lines = request.get("trace_lines", False)
    if type(trace_lines) is not bool:
        raise ValueError("trace_lines must be a boolean")
    if trace_lines and (module_bundle is not None or "module_bundle" in request):
        raise ValueError("line tracing supports single-file experiments only")
    if "generator_steps" in request:
        _validate_generator_steps(request["generator_steps"])
        if trace_lines or module_bundle is not None or "module_bundle" in request:
            raise ValueError("generator consumption requires an untraced single-file trial")
    if "watch_names" in request:
        names = request["watch_names"]
        if (type(names) is not list or not names or not trace_lines or "generator_steps" in request
                or module_bundle is not None or "module_bundle" in request):
            raise ValueError("watching requires nonempty names and a single-file line trace")
        _validate_watch_names(tuple(names))
        _validate_watch_source(request["source"], request["entry"])
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
