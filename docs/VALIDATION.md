# What has been checked

Development preview, September 2026. Hardware observations use one ASUS FA507NV
with an RTX 4060 Laptop (8,188 MiB VRAM) and approximately 32 GB RAM. Hosted CI
results are listed separately. Neither predicts model performance on another machine.

## Application paths

- GitHub-hosted Windows and Ubuntu each passed the 1,430-test Python suite and
  all seven Node UI harnesses. The Python runs took 148.0 s on Windows (13 skips)
  and 41.7 s on Ubuntu (12 skips). Windows setup fixtures also passed.
  See [the CI run](https://github.com/alvin0603/Forge8/actions/runs/34796570444).
- During publication preparation, one wheel was installed and exercised with a
  real browser against both native services, including an asset-free setup and
  narrow/desktop layouts.
- Windows setup passed refusal/consent fixtures, extracted all 55 pinned runtime
  files from existing ZIPs, and reused verified model assets. These checks did
  not test a fresh internet download or a second machine's GPU setup.
- Same-spelling call search located 16 manually reviewed spans in Requests and
  cachetools on both platforms. Four observed browser searches per platform
  completed in 0.047–0.225 s on Windows and 0.041–0.115 s on WSL. This measures
  static navigation, not AI understanding or a latency percentile.
- Explicit WASI examples covered literal-input carryover, pinning A and trying
  B, and a selected multi-module CSV example. In the latest three-run journey
  per platform, guest completion took 2.07–2.09 s on Windows and 5.11–5.13 s on
  WSL. Source inspection did not execute the project on the host.

The examples and tests are included. Raw development records are intentionally
not published because they may contain local source, paths or credentials.
The observations above are maintainer-reported, not independently certified.
CI checks application logic, not GPU compatibility or model-answer quality.

## Source-guided input search

The September 14 check compared source-guided input selection with the original
nearby rules using real, fresh HEAD/current WASI guests. Three deliberately simple
cases shared one complete owned module; each seed produced matching returns.
Old nearby inputs exhausted without finding a difference in all three cases.
The source-guided planner found each difference on both native hosts:

| Deliberate edit | Distinguishing argument | Input pairs | Windows | WSL |
| --- | --- | ---: | ---: | ---: |
| `count >= 100` becomes `count > 100` | `100` | 2 | 4.8 s | 28.6 s |
| Remove `"audit"` from a mode-membership condition | `"audit"` | 3 | 7.1 s | 42.5 s |
| `value > 9007199254740993` becomes `>=` | `9007199254740993` | 2 | 6.7 s | 29.4 s |

The respective reported returns were `0` / `5`, `"checked"` / `"unchecked"`, and
`true` / `false`. Integer inputs stayed exact above JavaScript's safe-integer range.
Old nearby searches took 7.2–11.8 s on Windows and 39.6–67.4 s on WSL without
exposing these changes. Times include checks and every guest call. They are single
observations, not percentiles or a general speedup/accuracy benchmark.

Three earlier development cases also retained their results: swapped clamp
bounds, first-wins versus last-wins duplicate records, and removal of CPython
dedent's whitespace normalization. An unchanged control exhausted its three
inputs without a difference. Each native host completed 58 guests across all ten
searches and cancelled one live worker without starting its partner. Source,
runtime and report checks passed; all started workers were reclaimed. No model
was called, and target source never executed on the host.

Final Edge journeys against both native services checked source-hint previews,
separate execution consent, exact original/found inputs, question preservation,
GET-only reload and 1440/390-pixel layouts. Each used four additional guests.
These checked a nonempty question with an empty answer, not populated AI history.
Mocked UI regressions cover preservation of selections/history and failed transport.

The final local suite passed 1,414 Python tests on each native host: WSL 97.0 s
(12 skips), Windows Python 3.10 200.0 s (35 skips). All seven Node harnesses passed
on both; Windows setup passed 21 assertions without downloads or native launches.
Skipped tests are not successful executions. CI results are separate.

This improves a specific blind spot without increasing the 12-input cap or adding
dependencies. It uses conventional constant seeding, not a new differential-testing
algorithm. These are consumed development examples, not upstream regressions,
held-out bug-finding rates or evidence that model explanations improved.

## Bounded generator trials

The September 14 native checks completed 21 fixed calls on each platform, using
small deliberate fixtures and unchanged CPython 3.14.7 `heapq.py`. For
`merge([1, 3], [2, 4])`, two attempts reported `[1, 2]` without claiming exhaustion;
six attempts reported `[1, 2, 3, 4]`, exhaustion on attempt five and a null return.
With inputs `[1, "x"]` and `[2, 4]`, the guest reported `1` before a `TypeError`
on attempt two. The ordinary, non-consuming call still reports that a generator
cannot be serialized; it never silently starts iteration.

Other cases checked detached mutable yields, empty generators, separate iteration
and close errors, unsupported JSON, duplicate normalized keys, retained-value
overflow, and a 30,000-element yield. Infinite loops in either `next()` or `close()`
were interrupted by the guest timer without publishing partial results. All
workers exited normally. One initial Windows proof stopped because its process
observer also counted Python's OS-version query; the corrected observer left
product code and expected results unchanged. The failed record was retained.

Real Edge journeys against both native services made three further `heapq.merge`
calls each. They checked retained A, separate submitted/draft modes, GET-only reload,
and 390/1440-pixel layouts. The same journeys selected all 122 unchanged lines of
Requests' `resolve_redirects` as 80+42 lines without executing it or asking a model.
A nonempty question, both selections and initially empty answer/history survived
the trials; populated history is covered by mocked regressions, not this journey.
After layout adjustments, both journeys passed again with the first yielded values
visible on completion and input focus preserved. A Windows viewport capture also
confirmed the result layout without changing scroll positions or rerunning on reload.

The local 1,430-test Python suite passed on each host (WSL: 12 skips; Windows
Python 3.10: 35 skips), along with all seven Node harnesses and 21 Windows setup
assertions. These checks establish bounded application behavior on the tested
inputs, not improved LLM reasoning, a trusted oracle or a general accuracy rate.

The included `batch_rows` walkthrough also completed three actual CLI trials on
each native host. Three attempts yielded `[[1, 2], [3]]` and detected exhaustion;
two yielded the same batches without claiming exhaustion. A zero batch size
reported `ValueError` on the first advance. Calls took 0.89–0.98 s on Windows
and 3.93–4.10 s on WSL, including verification. These are six observations,
not a latency benchmark.

## Watched local values

September 14 checks completed 17 actual WASI calls on each native host. Cases
covered caught exceptions, recursion, detached mutable values, null versus
unbound names, unsupported objects, size/event limits, refused entry replacements
and a real guest timer interruption. They also exercised unchanged CPython
`textwrap.dedent` and the included `sum_rows` example. All workers exited normally;
source, runtime, input and captured-output checks passed. Interrupted or
infrastructure-failed trials published no usable partial watch report.

For `sum_rows(["4", "bad", "6"])`, the line-12 snapshot showed `total=4`,
`rejected=0`, `row="bad"` before the handler increment. The final return was
`{"total":10,"rejected":1}`. This call took 0.97 s on Windows and 4.07 s on WSL,
including verification: two observations, not a latency benchmark.

Real Edge journeys against both native services made two further calls each.
They checked retained A, watched values beside highlighted source, separate
submitted/draft names, GET-only reload, and 390/1440-pixel layouts using explicit
step navigation and scrolling. Question, source selection and initially empty
history survived; populated history is covered by mocked regressions.

The complete local suite passed 1,452 Python tests per host: WSL 94.4 s (12 skips),
Windows Python 3.10 155.3 s (35 skips). All seven Node harnesses passed on each;
Windows setup passed 21 assertions without downloads or native launches.
This is bounded inspection of guest-reported values, not a new tracing algorithm
or evidence of improved model reasoning. No model was called in these journeys.

## Partially indexed projects

September 14 checks reproduced a project-wide discovery refusal caused by one
unparseable Python file. After the fix, healthy definitions remained selectable
and unavailable files were explicitly listed. A Python 3.12 type-alias fixture
also became usable on Windows Python 3.10. All previously successful catalogue
inputs remained byte-identical across 14 development cases on each platform.

Real Edge journeys against Windows and WSL each completed Locate and a project
question, with correct answers and source references for a small string helper.
They checked unindexed-file navigation, preserved drafts/selections, and
390/1440-pixel layouts. Each made three model requests without executing source.
One private checker incorrectly expected two request records; its failed record
was retained and all three were independently verified, without replaying answers.
Both model owners closed successfully. The local 1,460-test Python suite, seven
Node harnesses per platform, and 21 Windows setup assertions passed.
This removes an availability failure; it does not establish better model reasoning.

## Model quality and waiting time

The public desk reader is Qwen3.5-9B Q4_K_M with llama.cpp b10621, an 8K context
and q8_0 KV cache. Selected code uses a structured text/citation response.

This pipeline passes source identity and citation-range checks, but answer
quality is still a major limitation. A three-seed development screen produced
19 fully met, 4 partly met and 13 unmet rubric items out of 36. An alternative
model and a presentation experiment did not meet their adoption criteria.
Those failures were retained, not replaced with successful retries.

A later 18-call citation experiment kept all nine paired answer texts identical
and improved accepted delivery from 6/9 to 9/9. The variant allowed one reference
per source read, but sometimes chose narrower spans or omitted a useful reference
entirely. It was not adopted: valid references and fewer failures did not justify
making source verification harder. Model prose did not become more accurate.

A later 18-call context screen supplied five or six source ranges at 16K instead
of three at 8K. Across two development tasks and three seeds, fully met rubric
items increased from 9/36 to 18/36, but all six larger-context answers still
failed acceptance. A further nine-call plain-output variant fell to 13/36 and
left four task answers unfinished. Neither change shipped; these results do not
establish a general quality gain from larger context or a different output format.

One earlier four-question delivery check had only 2 fully correct answers on
each native platform. Windows cold completion took about 73 s; WSL took about
146 s. Warm completions took roughly 26–61 s. These are small development runs,
not P50/P95 estimates or a general accuracy score.

A separate initial change-reading answer omitted required references and added
an incorrect scope caveat. It did not pass answer acceptance.

An adopted WSL I/O change reduced one measured complete-byte preparation from
35.8 s to 16.5 s without skipping hashes. Full-answer latency has not been
remeasured under that change, so the preparation improvement is not advertised
as an end-to-end speedup.

The development cases are consumed examples, not a fresh human-validated
benchmark. There is no public scoring runner or independent quality result yet.

## What these checks do not establish

A citation is not proof that prose is true. Guest outputs are not trusted oracles.
A passed repair test is not proof that a patch is generally correct. Resident
model reuse avoids repeated loading, but does not make generation instantaneous.
No cloud parity, larger-model parity, broad autonomous repair success, or
independently reproduced model/runtime conversion is claimed.
