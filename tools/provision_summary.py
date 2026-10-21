#!/usr/bin/env python3
"""Explicit public asset provisioning; never imported by offline inference."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import urllib.request


REPO = "unsloth/Qwen3-4B-Instruct-2507-GGUF"
REVISION = "a06e946bb6b655725eafa393f4a9745d460374c9"
FILE = "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
SIZE = 2497281120
SHA = "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597"
BASE = "Qwen/Qwen3-4B-Instruct-2507"
BASE_REVISION = "cdbee75f17c01a7cc42f958dc650907174af0554"
MARKER = "local-voice-summary-assets-v1\n"


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1_048_576):
            digest.update(block)
    return digest.hexdigest()


def capacity(root):
    if shutil.disk_usage(root).free < 100 * 1024 ** 3:
        raise RuntimeError("100 GiB capacity floor")
    size = sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())
    if size > 4 * 1024 ** 3:
        raise RuntimeError("4 GiB asset cap")


def atomic_json(path, value):
    descriptor, name = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def provision(root):
    if root.is_symlink():
        raise RuntimeError("symlink asset root")
    if root.exists():
        if (root / ".local-voice-owned").is_symlink() or not (root / ".local-voice-owned").is_file() or (root / ".local-voice-owned").read_text() != MARKER:
            raise RuntimeError("unknown asset directory collision")
    else:
        root.mkdir(mode=0o700, parents=True)
        (root / ".local-voice-owned").write_text(MARKER)
    root.chmod(0o700)
    capacity(root)
    destination = root / FILE
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or destination.stat().st_size != SIZE or checksum(destination) != SHA:
            raise RuntimeError("existing model differs; preserved")
    else:
        descriptor, name = tempfile.mkstemp(prefix=".model-", dir=root)
        try:
            digest, total = hashlib.sha256(), 0
            request = urllib.request.Request(f"https://huggingface.co/{REPO}/resolve/{REVISION}/{FILE}",
                                             headers={"User-Agent": "local-voice-explicit-provision/1"})
            # No tokens, credentials, implicit model resolution, or runtime requests.
            with os.fdopen(descriptor, "wb") as target, urllib.request.urlopen(request, timeout=60) as source:
                while block := source.read(4_194_304):
                    total += len(block)
                    if total > SIZE:
                        raise RuntimeError("public model exceeds pinned size")
                    capacity(root)
                    digest.update(block)
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            if total != SIZE or digest.hexdigest() != SHA:
                raise RuntimeError("public model checksum mismatch")
            os.link(name, destination)
        finally:
            Path(name).unlink(missing_ok=True)
    license_path = root / "LICENSE.base"
    if license_path.is_symlink():
        raise RuntimeError("license symlink collision; preserved")
    if not license_path.exists():
        with urllib.request.urlopen(f"https://huggingface.co/{BASE}/resolve/{BASE_REVISION}/LICENSE", timeout=30) as source:
            license_text = source.read(65537)
        if len(license_text) > 65536 or b"Apache License" not in license_text:
            raise RuntimeError("license unavailable")
        with license_path.open("xb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(license_text)
    runner = Path("/usr/lib/ollama/llama-server").resolve()
    cuda = Path("/usr/lib/ollama/cuda_v13/libggml-cuda.so").resolve()
    if not runner.is_file() or not cuda.is_file():
        raise RuntimeError("installed CUDA llama-server unavailable")
    files = {runner, cuda}
    files.update(path.resolve() for path in runner.parent.glob("*.so*") if path.is_file())
    files.update(path.resolve() for path in cuda.parent.glob("*.so*") if path.is_file())
    runtime = {str(path): {"bytes": path.stat().st_size, "sha256": checksum(path)} for path in sorted(files)}
    manifest = {"schemaVersion": 1, "backend": "llama-server-cuda", "runner": str(runner),
                "cudaBackend": str(cuda), "runtimeFiles": runtime,
                "model": {"repo": REPO, "revision": REVISION, "file": FILE, "bytes": SIZE, "sha256": SHA,
                          "quantizer": "Unsloth (third-party quantization)", "quantization": "Q4_K_M"},
                "base": {"repo": BASE, "revision": BASE_REVISION, "license": "Apache-2.0",
                         "licenseSha256": checksum(license_path)}}
    atomic_json(root / "manifest.json", manifest)
    capacity(root)
    print(json.dumps({"status": "verified", "modelBytes": SIZE, "modelSha256": SHA,
                      "runtimeFiles": len(runtime), "assetBytes": sum(p.stat().st_size for p in root.iterdir() if p.is_file())}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path,
                        default=Path.home() / ".local/share/local-voice/models/qwen3-4b-instruct-2507-q4_k_m")
    provision(parser.parse_args().destination.expanduser().absolute())
