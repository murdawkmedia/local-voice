"""Explicit registration of locally built engines; no download or automatic trust."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .engine import EngineError


def _read(path):
    if path.is_symlink(): raise ValueError('Symlinked engine manifest')
    if not path.exists(): return {'schemaVersion': 1, 'engines': {}}
    if not path.is_file() or path.stat().st_size > 16384: raise ValueError('Invalid engine manifest')
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or data.get('schemaVersion') != 1
            or not isinstance(data.get('engines'), dict)
            or set(data['engines']) - {'whisper', 'parakeet'}): raise ValueError('Invalid engine manifest')
    for kind, entry in data['engines'].items():
        if (not isinstance(entry, dict) or entry.get('file') != 'voxtype-local-voice-'+kind
                or not isinstance(entry.get('sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])): raise ValueError('Invalid engine identity')
        icd = entry.get('vulkanIcd')
        if icd is not None and (not isinstance(icd, str) or len(icd) > 4096 or ':' in icd
                or Path(icd).parent not in {Path('/usr/share/vulkan/icd.d'), Path('/etc/vulkan/icd.d')}
                or Path(icd).suffix != '.json' or kind != 'whisper'): raise ValueError('Invalid system driver')
    return data


def select_engine(app_dir, kind, fallback, default_icd):
    path = Path(app_dir)/'engines/manifest.json'
    try:
        if not path.exists() and not path.is_symlink(): return (*fallback[kind], default_icd)
        item = _read(path)['engines'][kind]
        return item['file'], item['sha256'], Path(item['vulkanIcd']) if item.get('vulkanIcd') else None
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise EngineError('setup') from None


def _digest(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def register_engine(app_dir, kind, source, expected, vulkan_icd=None):
    source, app_dir = Path(source), Path(app_dir).absolute()
    if (kind not in {'whisper', 'parakeet'} or not re.fullmatch('[0-9a-f]{64}', expected)
            or source.is_symlink() or not source.is_file() or source.stat().st_size > 1024**3
            or _digest(source) != expected): raise ValueError('Verify the local binary and its expected SHA256')
    folder = app_dir/'engines'; manifest = folder/'manifest.json'
    for path in (folder, *folder.parents):
        if path.is_symlink(): raise ValueError('Symlinked application path')
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if folder.stat().st_uid != os.getuid(): raise ValueError('Application owner differs')
    lock = os.open(folder/'.setup.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode): raise ValueError('Invalid setup lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read(manifest); name = 'voxtype-local-voice-'+kind
        data['engines'][kind] = {'file': name, 'sha256': expected, 'vulkanIcd': vulkan_icd}
        # Validate the new entry using the same strict rules before committing.
        if vulkan_icd is not None and (kind != 'whisper' or ':' in vulkan_icd
                or Path(vulkan_icd).parent not in {Path('/usr/share/vulkan/icd.d'), Path('/etc/vulkan/icd.d')}
                or Path(vulkan_icd).suffix != '.json'): raise ValueError('Choose a system Vulkan driver')
        target = folder/name
        if target.is_symlink() or (target.exists() and (not target.is_file() or _digest(target) != expected)):
            raise ValueError('Existing engine differs; preserved')
        descriptor, temporary = tempfile.mkstemp(prefix='.engine-', dir=folder)
        try:
            with os.fdopen(descriptor, 'wb') as stream, source.open('rb') as incoming:
                copied = 0
                while chunk := incoming.read(1048576):
                    copied += len(chunk)
                    if copied > 1024**3: raise ValueError('Source exceeded engine size limit')
                    stream.write(chunk)
                stream.flush(); os.fchmod(stream.fileno(), 0o700); os.fsync(stream.fileno())
            if _digest(Path(temporary)) != expected: raise ValueError('Source changed; preserved')
            if not target.exists(): os.link(temporary, target)
        finally: Path(temporary).unlink(missing_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.manifest-', dir=folder)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(data, stream, indent=2); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, manifest)
        finally: Path(temporary).unlink(missing_ok=True)
        directory = os.open(folder, os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally: os.close(lock)
