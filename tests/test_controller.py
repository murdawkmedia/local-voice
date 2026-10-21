"""Real durable Store plus synthetic capture and inference; no audio devices."""

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from local_voice.store import Store

try:
    from local_voice import controller
except ImportError:
    controller = None


def until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return predicate()


class SyntheticCapture:
    def __init__(self, directory, *, sources, **kwargs):
        self.directory = Path(directory)
        self.sources = sources
        self.frames = dict.fromkeys(sources, 0)
        self.changed = threading.Condition()
        self.stopped = False

    def start(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for source in self.sources:
            self.append(source, 0)

    def append(self, source, frames):
        with self.changed:
            self.frames[source] += frames
            path = self.directory / (source + ".wav")
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\x01\0" * self.frames[source])
            path.chmod(0o600)
            self.changed.notify_all()

    def wait_ready(self, timeout=3):
        with self.changed:
            self.changed.wait_for(lambda: all(self.frames.values()) or self.stopped, timeout)
            return all(self.frames.values()) and not self.stopped

    def status(self):
        with self.changed:
            return {"ready": all(self.frames.values()) and not self.stopped,
                    "errors": [], "finalized": self.stopped,
                    "tracks": {source: {"path": self.directory / (source + ".wav"),
                                        "frames": frames, "rate": 48000, "errors": [],
                                        "finalized": self.stopped}
                               for source, frames in self.frames.items()}}

    def stop(self, timeout=5):
        with self.changed:
            self.stopped = True
            self.changed.notify_all()
        return self.status()


class SyntheticEngine:
    def __init__(self):
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()
        self.failures = 0

    def transcribe(self, path, cancel):
        with wave.open(str(path), "rb") as audio:
            self.calls.append((Path(path), audio.getnframes()))
        self.started.set()
        while not self.gate.wait(.01):
            if cancel.is_set():
                raise RuntimeError("synthetic cancelled")
        if self.failures:
            self.failures -= 1
            raise RuntimeError("private diagnostics must not enter metadata")
        return "  raw marker  "


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state" / "history.db")
        self.engine = SyntheticEngine()
        self.captures = []
        self.controllers = []
        self.events = []
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.engine.gate.set()
        for value in self.controllers:
            value.close(timeout=3)

    def make(self, **kwargs):
        self.assertIsNotNone(controller, "The asynchronous durable controller is missing")
        if not hasattr(self, "capture_patch"):
            def factory(*args, **kwargs):
                value = SyntheticCapture(*args, **kwargs)
                self.captures.append(value)
                return value
            self.capture_patch = patch.object(controller, "PulseRecorder", factory)
            self.capture_patch.start()
            self.addCleanup(self.capture_patch.stop)
        sources = lambda kind: ({"microphone": "generated-mic", "computer": "generated-monitor"}
                                if kind == "meeting" else {"microphone": "generated-mic"})
        value = controller.Controller(self.store, self.root / "state", lambda: self.engine,
                                      kwargs.pop("source_provider", sources),
                                      event_callback=kwargs.pop('event_callback', self.events.append), **kwargs)
        self.controllers.append(value)
        return value

    def started_capture(self, value, kind="dictation"):
        sid = value.start(kind=kind, title="Synthetic title", notes="Synthetic notes")
        self.assertTrue(until(lambda: bool(self.captures)))
        return sid, self.captures[-1]

    def test_spoken_email_and_private_name_correction_save_original_and_purge_successful_audio(self):
        raw = 'Ackme Studio, at example dot com.'
        self.store.save_dictionary_entry(word='AckmeStudio', replacement='AcmeStudio')
        self.store.save_dictionary_entry(word='AcmeStudio')
        self.engine.transcribe = lambda path, cancel: raw
        value = self.make(cleanup_provider=lambda: False)
        sid, capture = self.started_capture(value)
        capture.append('microphone', 481)
        self.assertTrue(until(lambda: value.status()['state'] == 'recording'))
        value.stop()
        self.assertTrue(value.wait_idle(4))
        row = Store(self.root / 'state/history.db').get_session(sid)
        self.assertEqual((row['state'], row['audio_state']), ('complete', 'removed'))
        self.assertEqual(row['segments'][0]['text'], 'AcmeStudio@example.com.')
        self.assertEqual(row['segments'][0]['raw_text'], raw)
        self.assertEqual(list(capture.directory.rglob('*.wav')), [])

    def test_early_stop_is_published_while_source_setup_blocks_and_never_makes_tiny_job(self):
        entered = threading.Event()
        release = threading.Event()
        def sources(kind):
            entered.set()
            release.wait(timeout=3)
            return {"microphone": "generated"}
        value = self.make(source_provider=sources)
        sid = value.start()
        self.assertTrue(entered.wait(timeout=2))
        with self.assertRaises(controller.ControllerError):
            value.start()
        began = time.monotonic()
        value.stop()
        self.assertLess(time.monotonic() - began, .1)
        self.assertEqual(value.status()["state"], "stopping")
        release.set()
        self.assertTrue(value.wait_idle(3))
        self.assertEqual(self.captures, [])
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.store.get_session(sid)["state"], "interrupted")

    def test_stop_before_actual_capture_readiness_retains_zero_track_without_inference(self):
        value = self.make()
        sid, capture = self.started_capture(value)
        value.stop()
        self.assertTrue(value.wait_idle(3))
        self.assertEqual(self.engine.calls, [])
        self.assertTrue((capture.directory / "microphone.wav").exists())
        self.assertEqual(self.store.get_session(sid)["state"], "interrupted")

    def test_success_removes_audio_after_text_save_and_retry_preserves_success(self):
        value = self.make()
        sid, capture = self.started_capture(value)
        capture.append('microphone', 481)
        self.assertTrue(until(lambda: value.status()['state'] == 'recording'))
        self.engine.gate.clear()
        value.stop()
        self.assertTrue(self.engine.started.wait(2))
        self.assertTrue((capture.directory / 'microphone.wav').is_file())
        self.engine.gate.set()
        self.assertTrue(value.wait_idle(3))
        row = self.store.get_session(sid)
        self.assertEqual((row['state'], row['audio_state']), ('complete', 'removed'))
        self.assertEqual(list(capture.directory.rglob('*.wav')), [])
        with self.assertRaises(controller.ControllerError) as error:
            value.retry(sid)
        self.assertEqual(error.exception.category, 'audio-removed')
        self.assertEqual(self.store.get_session(sid)['state'], 'complete')

    def test_empty_recognition_keeps_audio_for_retry(self):
        value = self.make()
        sid, capture = self.started_capture(value)
        capture.append('microphone', 481)
        self.assertTrue(until(lambda: value.status()['state'] == 'recording'))
        with patch.object(self.engine, 'transcribe', return_value='   '):
            value.stop()
            self.assertTrue(value.wait_idle(3))
        self.assertEqual(self.store.get_session(sid)['audio_state'], 'retained')
        self.assertTrue((capture.directory / 'microphone.wav').is_file())

    def test_startup_worker_cleans_previous_success_and_close_joins_it(self):
        sid = self.store.create_session('dictation')
        self.store.add_segment(sid, 'microphone', 0, 0, 1, 'Generated success')
        self.store.update_session(sid, state='complete')
        directory = self.root / 'state/recordings' / sid
        directory.mkdir(parents=True)
        (directory / 'session.json').write_text(json.dumps(dict(
            schemaVersion=1, kind='dictation', rate=48000, chunkFrames=576000)))
        (directory / 'microphone.wav').write_bytes(b'generated')
        value = self.make()
        self.assertTrue(until(lambda: self.store.get_session(sid)['audio_state'] == 'removed'))
        self.assertTrue(value.close(timeout=2))
        self.assertFalse(value._cleanup_thread.is_alive())
        self.assertEqual(self.captures, [])

    def test_frames_arriving_after_stop_cannot_turn_preparing_into_tiny_dictation(self):
        value = self.make()
        entered = threading.Event()
        release = threading.Event()
        def late_ready(capture, timeout=3):
            entered.set()
            release.wait(timeout=3)
            return all(capture.frames.values())
        with patch.object(SyntheticCapture, "wait_ready", late_ready):
            sid, capture = self.started_capture(value)
            self.assertTrue(entered.wait(timeout=2))
            value.stop()
            capture.append("microphone", 149)
            release.set()
            self.assertTrue(value.wait_idle(3))
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.store.get_session(sid)["state"], "interrupted")
        self.assertEqual(capture.frames["microphone"], 149)

    def test_recovery_uses_complete_uncheckpointed_pcm_and_preserves_original_bytes(self):
        sid = self.store.create_session("dictation")
        self.store.update_session(sid, state="captured")
        directory = self.root / "state" / "recordings" / sid
        directory.mkdir(mode=0o700, parents=True)
        (directory / "session.json").write_text(json.dumps({
            "schemaVersion": 1, "kind": "dictation", "rate": 48000, "chunkFrames": 480}))
        raw = directory / "microphone.wav"
        with wave.open(str(raw), "wb") as audio:
            audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x01\0" * 151)
        with raw.open("r+b") as output:
            output.seek(4)
            output.write((38).to_bytes(4, "little"))
            output.seek(40)
            output.write((2).to_bytes(4, "little"))
        original = raw.read_bytes()
        value = self.make()
        self.engine.gate.clear()
        value.retry(sid)
        self.assertTrue(self.engine.started.wait(2))
        self.assertEqual(raw.read_bytes(), original)
        self.engine.gate.set()
        self.assertTrue(value.wait_idle(3))
        self.assertEqual(self.engine.calls[0][1], 151)
        self.assertFalse(raw.exists())
        self.assertEqual(self.store.get_session(sid)["state"], "complete")

    def test_retry_uses_saved_chunk_size_when_controller_settings_change(self):
        value = self.make(chunk_seconds=.01)
        sid, capture = self.started_capture(value, "meeting")
        capture.append("microphone", 480 + 17)
        capture.append("computer", 480 + 29)
        self.assertTrue(until(lambda: len(self.store.get_session(sid)["segments"]) == 2))
        self.engine.failures = 1  # Keep an unfinished tail for restart/retry.
        value.stop()
        self.assertTrue(value.wait_idle(3))
        self.assertEqual(self.store.get_session(sid)['state'], 'failed')
        segments = self.store.get_session(sid)["segments"]
        calls = len(self.engine.calls)
        value.close(timeout=2)
        restarted = self.make(chunk_seconds=.02)
        restarted.retry(sid)
        self.assertTrue(restarted.wait_idle(3))
        recovered = self.store.get_session(sid)
        identities = {s['id']: s for s in recovered['segments']}
        self.assertTrue(all(identities[s['id']] == s for s in segments))
        self.assertEqual(len(self.engine.calls), calls + 2)
        self.assertEqual(recovered['audio_state'], 'removed')

    def test_meeting_transcribes_live_unequal_tracks_then_only_the_independent_tails(self):
        value = self.make(chunk_seconds=.01)
        sid, capture = self.started_capture(value, "meeting")
        capture.append("microphone", 480 * 2 + 17)
        capture.append("computer", 480 + 29)
        self.assertTrue(until(lambda: len(self.store.get_session(sid)["segments"]) == 3))
        self.assertEqual(self.store.get_session(sid)["state"], "recording")
        value.stop()
        self.assertTrue(value.wait_idle(4))
        row = self.store.get_session(sid)
        self.assertEqual(row["state"], "complete")
        self.assertEqual((row["title"], row["notes"]), ("Synthetic title", "Synthetic notes"))
        segments = {(s["source"], s["ordinal"]): s for s in row["segments"]}
        self.assertEqual(set(segments), {("microphone", 0), ("microphone", 1),
                                         ("microphone", 2), ("computer", 0), ("computer", 1)})
        self.assertEqual(segments["microphone", 2]["end"], 977 / 48000)
        self.assertEqual(segments["computer", 1]["end"], 509 / 48000)
        self.assertEqual(sorted(frames for _, frames in self.engine.calls), [17, 29, 480, 480, 480])
        self.assertNotIn("raw marker", json.dumps(self.events, default=str))

    def test_engine_failure_retains_audio_retry_personalizes_and_restart_skips_existing_identity(self):
        self.engine.failures = 1
        profile = lambda: {"replacements": {"raw": "corrected"}}
        value = self.make(profile_provider=profile)
        sid, capture = self.started_capture(value)
        capture.append("microphone", 113)
        self.assertTrue(until(lambda: value.status()["state"] == "recording"))
        value.stop()
        self.assertTrue(value.wait_idle(4))
        self.assertEqual(self.store.get_session(sid)["state"], "failed")
        self.assertTrue((capture.directory / "microphone.wav").exists())
        self.assertNotIn("private diagnostics", json.dumps(value.status(), default=str))
        self.assertEqual(value.retry(sid), sid)
        self.assertTrue(value.wait_idle(4))
        row = self.store.get_session(sid)
        self.assertEqual(row["state"], "complete")
        self.assertEqual(row["segments"][0]["raw_text"], "  raw marker  ")
        self.assertEqual(row["segments"][0]["text"], "corrected marker")
        calls = len(self.engine.calls)
        value.close(timeout=3)
        restarted = self.make(profile_provider=profile)
        self.assertEqual(self.store.get_session(sid)["state"], "complete")
        with self.assertRaises(controller.ControllerError) as error:
            restarted.retry(sid)
        self.assertEqual(error.exception.category, 'audio-removed')
        self.assertTrue(restarted.wait_idle(4))
        self.assertEqual(len(self.engine.calls), calls)
        self.assertEqual(self.store.get_session(sid)["segments"], row["segments"])

    def test_dictation_cleanup_setting_preserves_meetings_and_raw_recognition(self):
        from local_voice.service import Service
        from local_voice.settings import Settings
        from unittest.mock import Mock
        raw = 'Um, please send uh the file.'
        settings = Settings(self.root / 'config/settings.json')
        output = Mock()
        output.focus.return_value = None
        output.deliver.return_value = {'copied': True, 'pasted': False}
        self.engine.transcribe = lambda path, cancel: raw
        def factory(store, state, engine, sources, **kwargs):
            return self.make(**kwargs)
        service = Service(self.store, settings, self.root / 'state', lambda: self.engine,
                          controller_factory=factory, output=output,
                          devices_fn=lambda: {'sources': [], 'sinks': []},
                          ducker=Mock(), background=False)
        self.addCleanup(service.close)
        value = service.controller
        for kind, enabled, language, expected in [('dictation', True, 'en', 'Please send the file.'),
                ('dictation', False, 'en', raw), ('meeting', True, 'en', raw),
                ('dictation', True, 'de', raw)]:
            settings.update(cleanFillers=enabled, language=language)
            sid = value.start(kind)
            self.assertTrue(until(lambda: len(self.captures) > 0 and self.captures[-1].directory.name == sid))
            capture = self.captures[-1]
            for source in capture.sources:
                capture.append(source, 113)
            self.assertTrue(until(lambda: value.status()['state'] == 'recording'))
            value.stop()
            self.assertTrue(value.wait_idle(4))
            row = self.store.get_session(sid)
            self.assertEqual(row['state'], 'complete')
            for segment in row['segments']:
                self.assertEqual(segment['text'], expected)
                self.assertEqual(segment['raw_text'], raw)

    def test_startup_recovers_all_inflight_states_without_recording_or_inference(self):
        ids = []
        for state in ("preparing", "recording", "captured", "transcribing"):
            sid = self.store.create_session("dictation")
            self.store.update_session(sid, state=state)
            ids.append(sid)
        value = self.make()
        self.assertTrue(value.wait_idle(.5))
        self.assertTrue(all(self.store.get_session(sid)["state"] == "interrupted" for sid in ids))
        self.assertEqual(self.captures, [])
        self.assertEqual(self.engine.calls, [])

    def test_bounded_queue_backlog_preserves_live_capture_and_finishes_all_chunks_after_stop(self):
        self.engine.gate.clear()
        value = self.make(chunk_seconds=.01)
        sid, capture = self.started_capture(value, "meeting")
        capture.append("microphone", 480 * 40 + 17)
        capture.append("computer", 23)
        self.assertTrue(self.engine.started.wait(timeout=3))
        self.assertTrue(until(lambda: value.status().get("backlog", False)))
        self.assertLessEqual(value.status()["queuedJobs"], 32)
        self.assertTrue(value.status()["captureActive"])
        self.assertEqual(value.status()["state"], "recording")
        self.assertFalse(capture.stopped)
        capture.append("microphone", 480 * 2)
        capture.append("computer", 480 + 31)
        value.stop()
        self.engine.gate.set()
        self.assertTrue(value.wait_idle(6))
        row = self.store.get_session(sid)
        self.assertEqual(row["state"], "complete")
        self.assertEqual(len(row["segments"]), 45)
        self.assertFalse(value.status()["captureActive"])

    def test_meeting_engine_failure_keeps_both_tracks_recording_until_stop_then_retry(self):
        self.engine.failures = 1
        value = self.make(chunk_seconds=.01)
        sid, capture = self.started_capture(value, "meeting")
        capture.append("microphone", 480 + 17)
        capture.append("computer", 29)
        self.assertTrue(until(lambda: value.status().get("previewError") == "engine"))
        self.assertEqual(value.status()["state"], "recording")
        self.assertTrue(value.status()["captureActive"])
        self.assertFalse(capture.stopped)
        capture.append("microphone", 480)
        capture.append("computer", 480)
        value.stop()
        self.assertTrue(value.wait_idle(4))
        self.assertEqual(self.store.get_session(sid)["state"], "failed")
        value.retry(sid)
        self.assertTrue(value.wait_idle(4))
        row = self.store.get_session(sid)
        self.assertEqual(row["state"], "complete")
        self.assertEqual(len(row["segments"]), 5)

    def test_a_new_capture_can_start_while_one_stopped_meeting_backfills_bounded_queue(self):
        self.engine.gate.clear()
        value = self.make(chunk_seconds=.01)
        old_sid, capture = self.started_capture(value, "meeting")
        capture.append("microphone", 480 * 40 + 17)
        capture.append("computer", 23)
        self.assertTrue(until(lambda: value.status().get("backlog", False)))
        value.stop()
        self.assertTrue(until(lambda: not value.status()["captureActive"]))
        try:
            new_sid = value.start()
        except controller.ControllerError as exc:
            self.fail("A stopped meeting blocked the next capture: " + exc.category)
        self.assertTrue(until(lambda: len(self.captures) == 2))
        self.captures[-1].append("microphone", 157)
        self.assertTrue(until(lambda: value.status()["state"] == "recording"))
        value.stop()
        self.engine.gate.set()
        self.assertTrue(value.wait_idle(6))
        self.assertEqual(self.store.get_session(old_sid)["state"], "complete")
        self.assertEqual(self.store.get_session(new_sid)["state"], "complete")

    def test_meeting_limit_automatically_stops_drains_and_completes_without_error(self):
        self.assertEqual(getattr(controller.Controller if controller else None,
                                 "MAX_MEETING_SECONDS", None), 7200)
        value = self.make(chunk_seconds=.01)
        value.MAX_MEETING_SECONDS = .03
        sid, capture = self.started_capture(value, "meeting")
        capture.append("microphone", 480 * 3 + 17)
        capture.append("computer", 480 + 23)
        self.assertTrue(value.wait_idle(4))
        row = self.store.get_session(sid)
        self.assertEqual((row["state"], row["error"]), ("complete", ""))
        self.assertTrue(value.status()["limitReached"])
        self.assertFalse(value.status()["captureActive"])
        self.assertEqual(len(row["segments"]), 6)

    def test_old_inference_completion_cannot_change_the_new_active_session(self):
        self.engine.gate.clear()
        value = self.make()
        old_sid, old_capture = self.started_capture(value)
        old_capture.append("microphone", 89)
        self.assertTrue(until(lambda: value.status()["state"] == "recording"))
        value.stop()
        self.assertTrue(self.engine.started.wait(timeout=3))
        new_sid = value.start()
        self.assertTrue(until(lambda: len(self.captures) == 2))
        new_capture = self.captures[-1]
        new_capture.append("microphone", 97)
        self.assertTrue(until(lambda: value.status()["state"] == "recording"))
        self.engine.gate.set()
        self.assertTrue(until(lambda: self.store.get_session(old_sid)["state"] == "complete"))
        self.assertEqual(value.status()["sessionId"], new_sid)
        self.assertEqual(value.status()["state"], "recording")
        self.assertEqual(self.store.get_session(new_sid)["segments"], [])
        value.stop()
        self.assertTrue(value.wait_idle(4))

    def test_meeting_without_both_sources_fails_without_starting_capture(self):
        value = self.make(source_provider=lambda kind: {"microphone": "generated"})
        sid = value.start(kind="meeting")
        self.assertTrue(value.wait_idle(3))
        self.assertEqual(self.store.get_session(sid)["state"], "failed")
        self.assertEqual(self.captures, [])

    def test_close_during_chunk_creation_cannot_enqueue_work_after_worker_exits(self):
        value = self.make()
        sid, capture = self.started_capture(value)
        capture.append("microphone", 139)
        self.assertTrue(until(lambda: value.status()["state"] == "recording"))
        entered = threading.Event()
        release = threading.Event()
        real_clip = value._clip
        def delayed_clip(*args, **kwargs):
            entered.set()
            release.wait(timeout=3)
            return real_clip(*args, **kwargs)
        with patch.object(value, "_clip", delayed_clip):
            value.stop()
            self.assertTrue(entered.wait(timeout=2))
            self.assertFalse(value.close(timeout=.05))
            release.set()
            self.assertTrue(value.wait_idle(3))
        self.assertTrue(value.close(timeout=1))
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.store.get_session(sid)["state"], "interrupted")

    def test_late_captured_state_write_cannot_erase_concurrent_engine_failure(self):
        self.engine.failures = 1
        self.engine.gate.clear()
        value = self.make(chunk_seconds=.01)
        sid, capture = self.started_capture(value, "meeting")
        capture.append("microphone", 480 + 17)
        capture.append("computer", 29)
        self.assertTrue(self.engine.started.wait(timeout=3))
        entered = threading.Event()
        release = threading.Event()
        real_update = self.store.update_session
        def delayed_captured(sid, **fields):
            if fields.get("state") == "captured":
                entered.set()
                release.wait(timeout=3)
            return real_update(sid, **fields)
        with patch.object(self.store, "update_session", delayed_captured):
            value.stop()
            self.assertTrue(entered.wait(timeout=2))
            self.engine.gate.set()
            self.assertTrue(until(lambda: value.status()["state"] == "failed"))
            release.set()
            self.assertTrue(value.wait_idle(4))
        row = self.store.get_session(sid)
        self.assertEqual((row["state"], row["error"]), ("failed", "engine"))

    def test_retry_is_rejected_until_same_session_final_state_write_finishes(self):
        value = self.make()
        sid, capture = self.started_capture(value)
        capture.append("microphone", 163)
        self.assertTrue(until(lambda: value.status()["state"] == "recording"))
        old_ctx = value._sessions[sid]
        entered = threading.Event()
        release = threading.Event()
        real_complete = value._complete
        def paused_complete(ctx):
            if ctx is old_ctx and ctx.producer_done and not ctx.pending:
                entered.set()
                release.wait(timeout=3)
            return real_complete(ctx)
        with patch.object(value, "_complete", paused_complete):
            try:
                value.stop()
                self.assertTrue(entered.wait(timeout=2))
                self.assertFalse(value.wait_idle(.01))
                with self.assertRaises(controller.ControllerError) as error:
                    value.retry(sid)
                self.assertEqual(error.exception.category, "active")
            finally:
                release.set()
                self.assertTrue(value.wait_idle(3))
        with self.assertRaises(controller.ControllerError) as error:
            value.retry(sid)
        self.assertEqual(error.exception.category, 'audio-removed')
        self.assertTrue(value.wait_idle(3))
        self.assertEqual(self.store.get_session(sid)["state"], "complete")

    def test_stale_completion_cannot_remove_new_attempt_from_shutdown_cancellation(self):
        self.engine.failures = 1
        value = self.make()
        sid, capture = self.started_capture(value)
        capture.append("microphone", 167)
        self.assertTrue(until(lambda: value.status()["state"] == "recording"))
        value.stop()
        self.assertTrue(value.wait_idle(3))
        old_ctx = value._sessions[sid]
        self.engine.started.clear()
        self.engine.gate.clear()
        value.retry(sid)
        self.assertTrue(self.engine.started.wait(timeout=2))
        new_ctx = value._sessions[sid]
        value._complete(old_ctx)  # an already completed attempt delivers a stale callback
        self.assertIs(value._sessions.get(sid), new_ctx)
        self.assertTrue(value.close(timeout=2))

    def test_preparing_meeting_limit_stops_unready_source_and_retains_the_other_track(self):
        value = self.make()
        value.MAX_MEETING_SECONDS = .03
        sid, capture = self.started_capture(value, "meeting")
        capture.append("microphone", 173)
        self.assertTrue(value.wait_idle(1), "An unready computer source recorded forever")
        row = self.store.get_session(sid)
        self.assertEqual((row["state"], row["error"]), ("interrupted", "not-ready"))
        self.assertTrue(value.status()["limitReached"])
        self.assertFalse(value.status()["captureActive"])
        self.assertTrue(capture.stopped)
        with wave.open(str(capture.directory / "microphone.wav"), "rb") as audio:
            self.assertEqual(audio.getnframes(), 173)
        self.assertEqual(self.engine.calls, [])


if __name__ == "__main__":
    unittest.main()
