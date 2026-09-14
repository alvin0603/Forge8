"""A small input-cleaning example for guest line and local-value inspection."""


def sum_rows(rows):
    """Sum integer-like rows; count rows rejected with ValueError."""
    total = 0
    rejected = 0
    for row in rows:
        try:
            total += int(row)
        except ValueError:
            rejected += 1
    return {"total": total, "rejected": rejected}
