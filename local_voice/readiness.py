"""One silent shortcut cue from actual frames, outside capture and Qt workers."""

import threading


class ReadyNotifier:
    def __init__(self, notify, *, enabled=True):
        self._notify = notify
        self._changed = threading.Condition()
        self._enabled = enabled
        self._sid = None
        self._pending = False
        self._sent = False
        self._closed = False
        self._thread = threading.Thread(target=self._run, name='voice-ready', daemon=True)
        self._thread.start()

    def arm(self, sid):
        with self._changed:
            if not self._closed:
                self._sid, self._sent, self._pending = sid, False, False

    def cancel(self, sid=None):
        with self._changed:
            if sid is None or sid == self._sid:
                self._sid, self._pending = None, False

    def set_enabled(self, enabled):
        with self._changed:
            self._enabled = enabled
            if not enabled:
                self._pending = False

    def observe(self, value):
        """Controller callback: bounded state only; no storage or desktop calls."""
        with self._changed:
            if self._closed or self._sid is None or value.get('sessionId') != self._sid:
                return
            if not value.get('captureActive') or value.get('state') not in {'preparing', 'recording'}:
                self._sid, self._pending = None, False
                return
            microphone = value.get('capture', {}).get('tracks', {}).get('microphone', {})
            if (self._enabled and not self._sent and value.get('kind') == 'dictation'
                    and value.get('state') == 'recording' and microphone.get('ready')
                    and microphone.get('frames', 0) > 0):
                self._pending = True
                self._changed.notify()

    def _run(self):
        while True:
            with self._changed:
                self._changed.wait_for(lambda: self._closed or self._pending)
                if self._closed:
                    return
                self._pending, self._sent = False, True
            # Never hold the callback lock across a desktop process. Cancel/Stop
            # discards queued work; an already submitted popup expires shortly.
            try:
                self._notify()
            except Exception:
                # Existing in-window Listening remains available if the desktop
                # notification service is absent. Never disrupt capture or paste.
                pass

    def close(self, timeout=2):
        with self._changed:
            self._closed, self._sid, self._pending = True, None, False
            self._changed.notify()
        self._thread.join(timeout=max(0, timeout))
        return not self._thread.is_alive()
