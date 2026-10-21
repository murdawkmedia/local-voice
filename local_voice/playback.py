"""Dictation-only playback gain. Never change sinks, profiles or defaults."""

import hashlib
import json
import os
from pathlib import Path
import threading

from .desktop import run_command, _pulse_server, validate_pulse_server


class PlaybackDucker:
    def __init__(self, *, runner=run_command, server=None, journal=None):
        self.runner = runner
        self.server = validate_pulse_server(server) if server is not None else _pulse_server()
        self.journal = Path(journal) if journal else None
        self._lock = threading.RLock()
        self._owned = []
        if self.journal and self.journal.exists():
            if self.journal.is_symlink() or self.journal.stat().st_size > 262144:
                raise ValueError('Unsupported playback recovery journal')
            data = json.loads(self.journal.read_bytes())
            if not isinstance(data, list) or len(data) > 256:
                raise ValueError('Unsupported playback recovery journal')
            for row in data:
                if (not isinstance(row, dict) or type(row.get('index')) is not int or
                        not isinstance(row.get('identity'), str) or
                        not all(isinstance(row.get(k), list) and 1 <= len(row[k]) <= 32 and
                                all(type(v) is int and 0 <= v < 2**31 for v in row[k])
                                for k in ('original', 'lowered'))):
                    raise ValueError('Unsupported playback recovery journal')
            self._owned = data

    def _save(self):
        if self.journal is None:
            return
        if self.journal.is_symlink() or self.journal.parent.is_symlink():
            raise ValueError('Playback journal must be private')
        self.journal.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.journal.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(self._owned, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.journal)
        directory = os.open(self.journal.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _command(self, args):
        return self.runner(['pactl', '--server=' + self.server] + args)

    @staticmethod
    def _identity(row):
        props = row.get('properties', {})
        fields = {k: v for k, v in props.items() if k in {
            'application.name', 'application.process.id', 'application.process.binary'}}
        return hashlib.sha256(json.dumps([row.get('client'), fields],
                                         sort_keys=True).encode()).hexdigest()

    def _streams(self):
        rows = json.loads(self._command(['-f', 'json', 'list', 'sink-inputs']))
        if not isinstance(rows, list) or len(rows) > 256:
            raise ValueError('Unsupported playback stream count')
        result = {}
        for row in rows:
            values = [item['value'] for item in row.get('volume', {}).values()]
            if (type(row.get('index')) is not int or not 1 <= len(values) <= 32 or
                    not all(type(v) is int and 0 <= v < 2**31 for v in values)):
                raise ValueError('Unsupported playback metadata')
            result[row['index']] = {'index': row['index'], 'identity': self._identity(row), 'volume': values}
        return result

    def start(self):
        with self._lock:
            if self._owned:
                return
            streams = self._streams()
            for row in streams.values():
                original = row.pop('volume')
                lowered = [v // 4 for v in original]
                if lowered == original:
                    continue
                self._owned.append(row | {'original': original, 'lowered': lowered})
                self._save()  # Record ownership before a possibly interrupted mutation.
                self._command(['set-sink-input-volume', str(row['index']), *map(str, lowered)])

    def stop(self):
        with self._lock:
            if not self._owned:
                return {'restored': 0, 'preserved': 0}
            streams = self._streams()
            result = {'restored': 0, 'preserved': 0}
            pending = []
            for row in self._owned:
                current = streams.get(row['index'])
                if (current and current['identity'] == row['identity'] and
                        current['volume'] == row['lowered']):
                    try:
                        self._command(['set-sink-input-volume', str(row['index']), *map(str, row['original'])])
                    except Exception:
                        pending.append(row)
                        continue
                    result['restored'] += 1
                else:
                    result['preserved'] += 1
            self._owned = pending
            self._save()
            return result
