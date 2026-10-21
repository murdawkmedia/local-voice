# Optional offline meeting summaries

Meeting transcription works without a summary model. Summaries are an explicit
separate action after saving a meeting and use saved text; recording never starts
a summary automatically. They have no cloud account or allowance.

The currently qualified summary backend is the system Ollama `llama-server` at
`/usr/lib/ollama/llama-server` with its CUDA v13 backend and the pinned
Qwen3-4B-Instruct-2507 Q4_K_M model. Other packaging layouts, CPU-only summary
backends and other GPUs are not yet supported by the strict runtime verifier.
No automatic system package installation is performed.

`python tools/provision_summary.py --help` describes explicit provisioning of the
public model/tokenizer assets. It downloads about 2.5 GB only when invoked and
records lengths/hashes for the model and installed runtime files. It requires
100 GiB free as a conservative initial provisioning guard. Review the model's
Apache-2.0 terms, NVIDIA/CUDA dependencies and actual available disk space first.

The inference child has a network namespace, bounded memory/output and a deadline.
Meeting text is never logged or sent to a hosted service. Missing dependencies
produce a visible local setup error; they do not silently use another backend.
