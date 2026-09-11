# Forge8

A local code-reading desk for understanding unfamiliar projects without sending
their source to a cloud model. Open a file, select the relevant code, ask a
question, and follow the answer's citations back to the source it actually read.

Built around an 8 GB GPU budget, with native Windows and WSL paths. The AI reader
is experimental: it can be slow and materially wrong. Source navigation also
works without a GPU, model, or AI request.

The current interface is Traditional Chinese. This documentation describes its
actions in English; it does not imply an English interface. Questions can be in
either language.

## Try it without a model

You need Git, Python 3.10+ with venv support, and a local browser. Clone into a
directory with available space. These commands install this checkout, not a
published PyPI package; Python build dependencies may be downloaded.

Windows PowerShell:

```powershell
git clone https://github.com/alvin0603/forge8.git
cd forge8
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\forge8.exe read .\examples\isolated-functions --browse-only --state D:\Forge8\browse
```

WSL Bash, using a separate native environment:

```bash
git clone https://github.com/alvin0603/forge8.git
cd forge8
python3 -m venv .venv-wsl
./.venv-wsl/bin/python -m pip install .
./.venv-wsl/bin/forge8 read ./examples/isolated-functions --browse-only --state "$HOME/.local/state/forge8-browse"
```

Choose your own absolute state path outside the project; WSL state belongs on
the Linux filesystem, not `/mnt/c` or `/mnt/d`. It stores private source snapshots.
Open the private URL printed in the terminal. Try `exporter/rows.py`, expand
the file's definition outline, and search for same-spelling calls to reach
`export_calls.py`.

This mode provides static browsing, not AI answers or execution. Closing the
browser does not stop the service; press Ctrl+C in the terminal.

## Add local AI reading

The [Windows setup guide](docs/INSTALL.md) provisions the pinned Qwen reader and
native runtime into directories you choose. It shows the plan and asks before
installing or downloading. Inspect it first:

```powershell
.\scripts\setup.ps1 -Assets D:\Forge8\assets -State D:\Forge8\state\windows -WhatIf
```

Follow the guide to approve setup, then restart without both browse-only flags:

```powershell
.\.venv\Scripts\forge8.exe read .\examples\isolated-functions
```

Open `merge_records.py`, select its code, add it to the question, and ask what happens
when two rows have the same key. Check the answer against its citations.
The first question loads the model; opening files does not.

Native WSL GPU reading has run on the reference laptop, but a fresh Linux CUDA
runtime still requires an advanced local build. It is not a turnkey installation.
See [platform support](docs/PLATFORMS.md) and [measured limits](docs/VALIDATION.md).

## What you can do

- Read bounded source with clickable citations; inspect name origins and explicit
  import paths before choosing additional context.
- Navigate definitions, keyword matches, and individual same-spelling calls across
  files. These are static candidates, not a resolved call graph.
- Explicitly try small Python functions in a separate WASI guest, inspect reported
  results and optional line visits, or pin one input/result beside the next trial.
- Compare local HEAD with saved changes. Preview and try nearby inputs to find
  one that exposes a different return or exception, without writing a test suite.
  Separate Python repair can propose a patch checked by existing tests.

Trials require [optional setup and consent](docs/isolated-experiments.md); nothing
runs automatically from an answer. Repair needs separately provisioned assets,
and its checks execute trusted project code on the host. See [usage](docs/USAGE.md).

## Scope and trust

Manual questions use at most three source ranges, not the entire repository.
Citation checks establish source identity and coordinates, not whether an
explanation is true. Guest reports do not establish program equivalence or prove
an AI answer correct. This is a development preview, not a cloud-model replacement.

## Documentation

- [Install](docs/INSTALL.md): model-free browsing and Windows AI setup.
- [Use the desk](docs/USAGE.md): source selection, questions, navigation and export.
- [Platform support](docs/PLATFORMS.md) and [measured limits](docs/VALIDATION.md).
- Optional workflows: [function trials](docs/isolated-experiments.md),
  [change reading](docs/change-reading.md), [observed tests](docs/observed-reading.md).
- [Security](docs/SECURITY.md) and [contributing](CONTRIBUTING.md).

Forge8 is [MIT licensed](LICENSE). Retained upstream examples keep their own
licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
