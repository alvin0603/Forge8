"""Bounded same-file lexical binding navigation, without executing source.

Compiler scope classification is not a reaching-definition or runtime-value
proof. Imports remain declarations, not their targets' implementations. Dynamic
names, attributes and unsupported compiler scopes are never guessed by spelling.
"""

from __future__ import annotations

import ast
import json
import symtable
import sys
from dataclasses import dataclass, field
from pathlib import PurePath

_MAX_SOURCE_BYTES = 256 * 1024
_MAX_NODES = 50_000
_MAX_NAMES = 64
_MAX_CANDIDATES = 64
_MAX_BINDINGS = 64
_MAX_METADATA_BYTES = 64 * 1024
_MAX_SELECTION_LINES = 80
_SCOPE = "same-file lexical scopes"
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _result(status: str, reason: str | None = None) -> dict:
    return {"status": status, "reason": reason, "candidates": [], "bindings": [],
        "semantics_verified": False, "scope": _SCOPE}


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _target_names(targets: list[ast.AST]) -> list[str]:
    """Only names assigned by this statement, not obj.attr or mapping[key]."""
    names = set()
    stack = list(targets)
    while stack:
        target = stack.pop()
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            stack.extend(target.elts)
        elif isinstance(target, ast.Starred):
            stack.append(target.value)
    return sorted(names)


def _declaration(node: ast.AST, lines: list[str]) -> tuple[str, list[str], int, int] | None:
    if isinstance(node, ast.Assign):
        kind, names = "assignment", _target_names(node.targets)
    elif isinstance(node, ast.AnnAssign):
        kind, names = "annotated assignment", _target_names([node.target])
    elif isinstance(node, ast.Import):
        kind = "import"
        names = sorted({item.asname or item.name.split(".", 1)[0] for item in node.names})
    elif isinstance(node, ast.ImportFrom):
        kind = "from import"
        names = sorted({item.asname or item.name for item in node.names if item.name != "*"})
    elif isinstance(node, _DEFINITIONS):
        kind = ("class" if isinstance(node, ast.ClassDef) else
            "async function" if isinstance(node, ast.AsyncFunctionDef) else "function")
        names = [node.name]
    else:
        return None
    first = node.lineno
    if isinstance(node, _DEFINITIONS) and node.decorator_list:
        first = min(decorator.lineno for decorator in node.decorator_list)
        # AST expression coordinates inside @(...) can begin below the @ line.
        # Same physical-line rule used by the existing Python outline.
        while first > 1 and not lines[first - 1].lstrip().startswith("@"):
            first -= 1
    return kind, names, first, node.end_lineno



@dataclass(eq=False)
class _Scope:
    table: object
    name: str
    line: int
    kind: str
    parent: "_Scope | None" = None
    private: str | None = None
    declarations: dict = field(default_factory=dict)
    cautions: dict = field(default_factory=dict)
    children: dict | None = None
    comprehension: bool = False
    inlined: bool = False
    targets: set = field(default_factory=set)


def _symbol_name(scope: _Scope, name: str) -> str:
    # Python private-name mangling applies to names throughout a class's lexical
    # contents, including its methods, but not to dunder names.
    if scope.private and name.startswith("__") and not name.endswith("__"):
        private = scope.private.lstrip("_")
        if private:
            return "_" + private + name
    return name


def _table_type(table) -> str:
    value = table.get_type()
    return getattr(value, "value", value)  # 3.10 strings; 3.13+ string enums.


