# Usage

Start with [installation](INSTALL.md).
Commands below run from the checkout using your native installation. Where a
command says `forge8`, substitute `.\.venv\Scripts\forge8.exe` on Windows or
`./.venv-wsl/bin/forge8` in WSL if it is not on PATH.

The current interface is Traditional Chinese. Action descriptions below are
English explanations, not literal button labels.

## Open a project

After configuring the Qwen reader, open the browser desk:

```text
forge8 read PATH_TO_PROJECT
```

For static browsing with no assets, AI, guest execution, or Git comparison:

```powershell
.\.venv\Scripts\forge8.exe read .\examples\isolated-functions --browse-only --state D:\Forge8\browse
```

```bash
./.venv-wsl/bin/forge8 read ./examples/isolated-functions --browse-only --state "$HOME/.local/state/forge8-browse"
```

State must be an absolute path outside the project; use Linux ext4 in WSL.
Open the private terminal URL. To enable AI later, stop with Ctrl+C, finish
setup, and restart without both `--browse-only` and `--state` plus its value.
Closing a browser tab does not stop the desk or release a resident model.

## Read selected code

1. Open a file. Click a line number, then Shift-click the last line, or use the
   extend-selection control. The definition outline can select a Python function.
2. Add the selection to the question. Longer selections split into the available
   ranges automatically, keeping every line. Include relevant callers, constants
   or helpers deliberately.
3. Write a self-contained question and submit it with the selected-code answer
   button.

Manual input is limited to three ranges, each at most 80 lines / 4,000 characters
(including line-number prefixes), with at most 240 lines total and a shared
9,000-character source JSON budget.
If the whole selection cannot fit, existing selections stay unchanged. The shared
budget is checked before loading the model; input is never silently clipped.
The answer lists its actual source scope. Click a citation to inspect that
retained source.

For a CLI question after Qwen setup:

```text
forge8 explain examples/isolated-functions --reader qwen35 --focus merge_records.py:1-18 --question "How are duplicate keys and output order handled?"
```

`explain` otherwise defaults to the separately provisioned Gemma reader; specify
`--reader qwen35` when using the Windows Qwen-only setup.

## Find the code you need

The existing search dropdown offers four different kinds of navigation:

| Mode | What it finds |
| --- | --- |
| Text occurrences | Case-sensitive literal text occurrences. |
| Definition names | Exact or suffix-qualified Python definition names. |
| Keyword definitions | All query words in a function's name, path, or own lexical source. |
| Same-spelling calls | Individual Python calls with the exact terminal spelling. |

Keyword search splits underscores and camelCase; it does not translate or infer
synonyms. Call search includes bare and attribute calls, not resolved receivers,
aliases, or dynamic dispatch. It returns at most 40 rows and discloses skipped
files, incomplete coverage, and truncation. No results is not proof of no callers.

After adding Python source, the name-source control distinguishes parameters,
locals, enclosing scopes and module declarations. Import links can follow
explicit source candidates in the retained project, including `src` layouts and
re-exports. They do not import packages or establish runtime values. Viewing a
candidate does not automatically add it to the next question.

For a pasted traceback, expand the traceback-location controls. This maps source
locations without running a model or the failing program.

## Project questions and follow-ups

The collapsed AI-discovery controls offer optional model-assisted source search.
The project-answer action selects candidates before reading them; it does not
consume the entire project and can miss relevant code. Up to three model requests
may take minutes; the default resident desk reuses its model. CLI equivalents are
`forge8 locate PROJECT --question "..."` and `forge8 ask PROJECT --question "..."`.

Expand the reading-history and export controls to revisit completed questions.
Continuing reuses the selected question's exact source, not old answers or memory.
Write the new question in full. When a genuine insufficient-source result offers
recovery, inspect its source and deliberately add what is missing before asking
again. Invalid source or incomplete cleanup cannot be bypassed this way.

A reading-note download contains the question, answer, citations, and source.
Treat it as private data, not a signed receipt or proof of correctness. History
is bounded to the current desk session; refresh can invalidate old source actions.

## Optional function trials and changes

After [installing the separate WASI runtime](isolated-experiments.md), explicitly
enable the desk's trial controls with `forge8 read PROJECT --allow-experiments`.
Each run still needs consent. It executes a whole Python module, including
initialization, then one synchronous function in a separate CPython/WASI guest.
It does not use your project's installed dependencies or run AI-generated tests.

You can manually enter JSON arguments or carry over supported literal arguments
from a source call. Ambiguous, attribute, and nonliteral calls remain navigation
only. Optional module selection is explicit, limited to four files / 64 KiB total.
Single-file line visits can optionally include 1–3 named local-value snapshots;
see the [watch walkthrough](isolated-experiments.md#watch-a-few-local-values).
These remain untrusted guest reports.

Pin a completed single-file trial as A, then explicitly run a new input as B.
Comparison requires matching source, entry, runtime, and trace policy. Editing
the draft never changes the old result; cancelled or incomplete B is unavailable.
Pinning does not run anything, and refreshing invalidates the baseline.

Change-reading mode compares local HEAD with saved files, not the staging area or
unsaved edits. Its optional paired trial sends the same input to both versions
in separate guests. Equal results do not prove equivalence; different results
do not establish causality. See [change reading](change-reading.md).

## Repair and observed tests

Repair is separate from reading and needs the Gemma repair assets; the Qwen
setup script does not install them. On a trusted Python project with tests:

```text
forge8 fix examples/clamp-bug --goal "Handle values above the upper bound" --allow-write calculator.py --check python_unittest
```

It edits a staging copy and emits a patch, not an automatic change to your project.
Checks execute project code with your host user permissions, without a filesystem
or network sandbox. `observe` likewise executes a trusted test on the host;
it is not a WASI trial. Read [security](SECURITY.md) and
[observed reading](observed-reading.md) before using these commands.

## Diagnose the selected reader

`forge8 doctor --reader qwen35` checks configuration and full asset hashes without
starting a model or changing settings. Passing diagnostics is not GPU readiness
or an answer-quality guarantee. Use `forge8 configure` to inspect saved native
paths; see [installation](INSTALL.md) for creating or changing a deployment.

The desk retains an idle model for 600 seconds by default. The release-model
button stops it; `--idle-timeout 0` selects one-shot operation. If send or cleanup status is
unknown, use the displayed status/recheck controls rather than resubmitting.
Stop the service with Ctrl+C when finished. Source snapshots, questions, and
reports remain private files in your configured state directory.

[Measured limits](VALIDATION.md) · [Native platform differences](PLATFORMS.md)
