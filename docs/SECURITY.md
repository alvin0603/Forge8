# Forge8 security model

- **Status:** implemented `forge8 fix` and read-only `explain` / `read` boundaries,
  not an OS sandbox
- **Last reviewed:** 2026-09-10
- **Scope:** single-user native Windows and native WSL, using separate native
  Python and model-server processes

Forge8 turns a bounded repository repair into a reviewable patch and evidence
bundle, or one read-only code question into a citation-bound explanation bundle.
The public surface includes deployment diagnostics, repair, source reading and
separately authorized observation/guest trials. `fix` does not make arbitrary project
code safe to execute; source-reading commands never execute or write repository code.
The current boundary has three
deliberately different kinds of control:

| Layer | Meaning today |
| --- | --- |
| **Enforced before execution** | Input, paths, runtime identity, and allowed model actions are checked before the relevant read; `fix` additionally guards writes, fixed commands, and Windows check-process attachment, while `explain` exposes no effect or repository-code execution action. |
| **Detected and fail-closed afterward** | Independent inventories, clean replay, checks, lifecycle evidence, and package binding can invalidate a run after an effect or process has executed. They do not undo host side effects. |
| **Not enforced** | Filesystem containment, network denial, hostile-race resistance, and Windows owner-only ACLs are not provided. `fix` tests remain trusted `process_only` code; neither product path is OS-sealed. |

Do not collapse these layers into one “sandbox” claim. A detected mutation is
evidence for `NOT VERIFIED`; it is not proof that the mutation was prevented.

## 1. Enforced before execution

### App-local deployment settings

The optional Windows `scripts/setup.ps1` is a trusted-checkout installer, not part
of inference. It asks before installing the local Python package or downloading
pinned Qwen/runtime files; `-WhatIf` performs plan checks only. Missing assets go
to explicitly selected paths, ZIPs are hash-checked before bounded flat extraction,
and ordinary complete runtime/model verification remains required. Existing mismatched
files and partial downloads are not overwritten. Setup does not change execution
policy, drivers or PATH, execute project code, or certify GPU/answer readiness.
`-Archives` avoids asset downloads only; Python packaging can still contact PyPI.
It is not protection against a malicious checkout or hostile same-user filesystem races.

`configure` records native asset and state paths in a per-platform, app-local
JSON file; it does not change shell profiles, global environment variables or
drivers. Existing settings require explicit `--replace` to change. Path and
record validation reject links, non-regular files and malformed or oversized
records. Configuration checks layout, not model identity or readiness: each
job still performs its normal asset verification and source/state overlap
checks. These settings are not a hostile same-user race or Windows ACL boundary.

`doctor` reads these settings and reuses one full production preparation for the
selected reader, default Qwen. It has no project/write/execute option and does
not change settings or start a model/runtime/project process. It reports missing
assets and resident asset/state overlap, but does not probe GPU, ports, actual
state writes or snapshot publication. A pass is not inference readiness, project
admission, or a replacement for job-time verification. Reported paths may be private.
Before a resident generation or secret exists, failed asset/profile preparation
now reaches the existing actionable CLI formatter; errors after secret issuance
remain redacted. No hash, overlap, shutdown or secret-clearing gate is weakened.

### Repository admission and sanitized ingress

Before any selected `fix` test or either product path's local model is started,
both commands pass through the same bounded repository admission. It:

- requires an existing non-root source, an existing run-root parent, and no
  source/run overlap; `explain` requires an absent run root, while `fix` admits
  only its separately defined absent-or-empty preparation shape;
- inventories entries with no-follow metadata/final-file opens and publishes a
  content-pinned sanitized snapshot; `fix` later maintains a separate candidate
  staging tree while `explain` keeps only read-only input/evidence;
- admits only regular UTF-8 text without NUL or binary-control content;
- rejects symlinks, Windows reparse points, hard links, special files, case collisions, non-canonical Unicode,
  Windows reserved names, and non-portable path characters;
- rejects hidden and credential-like paths, including common SSH, cloud, container, key, certificate, `.env`, and
  package-registry credential names; the sole hidden-file exception is an exact regular UTF-8 `.gitignore`, whose
  bytes are fingerprinted but remain invisible to model workspace tools and permanently ineligible for writes;
- excludes known control/cache/build trees such as `.git`, `.forge8`, virtual environments, bytecode caches,
  `node_modules`, `dist`, and `build`;
- enforces 1,000 included files, 256 KiB per file, and 16 MiB total included bytes; and
- fingerprints every included file by path, size, and SHA-256, rescans after copying, and publishes only to a
  previously absent destination with fail-if-exists semantics.

Exclusion is not sanitization of the excluded content: excluded bytes are not
copied or hashed. The consequences are documented under “Not enforced.”
Forge8 does not parse `.gitignore` or use its patterns to exclude snapshot files.

For `fix`, writable roots must be named explicitly with `--allow-write` and already exist in the snapshot. Forge8 resolves actual
filesystem casing and rejects duplicate/overlapping roots, traversal, absolute or ambiguous paths, hidden/credential/test
paths, links/reparse points, special files, and hard links. A writable directory is recursively checked before admission.
`explain` accepts no writable roots and constructs its workspace policy with
writes disabled.

