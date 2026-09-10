"""Owned example: last value wins, but the first-seen key order is retained."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Record:
    key: str
    value: int


def merge_records(rows):
    by_key = {}
    for row in rows:
        record = Record(**row)
        by_key[record.key] = record
    return [asdict(record) for record in by_key.values()]
