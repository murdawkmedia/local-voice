from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from local_voice.service import Service
from local_voice.settings import Settings
from local_voice.store import Store


class FakeController:
    def __init__(self, store, *args, **kwargs):
        self.store = store
        self.event_callback = kwargs.get('event_callback')
        self.row = {'state': 'idle', 'sessionId': None, 'capture': {}, 'captureActive': False}
        self.starts = []
        self.stops = 0
    def start(self, kind='dictation', title='', notes=''):
        sid = self.store.create_session(kind, title=title, notes=notes)
        self.starts.append(sid)
        self.row = {'state': 'recording', 'sessionId': sid, 'kind': kind, 'capture': {}, 'captureActive': True}
        return sid
    def stop(self): self.stops += 1; self.row['captureActive'] = False; self.row['state'] = 'stopping'
    def status(self): return dict(self.row)
    def retry(self, sid): return sid
    def close(self, timeout=5): return True


class FakeOutput:
    def __init__(self): self.deliveries = [];self.copies = []
    def focus(self): return {'address': '0xgenerated', 'class': 'GeneratedEditor'}
    def notify_ready(self): self.ready.set()
    def copy(self, text): self.copies.append(text)
    def deliver(self, text, focus, auto_paste=True):
        self.deliveries.append((text, focus, auto_paste))
        return {'copied': True, 'pasted': auto_paste, 'reason': 'pasted' if auto_paste else 'copied'}


class FakeDucker:
    def __init__(self): self.starts = 0;self.stops = 0
    def start(self): self.starts += 1
    def stop(self): self.stops += 1


def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


class FakeHotkeys:
    def __init__(self, on_press, on_release, **kwargs):
        self.press = on_press
        self.release = on_release
    def start(self): return True
    def close(self): return True


