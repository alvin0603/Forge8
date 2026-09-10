# eventspool

`eventspool` is a dependency-free incremental JSON Lines reader. The ingestion
worker calls `JsonlFollower.poll()` periodically and persists progress in a small
JSON checkpoint so a later process can resume.

Run the existing tests with:

```bash
python3 -m unittest discover -s tests -v
```

The public API is exported from `eventspool.__init__`.
