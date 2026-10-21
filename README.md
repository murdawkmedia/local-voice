# Local Voice for Linux

Offline dictation and meeting notes for Linux. Hold Ctrl + Super, wait for the
silent **Listening** cue, speak, then release. Dictation can remove clear English
hesitations without rewriting your wording. The original recognition remains saved.

Meetings capture separate microphone and computer-audio tracks, show text in chunks
while recording, and save editable titles, transcripts and notes. Summaries are
optional and local. Successful audio is removed after nonempty text is saved;
failed or interrupted attempts keep audio for Retry. No account, telemetry or
cloud fallback is used. The app never presses Enter or changes Bluetooth profiles.

This is an early source release, **0.1.0.dev6**, developed on a Hyprland/systemd/
PipeWire desktop. There is no general-purpose binary installer yet. Other desktops,
distributions and hardware need testing. Initial model/GPU loading may take time;
the readiness cue reports actual audio arrival, rather than promising instant capture.

## Developer setup

Requirements: Linux, Python 3.11+, systemd user scopes, unprivileged user/network
namespaces, PipeWire with PulseAudio compatibility (`parec`, `pactl`), `wl-copy`,
`wtype`, `notify-send`, and Hyprland's `hyprctl` for guarded automatic insertion.
The Ctrl+Super listener needs read access to input devices; configure a narrowly
scoped system policy yourself. The app does not install broad input permissions.
On other Wayland desktops, text remains available for copying when focus cannot
be verified. No blind automatic paste is attempted.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests
```

Speech weights and the speech executable are deliberately separate from source.
Build the pinned [Voxtype](https://github.com/peteonrails/voxtype) file engine with
the included structured-result patch; see [engine setup](docs/ENGINE.md). Then
register your explicitly verified binary:

```bash
.venv/bin/python tools/register_engine.py --engine whisper \
  --binary /path/to/your/voxtype --sha256 YOUR_VERIFIED_SHA256
.venv/bin/python tools/register_model.py --engine whisper
.venv/bin/local-voice
```

`register_model.py` requires that you have already obtained the documented local
model files and verified their provenance. Neither registration command downloads
or records anything. Runtime refuses missing or mismatched engines/models.
The default registration is CPU; an explicit administrator-installed Vulkan ICD
can be selected for supported Whisper GPU builds. Details and limits are in
[engine setup](docs/ENGINE.md).

Local state uses `~/.local/share/local-voice`, settings use
`~/.config/local-voice`, and engine registration uses
`~/.local/opt/local-voice/engines`. These are not part of the repository. Optional
summaries currently require the documented Qwen3/llama-server CUDA setup; see
[summaries](docs/SUMMARIES.md). They never start automatically with a meeting.

## Using it

Enable the hold shortcut in Settings, or use **Start dictation**. Minimize the
window to keep the app available; closing quits. Shortcut results paste only when
the original window and released physical modifiers can be verified. Otherwise
use History/Copy. No action sends a message. **Record meeting** requires both
selected audio sources and fails visibly if either is unavailable.

Settings can disable English filler cleanup or the silent ready notification.
Do Not Disturb may hide the notification. Failed audio stays private until Retry
or deliberate removal. Successful audio deletion cannot be undone by code rollback.
Original and edited transcript text remain available after audio deletion.

## Source, contributions and license

Nostr is the primary repository transport; GitHub is a mirror at
[murdawkmedia/local-voice](https://github.com/murdawkmedia/local-voice).
The separate Android app is at
[local-voice-android](https://github.com/murdawkmedia/local-voice-android).
Keep issues and patches free of recordings, transcripts, credentials and private
device logs. Generated test fixtures are welcome. See [CONTRIBUTING](CONTRIBUTING.md)
and [third-party notices](THIRD-PARTY.md).

Application source is GPL-3.0-or-later. PyQt is distributed under GPL v3 or a
commercial license; see [Riverbank's licensing information](https://www.riverbankcomputing.com/software/pyqt/intro).
Model weights and upstream engines have their own licenses and are not included.
This project is independent of Wispr Flow and is not an official Wispr client.