def _scoped_context(tree: ast.AST, table, lines: list[str], start: int,
        end: int, include_selected: bool) -> dict:
    module = _Scope(table, "<module>", 1, "module")
    uses = {}

    def child_scope(node, parent, kind, table_name, display_name=None):
        if parent.children is None:
            parent.children = {}
            if parent.table is not None:
                for child in parent.table.get_children():
                    identity = (_table_type(child), child.get_name(), child.get_lineno())
                    parent.children.setdefault(identity, []).append(child)
        children = parent.children.get((kind, table_name, node.lineno), ())
        # Multiple anonymous scopes can share a line. The public table API has no
        # columns; never claim that source containment disambiguates them.
        matched = children[0] if len(children) == 1 else None
        name = display_name or table_name
        qualified = name if parent is module else parent.name + "." + name
        scope = _Scope(matched, qualified, node.lineno, kind, parent,
            node.name if isinstance(node, ast.ClassDef) else parent.private)
        if (not children and parent.table is not None
                and table_name in {"listcomp", "setcomp", "dictcomp"}
                and sys.implementation.name == "cpython" and sys.version_info >= (3, 12)):
            # PEP 709 removes these compiler child tables, but not their lexical
            # target isolation. Their remaining children are spliced into the
            # actual enclosing table. Do not apply this to ambiguous matches,
            # generators or unknown parent scopes.
            scope.table, scope.children, scope.inlined = parent.table, parent.children, True
        return scope

    def annotation_scope(node, parent):
        scope = _Scope(None, parent.name + ".<annotation>", node.lineno,
            "annotation", parent, parent.private)
        return scope

    def inline_owner(owner, key):
        while owner is not None:
            if owner.table is None:
                return "unresolved", (), "scope_mapping", key
            if owner.comprehension:
                if key in owner.targets:
                    return "free", (owner,), None, key
                owner = owner.parent
                continue
            if owner is module:
                return "module", (module,), None, key
            if owner.kind == "class":
                # A class does not pass its locals OR global declarations into
                # nested comprehensions. __class__ is the compiler's special
                # implicit cell, not an outer same-named local (PEP 3135).
                if key == "__class__":
                    return "unresolved", (), "implicit_class_cell", key
                owner = owner.parent
                continue
            try:
                symbol = owner.table.lookup(key)
            except KeyError:
                return "unresolved", (), "scope_mapping", key
            if owner.kind == "function":
                if symbol.is_global():
                    return "module", (module,), None, key
                if symbol.is_local():
                    # Inlining can add iteration-only locals to the parent
                    # table. A real outer binding needs an AST declaration or
                    # parameter, not that merged compiler flag alone.
                    if key in owner.declarations or symbol.is_parameter():
                        return "free", (owner,), None, key
                    return "unresolved", (), "scope_mapping", key
                if not (symbol.is_free() or symbol.is_nonlocal()):
                    return "unresolved", (), "scope_mapping", key
            else:
                return "unresolved", (), "scope_mapping", key
            owner = owner.parent
        return "unresolved", (), "scope_mapping", key

    def resolve(scope, name):
        key = _symbol_name(scope, name)
        if scope.kind == "annotation":
            return "annotation", (), "annotation_scope", key
        if scope.inlined:
            if key in scope.targets:
                return "local", (scope,), None, key
            return inline_owner(scope.parent, key)
        if scope.table is None:
            return "unresolved", (), "scope_mapping", key
        try:
            symbol = scope.table.lookup(key)
        except KeyError:
            return "unresolved", (), "scope_mapping", key
        if symbol.is_global():
            return "module", (module,), None, key
        if symbol.is_free() or symbol.is_nonlocal():
            owner = scope.parent
            while owner is not None:
                if owner.table is None:
                    return "unresolved", (), "scope_mapping", key
                if owner.kind == "class" and key == "__class__":
                    return "unresolved", (), "implicit_class_cell", key
                if owner.comprehension and key in owner.targets:
                    return "free", (owner,), None, key
                if owner.inlined:
                    owner = owner.parent
                    continue
                if owner.kind == "function" and owner.table is not None:
                    try:
                        enclosing = owner.table.lookup(key)
                    except KeyError:
                        enclosing = None
                    if enclosing is not None and enclosing.is_local():
                        return "free", (owner,), (
                            "class_namespace_lookup" if scope.kind == "class" else None), key
                owner = owner.parent
            return "unresolved", (), "scope_mapping", key
        if symbol.is_parameter():
            return "parameter", (scope,), None, key
        if symbol.is_local():
            if scope.kind == "class":
                # LOAD_NAME may fall back to module globals before a class-local
                # binding exists. Retain alternatives, not a runtime selection.
                return "class", (scope, module), "class_namespace_lookup", key
            return "local", (scope,), None, key
        return "unresolved", (), "scope_mapping", key

    def uncertain_write(scope, key):
        # A missing compiler scope cannot prove that a declaration is local:
        # explicit global/nonlocal writes may escape it. Preserve uncertainty
        # for every possible enclosing owner instead of creating fake uniqueness.
        while scope is not None:
            scope.cautions.setdefault(key, set()).add("unsupported_binding")
            scope = scope.parent

    def declaration(scope, name, kind, first, last, conditional, *, annotation=False):
        _classification, owners, _reason, key = resolve(scope, name)
        owner = owners[0] if owners else scope
        row = {"kind": kind, "start_line": first, "end_line": last,
            "conditional": bool(conditional or owner is not scope),
            "annotation_only": annotation}
        owner.declarations.setdefault(key, []).append(row)
        if scope.table is None and kind != "parameter" and not (
                scope.comprehension and kind == "loop target"):
            uncertain_write(scope, key)

    def caution(scope, name, reason):
        _classification, owners, _reason, key = resolve(scope, name)
        owner = owners[0] if owners else scope
        owner.cautions.setdefault(key, set()).add(reason)
        if scope.table is None:
            uncertain_write(scope, key)

    def use(node, scope, name):
        if start <= node.lineno <= node.end_lineno <= end:
            uses.setdefault((scope, name), set()).update(
                range(node.lineno, node.end_lineno + 1))

    def enqueue_annotation(node, parent, pending):
        if node is not None:
            pending.append((node, annotation_scope(node, parent), False))

    # No recursion into Python source or imports. This stack follows AST
    # evaluation scopes, not narrowest enclosing source coordinates.
    pending = [(tree, module, False)]
    while pending:
        node, scope, conditional = pending.pop()
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            use(node, scope, node.id)
        if isinstance(node, _DEFINITIONS):
            kind, names, first, last = _declaration(node, lines)
            declaration(scope, names[0], kind, first, last, conditional)
            inner = child_scope(node, scope,
                "class" if isinstance(node, ast.ClassDef) else "function", node.name)
            pending.extend((item, inner, False) for item in reversed(node.body))
            pending.extend((item, scope, conditional) for item in reversed(node.decorator_list))
            if isinstance(node, ast.ClassDef):
                if getattr(node, "type_params", ()):
                    for item in [*node.bases, *node.keywords, *node.type_params]:
                        enqueue_annotation(item, scope, pending)
                else:
                    pending.extend((item, scope, conditional)
                        for item in reversed([*node.bases, *node.keywords]))
            else:
                arguments = node.args
                for argument in [*arguments.posonlyargs, *arguments.args,
                        *arguments.kwonlyargs, arguments.vararg, arguments.kwarg]:
                    if argument is not None:
                        declaration(inner, argument.arg, "parameter",
                            argument.lineno, argument.end_lineno, False)
                        enqueue_annotation(argument.annotation, scope, pending)
                pending.extend((item, scope, conditional) for item in reversed(
                    [*arguments.defaults, *(item for item in arguments.kw_defaults if item is not None)]))
                enqueue_annotation(node.returns, scope, pending)
                for item in getattr(node, "type_params", ()):
                    enqueue_annotation(item, scope, pending)
            continue
        if isinstance(node, ast.Lambda):
            inner = child_scope(node, scope, "function", "lambda", "<lambda>")
            arguments = node.args
            for argument in [*arguments.posonlyargs, *arguments.args,
                    *arguments.kwonlyargs, arguments.vararg, arguments.kwarg]:
                if argument is not None:
                    declaration(inner, argument.arg, "parameter",
                        argument.lineno, argument.end_lineno, False)
            pending.append((node.body, inner, False))
            pending.extend((item, scope, conditional) for item in reversed(
                [*arguments.defaults, *(item for item in arguments.kw_defaults if item is not None)]))
            continue
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            table_name = {ast.ListComp: "listcomp", ast.SetComp: "setcomp",
                ast.DictComp: "dictcomp", ast.GeneratorExp: "genexpr"}[type(node)]
            inner = child_scope(node, scope, "function", table_name, "<" + table_name + ">")
            inner.comprehension = True
            inner.targets = {_symbol_name(inner, name) for generator in node.generators
                for name in _target_names([generator.target])}
            values = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            pending.extend((item, inner, True) for item in reversed(values))
            for index, generator in reversed(list(enumerate(node.generators))):
                for name in _target_names([generator.target]):
                    declaration(inner, name, "loop target", generator.target.lineno,
                        generator.target.end_lineno, True)
                pending.extend((item, inner, True) for item in reversed(generator.ifs))
                pending.append((generator.target, inner, True))
                pending.append((generator.iter, scope if index == 0 else inner, conditional))
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom)):
            kind, names, first, last = _declaration(node, lines)
            if isinstance(node, ast.ImportFrom) and any(item.name == "*" for item in node.names):
                # The imported module is never inspected. Its unknown exports
                # can replace any module name, including an apparent singleton.
                module.cautions.setdefault("*", set()).add("unsupported_binding")
            for name in names:
                declaration(scope, name, kind, first, last, conditional,
                    annotation=isinstance(node, ast.AnnAssign) and node.value is None)
            if isinstance(node, ast.AnnAssign):
                enqueue_annotation(node.annotation, scope, pending)
                pending.append((node.target, scope, conditional))
                if node.value is not None:
                    pending.append((node.value, scope, conditional))
                continue
        elif isinstance(node, ast.AugAssign):
            for name in _target_names([node.target]):
                declaration(scope, name, "augmented assignment", node.lineno,
                    node.end_lineno, conditional)
                use(node.target, scope, name)  # AugAssign is also an implicit read.
        elif isinstance(node, ast.NamedExpr):
            owner = scope
            while owner.comprehension:
                owner = owner.parent
            for name in _target_names([node.target]):
                declaration(owner, name, "named assignment", node.lineno,
                    node.end_lineno, True)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for name in _target_names([node.target]):
                declaration(scope, name, "loop target", node.target.lineno,
                    node.target.end_lineno, True)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    for name in _target_names([item.optional_vars]):
                        declaration(scope, name, "with target", item.optional_vars.lineno,
                            item.optional_vars.end_lineno, True)
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            declaration(scope, node.name, "exception target", node.lineno,
                node.end_lineno, True)
            # Exception target cleanup also unbinds the name after the handler.
            caution(scope, node.name, "unbinding")
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
            declaration(scope, node.name, "pattern target", node.lineno,
                node.end_lineno, True)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            declaration(scope, node.rest, "pattern target", node.lineno,
                node.end_lineno, True)
        elif isinstance(node, ast.Delete):
            for name in _target_names(node.targets):
                caution(scope, name, "unbinding")
        elif type(node).__name__ == "TypeAlias":
            for name in _target_names([node.name]):
                caution(scope, name, "unsupported_binding")
            for child in ast.iter_child_nodes(node):
                enqueue_annotation(child, scope, pending)
            continue
        child_conditional = conditional or isinstance(node, (ast.If, ast.For,
            ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match,
            ast.ExceptHandler)) or type(node).__name__ == "TryStar"
        pending.extend((child, scope, child_conditional)
            for child in reversed(list(ast.iter_child_nodes(node))))

    if len({name for _scope, name in uses}) > _MAX_NAMES:
        return _result("limited", "name_limit")
    if len(uses) > _MAX_BINDINGS:
        return _result("limited", "binding_limit")
    result = _result("available")
    for (scope, name), positions in uses.items():
        classification, owners, reason, key = resolve(scope, name)
        declarations = [row for owner in owners for row in owner.declarations.get(key, ())]
        cautions = {item for owner in owners for marker in (key, "*")
            for item in owner.cautions.get(marker, ())}
        if cautions:
            reason = "unbinding" if "unbinding" in cautions else "unsupported_binding"
        elif declarations and all(row["annotation_only"] for row in declarations):
            reason = "annotation_only"
        binding = {"id": "B" + str(len(result["bindings"]) + 1), "name": name,
            "classification": classification, "scope": scope.name,
            "scope_line": scope.line, "use_lines": sorted(positions), "reason": reason}
        result["bindings"].append(binding)
        for row in sorted(declarations, key=lambda item: (
                item["start_line"], item["end_line"], item["kind"])):
            first, last = row["start_line"], row["end_line"]
            if not include_selected and start <= first and last <= end:
                continue
            if len(result["candidates"]) >= _MAX_CANDIDATES:
                return _result("limited", "candidate_limit")
            result["candidates"].append({"name": name, "kind": row["kind"],
                "start_line": first, "end_line": last, "use_lines": sorted(positions),
                "conditional": row["conditional"], "binding_id": binding["id"]})
    result["candidates"].sort(key=lambda row: (row["start_line"], row["end_line"], row["binding_id"]))
    if _json_size(result) > _MAX_METADATA_BYTES:
        return _result("limited", "metadata_limit")
    return result


