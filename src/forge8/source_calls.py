"""Bounded, on-demand Python call spelling navigation over retained raw bytes.

No import resolution, caller attribution, target execution or persistent index.
Coordinates refer to original UTF-8 bytes, including a leading BOM when present.
"""
from __future__ import annotations

import ast
from bisect import insort
import hashlib
import json
import keyword
from pathlib import PurePosixPath
import unicodedata


_MAX_SOURCE_BYTES = 256 * 1024
_MAX_FILE_NODES = 50_000
_MAX_QUERY_NODES = 500_000
_MAX_MATCHES = 40
_NONPHYSICAL = "\v\f\x1c\x1d\x1e\x85\u2028\u2029"


class ScanStopped(Exception):
    """Cooperative stop; results from the unfinished file are discarded."""

    def __init__(self, reason: str):
        if reason not in {"time_limit", "node_budget"}:
            raise ValueError("unknown call scan stop reason")
        self.reason = reason
        super().__init__(reason)


class _Skipped(Exception):
    pass


def _position(node, lines: list[bytes], bom: int):
    first, start = getattr(node, "lineno", None), getattr(node, "col_offset", None)
    last, end = getattr(node, "end_lineno", None), getattr(node, "end_col_offset", None)
    if (any(type(value) is not int for value in (first, start, last, end))
            or not 1 <= first <= last <= len(lines) or start < 0 or end < 0):
        raise _Skipped("coordinates")
    start += bom if first == 1 else 0
    end += bom if last == 1 else 0
    if ((first, start) >= (last, end) or start > len(lines[first - 1].rstrip(b"\r\n"))
            or end > len(lines[last - 1].rstrip(b"\r\n"))):
        raise _Skipped("coordinates")
    try:
        lines[first - 1][:start].decode("utf-8")
        lines[last - 1][:end].decode("utf-8")
    except UnicodeError:
        raise _Skipped("coordinates") from None
    return first, start, last, end


def _preview(raw: bytes, offsets: list[int], coordinates: tuple[int, ...]) -> str:
    first, start, last, end = coordinates[:4]
    begin, finish = offsets[first - 1] + start, offsets[last - 1] + end
    # At most 240 codepoints can fit; 960 raw bytes suffice even for four-byte
    # UTF-8. Ignore only an incomplete final codepoint of this bounded prefix.
    limit = min(finish, begin + 960)
    source = raw[begin:limit].decode("utf-8", errors="ignore")
    pieces, size, clipped = [], 0, limit < finish
    for character in source:
        piece = character if character.isprintable() else json.dumps(character)[1:-1]
        if size + len(piece) > 240:
            clipped = True
            break
        pieces.append(piece)
        size += len(piece)
    if clipped:
        while size > 239:
            size -= len(pieces.pop())
        pieces.append("…")
    return "".join(pieces)


