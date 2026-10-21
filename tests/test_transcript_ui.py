import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import Mock, patch
from PyQt6.QtWidgets import QApplication, QMessageBox
from local_voice.transcript_ui import TranscriptDialog


class EditorService:
    def __init__(self):
        self.calls = []
        self.operation = 0
        self.data = {'sessionId': 'session-a', 'revision': 1, 'state': 'ready',
            'sessionState': 'complete', 'message': '', 'pending': 0, 'results': {}, 'segments': [
                {'id': 'segment-a', 'source': 'microphone', 'start': 0, 'end': 12,
                 'text': 'Generated first', 'recognized_text': 'Generated first', 'raw_text': '<b>Generated raw</b>'},
                {'id': 'segment-b', 'source': 'computer', 'start': 12, 'end': 24,
                 'text': 'Generated second', 'recognized_text': 'Generated second', 'raw_text': 'Generated raw second'}]}
    def request_transcript(self, sid): self.calls.append(('request', sid)); return True
    def transcript_snapshot(self): return copy.deepcopy(self.data)
    def edit_transcript(self, sid, edits):
        self.operation += 1; self.calls.append(('save', sid, edits)); return self.operation
    def ack(self, operation, success=True):
        self.data['results'][operation] = {'sessionId': 'session-a', 'success': success}
        self.data['message'] = 'Saved locally.' if success else 'The transcript was not saved. Retry.'


class TranscriptEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.service = EditorService()
        self.dialog = TranscriptDialog(self.service, 'session-a')
        self.addCleanup(self.dialog.deleteLater)
        self.dialog.show()
    def pump(self):
        self.app.processEvents(); self.app.processEvents()

    def test_segment_identity_and_raw_text_are_visible_without_saving(self):
        self.assertEqual(self.dialog.text_edit.toPlainText(), 'Generated first')
        self.assertEqual(self.dialog.raw_view.toPlainText(), '<b>Generated raw</b>')
        self.assertEqual(self.dialog._identity, 'segment-a')
        self.assertIn('Microphone', self.dialog.identity_label.text())
        self.assertFalse(any(call[0] == 'save' for call in self.service.calls))
        self.dialog.restore_button.click()
        self.assertEqual(self.dialog.text_edit.toPlainText(), '<b>Generated raw</b>')
        self.assertFalse(any(call[0] == 'save' for call in self.service.calls))

    def test_save_waits_for_commit_and_uses_original_session_segment_and_expected_text(self):
        self.dialog.text_edit.setPlainText('Generated correction')
        self.dialog.save_button.click()
        self.assertEqual(self.service.calls[-1], ('save', 'session-a', [
            {'id': 'segment-a', 'expected_text': 'Generated first', 'text': 'Generated correction'}]))
        self.assertFalse(self.dialog.text_edit.isEnabled())
        self.service.ack(1); self.dialog.refresh()
        self.assertIsNone(self.dialog.pending_operation)
        self.assertEqual(self.dialog.text_edit.toPlainText(), 'Generated correction')
        self.dialog.text_edit.setPlainText('Generated next correction'); self.dialog.save_button.click()
        self.assertEqual(self.service.calls[-1][2][0]['expected_text'], 'Generated correction')

    def test_cancel_navigation_preserves_draft_and_selection(self):
        self.dialog.text_edit.setPlainText('Generated draft')
        with patch('local_voice.transcript_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Cancel):
            self.dialog.segment_list.setCurrentRow(1)
        self.assertEqual(self.dialog.segment_list.currentRow(), 0)
        self.assertEqual(self.dialog.text_edit.toPlainText(), 'Generated draft')

    def test_save_navigation_waits_for_ack_and_wrong_session_ack_is_ignored(self):
        self.dialog.text_edit.setPlainText('Generated draft')
        with patch('local_voice.transcript_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Save):
            self.dialog.segment_list.setCurrentRow(1)
        self.assertEqual(self.dialog.segment_list.currentRow(), 0)
        self.service.data['results'][1] = {'sessionId': 'other-session', 'success': True}
        self.dialog.refresh(); self.assertEqual(self.dialog.pending_operation, 1)
        self.service.ack(1); self.dialog.refresh(); self.pump()
        self.assertEqual(self.dialog.segment_list.currentRow(), 1)
        self.assertEqual(self.dialog.text_edit.toPlainText(), 'Generated second')

    def test_rejection_and_failure_keep_draft_and_close_intent_for_successful_retry(self):
        self.dialog.text_edit.setPlainText('Generated unsaved draft')
        with patch('local_voice.transcript_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Save), \
                patch.object(self.service, 'edit_transcript', return_value=None):
            self.assertFalse(self.dialog.close())
        self.assertEqual(self.dialog.text_edit.toPlainText(), 'Generated unsaved draft')
        self.dialog.save_button.click(); self.service.ack(1, False); self.dialog.refresh()
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(self.dialog.text_edit.toPlainText(), 'Generated unsaved draft')
        self.dialog.save_button.click(); self.service.ack(2); self.dialog.refresh(); self.pump()
        self.assertFalse(self.dialog.isVisible())

    def test_escape_cancel_and_discard_do_not_save(self):
        self.dialog.text_edit.setPlainText('Generated draft')
        with patch('local_voice.transcript_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Cancel):
            self.dialog.reject()
        self.assertTrue(self.dialog.isVisible())
        with patch('local_voice.transcript_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Discard):
            self.dialog.reject()
        self.assertFalse(self.dialog.isVisible())
        self.assertFalse(any(call[0] == 'save' for call in self.service.calls))

    def test_app_quit_save_waits_for_ack_and_cancel_calls_parent(self):
        ready, cancelled = Mock(), Mock()
        self.dialog.text_edit.setPlainText('Generated draft')
        with patch('local_voice.transcript_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Cancel):
            self.assertFalse(self.dialog.prepare_app_close(ready, cancelled))
        cancelled.assert_called_once(); ready.assert_not_called()
        with patch('local_voice.transcript_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Save):
            self.assertFalse(self.dialog.prepare_app_close(ready, cancelled))
        ready.assert_not_called()
        self.service.ack(1); self.dialog.refresh(); self.pump(); ready.assert_called_once()

    def test_background_refresh_and_wrong_session_snapshot_preserve_current_draft(self):
        self.dialog.text_edit.setPlainText('Generated draft')
        self.service.data['revision'] += 1
        self.service.data['segments'][0]['text'] = 'Generated concurrent saved correction'
        self.dialog.refresh()
        self.assertEqual(self.dialog.text_edit.toPlainText(), 'Generated draft')
        self.service.data['sessionId'] = 'other-session'; self.service.data['revision'] += 1
        self.dialog.refresh()
        self.dialog.save_button.click()
        self.assertEqual(self.service.calls[-1][1], 'session-a')
        self.assertEqual(self.service.calls[-1][2][0]['expected_text'], 'Generated first')

    def test_native_editor_commits_through_real_worker_and_preserves_original_text(self):
        from local_voice.service import Service
        from local_voice.settings import Settings
        from local_voice.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / 'state/sessions.db')
            sid = store.create_session('meeting')
            store.add_segment(sid, 'computer', 0, 0, 12, 'Generated processed', raw_text='Generated raw')
            store.update_session(sid, state='complete')
            controller = Mock()
            controller.status.return_value = {'state': 'idle', 'captureActive': False}
            controller.close.return_value = True
            service = Service(store, Settings(root / 'config/settings.json'), root / 'state', lambda: None,
                controller_factory=lambda *args, **kwargs: controller, output=Mock(), ducker=Mock(), background=False)
            service._ensure_command_worker()
            dialog = TranscriptDialog(service, sid)
            io_threads = []
            get_session, edit_transcript = store.get_session, store.edit_transcript
            def get(*args):
                io_threads.append(threading.get_ident()); return get_session(*args)
            def edit(*args):
                io_threads.append(threading.get_ident()); return edit_transcript(*args)
            def until(predicate):
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    self.app.processEvents(); dialog.refresh()
                    if predicate(): return True
                    time.sleep(.005)
                return False
            try:
                with patch.object(store, 'get_session', get), patch.object(store, 'edit_transcript', edit):
                    self.assertTrue(until(lambda: dialog._identity is not None))
                    self.assertEqual(dialog.recognized_view.toPlainText(), 'Generated processed')
                    self.assertEqual(dialog.raw_view.toPlainText(), 'Generated raw')
                    dialog.text_edit.setPlainText('Generated corrected')
                    dialog.save_button.click()
                    self.assertTrue(until(lambda: dialog.pending_operation is None))
                segment = get_session(sid)['segments'][0]
                self.assertEqual((segment['text'], segment['recognized_text'], segment['raw_text']),
                                 ('Generated corrected', 'Generated processed', 'Generated raw'))
                self.assertTrue(io_threads)
                self.assertTrue(all(t != threading.get_ident() for t in io_threads))
                service.output.copy.assert_not_called()
                service.output.deliver.assert_not_called()
            finally:
                dialog.timer.stop(); dialog.deleteLater()
                service.close(timeout=2)


if __name__ == '__main__': unittest.main()