### `fix` model capability and guarded effects

The model does not receive a general shell, argv, environment, endpoint, model selector, commit, push, deploy, or
patch-apply capability. It emits strict typed actions; unknown fields, duplicate JSON keys, non-finite numbers, malformed
paths, and out-of-schema actions are rejected.

Every write is constrained to an admitted staging root. Existing-file writes carry the expected whole-file SHA-256;
stale preimages fail without an effect. Local replacements also require an exact unique text or line-range preimage.
Writes use bounded UTF-8 content and guarded atomic replacement. The model cannot widen its allowlist or check set.

Forge8 itself does **not** apply `changes.patch`, commit it, or push it. The operator must inspect and apply a successful
patch separately. This is not a promise that repository code cannot write source or other host paths; see below.

### Reading boundaries and the default Gemma explanation protocol

`read` serves only a loopback browser interface and an admitted, versioned source
snapshot. APIs require a per-session bearer key, the exact local Host, and a
same-origin Origin for writes. The page removes the URL's key fragment and uses
per-tab session storage, not cookies or localStorage. It does not serve arbitrary
filesystem paths, execute HTML from source/model output, or allow remote origins.
These checks are not a multi-user or same-user-malware boundary.

Explicit `read --browse-only --state ABSOLUTE_DIRECTORY` creates the same admitted
source snapshots without looking up model assets or deployment settings. It ignores
`FORGE8_HOME` / `FORGE8_STATE_HOME` only in that explicitly selected mode; there is
no automatic fallback from failed ordinary deployment. The supplied state path uses
the existing ancestor, link/reparse and source-overlap checks and remains private
source-bearing storage. Native WSL still requires its supported ext4 state location.
Source/search/name/import/traceback navigation retains the ordinary authentication,
version, byte-identity and budget gates. No model supervisor is constructed, and
all inference starts, guest trials and Git-comparison preparation are refused before
worker creation. The page hides those actions and does not poll model/jobs/history.
This mode is a static navigator, not AI explanation or stronger OS containment;
normal configured reading must be reopened explicitly to use a model.

Question-control HTTP exchanges (submit, cancel, current status and history)
are bounded to 10 seconds through response-body decoding. Client timeout/abort
does not cancel or prove rejection of an admitted server job. Uncertain sends
keep this page locked when status still shows the preceding job or no job;
reconciliation uses GET only, never automatic mutation replay. Current-job status
must have a recognized state and bounded safe ID. A different current job is not
proof of request identity across tabs. Pending-send uncertainty is page-local,
not durable across reloads; close the original service if it cannot be resolved.
The source, inference and server-process cleanup gates remain unchanged.

An explicit reading-note download exports only an allowlisted completed answer,
recorded citation identities, original scope and cached display-source excerpts.
It uses existing authenticated, version-bound source GETs; the only added HTTP
route serves a static formatter script. It adds no model request, arbitrary path
read, source execution, upload, clipboard operation or automatic persistence.
All variable Markdown content is enclosed in adaptive plain-text fences; no source/
model text can create active HTML, images or remote links in the generated note.
The output is bounded to 256 KiB as a whole. Refresh, history switches/eviction or
material answer changes invalidate an in-flight download without an automatic retry.

The downloaded note contains private questions and source. It is not signed or
independently verified, and no live-disk reverification occurs. Display escaping and
normalized line endings mean its excerpts are not the original raw bytes. Recorded
file hashes identify original cited files; artifact hashes identify retained model
reads, possibly larger than the cited excerpt. Neither is an export-text hash;
uncited selected file hashes are explicitly unavailable. Historical resident requests
remain cleanup-pending checkpoints, not current model state or final receipts.

The desk's Qwen3.5 reader, also available as `explain --reader qwen35 --focus ...`,
makes one synthesis-only call for a structured text/citation envelope and displays
decoded prose with range-checked references.
It does not use the Gemma action/quote protocol below. References prove source
locations, not support for every sentence or semantic truth. Both readers use
the same source/evidence postchecks and model cleanup gate. Inline previews use
the same snapshot version; source changes require refreshing before asking.

The desk can display up to 12,000 characters of transient, explicitly unverified
answer text during generation. Thinking deltas are discarded, and preview text
is not parsed into citation buttons or accepted claims. Cancellation and final
results clear the streaming preview. A coherent `stalled` result with proven
cleanup and intact source/snapshot may retain its existing first 12,000
characters as a separate RAM-only, unaccepted excerpt. Unsafe or blank text is
discarded. This excerpt has no formatted or clickable references, is not added
to the accepted answer, and is cleared by a new job or refresh. Cancellation,
uncertain cleanup and integrity failures do not retain it. The final explanation
still passes the existing gate. The SSE transport bounds lines/events/body/output and checks an elapsed
budget between reads; a final blocked read may additionally take its socket
timeout. This is not a hard 180-second wall-clock deadline.

