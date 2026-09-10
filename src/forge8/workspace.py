"""Capability-bounded workspace access and content-addressed tool artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class PolicyViolation(PermissionError):
    pass


_WINDOWS_ABSOLUTE = re.compile(r"^[a-zA-Z]:[\\/]")
_DENIED_PARTS = {".git", ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker"}
_DENIED_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}
_DENIED_SUFFIXES = {".pem", ".p12", ".pfx", ".key"}
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
MAX_REPLACE_LINES = 400

_TEXT_LINE_ENDINGS = (
    "\r\n",
    "\n",
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
)


def _terminal_line_ending(value: str) -> str:
    """Return the exact line boundary retained by ``splitlines(keepends=True)``."""

    for ending in _TEXT_LINE_ENDINGS:
        if value.endswith(ending):
            return ending
    return ""


def _decode_common_model_escapes(value: str) -> str:
    """Remove one accidental JSON-style escape layer from proposal evidence.

    Some compact models emit ``\\n`` and ``\\\"`` *inside* an already decoded
    JSON string. This helper is only used to locate old/preimage text. It never
    transforms replacement content or grants an effect.
    """

    escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
    decoded: list[str] = []
    index = 0
    while index < len(value):
        if (
            value[index] == "\\"
            and index + 1 < len(value)
            and value[index + 1] in escapes
        ):
            decoded.append(escapes[value[index + 1]])
            index += 2
            continue
        decoded.append(value[index])
        index += 1
    return "".join(decoded)


def _normalized_preimage_spans(content: str, proposal: str) -> tuple[str, ...]:
    """Find unique source spans after evidence-only escape/whitespace repair."""

    source_lines = content.splitlines(keepends=True)
    spans: list[str] = []
    candidates = [proposal]
    decoded = _decode_common_model_escapes(proposal)
    if decoded != proposal:
        candidates.append(decoded)
    for candidate in candidates:
        proposed_lines = candidate.splitlines()
        if not proposed_lines or len(proposed_lines) > len(source_lines):
            continue
        normalized_proposal = [line.rstrip() for line in proposed_lines]
        width = len(proposed_lines)
        for start in range(0, len(source_lines) - width + 1):
            window = source_lines[start : start + width]
            normalized_window = [line.rstrip("\r\n").rstrip() for line in window]
            if normalized_window == normalized_proposal:
                span = "".join(window)
                if span not in spans:
                    spans.append(span)
    return tuple(spans)


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    root: Path
    allow_hidden: bool = False
    allow_write: bool = False
    max_read_bytes: int = 256 * 1024
    max_write_bytes: int = 256 * 1024
    max_output_chars: int = 16_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve(strict=True))
        if not self.root.is_dir():
            raise ValueError(f"Workspace root is not a directory: {self.root}")
        if self.root == Path(self.root.anchor):
            raise ValueError("A filesystem root cannot be used as a workspace")

    def resolve(self, raw_path: str, *, must_exist: bool = True) -> Path:
        """Resolve a model-supplied path and prove it remains inside the root."""

        if not raw_path or "\x00" in raw_path:
            raise PolicyViolation("Path is empty or contains a null byte")
        normalized = raw_path.replace("\\", "/")
        if normalized.startswith(("/", "//")) or _WINDOWS_ABSOLUTE.match(raw_path):
            raise PolicyViolation("Absolute paths are not allowed")
        raw_parts = normalized.split("/")
        if any(part in {"", ".", ".."} for part in raw_parts):
            raise PolicyViolation("Relative traversal and ambiguous path parts are not allowed")
        pure = PurePosixPath(normalized)
        self._check_sensitive(pure)

        current = self.root
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise PolicyViolation("Symbolic links are disabled by workspace policy")

        candidate = (self.root / Path(*pure.parts)).resolve(strict=must_exist)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PolicyViolation("Resolved path escapes the workspace") from exc
        return candidate

    def _check_sensitive(self, path: PurePosixPath) -> None:
        lowered = tuple(part.lower() for part in path.parts)
        if any(part in _DENIED_PARTS for part in lowered):
            raise PolicyViolation("Credential directories are excluded from the workspace view")
        name = lowered[-1]
        if (
            name in _DENIED_NAMES
            or name.startswith(".env.")
            or Path(name).suffix in _DENIED_SUFFIXES
        ):
            raise PolicyViolation("Credential-like files are excluded from the workspace view")
        if not self.allow_hidden and any(part.startswith(".") for part in lowered):
            raise PolicyViolation("Hidden paths are disabled by policy")
        for original, part in zip(path.parts, lowered, strict=True):
            if unicodedata.normalize("NFC", original) != original:
                raise PolicyViolation("Non-canonical Unicode path components are disabled")
            if ":" in original:
                raise PolicyViolation("Colon and alternate-stream path syntax are disabled")
            windows_stem = part.rstrip(" .").split(".", 1)[0]
            if part != part.rstrip(" .") or windows_stem in _WINDOWS_RESERVED:
                raise PolicyViolation("Windows-reserved path components are disabled")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    sha256: str
    relative_path: str
    media_type: str
    size_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArtifactStore:
    """Append-only, content-addressed run storage.

    Model-facing context receives a small preview and this immutable handle. The
    complete bytes remain available to verifiers without being copied into every
    prompt.
    """

    def __init__(self, run_root: Path) -> None:
        self.run_root = run_root.resolve()
        self.objects = self.run_root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, data: bytes, media_type: str = "application/octet-stream") -> ArtifactRef:
        digest = hashlib.sha256(data).hexdigest()
        relative = Path("objects") / digest[:2] / digest
        target = self.run_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != data:
                raise RuntimeError("Content-address collision or corrupted artifact store")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".tmp-", dir=target.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                temporary.replace(target)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        return ArtifactRef(digest, relative.as_posix(), media_type, len(data))

    def put_text(self, text: str, media_type: str = "text/plain; charset=utf-8") -> ArtifactRef:
        return self.put_bytes(text.encode("utf-8"), media_type)

    def read(self, reference: ArtifactRef) -> bytes:
        path = (self.run_root / reference.relative_path).resolve(strict=True)
        try:
            path.relative_to(self.objects)
        except ValueError as exc:
            raise PolicyViolation("Artifact reference escapes its object store") from exc
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != reference.sha256:
            raise RuntimeError("Artifact hash verification failed")
        return data


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    preview: str
    artifact: ArtifactRef | None
    metadata: dict[str, Any]
    error: str | None = None

    def model_view(self) -> str:
        payload = {
            "ok": self.ok,
            "preview": self.preview,
            "artifact": self.artifact.as_dict() if self.artifact else None,
            "metadata": self.metadata,
            "error": self.error,
        }
        return json.dumps(payload, ensure_ascii=False)


class WorkspaceTools:
    def __init__(self, policy: WorkspacePolicy, artifacts: ArtifactStore) -> None:
        self.policy = policy
        self.artifacts = artifacts

    def list_files(self, path: str = ".", *, limit: int = 200) -> ToolResult:
        if path == ".":
            root = self.policy.root
        else:
            try:
                root = self.policy.resolve(path)
            except (PolicyViolation, OSError) as exc:
                return ToolResult(False, "", None, {}, str(exc))
        if not root.is_dir():
            return ToolResult(False, "", None, {}, "Requested path is not a directory")
        entries: list[str] = []
        for candidate in sorted(root.rglob("*")):
            if len(entries) >= max(1, min(limit, 1000)):
                break
            try:
                relative = candidate.relative_to(self.policy.root)
                # Reuse the sensitive-path checks and resolve symlinks before exposure.
                resolved = self.policy.resolve(relative.as_posix())
                if resolved.is_file() and resolved.stat().st_nlink > 1:
                    continue
            except (PolicyViolation, OSError):
                continue
            marker = "/" if candidate.is_dir() else ""
            entries.append(relative.as_posix() + marker)
        content = "\n".join(entries)
        return self._result(content, {"returned": len(entries), "truncated": len(entries) >= limit})

    def read_text(self, path: str, *, start_line: int = 1, end_line: int | None = None) -> ToolResult:
        try:
            target = self.policy.resolve(path)
        except (PolicyViolation, OSError) as exc:
            return ToolResult(False, "", None, {}, str(exc))
        if not target.is_file():
            return ToolResult(False, "", None, {}, "Requested path is not a file")
        if target.stat().st_nlink > 1:
            return ToolResult(False, "", None, {}, "Hard-linked files are disabled by policy")
        if target.stat().st_size > self.policy.max_read_bytes:
            return ToolResult(
                False,
                "",
                None,
                {"size_bytes": target.stat().st_size},
                f"File exceeds the {self.policy.max_read_bytes}-byte read policy",
            )
        try:
            data = target.read_bytes()
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return ToolResult(False, "", None, {}, "File is not valid UTF-8 text")
        first = max(start_line, 1)
        last = min(end_line or (first + 399), len(lines))
        if first > last and lines:
            return ToolResult(False, "", None, {"line_count": len(lines)}, "Requested line range is empty")
        # Source bytes begin immediately after the delimiter. Keeping delimiter
        # padding out of the source column makes Python indentation unambiguous
        # to compact models that copy a numbered range into replace_lines.
        rendered = "\n".join(
            f"{number:>6}|{lines[number - 1]}"
            for number in range(first, last + 1)
        )
        return self._result(
            rendered,
            {
                "path": path,
                "start_line": first,
                "end_line": last,
                "line_count": len(lines),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            },
        )

    def search_text(self, query: str, *, limit: int = 100) -> ToolResult:
        if not query or len(query) > 512:
            return ToolResult(False, "", None, {}, "Search query must contain 1-512 characters")
        matches: list[str] = []
        capped = max(1, min(limit, 500))
        for candidate in sorted(self.policy.root.rglob("*")):
            if len(matches) >= capped:
                break
            if not candidate.is_file() or candidate.stat().st_size > self.policy.max_read_bytes:
                continue
            try:
                relative = candidate.relative_to(self.policy.root).as_posix()
                resolved = self.policy.resolve(relative)
                if resolved.stat().st_nlink > 1:
                    continue
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except (PolicyViolation, OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, 1):
                if query.casefold() in line.casefold():
                    matches.append(f"{relative}:{line_number}:{line[:500]}")
                    if len(matches) >= capped:
                        break
        return self._result("\n".join(matches), {"matches": len(matches), "truncated": len(matches) >= capped})

    def write_text(
        self,
        path: str,
        content: str,
        *,
        expected_sha256: str,
    ) -> ToolResult:
        """Atomically write UTF-8 text with an optimistic-concurrency guard.

        Existing files require the exact SHA-256 observed by the model-facing
        read.  New files require the literal guard ``missing``.  This prevents a
        stale action from silently overwriting a file that changed after it was
        inspected.  OS-level sandboxing is still required to close link-swap
        races between resolution and replacement.
        """

        if not self.policy.allow_write:
            return ToolResult(False, "", None, {}, "Workspace writes are disabled by policy")
        if not isinstance(content, str):
            return ToolResult(False, "", None, {}, "Write content must be UTF-8 text")
        if "\x00" in content:
            return ToolResult(False, "", None, {}, "Write content must not contain a null character")
        encoded = content.encode("utf-8")
        if len(encoded) > self.policy.max_write_bytes:
            return ToolResult(
                False,
                "",
                None,
                {"size_bytes": len(encoded)},
                f"Content exceeds the {self.policy.max_write_bytes}-byte write policy",
            )
        try:
            target = self.policy.resolve(path, must_exist=False)
        except (PolicyViolation, OSError) as exc:
            return ToolResult(False, "", None, {}, str(exc))
        if target.exists() and not target.is_file():
            return ToolResult(False, "", None, {}, "Write target is not a regular file")
        if target.exists() and target.stat().st_nlink > 1:
            return ToolResult(False, "", None, {}, "Hard-linked files are disabled by policy")
        if not target.parent.is_dir():
            return ToolResult(False, "", None, {}, "Write target parent directory does not exist")

        before: bytes | None = target.read_bytes() if target.exists() else None
        actual_guard = "missing" if before is None else hashlib.sha256(before).hexdigest()
        if expected_sha256 != actual_guard:
            return ToolResult(
                False,
                "",
                None,
                {"expected_sha256": expected_sha256, "actual_sha256": actual_guard},
                "Write guard does not match the current file",
            )
        if before == encoded:
            return ToolResult(
                False,
                "",
                None,
                {
                    "path": path,
                    "size_bytes": len(encoded),
                    "actual_sha256": actual_guard,
                },
                "Write content is byte-identical to the current file",
            )

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.forge8-", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

        after_sha256 = hashlib.sha256(encoded).hexdigest()
        artifact = self.artifacts.put_bytes(encoded, "text/plain; charset=utf-8")
        preview = content
        if len(preview) > self.policy.max_output_chars:
            preview = preview[: self.policy.max_output_chars] + "\n…[truncated; use artifact handle]"
        return ToolResult(
            True,
            preview,
            artifact,
            {
                "path": path,
                "size_bytes": len(encoded),
                "before_sha256": actual_guard,
                "after_sha256": after_sha256,
            },
        )

    def replace_text(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        expected_sha256: str,
    ) -> ToolResult:
        """Replace exactly one observed text span under the same write guard.

        This is deliberately smaller than a general patch language. A local
        model must quote an existing, unique preimage span, and the complete
        file hash must still match the preceding read. Zero or ambiguous
        matches have no effect. The final atomic write rechecks the hash, which
        also closes changes between this read and replacement construction.
        """

        if not self.policy.allow_write:
            return ToolResult(False, "", None, {}, "Workspace writes are disabled by policy")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            return ToolResult(False, "", None, {}, "Replacement values must be UTF-8 text")
        if not old_text:
            return ToolResult(False, "", None, {}, "Replacement old_text must not be empty")
        if "\x00" in old_text or "\x00" in new_text:
            return ToolResult(
                False,
                "",
                None,
                {},
                "Replacement values must not contain a null character",
            )
        if (
            len(old_text.encode("utf-8")) > self.policy.max_write_bytes
            or len(new_text.encode("utf-8")) > self.policy.max_write_bytes
        ):
            return ToolResult(
                False,
                "",
                None,
                {},
                f"Replacement span exceeds the {self.policy.max_write_bytes}-byte write policy",
            )
        try:
            target = self.policy.resolve(path)
        except (PolicyViolation, OSError) as exc:
            return ToolResult(False, "", None, {}, str(exc))
        if not target.is_file():
            return ToolResult(False, "", None, {}, "Replacement target is not a regular file")
        if target.stat().st_nlink > 1:
            return ToolResult(False, "", None, {}, "Hard-linked files are disabled by policy")
        if target.stat().st_size > self.policy.max_read_bytes:
            return ToolResult(
                False,
                "",
                None,
                {"size_bytes": target.stat().st_size},
                f"File exceeds the {self.policy.max_read_bytes}-byte read policy",
            )

        before = target.read_bytes()
        actual_guard = hashlib.sha256(before).hexdigest()
        if expected_sha256 != actual_guard:
            return ToolResult(
                False,
                "",
                None,
                {"expected_sha256": expected_sha256, "actual_sha256": actual_guard},
                "Replacement guard does not match the current file",
            )
        try:
            content = before.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(False, "", None, {}, "Replacement target is not valid UTF-8 text")
        occurrences = content.count(old_text)
        selected_old_text = old_text
        preimage_normalized = False
        if occurrences == 0:
            normalized_spans = _normalized_preimage_spans(content, old_text)
            if len(normalized_spans) == 1:
                selected_old_text = normalized_spans[0]
                occurrences = 1
                preimage_normalized = True
            else:
                occurrences = len(normalized_spans)
        if occurrences != 1:
            return ToolResult(
                False,
                "",
                None,
                {"matches": occurrences, "path": path},
                "Replacement old_text must match exactly once",
            )
        updated = content.replace(selected_old_text, new_text, 1)
        result = self.write_text(path, updated, expected_sha256=actual_guard)
        if not result.ok:
            return result
        return ToolResult(
            True,
            result.preview,
            result.artifact,
            {
                **result.metadata,
                "matches": 1,
                "old_text_sha256": hashlib.sha256(old_text.encode("utf-8")).hexdigest(),
                "matched_preimage_sha256": hashlib.sha256(
                    selected_old_text.encode("utf-8")
                ).hexdigest(),
                "new_text_sha256": hashlib.sha256(new_text.encode("utf-8")).hexdigest(),
                "preimage_normalized": preimage_normalized,
            },
        )

    def replace_lines(
        self,
        path: str,
        start_line: int,
        end_line: int,
        new_text: str,
        *,
        expected_sha256: str,
    ) -> ToolResult:
        """Atomically replace an inclusive range of existing UTF-8 text lines.

        Line numbers are one-based and must identify 1-400 lines that already
        exist. Bytes outside the selected span are copied exactly. If the final
        selected line has a line ending and non-empty ``new_text`` has none, that
        exact ending is appended so a middle-of-file replacement cannot merge
        with the following line. Empty ``new_text`` deletes the selected lines
        without retaining their ending. The whole-file SHA-256 is checked both
        before construction and again by :meth:`write_text` at atomic commit.
        """

        if not self.policy.allow_write:
            return ToolResult(False, "", None, {}, "Workspace writes are disabled by policy")
        if (
            isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or isinstance(end_line, bool)
            or not isinstance(end_line, int)
        ):
            return ToolResult(False, "", None, {}, "Replacement line numbers must be integers")
        if start_line < 1 or end_line < start_line:
            return ToolResult(
                False,
                "",
                None,
                {"start_line": start_line, "end_line": end_line},
                "Replacement line range must be one-based, inclusive, and non-empty",
            )
        replaced_line_count = end_line - start_line + 1
        if replaced_line_count > MAX_REPLACE_LINES:
            return ToolResult(
                False,
                "",
                None,
                {
                    "start_line": start_line,
                    "end_line": end_line,
                    "replaced_line_count": replaced_line_count,
                },
                f"Replacement range exceeds the {MAX_REPLACE_LINES}-line policy",
            )
        if not isinstance(new_text, str):
            return ToolResult(False, "", None, {}, "Replacement new_text must be UTF-8 text")
        if "\x00" in new_text:
            return ToolResult(
                False,
                "",
                None,
                {},
                "Replacement new_text must not contain a null character",
            )
        try:
            new_text_bytes = new_text.encode("utf-8")
        except UnicodeEncodeError:
            return ToolResult(False, "", None, {}, "Replacement new_text is not valid UTF-8 text")

        try:
            target = self.policy.resolve(path)
        except (PolicyViolation, OSError) as exc:
            return ToolResult(False, "", None, {}, str(exc))
        if not target.is_file():
            return ToolResult(False, "", None, {}, "Replacement target is not a regular file")
        if target.stat().st_nlink > 1:
            return ToolResult(False, "", None, {}, "Hard-linked files are disabled by policy")
        if target.stat().st_size > self.policy.max_read_bytes:
            return ToolResult(
                False,
                "",
                None,
                {"size_bytes": target.stat().st_size},
                f"File exceeds the {self.policy.max_read_bytes}-byte read policy",
            )

        before = target.read_bytes()
        actual_guard = hashlib.sha256(before).hexdigest()
        if expected_sha256 != actual_guard:
            return ToolResult(
                False,
                "",
                None,
                {"expected_sha256": expected_sha256, "actual_sha256": actual_guard},
                "Replacement guard does not match the current file",
            )
        try:
            content = before.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(False, "", None, {}, "Replacement target is not valid UTF-8 text")

        lines = content.splitlines(keepends=True)
        if end_line > len(lines):
            return ToolResult(
                False,
                "",
                None,
                {
                    "start_line": start_line,
                    "end_line": end_line,
                    "line_count": len(lines),
                },
                "Replacement line range exceeds the current file",
            )

        terminal_ending = _terminal_line_ending(lines[end_line - 1])
        replacement = new_text
        terminal_eol_preserved = False
        if replacement and terminal_ending and not _terminal_line_ending(replacement):
            replacement += terminal_ending
            terminal_eol_preserved = True

        updated = (
            "".join(lines[: start_line - 1])
            + replacement
            + "".join(lines[end_line:])
        )
        result = self.write_text(path, updated, expected_sha256=actual_guard)
        if not result.ok:
            return result
        return ToolResult(
            True,
            result.preview,
            result.artifact,
            {
                **result.metadata,
                "start_line": start_line,
                "end_line": end_line,
                "replaced_line_count": replaced_line_count,
                "line_count_before": len(lines),
                "new_text_size_bytes": len(new_text_bytes),
                "new_text_sha256": hashlib.sha256(new_text_bytes).hexdigest(),
                "terminal_eol_preserved": terminal_eol_preserved,
            },
        )

    def _result(self, content: str, metadata: dict[str, Any]) -> ToolResult:
        artifact = self.artifacts.put_text(content)
        limit = self.policy.max_output_chars
        preview = content if len(content) <= limit else content[:limit] + "\n…[truncated; use artifact handle]"
        return ToolResult(True, preview, artifact, metadata)