def selected_context(path: str, text: str, start: int, end: int, *,
        include_selected: bool = False) -> dict:
    """Return compiler-classified bindings and possible declaration coordinates.

    start/end are one-based inclusive source lines, at most 80. Invalid types or
    ordinary out-of-range selections raise ValueError. Source/AST/result limits
    return no partial candidates. Oversized source is refused before splitting or
    parsing; it is not inspected further to establish its final line count.

    References are grouped by use scope and spelling, not just spelling. They
    include implicit augmented-assignment reads. Defaults/decorators and the
    outermost comprehension iterable use their enclosing scopes. Annotation and
    unalignable compiler scopes are explicitly unresolved rather than guessed.
    By default whole declarations already selected are omitted; bindings remain.
    include_selected=True also retains those declaration alternatives. Neither
    option executes source, proves a runtime binding or adds model context.
    """
    if (type(path) is not str or not path or "\x00" in path or type(text) is not str
            or type(start) is not int or type(end) is not int
            or type(include_selected) is not bool
            or start < 1 or end < start or end - start + 1 > _MAX_SELECTION_LINES):
        raise ValueError("invalid source selection")
    if len(text) > _MAX_SOURCE_BYTES:
        return _result("limited", "source_limit")
    try:
        if len(text.encode("utf-8")) > _MAX_SOURCE_BYTES:
            return _result("limited", "source_limit")
    except UnicodeEncodeError:
        return _result("unavailable", "utf8")
    lines = text.splitlines()
    if end > len(lines):
        raise ValueError("source selection exceeds available lines")
    if PurePath(path).suffix.lower() not in {".py", ".pyi"}:
        return _result("unsupported", "language")
    if any(separator in text for separator in "\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
        return _result("unavailable", "line_separators")
    try:
        tree = ast.parse(text)
    except RecursionError:
        return _result("unavailable", "parse_depth")
    except (SyntaxError, ValueError):
        return _result("unavailable", "syntax_or_version")

    stack, visited = [tree], 0
    while stack:
        node = stack.pop()
        visited += 1
        if visited > _MAX_NODES:
            return _result("limited", "node_limit")
        stack.extend(reversed(list(ast.iter_child_nodes(node))))

    # AST size is checked before asking the compiler's symbol-table frontend to
    # inspect source. No target bytecode is generated, imported or executed.
    try:
        table = symtable.symtable(text, path, "exec")
    except RecursionError:
        return _result("unavailable", "parse_depth")
    except (SyntaxError, ValueError):
        return _result("unavailable", "syntax_or_version")
    return _scoped_context(tree, table, lines, start, end, include_selected)
