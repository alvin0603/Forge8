# Third-party material

Forge8's own code is MIT licensed. Other material keeps its original license.

- llama.cpp is MIT licensed. Runtime manifests identify the exact upstream
  release or reviewed local build; CUDA libraries and other bundled components
  retain their respective distribution terms. Runtime files are downloaded
  separately, not shipped in this repository.
- Qwen3.5-9B and its selected Unsloth quantization are identified in
  `config/models/qwen35_9b_q4_k_m.json` under Apache-2.0. The manifest pins the
  quantized artifact; it does not prove an independently reproduced conversion.
- Optional Gemma models retain their upstream licenses, recorded in each
  model manifest; they are not relicensed under Forge8's MIT license or included
  in the default reading setup.
- Optional guest trials use CPython (Python/PSF notices) and Wasmtime
  (Apache-2.0 with LLVM exception), installed separately. See the pinned URLs
  and setup procedure in [isolated trials](docs/isolated-experiments.md).

Model weights, native runtime binaries, downloaded archives, development
evaluation corpora and build toolchains are not distributed in this repository.
Review each upstream's terms before redistributing your own package containing them.