class ServiceTests(unittest.TestCase):
    def test_controller_startup_failure_closes_new_notification_worker(self):
        from local_voice.readiness import ReadyNotifier
        created = []
        def tracked(*args, **kwargs):
            worker = ReadyNotifier(*args, **kwargs); created.append(worker); return worker
        with patch('local_voice.service.ReadyNotifier', side_effect=tracked):
            with self.assertRaises(RuntimeError):
                Service(self.store, self.settings, self.root/'state', lambda: None,
                        controller_factory=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError()),
                        output=self.output, ducker=self.ducker, background=False)
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0]._thread.is_alive())

    def test_release_during_start_cannot_arm_an_already_ready_recording(self):
        request = threading.Event()
        original = self.service.controller.start
        def released(*args, **kwargs):
            sid = original(*args, **kwargs)
            self.service.controller.row['capture'] = {'tracks': {'microphone': {'ready': True, 'frames': 3840}}}
            request.set()
            return sid
        with patch.object(self.service.controller, 'start', released), \
                patch.object(self.service._readiness, 'arm') as arm:
            self.service.start_sync('dictation', origin='hotkey', request=request)
        arm.assert_not_called()
        self.assertEqual(self.service.controller.stops, 1)

    def test_hotkey_actual_frames_notify_without_waiting_for_cached_status(self):
        self.output.ready = threading.Event()
        self.assertTrue(callable(self.service.controller.event_callback))
        self.service.start_sync('dictation', origin='hotkey')
        self.assertFalse(self.output.ready.wait(.02))
        row = self.service.controller.row
        row['capture'] = {'tracks': {'microphone': {'ready': True, 'frames': 3840}}}
        self.service.controller.event_callback(row)
        self.assertTrue(self.output.ready.wait(1))
        self.assertEqual(self.service.public_status()['state'], 'idle')

    def test_button_and_meeting_do_not_post_shortcut_ready_cues(self):
        self.output.ready = threading.Event()
        self.assertTrue(callable(self.service.controller.event_callback))
        for kind, origin in [('dictation', 'button'), ('meeting', 'hotkey')]:
            self.service.start_sync(kind, origin=origin)
            row = self.service.controller.row
            row['capture'] = {'tracks': {'microphone': {'ready': True, 'frames': 3840}}}
            self.service.controller.event_callback(row)
        self.assertFalse(self.output.ready.wait(.03))

    def test_setting_applies_on_worker_before_next_monitor_tick(self):
        self.output.ready = threading.Event()
        self.assertTrue(self.service.configure(readyNotification=False))
        self.worker()
        self.assertTrue(until(lambda: self.service._commands.unfinished_tasks == 0))
        self.assertFalse(self.settings.get()['readyNotification'])
        self.service.start_sync('dictation', origin='hotkey')
        row = self.service.controller.row
        row['capture'] = {'tracks': {'microphone': {'ready': True, 'frames': 3840}}}
        self.service.controller.event_callback(row)
        self.assertFalse(self.output.ready.wait(.03))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'state/sessions.db')
        self.settings = Settings(self.root/'config/settings.json')
        self.output = FakeOutput();self.ducker = FakeDucker()
        self.devices = {'sources':[{'name':'mic','monitor':False},{'name':'speaker.monitor','monitor':True}],
                        'sinks':[{'name':'speaker','monitor':'speaker.monitor'}],
                        'defaultSource':'mic','defaultSink':'speaker'}
        self.service = Service(self.store,self.settings,self.root/'state',lambda:None,
                               controller_factory=FakeController,output=self.output,
                               devices_fn=lambda:self.devices,ducker=self.ducker,background=False)
        self.addCleanup(self.service.close)

    def worker(self):
        thread = threading.Thread(target=self.service._command_loop, daemon=True)
        self.service._command_thread = thread
        self.service._threads.append(thread)
        thread.start()
        return thread

    def complete(self, kind='dictation'):
        sid = self.store.create_session(kind)
        self.store.add_segment(sid, 'microphone', 0, 0, 1, 'Generated text')
        self.store.update_session(sid, state='complete')
        return sid

    def test_starting_application_never_starts_capture_or_summary(self):
        self.service.tick()
        self.assertEqual(self.service.controller.starts, [])
        self.assertEqual(self.output.deliveries, [])

    def test_transcript_edit_is_worker_owned_durable_private_and_acknowledged(self):
        sid = self.complete('meeting')
        segment = self.store.get_session(sid)['segments'][0]
        self.assertTrue(self.service.request_transcript(sid))
        self.worker()
        self.assertTrue(until(lambda: bool(self.service.transcript_snapshot()['segments'])))
        entered = threading.Event(); release = threading.Event(); threads = []
        original = self.store.edit_transcript
        def delayed(*args):
            threads.append(threading.get_ident()); entered.set(); release.wait(timeout=2)
            return original(*args)
        with patch.object(self.store, 'edit_transcript', delayed):
            operation = self.service.edit_transcript(sid, [{'id': segment['id'],
                'expected_text': segment['text'], 'text': 'Generated private correction'}])
            self.assertIsInstance(operation, int)
            self.assertTrue(entered.wait(timeout=2))
            self.assertNotIn(operation, self.service.transcript_snapshot()['results'])
            self.assertNotIn('Generated private correction', str(self.service.public_status()))
            release.set()
            self.assertTrue(until(lambda: operation in self.service.transcript_snapshot()['results']))
        self.assertEqual(self.service.transcript_snapshot()['results'][operation], {'sessionId': sid, 'success': True})
        self.assertTrue(all(t != threading.get_ident() for t in threads))
        self.assertEqual(self.store.get_session(sid)['transcript'], 'Generated private correction')
        self.assertEqual(self.output.copies, [])
        self.assertEqual(self.output.deliveries, [])

    def test_transcript_edits_are_bounded_and_close_drains_accepted_work(self):
        sid = self.complete()
        segment = self.store.get_session(sid)['segments'][0]
        for _ in range(32):
            self.assertIsInstance(self.service.edit_transcript(sid, [{'id': segment['id'],
                'expected_text': segment['text'], 'text': segment['text']}]), int)
        self.assertIsNone(self.service.edit_transcript(sid, [{'id': segment['id'],
            'expected_text': segment['text'], 'text': segment['text']}]))
        self.assertTrue(self.service.close(timeout=2))
        self.assertEqual(self.service.transcript_snapshot()['pending'], 0)
        self.assertTrue(all(result['success'] for result in self.service.transcript_snapshot()['results'].values()))

    def test_transcript_commit_remains_success_when_refresh_fails(self):
        sid = self.complete()
        segment = self.store.get_session(sid)['segments'][0]
        self.service.request_transcript(sid)
        self.worker()
        self.assertTrue(until(lambda: bool(self.service.transcript_snapshot()['segments'])))
        with patch.object(self.store, 'get_session', side_effect=OSError('Generated private diagnostic')):
            operation = self.service.edit_transcript(sid, [{'id': segment['id'],
                'expected_text': segment['text'], 'text': 'Generated committed edit'}])
            self.assertTrue(until(lambda: operation in self.service.transcript_snapshot()['results']))
        snapshot = self.service.transcript_snapshot()
        self.assertTrue(snapshot['results'][operation]['success'])
        self.assertIn('Saved locally', snapshot['message'])
        self.assertIn('Refresh', snapshot['message'])
        self.assertNotIn('private diagnostic', snapshot['message'])
        self.assertEqual(self.store.get_session(sid)['transcript'], 'Generated committed edit')

    def test_transcript_cache_does_not_publish_other_session_after_delayed_read(self):
        first, second = self.complete(), self.complete()
        entered = threading.Event(); release = threading.Event()
        original = self.store.get_session
        def delayed(sid):
            if sid == first:
                entered.set(); release.wait(timeout=2)
            return original(sid)
        with patch.object(self.store, 'get_session', delayed):
            self.service.request_transcript(first); self.worker()
            self.assertTrue(entered.wait(timeout=2))
            self.service.request_transcript(second)
            release.set()
            self.assertTrue(until(lambda: self.service.transcript_snapshot()['state'] == 'ready'))
        snapshot = self.service.transcript_snapshot()
        self.assertEqual(snapshot['sessionId'], second)
        self.assertTrue(all(row['session_id'] == second for row in snapshot['segments']))

    def test_summary_from_old_effective_text_cannot_replace_retained_summary(self):
        sid = self.complete('meeting')
        segment = self.store.get_session(sid)['segments'][0]
        self.store.update_session(sid, summary={'items': [{'text': 'Generated retained summary',
                                                        'segment_ids': [segment['id']]}]})
        entered = threading.Event(); release = threading.Event()
        class Summarizer:
            def summarize(inner, segments, cancel):
                entered.set(); release.wait(timeout=2)
                return {'items': [{'text': 'Generated obsolete summary', 'segment_ids': [segment['id']]}]}
        self.service.summarizer = Summarizer()
        self.assertTrue(self.service.summarize(sid))
        self.assertTrue(entered.wait(timeout=2))
        self.store.edit_transcript(sid, [{'id': segment['id'], 'expected_text': segment['text'],
                                        'text': 'Generated correction during summary'}])
        release.set()
        self.assertTrue(until(lambda: self.service._summaries.unfinished_tasks == 0))
        summary = self.store.get_session(sid)['summary']
        self.assertEqual(summary['items'][0]['text'], 'Generated retained summary')
        self.assertTrue(summary['stale'])

    def test_human_readable_exports_warn_when_retained_summary_is_stale(self):
        sid = self.complete('meeting')
        segment = self.store.get_session(sid)['segments'][0]
        self.store.update_session(sid, summary={'items': [
            {'text': 'Generated older summary', 'segment_ids': [segment['id']]}]})
        self.store.edit_transcript(sid, [{'id': segment['id'], 'expected_text': segment['text'],
                                        'text': 'Generated corrected transcript'}])
        for fmt in ('md', 'txt'):
            target = self.root / ('generated-export.' + fmt)
            self.service.export_sync(sid, target, fmt)
            text = target.read_text()
            self.assertIn('Generated corrected transcript', text)
            self.assertIn('Generated older summary', text)
            self.assertIn('out of date', text)

    def test_transcript_edit_conflict_is_acknowledged_without_private_diagnostics(self):
        sid = self.complete()
        segment = self.store.get_session(sid)['segments'][0]
        self.service.request_transcript(sid); self.worker()
        self.assertTrue(until(lambda: bool(self.service.transcript_snapshot()['segments'])))
        self.store.edit_transcript(sid, [{'id': segment['id'], 'expected_text': segment['text'],
                                        'text': 'Generated intervening edit'}])
        operation = self.service.edit_transcript(sid, [{'id': segment['id'],
            'expected_text': segment['text'], 'text': 'Generated rejected private draft'}])
        self.assertTrue(until(lambda: operation in self.service.transcript_snapshot()['results']))
        snapshot = self.service.transcript_snapshot()
        self.assertFalse(snapshot['results'][operation]['success'])
        self.assertNotIn('Generated', snapshot['message'])
        self.assertEqual(self.store.get_session(sid)['transcript'], 'Generated intervening edit')
        self.assertTrue(self.service.close(timeout=2))

    def test_dictionary_commands_are_private_async_durable_and_acknowledged(self):
        entered = threading.Event(); release = threading.Event()
        real_save = self.store.save_dictionary_entry
        threads = []
        def delayed(**fields):
            threads.append(threading.current_thread().name)
            entered.set(); release.wait(timeout=3)
            return real_save(**fields)
        with patch.object(self.store, 'save_dictionary_entry', delayed):
            self.worker()
            began = time.monotonic()
            operation = self.service.save_dictionary_entry(word='GeneratedPrivateWord', replacement='Generated expansion')
            self.assertLess(time.monotonic() - began, .1)
            self.assertIsInstance(operation, int)
            self.assertTrue(entered.wait(timeout=2))
            self.assertTrue(self.service.dictionary_snapshot()['pending'])
            self.assertNotIn('GeneratedPrivateWord', str(self.service.public_status()))
            release.set()
            self.assertTrue(until(lambda: self.service.dictionary_snapshot()['completed'] >= operation))
        rows = self.service.dictionary_snapshot()['rows']
        self.assertEqual(rows[0]['word'], 'GeneratedPrivateWord')
        self.assertNotIn(threading.current_thread().name, threads)
        operation = self.service.delete_dictionary_entry(rows[0]['origin'], rows[0]['id'])
        self.assertTrue(self.service.close(timeout=2))
        self.assertEqual(self.store.list_dictionary(), [])
        self.assertGreaterEqual(self.service.dictionary_snapshot()['completed'], operation)

    def test_close_drains_accepted_dictionary_save_without_starting_capture(self):
        operation = self.service.save_dictionary_entry(word='GeneratedQueuedWord')
        self.assertIsInstance(operation, int)
        self.assertTrue(self.service.close(timeout=2))
        self.assertEqual(self.store.personalization()['words'], ['GeneratedQueuedWord'])
        self.assertEqual(self.service.controller.starts, [])

    def test_public_version_uses_package_version(self):
        from local_voice import __version__
        self.assertEqual(self.service.public_status()['version'], __version__)

    def test_dictionary_pending_work_is_bounded_and_close_drains_accepted_changes(self):
        for ordinal in range(32):
            self.assertIsInstance(self.service.save_dictionary_entry(word=f'Generated{ordinal}'), int)
        self.assertIsNone(self.service.save_dictionary_entry(word='GeneratedOverflow'))
        self.assertEqual(self.service.dictionary_snapshot()['pending'], 32)
        self.assertTrue(self.service.close(timeout=3))
        self.assertEqual(len(self.store.list_dictionary()), 32)

    def test_dictionary_io_failure_is_generic_and_shutdown_cannot_claim_clean(self):
        with patch.object(self.store, 'save_dictionary_entry', side_effect=OSError('Generated private exception detail')):
            operation = self.service.save_dictionary_entry(word='GeneratedPrivateWord')
            self.assertFalse(self.service.close(timeout=2))
        snapshot = self.service.dictionary_snapshot()
        self.assertFalse(snapshot['results'][operation])
        self.assertNotIn('Generated private exception detail', snapshot['message'])
        self.assertEqual(self.store.list_dictionary(), [])

    def test_dictionary_commit_ack_survives_refresh_failure_with_honest_message(self):
        self.store.save_dictionary_entry(word='GeneratedCachedWord')
        self.assertTrue(self.service._refresh_dictionary())
        cached = self.service.dictionary_snapshot()['rows']
        with patch.object(self.store, 'list_dictionary', side_effect=OSError('Generated private refresh detail')):
            operation = self.service.save_dictionary_entry(word='GeneratedCommittedWord')
            self.assertTrue(self.service.close(timeout=2))
        snapshot = self.service.dictionary_snapshot()
        self.assertTrue(snapshot['results'][operation])
        self.assertEqual(snapshot['rows'], cached)
        self.assertEqual(snapshot['message'], 'Saved locally. Refresh is unavailable; retry Refresh.')
        self.assertEqual(set(self.store.personalization()['words']),
                         {'GeneratedCachedWord', 'GeneratedCommittedWord'})

    def test_source_selection_resolves_monitor_without_changing_defaults(self):
        self.service.refresh_devices()
        self.assertEqual(self.service.sources('meeting'), {'microphone':'mic','computer':'speaker.monitor'})
        self.assertEqual(self.devices['defaultSink'],'speaker')

    def test_refresh_keeps_follow_default_choice_and_tracks_new_cached_output(self):
        self.service.refresh_devices()
        self.assertEqual(self.settings.get()['computerSource'], '')
        self.assertEqual(self.settings.get()['microphoneSource'], 'mic')
        self.assertEqual(self.service.sources('meeting')['computer'], 'speaker.monitor')
        self.devices = {'sources': self.devices['sources'] + [
            {'name': 'next.monitor', 'monitor': True}, {'name': 'next-mic', 'monitor': False}],
            'sinks': self.devices['sinks'] + [{'name': 'next', 'monitor': 'next.monitor'}],
            'defaultSource': 'next-mic', 'defaultSink': 'next'}
        self.service.refresh_devices()
        self.assertEqual(self.settings.get()['computerSource'], '')
        self.assertEqual(self.service.sources('meeting'), {'microphone': 'mic', 'computer': 'next.monitor'})
        self.settings.update(computerSource='speaker.monitor')
        self.service.refresh_devices()
        self.assertEqual(self.settings.get()['computerSource'], 'speaker.monitor')
        self.assertEqual(self.service.sources('meeting')['computer'], 'speaker.monitor')

    def test_completed_dictation_delivered_once_and_meeting_never_auto_copied(self):
        self.service.refresh_devices()
        sid=self.service.start_sync('dictation', origin='hotkey')
        self.store.add_segment(sid,'microphone',0,0,1,'Generated dictation')
        self.store.update_session(sid,state='complete')
        self.service.controller.row.update(state='complete',captureActive=False)
        self.service.tick();self.service.tick()
        self.assertEqual(len(self.output.deliveries),1)
        sid2=self.service.start_sync('meeting')
        self.store.add_segment(sid2,'computer',0,0,1,'Generated meeting')
        self.store.update_session(sid2,state='complete')
        self.service.controller.row.update(state='complete',captureActive=False)
        self.service.tick()
        self.assertEqual(len(self.output.deliveries),1)

    def test_old_session_completion_cannot_paste_into_a_new_capture(self):
        self.service.refresh_devices()
        old=self.service.start_sync('dictation',origin='hotkey')
        current=self.service.start_sync('dictation',origin='hotkey')
        self.store.add_segment(old,'microphone',0,0,1,'Stale generated text')
        self.store.update_session(old,state='complete')
        self.service.tick()
        self.assertEqual(self.output.deliveries,[])
        self.assertEqual(self.service.snapshot()['status']['sessionId'],current)

    def test_delivery_rechecks_attempt_after_blocked_store_read_and_new_capture(self):
        old = self.service.start_sync('dictation', origin='hotkey')
        self.store.add_segment(old, 'microphone', 0, 0, 1, 'Stale generated text')
        self.store.update_session(old, state='complete')
        self.service.controller.row.update(state='complete', captureActive=False)
        entered = threading.Event()
        release = threading.Event()
        real_get = self.store.get_session
        def delayed_get(sid):
            if sid == old and not entered.is_set():
                entered.set()
                release.wait(timeout=3)
            return real_get(sid)
        with patch.object(self.store, 'get_session', delayed_get):
            thread = threading.Thread(target=self.service.tick)
            thread.start()
            self.assertTrue(entered.wait(timeout=2))
            current = self.service.start_sync('dictation', origin='hotkey')
            release.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.output.deliveries, [])
        self.assertEqual(self.service.snapshot()['status']['sessionId'], current)
        self.service.tick()
        self.assertEqual(self.service.snapshot()['status']['sessionId'], current)

    def test_start_and_stop_return_immediately_while_an_existing_delivery_finishes(self):
        sid = self.service.start_sync('dictation', origin='hotkey')
        self.store.add_segment(sid, 'microphone', 0, 0, 1, 'Generated delivery')
        self.store.update_session(sid, state='complete')
        self.service.controller.row.update(state='complete', captureActive=False)
        entered = threading.Event()
        release = threading.Event()
        real_deliver = self.output.deliver
        def delayed_delivery(*args, **kwargs):
            entered.set()
            release.wait(timeout=3)
            return real_deliver(*args, **kwargs)
        with patch.object(self.output, 'deliver', delayed_delivery):
            thread = threading.Thread(target=self.service.tick)
            thread.start()
            self.assertTrue(entered.wait(timeout=2))
            self.worker()
            began = time.monotonic()
            self.assertTrue(self.service.start('dictation'))
            self.service.stop()
            self.assertLess(time.monotonic() - began, .1)
            release.set()
            thread.join(timeout=2)
            self.assertTrue(until(lambda: self.service._commands.unfinished_tasks == 0))
        self.assertEqual(self.service.controller.starts, [sid])

    def test_hotkey_release_after_rejected_press_cannot_stop_button_meeting(self):
        sid = self.service.start_sync('meeting')
        real_start = self.service.controller.start
        def reject_active(*args, **kwargs):
            if self.service.controller.row['captureActive']:
                raise RuntimeError('synthetic capture already active')
            return real_start(*args, **kwargs)
        with patch('local_voice.service.HotkeyListener', FakeHotkeys), patch.object(
                self.service.controller, 'start', reject_active):
            self.service._configure_hotkeys(True)
            self.service._hotkeys.press()
            self.worker()
            self.assertTrue(until(lambda: self.service._commands.unfinished_tasks == 0))
            self.service._hotkeys.release()
        self.assertEqual(self.service.controller.row['sessionId'], sid)
        self.assertTrue(self.service.controller.row['captureActive'])
        self.assertEqual(self.service.controller.stops, 0)

    def test_hotkey_release_cancels_only_its_pending_start(self):
        with patch('local_voice.service.HotkeyListener', FakeHotkeys):
            self.service._configure_hotkeys(True)
            self.service._hotkeys.press()
            self.service._hotkeys.release()
            self.worker()
            self.assertTrue(until(lambda: self.service._commands.unfinished_tasks == 0))
        self.assertEqual(self.service.controller.starts, [])
        self.assertEqual(self.service.controller.stops, 0)

    def test_hotkey_release_cannot_stop_a_newer_button_capture(self):
        with patch('local_voice.service.HotkeyListener', FakeHotkeys):
            self.service._configure_hotkeys(True)
            self.service._hotkeys.press()
            self.worker()
            self.assertTrue(until(lambda: bool(self.service.controller.starts)))
            current = self.service.start_sync('meeting')
            self.service._hotkeys.release()
        self.assertEqual(self.service.controller.row['sessionId'], current)
        self.assertTrue(self.service.controller.row['captureActive'])
        self.assertEqual(self.service.controller.stops, 0)

    def test_close_drains_accepted_edits_and_settings_but_skips_queued_capture_and_copy(self):
        sid = self.complete()
        self.assertTrue(self.service.save_session(sid, title='Latest generated title', notes='Latest generated notes'))
        self.assertTrue(self.service.configure(language='fr'))
        self.assertTrue(self.service.start('dictation'))
        self.assertTrue(self.service.copy_session(sid))
        self.assertTrue(self.service.close(timeout=2))
        row = self.store.get_session(sid)
        self.assertEqual((row['title'], row['notes']), ('Latest generated title', 'Latest generated notes'))
        self.assertEqual(self.settings.get()['language'], 'fr')
        self.assertEqual(self.service.controller.starts, [])
        self.assertEqual(self.output.copies, [])

    def test_pending_edits_overlay_stale_refresh_and_only_latest_committed_revision_clears(self):
        sid = self.complete()
        self.service.select_session(sid)
        self.service.tick()
        self.service.save_session(sid, notes='First generated edit')
        self.assertEqual(self.service.snapshot()['selected']['notes'], 'First generated edit')
        self.assertEqual(self.service.snapshot()['pendingEdits'][sid]['notes'], 'First generated edit')
        entered = threading.Event()
        release = threading.Event()
        real_update = self.store.update_session
        def delayed_update(sid, **fields):
            if fields.get('notes') == 'First generated edit':
                entered.set()
                release.wait(timeout=3)
            return real_update(sid, **fields)
        with patch.object(self.store, 'update_session', delayed_update):
            self.worker()
            self.assertTrue(entered.wait(timeout=2))
            self.service.save_session(sid, notes='Latest generated edit')
            self.service.tick()
            self.assertEqual(self.service.snapshot()['selected']['notes'], 'Latest generated edit')
            release.set()
            self.assertTrue(until(lambda: not self.service.snapshot()['pendingEdits']))
        self.assertEqual(self.store.get_session(sid)['notes'], 'Latest generated edit')
        self.assertEqual(self.service.snapshot()['selected']['notes'], 'Latest generated edit')

    def test_close_timeout_stays_false_until_accepted_durable_write_finishes(self):
        sid = self.complete()
        entered = threading.Event()
        release = threading.Event()
        real_update = self.store.update_session
        def delayed_update(sid, **fields):
            entered.set()
            release.wait(timeout=3)
            return real_update(sid, **fields)
        with patch.object(self.store, 'update_session', delayed_update):
            self.service.save_session(sid, notes='Retained generated edit')
            self.worker()
            self.assertTrue(entered.wait(timeout=2))
            try:
                self.assertFalse(self.service.close(timeout=.03))
                self.assertFalse(self.service.close(timeout=.03))
            finally:
                release.set()
        self.assertTrue(self.service.close(timeout=2))
        self.assertEqual(self.store.get_session(sid)['notes'], 'Retained generated edit')

    def test_idle_startup_recovers_owned_playback_before_ready(self):
        self.assertGreaterEqual(self.ducker.stops, 1)

    def test_background_startup_returns_fast_and_never_reports_idle_before_recovery(self):
        entered = threading.Event()
        release = threading.Event()
        class RecoveryDucker(FakeDucker):
            def stop(inner):
                entered.set()
                release.wait(timeout=3)
                super().stop()
        ducker = RecoveryDucker()
        began = time.monotonic()
        service = Service(self.store, self.settings, self.root / 'state', lambda: None,
                          controller_factory=FakeController, output=self.output,
                          devices_fn=lambda: self.devices, ducker=ducker, background=True)
        self.addCleanup(service.close)
        try:
            self.assertLess(time.monotonic() - began, .15)
            self.assertTrue(entered.wait(timeout=2))
            self.assertEqual(service.snapshot()['status']['state'], 'recovering')
            self.assertFalse(service.snapshot()['startupReady'])
            service.start('dictation')
            self.assertEqual(service.controller.starts, [])
        finally:
            release.set()
        self.assertTrue(until(lambda: bool(service.controller.starts)))
        self.assertTrue(service.snapshot()['startupReady'])

    def test_late_store_refresh_cannot_erase_an_already_committed_edit_overlay(self):
        sid = self.complete()
        self.service.select_session(sid)
        self.service.tick()
        entered = threading.Event()
        release = threading.Event()
        real_get = self.store.get_session
        def old_row_then_pause(requested):
            row = real_get(requested)
            if requested == sid and not entered.is_set():
                entered.set()
                release.wait(timeout=3)
            return row
        with patch.object(self.store, 'get_session', old_row_then_pause):
            thread = threading.Thread(target=self.service.tick)
            thread.start()
            self.assertTrue(entered.wait(timeout=2))
            self.worker()
            self.service.save_session(sid, notes='Latest committed generated note')
            self.assertTrue(until(lambda: not self.service.snapshot()['pendingEdits']))
            release.set()
            thread.join(timeout=2)
        self.assertEqual(self.service.snapshot()['selected']['notes'], 'Latest committed generated note')
        self.service.tick()
        self.assertEqual(self.service.snapshot()['selected']['notes'], 'Latest committed generated note')

    def test_summary_worker_does_not_block_capture_or_edits_and_has_one_waiting_slot(self):
        sid = self.complete('meeting')
        second = self.complete('meeting')
        third = self.complete('meeting')
        entered = threading.Event()
        release = threading.Event()
        class Summarizer:
            def summarize(inner, segments, cancel):
                entered.set()
                while not release.wait(.01):
                    if cancel.is_set():
                        raise RuntimeError('synthetic cancellation')
                return {'items': [{'text': 'Generated summary', 'segment_ids': [segments[0]['id']]}]}
        self.service.summarizer = Summarizer()
        self.worker()
        try:
            self.assertTrue(self.service.summarize(sid))
            self.assertTrue(entered.wait(timeout=2))
            self.assertFalse(self.service.model_lock.acquire(blocking=False))
            self.service.save_session(sid, notes='Saved while summary runs')
            self.service.start('dictation')
            self.assertTrue(until(lambda: bool(self.service.controller.starts)),
                            'A summary blocked the recording command queue')
            self.assertTrue(until(lambda: self.store.get_session(sid)['notes'] == 'Saved while summary runs'))
            self.assertTrue(self.service.summarize(second))
            self.assertFalse(self.service.summarize(third))
        finally:
            release.set()

    def test_export_retains_title_notes_sources_and_validated_summary(self):
        sid=self.store.create_session('meeting',title='Generated meeting',notes='Generated notes')
        seg=self.store.add_segment(sid,'computer',0,0,12,'Generated sentence')
        self.store.update_session(sid,state='complete',summary={'items':[{'text':'Generated summary','segment_ids':[seg]}]})
        destination=self.root/'generated.md'
        self.service.export_sync(sid,destination,'md')
        value=destination.read_text()
        for term in ['Generated meeting','Generated notes','computer','Generated sentence','Generated summary']:
            self.assertIn(term,value)


if __name__=='__main__':unittest.main()
