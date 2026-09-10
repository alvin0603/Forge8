"""Compose fields through the sibling helper and package-level delimiter."""

from . import DELIMITER
from .escaping import quote_field


def render_row(values: list[str]) -> str:
    return DELIMITER.join(quote_field(value) for value in values) + "\r\n"
