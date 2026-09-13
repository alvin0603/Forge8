"""Explicit, offline bounded Python experiments; never a model tool.

Optional assets are installed from two operator-provided pinned archives. The
existing runtime verifier and process controller provide integrity and cleanup.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import sys
import time
import tokenize
import zipfile
from copy import deepcopy
from typing import Callable

from ._experiment_worker import ENGINE_OPTIONS, OUTPUT_BYTES
from .checks import CheckDefinition, CheckRunner
from .repository import _read_regular_file, _strict_existing_directory
from .runtime import (_artifact_path, _check_cancel, _installed_inventory, _is_reparse_point, _relative_manifest_path,
                      _strict_json_object, _reject_json_constant, load_manifest,
                      sha256_file, verify_runtime)
from .workspace import ArtifactStore, WorkspacePolicy


PYTHON_ARCHIVE = ("python-3.14.7-wasi_sdk-24.zip", 14_291_017,
    "2e064d3fb8172471d39d741348efa722349c40b96301f69968dff714999c584b")
WHEELS = {
    "linux": ("wasmtime-48.0.0-py3-none-manylinux1_x86_64.whl", 9_796_354,
        "58544d539053dff7bd4cf30c40d7a540862d683013c0dfa6ba46a063f5b682f7"),
    "windows": ("wasmtime-48.0.0-py3-none-win_amd64.whl", 8_157_931,
        "21fa500e70f3819a8c0539c3f0be6b3b81ec3c630bb90c47dba4d8a2c1d4c698"),
}
MAX_SOURCE_BYTES = 65_536
MAX_INPUT_BYTES = 16_384
MAX_MODULE_FILES = 4
MAX_MODULE_BUNDLE_BYTES = 131_072
_MAX_CALL_SOURCE_BYTES = 256 * 1024
_MAX_CALL_AST_NODES = 50_000
_MAX_CALL_LITERAL_NODES = 1024
_MAX_CALL_LITERAL_DEPTH = 24
RUNTIME_NAME = "python314-wasmtime48-zip1"
REQUIRED_FILES = ["guest/python.wasm", "guest/lib/python314.zip", "python.cwasm"]


def native_platform() -> str:
    system = platform.system().lower()
    if system not in WHEELS or platform.machine().lower() not in {"amd64", "x86_64"} or sys.maxsize <= 2**32:
        raise ValueError("experiments support native x86-64 Windows and Linux only")
    return system


def _metadata() -> dict:
    return {"protocol": 1, "platform": native_platform(), "python": "3.14.7",
            "wasmtime": "48.0.0", "engine_options": ENGINE_OPTIONS,
            "stdlib_layout": "python314-zip-stored-v1"}


def _json(data: bytes | str):
    return json.loads(data, object_pairs_hook=_strict_json_object, parse_constant=_reject_json_constant)


def _write_json(path: Path, payload: dict) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, allow_nan=False, indent=2)


def _file(path: Path, limit: int, *, binary: bool = False) -> bytes:
    parent = _strict_existing_directory(path.parent, "input parent")
    target = parent / path.name
    metadata = target.lstat()
    if not stat.S_ISREG(metadata.st_mode) or _is_reparse_point(metadata) or metadata.st_nlink != 1:
        raise ValueError("input must be a regular single-link file")
    if metadata.st_size > limit:
        raise ValueError(f"input exceeds {limit} bytes")
    if binary:
        # Generated ZIPs are binary; source/JSON still use the unchanged text
        # admission below. Bound reads and bind the open handle to its path.
        # Windows Python 3.12 can report creation time through lstat's ctime
        # but metadata-change time through fstat's ctime. Bind the two APIs by
        # birth time, then check each API's own ctime for changes independently.
        timestamp = "st_birthtime_ns" if os.name == "nt" and hasattr(metadata, "st_birthtime_ns") else "st_ctime_ns"
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", timestamp)
        expected = tuple(getattr(metadata, name) for name in fields)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(target, flags), "rb") as handle:
            opened = os.fstat(handle.fileno())
            if _is_reparse_point(opened) or tuple(getattr(opened, name) for name in fields) != expected:
                raise ValueError("binary input changed before reading")
            data = handle.read(limit + 1)
            final = os.fstat(handle.fileno())
        current = target.lstat()
        if (len(data) != metadata.st_size or tuple(getattr(final, name) for name in fields) != expected
                or tuple(getattr(current, name) for name in fields) != expected
                or final.st_ctime_ns != opened.st_ctime_ns
                or current.st_ctime_ns != metadata.st_ctime_ns):
            raise ValueError("binary input changed while reading")
    else:
        data = _read_regular_file(target, metadata, path.name)
    if len(data) > limit:
        raise ValueError(f"input exceeds {limit} bytes")
    return data


def prepare_module(code: bytes, entry: str) -> str:
    """Admit a whole module without executing or extracting any of it."""
    if len(code) > MAX_SOURCE_BYTES:
        raise ValueError(f"source exceeds {MAX_SOURCE_BYTES} bytes")
    if not isinstance(entry, str) or not entry.isidentifier() or entry.startswith("__"):
        raise ValueError("select a top-level function name")
    text = code.decode("utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise ValueError(f"module cannot be parsed by host Python {sys.version_info.major}.{sys.version_info.minor} at line {exc.lineno}: {exc.msg}") from exc
    if sum(isinstance(node, ast.FunctionDef) and node.name == entry for node in tree.body) != 1:
        raise ValueError("entry must name one synchronous top-level function; no function extraction or guessed globals")
    return text


def _module_names(paths: list[str]) -> dict[str, str]:
    """Canonical named modules only; no import-root or namespace guessing."""
    if type(paths) is not list or not 1 <= len(paths) <= MAX_MODULE_FILES:
        raise ValueError("select one to four module files, including package __init__.py files")
    names, components, modules = {}, {}, set()
    for path in paths:
        if (type(path) is not str or not 1 <= len(path) <= 1024
                or not path.endswith(".py") or "\\" in path):
            raise ValueError("module paths must be bounded relative ASCII identifier .py paths")
        parts = path.split("/")
        identifiers = parts[:-1] + [parts[-1][:-3]]
        if any(not part.isascii() or not part.isidentifier() for part in identifiers):
            raise ValueError("module paths must use relative ASCII identifier components")
        if path == "__init__.py":
            raise ValueError("a root __init__.py has no named package; select an explicit parent import root")
        for count in range(1, len(parts) + 1):
            prefix = "/".join(parts[:count])
            if components.setdefault(prefix.casefold(), prefix) != prefix:
                raise ValueError("module paths have case-colliding components")
        module = ".".join(identifiers[:-1] if identifiers[-1] == "__init__" else identifiers)
        if path in names or module.casefold() in modules:
            raise ValueError("duplicate module or module/package collision")
        names[path] = module
        modules.add(module.casefold())
    for path in paths:
        parts = path.split("/")
        for count in range(1, len(parts)):
            if "/".join(parts[:count] + ["__init__.py"]) not in names:
                raise ValueError("include every parent package __init__.py explicitly; namespace packages are unsupported")
    return names


def prepare_module_set(files: dict[str, bytes], entry_path: str, entry: str) -> dict:
    """Fingerprint an explicit, bounded source set. AST parsing never runs it."""
    if type(files) is not dict:
        raise ValueError("module files must map explicit relative paths to raw bytes")
    names = _module_names(list(files))
    if type(entry_path) is not str or entry_path not in names:
        raise ValueError("entry path must be one of the explicitly selected module files")
    if any(type(code) is not bytes for code in files.values()):
        raise ValueError("module source must be raw UTF-8 bytes")
    if sum(map(len, files.values())) > MAX_SOURCE_BYTES:
        raise ValueError(f"selected module source exceeds {MAX_SOURCE_BYTES} bytes in total")
    pins = []
    for path, code in sorted(files.items()):
        # ZIP import honors source encoding cookies. Do not parse UTF-8 text on
        # the host and then execute differently decoded bytes in the guest.
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(code).readline)
            if encoding not in {"utf-8", "utf-8-sig"}:
                raise ValueError("module-set encoding cookies must specify UTF-8")
            if path == entry_path:
                prepare_module(code, entry)
            else:
                ast.parse(code.decode("utf-8"), filename=path)
        except (SyntaxError, UnicodeError, RecursionError) as exc:
            raise ValueError(f"cannot statically parse selected UTF-8 module: {path}") from exc
        pins.append({"path": path, "sha256": hashlib.sha256(code).hexdigest(), "size_bytes": len(code)})
    identity = {"import_root": ".", "entry_path": entry_path, "entry": entry,
                "entry_module": names[entry_path], "files": pins}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    return {**identity, "sha256": digest}


def _read_module_files(root: Path, paths: list[str], *,
                       cancel_requested: Callable[[], bool] | None = None) -> dict[str, bytes]:
    _module_names(paths)
    if _strict_existing_directory(root, "module import root") != root:
        raise ValueError("module import root must not be redirected")
    files, total = {}, 0
    for name in sorted(paths):
        _check_cancel(cancel_requested)
        path = root / name
        for parent in reversed(path.parents):
            if parent == root or root in parent.parents:
                if _strict_existing_directory(parent, "module parent") != parent:
                    raise ValueError("module parent must not be redirected")
        code = _file(path, MAX_SOURCE_BYTES - total)
        files[name] = code
        total += len(code)
    return files


def _write_module_bundle(directory: Path, files: dict[str, bytes], metadata: dict) -> dict:
    directory.mkdir(mode=0o700)
    path = directory / "selected.zip"
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_STORED) as archive:
        for name, code in sorted(files.items()):
            member = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            member.create_system = 3
            member.external_attr = (stat.S_IFREG | 0o444) << 16
            archive.writestr(member, code)
    data = _file(path, MAX_MODULE_BUNDLE_BYTES, binary=True)
    return {"entry_module": metadata["entry_module"],
            "top_levels": sorted({name.split(".")[0] for name in _module_names(list(files)).values()}),
            "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def _module_bundle_unchanged(directory: Path, pin: dict) -> bool:
    if _strict_existing_directory(directory, "selected module bundle") != directory:
        return False
    with os.scandir(directory) as entries:
        first = next(entries, None)
        if first is None or first.name != "selected.zip" or next(entries, None) is not None:
            return False
    data = _file(directory / "selected.zip", MAX_MODULE_BUNDLE_BYTES, binary=True)
    return len(data) == pin["size_bytes"] and hashlib.sha256(data).hexdigest() == pin["sha256"]


def _bounded_json_value(value) -> bool:
    pending = [(value, 0)]
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if depth > 64 or visited + len(pending) > 65_536:
            return False
        if type(item) is dict and all(type(key) is str for key in item):
            if len(item) > 65_536:
                return False
            pending.extend((part, depth + 1) for part in item.values())
        elif type(item) is list:
            if len(item) > 65_536:
                return False
            pending.extend((part, depth + 1) for part in item)
        elif type(item) not in (str, int, float, bool, type(None)):
            return False
    return True


def prepare_inputs(raw_input: bytes) -> dict:
    if len(raw_input) > MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds {MAX_INPUT_BYTES} bytes")
    try:
        payload = _json(raw_input)
        if not _bounded_json_value(payload):
            raise ValueError("input JSON nesting exceeds the supported limit")
        json.dumps(payload, allow_nan=False)  # Reject overflow such as 1e999 too.
    except RecursionError as exc:
        raise ValueError("input JSON nesting exceeds the supported limit") from exc
    if (type(payload) is not dict or set(payload) != {"args", "kwargs"}
            or type(payload["args"]) is not list or type(payload["kwargs"]) is not dict
            or not all(isinstance(key, str) for key in payload["kwargs"])):
        raise ValueError('input must be exactly {"args": [...], "kwargs": {...}}')
    return payload


def _source_input_hints(code: bytes, entry: str, seed: dict):
    """Yield same-spelling comparison literals, never inferred program values."""
    try:
        text = prepare_module(code, entry)
        tree = ast.parse(text)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("input-search source cannot be statically parsed as UTF-8") from exc
    stack, visited = [tree], 0
    while stack:
        node = stack.pop()
        visited += 1
        if visited > _MAX_CALL_AST_NODES:
            raise ValueError("input-search source exceeds 50000 AST nodes")
        stack.extend(ast.iter_child_nodes(node))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == entry)
    arguments = function.args
    positional = arguments.posonlyargs + arguments.args
    names = [arg.arg for arg in positional + arguments.kwonlyargs]
    if (arguments.vararg or arguments.kwarg or len(names) != len(set(names))
            or len(seed["args"]) > len(positional)):
        return iter(())
    # This describes the written signature only. Reassignment, decorators and
    # dynamic rebinding can change runtime meaning; hints are not binding proofs.
    paths = {arg.arg: ("args", i) for i, arg in enumerate(positional[:len(seed["args"])])}
    keywords = {arg.arg for arg in arguments.args + arguments.kwonlyargs}
    for key in seed["kwargs"]:
        if key not in keywords or key in paths:
            return iter(())
        paths[key] = ("kwargs", key)
    required = [arg.arg for arg in positional[:len(positional) - len(arguments.defaults)]]
    required.extend(arg.arg for arg, default in zip(arguments.kwonlyargs, arguments.kw_defaults) if default is None)
    if any(name not in paths for name in required):
        return iter(())
    # AST identifiers are normalized; do not treat a different raw spelling as
    # an explicitly supplied keyword or a same-spelling parameter reference.
    lines = [line.encode("utf-8") for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    for arg in positional + arguments.kwonlyargs:
        spelling = arg.arg.encode("utf-8")
        if lines[arg.lineno - 1][arg.col_offset:arg.col_offset + len(spelling)] != spelling:
            paths.pop(arg.arg, None)

    absent = object()

    def literal(node):
        if isinstance(node, ast.Constant) and type(node.value) in (str, int, bool, type(None)):
            return node.value
        if (isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub))
                and isinstance(node.operand, ast.Constant) and type(node.operand.value) is int):
            return -node.operand.value if isinstance(node.op, ast.USub) else node.operand.value
        return absent

    def hints():
        stack = list(reversed(function.body))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                                 ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                continue
            if isinstance(node, ast.Compare) and len(node.ops) == 1:
                left, right, operator = node.left, node.comparators[0], node.ops[0]
                if isinstance(operator, (ast.In, ast.NotIn)):
                    name = left
                    literals = right.elts if isinstance(right, (ast.Tuple, ast.List)) and len(right.elts) <= 16 else []
                    if any(literal(item) is absent for item in literals):
                        literals = []
                else:
                    name, other = (left, right) if isinstance(left, ast.Name) else (right, left)
                    literals = [other]
                if isinstance(name, ast.Name) and name.id in paths:
                    spelling = lines[name.lineno - 1][name.col_offset:name.end_col_offset]
                    if spelling == name.id.encode("utf-8"):
                        path = paths[name.id]
                        seed_value = seed[path[0]][path[1]]
                        for item in literals:
                            value = literal(item)
                            if value is absent or type(value) is not type(seed_value):
                                continue
                            values = (value, value - 1, value + 1) if type(value) is int else (value,)
                            for replacement in values:
                                if replacement != seed_value:
                                    yield path, replacement, node.lineno
            stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return hints()


def prepare_input_search(raw_input: bytes, *, sources: tuple[bytes, bytes] | None = None,
                         entry: str | None = None) -> dict:
    """Preview a small, deterministic set of single-scalar input replacements.

    Traverse args then kwargs in their original order, across at most 32 scalar
    locations. Optional whole sources supply six same-spelling comparison hints
    before nearby replacements. Neither path evaluates source or proves domains,
    bindings or reachability. Exact seed bytes and dictionary order are retained.
    """
    seed = prepare_inputs(raw_input)
    seed_text = raw_input.decode("utf-8")
    source_hints = None
    if sources is not None:
        if (type(sources) is not tuple or len(sources) != 2 or any(type(code) is not bytes for code in sources)
                or type(entry) is not str or len(entry) > 200):
            raise ValueError("input-search sources require two complete byte strings and a function name")
        source_hints = [_source_input_hints(code, entry, seed) for code in sources]
    elif entry is not None:
        raise ValueError("input-search entry requires both sources")

    def encoded(value):
        return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))

    pending = [(("kwargs", key), value) for key, value in reversed(list(seed["kwargs"].items()))]
    pending.extend((("args", i), seed["args"][i]) for i in reversed(range(len(seed["args"]))))
    locations = []
    limited = False
    while pending:
        path, value = pending.pop()
        if type(value) is dict:
            pending.extend((path + (key,), part) for key, part in reversed(list(value.items())))
        elif type(value) is list:
            pending.extend((path + (i,), value[i]) for i in reversed(range(len(value))))
        else:
            if len(locations) == 32:
                limited = True
                break
            if type(value) is bool:
                replacements = [not value]
            elif type(value) is int:
                replacements = [0, 1, -1, value - 1, value + 1]
            elif type(value) is float:
                replacements = [part for part in (0.0, 1.0, -1.0, value - 1.0, value + 1.0)
                                if math.isfinite(part)]
            elif type(value) is str:
                replacements = ["", " ", "\t", value.strip(), value[:-1]]
            else:  # JSON null has no inferred application type.
                replacements = [False, 0, ""]
            distinct, seen_values = [], {encoded(value)}
            for part in replacements:
                try:
                    key = encoded(part)
                except ValueError:  # An adjacent integer may exceed the native digit cap.
                    limited = True
                    continue
                if key not in seen_values:
                    distinct.append(part)
                    seen_values.add(key)
            locations.append((path, distinct))

    inputs = [{"input_text": seed_text, "location": "seed"}]
    seen = {encoded(seed)}

    def append_input(path, replacement, location_index, hint=None):
        nonlocal limited
        candidate = deepcopy(seed)
        parent = candidate
        for component in path[:-1]:
            parent = parent[component]
        parent[path[-1]] = replacement
        try:
            text = encoded(candidate)
        except ValueError:
            limited = True
            return False
        if text in seen:
            return False
        if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            limited = True
            return False
        if len(inputs) == 12:
            limited = True
            return False
        prepare_inputs(text.encode("utf-8"))
        pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in path)
        location = pointer if len(pointer) <= 256 and pointer.isprintable() else f"scalar {location_index}"
        item = {"input_text": text, "location": location}
        if hint is not None:
            item["hint"] = hint
        inputs.append(item)
        seen.add(text)
        return True

    if source_hints is not None:
        location_indices = {path: index for index, (path, _) in enumerate(locations, 1)}
        distinct, hint_count = set(), 0
        active = list(enumerate(source_hints))
        while active and len(distinct) < 32:
            remaining = []
            for side, hints in active:
                item = next(hints, None)
                if item is None:
                    continue
                remaining.append((side, hints))
                path, replacement, line = item
                if path not in location_indices:
                    continue
                try:
                    key = (path, encoded(replacement))
                except ValueError:
                    limited = True
                    continue
                if key in distinct:
                    continue
                distinct.add(key)
                if hint_count < 6:
                    hint = {"side": ("before", "after")[side], "line": line}
                    hint_count += append_input(path, replacement, location_indices[path], hint)
                else:
                    limited = True
                if len(distinct) == 32:
                    limited = True
                    break
            active = remaining
    for replacement_index in range(5):
        for location_index, (path, replacements) in enumerate(locations, 1):
            if replacement_index >= len(replacements):
                continue
            append_input(path, replacements[replacement_index], location_index)
            if len(inputs) == 12 and limited:
                break
        if len(inputs) == 12 and limited:
            break
    plan = {"strategy": "nearby-v1", "seed_input_text": seed_text, "inputs": inputs,
            "max_initializations": 2 * len(inputs), "max_seconds": 120, "limited": limited}
    if sources is not None:
        plan["strategy"] = "source-v1"
        plan["sources"] = {"before": hashlib.sha256(sources[0]).hexdigest(),
                           "after": hashlib.sha256(sources[1]).hexdigest(), "entry": entry}
    return {**plan, "sha256": hashlib.sha256(encoded(plan).encode("ascii")).hexdigest()}


def prepare_call_inputs(code: bytes, entry: str, start: int, end: int) -> dict:
    """Draft JSON from one complete, same-spelling bare-name source call.

    Static syntax only: no target binding, alias resolution, argument evaluation,
    module preparation or execution. Selection is inclusive physical source lines,
    at most 80 lines/4000 UTF-8 bytes, including original line endings. Literal
    expression nodes share a 1024-node budget; their maximum depth is 24.
    """
    if (type(code) is not bytes or type(entry) is not str or not entry.isidentifier()
            or len(entry) > 200 or entry.startswith("__")
            or type(start) is not int or type(end) is not int
            or start < 1 or end < start or end - start >= 80):
        raise ValueError("invalid function name or inclusive source selection")
    if len(code) > _MAX_CALL_SOURCE_BYTES:
        raise ValueError("call source exceeds 256 KiB")
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(code).readline)
        if encoding not in {"utf-8", "utf-8-sig"}:
            raise ValueError("call source encoding cookies must specify UTF-8")
        text = code.decode("utf-8")
    except (UnicodeError, SyntaxError) as exc:
        raise ValueError("call source must be UTF-8") from exc
    if any(separator in text for separator in "\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
        raise ValueError("call source contains nonphysical line separators")
    lines = text.splitlines(keepends=True)
    if end > len(lines):
        raise ValueError("source selection exceeds available lines")
    if len("".join(lines[start - 1:end]).encode("utf-8")) > 4000:
        raise ValueError("selected call source exceeds 4000 UTF-8 bytes")
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ValueError("call source cannot be statically parsed by this Python version") from exc
    matches, stack, visited = [], [tree], 0
    while stack:
        node = stack.pop()
        visited += 1
        if visited > _MAX_CALL_AST_NODES:
            raise ValueError("call source exceeds 50000 AST nodes")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == entry and node.lineno <= end and node.end_lineno >= start):
            # AST identifiers are Unicode-normalized. Preserve the narrower
            # same-spelling promise without repeatedly splitting the full source.
            spelling = lines[node.func.lineno - 1].encode("utf-8")[node.func.col_offset:node.func.end_col_offset]
            if spelling == entry.encode("utf-8"):
                matches.append(node)
        stack.extend(ast.iter_child_nodes(node))
    if len(matches) != 1 or not start <= matches[0].lineno <= matches[0].end_lineno <= end:
        raise ValueError("select exactly one complete same-spelling bare-name call")
    selected = matches[0]
    literal_nodes = 0

    def literal(node: ast.AST, depth: int = 1):
        nonlocal literal_nodes
        literal_nodes += 1
        if literal_nodes > _MAX_CALL_LITERAL_NODES or depth > _MAX_CALL_LITERAL_DEPTH:
            raise ValueError("call literals exceed the 1024-node or 24-depth limit")
        if isinstance(node, ast.Constant):
            value = node.value
            if type(value) in (str, int, float, bool, type(None)):
                if type(value) is float and not math.isfinite(value):
                    raise ValueError("call floats must be finite")
                return value
        elif (isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub))
                and isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float)):
            value = literal(node.operand, depth + 1)
            return -value if isinstance(node.op, ast.USub) else value
        elif isinstance(node, ast.List):
            return [literal(item, depth + 1) for item in node.elts]
        elif isinstance(node, ast.Dict):
            result = {}
            for key, value in zip(node.keys, node.values):
                name = literal(key, depth + 1)
                if type(name) is not str or name in result:
                    raise ValueError("call dictionaries require unique string keys")
                result[name] = literal(value, depth + 1)
            return result
        raise ValueError("call arguments must contain JSON-compatible literals only; no evaluation")

    args = [literal(item) for item in selected.args]
    kwargs = {}
    for keyword in selected.keywords:
        if keyword.arg is None or keyword.arg in kwargs:
            raise ValueError("call keywords must be explicit and unique; no ** expansion")
        kwargs[keyword.arg] = literal(keyword.value)
    try:
        input_text = json.dumps({"args": args, "kwargs": kwargs}, ensure_ascii=False, allow_nan=False, indent=2)
        prepare_inputs(input_text.encode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("call input must fit the existing 16 KiB UTF-8 JSON limit") from exc
    return {"entry": entry, "start": start, "end": end, "call_start": selected.lineno,
            "call_end": selected.end_lineno, "input_text": input_text}


def prepare_request(source: Path, entry: str, inputs: Path) -> tuple[dict, dict]:
    """Static admission only. The full module, including initialization, runs later."""
    if source.suffix != ".py" or not isinstance(entry, str) or not entry.isidentifier() or entry.startswith("__"):
        raise ValueError("select one .py module and a top-level function name")
    code = _file(source, MAX_SOURCE_BYTES)
    raw_input = _file(inputs, MAX_INPUT_BYTES)
    text = prepare_module(code, entry)
    payload = prepare_inputs(raw_input)
    request = {"source": text, "entry": entry, "input": payload}
    identity = {"source_sha256": hashlib.sha256(code).hexdigest(),
                "input_sha256": hashlib.sha256(raw_input).hexdigest(), "entry": entry,
                "source_bytes": len(code), "input_bytes": len(raw_input)}
    return request, identity


def _unpack(archive: Path, destination: Path, *, bundle_stdlib: bool = False) -> None:
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if len(members) > 2500 or sum(item.file_size for item in members) > 200 * 1024**2:
            raise ValueError("runtime archive exceeds extraction bounds")
        names = set()
        for item in members:
            if item.orig_filename != item.filename:
                raise ValueError("runtime archive member was implicitly normalized")
            name = item.filename.rstrip("/")
            _relative_manifest_path(name, "archive member")
            parts = PurePosixPath(name).parts
            if (any(part.rstrip(" .") != part for part in parts)
                    or stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or name.casefold() in names):
                raise ValueError("runtime archive contains an unsafe or duplicate path")
            names.add(name.casefold())
        if bundle_stdlib:
            prefix = "lib/python3.14/"
            if (not any(item.filename == prefix + "encodings/__init__.py" and not item.is_dir()
                        for item in members)
                    or "lib/python314.zip" in names
                    or any(item.filename.startswith(prefix + "lib-dynload/")
                           and not item.is_dir() for item in members)):
                raise ValueError("guest archive does not support the pinned stdlib ZIP layout")
        destination.mkdir()
        if not bundle_stdlib:
            package.extractall(destination)
            return
        # CPython's own POSIX path discovery imports this ZIP without custom
        # loaders or PYTHONPATH. Keep every original stdlib member byte-for-byte;
        # ZIP_STORED requires no zlib in the WASI guest. Fixed ordering/metadata
        # make the result identical on Windows and Linux. No .pyc generation.
        (destination / "lib/python3.14/lib-dynload").mkdir(parents=True)
        with zipfile.ZipFile(destination / "lib/python314.zip", "x", compression=zipfile.ZIP_STORED) as bundled:
            for item in sorted(members, key=lambda member: member.filename):
                if not item.filename.startswith(prefix):
                    package.extract(item, destination)
                elif item.filename != prefix:
                    entry = zipfile.ZipInfo(item.filename[len(prefix):], (1980, 1, 1, 0, 0, 0))
                    entry.create_system = 3
                    entry.external_attr = ((stat.S_IFDIR | 0o755) if item.is_dir()
                                           else (stat.S_IFREG | 0o644)) << 16
                    bundled.writestr(entry, package.read(item))


def _pins(root: Path) -> list[dict]:
    files, unsafe = _installed_inventory(root)
    if unsafe:
        raise ValueError("runtime inventory contains unsafe entries")
    return [{"filename": name, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
            for name, path in sorted(files.items())]


def _worker(root: Path, directory: Path, operation: str, payload: dict, *,
            cancel_requested: Callable[[], bool] | None = None):
    request = directory / "worker-input.json"
    _write_json(request, payload)
    worker = Path(__file__).with_name("_experiment_worker.py").resolve()
    definition = CheckDefinition("isolated_experiment", "Explicit Python WASI experiment",
        "Fixed worker; no host project import", (os.path.abspath(sys.executable), "-I", "-B", "-X", "utf8",
        str(worker), operation, str(root), str(request)), ".", ".", 55.0 if operation == "compile" else 10.0)
    artifacts = ArtifactStore(directory)
    runner = CheckRunner(WorkspacePolicy(directory), artifacts, max_output_bytes=512 * 1024,
        max_timeout_seconds=definition.timeout_seconds,
        process_memory_limit_bytes=1024**3 if os.name == "nt" else None)
    result = runner._run_definition(definition, **({} if cancel_requested is None else
                                                  {"cancel_requested": cancel_requested}))
    _write_json(directory / "process.json", result.as_dict())
    envelope = None
    if result.ok and not result.output_truncated and not result.capture_errors:
        envelope = _json(artifacts.read(result.stdout_artifact))
        if type(envelope) is not dict:
            raise ValueError("invalid native worker envelope")
    return result, envelope


def setup_runtime(destination: Path, archives: Path) -> dict:
    """One explicit offline installation. Never merge/overwrite an existing runtime."""
    host = native_platform()
    if not destination.is_absolute() or ".." in destination.parts:
        raise ValueError("runtime destination must be absolute without '..'")
    parent = _strict_existing_directory(destination.parent, "runtime destination parent")
    root = parent / destination.name
    if os.path.lexists(root):
        raise ValueError("runtime destination already exists; select a new directory")
    archive_root = _strict_existing_directory(archives, "pinned archive directory")
    selected = (PYTHON_ARCHIVE, WHEELS[host])
    for name, size, digest in selected:
        path = _artifact_path(archive_root, name)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size or sha256_file(path) != digest:
            raise ValueError(f"pinned archive is missing, unsafe or changed: {name}")
    root.mkdir(mode=0o700)
    installed = root / "installed"
    installed.mkdir()
    for (name, _, digest), folder in zip(selected, ("guest", "host")):
        shutil.copyfile(archive_root / name, root / name)
        if sha256_file(root / name) != digest:
            raise ValueError("archive changed while copying; incomplete setup retained")
        _unpack(root / name, installed / folder, bundle_stdlib=folder == "guest")
    manifest = {"schema_version": 2, "build": "CPython3.14.7-Wasmtime48",
        "commit": "823f0323ee6ec1402088b73bce1a38473cac36dc", "install_dir": "installed",
        "assets": [{"filename": name, "sha256": digest} for name, _, digest in selected],
        "required_files": REQUIRED_FILES[:-1], "installed_files": _pins(installed),
        "experiment": _metadata()}
    _write_json(root / "vendor.json", manifest)
    if not verify_runtime(root / "vendor.json", root).ok:
        raise ValueError("vendor integrity verification failed")
    build = root / "build"
    build.mkdir(mode=0o700)
    process, compiled = _worker(root, build, "compile", {})
    if not process.ok or compiled is None:
        raise ValueError(f"interpreter compilation {process.status}; inspect build/process.json; setup retained")
    cache = installed / "python.cwasm"
    if cache.stat().st_size != compiled.get("bytes") or sha256_file(cache) != compiled.get("sha256"):
        raise ValueError("compiled interpreter output does not match native compiler report")
    manifest["required_files"].append("python.cwasm")
    # Keep the original archive-derived vendor pins. Compilation must not
    # silently bless changed vendor bytes or extra files as a new baseline.
    manifest["installed_files"].append({"filename": "python.cwasm",
        "size_bytes": compiled["bytes"], "sha256": compiled["sha256"]})
    _write_json(root / "runtime.json", manifest)
    verify_experiment_runtime(root)
    return {"runtime": str(root), "platform": host, "python": "3.14.7", "wasmtime": "48.0.0",
            "compile_seconds": compiled["compile_seconds"], "network_used": False}


def verify_experiment_runtime(directory: Path, *,
                              cancel_requested: Callable[[], bool] | None = None) -> tuple[Path, dict]:
    _check_cancel(cancel_requested)
    root = _strict_existing_directory(directory, "experiment runtime")
    path = _artifact_path(root, "runtime.json")
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or metadata.st_size > 512 * 1024):
        raise ValueError("experiment runtime manifest must be a bounded regular single-link file")
    manifest = load_manifest(path)
    expected = [{"filename": name, "sha256": digest}
                for name, _, digest in (PYTHON_ARCHIVE, WHEELS[native_platform()])]
    if (manifest.get("experiment") != _metadata() or manifest["assets"] != expected
            or manifest["install_dir"] != "installed"
            or manifest["required_files"] != REQUIRED_FILES):
        raise ValueError("runtime belongs to another platform, version or engine configuration")
    if not verify_runtime(path, root, **({} if cancel_requested is None else
                                       {"cancel_requested": cancel_requested})).ok:
        raise ValueError("experiment runtime integrity failed; no execution attempted")
    pin = next(item for item in manifest["installed_files"] if item["filename"] == "python.cwasm")
    return root, pin


def reported_result(host: dict | None) -> dict | None:
    """Display-only guest protocol. Never promotes output into trusted evidence."""
    if (host is None or host["output_limit"]
            or (host["host_status"], host["detail"]) not in (("exited", None), ("guest_exit", 0))):
        return None
    marker = "FORGE8_GUEST_RESULT="
    lines = host["guest_output"]["stdout"].splitlines()
    candidates = [line[len(marker):] for line in lines if line.startswith(marker)]
    if len(candidates) != 1 or not lines or not lines[-1].startswith(marker):
        return None
    try:
        value = _json(candidates[0])
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return None
    if type(value) is dict and (set(value) == {"return"} or (
            set(value) == {"exception", "message", "phase"}
            and type(value["exception"]) is str and type(value["message"]) is str and type(value["phase"]) is str
            and value["phase"] in {"module_initialization", "call", "serialization"})):
        return value
    return None


def _reported_trace(host: dict | None, source: str) -> dict | None:
    """Bounded guest-reported line visits, never a trusted execution path.

    A missing or invalid trace cannot manufacture an empty path. End-hook
    identity does not establish that tracing remained active throughout a call.
    The independently parsed result protocol is deliberately unchanged.
    """
    result = reported_result(host)
    if result is None or result.get("phase") == "module_initialization":
        return None
    marker = "FORGE8_GUEST_TRACE="
    lines = [line for line in host["guest_output"]["stdout"].splitlines() if line]
    candidates = [line[len(marker):] for line in lines if line.startswith(marker)]
    if len(candidates) != 1 or len(lines) < 2 or not lines[-2].startswith(marker):
        return None
    # Python's physical source positions use LF, CRLF and CR, not all of the
    # Unicode separators recognized by str.splitlines(). Keep source bytes as-is.
    physical = source.replace("\r\n", "\n").replace("\r", "\n")
    line_count = physical.count("\n") + int(bool(physical) and not physical.endswith("\n"))
    try:
        value = _json(candidates[0])
    except (ValueError, TypeError, RecursionError):
        return None
    if (type(value) is not dict or set(value) != {"line_events", "truncated", "hook_intact"}
            or type(value["line_events"]) is not list or len(value["line_events"]) > 1000
            or type(value["truncated"]) is not bool or type(value["hook_intact"]) is not bool
            or (value["truncated"] and len(value["line_events"]) != 1000)
            or any(type(line) is not int or not 1 <= line <= line_count
                   for line in value["line_events"])):
        return None
    return value


def compare_reported_results(left: dict | None, right: dict | None) -> str:
    """Compare bounded JSON reports, not execution truth or Python equivalence.

    canonical-json-v1 ignores object key order, preserves array order and keeps
    bool/int/float distinct. Floats already have the host JSON decoder's binary
    precision. Guest code can interfere with this display-only protocol.
    """
    canonical = []
    for value in (left, right):
        if type(value) is not dict or not (set(value) == {"return"} or (
                set(value) == {"exception", "message", "phase"}
                and all(type(value[key]) is str for key in value)
                and value["phase"] in {"module_initialization", "call", "serialization"})):
            return "unavailable"
        if not _bounded_json_value(value):
            return "unavailable"
        try:
            text = json.dumps(value, sort_keys=True, ensure_ascii=True,
                              separators=(",", ":"), allow_nan=False)
            if len(text) > 6 * OUTPUT_BYTES:
                return "unavailable"
        except (ValueError, TypeError, RecursionError):
            return "unavailable"
        canonical.append(text)
    return "same" if canonical[0] == canonical[1] else "different"


def retain_trial_result(target: dict, identifier: str, input_text: str, report: dict,
                        report_bytes: bytes, runtime_manifest: bytes, source_bytes: bytes,
                        run_root: Path, *, trace_lines: bool = False) -> bytes:
    """Capture one completed, source-bound report for explicit input comparison.

    This is an immutable historical observation, not a new execution gate or a
    live-disk receipt. The desk checks identical manifest bytes before/after the
    existing runner; callers cannot supply source/report paths through the API.
    Capture failure must not relabel the ordinary trial's existing outcome.
    """
    def canonical(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")

    if (type(trace_lines) is not bool or type(input_text) is not str
            or type(identifier) is not str or not 1 <= len(identifier) <= 200
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in identifier)
            or type(report_bytes) is not bytes or len(report_bytes) > 2 * 1024**2
            or type(runtime_manifest) is not bytes or len(runtime_manifest) > 512 * 1024
            or type(source_bytes) is not bytes or not 1 <= len(source_bytes) <= MAX_SOURCE_BYTES):
        raise ValueError("trial comparison requires bounded exact captured inputs")
    expected_target = {"file", "path", "version", "entry", "source_sha256", "source_bytes"}
    if (type(target) is not dict or set(target) != expected_target
            or any(type(target[key]) is not str or not target[key] for key in expected_target - {"source_bytes"})
            or type(target["source_bytes"]) is not int
            or target["source_bytes"] != len(source_bytes)
            or hashlib.sha256(source_bytes).hexdigest() != target["source_sha256"]):
        raise ValueError("trial comparison source identity is unavailable")
    raw_input = input_text.encode("utf-8")
    prepare_inputs(raw_input)
    parsed = _json(report_bytes)
    manifest = _json(runtime_manifest)
    if (type(parsed) is not dict or type(report) is not dict
            or canonical(parsed) != canonical(report)
            or type(parsed.get("schema_version")) is not int or parsed["schema_version"] != 1
            or parsed.get("kind") != "forge8.experiment"
            or parsed.get("run_root") != str(run_root)
            or parsed.get("source_unchanged") is not True or parsed.get("runtime_unchanged") is not True
            or parsed.get("postcheck_errors") != [] or type(parsed.get("postcheck_errors")) is not list
            or parsed.get("process_status") != "passed"):
        raise ValueError("trial comparison requires an intact completed report")
    identity = {"source_sha256": target["source_sha256"], "source_bytes": len(source_bytes),
        "input_sha256": hashlib.sha256(raw_input).hexdigest(), "input_bytes": len(raw_input), "entry": target["entry"]}
    if trace_lines:
        identity["trace_lines"] = True
    if canonical(parsed.get("identity")) != canonical(identity):
        raise ValueError("trial comparison report does not bind the submitted source and input")
    if (type(manifest) is not dict or canonical(manifest.get("experiment")) != canonical(_metadata())
            or manifest.get("install_dir") != "installed" or type(manifest.get("installed_files")) is not list):
        raise ValueError("trial comparison runtime identity is unavailable")
    caches = [row for row in manifest["installed_files"] if type(row) is dict and row.get("filename") == "python.cwasm"]
    if (len(caches) != 1 or canonical(parsed.get("runtime")) != canonical({**_metadata(), "cache": caches[0]})):
        raise ValueError("trial comparison runtime does not match the captured manifest")
    execution = parsed.get("execution")
    if (type(execution) is not dict or execution.get("output_limit") is not False
            or not (execution.get("host_status") == "exited" and execution.get("detail") is None
                or execution.get("host_status") == "guest_exit" and type(execution.get("detail")) is int and execution["detail"] == 0)):
        raise ValueError("trial comparison requires completed guest output")
    output = execution.get("guest_output")
    if (type(output) is not dict or set(output) != {"stdout", "stderr"}
            or any(type(text) is not str for text in output.values())
            or sum(len(text.encode("utf-8")) for text in output.values()) > 3 * OUTPUT_BYTES):
        raise ValueError("trial comparison requires complete bounded guest output")
    result = parsed.get("reported_result")
    if (compare_reported_results(result, result) == "unavailable"
            or canonical(reported_result(execution)) != canonical(result)):
        raise ValueError("trial comparison has no complete typed guest result")
    text = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
    text = "".join(c if c.isprintable() or c == "\n" else json.dumps(c)[1:-1] for c in text)
    view = {**target, "id": identifier, "input_text": input_text,
        "input_sha256": identity["input_sha256"], "runtime_sha256": hashlib.sha256(runtime_manifest).hexdigest(),
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(), "result_text": text, "trace_lines": trace_lines}
    if len(json.dumps(view, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 128 * 1024:
        raise ValueError("trial comparison display exceeds 128 KiB; no partial baseline is retained")
    retained = canonical({"view": view, "result": result, "runtime": parsed["runtime"]})
    if len(retained) > 256 * 1024:
        raise ValueError("trial comparison capture exceeds 256 KiB; no partial baseline is retained")
    return retained


def trial_result_view(record: bytes) -> dict:
    """Return a detached public projection of host-owned immutable bytes."""
    return _json(record)["view"]


def compare_trial_results(baseline: bytes | None, current: bytes | None) -> tuple[str, str | None]:
    """Compare captured reports, never infer equivalence or a passing test."""
    if baseline is None:
        return "unavailable", "no_baseline"
    if current is None:
        return "unavailable", "current_unavailable"
    left, right = _json(baseline), _json(current)
    a, b = left["view"], right["view"]
    if a["id"] == b["id"]:
        return "unavailable", "same_run"
    if any(a[key] != b[key] for key in ("version", "file", "path", "entry", "source_sha256", "source_bytes")):
        return "unavailable", "source_changed"
    if a["runtime_sha256"] != b["runtime_sha256"] or left["runtime"] != right["runtime"]:
        return "unavailable", "runtime_changed"
    if a["trace_lines"] != b["trace_lines"]:
        return "unavailable", "trace_mode_changed"
    outcome = compare_reported_results(left["result"], right["result"])
    return outcome, "current_unavailable" if outcome == "unavailable" else None


def run_experiment(runtime: Path, source: Path, entry: str, inputs: Path, run_root: Path,
                   *, allow_execution: bool = False, expected_source_sha256: str | None = None,
                   expected_input_sha256: str | None = None,
                   cancel_requested: Callable[[], bool] | None = None,
                   module_root: Path | None = None, module_files: list[str] | None = None,
                   expected_module_set_sha256: str | None = None,
                   trace_lines: bool = False) -> dict:
    if type(trace_lines) is not bool:
        raise ValueError("trace_lines must be a boolean")
    if allow_execution is not True:
        raise ValueError("this executes the whole module and one call; explicit --allow-execution is required")
    module_mode = module_root is not None or module_files is not None or expected_module_set_sha256 is not None
    if trace_lines and module_mode:
        raise ValueError("line tracing supports single-file experiments only")
    started = time.monotonic()
    _check_cancel(cancel_requested)
    cancel_options = {} if cancel_requested is None else {"cancel_requested": cancel_requested}
    bundle = None
    if module_mode:
        if module_root is None or module_files is None:
            raise ValueError("module mode requires an explicit import root and complete selected-file list")
        _module_names(module_files)
        module_files = list(module_files)  # Own the selection while verification can yield.
        module_root = Path(os.path.abspath(module_root))
        source = Path(os.path.abspath(source))
        try:
            entry_path = source.relative_to(module_root).as_posix()
        except ValueError as exc:
            raise ValueError("entry source must be inside the explicit module import root") from exc
        files = _read_module_files(module_root, module_files, **cancel_options)
        metadata = prepare_module_set(files, entry_path, entry)
        raw_input = _file(inputs, MAX_INPUT_BYTES)
        request = {"source": files[entry_path].decode("utf-8"), "entry": entry,
                   "input": prepare_inputs(raw_input)}
        identity = {"source_sha256": hashlib.sha256(files[entry_path]).hexdigest(),
                    "input_sha256": hashlib.sha256(raw_input).hexdigest(), "entry": entry,
                    "source_bytes": len(files[entry_path]), "input_bytes": len(raw_input),
                    "module_set": metadata}
        if expected_module_set_sha256 is not None and metadata["sha256"] != expected_module_set_sha256:
            raise ValueError("selected module set changed; no execution attempted")
        run_root = Path(os.path.abspath(run_root))
        if (_strict_existing_directory(run_root.parent, "experiment run parent") != run_root.parent
                or run_root == module_root or module_root in run_root.parents):
            raise ValueError("module experiment run must be unredirected and outside the import root")
    else:
        request, identity = prepare_request(source, entry, inputs)
    if trace_lines:
        request["trace_lines"] = identity["trace_lines"] = True
    if expected_source_sha256 is not None and identity["source_sha256"] != expected_source_sha256:
        raise ValueError("selected source version changed; no execution attempted")
    if expected_input_sha256 is not None and identity["input_sha256"] != expected_input_sha256:
        raise ValueError("selected input changed; no execution attempted")
    root, pin = verify_experiment_runtime(runtime, **cancel_options)
    supplied_sources = [module_root / name for name in module_files] if module_mode else [source]
    for supplied in (path.resolve() for path in [*supplied_sources, inputs]):
        if supplied == root or root in supplied.parents:
            raise ValueError("experiment inputs must be outside the trusted runtime")
    if run_root == root or root in run_root.parents or run_root in root.parents:
        raise ValueError("experiment run and runtime must not overlap")
    if module_mode:
        # Reconfirm exactly the admitted bytes after runtime verification; the
        # ZIP is generated from those bytes, not a fresh unbound source read.
        if (_read_module_files(module_root, module_files, **cancel_options) != files
                or _file(inputs, MAX_INPUT_BYTES) != raw_input):
            raise ValueError("module source or input changed before execution")
    run_root.mkdir(mode=0o700)
    if module_mode:
        bundle = _write_module_bundle(run_root / "modules", files, metadata)
        if not _module_bundle_unchanged(run_root / "modules", bundle):
            raise ValueError("selected module bundle changed before execution")
    captured = {"identity": identity, "request": request}
    worker_payload = {"request": request, "cache_pin": pin}
    if bundle is not None:
        captured["module_bundle"] = worker_payload["module_bundle"] = bundle
    _write_json(run_root / "input.json", captured)
    _check_cancel(cancel_requested)
    process, host = _worker(root, run_root, "run", worker_payload, **cancel_options)
    postcheck_errors = []
    runtime_unchanged = True
    try:
        verify_experiment_runtime(root, **cancel_options)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        runtime_unchanged = False
        postcheck_errors.append("Runtime integrity could not be reconfirmed after execution")
    try:
        if module_mode:
            unchanged = _read_module_files(module_root, module_files, **cancel_options) == files
        else:
            unchanged = hashlib.sha256(_file(source, MAX_SOURCE_BYTES)).hexdigest() == identity["source_sha256"]
        unchanged = hashlib.sha256(_file(inputs, MAX_INPUT_BYTES)).hexdigest() == identity["input_sha256"] and unchanged
    except (OSError, ValueError):
        unchanged = False
        postcheck_errors.append("Source or input could not be reread after execution")
    bundle_unchanged = True
    if bundle is not None:
        try:
            bundle_unchanged = _module_bundle_unchanged(run_root / "modules", bundle)
        except (OSError, ValueError):
            bundle_unchanged = False
        if not bundle_unchanged:
            unchanged = False
            postcheck_errors.append("Selected module bundle integrity could not be reconfirmed after execution")
    report = {"schema_version": 1, "kind": "forge8.experiment", "identity": identity,
        "runtime": {**_metadata(), "cache": pin}, "source_unchanged": unchanged,
        "runtime_unchanged": runtime_unchanged, "postcheck_errors": postcheck_errors,
        "process_status": process.status, "duration_seconds": process.duration_seconds,
        "execution": host, "run_root": str(run_root),
        "notice": "Guest-reported output from a separate WASI environment; not an original-project test or verified explanation."}
    if bundle is not None:
        report["module_bundle"] = bundle
    if host is not None:
        if (set(host) != {"host_status", "detail", "output_limit", "load_seconds", "worker_seconds", "guest_output"}
                or host["host_status"] not in {"exited", "guest_exit", "trap"}
                or type(host["output_limit"]) is not bool or type(host["guest_output"]) is not dict
                or set(host["guest_output"]) != {"stdout", "stderr"}
                or not all(type(value) is str for value in host["guest_output"].values())
                or sum(len(value.encode("utf-8")) for value in host["guest_output"].values()) > 3 * OUTPUT_BYTES):
            raise ValueError("native worker returned invalid bounded output")
    report["reported_result"] = reported_result(host) if runtime_unchanged and bundle_unchanged else None
    if trace_lines:
        report["reported_trace"] = (_reported_trace(host, request["source"])
            if (unchanged and runtime_unchanged and bundle_unchanged and process.ok
                and not process.output_truncated and not process.capture_errors
                and report["reported_result"] is not None) else None)
    _check_cancel(cancel_requested)
    # Includes admission and full pre/post verification, not only the worker.
    # CLI interpreter startup and final report serialization are outside this.
    report["elapsed_seconds"] = time.monotonic() - started
    _write_json(run_root / "experiment.json", report)
    return report
