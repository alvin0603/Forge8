"""A source-call example for ReadingDesk's explicit argument carryover."""

from exporter.rows import render_row


def example_row():
    return render_row(
        ["Ada;Lin", 'say "yes"', "first\nsecond", "ok"],
    )
