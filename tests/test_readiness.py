import importlib
import importlib.util
import threading
import time
import unittest


def state(sid='generated', *, ready=True, frames=3840, active=True,
          phase='recording', kind='dictation'):
    return {'sessionId': sid, 'kind': kind, 'state': phase, 'captureActive': active,
            'capture': {'tracks': {'microphone': {'ready': ready, 'frames': frames}}}}


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('local_voice.readiness'),
                             'Actual-frame readiness cue is missing')
        self.module = importlib.import_module('local_voice.readiness')
        self.calls = []
        self.called = threading.Event()

    def notifier(self, notify=None):
        def record():
            self.calls.append(threading.get_ident())
            self.called.set()
        cue = self.module.ReadyNotifier(notify or record)
        self.addCleanup(cue.close)
        return cue

    def test_requires_actual_frames_in_active_dictation_then_notifies_once(self):
        cue = self.notifier()
        cue.arm('generated')
        for value in (state(phase='preparing'), state(ready=False), state(frames=0),
                      state(kind='meeting'), state(active=False), state(sid='other')):
            cue.observe(value)
        self.assertFalse(self.called.wait(.03))
        # Re-arm because a stopped recording must never become eligible again.
        cue.arm('generated')
        cue.observe(state())
        self.assertTrue(self.called.wait(1))
        for _ in range(20): cue.observe(state())
        self.assertTrue(cue.close())
        self.assertEqual(len(self.calls), 1)
        self.assertNotEqual(self.calls[0], threading.get_ident())

    def test_notification_io_never_blocks_capture_and_pending_cue_is_canceled(self):
        entered = threading.Event(); release = threading.Event()
        self.addCleanup(release.set)
        def blocked():
            self.calls.append('sent'); entered.set(); release.wait(2)
        cue = self.notifier(blocked)
        cue.arm('first'); cue.observe(state('first'))
        self.assertTrue(entered.wait(1))
        start = time.monotonic()
        cue.arm('second'); cue.observe(state('second')); cue.cancel('second')
        self.assertLess(time.monotonic() - start, .1)
        # Stale callbacks cannot re-arm a released or superseded session.
        cue.observe(state('second')); cue.observe(state('first'))
        release.set()
        self.assertTrue(cue.close())
        self.assertEqual(self.calls, ['sent'])

    def test_opt_out_cancels_pending_and_later_sessions_can_notify(self):
        cue = self.notifier()
        cue.set_enabled(False)
        cue.arm('disabled'); cue.observe(state('disabled'))
        self.assertFalse(self.called.wait(.03))
        cue.cancel('disabled'); cue.set_enabled(True)
        cue.arm('enabled'); cue.observe(state('enabled'))
        self.assertTrue(self.called.wait(1))

    def test_stop_is_terminal_even_if_an_old_ready_snapshot_arrives_late(self):
        cue = self.notifier()
        cue.arm('generated'); cue.observe(state(phase='stopping'))
        cue.observe(state())
        self.assertFalse(self.called.wait(.03))

    def test_unavailable_desktop_does_not_kill_worker_or_retry_same_session(self):
        failed = threading.Event()
        def unavailable():
            self.calls.append('attempt'); failed.set(); raise OSError('generated')
        cue = self.notifier(unavailable)
        cue.arm('first'); cue.observe(state('first'))
        self.assertTrue(failed.wait(1)); failed.clear()
        cue.observe(state('first')); cue.arm('second'); cue.observe(state('second'))
        self.assertTrue(failed.wait(1))
        self.assertTrue(cue.close())
        self.assertEqual(len(self.calls), 2)

    def test_close_cancels_pending_and_reports_unfinished_notification(self):
        entered = threading.Event(); release = threading.Event()
        self.addCleanup(release.set)
        def blocked(): entered.set(); release.wait(2)
        cue = self.notifier(blocked)
        cue.arm('first'); cue.observe(state('first'))
        self.assertTrue(entered.wait(1))
        cue.arm('second'); cue.observe(state('second'))
        self.assertFalse(cue.close(timeout=.01))
        release.set()
        self.assertTrue(cue.close())
        cue.arm('third'); cue.observe(state('third'))
        self.assertFalse(self.called.is_set())

    def test_pending_slot_coalesces_to_latest_session_and_old_cancel_is_ignored(self):
        entered = threading.Event(); release = threading.Event(); finished = threading.Event()
        self.addCleanup(release.set)
        def notify():
            self.calls.append('sent')
            if len(self.calls) == 1: entered.set(); release.wait(2)
            else: finished.set()
        cue = self.notifier(notify)
        cue.arm('first'); cue.observe(state('first')); self.assertTrue(entered.wait(1))
        for index in range(1000):
            sid = f'generated-{index}'; cue.arm(sid); cue.observe(state(sid))
        cue.cancel('first'); cue.observe(state('first'))
        release.set()
        self.assertTrue(finished.wait(1)); self.assertTrue(cue.close())
        self.assertEqual(len(self.calls), 2)