Qwen selected CLI/desk reading now separates Markdown text from explicit citation
coordinates using the existing schema-constrained JSON response. The same engine
option remains experimental for Gemma12B; its public path and the engine's legacy
prose default are unchanged. The SSE transport
returns the original ordinary-content bytes, including JSON syntax; it still
rejects tool-call deltas and discards thinking deltas. A bounded preview adapter
extracts only the canonical answer's text field for transient, unverified display.
It does not repair JSON, supply citations, or validate the complete answer; a later
invalid chunk can stop further preview but cannot retract an already shown prefix.
Only a normally finished, strictly parsed complete response can enter the existing
source/range/integrity gates. Schema compliance does not prove semantic correctness.
Only exact final citation-array members can authorize inline source buttons; model
mentions cannot create missing citations, subsets or expanded ranges. Final prose
is visibly marked as content-unverified. Structured failures cannot use the legacy
complete `unverified_prose` fallback; a RAM-only decoded rejection excerpt cannot
grant a continuation scope. Genuine insufficient-source recovery remains gated
independently from streamed text.
The optional request reasoning budget is per thinking block in the pinned runtime,
not a total reasoning-token or wall-clock guarantee; total output/transport caps remain.

`explain` accepts one required question of at most 2,000 characters. Ordinary
line breaks and tabs are preserved, including indentation in pasted examples;
blank-only text, invalid UTF-8 and unsafe control characters remain rejected.
An optional `--focus path:start-end` selects up to three explicit source ranges,
at most 80 lines each. The host materializes them through the same read/evidence
validators before a single synthesis-only inference; incomplete or over-budget
selections are not silently clipped. Focus selectors are bound into ingress and
trace, and do not authorize source execution or writes.
Its strict action union contains only bounded file listing, line-range text reads,
literal text search, terminal answer, and terminal insufficient-evidence actions.
The model receives no write/effect, test, import, process, shell, Git, endpoint,
model-selection, or follow-up/session action. A defense-in-depth workspace policy
also has writes disabled. Repository code is never imported, tested, or executed.

The explicit same-file name-source navigator inspects retained, hash-checked UTF-8
bytes with AST and the standard-library symbol-table frontend, without generating
target bytecode or importing/executing source. Scope classification and possible
declarations are navigation aids, not runtime-value or reaching-definition proofs.
Unknown/annotation scopes do not fall back to unrelated same-spelling declarations.
Manual navigation preserves the question and source version; expansion requires an
explicit action and unchanged range budgets. Automatic project supplements consider
only module-bound names without uncertainty, counting competing declarations before
selection. Source256KiB/AST50,000 nodes/80 selected lines/64 names/64 groups/64
candidates/64KiB metadata limits fail without a partial list. This does not resolve
attributes, imported implementations, wildcard or dynamic bindings.

The explicit **keyword-definition search** uses only the already admitted desk
outline and cached display lines. It performs no new target parse, file read,
import, execution, subprocess, model request or persistent indexing. All 1–8
case-folded lexical terms must match a function's qualified name, path or own
text; comments/strings count, and nested definitions/classes exclude their body
words from the enclosing function. Matching transformed display text is a
navigation aid, not byte-exact substring search, behavior or runtime resolution.
The existing authenticated, version-bound definitions GET accepts an explicit
mode; legacy exact mode is unchanged. Query128 characters, 40 result rows and
64KiB whole-response limits do not enlarge source or inference budgets.
UI validates the whole response and guards asynchronous navigation against changed
project, query, selection, question and trial state. A cross-file click uses the
existing retained-source GET, not a search result's preview as executable source
or model context. It never submits a question or adds a manual selection.

Explicit **same-spelling call search** parses retained raw Python bytes on demand,
without changing the outline or inference catalogue. Captured source members are
fully hash/path-checked before inspection and rechecked before publication; the
project object must still be current, even after an unchanged-content refresh.
Reading/parsing/rechecking happens outside the main desk lock, with one nonqueued
scan slot. Existing source admission, 50K AST nodes/file, 500K nodes/query, 40 rows
and 64 KiB response limits apply. The three-second scan budget is cooperative:
active reads/parses and final identity checks may extend it. Skipped or unfinished
files contribute no matches and are counted explicitly. This is ordinary drift
detection, not hostile filesystem race protection or an atomic disk observation.

Each call keeps original whole-file SHA and physical UTF-8 byte coordinates;
attribute terminal matches do not resolve receivers, aliases or actual bindings.
Source navigation uses retained display GETs, not previews as executable text.
Search/navigation do not import, execute, submit a model request, change automatic
context, overwrite trial drafts/A or authorize a guest. Literal input carryover
and execution remain distinct explicit actions with their existing refusals.

An explicit **import-source** request recomputes that C1 selection and accepts only
one of its exact import declarations. The caller cannot supply an import target
path or module name. A bounded, per-request AST cache follows physical project
root/src paths, relative imports and explicit reexports using admitted retained
bytes; it never calls host import machinery, consults installed packages, evaluates
`__all__` or imports/executes project source. Comparison paths stay on their original
HEAD/current side. Every visited file is hash-checked before inspection and again
before returning routes. This detects ordinary retained-byte drift, not hostile
concurrent pathname races or live-source changes outside the retained snapshot.

The route steps are source candidates, not actual import precedence, successful
initialization, runtime values or complete dependency proof. Conditional imports,
overloads, package/module collisions, dynamic hooks and unresolved routes cannot be
promoted to a unique implementation. Module steps open an admitted file without
inventing line coordinates; declaration steps use physical source coordinates.
Browsing preserves the question, selected ranges and answer history; it does not
automatically add source to a model request or authorize any execution.

