# Install from a fresh checkout

Forge8 is a development preview. Install this checkout, not an assumed `pip install forge8`
PyPI release. Python 3.10+ and a local browser are required. Windows and WSL use
separate native Python environments. Do not reuse a Windows venv from WSL.

## First, browse without any model

In the cloned Forge8 directory, native Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\forge8.exe read .\examples\isolated-functions --browse-only --state D:\Forge8\private-browse
```

Native WSL Bash:

```bash
python3 -m venv .venv-wsl
./.venv-wsl/bin/python -m pip install .
./.venv-wsl/bin/forge8 read ./examples/isolated-functions --browse-only --state "$HOME/.local/state/forge8-browse"
```

Install Python/venv first if those commands are unavailable; Forge8 does not install
OS prerequisites. Building from source fetches the pinned setuptools build dependency
unless already available. An already built trusted Forge8 wheel can instead be installed
with `python -m pip install --no-index --no-deps PATH_TO_WHEEL`.

Open the private URL printed in the terminal. Try `exporter/rows.py`, its definition
outline and same-spelling call search to reach `export_calls.py`. These are source-navigation
features, not AI answers or execution. Closing the page does not stop the service;
press Ctrl+C in the terminal. Snapshots retain private source in the selected state path.
State must be outside the project; use Linux ext4 for WSL state, not `/mnt/c` or `/mnt/d`.

## Add Windows AI reading

You need a compatible NVIDIA Windows driver/GPU, native 64-bit Python 3.10+ and
64-bit Windows PowerShell 5.1+. The reference machine is an RTX 4060 Laptop with
8 GB VRAM; another GPU or driver has not thereby passed inference acceptance.
Keep the checkout trusted: package installation executes Forge8's build code.

From the checkout, inspect the plan, then run it:

```powershell
.\scripts\setup.ps1 -Assets D:\Forge8\assets -State D:\Forge8\state\windows -WhatIf
.\scripts\setup.ps1 -Assets D:\Forge8\assets -State D:\Forge8\state\windows
```

Choose your own absolute, separate asset/state directories outside the checkout.
The script shows storage and network use and asks for `YES` before installation.
`-WhatIf` only checks the plan; it does not install, download, configure or start a model.
If your execution policy blocks scripts, inspect the script and follow your machine's
approved policy; this installer does not change execution policy or require an admin shell.
If a WSL-launched PowerShell reports that `PATHEXT` lacks `.EXE`, use a fresh native
Windows terminal with its normal environment. Setup does not edit system settings.

The script installs Forge8 into `.venv`, copies the checkout's configuration into a
new asset root, and provisions only the manifest-pinned Qwen3.5-9B Q4_K_M model
(5,680,522,464 bytes) and two Windows llama.cpp b10621 CUDA ZIPs. It retains the ZIPs,
checks their full hashes before extracting the exact flat runtime inventory, then
uses Forge8's existing full runtime/model verification and `configure` commands.
Allow at least 12 GiB for a fresh asset installation. No Gemma weights, CUDA toolkit,
driver, global PATH changes or optional WASI runtime are installed. No model starts.

Existing files must match their pins. Wrong or partial files are left untouched;
inspect the named path instead of repeatedly rerunning. A partially extracted runtime
also fails full verification, rather than being overwritten. Existing configuration
files must match the checkout; a different saved deployment is not replaced.
Explicit `FORGE8_HOME`/`FORGE8_STATE_HOME` overrides must agree with your chosen paths.

Already have the three downloaded files in a local directory? Add `-Archives D:\my-downloads`.
They must have the exact manifest filenames and hashes. This prevents asset downloads,
but Python packaging may still access PyPI; it is not a fully offline installer.
Verified files already in the target asset tree are reused without copying weights.

After setup, reopen **without both** browse-only flags (`--browse-only` and `--state`):

```powershell
.\.venv\Scripts\forge8.exe read .\examples\isolated-functions
```

The first explicit question loads the model; browsing does not. Select source, ask a
self-contained question and check its references. Answers can be slow and materially
wrong; installation success is not GPU readiness or semantic verification.
[Reading workflow](USAGE.md#read-selected-code) ·
[Diagnostics](USAGE.md#diagnose-the-selected-reader).

## WSL AI is an advanced deployment, not this installer

WSL source browsing above works without assets. Native WSL AI on the reference laptop
uses a locally built Linux CUDA runtime; the tracked manifest pins that private build,
not a downloadable public Linux CUDA release. Windows `.exe` runtime files cannot be
substituted. A fresh source build is not guaranteed to reproduce those exact bytes.

[The build recipe](native-wsl-build.md) requires a compatible toolchain, regular-file
SONAME bundling, licenses and an explicitly reviewed runtime manifest before inference.
It is not currently a complete one-command fresh WSL AI installation. Do not regenerate
hashes merely to make an unexplained mismatch pass. Qwen weights may be shared on D,
but WSL's small private state must remain on ext4. [Platform evidence](PLATFORMS.md).
