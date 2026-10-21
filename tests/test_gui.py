import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import patch
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import Mock
from PyQt6.QtWidgets import QApplication
from local_voice.gui import MainWindow


class FakeService:
    def __init__(self):
        self.calls = []
        self.data = {'status': {'state': 'idle', 'message': 'Ready', 'capture': {}},
                     'sessions': [], 'selected': None, 'settings': {}, 'devices': {},
                     'notice': ''}
    def snapshot(self): return self.data
    def start(self, kind, **kw): self.calls.append(('start', kind, kw))
    def stop(self): self.calls.append(('stop',))
    def select_session(self, sid): self.calls.append(('select', sid))
    def save_session(self, sid, **kw): self.calls.append(('save', sid, kw)); return True
    def configure(self, **kw): self.calls.append(('configure', kw))
    def search(self, value): self.calls.append(('search', value))
    def copy_session(self, sid): self.calls.append(('copy', sid))
    def export_session(self, *args): self.calls.append(('export', *args))
    def retry(self, sid): self.calls.append(('retry', sid))
    def summarize(self, sid): self.calls.append(('summarize', sid))


class NativeWindowTests(unittest.TestCase):
    def test_window_close_hides_to_tray_without_starting_shutdown(self):
        self.window.close_to_tray = lambda: True
        self.window.show()
        self.window.close()
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window._closing_requested)
        self.assertFalse(self.window.shutdown_edits_accepted)
        self.assertFalse(any(c[0] == 'stop' for c in self.service.calls))

    def test_explicit_quit_still_closes_when_close_to_tray_is_available(self):
        self.window.close_to_tray = lambda: True
        self.window.show()
        deadline = time.monotonic() + 2
        closed = self.window.request_close()
        while not closed and time.monotonic() < deadline:
            self.app.processEvents()
            closed = self.window.request_close()
        self.assertTrue(closed)
        self.assertTrue(self.window._closing_requested)

    def test_silent_ready_notification_setting_refresh_and_toggle(self):
        self.assertIn('readyNotification', self.window.checks)
        self.service.data['settings']['readyNotification'] = True
        self.window.refresh()
        self.assertTrue(self.window.checks['readyNotification'].isChecked())
        self.assertFalse(any(call[0] == 'configure' for call in self.service.calls))
        self.window.checks['readyNotification'].click()
        self.assertIn(('configure', {'readyNotification': False}), self.service.calls)

    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = FakeService()
        self.service.state_dir = Path(self.tmp.name)
        self.window = MainWindow(self.service)
        self.addCleanup(self.window.deleteLater)

    def test_window_construction_never_starts_recording(self):
        self.assertFalse(any(c[0] == 'start' for c in self.service.calls))

    def test_cleanup_setting_refresh_and_explicit_toggle(self):
        self.assertIn('cleanFillers', self.window.checks)
        self.service.data['settings']['cleanFillers'] = True
        self.window.refresh()
        self.assertTrue(self.window.checks['cleanFillers'].isChecked())
        self.assertFalse(any(call[0] == 'configure' for call in self.service.calls))
        self.window.checks['cleanFillers'].click()
        self.assertIn(('configure', {'cleanFillers': False}), self.service.calls)

    def test_edit_transcript_is_explicit_for_finished_session_and_stale_summary_is_labelled(self):
        self.load_meeting()
        self.assertFalse(self.window.edit_transcript_button.isEnabled())
        self.service.data['selected'].update(state='complete', updated=3,
            summary={'stale': True, 'items': [{'text': 'Generated old summary'}]})
        self.window.refresh()
        self.assertTrue(self.window.edit_transcript_button.isEnabled())
        self.assertIn('transcript was edited', self.window.summary_view.toPlainText())
        with patch('local_voice.gui.TranscriptDialog') as dialog:
            self.window.edit_transcript_button.click()
        dialog.assert_called_once_with(self.service, 'generated-id', self.window)
        self.assertFalse(any(call[0] in {'copy', 'start'} for call in self.service.calls))

    def test_app_close_checks_both_editors_and_suppresses_repeated_pending_prompt(self):
        dictionary, transcript = Mock(), Mock()
        dictionary.prepare_app_close.return_value = True
        transcript.prepare_app_close.return_value = False
        self.window._dictionary_dialog = dictionary
        self.window._transcript_dialog = transcript
        self.assertFalse(self.window.request_close())
        self.assertFalse(self.window.request_close())
        dictionary.prepare_app_close.assert_called_once()
        transcript.prepare_app_close.assert_called_once()
        transcript.prepare_app_close.call_args.args[1]()
        self.assertTrue(self.window.close_cancelled_by_user)
        self.assertFalse(self.window._closing_requested)

    def test_computer_audio_has_explicit_follow_default_choice(self):
        self.service.data['devices'] = {'sources': [
            {'name': 'generated.monitor', 'description': 'Generated output', 'monitor': True}]}
        self.service.data['settings'] = {'computerSource': ''}
        self.window.refresh()
        combo = self.window.computer_combo
        self.assertEqual(combo.currentText(), 'Follow default output')
        self.assertEqual(combo.currentData(), '')
        self.service.data['settings']['computerSource'] = 'generated.monitor'
        self.window.refresh()
        self.assertEqual(combo.currentData(), 'generated.monitor')
        combo.setCurrentIndex(combo.findData(''))
        combo.activated.emit(combo.currentIndex())
        self.assertIn(('configure', {'computerSource': ''}), self.service.calls)
        self.assertFalse(any(call[0] == 'start' for call in self.service.calls))

    def test_app_close_consults_hidden_dictionary_draft_and_cancel_retains_window(self):
        dialog = Mock()
        dialog.prepare_app_close.return_value = False
        self.window._dictionary_dialog = dialog
        self.assertFalse(self.window.close())
        dialog.prepare_app_close.assert_called_once()
        cancelled = dialog.prepare_app_close.call_args.args[1]
        cancelled()
        self.assertTrue(self.window.close_cancelled_by_user)
        self.assertFalse(self.window._closing_requested)

    def test_settings_dictionary_button_opens_native_editor_without_starting_capture(self):
        with patch('local_voice.gui.DictionaryDialog') as dialog:
            self.window.dictionary_button.click()
            self.window.dictionary_button.click()
        dialog.assert_called_once_with(self.service, self.window)
        dialog.return_value.show.assert_called()
        self.assertFalse(any(call[0] == 'start' for call in self.service.calls))

    def test_imported_naive_source_date_is_labelled_without_timezone_conversion(self):
        self.service.data['sessions'] = [{'id': 'generated-source-time', 'kind': 'meeting',
            'title': 'Generated historical meeting', 'created': 0, 'state': 'complete', 'updated': 1,
            'summary': {'sourceCreatedAt': '2025-03-04T05:06:07', 'sourceTimezoneUnspecified': True}}]
        self.window.refresh()
        label = self.window.session_list.item(0).text()
        self.assertIn('2025-03-04 · 05:06', label)
        self.assertIn('source time', label)

    def test_explicit_buttons_start_separate_modes_and_stop(self):
        self.window.dictate_button.click()
        self.window.meeting_button.click()
        self.service.data['status']['state'] = 'recording'
        self.window.refresh()
        self.window.stop_button.click()
        self.assertEqual([c[:2] for c in self.service.calls if c[0] == 'start'],
                         [('start', 'dictation'), ('start', 'meeting')])
        self.assertIn(('stop',), self.service.calls)

    def load_meeting(self):
        self.service.data['selected'] = {'id': 'generated-id', 'kind': 'meeting',
            'title': 'Generated title', 'notes': 'Typed note', 'state': 'recording',
            'summary': {}, 'segments': [{'id': 's1', 'source': 'computer', 'start': 0,
                                        'end': 12, 'text': 'Generated live sentence.'}],
            'transcript': 'Generated live sentence.', 'updated': 1}
        self.window.refresh()

    def test_live_text_title_and_notes_visible_before_stop(self):
        self.load_meeting()
        self.assertEqual(self.window.title_edit.text(), 'Generated title')
        self.assertEqual(self.window.notes_edit.toPlainText(), 'Typed note')
        self.assertIn('Generated live sentence.', self.window.transcript_view.toPlainText())
        self.assertIn('Computer', self.window.transcript_view.toPlainText())

    def test_title_and_typed_notes_save_to_selected_session(self):
        self.load_meeting()
        self.window.title_edit.setText('Renamed')
        self.window.notes_edit.setPlainText('More notes')
        self.window.save_edits()
        self.assertIn(('save', 'generated-id', {'title': 'Renamed', 'notes': 'More notes'}),
                      self.service.calls)

    def test_removed_audio_disables_retry_but_keeps_summary_and_transcript(self):
        self.load_meeting()
        self.service.data['selected'].update(state='complete', updated=2, audio_state='removed')
        self.window.refresh()
        self.assertFalse(self.window.retry_button.isEnabled())
        self.assertTrue(self.window.summary_button.isEnabled())
        self.assertIn('audio removed', self.window.session_state.text().lower())
        self.service.data['selected']['audio_state'] = 'pending'
        self.window.refresh()
        self.assertFalse(self.window.retry_button.isEnabled())
        self.assertIn('cleanup pending', self.window.session_state.text().lower())

    def test_summary_is_explicit_and_retry_targets_saved_session(self):
        self.load_meeting()
        self.assertFalse(any(c[0] == 'summarize' for c in self.service.calls))
        self.service.data['selected'].update(state='complete', updated=2)
        self.window.refresh()
        self.window.summary_button.click()
        self.window.retry_button.click()
        self.assertIn(('summarize', 'generated-id'), self.service.calls)
        self.assertIn(('retry', 'generated-id'), self.service.calls)

    def test_search_uses_transcript_search_service(self):
        self.window.search_edit.setText('words from the transcript')
        self.assertIn(('search', 'words from the transcript'), self.service.calls)

    def test_dictation_copy_stays_bound_to_displayed_dictation(self):
        self.load_meeting()
        self.service.data['selected'] = dict(self.service.data['selected'], id='dictation-a', kind='dictation')
        self.window.refresh()
        self.service.data['selected'] = dict(self.service.data['selected'], id='meeting-b', kind='meeting')
        self.window.refresh()
        self.window.dictation_copy_button.click()
        self.assertIn(('copy', 'dictation-a'), self.service.calls)
        self.assertNotIn(('copy', 'meeting-b'), self.service.calls)

    def test_unacknowledged_edit_survives_live_transcript_refresh_and_blur(self):
        self.load_meeting()
        self.window.notes_edit.setPlainText('New unsaved notes')
        self.window.save_edits()
        self.window.notes_edit.clearFocus()
        self.service.data['selected']['updated'] = 2
        self.window.refresh()
        self.assertEqual(self.window.notes_edit.toPlainText(), 'New unsaved notes')

    def pump_until(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(.005)
        return predicate()

    def test_close_flushes_every_dirty_session_and_journals_rejected_private_edits(self):
        self.load_meeting()
        def save(sid, **fields):
            self.service.calls.append(('save', sid, fields))
            return sid != 'generated-id'
        self.service.save_session = save
        self.window.notes_edit.setPlainText('Generated rejected A note')
        self.window.save_edits()
        self.service.data['selected'] = dict(self.service.data['selected'], id='session-b',
                                             title='Generated B', notes='Original B', updated=2)
        self.window.refresh()
        self.window.notes_edit.setPlainText('Generated B note')
        self.window.save_edits()
        self.service.calls.clear()
        self.window.close()
        self.assertTrue(any(call[:2] == ('save', 'generated-id') for call in self.service.calls))
        journal = self.service.state_dir / 'gui-edits-recovery.json'
        self.assertTrue(self.pump_until(journal.exists))
        payload = json.loads(journal.read_text())
        self.assertEqual(payload['sessions']['generated-id']['notes'], 'Generated rejected A note')
        self.assertEqual(payload['sessions']['session-b']['notes'], 'Generated B note')
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)

    def test_private_recovery_journal_requeues_edits_without_starting_recording(self):
        self.load_meeting()
        journal = self.service.state_dir / 'gui-edits-recovery.json'
        journal.write_text(json.dumps({'schemaVersion': 1, 'sessions': {
            'generated-id': {'title': 'Recovered generated title', 'notes': 'Recovered generated note'}}}))
        journal.chmod(0o600)
        recovered = MainWindow(self.service)
        self.addCleanup(recovered.deleteLater)
        self.assertTrue(self.pump_until(lambda: any(
            call[:2] == ('save', 'generated-id') and call[2]['notes'] == 'Recovered generated note'
            for call in self.service.calls)))
        recovered.refresh()
        self.assertEqual(recovered.notes_edit.toPlainText(), 'Recovered generated note')
        self.assertFalse(any(call[0] == 'start' for call in self.service.calls))

    def test_recovery_write_failure_keeps_window_open_and_dirty_fields_in_memory(self):
        self.load_meeting()
        self.window.notes_edit.setPlainText('Generated pending note')
        with patch('local_voice.gui._write_recovery', side_effect=OSError('private synthetic diagnostic')):
            self.assertFalse(self.window.close())
            self.assertTrue(self.pump_until(lambda: self.window._journal_error))
        self.assertIn('generated-id', self.window._local_edits)
        self.assertFalse((self.service.state_dir / 'gui-edits-recovery.json').exists())
        self.assertNotIn('private synthetic diagnostic', self.window.notice_label.text())


if __name__ == '__main__': unittest.main()
