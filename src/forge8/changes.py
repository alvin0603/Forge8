"""Deterministic navigation between two already-admitted UTF-8 source maps.

No Git, I/O, source execution, rename detection or behavior/caller inference.
Definition pairs mean only the same path, kind and qualified lexical name.
Hunks use ZERO-based start + count boundaries (zero count is not a citation).
Definition ranges use existing ONE-based inclusive source lines. All strings are
untrusted metadata; consumers must render text, never interpret them as HTML.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import json
from typing import Mapping

from .discovery import _python_outline

MAX_CATALOGUE_BYTES = 256 * 1024  # json.dumps(..., ensure_ascii=False), UTF-8.
MAX_CHANGE_ITEMS = 300  # Combined hunks + definition units, not per file.
_MAX_FILES = 1000
_DIFF_CHARS = 64 * 1024
_DIFF_LINES = 2000
_DIFF_PAIRS = 250_000
_TOTAL_DIFF_PAIRS = 1_000_000


class ChangeCatalogueError(ValueError):
    """The complete minimal inventory cannot fit; never return a partial list."""


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _span(old_start: int, old_end: int, new_start: int, new_end: int) -> dict:
    return {"before": {"start": old_start, "count": old_end - old_start},
        "after": {"start": new_start, "count": new_end - new_start}}


def _coarse(before: list[str], after: list[str]) -> list[dict]:
    """Linear enclosing span; unchanged lines inside it are not claimed changed."""
    prefix, common = 0, min(len(before), len(after))
    while prefix < common and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while suffix < common - prefix and before[-suffix - 1] == after[-suffix - 1]:
        suffix += 1
    if prefix == len(before) == len(after):
        return []
    return [_span(prefix, len(before) - suffix, prefix, len(after) - suffix)]


def _range(item: dict | None) -> dict | None:
    if item is None:
        return None
    return {"start_line": item["start_line"], "end_line": item["end_line"]}


def _definition_units(path: str, old: str | None, new: str | None,
                      before: list[str], after: list[str]) -> tuple[list[dict], list[str]]:
    groups = []
    for side, text in (("before", old), ("after", new)):
        outline = {"status": "available", "items": []} if text is None else _python_outline(path, text)
        if outline["status"] != "available":
            return [], ["outline_" + side + "_" + outline["reason"]]
        grouped: dict[tuple[str, str], list[dict]] = {}
        for item in outline["items"]:
            grouped.setdefault((item["kind"], item["name"]), []).append(item)
        groups.append(grouped)
    result = []
    for kind, name in sorted(groups[0].keys() | groups[1].keys()):
        left, right = groups[0].get((kind, name), []), groups[1].get((kind, name), [])
        old_bodies = [before[item["start_line"]-1:item["end_line"]] for item in left]
        new_bodies = [after[item["start_line"]-1:item["end_line"]] for item in right]
        if old_bodies == new_bodies:
            continue  # Moving unchanged text alone does not make a changed unit.
        if len(left) > 1 or len(right) > 1:
            return [], ["ambiguous_definitions"]
        if any(item["stub"] for item in left + right):
            return [], ["stub_definitions"]
        before_item, after_item = left[0] if left else None, right[0] if right else None
        result.append({"name": name, "kind": kind,
            "status": "added" if not left else "absent" if not right else "modified",
            "before": _range(before_item), "after": _range(after_item)})
    result.sort(key=lambda item: (min(side["start_line"] for side in
        (item["before"], item["after"]) if side is not None), item["kind"], item["name"]))
    return result, []


def _details(path: str, old: str | None, new: str | None,
             before: list[str], after: list[str], work_left: int) -> tuple[dict, int]:
    pairs = len(before) * len(after)
    if (len(old or "") + len(new or "") > _DIFF_CHARS
            or len(before) + len(after) > _DIFF_LINES
            or pairs > min(_DIFF_PAIRS, work_left)):
        return {"hunks": _coarse(before, after), "units": [], "detail": {
            "hunks": "coarse", "units": "unavailable", "reasons": ["diff_work_limit"]}}, 0
    # Work is admitted even for repetitive input; autojunk cannot hide repetition.
    hunks = [_span(i, j, k, end) for tag, i, j, k, end in
        SequenceMatcher(None, before, after, autojunk=False).get_opcodes() if tag != "equal"]
    units, reasons = _definition_units(path, old, new, before, after)
    return {"hunks": hunks, "units": units, "detail": {"hunks": "exact",
        "units": "unavailable" if reasons else "available", "reasons": reasons}}, pairs


def build_change_catalogue(before: Mapping[str, str], after: Mapping[str, str]) -> dict:
    """Keep every changed path or fail explicitly; never silently omit changes.

    Inputs are admitted original-path maps, not a request to read those paths.
    Absence means absent from that input map, not a proven intentional deletion.
    File/metadata admission failures raise ChangeCatalogueError with a fixed code;
    an unavailable catalogue is NOT an empty/no-changes result.
    """
    if len(before) > _MAX_FILES or len(after) > _MAX_FILES:
        raise ChangeCatalogueError("file_limit")
    paths = set(before) | set(after)
    if len(paths) > _MAX_FILES:
        raise ChangeCatalogueError("file_limit")
    if any(type(path) is not str or not path for path in paths) or any(
            type(text) is not str for source in (before, after) for text in source.values()):
        raise ChangeCatalogueError("source_map_shape")
    files = []
    for path in sorted(paths):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        old_lines, new_lines = (old or "").splitlines(), (new or "").splitlines()
        files.append({"path": path,
            "status": "added" if old is None else "absent" if new is None else "modified",
            "before_lines": len(old_lines), "after_lines": len(new_lines),
            "line_endings_only": old is not None and new is not None and old_lines == new_lines,
            "hunks": [], "units": [], "detail": {"hunks": "unavailable",
                "units": "unavailable", "reasons": ["catalogue_limit"]}})
    result = {"schema_version": 1, "kind": "source_change_catalogue",
        "semantics_verified": False, "files": files, "detail_limited": True}
    used = _size(result) + 1  # A final true -> false boolean costs one more byte.
    if used > MAX_CATALOGUE_BYTES:
        raise ChangeCatalogueError("path_inventory_limit")
    items_left, work_left, next_unit = MAX_CHANGE_ITEMS, _TOTAL_DIFF_PAIRS, 1
    for index, entry in enumerate(files):
        path = entry["path"]
        old, new = before.get(path), after.get(path)
        old_lines, new_lines = (old or "").splitlines(), (new or "").splitlines()
        if entry["line_endings_only"]:
            detail = {"hunks": [], "units": [], "detail": {
                "hunks": "exact", "units": "available", "reasons": ["line_endings_only"]}}
        elif not items_left:
            continue
        else:
            detail, work = _details(path, old, new, old_lines, new_lines, work_left)
            work_left -= work
        for offset, unit in enumerate(detail["units"]):
            unit["id"] = "U" + str(next_unit + offset)
        if len(detail["hunks"]) + len(detail["units"]) > items_left:
            detail = {"hunks": _coarse(old_lines, new_lines), "units": [], "detail": {
                "hunks": "coarse", "units": "unavailable", "reasons": ["catalogue_item_limit"]}}
        candidate = {**entry, **detail}
        difference = _size(candidate) - _size(entry)
        if used + difference > MAX_CATALOGUE_BYTES:
            candidate = {**entry, "hunks": _coarse(old_lines, new_lines), "units": [],
                "detail": {"hunks": "coarse", "units": "unavailable", "reasons": ["catalogue_byte_limit"]}}
            difference = _size(candidate) - _size(entry)
        count = len(candidate["hunks"]) + len(candidate["units"])
        if count <= items_left and used + difference <= MAX_CATALOGUE_BYTES:
            files[index] = candidate
            used += difference
            items_left -= count
            next_unit += len(candidate["units"])
    result["detail_limited"] = any(entry["detail"]["hunks"] != "exact"
        or entry["detail"]["units"] != "available" for entry in files)
    return result
