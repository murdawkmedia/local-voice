from collections import deque
import importlib
import importlib.util
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class FakeDevice:
    def __init__(self, path, active=()):
        self.path = path
        self.fd, self._writer = os.pipe()
        os.set_blocking(self.fd, False)
        self.active = set(active)
        self.events = deque()
        self.closed = False

    def fileno(self):
        return self.fd

    def capabilities(self):
        return {1: [30, 29, 97, 125, 126]}

    def active_keys(self):
        return list(self.active)

    def emit(self, code, value, kind=1):
        if kind == 1:
            if value == 1:
                self.active.add(code)
            elif value == 0:
                self.active.discard(code)
        self.events.append(SimpleNamespace(type=kind, code=code, value=value))
        os.write(self._writer, b'x')

    def read(self):
        os.read(self.fd, 4096)
        events = list(self.events)
        self.events.clear()
        return events

    def grab(self):
        raise AssertionError('Exclusive grabs are forbidden')

    def close(self):
        if not self.closed:
            self.closed = True
            os.close(self.fd)
            os.close(self._writer)


class FakeBackend:
    def __init__(self, devices):
        self.devices = {d.path: d for d in devices}
        self.denied = set()

    def list_devices(self):
        return list(self.devices)

    def InputDevice(self, path):
        if path in self.denied:
            raise PermissionError('Generated inaccessible device')
        return self.devices[path]


class HotkeyTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('local_voice.hotkeys'),
                             'Owned hotkey listening is not implemented')
        self.module = importlib.import_module('local_voice.hotkeys')
        self.first = FakeDevice('generated-first')
        self.second = FakeDevice('generated-second')
        self.addCleanup(self.first.close)
        self.addCleanup(self.second.close)
        self.backend = FakeBackend([self.first, self.second])
        self.condition = threading.Condition()
        self.events = []
        self.statuses = []
        self.listener = None

    def record(self, value):
        with self.condition:
            self.events.append(value)
            self.condition.notify_all()

    def status(self, value):
        with self.condition:
            self.statuses.append(value)
            self.condition.notify_all()

    def start(self):
        self.listener = self.module.HotkeyListener(lambda: self.record('press'), lambda: self.record('release'),
                                                  self.status, backend=self.backend,
                                                  scan_interval=0.03, reconcile_interval=0.03, poll_interval=0.01)
        self.addCleanup(self.listener.close)
        self.assertTrue(self.listener.start())
        self.assertTrue(self.wait(lambda: any(s['devices'] == 2 for s in self.statuses)))

    def wait(self, predicate, timeout=1):
        with self.condition:
            return self.condition.wait_for(predicate, timeout)

    def test_any_left_right_modifiers_across_devices_emit_only_chord_edges(self):
        self.start()
        self.first.emit(29, 1)
        self.second.emit(126, 1)
        self.assertTrue(self.wait(lambda: self.events == ['press']))
        self.first.emit(29, 2)
        self.second.emit(126, 1)
        self.first.emit(97, 1)
        self.first.emit(29, 0)
        self.assertFalse(self.wait(lambda: len(self.events) > 1, timeout=0.06))
        self.first.emit(97, 0)
        self.assertTrue(self.wait(lambda: self.events == ['press', 'release']))

    def test_same_modifier_on_two_keyboards_does_not_release_until_both_are_up(self):
        self.start()
        self.first.emit(29, 1)
        self.first.emit(125, 1)
        self.second.emit(29, 1)
        self.assertTrue(self.wait(lambda: self.events == ['press']))
        self.first.emit(29, 0)
        self.assertFalse(self.wait(lambda: len(self.events) > 1, timeout=0.06))
        self.second.emit(29, 0)
        self.assertTrue(self.wait(lambda: self.events == ['press', 'release']))

    def test_startup_held_chord_does_not_start_recording(self):
        self.first.active = {29, 125}
        self.start()
        self.assertEqual(self.events, [])
        self.first.emit(125, 0)
        self.first.emit(125, 1)
        self.assertTrue(self.wait(lambda: self.events == ['press']))

    def test_missing_release_is_reconciled_from_device_state(self):
        self.start()
        self.first.emit(29, 1)
        self.first.emit(125, 1)
        self.assertTrue(self.wait(lambda: self.events == ['press']))
        self.first.active.clear()
        self.assertTrue(self.wait(lambda: self.events == ['press', 'release']))

    def test_removed_device_releases_chord_and_close_is_idempotent(self):
        self.start()
        self.first.emit(97, 1)
        self.first.emit(126, 1)
        self.assertTrue(self.wait(lambda: self.events == ['press']))
        self.backend.devices.pop(self.first.path)
        self.assertTrue(self.wait(lambda: self.events == ['press', 'release']))
        self.assertTrue(self.listener.close())
        self.assertTrue(self.listener.close())
        self.assertTrue(self.first.closed)
        self.assertTrue(self.second.closed)

    def test_close_releases_active_chord_once(self):
        self.start()
        self.first.emit(29, 1)
        self.first.emit(125, 1)
        self.assertTrue(self.wait(lambda: self.events == ['press']))
        self.assertTrue(self.listener.close())
        self.assertTrue(self.listener.close())
        self.assertEqual(self.events, ['press', 'release'])

    def test_syn_dropped_releases_then_requires_new_chord(self):
        self.start()
        self.first.emit(29, 1)
        self.first.emit(125, 1)
        self.assertTrue(self.wait(lambda: self.events == ['press']))
        self.first.emit(3, 0, kind=0)
        self.first.emit(0, 0, kind=0)
        self.assertTrue(self.wait(lambda: self.events == ['press', 'release']))
        self.first.emit(125, 2)
        self.assertFalse(self.wait(lambda: len(self.events) > 2, timeout=0.06))
        self.first.emit(125, 0)
        self.first.emit(125, 1)
        self.assertTrue(self.wait(lambda: self.events == ['press', 'release', 'press']))

    def test_permission_failure_is_visible_and_never_starts_recording(self):
        self.backend.denied = set(self.backend.devices)
        self.listener = self.module.HotkeyListener(lambda: self.record('press'), lambda: self.record('release'),
                                                  self.status, backend=self.backend, poll_interval=0.01)
        self.addCleanup(self.listener.close)
        self.listener.start()
        self.assertTrue(self.wait(lambda: any(s['state'] == 'permission-denied' for s in self.statuses)))
        self.assertEqual(self.events, [])

    def test_missing_evdev_dependency_is_visible_without_starting_thread(self):
        listener = self.module.HotkeyListener(lambda: self.record('press'), lambda: self.record('release'), self.status)
        with patch.dict('sys.modules', {'evdev': None}):
            self.assertFalse(listener.start())
        self.assertEqual(self.statuses[-1]['state'], 'unavailable')
        self.assertEqual(self.events, [])
        self.assertTrue(listener.close())


if __name__ == '__main__':
    unittest.main()
