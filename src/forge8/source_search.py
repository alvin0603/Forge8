"""Bounded lexical definition search over the desk's existing retained cache.

No parser, persistent index, import resolution or model is involved. A source
hit means a word occurs in the innermost definition's own lexical text, including
comments and strings; it does not establish behavior or runtime name binding.
"""
from __future__ import annotations

from bisect import insort
import json
import re

from .explain import _bounded_text


_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_ACRONYM = re.compile(r"([A-Z])([A-Z][a-z])")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_FUNCTION_KINDS = {"function", "async function"}
_MAX_MATCHES = 40
_MAX_RESPONSE_BYTES = 64 * 1024


def _words(text: str):
    for match in _WORDS.finditer(text):
        separated = _CAMEL.sub(r"\1 \2", _ACRONYM.sub(r"\1 \2", match.group()))
        yield from (part.casefold() for part in separated.split())


def keyword_definitions(query: str, files: list[dict], contents: dict,
                        outlines: dict) -> dict:
    """Return all-term lexical candidates, ranked without guessing relevance.

    Inputs are the desk's bounded cached outlines and display lines, not live
    paths. Nested definitions (including classes) exclude their entire lexical
    intervals from outer source attribution. Each file has one interval sweep;
    only first source occurrences and the best forty result rows are retained.
    """
    _bounded_text(query, "definition keywords", 128)
    terms = list(dict.fromkeys(_words(query)))
    if not 1 <= len(terms) <= 8:
        raise ValueError("definition keywords must contain 1-8 unique word terms")
    best, total, unavailable = [], 0, 0
    for file in files:
        identifier, path = file["id"], file["path"]
        if not path.lower().endswith((".py", ".pyi")):
            continue
        outline = outlines[identifier]
        if outline["status"] != "available":
            unavailable += outline["status"] in {"unavailable", "limited"}
            continue
        lines, items = contents[identifier], outline["items"]
        path_words = set(_words(path))
        reasons, missing = [], []
        for item in items:
            start, definition, end = (item[key] for key in
                                      ("start_line", "definition_line", "end_line"))
            if (any(type(value) is not int for value in (start, definition, end))
                    or not 1 <= start <= definition <= end <= len(lines)
                    or item["kind"] not in _FUNCTION_KINDS | {"class"}):
                raise ValueError("cached definition coordinates or kind are invalid")
            name_words = set(_words(item["name"]))
            hits = {term: {"term": term, "field": "name" if term in name_words else "path",
                           "line": None}
                    for term in terms if term in name_words or term in path_words}
            reasons.append(hits)
            missing.append(set(terms) - hits.keys())

        ordered = sorted(range(len(items)), key=lambda index:
                         (items[index]["start_line"], -items[index]["end_line"],
                          items[index]["definition_line"]))
        active, cursor = [], 0
        for number, text in enumerate(lines, 1):
            while active and items[active[-1]]["end_line"] < number:
                active.pop()
            while cursor < len(ordered) and items[ordered[cursor]]["start_line"] == number:
                index = ordered[cursor]
                if active and (items[index]["start_line"] == items[active[-1]]["start_line"]
                               or items[index]["end_line"] > items[active[-1]]["end_line"]):
                    raise ValueError("cached definition intervals overlap ambiguously")
                active.append(index)
                cursor += 1
            if not active:
                continue
            owner = active[-1]
            if items[owner]["kind"] not in _FUNCTION_KINDS or not missing[owner]:
                continue  # A class is a barrier, not a source-bearing result.
            for term in _words(text):
                if term in missing[owner]:
                    reasons[owner][term] = {"term": term, "field": "source", "line": number}
                    missing[owner].remove(term)

        for index, item in enumerate(items):
            if item["kind"] not in _FUNCTION_KINDS or missing[index]:
                continue
            hits = [reasons[index][term] for term in terms]
            source_lines = [hit["line"] for hit in hits if hit["field"] == "source"]
            preview_line = min(source_lines) if source_lines else item["definition_line"]
            row = {"file": identifier, "path": path, **item, "matched_terms": hits,
                   "preview": {"line": preview_line, "text": lines[preview_line - 1][:240]}}
            rank = (-sum(hit["field"] == "name" for hit in hits),
                    -sum(hit["field"] == "path" for hit in hits),
                    item["end_line"] - item["start_line"] + 1, path, item["definition_line"])
            total += 1
            insort(best, (rank, total, row))  # Counter prevents comparing result dictionaries.
            if len(best) > _MAX_MATCHES:
                best.pop()
    result = {"terms": terms, "matches": [row for _, _, row in best],
              "truncated": total > _MAX_MATCHES, "unavailable_files": unavailable,
              "total_matches": total}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise ValueError("definition keyword response exceeds 65536 bytes; narrow the query")
    return result
