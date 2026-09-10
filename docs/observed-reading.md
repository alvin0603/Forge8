# Read an observed execution path

Early feature: capture and read-only browser navigation have been exercised on
native Windows and WSL on the reference laptop. This is a way to inspect actual
source-line visits, not evidence that model answers improved or a general debugger.

`observe` and `read` are deliberately separate. `observe` imports your project
and executes one named unittest, including its fixtures and cleanup. Add
`--trust-project-execution` only after reviewing the project and its dependencies.
It runs on the original project with your user permissions (`process_only`):
files can be changed and network access is possible. There is no filesystem or
network sandbox, automatic rollback, or restriction to the selected files.
Forge8 starts no model/GPU for observation; trusted project code itself can use
any resources available to your account.

Use an already installed native Forge8 environment and its saved `configure`
record. The project must be outside the Forge8 asset directory. Its dependencies
must be installed in the **same Python virtual environment as the Forge8 CLI**,
not merely another activated project environment. Install only dependencies you
trust; Forge8 does not install them automatically. Windows and WSL have separate
native environments and package installations.

## Capture once, then open read-only

These are placeholders for an existing trusted project containing `cache.py`
and `tests/test_cache.py`; replace the paths and exact class/method names with
your own. No test is selected or executed automatically from a model answer.

Native PowerShell, using the executable from your Windows installation:

```powershell
$Forge8 = "C:\path\to\forge8\.venv\Scripts\forge8.exe"
$Project = "C:\work\trusted-project"
& $Forge8 observe $Project --test test_cache.CacheTests.test_reuse `
  --source tests/test_cache.py --source cache.py --trust-project-execution
# Replace the next path with the printed Observation path from a complete capture.
& $Forge8 read $Project --observation "C:\path\from\observe\observation.json"
```

Native WSL, using the executable from your Linux installation:

```bash
forge8_cli=/path/to/forge8/.venv-wsl/bin/forge8
project=/path/to/trusted-project
"$forge8_cli" observe "$project" --test test_cache.CacheTests.test_reuse \
  --source tests/test_cache.py --source cache.py --trust-project-execution
# Replace the next path with the printed Observation path from a complete capture.
"$forge8_cli" read "$project" --observation /path/from/observe/observation.json
```

Saved asset/state paths are reused. Explicit `FORGE8_HOME` and
`FORGE8_STATE_HOME` still override them; keep WSL state on its Linux filesystem,
for example `$HOME/.local/state/forge8`, not `/mnt/c`. No shell-profile changes
are needed. See [native setup](INSTALL.md).

`--test` accepts exactly `module.Class.test_method`, not a shell command,
discovery pattern or pytest selector. Repeat `--source` for 1–4 explicit
repository-relative `.py` files, including the test module. Each selected file
must be regular UTF-8 and at most 64 KiB; links and unsafe paths are rejected.
The capture timeout is 60 seconds, plus bounded cleanup. Ctrl+C requests child
cleanup. Windows uses a Job Object; Linux cleans the owned process group even
after its leader exits. Descendants that escape that group are not contained;
abruptly killing Forge8 is not a cleanup guarantee.

## Interpret the result

`--json` exposes `complete`, `test_passed`, counts, `capture_process` and the saved
`observation` path separately. Exit 0 means capture completed, **not** that the
test passed. An ordinary assertion failure can yield a complete, useful trace.
Do not load an incomplete capture as if it described every selected-file event.
The private run also contains test output; although event records omit values
and exception payloads, test stdout/stderr may contain secrets. Do not upload it
without review.

The report is capped at 1,000 events and 128 KiB. It records synchronous Python
call/line/return/exception positions in the selected files, not arguments,
locals, return values, C function internals, earlier test-loading imports/test
construction, or other threads/processes. A line event
precedes execution; a return event is not an operation-success flag. Parent IDs
omit unselected frames. Limits, unsupported execution or source drift make the
capture incomplete; missing events cannot prove that no filesystem write occurred.

`read --observation` only reads and validates the saved report against current
admitted source. It never imports or executes the project. Refresh the desk after
editing source: changed bytes invalidate the displayed path's binding.
A file hash is not authentication
of a report from someone else. The desk's call navigation and ordered-path
comparison require no model. Asking a question still sends only the selected
source through the existing reader: the imported trace is **not** added to the
prompt. Model prose can contradict an observed path; inspect the code and events.

Choose a call to inspect its ordered line visits, then choose another call to the
same function to compare their common prefix and next positions. Repeated visits
are retained. The parent-position link goes to the selected parent's last observed
line before the child call; unselected intermediate frames may be omitted, so it
is not necessarily the direct call site. Navigation does not add model selections.
