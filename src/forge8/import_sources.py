"""Bounded import-source path hypotheses inside one admitted snapshot side.

No import machinery, project configuration, __all__ expressions or target code
is evaluated. Conventional root/src paths are alternatives, not sys.path or
runtime precedence claims. The caller owns snapshot identity and file reads.
"""

from __future__ import annotations

import ast
import json
from pathlib import PurePosixPath
from typing import Callable

from .source_context import _declaration, _target_names

_MAX_FILES = 16
_MAX_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_BYTES = 256 * 1024
_MAX_NODES = 50_000
_MAX_STEPS = 8
_MAX_ROUTES = 32
_MAX_METADATA_BYTES = 64 * 1024
_SCOPE = "snapshot import candidates"


class _Refusal(Exception):
    def __init__(self, status: str, reason: str):
        self.status, self.reason = status, reason


def _result(status="available", reason=None, routes=None):
    return {"scope": _SCOPE, "semantics_verified": False, "status": status,
        "reason": reason, "routes": [] if routes is None else routes}


def _path(value):
    return (type(value) is str and bool(value) and "\\" not in value and ":" not in value and "\x00" not in value
        and not value.startswith("/") and PurePosixPath(value).as_posix() == value
        and all(part not in {".", ".."} for part in value.split("/")))


