# What has been checked

Development preview, September 2026. These results describe the reference
ASUS FA507NV with an RTX 4060 Laptop (8,188 MiB VRAM) and approximately 32 GB RAM.
They are not independent benchmarks or predictions for another machine.

## Application paths

Publication preparation additionally checked the simplified desktop/narrow UI
against real Windows and WSL services, without submitting model or guest jobs.
The Windows setup script passed small refusal/consent fixtures, extracted all
55 pinned runtime files from the actual local ZIPs, and completed using existing
verified model assets. No fresh internet download or second-machine GPU setup
is claimed by these installer checks.

After public-file cleanup, fresh installations of the same wheel each passed
1,367 Python tests: WSL in 96.5 s (12 skips), Windows in 144.5 s (40 skips,
including unavailable optional pytest checks). The final fixture set does not
depend on private development directories. An earlier Windows run exposed a
header-rejection test race; the fixture now requires rejection before sending
the body, without relaxing server authentication.

The first GitHub-hosted run passed on Ubuntu but exposed Windows short-path
assumptions and a Python 3.12 file-timestamp discrepancy. The fixes retain file
identity checks and recognize cached source aliases during observation. Added
regressions check both valid aliases and stale code, plus timestamp/identity
changes; tests are not skipped to accommodate the runner.
After these fixes, local full suites passed 1,373 tests on each platform (WSL:
91.3 s, 12 skips; Windows: 153.7 s, 35 skips). A separate Windows Python 3.12.10
check passed 189 relevant tests, with 6 platform/optional-dependency skips.

- The pre-publication application passed 1,367 Python tests on each native
  platform: Windows Python 3.10 and WSL Python 3.12. Platform-specific skips were
  35 and 12 respectively. Seven Node UI harnesses passed.
- The same installed wheel was exercised with a real browser against both
  native services, including an asset-free setup and narrow/desktop layouts.
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
