# Platform support

Forge8 uses one application core with separate native Windows and WSL Python
environments. Running a Windows executable from WSL is not native WSL support.

| Workflow | Windows | WSL |
| --- | --- | --- |
| Model-free source browsing | Exercised in a fresh native installation | Exercised in a fresh native installation |
| GPU reading | Exercised on the reference laptop; guided fresh setup is provided | Exercised with a locally built Linux CUDA runtime; fresh setup is advanced/manual |
| Explicit WASI trials | Exercised with the separate Windows runtime | Exercised with the separate Linux runtime |

The reference is one ASUS FA507NV laptop with an RTX 4060 Laptop GPU
(8,188 MiB reported VRAM) and approximately 32 GB RAM. Native acceptance used
Windows Python 3.10.11 and WSL Python 3.12.3. These are not results from two
physical machines, a minimum-hardware guarantee, or macOS support.

## Fresh installations

Python 3.10+ with venv support and a local browser are required. Install the
trusted checkout with `python -m pip install .`; do not assume a PyPI release.
Create separate `.venv` and `.venv-wsl` environments. No model is needed for
`read --browse-only --state ABSOLUTE_DIRECTORY`.

[Windows setup](INSTALL.md#add-windows-ai-reading) provisions the pinned Qwen
model and two native CUDA runtime ZIPs after explicit confirmation. It does not
install a driver, CUDA toolkit, Gemma weights, or the optional WASI runtime.
Successful installation and byte verification do not prove inference readiness.

WSL AI currently depends on a local Linux CUDA build. Its tracked manifest pins
the reference build, not a downloadable public Linux CUDA package. Fresh builds
may differ; they need an explicitly reviewed manifest, not blindly replaced
hashes. See [installation limits](INSTALL.md#wsl-ai-is-an-advanced-deployment-not-this-installer)
and the [advanced build recipe](native-wsl-build.md).

## Storage and privacy

Keep large assets on a drive with sufficient space; `D:\Forge8\assets` is an
example, not a required location. Verified model weights can be shared between
Windows and WSL, but native runtimes and configuration records remain separate.

Use a private state directory outside the project and assets. WSL state must
live on its Linux filesystem, for example `$HOME/.local/state/forge8`, not a
Windows-mounted `/mnt/c` or `/mnt/d` path. Windows state uses Windows paths.
Snapshots, questions, reports, and exports can contain private source.

Reading uses local inference after assets are provisioned; installation may
need network access. Optional host test execution has separate trust and network
boundaries. See [security](SECURITY.md) and [validation](VALIDATION.md) for the
tested workflows, measurement scope, and remaining answer-quality/latency limits.
