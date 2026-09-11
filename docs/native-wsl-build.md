# Rebuilding the native WSL CUDA backend

This is a source-build recipe, not a one-click installer or a promise of
bit-identical binaries on another machine. The current local build passes
compilation, native help/version, linkage and runtime integrity checks; real
model/lifecycle acceptance is separate. A rebuilt candidate does **not** automatically replace the
reviewed runtime manifest or its CLI discovery pin. No Linux CUDA release
archive is being published by this project yet.

Tested build host: WSL2 Ubuntu 24.04, GCC/G++ 13.3.0, CMake 3.28.3 and GNU Make
4.3. It needs `curl`, `tar`, `xz`, `sha256sum`, and a C/C++ toolchain. Downloads
are about 1 GB; allow several GB for extraction and compilation. This recipe
targets NVIDIA compute capability **8.9** (RTX 40-series), not all NVIDIA GPUs.
The driver remains the Windows-provided WSL driver; do not install a Linux
display driver. See [platform support and deployment limits](PLATFORMS.md).

Run in native WSL Bash. Choose a build drive with several GB free; all toolkit
files stay there. The commands do not change system PATH, linker or WSL settings.
The new staging directory avoids reusing unidentified source/build leftovers.

```bash
set -euo pipefail
mkdir -p /mnt/d/Forge8/builds
forge8_native_stage=$(mktemp -d /mnt/d/Forge8/builds/wsl-b10621.XXXXXX)
cd "$forge8_native_stage"
mkdir downloads toolkit

fetch_pinned() {
    curl --fail --location --show-error "$1" --output "downloads/$2"
    printf '%s  %s\n' "$3" "downloads/$2" | sha256sum --check -
}
cuda_component() {
    fetch_pinned \
      "https://developer.download.nvidia.com/compute/cuda/redist/$1/linux-x86_64/$1-linux-x86_64-$2-archive.tar.xz" \
      "$1-linux-x86_64-$2-archive.tar.xz" "$3"
}
cuda_component cuda_nvcc 12.8.93 9961b3484b6b71314063709a4f9529654f96782ad39e72bf1e00f070db8210d3
cuda_component cuda_cudart 12.8.90 8d566b5fe745c46842dc16945cf36686227536decd2302c372be86da37faca68
cuda_component cuda_cccl 12.8.90 0740e9e01e4f15e17c5ab8d68bba4f8ec0eb6b84edccba4ac45112d2d2174e4b
cuda_component libcublas 12.8.4.1 21718957c2cf000bacd69d36c95708a2319199e39e056f8b4f0f68e3b9f323bb
fetch_pinned \
  https://codeload.github.com/ggml-org/llama.cpp/tar.gz/c1d0e7a004015f23bc0233470b747b596f29b264 \
  llama-c1d0e7a.tar.gz e381b23a9aba7e1615ef8d4713bc2f8d4777255a5b1124d633f0af280a2d5415

for forge8_archive in \
  cuda_nvcc-linux-x86_64-12.8.93-archive.tar.xz \
  cuda_cudart-linux-x86_64-12.8.90-archive.tar.xz \
  cuda_cccl-linux-x86_64-12.8.90-archive.tar.xz \
  libcublas-linux-x86_64-12.8.4.1-archive.tar.xz; do
    tar --extract --file "downloads/$forge8_archive" --directory toolkit \
      --strip-components=1 --no-same-owner
done
tar --extract --file downloads/llama-c1d0e7a.tar.gz --no-same-owner
ln -s lib toolkit/lib64

CUDA_VISIBLE_DEVICES="" GIT_CEILING_DIRECTORIES="$forge8_native_stage" \
PATH=/usr/bin:/bin cmake \
  -S llama.cpp-c1d0e7a004015f23bc0233470b747b596f29b264 -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CUDA_COMPILER="$forge8_native_stage/toolkit/bin/nvcc" \
  -DCUDAToolkit_ROOT="$forge8_native_stage/toolkit" \
  -DCUDA_cuda_driver_LIBRARY=/usr/lib/wsl/lib/libcuda.so \
  -DCMAKE_CUDA_ARCHITECTURES=89-real \
  -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON '-DCMAKE_INSTALL_RPATH=$ORIGIN' \
  "-DCMAKE_EXE_LINKER_FLAGS=-Wl,-rpath-link,\"$forge8_native_stage/toolkit/lib\"" \
  -DGGML_CUDA=ON -DGGML_NATIVE=OFF -DGGML_CUDA_NCCL=OFF -DGGML_CCACHE=OFF \
  -DLLAMA_BUILD_NUMBER=10621 \
  -DLLAMA_BUILD_COMMIT=c1d0e7a004015f23bc0233470b747b596f29b264 \
  -DGIT_EXE:FILEPATH= \
  -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_APP=OFF \
  -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_OPENSSL=OFF
PATH=/usr/bin:/bin cmake --build build --target llama-server llama-cli llama-bench --parallel 4
```

Stop immediately if a download/hash/configure/build command fails. The component
pins come from [NVIDIA's CUDA 12.8.1 manifest](https://developer.download.nvidia.com/compute/cuda/redist/redistrib_12.8.1.json).
The source archive is the exact b10621 commit, 36,963,693 bytes. The source SHA
above records the downloaded GitHub archive; it is not an upstream signature.

Why the less obvious flags matter:

- Redistributables use `lib/`, while NVCC searches `lib64/`. That link is confined
  to the **build toolchain**. Deployed files must be regular SONAME copies.
- Both UI flags are required: disabling the npm build alone still downloads a
  prebuilt UI. Neither the UI nor HTTPS support is needed for this offline API.
- The source tarball is not a Git checkout. The Git ceiling and empty GGML
  `GIT_EXE` prevent the enclosing Forge8 repository's commit being misreported
  as the backend revision. Explicit b10621 metadata remains authoritative;
  the upstream missing-Git warning is expected for a tarball build.
- The empty CUDA device list keeps compiler capability probing off the GPU.
  The explicit `89-real` architecture still builds native RTX 40-series kernels.
- `GGML_NATIVE=OFF` avoids host-specific tuning; it still enables
  SSE4.2/F16C/FMA/BMI2/AVX/AVX2. This is not a baseline-x86 CPU binary.
- `$ORIGIN` lookup is for the eventual flat runtime directory. Copy required
  shared libraries there before launching; the build directory alone does not
  contain the CUDA runtime libraries. Do not fix this with a global linker edit.
  The `-rpath-link` option provides the toolkit path to the build-time linker
  only; it does not embed a dependency on that path in the deployed binary.

After a successful build, inspect ELF dependencies, materialize the exact needed
SONAMEs as regular files, and retain all relevant license notices. Hash the
finished bundle and each deployed file. Review changes to
`config/runtimes/llama_cpp_b10621_linux_cuda.json` and the CLI config-content
pin explicitly; never silently regenerate trust pins to make verification pass.
Native inference and lifecycle acceptance are separate from compilation.
