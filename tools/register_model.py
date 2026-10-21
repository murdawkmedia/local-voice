#!/usr/bin/env python3
"""Create a checksum manifest for model files you explicitly provisioned."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=['whisper', 'parakeet'], required=True)
    args = parser.parse_args()
    root = Path.home()/'.local/share/local-voice/models'/('whisper-turbo-q5_0' if args.engine == 'whisper' else 'parakeet-v3-int8')
    if any(path.is_symlink() for path in (root, *root.parents)): raise ValueError('Symlinked model path preserved')
    if root.is_symlink() or not root.is_dir(): raise ValueError('Provision model files in the documented app-owned directory first')
    names = sorted(p.name for p in root.glob('ggml-*.bin')) if args.engine == 'whisper' else ['encoder-model.int8.onnx','decoder_joint-model.int8.onnx','vocab.txt','config.json']
    if not names or (args.engine == 'whisper' and len(names) != 1): raise ValueError('Expected the documented model files')
    files = {}
    for name in names:
        path = root/name
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 10*1024**3: raise ValueError('Invalid model file')
        with path.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        files[name] = {'bytes': path.stat().st_size, 'sha256': digest}
    target = root/'manifest.json'
    if target.exists() or target.is_symlink(): raise ValueError('Existing model manifest preserved; review it before replacement')
    fd, name = tempfile.mkstemp(prefix='.model-', dir=root)
    try:
        with os.fdopen(fd,'w') as stream: json.dump({'files':files}, stream, indent=2);stream.flush();os.fsync(stream.fileno())
        os.link(name, target)
    finally: Path(name).unlink(missing_ok=True)
    print('Local model manifest created. No files were downloaded.')

if __name__ == '__main__': main()
