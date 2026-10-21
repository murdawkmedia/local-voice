"""Private atomic settings; source preferences never replace explicit user edits."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import threading


DEFAULTS = {
    'autoPaste': True, 'lowerPlayback': False, 'cleanFillers': True, 'meetingSystemAudio': True,
    'meetingSummary': False, 'microphoneSource': '', 'computerSource': '',
    'engine': 'whisper', 'language': 'en', 'hotkeyEnabled': False, 'readyNotification': True,
}
_PREFERENCES = {'automaticPaste': 'autoPaste', 'mutePlayback': 'lowerPlayback',
                'meetingSystemAudio': 'meetingSystemAudio', 'meetingSummary': 'meetingSummary'}


class SettingsError(ValueError):
    """Invalid settings without exposing file contents."""


def _validate(fields):
    if fields.keys() - DEFAULTS.keys():
        raise SettingsError('Unknown setting')
    for key, value in fields.items():
        if isinstance(DEFAULTS[key], bool):
            if type(value) is not bool:
                raise SettingsError('Expected a boolean setting')
        elif not isinstance(value, str) or len(value) > 1024 or any(c in value for c in '\x00\r\n'):
            raise SettingsError('Expected a bounded text setting')
        elif key == 'engine' and value not in {'parakeet', 'whisper'}:
            raise SettingsError('Unknown local engine')
        elif key == 'language' and (not value or len(value) > 32):
            raise SettingsError('Invalid language setting')


class Settings:
    def __init__(self, path):
        self.path = Path(path).absolute()
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise SettingsError('Settings paths cannot be symlinks')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self._mutex = threading.RLock()
        with self._locked():
            if self.path.exists():
                self._load()
                self.path.chmod(0o600)
            else:
                self._save(dict(DEFAULTS), set())

    @contextmanager
    def _locked(self):
        with self._mutex:
            fd = os.open(self.path.with_name(self.path.name + '.lock'),
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise SettingsError('Unsupported settings lock')
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _load(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise SettingsError('Unsupported settings path')
        try:
            with self.path.open('rb') as stream:
                payload = stream.read(65_537)
            if len(payload) > 65_536:
                raise SettingsError('Settings file is too large')
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get('schemaVersion', 1) != 1:
                raise SettingsError('Unsupported settings schema')
            known = {key: value for key, value in data.items() if key in DEFAULTS}
            _validate(known)
            edited = data.get('_edited', [])
            if not isinstance(edited, list) or any(not isinstance(key, str) or key not in DEFAULTS for key in edited):
                raise SettingsError('Invalid settings edit markers')
            return dict(DEFAULTS) | known, set(edited)
        except (ValueError, UnicodeError, RecursionError):
            raise SettingsError('Settings could not be read safely') from None

    def _save(self, values, edited):
        payload = json.dumps(values | {'schemaVersion': 1, '_edited': sorted(edited)},
                             ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
        fd, temporary = tempfile.mkstemp(prefix='.settings-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def get(self):
        with self._locked():
            values, _ = self._load()
            return values

    def update(self, **fields):
        _validate(fields)
        with self._locked():
            values, edited = self._load()
            values.update(fields)
            # This cue has no imported counterpart, so it needs no import-edit
            # marker. Dev5 ignores the new value but rejects unknown markers;
            # keep its loader usable when rolling back after a cue toggle.
            edited.update(fields.keys() - {'readyNotification'})
            self._save(values, edited)
            return dict(values)

    def import_native_preferences(self, preview):
        preferences = preview.get('preferences', {}) if isinstance(preview, dict) else {}
        if not isinstance(preferences, dict):
            raise SettingsError('Unsupported preference preview')
        result = {'imported': 0, 'preserved': 0}
        with self._locked():
            values, edited = self._load()
            for old, new in _PREFERENCES.items():
                if type(preferences.get(old)) is not bool:
                    continue
                if new in edited:
                    result['preserved'] += 1
                else:
                    values[new] = preferences[old]
                    result['imported'] += 1
            self._save(values, edited)
        return result
