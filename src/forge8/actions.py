"""Strict, deliberation-first action envelopes for small local models.

The order in :data:`ACTION_ENVELOPE_SCHEMA` is intentional. Constrained JSON
generation visits ``rationale`` before ``action`` so a model deliberates before
committing to an effect. Effect-bearing fields, especially write ``content``, are
placed last inside their action variant.

This module only parses and validates model output. It never executes an action.
The Python validator is authoritative even when a backend claims to enforce the
JSON Schema.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal, TypeAlias


MAX_ENVELOPE_CHARS = 300_000
MAX_RATIONALE_CHARS = 2_000
# The constrained llama.cpp wire contract is deliberately tighter than the
# stable public API. This stops a failing small-model loop before it spends the
# rest of its generation budget expanding rationale that cannot aid recovery.
MAX_LLAMA_CPP_RATIONALE_CHARS = 1_200
MAX_PATH_CHARS = 512
MAX_LIST_FILES = 1_000
MAX_QUERY_CHARS = 512
MAX_SEARCH_RESULTS = 500
MAX_LINE_NUMBER = 1_000_000
MAX_READ_LINES = 400
MAX_WRITE_BYTES = 256 * 1024
MAX_CHECKS = 8
MAX_CHECK_ID_CHARS = 64
MAX_CHECK_TIMEOUT_SECONDS = 120
MAX_CHECK_OUTPUT_CHARS = 16_000
MAX_FINISH_SUMMARY_CHARS = 4_000

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_SHA256_OR_MISSING = re.compile(r"^(?:[0-9A-Fa-f]{64}|missing)$")
_CHECK_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ActionEnvelopeError(ValueError):
    """Base class for rejected model action output."""


class ActionJSONError(ActionEnvelopeError):
    """The model output was not one strict JSON document."""


class ActionValidationError(ActionEnvelopeError):
    """The JSON document did not satisfy the action contract."""


@dataclass(frozen=True, slots=True)
class ListFilesAction:
    path: str
    limit: int
    kind: Literal["list_files"] = field(default="list_files", init=False)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "path": self.path, "limit": self.limit}


@dataclass(frozen=True, slots=True)
class ReadTextAction:
    path: str
    start_line: int
    end_line: int
    kind: Literal["read_text"] = field(default="read_text", init=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


@dataclass(frozen=True, slots=True)
class SearchTextAction:
    query: str
    limit: int
    kind: Literal["search_text"] = field(default="search_text", init=False)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "query": self.query, "limit": self.limit}


@dataclass(frozen=True, slots=True)
class ReplaceLinesAction:
    """A guarded proposal to replace one inclusive line range."""

    path: str
    expected_sha256: str
    start_line: int
    end_line: int
    new_text: str
    kind: Literal["replace_lines"] = field(default="replace_lines", init=False)

    def as_dict(self) -> dict[str, Any]:
        # The replacement effect is deliberately last, after the target,
        # whole-file guard, and bounded source coordinates.
        return {
            "kind": self.kind,
            "path": self.path,
            "expected_sha256": self.expected_sha256,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "new_text": self.new_text,
        }


@dataclass(frozen=True, slots=True)
class WriteTextAction:
    path: str
    expected_sha256: str
    content: str
    kind: Literal["write_text"] = field(default="write_text", init=False)

    def as_dict(self) -> dict[str, Any]:
        # Content is deliberately last: it is the irreversible model-generated
        # payload and should follow target selection and concurrency guard.
        return {
            "kind": self.kind,
            "path": self.path,
            "expected_sha256": self.expected_sha256,
            "content": self.content,
        }


@dataclass(frozen=True, slots=True)
class ReplaceTextAction:
    """A proposal to replace one exact text segment in an existing file."""

    path: str
    expected_sha256: str
    old_text: str
    new_text: str
    kind: Literal["replace_text"] = field(default="replace_text", init=False)

    def as_dict(self) -> dict[str, Any]:
        # The replacement effect is deliberately last, after target, guard,
        # and the exact evidence-bearing text that it is intended to replace.
        return {
            "kind": self.kind,
            "path": self.path,
            "expected_sha256": self.expected_sha256,
            "old_text": self.old_text,
            "new_text": self.new_text,
        }


@dataclass(frozen=True, slots=True)
class CheckBudget:
    timeout_seconds: int
    max_output_chars: int

    def as_dict(self) -> dict[str, int]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_output_chars": self.max_output_chars,
        }


@dataclass(frozen=True, slots=True)
class RunChecksAction:
    checks: tuple[str, ...]
    budget: CheckBudget
    kind: Literal["run_checks"] = field(default="run_checks", init=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "checks": list(self.checks),
            "budget": self.budget.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class FinishAction:
    summary: str
    kind: Literal["finish"] = field(default="finish", init=False)

    def as_dict(self) -> dict[str, str]:
        # A finish action reports state; it carries no filesystem or process
        # effect and cannot smuggle one through unused fields.
        return {"kind": self.kind, "summary": self.summary}


Action: TypeAlias = (
    ListFilesAction
    | ReadTextAction
    | SearchTextAction
    | ReplaceLinesAction
    | WriteTextAction
    | ReplaceTextAction
    | RunChecksAction
    | FinishAction
)


@dataclass(frozen=True, slots=True)
class ActionEnvelope:
    """One bounded model decision, with deliberation serialized first."""

    rationale: str
    action: Action

    def as_dict(self) -> dict[str, Any]:
        # Dict insertion order is part of the model-facing contract.
        return {"rationale": self.rationale, "action": self.action.as_dict()}

    def as_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
        )


def _string_schema(
    description: str,
    *,
    min_length: int = 0,
    max_length: int,
    pattern: str | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "string",
        "description": description,
        "minLength": min_length,
        "maxLength": max_length,
    }
    if pattern is not None:
        schema["pattern"] = pattern
    return schema


def _integer_schema(description: str, minimum: int, maximum: int) -> dict[str, Any]:
    return {
        "type": "integer",
        "description": description,
        "minimum": minimum,
        "maximum": maximum,
    }


def _object_variant(
    description: str,
    properties: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _build_action_envelope_schema() -> dict[str, Any]:
    path_schema = _string_schema(
        "Normalized POSIX path relative to the mounted workspace.",
        min_length=1,
        max_length=MAX_PATH_CHARS,
    )

    list_files = _object_variant(
        "List a bounded number of entries under a workspace directory.",
        {
            "kind": {"const": "list_files"},
            "path": copy.deepcopy(path_schema),
            "limit": _integer_schema("Maximum returned entries.", 1, MAX_LIST_FILES),
        },
    )
    read_text = _object_variant(
        "Read an inclusive, bounded line range from one UTF-8 workspace file.",
        {
            "kind": {"const": "read_text"},
            "path": copy.deepcopy(path_schema),
            "start_line": _integer_schema("First one-based line.", 1, MAX_LINE_NUMBER),
            "end_line": _integer_schema("Last one-based line, inclusive.", 1, MAX_LINE_NUMBER),
        },
    )
    search_text = _object_variant(
        "Search workspace text for one bounded literal query.",
        {
            "kind": {"const": "search_text"},
            "query": _string_schema(
                "Non-blank literal query.",
                min_length=1,
                max_length=MAX_QUERY_CHARS,
            ),
            "limit": _integer_schema("Maximum returned matches.", 1, MAX_SEARCH_RESULTS),
        },
    )
    replace_lines = _object_variant(
        "Replace one bounded inclusive line range in an existing UTF-8 file.",
        {
            "kind": {"const": "replace_lines"},
            "path": copy.deepcopy(path_schema),
            "expected_sha256": _string_schema(
                "Observed SHA-256 of the existing file.",
                min_length=64,
                max_length=64,
                pattern=r"^[0-9A-Fa-f]{64}$",
            ),
            "start_line": _integer_schema(
                "First one-based line to replace.", 1, MAX_LINE_NUMBER
            ),
            "end_line": _integer_schema(
                "Last one-based line to replace, inclusive.", 1, MAX_LINE_NUMBER
            ),
            # Keep the proposed replacement effect last.
            "new_text": _string_schema(
                "UTF-8 replacement text; empty means remove the selected lines.",
                max_length=MAX_WRITE_BYTES,
            ),
        },
    )
    write_text = _object_variant(
        "Atomically write UTF-8 text after an optimistic-concurrency guard.",
        {
            "kind": {"const": "write_text"},
            "path": copy.deepcopy(path_schema),
            "expected_sha256": _string_schema(
                "Observed SHA-256 for an existing file, or 'missing' for creation.",
                min_length=7,
                max_length=64,
                pattern=r"^(?:[0-9A-Fa-f]{64}|missing)$",
            ),
            # Keep the effect payload last. Do not reorder without a model
            # regression experiment and an explicit schema-order test update.
            "content": _string_schema(
                "Complete replacement UTF-8 content.",
                max_length=MAX_WRITE_BYTES,
            ),
        },
    )
    replace_text = _object_variant(
        "Propose replacing one uniquely matching text segment in an existing file.",
        {
            "kind": {"const": "replace_text"},
            "path": copy.deepcopy(path_schema),
            "expected_sha256": _string_schema(
                "Observed SHA-256 of the existing file.",
                min_length=64,
                max_length=64,
                pattern=r"^[0-9A-Fa-f]{64}$",
            ),
            "old_text": _string_schema(
                "Non-empty exact UTF-8 text expected to occur once.",
                min_length=1,
                max_length=MAX_WRITE_BYTES,
            ),
            # Keep the proposed replacement effect last.
            "new_text": _string_schema(
                "UTF-8 replacement text; empty means remove the matched text.",
                max_length=MAX_WRITE_BYTES,
            ),
        },
    )
    check_budget = {
        "type": "object",
        "description": "Hard resource budget for this check batch.",
        "additionalProperties": False,
        "required": ["timeout_seconds", "max_output_chars"],
        "properties": {
            "timeout_seconds": _integer_schema(
                "Total wall-time budget.", 1, MAX_CHECK_TIMEOUT_SECONDS
            ),
            "max_output_chars": _integer_schema(
                "Maximum captured output returned to model context.",
                1,
                MAX_CHECK_OUTPUT_CHARS,
            ),
        },
    }
    run_checks = _object_variant(
        "Run named, policy-declared checks; never arbitrary shell from this envelope.",
        {
            "kind": {"const": "run_checks"},
            "checks": {
                "type": "array",
                "description": "Unique check identifiers from the active task contract.",
                "minItems": 1,
                "maxItems": MAX_CHECKS,
                "uniqueItems": True,
                "items": _string_schema(
                    "Lowercase check identifier.",
                    min_length=1,
                    max_length=MAX_CHECK_ID_CHARS,
                    pattern=r"^[a-z][a-z0-9_]{0,63}$",
                ),
            },
            "budget": check_budget,
        },
    )
    finish = _object_variant(
        "Stop acting and summarize the verified or unresolved state without an effect.",
        {
            "kind": {"const": "finish"},
            "summary": _string_schema(
                "Concise final state and evidence summary.",
                min_length=1,
                max_length=MAX_FINISH_SUMMARY_CHARS,
            ),
        },
    )

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Forge8 ActionEnvelope",
        "type": "object",
        "description": (
            "Emit rationale first, then exactly one bounded action. "
            "The action is the last envelope property by design."
        ),
        "additionalProperties": False,
        "required": ["rationale", "action"],
        # Property order affects constrained autoregressive generation. Keep
        # rationale before the action/effect.
        "properties": {
            "rationale": _string_schema(
                "Deliberate on evidence, constraints, and the next smallest safe step.",
                min_length=1,
                max_length=MAX_RATIONALE_CHARS,
            ),
            "action": {
                "description": "Exactly one action selected after deliberation.",
                "oneOf": [
                    list_files,
                    read_text,
                    search_text,
                    replace_lines,
                    write_text,
                    replace_text,
                    run_checks,
                    finish,
                ],
            },
        },
    }


ACTION_ENVELOPE_SCHEMA: dict[str, Any] = _build_action_envelope_schema()


def action_envelope_schema() -> dict[str, Any]:
    """Return a defensive copy of the ordered model-facing JSON Schema."""

    return copy.deepcopy(ACTION_ENVELOPE_SCHEMA)


def _llama_cpp_schema_projection(value: Any) -> Any:
    """Project the authoritative schema onto llama.cpp's grammar subset.

    Pinned build b10621 accepts nested ``oneOf`` and integer bounds but rejects
    the full Draft 2020-12 document used by Forge8 validation. The projection
    keeps structure, required fields, enums, integer bounds, and deliberate
    property order. The strict Python parser remains authoritative for every
    constraint, including the keywords omitted here.
    """

    if isinstance(value, list):
        return [_llama_cpp_schema_projection(item) for item in value]
    if not isinstance(value, dict):
        return value
    projected: dict[str, Any] = {}
    if "const" in value:
        constant = value["const"]
        projected["type"] = (
            "string"
            if isinstance(constant, str)
            else "boolean"
            if isinstance(constant, bool)
            else "integer"
            if isinstance(constant, int)
            else "null"
            if constant is None
            else "string"
        )
        projected["enum"] = [constant]
    ignored = {
        "$schema",
        "title",
        "description",
        "const",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
    for key, item in value.items():
        if key in ignored:
            continue
        projected[key] = _llama_cpp_schema_projection(item)
    return projected


def _build_llama_cpp_wire_schema() -> dict[str, Any]:
    """Build the backend wire contract without changing the public action API.

    ``end_line`` asks a model to satisfy two independent bounds plus a relation
    to ``start_line``. Small models commonly emit either zero or an enormous
    sentinel for it. The llama.cpp wire shape instead asks for a locally bounded
    ``line_count``; the parser below compiles that count back to the
    authoritative inclusive ``end_line`` representation.
    """

    schema = action_envelope_schema()
    schema["properties"]["rationale"]["maxLength"] = (
        MAX_LLAMA_CPP_RATIONALE_CHARS
    )
    variants = schema["properties"]["action"]["oneOf"]
    for index, variant in enumerate(variants):
        if variant["properties"]["kind"].get("const") != "read_text":
            continue
        variants[index] = _object_variant(
            "Read a bounded number of lines from one UTF-8 workspace file.",
            {
                "kind": {"const": "read_text"},
                "path": copy.deepcopy(variant["properties"]["path"]),
                "start_line": _integer_schema(
                    "First one-based line.", 1, MAX_LINE_NUMBER
                ),
                "line_count": _integer_schema(
                    "Number of lines to read.", 1, MAX_READ_LINES
                ),
            },
        )
        break
    else:  # pragma: no cover - protects this compiler from schema drift.
        raise RuntimeError("authoritative schema has no read_text action")
    schema["title"] = "Forge8 llama.cpp ActionEnvelope wire contract"
    return schema


_LLAMA_CPP_WIRE_SCHEMA: dict[str, Any] = _build_llama_cpp_wire_schema()


def llama_cpp_action_envelope_schema() -> dict[str, Any]:
    """Return an ordered grammar-safe wire schema for pinned llama.cpp.

    The projection retains grammar-safe integer bounds and the deliberately
    short top-level rationale bound. Other string and array constraints remain
    omitted: compiling the large write-content bound into a repetition grammar
    is both expensive and counterproductive. Output must still pass
    :func:`parse_llama_cpp_action_envelope` before it can become executable.
    """

    projected = _llama_cpp_schema_projection(_LLAMA_CPP_WIRE_SCHEMA)
    source_rationale = _LLAMA_CPP_WIRE_SCHEMA["properties"]["rationale"]
    projected_rationale = projected["properties"]["rationale"]
    # This is the only string node whose length keywords are grammar-facing.
    projected_rationale["minLength"] = source_rationale["minLength"]
    projected_rationale["maxLength"] = source_rationale["maxLength"]
    return projected


def _reject_constant(value: str) -> None:
    raise ActionJSONError(f"Non-finite JSON number is not allowed: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ActionJSONError(f"Duplicate JSON key is not allowed: {key!r}")
        result[key] = value
    return result


def _parse_strict_json_document(raw: str) -> Any:
    """Decode exactly one bounded JSON document using the shared wire policy."""

    if type(raw) is not str:
        raise ActionJSONError("Action output must be a JSON string")
    if not raw or len(raw) > MAX_ENVELOPE_CHARS:
        raise ActionJSONError(
            f"Action JSON must contain 1-{MAX_ENVELOPE_CHARS} characters"
        )
    try:
        return json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except ActionJSONError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ActionJSONError(
            "Action output must be exactly one valid JSON document"
        ) from exc


def _exact_keys(value: Any, expected: tuple[str, ...], location: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ActionValidationError(f"{location} must be a JSON object")
    actual = set(value)
    wanted = set(expected)
    missing = sorted(wanted - actual)
    extra = sorted(actual - wanted)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unknown {extra}")
        raise ActionValidationError(f"{location} has invalid keys: {'; '.join(details)}")
    return value


def _text(
    value: Any,
    location: str,
    *,
    minimum: int = 0,
    maximum: int,
    reject_blank: bool = False,
    reject_nul: bool = True,
) -> str:
    if type(value) is not str:
        raise ActionValidationError(f"{location} must be a string")
    length = len(value)
    if length < minimum or length > maximum:
        raise ActionValidationError(
            f"{location} length must be between {minimum} and {maximum} characters"
        )
    if reject_blank and not value.strip():
        raise ActionValidationError(f"{location} must not be blank")
    if reject_nul and "\x00" in value:
        raise ActionValidationError(f"{location} must not contain a null character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ActionValidationError(f"{location} must be valid UTF-8 text") from exc
    return value


def _bounded_int(value: Any, location: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise ActionValidationError(f"{location} must be an integer")
    if not minimum <= value <= maximum:
        raise ActionValidationError(
            f"{location} must be between {minimum} and {maximum}"
        )
    return value


def _relative_path(
    value: Any,
    location: str,
    *,
    allow_root: bool,
    allow_single_terminal_slash: bool = False,
) -> str:
    raw = _text(value, location, minimum=1, maximum=MAX_PATH_CHARS)
    if raw == ".":
        if allow_root:
            return raw
        raise ActionValidationError(f"{location} must name a path below the workspace root")
    if raw.startswith(("/", "//")) or _WINDOWS_ABSOLUTE.match(raw):
        raise ActionValidationError(f"{location} must be relative to the workspace")
    if "\\" in raw:
        raise ActionValidationError(f"{location} must use POSIX '/' separators")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ActionValidationError(f"{location} must not contain control characters")
    normalized = (
        raw[:-1]
        if allow_single_terminal_slash and raw.endswith("/")
        else raw
    )
    path = PurePosixPath(normalized)
    if (
        not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != normalized
    ):
        raise ActionValidationError(f"{location} must be a normalized relative path")
    if any(len(part.encode("utf-8")) > 255 for part in path.parts):
        raise ActionValidationError(f"{location} contains a path segment longer than 255 bytes")
    return normalized


def _parse_list_files(payload: dict[str, Any]) -> ListFilesAction:
    action = _exact_keys(payload, ("kind", "path", "limit"), "action[list_files]")
    return ListFilesAction(
        path=_relative_path(
            action["path"],
            "action.path",
            allow_root=True,
            allow_single_terminal_slash=True,
        ),
        limit=_bounded_int(action["limit"], "action.limit", 1, MAX_LIST_FILES),
    )


def _parse_read_text(payload: dict[str, Any]) -> ReadTextAction:
    action = _exact_keys(
        payload,
        ("kind", "path", "start_line", "end_line"),
        "action[read_text]",
    )
    start = _bounded_int(action["start_line"], "action.start_line", 1, MAX_LINE_NUMBER)
    end = _bounded_int(action["end_line"], "action.end_line", 1, MAX_LINE_NUMBER)
    if end < start:
        raise ActionValidationError("action.end_line must not precede action.start_line")
    if end - start + 1 > MAX_READ_LINES:
        raise ActionValidationError(
            f"read_text may request at most {MAX_READ_LINES} lines per action"
        )
    return ReadTextAction(
        path=_relative_path(action["path"], "action.path", allow_root=False),
        start_line=start,
        end_line=end,
    )


def _parse_search_text(payload: dict[str, Any]) -> SearchTextAction:
    action = _exact_keys(payload, ("kind", "query", "limit"), "action[search_text]")
    query = _text(
        action["query"],
        "action.query",
        minimum=1,
        maximum=MAX_QUERY_CHARS,
        reject_blank=True,
    )
    return SearchTextAction(
        query=query,
        limit=_bounded_int(action["limit"], "action.limit", 1, MAX_SEARCH_RESULTS),
    )


def _parse_replace_lines(payload: dict[str, Any]) -> ReplaceLinesAction:
    action = _exact_keys(
        payload,
        (
            "kind",
            "path",
            "expected_sha256",
            "start_line",
            "end_line",
            "new_text",
        ),
        "action[replace_lines]",
    )
    guard = _text(
        action["expected_sha256"],
        "action.expected_sha256",
        minimum=64,
        maximum=64,
    )
    if not _SHA256.fullmatch(guard):
        raise ActionValidationError(
            "action.expected_sha256 must be exactly 64 hexadecimal characters"
        )

    start = _bounded_int(action["start_line"], "action.start_line", 1, MAX_LINE_NUMBER)
    end = _bounded_int(action["end_line"], "action.end_line", 1, MAX_LINE_NUMBER)
    if end < start:
        raise ActionValidationError("action.end_line must not precede action.start_line")
    if end - start + 1 > MAX_READ_LINES:
        raise ActionValidationError(
            f"replace_lines may replace at most {MAX_READ_LINES} lines per action"
        )

    new_text = _text(
        action["new_text"],
        "action.new_text",
        maximum=MAX_WRITE_BYTES,
    )
    if len(new_text.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ActionValidationError(
            f"action.new_text exceeds the {MAX_WRITE_BYTES}-byte UTF-8 budget"
        )

    return ReplaceLinesAction(
        path=_relative_path(action["path"], "action.path", allow_root=False),
        expected_sha256=guard.lower(),
        start_line=start,
        end_line=end,
        new_text=new_text,
    )


def _parse_write_text(payload: dict[str, Any]) -> WriteTextAction:
    action = _exact_keys(
        payload,
        ("kind", "path", "expected_sha256", "content"),
        "action[write_text]",
    )
    guard = _text(
        action["expected_sha256"],
        "action.expected_sha256",
        minimum=7,
        maximum=64,
    )
    if not _SHA256_OR_MISSING.fullmatch(guard):
        raise ActionValidationError(
            "action.expected_sha256 must be 64 hexadecimal characters or 'missing'"
        )
    content = _text(action["content"], "action.content", maximum=MAX_WRITE_BYTES)
    encoded_size = len(content.encode("utf-8"))
    if encoded_size > MAX_WRITE_BYTES:
        raise ActionValidationError(
            f"action.content exceeds the {MAX_WRITE_BYTES}-byte UTF-8 write budget"
        )
    return WriteTextAction(
        path=_relative_path(action["path"], "action.path", allow_root=False),
        expected_sha256=guard if guard == "missing" else guard.lower(),
        content=content,
    )


def _parse_replace_text(payload: dict[str, Any]) -> ReplaceTextAction:
    action = _exact_keys(
        payload,
        ("kind", "path", "expected_sha256", "old_text", "new_text"),
        "action[replace_text]",
    )
    guard = _text(
        action["expected_sha256"],
        "action.expected_sha256",
        minimum=64,
        maximum=64,
    )
    if not _SHA256.fullmatch(guard):
        raise ActionValidationError(
            "action.expected_sha256 must be exactly 64 hexadecimal characters"
        )

    old_text = _text(
        action["old_text"],
        "action.old_text",
        minimum=1,
        maximum=MAX_WRITE_BYTES,
    )
    new_text = _text(
        action["new_text"],
        "action.new_text",
        maximum=MAX_WRITE_BYTES,
    )
    for location, text in (("old_text", old_text), ("new_text", new_text)):
        if len(text.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ActionValidationError(
                f"action.{location} exceeds the {MAX_WRITE_BYTES}-byte UTF-8 budget"
            )

    return ReplaceTextAction(
        path=_relative_path(action["path"], "action.path", allow_root=False),
        expected_sha256=guard.lower(),
        old_text=old_text,
        new_text=new_text,
    )


def _parse_run_checks(payload: dict[str, Any]) -> RunChecksAction:
    action = _exact_keys(
        payload,
        ("kind", "checks", "budget"),
        "action[run_checks]",
    )
    raw_checks = action["checks"]
    if type(raw_checks) is not list:
        raise ActionValidationError("action.checks must be an array")
    if not 1 <= len(raw_checks) <= MAX_CHECKS:
        raise ActionValidationError(f"action.checks must contain 1-{MAX_CHECKS} identifiers")
    checks: list[str] = []
    for index, raw_check in enumerate(raw_checks):
        check = _text(
            raw_check,
            f"action.checks[{index}]",
            minimum=1,
            maximum=MAX_CHECK_ID_CHARS,
        )
        if not _CHECK_ID.fullmatch(check):
            raise ActionValidationError(
                f"action.checks[{index}] is not a normalized lowercase check identifier"
            )
        checks.append(check)
    if len(checks) != len(set(checks)):
        raise ActionValidationError("action.checks must not contain duplicates")

    raw_budget = _exact_keys(
        action["budget"],
        ("timeout_seconds", "max_output_chars"),
        "action.budget",
    )
    budget = CheckBudget(
        timeout_seconds=_bounded_int(
            raw_budget["timeout_seconds"],
            "action.budget.timeout_seconds",
            1,
            MAX_CHECK_TIMEOUT_SECONDS,
        ),
        max_output_chars=_bounded_int(
            raw_budget["max_output_chars"],
            "action.budget.max_output_chars",
            1,
            MAX_CHECK_OUTPUT_CHARS,
        ),
    )
    return RunChecksAction(tuple(checks), budget)


def _parse_finish(payload: dict[str, Any]) -> FinishAction:
    action = _exact_keys(payload, ("kind", "summary"), "action[finish]")
    summary = _text(
        action["summary"],
        "action.summary",
        minimum=1,
        maximum=MAX_FINISH_SUMMARY_CHARS,
        reject_blank=True,
    )
    return FinishAction(summary)


_PARSERS = {
    "list_files": _parse_list_files,
    "read_text": _parse_read_text,
    "search_text": _parse_search_text,
    "replace_lines": _parse_replace_lines,
    "write_text": _parse_write_text,
    "replace_text": _parse_replace_text,
    "run_checks": _parse_run_checks,
    "finish": _parse_finish,
}


def validate_action_envelope(payload: Any) -> ActionEnvelope:
    """Validate an already-decoded value and return a typed envelope.

    Callers handling model text should prefer :func:`parse_action_envelope`, which
    additionally rejects duplicate object keys and non-standard JSON numbers.
    """

    envelope = _exact_keys(payload, ("rationale", "action"), "envelope")
    rationale = _text(
        envelope["rationale"],
        "rationale",
        minimum=1,
        maximum=MAX_RATIONALE_CHARS,
        reject_blank=True,
    )
    raw_action = envelope["action"]
    if type(raw_action) is not dict:
        raise ActionValidationError("action must be a JSON object")
    kind = raw_action.get("kind")
    if type(kind) is not str or kind not in _PARSERS:
        allowed = ", ".join(_PARSERS)
        raise ActionValidationError(f"action.kind must be one of: {allowed}")
    return ActionEnvelope(rationale=rationale, action=_PARSERS[kind](raw_action))


def parse_action_envelope(raw: str) -> ActionEnvelope:
    """Parse one strict JSON-only model response into a typed action envelope.

    Duplicate keys, non-finite numbers, markdown fences, trailing prose, unknown
    fields, unused fields from another action kind, and out-of-budget values are
    rejected. No filesystem or process operation occurs here.
    """

    return validate_action_envelope(_parse_strict_json_document(raw))


def parse_llama_cpp_action_envelope(raw: str) -> ActionEnvelope:
    """Compile one strict llama.cpp wire response to an authoritative envelope.

    The pinned Gemma/llama.cpp chat-template path can emit one known channel
    control marker immediately before grammar-constrained JSON.  Strip only that
    exact backend artifact; arbitrary prefixes, prose, repeated markers, and
    trailing text remain invalid strict JSON.

    Only ``read_text`` differs on the wire: its bounded ``line_count`` becomes
    the inclusive ``end_line`` used by :class:`ReadTextAction`. Every other
    action is passed unchanged to :func:`validate_action_envelope`; this compiler
    never clamps a value, repairs a write guard, or rewrites effect content.
    """

    if isinstance(raw, str):
        leading = len(raw) - len(raw.lstrip())
        marker = "<|channel>thought<channel|>"
        body = raw[leading:]
        if body.startswith(marker):
            raw = raw[:leading] + body[len(marker) :]

    payload = _parse_strict_json_document(raw)
    envelope = _exact_keys(payload, ("rationale", "action"), "envelope")
    wire_rationale = _text(
        envelope["rationale"],
        "rationale",
        minimum=1,
        maximum=MAX_LLAMA_CPP_RATIONALE_CHARS,
        reject_blank=True,
    )
    raw_action = envelope["action"]
    if type(raw_action) is not dict or raw_action.get("kind") != "read_text":
        return validate_action_envelope(payload)

    wire_action = _exact_keys(
        raw_action,
        ("kind", "path", "start_line", "line_count"),
        "action[read_text]",
    )
    start_line = _bounded_int(
        wire_action["start_line"],
        "action.start_line",
        1,
        MAX_LINE_NUMBER,
    )
    line_count = _bounded_int(
        wire_action["line_count"],
        "action.line_count",
        1,
        MAX_READ_LINES,
    )
    end_line = start_line + line_count - 1
    if end_line > MAX_LINE_NUMBER:
        raise ActionValidationError(
            f"read_text end_line compiled from start_line and line_count "
            f"must not exceed {MAX_LINE_NUMBER}"
        )

    authoritative_payload = {
        "rationale": wire_rationale,
        "action": {
            "kind": "read_text",
            "path": wire_action["path"],
            "start_line": start_line,
            "end_line": end_line,
        },
    }
    return validate_action_envelope(authoritative_payload)


__all__ = [
    "ACTION_ENVELOPE_SCHEMA",
    "Action",
    "ActionEnvelope",
    "ActionEnvelopeError",
    "ActionJSONError",
    "ActionValidationError",
    "CheckBudget",
    "FinishAction",
    "ListFilesAction",
    "MAX_LLAMA_CPP_RATIONALE_CHARS",
    "ReadTextAction",
    "ReplaceLinesAction",
    "ReplaceTextAction",
    "RunChecksAction",
    "SearchTextAction",
    "WriteTextAction",
    "action_envelope_schema",
    "llama_cpp_action_envelope_schema",
    "parse_action_envelope",
    "parse_llama_cpp_action_envelope",
    "validate_action_envelope",
]
