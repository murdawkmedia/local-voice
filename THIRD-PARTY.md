# Third-party components

- PyQt6/Qt: GUI bindings and toolkit, GPL v3/commercial and LGPL Qt respectively.
  The application is GPL-3.0-or-later; dependency distributions retain their terms.
- evdev: Python input-event access, BSD-style license.
- Voxtype by Peter Jackson: pinned source `014c56105b2f071bb3457c51a95c38be380e5124`,
  MIT; original notice retained in `licenses/voxtype-MIT.txt`. The included patch
  adds a file-only structured result boundary and synthetic tests, not a cloud API.
- PipeWire/PulseAudio, libnotify, wl-clipboard, wtype, systemd and Hyprland are
  separately installed operating-system components, not vendored here.
- Whisper/whisper.cpp, Parakeet/ONNX and optional Qwen3/llama.cpp are separately
  provisioned engines/models. Check their licenses before obtaining or distributing
  weights. NVIDIA Parakeet v3 weights use CC BY 4.0; retain upstream attribution.

No model weights, compiled speech engines, account data or third-party APKs are
included in this repository. Dependency licenses are not replaced by this project's
license. Existing provider-export import helpers are local-only and require explicit
user action; runtime does not contact a provider account.
