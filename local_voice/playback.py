"""Temporary PipeWire software gain; preserve hardware and application volume."""

import hashlib
import json
import math
import os
from pathlib import Path
import threading

from .desktop import run_command, _pulse_server, validate_pulse_server


class _LegacyPlaybackRecovery:
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


class PlaybackDucker(_LegacyPlaybackRecovery):
    """One output factor covers media and even newly created notification streams.

    Output journals use a separate path from the legacy per-stream journal. Never
    change microphone gain, mutes, profiles or defaults. Disconnected outputs keep
    their recovery row until the same device returns; manual gain changes win.
    """
    SCALE = 1_000_000
    # pw-dump serializes native floats with limited decimal precision. Five
    # millionths of software gain covers rounding, not an audible manual change.
    TOLERANCE = 5

    def __init__(self, *, runner=run_command, server=None, journal=None, legacy_journal=None,
                 pipewire_remote=None):
        super().__init__(runner=runner, server=server, journal=journal)
        pulse_path = Path(self.server.removeprefix('unix:'))
        runtime = pulse_path.parent.parent if pulse_path.parent.name == 'pulse' else pulse_path.parent
        remote = str(pipewire_remote or runtime / 'pipewire-0')
        if not Path(remote).is_absolute() or any(c.isspace() or c in '\0;' for c in remote):
            raise ValueError('A local PipeWire socket is required')
        self.pipewire_remote = remote
        self._active = False
        self._legacy = _LegacyPlaybackRecovery(runner=runner, server=self.server, journal=legacy_journal)

    @property
    def pending(self):
        return bool(self._owned or self._legacy._owned)

    @staticmethod
    def _output_identity(row):
        props = row.get('properties', {})
        # Stable device fields allow restoration after a Bluetooth reconnect,
        # while a reused numeric sink index alone conveys no ownership.
        fields = {key: props[key] for key in ('device.serial', 'device.name', 'node.name') if key in props}
        return hashlib.sha256(json.dumps([row.get('name'), fields], sort_keys=True).encode()).hexdigest()

    def _outputs(self):
        rows = json.loads(self.runner(['pw-dump', '--remote='+self.pipewire_remote], maximum=4_194_304))
        if not isinstance(rows, list) or len(rows) > 8192:
            raise ValueError('Unsupported audio graph')
        result = {}
        for node in rows:
            info = node.get('info') or {}
            props = info.get('props') or {}
            if node.get('type') != 'PipeWire:Interface:Node' or props.get('media.class') != 'Audio/Sink':
                continue
            params = info.get('params') or {}
            gain = next((p['volume'] for p in params.get('Props', []) if 'volume' in p), None)
            if gain is None:
                continue
            if type(gain) not in {int, float} or not math.isfinite(gain) or not 0 <= gain <= 10:
                raise ValueError('Unsupported software gain')
            row = {'index': node.get('id'), 'name': props.get('node.name'), 'properties': props}
            if type(row['index']) is not int or not isinstance(row['name'], str):
                raise ValueError('Unsupported output identity')
            values = [round(gain * self.SCALE)]
            identity = self._output_identity(row)
            if identity in result:
                raise ValueError('Ambiguous output identity')
            result[identity] = {'index': row['index'], 'identity': identity, 'volume': values}
        return result

    def _set_gain(self, index, values):
        gain = values[0] / self.SCALE
        self.runner(['pw-cli', '--remote='+self.pipewire_remote, 'set-param', str(index),
                     'Props', '{ volume: '+repr(gain)+' }'])
        current = next((row for row in self._outputs().values() if row['index'] == index), None)
        if current is None or abs(current['volume'][0] - values[0]) > self.TOLERANCE:
            raise OSError('Software playback gain was not acknowledged')

    def start(self):
        with self._lock:
            if not self._active:
                self.stop()
                self._active = True
            self.refresh()

    def refresh(self):
        with self._lock:
            if not self._active:
                return
            outputs = self._outputs()
            owned = {row['identity']: row for row in self._owned}
            failed = False
            for identity, current in outputs.items():
                if identity in owned:
                    row = owned[identity]
                    if row.get('applied', True):
                        continue
                    if (abs(current['volume'][0] - row['original'][0]) > self.TOLERANCE and
                            abs(current['volume'][0] - row['lowered'][0]) > self.TOLERANCE):
                        row['applied'] = True  # A manual change wins during recovery too.
                        self._save()
                        continue
                    try:
                        self._set_gain(current['index'], row['lowered'])
                        row['applied'] = True
                        self._save()
                    except Exception:
                        failed = True
                    continue
                original = current['volume']
                lowered = [v // 4 for v in original]
                if lowered == original:
                    continue
                row = {k: v for k, v in current.items() if k != 'volume'}
                if len(self._owned) >= 256:
                    raise ValueError('Output recovery limit reached')
                row = row | {'original': original, 'lowered': lowered, 'applied': False}
                self._owned.append(row)
                self._save()
                try:
                    self._set_gain(current['index'], lowered)
                    row['applied'] = True
                    self._save()
                except Exception:
                    failed = True
            if failed:
                raise OSError('Software playback gain is pending')

    def stop(self):
        with self._lock:
            self._active = False
            result = self._legacy.stop()
            failed = bool(self._legacy._owned)
            if not self._owned:
                if failed:
                    raise OSError('Playback restoration is pending')
                return result
            outputs = self._outputs()
            pending = []
            for row in self._owned:
                current = outputs.get(row['identity'])
                if current is None:
                    pending.append(row)
                    continue
                values = current['volume']
                if len(values) != len(row['lowered']):
                    result['preserved'] += 1
                    continue
                restored = [original if abs(value - lowered) <= self.TOLERANCE else value
                            for value, original, lowered in zip(values, row['original'], row['lowered'])]
                if restored != values:
                    try:
                        self._set_gain(current['index'], restored)
                    except Exception:
                        pending.append(row)
                        failed = True
                        continue
                    result['restored'] += 1
                else:
                    result['preserved'] += 1
            self._owned = pending
            self._save()
            if failed:
                raise OSError('Playback restoration is pending')
            return result | {'deferred': len(pending)}