def trace_import(paths: list[str], path: str, text: str, candidate: dict,
        read_text: Callable[[str], str]) -> dict:
    """Follow one recomputed C1 import origin, never accepting a requested target.

    All paths are logical, admitted, same-side relative paths. Only read_text
    accesses retained bytes; cached ASTs live for this request only. Refusals
    publish no partial routes. A module step is file-only, with null coordinates.
    """
    if (type(paths) is not list or any(not _path(item) for item in paths)
            or len(set(paths)) != len(paths) or not _path(path) or path not in paths
            or type(text) is not str or not callable(read_text)
            or type(candidate) is not dict
            or type(candidate.get("kind")) is not str
            or candidate.get("kind") not in {"import", "from import"}
            or type(candidate.get("name")) is not str or not candidate["name"]
            or type(candidate.get("start_line")) is not int
            or type(candidate.get("end_line")) is not int
            or not 1 <= candidate["start_line"] <= candidate["end_line"]
            or type(candidate.get("conditional")) is not bool):
        raise ValueError("expected an admitted file and exact import declaration")
    if len(paths) > 1000:
        return _result("limited", "file_inventory_limit")
    if not path.endswith(".py"):
        return _result("unsupported", "language")
    admitted = set(paths)
    roots = ("", "src") if any(item.startswith("src/") for item in paths) else ("",)
    cache, routes, route_keys = {}, [], set()
    byte_count = 0

    def load(name):
        nonlocal byte_count
        if name in cache:
            return cache[name]
        if name not in admitted or not name.endswith(".py"):
            raise ValueError("import lookup requested a non-admitted Python source")
        if len(cache) >= _MAX_FILES:
            raise _Refusal("limited", "file_limit")
        # The callback enforces the snapshot boundary. Its refusal must reach
        # the caller, not become an ordinary unresolved navigation result.
        source = text if name == path else read_text(name)
        try:
            if type(source) is not str:
                raise _Refusal("unavailable", "source_unavailable")
            if len(source) > _MAX_SOURCE_BYTES:
                raise _Refusal("limited", "source_limit")
            size = len(source.encode("utf-8"))
            if size > _MAX_SOURCE_BYTES:
                raise _Refusal("limited", "source_limit")
            if byte_count + size > _MAX_BYTES:
                raise _Refusal("limited", "byte_limit")
            if any(char in source for char in "\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
                raise _Refusal("unavailable", "line_separators")
            tree = ast.parse(source, filename=name)
        except UnicodeError:
            raise _Refusal("unavailable", "utf8") from None
        except RecursionError:
            raise _Refusal("unavailable", "parse_depth") from None
        except SyntaxError:
            raise _Refusal("unavailable", "syntax_or_version") from None
        except (OSError, ValueError):
            raise _Refusal("unavailable", "source_unavailable") from None
        nodes, pending = [], [tree]
        while pending:
            node = pending.pop()
            nodes.append(node)
            if len(nodes) > _MAX_NODES:
                raise _Refusal("limited", "node_limit")
            pending.extend(reversed(list(ast.iter_child_nodes(node))))
        value = (tree, source.splitlines(), nodes)
        cache[name] = value
        byte_count += size
        return value

    def step(name, label, kind, first=None, last=None, conditional=False):
        return {"path": name, "name": label, "kind": kind, "start_line": first,
            "end_line": last, "conditional": conditional}

    def append(steps, item):
        if len(steps) >= _MAX_STEPS:
            raise _Refusal("limited", "step_limit")
        return [*steps, item]

    def emit(outcome, reason, steps):
        row = {"outcome": outcome, "reason": reason, "steps": steps}
        key = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if key in route_keys:
            return
        if len(routes) >= _MAX_ROUTES:
            raise _Refusal("limited", "route_limit")
        routes.append(row)
        route_keys.add(key)
        if len(json.dumps(_result(routes=routes), ensure_ascii=False).encode("utf-8")) > _MAX_METADATA_BYTES:
            raise _Refusal("limited", "metadata_limit")

    def locations(base):
        # A namespace directory has no file of its own. Never invent one.
        files = [name for name in (
            base + ".py" if base else "", (base + "/" if base else "") + "__init__.py")
            if name in admitted]
        answer = [(name, name.endswith("/__init__.py") or name == "__init__.py") for name in files]
        prefix = base + "/" if base else ""
        if not any(package for _, package in answer) and any(
                item.startswith(prefix) and item.endswith(".py") for item in admitted):
            answer.append((None, True))
        return answer

    def bases(name, node, requested):
        if not isinstance(node, ast.ImportFrom) or node.level == 0:
            suffix = requested.replace(".", "/")
            return [((root + "/") if root else "") + suffix for root in roots]
        # Relative navigation is physical and bounded by the conventional root.
        # src/ is treated as the source root, never traversed above for a relative
        # request. This is a source-layout hypothesis, not __package__ discovery.
        root_parts = ["src"] if name.startswith("src/") else []
        parent = name.split("/")[:-1]
        remove = node.level - 1
        if remove > len(parent) - len(root_parts):
            return []
        kept = parent[:len(parent) - remove] if remove else parent
        # The conventional search root is not itself an unnamed namespace
        # package. It can be the opened package only when its __init__ is retained.
        if kept == root_parts and "/".join([*root_parts, "__init__.py"]) not in admitted:
            return []
        return ["/".join([*kept, *requested.split(".")]) if requested else "/".join(kept)]

    def declarations(name, wanted):
        tree, lines, nodes = load(name)
        matches, dynamic = [], False
        pending = [(tree, False)]
        while pending:
            node, conditional = pending.pop()
            if isinstance(node, ast.Lambda):
                # Only defaults use the surrounding module scope; the body and
                # parameters belong to the lambda and cannot be module exports.
                pending.extend((item, conditional) for item in reversed([
                    *node.args.defaults, *(item for item in node.args.kw_defaults if item is not None)]))
                continue
            found = _declaration(node, lines)
            if found is not None:
                kind, names, first, last = found
                if wanted in names:
                    matches.append((node, step(name, wanted, kind, first, last, conditional)))
                if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
                    dynamic = True
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == "__getattr__":
                        dynamic = True
                    surrounding = list(node.decorator_list)
                    if getattr(node, "type_params", ()):
                        # Generic headers have annotation scopes, not an
                        # ordinary surrounding module namespace. Do not guess.
                        dynamic = True
                    elif isinstance(node, ast.ClassDef):
                        surrounding.extend([*node.bases, *(item.value for item in node.keywords)])
                    else:
                        surrounding.extend([*node.args.defaults,
                            *(item for item in node.args.kw_defaults if item is not None)])
                    pending.extend((item, conditional) for item in reversed(surrounding))
                    continue
            extra = []
            if isinstance(node, (ast.AugAssign, ast.NamedExpr)):
                extra = [(node.target, "augmented assignment" if isinstance(node, ast.AugAssign)
                    else "named assignment", True)]
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                extra = [(node.target, "loop target", True)]
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                extra = [(item.optional_vars, "with target", True) for item in node.items
                    if item.optional_vars is not None]
            for target, kind, uncertain in extra:
                if wanted in _target_names([target]):
                    matches.append((node, step(name, wanted, kind,
                        node.lineno, node.end_lineno, uncertain)))
            if isinstance(node, ast.Delete) and wanted in _target_names(node.targets):
                dynamic = True
            if isinstance(node, ast.ExceptHandler) and node.name == wanted:
                matches.append((node, step(name, wanted, "exception target", node.lineno,
                    node.end_lineno, True)))
                dynamic = True  # Handler cleanup removes the exception name.
            if ((isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == wanted)
                    or isinstance(node, ast.MatchMapping) and node.rest == wanted):
                matches.append((node, step(name, wanted, "pattern target", node.lineno,
                    node.end_lineno, True)))
            nested = conditional or isinstance(node, ast.stmt)
            pending.extend((child, nested) for child in reversed(list(ast.iter_child_nodes(node))))
        # Global writes in function bodies and dynamic exports are outside this
        # module-suite index; do not silently turn its candidates into uniqueness.
        dynamic |= any(isinstance(node, ast.Global) and wanted in node.names for node in nodes)
        return matches, dynamic

    def member(base, name, steps, seen):
        places = locations(base)
        if not places:
            emit("unresolved", "not_in_snapshot", steps)
            return
        for target, package in places:
            current = steps
            matches, dynamic = [], False
            if target is not None:
                load(target)
                current = append(steps, step(target, base.replace("/", ".") or "<package>", "module"))
                matches, dynamic = declarations(target, name)
                for node, origin in matches:
                    following = append(steps, origin)
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        follow(target, node, name, following, seen)
                    else:
                        reason = ("annotation_only" if isinstance(node, ast.AnnAssign) and node.value is None
                            else "expression_alias_not_followed" if isinstance(node, (ast.Assign, ast.AnnAssign))
                            and isinstance(node.value, (ast.Name, ast.Attribute)) else None)
                        emit("declaration", reason, following)
            children = locations((base + "/" if base else "") + name) if package else []
            for child, _child_package in children:
                if child is not None:
                    load(child)
                    emit("module", "submodule_candidate",
                        append(current, step(child, name, "module")))
                elif not matches:
                    emit("unresolved", "namespace_package", current)
            if dynamic:
                emit("unresolved", "dynamic_exports", current)
            if not matches and not children and not dynamic:
                emit("unresolved", "name_not_declared" if target is not None else "namespace_package", current)

    def follow(name, node, bound, steps, seen):
        identity = (name, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, bound)
        if identity in seen:
            emit("cycle", "cycle", steps)
            return
        seen = seen | {identity}
        aliases = [alias for alias in node.names if (alias.asname or (
            alias.name.split(".", 1)[0] if isinstance(node, ast.Import) else alias.name)) == bound]
        if not aliases:
            emit("unresolved", "import_alias_unavailable", steps)
        for alias in aliases:
            if alias.name == "*":
                emit("unresolved", "wildcard_import", steps)
                continue
            requested = (alias.name if isinstance(node, ast.Import) else node.module or "")
            partial = isinstance(node, ast.Import) and alias.asname is None and "." in alias.name
            if partial:
                requested = alias.name.split(".", 1)[0]
            choices = bases(name, node, requested)
            if not choices:
                emit("unresolved", "relative_escape", steps)
                continue
            found = False
            for base in choices:
                places = locations(base)
                if not places:
                    continue
                found = True
                if isinstance(node, ast.ImportFrom):
                    member(base, alias.name, steps, seen)
                else:
                    for target, _package in places:
                        if target is None:
                            emit("unresolved", "namespace_package", steps)
                        else:
                            load(target)
                            emit("module", "imported_submodule_not_attribute_resolution" if partial else None,
                                append(steps, step(target, requested, "module")))
            if not found:
                emit("unresolved", "not_in_snapshot", steps)

    try:
        _tree, lines, nodes = load(path)
        matches = []
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                kind, names, first, last = _declaration(node, lines)
                if (kind == candidate["kind"] and candidate["name"] in names
                        and (first, last) == (candidate["start_line"], candidate["end_line"])):
                    matches.append(node)
        if not matches:
            raise ValueError("import declaration differs from retained source")
        initial = step(path, candidate["name"], candidate["kind"],
            candidate["start_line"], candidate["end_line"], candidate["conditional"])
        for node in matches:
            follow(path, node, candidate["name"], [initial], frozenset())
        return _result(routes=routes)
    except _Refusal as exc:
        return _result(exc.status, exc.reason)
