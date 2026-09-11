# Try a function without running its project on your host

An optional CPU-only trial takes a function name, JSON arguments and complete
Python source: one module by default, or a small explicit set. It runs in a
separate CPython/WASI environment, not your project's Python installation.
No model, GPU, existing tests, project dependency installation or network is used
by `setup` or `run`. Reading and asking questions never trigger experiments.

Useful for data transformations, configuration helpers and small algorithms.
Not a general project runner: the original project is not mounted, native
third-party packages are unavailable, and platform behavior differs. Source
admission uses your host Python parser; syntax newer than that interpreter is rejected even
though the guest is Python 3.14.7. Async entry points are not supported.

The interface is currently Traditional Chinese. Action descriptions below are
English explanations, not literal button labels. See [validation](VALIDATION.md)
for the tested Windows and WSL workflows.

## Install the optional runtime once

Forge8's regular installation remains dependency-free. Obtain these pinned
archives separately; they can be transferred offline. Keep the archives and
runtime on a drive with enough space; the examples use D:.

| Archive | SHA-256 |
| --- | --- |
| [python-3.14.7-wasi_sdk-24.zip](https://github.com/brettcannon/cpython-wasi-build/releases/tag/v3.14.7) | `2e064d3fb8172471d39d741348efa722349c40b96301f69968dff714999c584b` |
| [wasmtime-48.0.0-py3-none-win_amd64.whl](https://pypi.org/project/wasmtime/48.0.0/#files) | `21fa500e70f3819a8c0539c3f0be6b3b81ec3c630bb90c47dba4d8a2c1d4c698` |
| [wasmtime-48.0.0-py3-none-manylinux1_x86_64.whl](https://pypi.org/project/wasmtime/48.0.0/#files) | `58544d539053dff7bd4cf30c40d7a540862d683013c0dfa6ba46a063f5b682f7` |

Only the Python archive and the current native host's wheel are required.
The Python build is Brett Cannon's **unofficial maintainer build**, not a
python.org binary. Wasmtime is a Bytecode Alliance dependency. These are trusted
runtime components; hashes do not independently certify their build provenance.

Native PowerShell, using your installed `forge8` executable:

```powershell
forge8 experiment setup --archives D:\Forge8\downloads `
  --runtime D:\Forge8\experiment-windows
```

Native WSL, using the Linux installation:

```bash
forge8 experiment setup --archives /mnt/d/Forge8/downloads \
  --runtime /mnt/d/Forge8/experiment-linux
```

The destination must be new, with an existing parent. Setup verifies both
archives, bundles every original standard-library member into a deterministic
uncompressed ZIP (unchanged member bytes), then compiles the fixed
interpreter once. It never overwrites an existing runtime. On failure it retains
the partial directory and diagnostics; choose a new directory after diagnosing
the cause. Windows and WSL need separate native compiled runtimes.
The complete installed inventory, including that ZIP, is hashed before and after
each call. Bundling avoids hundreds of small-file operations across WSL/Windows;
it does not skip verification or cache a previous integrity result.

Omit `--runtime` to use existing `FORGE8_HOME` or saved Forge8 asset settings;
there is no current-directory discovery and no requirement to load a model.
No new global configuration, PATH edits or Python package installation occurs.

## Try an input beside the source in ReadingDesk (preview)

ReadingDesk uses the optional runtime in its configured **asset directory**;
it does not discover the custom `--runtime` paths above. If that native runtime
is not already installed, run setup once **without `--runtime`**, using the same
`FORGE8_HOME` or saved asset settings as your reading desk:

```powershell
forge8 experiment setup --archives D:\Forge8\downloads
forge8 read "C:\work\project" --allow-experiments
```

```bash
forge8 experiment setup --archives /mnt/d/Forge8/downloads
forge8 read '/mnt/c/work/project' --allow-experiments
```

The flag enables the controls for this desk session; it does not execute anything.
Without it, experiments remain disabled. No model is loaded by a trial.

1. Open a `.py` file in ordinary source mode, expand the definition outline, and
   choose the trial action beside a synchronous top-level function. This only checks the
   retained module and entry; it does not execute them.
2. Check the filename/function; expand the source and execution details for the
   snapshot and complete-module SHA-256.
   Enter JSON such as `{"args": [[1, 2, 3]], "kwargs": {}}`. Input is sent as raw
   text, without browser number conversion, automatic correction or truncation.
3. Explicitly confirm execution. This authorizes initialization of
   the **whole module**, followed by that function call—not just selected lines.
   Read the returned JSON or exception beside the source; expand the
   unverified-output section for stdout/stderr. Large integers remain exact.

Wide panels place input and output side by side. Narrow panels reveal a newly
returned result without moving keyboard focus or your source/question position.
Editing JSON keeps the previous result visible but explicitly labels it as the
previous call's result; only another execution click runs the new input.
Returned JSON displays readable Unicode directly; invisible
controls, bidirectional controls and lone surrogates remain escaped. It stays
JSON text throughout the browser, so large integers are not rounded.

### Bring arguments from an existing call

After choosing a trial function, you can browse another `.py` file without
changing that target. Select the complete same-spelling call, expand the optional
literal-input controls, check the displayed source and trial target, then request
argument carryover. This explicitly replaces the JSON draft,
not the existing result. It does not execute the calling file, load a model or
select any additional modules. You still confirm execution separately.

For the exporter example below, first prepare `exporter/rows.py :: render_row`
and its three selected modules. Then open
[export_calls.py](../examples/isolated-functions/export_calls.py), select the
`render_row(...)` call on lines 7–9, and bring in its arguments. There is no need
to type the JSON or execute `example_row`. The original question and selections
remain in place.

This is a literal reader, not a call-binding resolver: the spelling may be
shadowed or refer to another function. Only strings, integers, finite floats,
booleans, `None`, lists and unique string-key dictionaries are accepted, including
explicit keyword arguments. Variables, nested calls, `*`/`**`, tuples, sets,
duplicate keys, partial calls and multiple matching calls are refused without
changing the draft. Aliases and attribute calls are not resolved. The selected
span is at most 80 lines / 4,000 UTF-8 bytes; generated JSON remains limited to
16 KiB. The host Python parser and retained-source restrictions still apply.
Changing source, selection, target or draft while the request is pending prevents
the late response from replacing newer work. The source SHA and call coordinates
are available under provenance until the draft is edited; that draft provenance
is not persisted across a browser reload.

The trial uses the desk's retained raw module bytes, not a reconstruction of
displayed lines or later editor changes. To try newly saved edits, finish or
cancel the trial, then refresh the project. Integrity warnings refer to the retained
snapshot/input and runtime, not a check that your live checkout is unchanged in
ordinary single-version mode. Paired HEAD/current mode additionally guards live
source and HEAD as described below.
Running a trial preserves the reading question, manual selections, existing
answer and question history. A trial and a model question cannot run simultaneously; refreshing
the project is also blocked while work is active.

Use the trial's cancel button and wait for cleanup. Closing or reloading the
browser does **not** cancel: reload retrieves the same trial without executing again. Ctrl+C
in the hosting terminal cancels active work and waits for cleanup. If cleanup
cannot be confirmed, the desk blocks new trials, model questions and refresh;
inspect the terminal's process information rather than treating it as stopped.
The status-recheck button only queries status; it never retries execution or cleanup.
Successful project refresh clears the desk's trial display; it is not a trial
history. Private inputs and available reports remain in Forge8's local run state.

The default is one self-contained module; [selected-module mode](#include-explicitly-selected-project-modules-preview)
can supply a few same-project imports. Neither mode supports arbitrary projects,
methods or async functions. HEAD/current trials use the explicit paired mode below,
not these ordinary single-version/module-set controls. Opening a file, selecting lines,
asking the model or receiving an answer never starts a trial or feeds its output
back to the model.
The limits and environment differences below still apply; an observed result is
not a verified explanation.

### Keep one completed result A while trying input B

In ordinary **single-file** mode, you can explicitly retain one completed trial
beside the next manually executed trial. No new runtime or model is needed.
With the example desk open at `examples/isolated-functions`:

1. Prepare `merge_records.py :: merge_records`, without extra modules. Leave line
   tracing off for both calls. Enter `{"args":[[]],"kwargs":{}}` and explicitly run.
2. After the call finishes, pin its report as A. A retains its
   submitted input and empty-list return; this click does not execute anything.
3. Replace the input draft with the complete
   [merge-input.json](../examples/isolated-functions/merge-input.json), then explicitly
   run again. B should report key `A`=3 then key `B`=9007199254740993: last value wins while
   first-seen key order remains. The reports differ; fixed A is still the empty list.
4. Inspect B's submitted-input disclosure separately from the next-call draft.
   The replace-baseline and clear-baseline actions change A explicitly; neither
   runs a call.

Only matching retained snapshot/file/source, entry, runtime and line-tracing policy
are comparable. Inputs may differ: their original JSON is kept as text, preserving
large integers without browser numeric conversion. The host compares guest-reported
JSON, not the draft or model prose. Two identical exceptions count as **same**, not
a passing test; differing reports do not prove that input alone caused the difference.
Cancelled, incomplete or unbound B has no comparison result and does not replace A.

Pin/clear control exchanges have a 10-second timeout, not an execution deadline.
An uncertain reply is recovered by status GETs only, without replaying the update
or trial; use the status-recheck button if needed. Persistent uncertainty requires
closing the service in its terminal. Browser reload likewise retrieves state only.
Successful project refresh clears A even when source hashes are unchanged; restarting
the service loses the in-memory baseline. Private trial files remain on disk, but
there is no trial-history database, batch runner or automatic model feedback.

### Inspect guest-reported call line visits

For an ordinary **single-file** trial, enable the optional line-recording checkbox
before confirming execution. The result adds ordered source-line visits,
including repeated loop lines and same-file helpers. Expand the submitted-input
disclosure to inspect the exact JSON separately from your editable next-call draft.
Changing the draft or checkbox neither executes it nor changes the old report.

Use the step number, previous/next controls and source-view button to inspect a
checked source excerpt. The first source fetch is explicit and read-only; later
steps reuse that checked excerpt source. It does not move the main source selection,
change your question/history, enlarge model context or start inference. The excerpt
contains at most seven complete lines / 4,000 characters; an oversized active line
is not silently clipped. Browser reload retrieves the submitted job without replay;
unsent input/checkbox drafts are not promised across reloads.

From the repository root, the installed native CLI supports the same opt-in:

```text
forge8 experiment run examples/isolated-functions/merge_records.py --entry merge_records --input examples/isolated-functions/merge-input.json --allow-execution --trace-lines
```

Use the platform's native executable and already provisioned runtime. Add `--json`
for the full report: optional `reported_trace` is separate from the unchanged
`reported_result` envelope. See [the reading workflow](USAGE.md).

This remains **guest-reported, untrusted** data. At most 1,000 line events are
captured during the explicit function call, after module initialization and before
exception formatting/result serialization. Only original compiled code identities
from that module qualify; external code and dynamically generated code with the
same filename do not. No locals, frame graph, argument values or intermediate
values are captured. A line event precedes execution, not successful completion.

`truncated` means only a prefix was retained. `hook_intact` checks the installed
hook at call end, not continuous observation; even `true` does not prove completeness.
Guest code can interfere with the collector or its report. Missing/malformed output,
source/runtime drift, incomplete cleanup, timeout or cancellation cannot produce
a usable path. An ordinary call exception may still have a bounded trace.
Instrumentation can increase execution time and cause a timeout; there is no
automatic untraced retry. The existing time/memory/output caps stay unchanged.

Default-off, selected-module and paired-trial behavior is unchanged; enabling
tracing with extra modules is refused. Reports are not converted to host `observe`
captures or fed back to a model as verified claims.

### Compare HEAD and current with the same input

With the same `forge8 read <git-project> --allow-experiments` command, switch to
change-reading mode and open the current (`after/`) version of a `.py` file.
Use the complete local Git root with an ordinary `.git` directory, existing HEAD
and native Git 2.43+; [change-reading admission limits](change-reading.md#bounds-and-unsupported-comparisons)
apply. A source subdirectory alone is not a Git root.
Choose the paired trial beside a synchronous top-level function. Preparation
resolves the same path/function in retained HEAD and shows both complete-module
hashes; it neither executes code nor asks a model. The AI question's three source
selections are not required for this independent trial.

Enter one JSON argument draft, then explicitly authorize **two** complete-module
initializations and one call per version. Two separate WASI guests run sequentially
with identical raw input. The panel retains each guest's return or exception and
separate logs, and labels the JSON reports as same, different or unavailable.
For example, changing `return amount * 2` to `return amount * 3` with input
`{"args":[7],"kwargs":{}}` should produce reports `14` and `21`; that is one observed
input, not proof about every input. No existing test suite or GPU is needed.

`canonical-json-v1` ignores object-key order but preserves list order, exact integers,
bool/int/float distinctions, and exception phase/type/message. Floats have the host
JSON decoder's binary precision. Results deeper than 64 containers or exceeding
393,216 canonical ASCII characters are not compared. The browser displays text
without converting large numbers. Equal reports do not establish equivalence; a
difference may involve time/randomness, not just the edit. Guest code can influence
its own report, so this is **not a trusted oracle or verification of AI reasoning**.
Two identical guest-reported exceptions are also "same", not a repaired function
or a passing test.

Initially both sides must have one complete module at the same path, each at most
64 KiB, with the same synchronous top-level function. Added/deleted/renamed modules,
methods, async functions, extra project modules and automatic literal carryover are
not supported in paired mode. The fixed caller selected for change reading is **not**
executed by these trials. Module initialization happens twice and is explicitly
included in consent; no purity assumption or function extraction is made.

Source/HEAD guards run before, between and after. Cancellation or the first trial's
infrastructure/integrity failure prevents the second; a normal guest-reported
exception can still be compared. Missing results, drift or incomplete cleanup never
become "same". Closing/reloading the browser only retrieves state, never reruns a
trial. Editing input leaves old results visibly labelled; refreshing requires no
active work and captures a new comparison. A private `comparison-experiment.json`
binds both actual child reports, input, source and runtime identities without
changing earlier reading receipts. The ordinary single-version trial remains available.
Expand the submitted-input disclosure beside the comparison verdict to inspect the
exact recorded JSON while reading the results. It does not follow the newer draft,
parse or round its numbers, or trigger execution. Worker/WASI completion labels
describe the execution environment, not whether the function is correct.

### Find an input that exposes a change

In the same paired panel, enable nearby-input search after entering a seed. The
first button click **only previews** the complete input list. A second click
authorizes the displayed plan. Editing the seed or changing the target requires
a new preview. This needs the optional WASI runtime, not a model or existing tests.
The preview opens in full before consent and folds away during execution to leave
room for results; you can reopen it without running anything.

For example, two versions of a configuration helper may both return `5` for a
timeout of `5`, while disagreeing on `0`: `options.get("timeout") or 30` substitutes
`30`, but `options.get("timeout", 30)` preserves `0`. A nearby-input search can
try that boundary without you manually guessing and submitting each pair.

`nearby-v1` includes your original input and at most 11 variants. Each variant
replaces one scalar value with a common boundary: zero or adjacent integers,
empty/whitespace strings, a flipped boolean, or a small replacement for null.
It visits arguments then keyword arguments in their original order, interleaving
replacements across at most 32 scalar locations. It does not combine changes,
infer valid input contracts, alter containers or minimize a discovered example.
The preview identifies when candidates were limited; it is not exhaustive.

After consent, one cancellable job runs fresh HEAD/current guests sequentially.
It stops at the first differing reported return or exception, with at most 24
whole-module initializations and a 120-second cooperative search budget, plus
cleanup. Integrity failures stop the search; it does not retry or skip failures.
The found input appears beside both results. Your original seed and editable
draft remain separate; neither is replaced with the generated input.

No difference means **no difference among the inputs tried**, not equivalence.
Generated inputs can be outside your function's valid domain, and time/randomness
can produce differences unrelated to an edit. Only guest-reported returns and
exceptions are compared: changes to argument objects, external effects and stdout
are not compared. A module-initialization or serialization exception does not
establish that the selected function completed. Nothing is sent back to the model.

Reload retrieves the same job without replaying it. Private run state retains
the exact plan and per-input paired reports; it is not a public test suite or a
new differential-testing algorithm.

## Submit exactly one call

From the Forge8 repository, after substituting your installed runtime path:

```powershell
forge8 experiment run examples/isolated-functions/merge_records.py `
  --entry merge_records --input examples/isolated-functions/merge-input.json `
  --runtime D:\Forge8\experiment-windows --allow-execution
```

```bash
forge8 experiment run examples/isolated-functions/merge_records.py \
  --entry merge_records --input examples/isolated-functions/merge-input.json \
  --runtime /mnt/d/Forge8/experiment-linux --allow-execution
```

The expected JSON result is A with value 3, then B with value
9007199254740993. This example also exercises standard-library dataclasses and
large integers without JavaScript numeric conversion. Submitting
`missing-key.json` instead exposes the actual constructor exception.

`--allow-execution` authorizes **the whole module's initialization and one call**,
not only the function body. Nothing is extracted or rewritten; missing imports
are not faked or installed. Inputs must be exactly `{"args": [...], "kwargs": {...}}`.
Source is limited to 64 KiB UTF-8; input to 16 KiB. Only JSON-compatible return
values can be reported; serialization errors are distinguished from call or
module-initialization errors. Tuples become JSON arrays.
The bundled standard library uses CPython's built-in ZIP importer. Module
`__file__` paths inside a ZIP cannot be opened as ordinary filesystem files;
this is not filesystem-identical to an unpacked Python installation.

Add `--json` for separate host status, untrusted guest output and the
guest-reported result. Exit 0 means the bounded execution finished with unchanged
inputs/runtime; **it does not mean the function succeeded or a test passed**.
An exception can be a useful completed observation. Limits, source drift and
runtime failure produce a nonzero exit. If files change while the call runs,
the captured version and result remain in the private run, marked as no longer
bound to the live files. Existing native Forge8 state settings are reused.

## Include explicitly selected project modules (preview)

For a function that imports a sibling helper or package constant, provide the
complete source files yourself. This uses the **same optional runtime**, not a
new deployment or dependency installer. The limit is **1–4 files and 64 KiB total**,
including the entry and every required parent `__init__.py` (even empty ones).
Without the following flags, single-file behavior is unchanged; project imports
are not added automatically.

From the Forge8 repository, using the runtime in your configured asset directory
(or add your explicit `--runtime` path as above):

```powershell
forge8 experiment run examples/isolated-functions/exporter/rows.py `
  --entry render_row --input examples/isolated-functions/export-input.json `
  --module-root examples/isolated-functions `
  --module-file exporter/__init__.py --module-file exporter/escaping.py `
  --module-file exporter/rows.py --allow-execution
```

```bash
forge8 experiment run examples/isolated-functions/exporter/rows.py \
  --entry render_row --input examples/isolated-functions/export-input.json \
  --module-root examples/isolated-functions \
  --module-file exporter/__init__.py --module-file exporter/escaping.py \
  --module-file exporter/rows.py --allow-execution
```

Both flags are required together: repeat `--module-file` for **every** selected
path relative to `--module-root`, including the entry itself. This example imports
`exporter.rows`; its parent supplies the semicolon delimiter and its sibling
quotes fields. Expected for the supplied input: quoted semicolons and embedded
newlines, doubled internal quotes, and a final CRLF.

For the same trial in ReadingDesk, launch
`forge8 read examples/isolated-functions --allow-experiments` from the repository.
Open `exporter/rows.py`, choose the trial action for `render_row`, then expand
the optional project-module controls. The entry is already included; select
`exporter/__init__.py` and `exporter/escaping.py`, then request module checking.
Check the file list and snapshot/hash details, paste
[the example JSON](../examples/isolated-functions/export-input.json), then
explicitly authorize importing all three complete modules and calling `render_row`.
Selection and checking do not execute anything. Changing selections requires checking again; clearing
the extra files and checking returns to single-file mode.

The desk's opened source root is the import root: open `examples/isolated-functions`,
not `exporter` or the whole Forge8 checkout for this example. There is no automatic
`src`-layout discovery, namespace-package support, recursive dependency selection,
stub generation or package installation. Use UTF-8 source and relative `.py` paths
whose module/package components are ASCII identifiers. Missing parent initializers,
conflicting encoding cookies, duplicate/case-colliding paths and module/package
name collisions are refused. A root-level `__init__.py` has no named package.

The exact selected bytes become one deterministic `ZIP_STORED` archive, at most
128 KiB. CPython's native ZIP importer loads the named entry module; parent-package
and imported-module initialization may run before the function. The guest rejects
selected top-level names that collide with its standard library, builtins,
already-loaded interpreter modules or another existing import target (including
frozen modules). This includes standard-library names unavailable in WASI.
Missing imports stay real import errors; this is not the full project
environment. Source and ZIP hashes remain in the private report and are rechecked
after execution; ZIP drift suppresses the separate result display, while raw logs
remain untrusted. All existing input, memory, time, output and cancellation limits
remain unchanged.

## Boundary and limitations

Single-file mode preopens only the pinned standard library, read-only. Selected-module
mode additionally preopens a read-only directory containing **only the generated
selected-source ZIP**, not the original project, snapshot or private run directory.
The guest receives no host environment, project mount, inherited stdin or
host-created socket. There is no automatic model tool access. This is Wasmtime
capability isolation, not an absolute security guarantee against runtime vulnerabilities or a certification
for hostile multi-tenant workloads. Runtime and local AOT assets selected by the
operator are trusted; never point `--runtime` at a downloaded project bundle.

Each call has 128 MiB guest linear memory, 64 KiB combined retained stdout/stderr,
a five-second guest interruption timer, and a ten-second worker wait followed
by bounded cleanup. Host memory is also bounded: Linux uses a 2 GiB virtual
address-space limit; Windows a 1 GiB process committed-memory Job limit. These
are different OS measurements, not equivalent RSS limits. Linux additionally
limits CPU time, file descriptors and core dumps. Setup has a separate bounded
compilation allowance. Runtime verification and process setup add latency.

Blocking runtime calls may need the outer process deadline; Ctrl+C requests
owned-process cleanup. Raw output is always untrusted and may contain sensitive
input values. Keep private run artifacts local. A returned value can contradict
an explanation, but agreement on outputs does **not** prove its causal reasoning.
