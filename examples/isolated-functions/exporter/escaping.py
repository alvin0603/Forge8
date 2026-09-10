"""Keep delimiters, quotes and embedded newlines inside one field."""

from . import DELIMITER


def quote_field(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("each field must be a string")
    if any(marker in value for marker in (DELIMITER, '"', "\r", "\n")):
        return '"' + value.replace('"', '""') + '"'
    return value
