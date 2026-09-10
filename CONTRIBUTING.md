# Contributing

Start with a small reproducible problem: a confusing workflow, a wrong source
location, or a safe public example the reader explains incorrectly. Include the
platform, Python version, command and expected behavior. Do not attach private
run directories, source, tokens or browser URLs.

## Development

```sh
python -m venv .venv
# Activate this environment, then:
python -m pip install -e .
python -B -m unittest discover -s tests
node tests/test_desk_ui.cjs
node tests/web_changes_test.js
node tests/web_experiment_trace_test.cjs
node tests/web_keyword_search_test.cjs
node tests/web_reading_note_test.cjs
node tests/web_reading_note_ui_test.cjs
node tests/web_call_search_test.cjs
```

On Windows, also run `powershell -NoProfile -File tests/test_setup.ps1` for the
small installer fixtures. They do not download assets or execute Python models.

Tests use temporary fixtures; the default suite does not require model downloads
or a GPU. Some platform-specific tests skip on unsupported hosts. Optional pytest
checks require `pip install -e '.[pytest]'` in the trusted test environment.

Keep changes focused. A UI improvement should remove friction, not add another
dashboard. A model or prompt change needs paired, source-reviewed examples,
including failures and latency; citation validity alone is not a quality metric.
Do not weaken source checks or execution consent to make an example pass.

Forge8 is dependency-free at runtime. Propose new dependencies before adding them.
The [security model](docs/SECURITY.md) documents the boundaries that changes must
preserve. For sensitive reports, use the repository's private vulnerability
reporting if available; otherwise contact the maintainer privately before
publishing details. Never put credentials in a public issue.
