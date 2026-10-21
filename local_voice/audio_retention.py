"""Discard only native successful audio, after durable text and cleanup intent."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import uuid


class AudioRetention:
    MAX_FILES = 20_000

    def __init__(self, store, recordings):
        self.store = store
        self.recordings = Path(recordings).absolute()
        self.lock = threading.RLock()

    @staticmethod
    def _regular(info):
        return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1

    @staticmethod
    def _entries(fd, maximum):
        names = []
        with os.scandir(fd) as entries:
            for entry in entries:
                if len(names) >= maximum:
                    raise ValueError('Audio inventory exceeded its bound')
                names.append(entry.name)
        return names

    def _remove_audio(self, sid, kind):
        sources = {'microphone', 'computer'} if kind == 'meeting' else {'microphone'}
        if any(p.is_symlink() for p in (self.recordings, *self.recordings.parents)):
            return False
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        with ExitStack() as stack:
            root = os.open(self.recordings, flags)
            stack.callback(os.close, root)
            try:
                directory = os.open(sid, flags, dir_fd=root)
            except FileNotFoundError:
                return True  # A prior cleanup/crash may have removed the directory.
            stack.callback(os.close, directory)
            manifest = os.open('session.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                               dir_fd=directory)
            stack.callback(os.close, manifest)
            if not self._regular(os.fstat(manifest)):
                return False
            payload = os.read(manifest, 4097)
            if len(payload) > 4096:
                return False
            data = json.loads(payload)
            if (not isinstance(data, dict) or data.get('schemaVersion') != 1 or
                    data.get('kind') != kind or data.get('rate') != 48000 or
                    type(data.get('chunkFrames')) is not int or
                    not 1 <= data['chunkFrames'] <= 48000 * 600):
                return False
            planned = []
            directories = {directory}
            for name in self._entries(directory, 128):
                if name in {source + '.wav' for source in sources}:
                    planned.append((directory, name))
                elif name == 'chunks':
                    chunks = os.open(name, flags, dir_fd=directory)
                    stack.callback(os.close, chunks)
                    directories.add(chunks)
                    for chunk in self._entries(chunks, self.MAX_FILES):
                        match = re.fullmatch(r'(microphone|computer)-[0-9]{8}\.wav', chunk)
                        if match is None or match[1] not in sources:
                            return False
                        planned.append((chunks, chunk))
                elif name.endswith('.wav'):
                    return False  # Preserve an unexpected track rather than guessing.
            checked = []
            for fd, name in planned:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not self._regular(info):
                    return False
                checked.append((fd, name, info.st_dev, info.st_ino))
            # Preflight every target before the first unlink. Descriptor-relative
            # operations never traverse a substituted session/chunk symlink.
            for fd, name, device, inode in checked:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not self._regular(info) or (info.st_dev, info.st_ino) != (device, inode):
                    return False
                os.unlink(name, dir_fd=fd)
            for fd in directories:
                os.fsync(fd)
        return True

    def clean(self, sid):
        """Best effort; cleanup failure never turns saved text into a failed session."""
        with self.lock:
            try:
                if str(uuid.UUID(sid)) != sid or uuid.UUID(sid).version != 4:
                    return False
                if not self.store.begin_audio_cleanup(sid):
                    return False
                row = self.store.get_session(sid)
                if row['audio_state'] == 'removed':
                    return True
                if not self._remove_audio(sid, row['kind']):
                    return False
                self.store.finish_audio_cleanup(sid)
                return True
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                return False

    def sweep(self, stop=None):
        """Bounded startup pass over eligible native identities, without audio reads."""
        after = ''
        for _ in range(313):  # At most 10,016 identities; later startup resumes the rest.
            try:
                candidates = self.store.audio_cleanup_candidates(after, 32)
            except (OSError, ValueError, sqlite3.Error):
                return
            if not candidates:
                return
            for sid in candidates:
                if stop is not None and stop.is_set():
                    return
                self.clean(sid)
                after = sid
