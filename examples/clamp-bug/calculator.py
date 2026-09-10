"""Small intentionally broken module for the Forge8 native demo."""


def clamp(value: int, lower: int, upper: int) -> int:
    """Return value constrained to the inclusive [lower, upper] interval."""

    if lower > upper:
        raise ValueError("lower must not exceed upper")
    return min(lower, max(upper, value))
