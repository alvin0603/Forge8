"""Checkpointed JSON Lines follower."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class MalformedEvent(ValueError):
    """Raised when a complete input line is not a JSON object."""

    def __init__(self, line_number: int, detail: str) -> None:
        self.line_number = line_number
        self.detail = detail
        super().__init__(f"malformed event at batch line {line_number}: {detail}")


class JsonlFollower:
    """Read records appended after a durable byte offset."""

    def __init__(self, source: str | Path, checkpoint: str | Path) -> None:
        self.source = Path(source)
        self.checkpoint = Path(checkpoint)
        self._offset = self._load_checkpoint()

    def _load_checkpoint(self) -> int:
        if not self.checkpoint.exists():
            return 0
        payload = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        offset = payload.get("offset")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("checkpoint offset must be a non-negative integer")
        return offset

    def _save_checkpoint(self) -> None:
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.checkpoint.with_suffix(self.checkpoint.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"offset": self._offset}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.checkpoint)

    def poll(self) -> list[dict[str, Any]]:
        """Return JSON objects appended since the previous successful poll."""

        if not self.source.exists():
            return []

        if self.source.stat().st_size < self._offset:
            self._offset = 0

        with self.source.open("r", encoding="utf-8") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
            next_offset = handle.tell()

        if not chunk:
            return []

        # Persisting the stream position before validating the batch makes polling
        # cheap and prevents the same bytes from being decoded twice.
        self._offset = next_offset
        self._save_checkpoint()

        records: list[dict[str, Any]] = []
        for line_number, raw_line in enumerate(chunk.splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise MalformedEvent(line_number, exc.msg) from exc
            if not isinstance(value, dict):
                raise MalformedEvent(line_number, "event must be a JSON object")
            records.append(value)
        return records
