"""Optional evdev Ctrl+Super listener. No grabs, configuration writes, or key logs."""

import select
import threading
import time


_CTRL = {29, 97}
_SUPER = {125, 126}
_MODIFIERS = _CTRL | _SUPER


class HotkeyListener:
    def __init__(self, on_press, on_release, status_callback=None, *, backend=None,
                 scan_interval=1.0, reconcile_interval=0.25, poll_interval=0.05):
        for value in (scan_interval, reconcile_interval, poll_interval):
            if not isinstance(value, (int, float)) or not 0.01 <= value <= 2:
                raise ValueError('Invalid hotkey polling bound')
        self.on_press = on_press
        self.on_release = on_release
        self.status_callback = status_callback
        self.backend = backend
        self.scan_interval = scan_interval
        self.reconcile_interval = reconcile_interval
        self.poll_interval = poll_interval
        self._devices = {}
        self._pressed = {}
        self._dropped = set()
        self._held = False
        self._armed = True
        self._status_value = None
        self._stop = threading.Event()
        self._lifecycle = threading.RLock()
        self._thread = None

    def _status(self, state, errors=0):
        value = {'state': state, 'devices': len(self._devices), 'errors': errors}
        if value != self._status_value:
            self._status_value = value
            if self.status_callback:
                try:
                    self.status_callback(dict(value))
                except Exception:
                    pass

    def _release(self):
        if self._held:
            self._held = False
            try:
                self.on_release()
            except Exception:
                self._status('callback-error')

    def _update(self, *, allow_press=False):
        pressed = set().union(*self._pressed.values()) if self._pressed else set()
        chord = bool(pressed & _CTRL and pressed & _SUPER)
        if not chord:
            self._release()
            self._armed = not self._dropped
        elif not self._held:
            if allow_press and self._armed and not self._dropped and not self._stop.is_set():
                self._held = True
                try:
                    self.on_press()
                except Exception:
                    self._held = False
                    self._armed = False
                    self._status('callback-error')
            elif not allow_press:
                # An already held chord discovered at startup/hotplug is a baseline,
                # never an explicit action that should begin recording.
                self._armed = False

    def _remove(self, path):
        device = self._devices.pop(path, None)
        self._pressed.pop(path, None)
        self._dropped.discard(path)
        if device is not None:
            try:
                device.close()
            except OSError:
                pass
        self._update()

    def _scan(self):
        denied = errors = 0
        try:
            paths = self.backend.list_devices()
            if len(paths) > 64:
                raise OSError('Input inventory exceeds supported boundary')
            paths = set(paths)
        except OSError:
            for path in list(self._devices):
                self._remove(path)
            self._status('unavailable', 1)
            return
        for path in set(self._devices) - paths:
            self._remove(path)
        for path in sorted(paths - self._devices.keys()):
            device = None
            try:
                device = self.backend.InputDevice(path)
                capabilities = set(device.capabilities().get(1, []))
                if not capabilities.intersection(_MODIFIERS):
                    device.close()
                    continue
                active = set(device.active_keys()) & _MODIFIERS
                self._devices[path] = device
                self._pressed[path] = active
                self._update()
            except OSError as exc:
                denied += isinstance(exc, PermissionError)
                errors += 1
                if device is not None:
                    try:
                        device.close()
                    except OSError:
                        pass
        state = ('partial' if errors else 'ready') if self._devices else 'permission-denied' if denied else 'unavailable'
        self._status(state, errors)

    def _reconcile(self):
        for path, device in list(self._devices.items()):
            if path in self._dropped:
                continue
            try:
                # Reconciliation removes lost releases only. Adding a newly held
                # modifier here could consume a press before its queued event arrives.
                self._pressed[path].intersection_update(device.active_keys())
            except OSError:
                self._remove(path)
        self._update()

    def _read(self, path, device):
        try:
            for index, event in enumerate(device.read()):
                if index >= 256:
                    raise OSError('Input batch exceeds supported boundary')
                if event.type == 0:
                    if event.code == 3:  # SYN_DROPPED
                        self._dropped.add(path)
                        self._pressed[path].clear()
                        self._release()
                        self._armed = False
                    elif event.code == 0 and path in self._dropped:  # SYN_REPORT
                        self._pressed[path] = set(device.active_keys()) & _MODIFIERS
                        self._dropped.discard(path)
                        self._update()
                    continue
                if path in self._dropped or event.type != 1 or event.code not in _MODIFIERS or event.value not in (0, 1):
                    continue
                if event.value:
                    self._pressed[path].add(event.code)
                else:
                    self._pressed[path].discard(event.code)
                self._update(allow_press=True)
        except BlockingIOError:
            pass
        except OSError:
            self._remove(path)
            self._status('device-removed')

    def _run(self):
        scanned = reconciled = float('-inf')
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now - scanned >= self.scan_interval:
                    self._scan()
                    scanned = now
                if not self._devices:
                    self._stop.wait(self.poll_interval)
                    continue
                try:
                    readable, _, _ = select.select(list(self._devices.values()), [], [], self.poll_interval)
                except (OSError, ValueError):
                    for path in list(self._devices):
                        self._remove(path)
                    self._status('device-removed')
                    continue
                for device in readable:
                    self._read(device.path, device)
                if time.monotonic() - reconciled >= self.reconcile_interval:
                    self._reconcile()
                    reconciled = time.monotonic()
        except Exception:
            self._status('listener-error')
        finally:
            self._release()
            for path in list(self._devices):
                self._remove(path)
            self._status('stopped')

    def start(self):
        """Explicitly enable listening. Root must first honor hotkeyEnabled=False."""
        with self._lifecycle:
            if self._thread is not None and self._thread.is_alive():
                return True
            if self.backend is None:
                try:
                    import evdev
                    self.backend = evdev
                except ImportError:
                    self._status('unavailable', 1)
                    return False
            self._stop.clear()
            self._armed = True
            self._thread = threading.Thread(target=self._run, name='local-voice-hotkey', daemon=True)
            self._thread.start()
            return True

    def close(self):
        self._stop.set()
        with self._lifecycle:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        return thread is None or not thread.is_alive()
