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
