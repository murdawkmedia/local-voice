"""Provisioned speech selection. Never fetch models or consult cloud services."""

import hashlib
from pathlib import Path

from .engine import EngineError, EngineSpec, LocalEngine
from .engine_selection import select_engine


class SpeechRuntime:
    NVIDIA_VULKAN_ICD = Path('/usr/share/vulkan/icd.d/nvidia_icd.json')
    ENGINE_FILES = {
        'parakeet': ('voxtype-local-voice-parakeet-cpu',
                     '884d7813285af5d97fd4f93427d2e5cd9ca691ce7e2e1eac2f8cdc58c371c5c9'),
        'whisper': ('voxtype-local-voice-parakeet-vulkan',
                    'd9e74c35a05bb7e9a72f2c781717c02145dd7f5ec043f6bd9197cd453287fc86'),
    }

    def __init__(self, app_dir, state_dir, settings, store, model_lock):
        self.app_dir = Path(app_dir)
        self.state_dir = Path(state_dir)
        self.settings = settings
        self.store = store
        self.model_lock = model_lock
        self._engine = None
        self._key = None
        self._verified_binary = None

    def _select(self):
        values = self.settings.get()
        kind = values['engine']
        name, expected, driver = select_engine(self.app_dir, kind, self.ENGINE_FILES,
                                               self.NVIDIA_VULKAN_ICD if kind == 'whisper' else None)
        binary = self.app_dir / 'engines' / name
        try:
            info = binary.stat()
            if binary.is_symlink():
                raise OSError()
            identity = (str(binary), info.st_size, info.st_mtime_ns, expected, str(driver))
            if identity != self._verified_binary:
                if hashlib.sha256(binary.read_bytes()).hexdigest() != expected:
                    raise OSError()
                self._verified_binary = identity
        except OSError:
            raise EngineError('setup') from None
        words = self.store.personalization()['words']
        hints = ', '.join(words)[:2048] if kind == 'whisper' else ''
        key = (kind, values['language'], hints, identity)
        if key != self._key:
            model = self.state_dir / 'models' / ('parakeet-v3-int8' if kind == 'parakeet' else 'whisper-turbo-q5_0')
            self._engine = LocalEngine(EngineSpec(kind, binary, model, language=values['language'], prompt=hints,
                                                 vulkan_icd=driver),
                                       self.state_dir / 'jobs')
            self._key = key
        return self._engine

    def transcribe(self, wav_path, cancel_event):
        while not cancel_event.is_set():
            if self.model_lock.acquire(timeout=.1):
                try:
                    if cancel_event.is_set():
                        break
                    return self._select().transcribe(wav_path, cancel_event)
                finally:
                    self.model_lock.release()
        raise EngineError('cancelled')

    def diagnostics(self):
        return dict(self._engine.last_run) if self._engine else {'outcome': 'not-started'}