def call_occurrences(query, files, read, checkpoint) -> dict:
    """Find exact raw terminal spellings in completed retained Python files.

    read(file) must return admitted raw bytes and their pinned SHA-256; its errors
    propagate. checkpoint() may refuse stale/closed state or raise ScanStopped.
    A skipped or interrupted file contributes no matches. Coverage counts include
    non-Python files, and partial coverage is separate from result truncation.
    """
    if (type(query) is not str or not 1 <= len(query) <= 128
            or not query.isidentifier() or keyword.iskeyword(query)):
        raise ValueError("call name must be one Python identifier of at most 128 characters")
    if type(files) is not list or len(files) > 1000 or not callable(read) or not callable(checkpoint):
        raise ValueError("expected a bounded admitted file inventory and read callbacks")
    inventory, identifiers, paths = [], set(), set()
    for file in files:
        if (type(file) is not dict or set(file) != {"id", "path", "lines"}
                or type(file["id"]) is not str or not 1 <= len(file["id"]) <= 200
                or type(file["path"]) is not str or not 1 <= len(file["path"]) <= 1024
                or type(file["lines"]) is not int or not 0 <= file["lines"] <= _MAX_SOURCE_BYTES
                or file["id"] in identifiers or file["path"] in paths):
            raise ValueError("invalid admitted call-search file")
        path = file["path"]
        if ("\\" in path or ":" in path or "\x00" in path or path.startswith("/")
                or PurePosixPath(path).as_posix() != path
                or any(part in {".", ".."} for part in path.split("/"))):
            raise ValueError("call-search paths must be admitted relative paths")
        inventory.append(dict(file))
        identifiers.add(file["id"])
        paths.add(path)
    query_bytes = query.encode("utf-8")
    normalized = unicodedata.normalize("NFKC", query)
    result = {"query": query, "column_unit": "utf8_bytes", "semantics_verified": False,
        "matches": [], "total_matches": 0, "truncated": False, "inspected_files": 0,
        "skipped_files": {}, "uninspected_files": 0, "total_files": len(inventory), "stop_reason": None}
    visited_total = 0
    for index, file in enumerate(inventory):
        try:
            checkpoint()
            if PurePosixPath(file["path"]).suffix.lower() not in {".py", ".pyi"}:
                raise _Skipped("not_python")
            raw, digest = read(dict(file))  # Never catch callback/integrity errors.
            if (type(raw) is not bytes or type(digest) is not str or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)):
                raise ValueError("retained call-search input identity is invalid")
            if len(raw) > _MAX_SOURCE_BYTES:
                raise _Skipped("source_size")
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("retained call-search bytes differ from their source identity")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeError:
                raise _Skipped("encoding") from None
            if any(c in text for c in _NONPHYSICAL):
                raise _Skipped("nonphysical_lines")
            lines = raw.splitlines(keepends=True)
            if len(lines) != file["lines"]:
                raise _Skipped("coordinates")
            try:
                tree = ast.parse(text, filename=file["path"])
            except (SyntaxError, ValueError, RecursionError):
                raise _Skipped("syntax") from None
            bom = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
            pending, visited, count, retained = [tree], 0, 0, []
            capacity = _MAX_MATCHES - len(result["matches"])
            while pending:
                if visited % 256 == 0:
                    checkpoint()
                if visited_total >= _MAX_QUERY_NODES:
                    raise ScanStopped("node_budget")
                node = pending.pop()
                visited += 1
                visited_total += 1
                if visited > _MAX_FILE_NODES:
                    raise _Skipped("node_limit")
                pending.extend(reversed(list(ast.iter_child_nodes(node))))
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if isinstance(function, ast.Name):
                    name, kind = function.id, "name"
                elif isinstance(function, ast.Attribute):
                    name, kind = function.attr, "attribute"
                else:
                    continue  # Aliases and computed callable expressions are not followed.
                if name != normalized:
                    continue
                function_span = _position(function, lines, bom)
                name_line, name_end = function_span[2:]
                name_start = name_end - len(query_bytes)
                if name_start < 0 or lines[name_line - 1][name_start:name_end] != query_bytes:
                    continue  # AST normalization alone cannot establish exact spelling.
                if kind == "name" and function_span[:2] != (name_line, name_start):
                    continue
                span = _position(node, lines, bom)
                if not span[:2] <= function_span[:2] < function_span[2:] <= span[2:]:
                    raise _Skipped("coordinates")
                coordinates = (*span, name_line, name_start)
                count += 1
                if capacity:
                    insort(retained, (coordinates, count, kind))
                    if len(retained) > capacity:
                        retained.pop()
            checkpoint()
            offsets, offset = [], 0
            for line in lines:
                offsets.append(offset)
                offset += len(line)
            rows = []
            for coordinates, _number, kind in retained:
                first, start, last, end, name_line, name_column = coordinates
                rows.append({"file": file["id"], "path": file["path"], "source_sha256": digest,
                    "kind": kind, "name": query, "start_line": first, "start_column": start,
                    "end_line": last, "end_column": end, "name_line": name_line, "name_column": name_column,
                    "preview": _preview(raw, offsets, coordinates)})
            checkpoint()
            result["matches"].extend(rows)
            result["total_matches"] += count
            result["inspected_files"] += 1
        except _Skipped as exc:
            reason = str(exc)
            result["skipped_files"][reason] = result["skipped_files"].get(reason, 0) + 1
        except ScanStopped as exc:
            result["stop_reason"] = exc.reason
            result["uninspected_files"] = len(inventory) - index
            break
        finally:
            # A limit may leave pending nodes; do not keep that AST alive while
            # parsing the next file. Retained rows contain only scalar metadata.
            tree = pending = node = function = None
    result["truncated"] = result["total_matches"] > len(result["matches"])
    return result
