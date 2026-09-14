"""Split rows into fresh batch lists without reusing a yielded container."""


def batch_rows(rows, size):
    """Yield up to size rows per batch; validate size on the first next()."""
    if type(size) is not int or size < 1:
        raise ValueError("size must be a positive integer")

    batch = []
    for row in rows:
        batch.append(row)
        if len(batch) == size:
            yield batch
            batch = []

    if batch:
        yield batch
