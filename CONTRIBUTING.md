# Contributing

Use generated audio/text for tests. Never attach private dictation, account tokens,
signing keys or model weights. Explain the desktop/audio stack and the visible
failure without posting personal device identifiers. Include focused tests and run
`QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests` before a patch.

Preserve explicit recording, separate capture/model workers, bounded queues,
session ownership, guarded insertion and optional summaries. Do not keep a hidden
microphone open or add network inference. Never press Enter on behalf of the user.
Do not change system audio/Bluetooth profiles or user services to make a test pass.

Nostr proposals and GitHub pull requests are accepted. Mirrors must be updated with
explicit intended refs after review; never force-push private development history.