Only complete retained reads can become citable evidence. One read is limited to
80 lines. A requested range wholly contained by an earlier retained span of the
same canonical path, whole-file SHA-256, and snapshot inventory SHA-256 reuses the
earliest covering evidence ID rather than occupying the citable pool twice;
partial overlaps that add lines remain distinct evidence. An exact empty-file read
is navigation-only, while inconsistent inverted coordinates fail closed as
snapshot drift. After a retained or reused read, an advisory same-file next-unread
action is included only when later lines exist and as a complete schema-valid inner
JSON action object when it fits the fixed context; otherwise it is omitted whole.
At EOF the host suggests search or another admitted file without selecting a
cross-file action, and it never executes guidance automatically. A third identical
action is stopped even if other navigation is interleaved. For `source_quote`, the
model supplies a retained range citation (at most 80 lines); Forge8 extracts at
most 600 source characters from validated evidence instead of trusting
model-copied text. Line endings are normalized to LF; line characters are retained.
Human source displays use a gutter so source text cannot mimic report headings.
Every quote or inference citation resolves to an observed evidence ID, canonical
path and line subrange, whole-file
hash/size, snapshot inventory hash, and content-addressed excerpt. An accepted
answer must contain both a source quote and a separately labeled inference. These
checks establish source provenance, not the truth of an inference, behavioral
correctness, or whole-project understanding. Coverage reports admitted, observed,
cited, and unread files/line ranges; a stalled `ANSWER.txt` renders at most five
inert unread ranges with exact shown/total counts and, when more are omitted,
points to the complete machine-readable coverage. Those ranges may include
admitted paths unavailable to model navigation, so the display is diagnostic
rather than an executable action list; it cannot restore claims or promote a
stalled bundle.

A narrower no-progress transition applies only to the second canonical
`read_text` request whose entire requested range is already retained as citable
evidence. Forge8 accepts but does not dispatch that duplicate, records an
`explain.terminalization_requested` transition, removes recent-navigation content,
and spends at most the next ordinary inference slot under a static grammar that
contains only the existing `answer` and `insufficient` variants. The host still
rejects a backend-supplied navigation action, malformed output, or ungrounded
claims; there is no correction retry or rationale promotion on that turn. At a
call/action ceiling the read follows the ordinary dispatcher so the accepted
action never vanishes from the trace. Detailed terminal parser/grounding rejection
text remains in the private sealed trace, while user-visible failure fields use a
fixed diagnostic and cannot copy a model-controlled parser key into `ANSWER.txt`
or `explanation.json`.

### Fixed deterministic checks and Windows descendants

The production `fix` path exposes only the registered `python_unittest` and
`python_pytest` checks. Their executable, arguments, discovery root, working
directory, timeout, and output budget are trusted registry data. The model
selects only an ID. Launch uses `shell=False`, closed stdin, bounded output, and
a small reconstructed environment instead of the full parent environment.

`python_unittest` starts with `-I -S -X utf8`, imports the stdlib runner before
adding the workspace to `sys.path`, and ignores the explicit workspace
`PYTHONPATH` during bootstrap, so ambient site packages and workspace runner
shadows cannot enter first. `python_pytest` must see the pinned Forge8 optional
runtime, so it instead starts with Python `-I` and no workspace `PYTHONPATH`. In
that isolated child it imports and attests pytest 9.1.1 before adding the
workspace. It then calls `pytest.main` with an OS-null configuration source, fixed
`--rootdir=.`, fixed `--confcutdir=.`, and the explicit `tests/` target.
Repository pytest configuration is therefore ignored, parent `PYTEST_ADDOPTS` is
absent, and automatic third-party plugin loading and the cache provider are
disabled. Repository root `conftest.py` may still load but cannot be an allowed
write path for `python_pytest`; `tests/` is already non-writable and
`--confcutdir=.` cuts off parent `conftest.py` files.

The check executable is the Python interpreter that owns the invoked Forge8
installation. For `python_pytest`, application packages deliberately
preprovisioned in that same trusted environment can therefore import after the
runner is attested; an excluded target `.venv` is not added to a standalone
Forge8 check's import path. Forge8 neither resolves nor installs those packages,
and it does not fingerprint them as repository input. They join the trusted
`process_only` test boundary and can access host resources or emit their contents
into captured evidence and model-visible check output. `python_unittest` keeps
`-S`, so it does not import venv site packages even when Forge8 is installed
beside them.

These controls constrain candidate-writable selection inputs and the evidence
visible to Forge8's hooks. Repository tests, `conftest.py`, and project plugins
remain trusted `process_only` Python with the invoking user's privileges; they
can affect what the hooks see. This is neither a proof of suite completeness nor
a sandbox.

On Windows, Forge8 pre-creates a `KILL_ON_JOB_CLOSE` Job Object, launches the check root suspended, assigns it, then
resumes it. Assignment or resume failure permits no repository code to run. On timeout and normal root exit, Forge8
terminates/closes the Job before finalizing output. Setup or cleanup failure cannot yield a passing check.

This is descendant lifecycle control, not a filesystem, token, network, resource, or privilege sandbox. Forced Forge8
death in the narrow post-`Popen`, pre-assignment window can leave a suspended root that has not executed repository code.

