# Engine and model setup

The application consumes a structured file-result protocol added to a pinned
Voxtype source revision. A stock Voxtype binary lacks this result boundary. Speech
runs as a supervised child in a network namespace, with bounded output, deadlines
and memory, not as an always-listening Voxtype service.

Install Rust/Cargo, a C/C++ compiler, CMake, pkg-config and ALSA/OpenSSL development
headers. Vulkan builds additionally need Vulkan headers and `glslc`. Build in a
separate directory; do not modify an existing installed Voxtype service.

```bash
git clone https://github.com/peteonrails/voxtype.git engine-source
git -C engine-source checkout --detach 014c56105b2f071bb3457c51a95c38be380e5124
git -C engine-source apply ../patches/voxtype-result-json.patch
cargo test --manifest-path engine-source/tests/result-json-harness/Cargo.toml --locked --jobs 2
cargo build --manifest-path engine-source/Cargo.toml --locked --release --features parakeet --jobs 2
sha256sum engine-source/target/release/voxtype
```

Use the printed hash as the explicit `--sha256` when registering your trusted local
build. Builds differ by toolchain and linked libraries; a locally produced binary
should not be expected to match another machine's compiled hash. Registration
refuses checksum mismatches or replacing an existing different engine. It writes a
small app-owned manifest; runtime continues to verify the selected binary.

To enable Whisper Vulkan, build with `--features parakeet,gpu-vulkan` instead and
pass `--vulkan-icd /usr/share/vulkan/icd.d/nvidia_icd.json` for that supported NVIDIA
setup. Only the speech child receives the override. Other administrator-installed
system ICD JSONs are accepted by the same strict verifier, but AMD/Intel GPU
behavior is not qualified. CPU registration omits the ICD argument. CPU Whisper
may be substantially slower. Parakeet registration uses `--engine parakeet`; select
Parakeet explicitly in Settings after its files are provisioned.

## Models

Obtain weights directly from an upstream source and review the model's license and
checksums. No weights are in this repository and the application never downloads
them during recognition.

- Whisper: place exactly one suitable `ggml-*.bin` in
  `~/.local/share/local-voice/models/whisper-turbo-q5_0/`. The qualified model family
  is large-v3-turbo Q5_0. See [whisper.cpp models](https://huggingface.co/ggerganov/whisper.cpp).
- Parakeet: place the **Voxtype-compatible** int8 v3 files `encoder-model.int8.onnx`,
  `decoder_joint-model.int8.onnx`, `vocab.txt`, and `config.json` in
  `~/.local/share/local-voice/models/parakeet-v3-int8/`. These are not the split
  encoder/decoder/joiner Android Sherpa files. Follow the pinned Voxtype backend's
  model preparation instructions; arbitrary Parakeet exports are not interchangeable.

Run `python tools/register_model.py --engine whisper` (or parakeet) only after those
files are in place. This records hashes/lengths for subsequent integrity checks;
it is not a provenance verifier and never downloads files. Existing manifests are
preserved. Moving or editing weights causes a visible setup failure until you
deliberately review and reprovision them.

The default state/config paths can be overridden by CLI arguments. Custom state
roots need corresponding model directories; registration tools document the default
layout. Models, registered engines and private results must never be committed.
