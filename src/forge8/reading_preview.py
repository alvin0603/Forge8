"""Unverified text-only preview of the canonical structured reading answer.

This never parses an answer, repairs JSON, or supplies citations. The final
engine must independently validate the complete original response. A malformed
later chunk stops preview; it cannot retract an already emitted safe prefix.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable


_MAX_CHARS = 32_768
_PREFIX = re.compile(r'\A[ \t\r\n]*\{[ \t\r\n]*"kind"[ \t\r\n]*:[ \t\r\n]*"answer"[ \t\r\n]*,[ \t\r\n]*"text"[ \t\r\n]*:[ \t\r\n]*"')
_HEX = frozenset("0123456789abcdefABCDEF")
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class StructuredAnswerPreview:
    """Pass this callable only ordinary on_text deltas, never reasoning deltas."""

    def __init__(self, on_text: Callable[[str], None]) -> None:
        if not callable(on_text):
            raise TypeError("preview callback must be callable")
        self._on_text = on_text
        self._buffer = ""
        self._offset: int | None = None
        self._stopped = False

    def _stop(self) -> None:
        self._stopped = True
        self._buffer = ""

    def __call__(self, delta: str) -> None:
        if self._stopped:
            return
        if type(delta) is not str or len(delta) > _MAX_CHARS - len(self._buffer):
            self._stop()
            return
        self._buffer += delta
        if self._offset is None:
            match = _PREFIX.match(self._buffer)
            if match is None:
                return
            self._offset = match.end()
        raw, output = self._buffer, []
        while self._offset < len(raw):
            start = self._offset
            character, end = raw[start], start + 1
            if character == '"':
                self._stop()  # Never traverse the wrapper's citations or suffix.
                break
            if character == "\\":
                if end == len(raw):
                    break
                escape = raw[end]
                if escape == "u":
                    digits = raw[start + 2:start + 6]
                    if any(digit not in _HEX for digit in digits):
                        self._stop(); break
                    if len(digits) < 4:
                        break
                    code, end = int(digits, 16), start + 6
                    if 0xD800 <= code <= 0xDBFF:
                        tail = raw[end:end + 6]
                        if not "\\u".startswith(tail[:2]) or any(digit not in _HEX for digit in tail[2:]):
                            self._stop(); break
                        if len(tail) < 6:
                            break
                        low = int(tail[2:], 16)
                        if not 0xDC00 <= low <= 0xDFFF:
                            self._stop(); break
                        code, end = 0x10000 + ((code - 0xD800) << 10) + low - 0xDC00, end + 6
                    character = chr(code)
                elif escape in _ESCAPES:
                    character, end = _ESCAPES[escape], start + 2
                else:
                    self._stop(); break
            elif ord(character) < 32:  # Raw controls are invalid JSON, including LF/TAB.
                self._stop(); break
            category = unicodedata.category(character)
            # Same multiline character policy as explain._bounded_text, including
            # UTF-8 encodability (Cs); never emit either half of a surrogate pair.
            if category in {"Cs", "Cf", "Zl", "Zp"} or (category == "Cc" and character not in "\r\n\t"):
                self._stop(); break
            output.append(character)
            self._offset = end
        if output:
            self._on_text("".join(output))