### Pinned local inference assets

Before inference, the production profile binds both manifests, executable, model, loopback address, fixed 8K profile,
and a strict flag allowlist. WSL-to-Windows child launch is refused; use native Windows Python for that runtime.

Runtime manifest schema v2 pins release archives and the exact installed inventory by relative path, size, and SHA-256.
Extra, missing, modified, linked/reparse, hard-linked, or special entries fail preflight. The model target is also checked
against pinned size and SHA-256. Pins establish byte identity, not that the code or model is benign.

For regular files of at least 64 MiB at conventional `/mnt/<drive>/` paths on WSL,
the default 4 MiB hasher uses at most four ordered positional reads. It still hashes
every byte and compares the complete digest; it is not a metadata cache. Short
reads are filled, premature EOF/growth or changed descriptor metadata are refused,
and pending workers finish before the file closes. Cancellation is cooperative:
an in-progress OS read must return. Native Windows, other paths and small files
retain the serial reader. This performance routing grants no filesystem trust and
does not establish an atomic snapshot or prevent hostile concurrent pathname races.

The fixed server profile includes llama.cpp offline mode and binds to loopback.
Inference and health HTTP requests use a private opener that disables proxy
inheritance and rejects redirects; they do not install a global opener or alter
the user's network settings. This matters because Python's default URL opener
can inherit environment or system proxies.
[Python URL opener behavior](https://docs.python.org/3/library/urllib.request.html#urllib.request.ProxyHandler).
None of these settings is an OS egress control for project tests or a compromised
native model server.

## 2. Detected and fail-closed afterward

### `fix` baseline, disposable checks, and clean replay

Inference starts only after the selected checks produce deterministic results and
at least one genuine failing baseline. Already-passing, unavailable, timed-out,
zero-test, pytest bootstrap/version/import failure, nonreserved raw status, or
other runner-observed infrastructure results do not enter
model repair. The isolated pytest bootstrap prevents workspace `sitecustomize`
or dependency-name shadows from running before the runner is attested; it does
not make the later trusted test execution safe.

For `python_unittest`, the isolated child counts the discovered suite and reads
the returned stdlib `TestResult` directly. The discovered suite must be non-empty;
a complete all-pass run maps to reserved status 70, while ordinary recorded
assertion, fixture, discovery, or test errors map to 71 even when a pre-test
fixture prevents every test body from starting. Raw 0/1, clean incomplete or
nonexecution, skipped, expected-failure, unexpected-success, and runner-observed
early-exit results are unavailable. Stdout/stderr are diagnostics, not outcome
evidence, so forged summary text cannot promote a run; timeout and launch states
remain infrastructure failures.

For `python_pytest`, a Forge8 evidence plugin requires one stable non-empty
collected-test inventory through session finish and report history for every
visible item. Only a genuine all-pass session maps to reserved status 80; at
least one ordinary failure/error with otherwise genuine passes maps to 81.
Skipped, xfailed/xpassed, deselected, collection-failed, zero-test, and
runner-observed early/session-exit results are unavailable. Raw startup 0/1,
bootstrap 90, unexpected return codes, and pytest infrastructure statuses cannot
masquerade as pass/fail.

Each check runs on a fresh sanitized candidate copy. Forge8 rescans it and the candidate afterward; persistent check
mutation, candidate mutation, and excluded paths invalidate the result. Regressions roll back.

Final verification captures an independent candidate inventory, requires a non-empty allowlisted diff, and replays the
exact changed bytes from immutable ingress using original preimage hashes. Replay must equal the captured candidate; the
same checks run on fresh replay copies. Included-file inventories for live source, immutable source, replay, and candidate
are checked again before acceptance.

These comparisons detect inventoried-byte changes; they do not prevent or revert writes to other host locations.

### `explain` grounding, postscan, and failure bundles

The one-shot explanation runner requires server cleanup before it can promote
an answer. It then rechecks the original included source inventory, sanitized
snapshot, ingress bytes, every retained read excerpt, the content-addressed
object inventory, and bound server-log states. Navigation-only, clipped, evicted,
unknown, or drifted observations cannot support a claim. Any source, snapshot,
ingress, evidence, artifact, or cleanup mismatch clears all accepted claims.

A completed runner writes canonical `explanation.json`, inert `ANSWER.txt`, a
hash-chained trace/seal, and `manifest.json` last. Success is `status=answered`
and `manifest.ok=true`, but `semantic_claims_verified` remains false. Any other
completed runner status records `answer=null` and `manifest.ok=false`; it is a
diagnostic failure bundle, not a partial answer. Configuration, ingress, asset,
or server-start rejection can occur before that complete bundle exists.

These rescans detect changes to admitted bytes and retained artifacts. They do
not establish semantic truth, authenticate the whole bundle, prevent a hostile
concurrent pathname race, scrub secrets from storage, or prove that unread code
was understood.

### Server shutdown and per-run secret cleanup

The reading desk now has a distinct **resident-request** mode (default idle timeout
600 seconds; `read --idle-timeout 0` retains one-shot behavior). It does not relax
the one-shot gates below. A completed resident request uses a different `.request`
result/artifact kind and a seven-field `scope=resident_request` checkpoint: the
response and cancellation watcher finished, the transport key reference cleared,
and the owned living process reported its single authenticated slot idle. This
does **not** prove process exit or clearing the supervisor's still-needed key.
Every original source/snapshot/ingress/citation/object/trace check still applies.
Its immutable manifest has `ok=false`, separate `request_ok`, session identity,
and `session_finalization=pending`; it cannot impersonate a one-shot manifest.

Live server logs reside outside question bundles. Explicit release, idle timeout,
cancel/uncertain transport, or normal desk close stops the owned generation. A
separate immutable `model-sessions/<id>/closed.json` binds start/registered request
hashes, final logs, actual shutdown and cleared keys, and fully rechecks assets
against both the initial anchor bytes and original verified inventory. Later
failure never rewrites earlier sealed requests; the UI displays final receipt
failures separately from live process state. Unknown process/key cleanup blocks
new model requests. Final validation failures remain latched for the desk's exit
status even if the bounded five-receipt UI history evicts the failed entry.

Status polling cannot load a model or renew idle time. Release requires an exact
session ID and refuses busy/stale generations. Cancellation and final publication
are serialized: a cancellation already accepted at publication discards the answer
and reclaims that owned session. A later cancel cannot undo an already publishing
result. Browser close does not close the desk; Ctrl+C/normal desk close does.
This is not a parent-death guarantee for SIGKILL/host crash or secure memory erasure.

Both `fix` and `explain` create a random per-run llama.cpp API key only after
their model-free admission/preparation succeeds, hold it in supervisor and
transport, pass it through `LLAMA_API_KEY`, and send local Authorization headers.
Forge8 does not intentionally serialize the key or include it in the evidence
bundle. The owned server could still echo it into private raw logs;
machine-readable results redact the exact value, but operators must treat the run
directory as sensitive.

Supervisor results redact the exact key from health errors, log tails, startup/shutdown errors, and process-API failures.
Transport request/stream failures and malformed-response errors do likewise. After issuance, unexpected outer CLI
exceptions are reported generically without their text.

For promotion, the one-shot acceptance gate must prove a reclaimed server status
(`already_exited`, `terminated`, or `killed`), observe a return code, and observe
both supervisor and transport key references cleared. `fix` then reruns the exact
checks and authoritative verifier. `explain` binds that lifecycle and server-log
evidence into its runner before the source/evidence/artifact postchecks and
terminal bundle. Any shutdown, cleanup, gate, or later verification failure
produces a non-promotable result.

Clearing those two in-memory references is not secure erasure and makes no claim
that the key, source, prompt, response, or other sensitive bytes were scrubbed
from process memory, logs, swap, page files, or disk.

Redaction is exact-value defense in depth, not a general secret detector. A
malicious or compromised successful inference server could reflect data in a
normal response that later appears in the trace. The pinned server is therefore
part of the trust assumptions.

### `fix` delivery and shared trace integrity

Before creating an accepted delivery directory, packaging captures source and
candidate bytes once and requires exact agreement among:

- ingress source fingerprints;
- the verifier's complete candidate fingerprints;
- the verifier's added/modified/removed paths; and
- the patch and manifest generated from those captured bytes.

The delivery root must be previously absent. It contains a deterministic patch,
verification result, manifest, and handoff. Failed runs may still produce a
diagnostic bundle, but it is marked `NOT VERIFIED` and is not promotable.

Trace events are canonical JSONL with sequence numbers, previous-event hashes,
and per-event SHA-256. A terminal seal binds the event count, chain head, and
exact trace bytes; Forge8 verifies that seal before returning the outcome.

The trace seal is **not a digital signature, timestamp authority, or external
anchor**. Anyone who can rewrite the entire run directory can construct a new
internally consistent trace and seal. It detects accidental or partial changes
when checked against the retained seal; it does not establish authorship.

## 3. Not enforced

Current `fix` repository checks and verifier processes are `process_only`:

- There is no OS-enforced filesystem sandbox. Selected tests are trusted code
  running with the invoking user's privileges. They can read or write any host
  path that user can access, not just the disposable working directory.
- There is no OS-enforced network deny. Tests and child processes can open DNS,
  LAN, internet, loopback, IPC, or inherited-user services if the host permits it.
- A cleaned environment and fixed argv reduce accidental authority but do not
  contain Python code. Do not run repositories whose tests you do not trust.
- Trusted same-process test code can deliberately tamper with runner evidence or
  terminate with Forge8's reserved 70/71/80/81 status. The parent process cannot
  distinguish that deliberate act from its bootstrap's status; this is part of
  the `process_only` trusted-test boundary, not a hostile-code guarantee.
- Stable link/reparse and ordinary file-change races are checked, but directory
  walks are not anchored to immutable directory handles. A hostile same-user
  process can race path components between `lstat`/`resolve`, open, rescan, replay,
  or package operations. This TOCTOU boundary is not defended.
- Content below excluded trees is not inventoried. Mutations inside `.git`,
  `.forge8`, caches/build trees, or other excluded/hidden control locations may
  evade live-source fingerprint comparisons. Side effects outside the repository
  are likewise invisible to those comparisons.
- Forge8 does not provision or verify owner-only ACLs for run directories,
  bundles, logs, models, or runtimes. On Windows they inherit parent-directory
  ACLs. POSIX mode requests do not create a cross-platform confidentiality claim.
- Process lifecycle controls do not impose CPU, RAM, disk, GPU, handle, or total
  host-availability quotas. Time, action, context, and captured-output budgets
  bound the operator, not every possible project-code side effect.
- Runtime/model hashes prove selected bytes, not supply-chain provenance,
  vulnerability absence, semantic safety, or reproducible build origin.
- Verification proves only the selected checks' contracts on the captured bytes. It
  does not prove the natural-language goal, complete correctness, absence of
  malicious behavior, or suitability for production.

`explain` does not launch repository code, so the trusted-test execution authority
above does not apply to that command. It still reads admitted source into local
model prompts and private evidence, launches the pinned local server with the
invoking user's authority, inherits run-directory ACLs, and shares the documented
pathname/TOCTOU, runtime/model trust, resource, and evidence-authentication limits.
Read-only at the Forge8 tool boundary is not an OS sandbox or a semantic guarantee.

## Trust assumptions

Use either product path only when all of the following are acceptable:

- the host OS, Python interpreter, Forge8 code/configuration, GPU driver, and
  filesystem are trusted;
- the invoking account and parent-directory permissions are appropriate for the
  repository and run evidence;
- for `fix`, the selected repository's tests are trusted to execute with that
  account's host filesystem and network authority;
- admitted repository source, prompts, responses, and logs may be retained in a
  private local run directory and processed by the pinned local model;
- no hostile local process is racing repository, staging, runtime, or delivery
  pathnames;
- the pinned llama.cpp runtime and model are trusted not to exfiltrate or reflect
  the per-run secret; and
- the operator reviews the patch and evidence before any manual application.

Forge8 is not a malware-analysis sandbox, multi-tenant boundary, compliance
control, or defense against an administrator, compromised kernel, or malware
already running as the same user.

## Terminal success and failure

A `fix` delivery can say `VERIFIED` only when the terminal operator status is
`verified`, the authoritative verifier passes, the exact fixed checks pass on
clean guarded replay, the delta is non-empty and allowlisted, source/candidate
fingerprints agree, existing modified Python files pass the bounded source-
preservation checks, the server/secret acceptance gate passes, and the trace seals
and verifies. These checks prevent three observed regressions; they do not prove
general merge quality. A passing check or verifier alone cannot override another
terminal failure.

Ingress rejection, invalid or already-passing baseline, check infrastructure
failure, parse/action/backend/stall exhaustion, empty or disallowed changes,
mutation or fingerprint mismatch, replay/check failure, server reclamation or
secret-cleanup uncertainty, acceptance-gate failure, and incomplete packaging all
remain non-promotable. If a bundle says `NOT VERIFIED`, do not apply its patch;
use it only for diagnosis. `VERIFIED` still requires human review.

An ordinary one-shot explanation result is mechanically successful only when the dedicated runner
returns `answered`, the reader's required citations resolve to retained evidence
(plus the quote/inference contract for Gemma), all postscans and cleanup pass,
and `manifest.ok=true`. This does not
verify the inference: citations prove provenance and
`semantic_claims_verified=false` remains explicit. Insufficient evidence,
budget/stall/backend/tool failure, interruption, drift, or cleanup uncertainty
cannot retain accepted claims; a completed runner bundle has `answer=null` and
`manifest.ok=false`. Human review remains required for every inference.

A clean `insufficient_evidence` result may retain its original reading scope for
explicit source-only continuation or manual selection recovery. Both outer and
engine status/false booleans must agree, the answer must be explicitly null, and
the ordinary reason must be bounded to 1,000 safe characters. Existing source,
snapshot, acceptance, request-completion and final cancellation/error gates still
apply. This does not verify that the named context is missing or interpret the
reason as executable instructions, paths or citations. Project scope remains the
actual retained selection, never reconstructed from the model's reason.

Manual recovery uses existing authenticated retained-source GETs and exact path/
version/line checks. It replaces all original windows only after explicit action,
within the manual three-range limit, without auto-asking or widening context.
The browser's conservative display-source budget check is not an authorization
boundary; the normal server preflight remains authoritative for the next question.
Source/draft/history/job changes invalidate pending recovery. No new endpoint,
source execution, persisted browser data or comparison continuation is introduced.

## Optional single-file guest line reports

An explicitly authorized ordinary WASI call may opt into `trace_lines: true`
(CLI `--trace-lines`). The option is an exact boolean, default-off; tracing with
extra modules or paired versions is not supported. It is bound to the source,
original raw input and request identity. Whole-module initialization still executes
in the guest, but only the subsequent explicit call is traced. The host never
executes the target Python source to obtain these events.

The guest keeps at most 1,000 ordered line numbers, filtering original compiled
code-object identities, including nested same-file code. It stops collecting before
exception formatting/serialization. It does not collect locals, frames or values,
and excludes foreign/generated code even with a matching filename. This is a
best-effort guest mechanism: target code can interfere with the hook or protocol.
`hook_intact` checks call-end hook identity, not continuity; a line event does not
prove the line completed. Neither a non-truncated report nor a matching hash makes
this complete, tamper-proof, a coverage result or validation of an AI explanation.

The separate trace marker has a strict shape, exact flags, physical source-line
bounds, a 1,000-event cap and a required valid ordinary result envelope. Duplicate,
misordered or malformed trace records are unavailable without altering the ordinary
result parser. Publication additionally requires normal worker completion, unchanged
source/input/runtime and intact bounded output capture; cancellation or uncertain
cleanup cannot leave a usable trace. Existing WASI capabilities and caps are not
widened; instrumentation may consume more of the same time budget.

The desk displays the exact submitted input separately from editable drafts. Its
explicit retained-source GET checks identity and stale navigation state; it does
not execute, ask a model, replace reading selections or persist new browser state.
Guest reports remain distinct from host `observe` captures and are never promoted
to trusted observations or automatically fed back as verified prose.
[Workflow and actual evidence](isolated-experiments.md#inspect-guest-reported-call-line-visits).

## Optional paired HEAD/current guest trials

Reading and model answers do not authorize execution. A desk explicitly opened with
`--allow-experiments` may additionally accept `head_current` trial mode in a retained
HEAD/current comparison. The user selects an after-side synchronous top-level Python
function, confirms both complete modules and source hashes, and explicitly authorizes
one initialization plus one call **per version**. The host only parses source; two
sequential, separate CPython/WASI guests execute it. The same raw JSON input is supplied
to both. This initial mode has no project module-set imports, function extraction,
dependency installation, automatic input search or execution of the fixed caller.

The existing WASI runtime verification, host process limits, guest memory/output/time
bounds and cancellation remain unchanged; see [isolated trials](isolated-experiments.md).
One existing desk worker slot owns both calls. Source/HEAD/capsule guards run before,
between and after the calls without holding the cancellation lock. A failure of the
first call's process/cleanup/integrity prevents the second; an ordinary guest-reported
exception is a completed observation, not a process failure. Accepted cancellation
prevents a completed comparison. Uncertain process cleanup blocks new work.

A private paired receipt binds the captured version/HEAD/source hashes, raw input hash,
runtime manifest identity, guard observations and both unchanged child report hashes.
It does not rewrite source-reading provenance or claim that source reading executed code.
Both child reports and their inputs remain local. A failed final guard, changed identity,
incomplete execution, missing result or unsupported JSON yields **unavailable**, never
an inferred agreement. These are bounded observations, not hostile-mutation locks.

`canonical-json-v1` compares guest-reported JSON envelopes on the host: object key order
is ignored, list order retained, bool/int/float and exception phase/type/message remain
distinct. Floats retain the JSON decoder's binary precision, not arbitrary decimal
precision. Comparison admits at most 64 nested containers and 393,216 canonical ASCII
characters per envelope. The browser receives result text, not numeric objects.
Guest code can interfere with its own reporting protocol. Agreement is not equivalence,
purity or validation of an AI explanation; a difference need not be caused by the edit
(for example, time/randomness can differ). This is deliberately **not a trusted oracle**.

## One explicitly pinned ordinary trial

An enabled ordinary single-file desk trial can retain one completed report as A,
then compare it with the next explicitly executed B. Pin, replace and clear only
change RAM state; they do not execute source, run a model or widen WASI capabilities.
Capture binds the exact submitted input, retained complete source, original report
bytes and unchanged full runtime-manifest bytes. The ordinary runner's checks and
guest-result parsing must agree; capture failure leaves the original trial display
intact but unavailable for pinning. A public record is bounded to 128 KiB, its
immutable internal capture to 256 KiB. Polling does not reread these files.

The typed comparison requires matching snapshot, file, entry, source, runtime and
trace policy; only inputs may differ. An exception is an observation, not a passing
test. Cancellation, incomplete cleanup or missing capture cannot produce a verdict.
This is historical guest-reported data, not current disk attestation, causal proof,
equivalence or validation of an explanation. No result is fed back into inference.

Authenticated pin/clear mutations require an exact job ID, source version and
comparison revision. Successful refresh increments the revision and clears A even
when the content hash is unchanged. Lost replies use GET-only reconciliation, never
automatic mutation replay; the 10-second client exchange budget is not a guest
execution deadline. Reload can retrieve current RAM state, not unsent drafts or a
durable transaction identity. Service restart loses A. There is no history database,
multi-tab exactly-once promise or new execution scheduler.

## Run-directory cautions

Run directories are private working records. They can contain source snippets,
full patches, prompts, model output, test output, paths, logs, and failure details.
Keep `.forge8/runs` out of version control and do not upload or share a bundle
without reviewing every file. Put the Forge8 asset/run root under a directory
whose Windows ACL or POSIX permissions already match the desired audience.

## Reporting a security issue

When reporting a problem, include the platform, Python and Forge8 versions,
command shape with secrets removed, terminal status, and the smallest safe trace
or bundle excerpt that reproduces it. Never publish a private run directory
unchanged.

## Appendix: future sealed boundary

A future “sealed” profile would require independently verified OS filesystem and
network isolation for test processes, handle-anchored path traversal and
publication, explicit Windows ACL creation/verification, stronger resource
limits, and an external signature or trusted anchor for evidence. None of those
future controls is implied by the current CLI or by a `VERIFIED` bundle.
