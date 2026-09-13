# What has been checked

Development preview, September 2026. Hardware observations use one ASUS FA507NV
with an RTX 4060 Laptop (8,188 MiB VRAM) and approximately 32 GB RAM. Hosted CI
results are listed separately. Neither predicts model performance on another machine.

## Application paths

- GitHub-hosted Windows and Ubuntu each passed the 1,374-test Python suite and
  all seven Node UI harnesses. The Python runs took 124.8 s on Windows (13 skips)
  and 41.0 s on Ubuntu (12 skips). Windows setup fixtures also passed.
  See [the CI run](https://github.com/alvin0603/forge8/actions/runs/34521454959).
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

## Model quality and waiting time

The public desk reader is Qwen3.5-9B Q4_K_M with llama.cpp b10621, an 8K context
and q8_0 KV cache. Selected code uses a structured text/citation response.

This pipeline passes source identity and citation-range checks, but answer
quality is still a major limitation. A three-seed development screen produced
19 fully met, 4 partly met and 13 unmet rubric items out of 36. An alternative
model and a presentation experiment did not meet their adoption criteria.
Those failures were retained, not replaced with successful retries.

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
