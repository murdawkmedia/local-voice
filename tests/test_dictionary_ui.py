import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QMessageBox
from local_voice.dictionary_ui import DictionaryDialog
from local_voice.service import Service
from local_voice.settings import Settings
from local_voice.store import Store


class IdleController:
    def __init__(self, *args, **kwargs): pass
    def status(self): return {'state': 'idle', 'captureActive': False}
    def stop(self): pass
    def close(self, timeout=5): return True


class NoDesktop:
    def focus(self): return None


class NoPlayback:
    def stop(self): pass


class DictionaryEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state/sessions.db')
        self.store.import_dictionary([{'id': 'generated', 'word': 'GeneratedName'},
                                      {'id': 'markup', 'word': '<b>Generated</b>', 'replacement': '<i>Literal</i>'}])
        settings = Settings(self.root / 'config/settings.json')
        self.service = Service(self.store, settings, self.root / 'state', lambda: None,
            controller_factory=IdleController, output=NoDesktop(), ducker=NoPlayback(), background=False)
        self.addCleanup(self.service.close)
        self.service._ensure_command_worker()
        self.dialog = DictionaryDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)
        self.dialog.show()

    def until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate(): return True
            time.sleep(.005)
        return predicate()

    def test_native_editor_search_preserves_literal_text_and_never_records(self):
        self.assertTrue(self.until(lambda: self.dialog.table.rowCount() == 2))
        self.dialog.search_edit.setText('<b>')
        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.assertEqual(self.dialog.table.item(0, 0).text(), '<b>Generated</b>')
        self.dialog.table.selectRow(0)
        self.assertEqual(self.dialog.replacement_edit.toPlainText(), '<i>Literal</i>')

    def test_imported_edit_and_delete_use_worker_and_survive_reimport(self):
        self.assertTrue(self.until(lambda: self.dialog.table.rowCount() == 2))
        self.dialog.search_edit.setText('GeneratedName')
        self.dialog.table.selectRow(0)
        self.dialog.replacement_edit.setPlainText('Generated whole expansion')
        self.dialog.snippet_check.setChecked(True)
        threads = []
        real_save = self.store.save_dictionary_entry
        real_list = self.store.list_dictionary
        def save(**fields):
            threads.append(threading.get_ident()); return real_save(**fields)
        def listing(*args, **kwargs):
            threads.append(threading.get_ident()); return real_list(*args, **kwargs)
        with patch.object(self.store, 'save_dictionary_entry', save), patch.object(self.store, 'list_dictionary', listing):
            self.dialog.save_button.click()
            self.assertTrue(self.until(lambda: self.dialog.pending_operation is None))
        self.assertTrue(threads)
        self.assertTrue(all(thread != threading.get_ident() for thread in threads))
        self.assertEqual(self.store.personalization()['snippets'], {'GeneratedName': 'Generated whole expansion'})
        self.dialog.search_edit.setText('GeneratedName')
        self.dialog.table.selectRow(0)
        self.dialog.delete_button.click()
        self.assertTrue(self.until(lambda: self.dialog.pending_operation is None))
        self.store.import_dictionary([{'id': 'generated', 'word': 'GeneratedName'}])
        self.assertNotIn('GeneratedName', self.store.personalization()['words'])
        self.assertNotIn('GeneratedName', self.store.personalization()['snippets'])

    def test_rejected_queue_preserves_form_and_shows_generic_error(self):
        self.dialog.add_button.click()
        self.dialog.word_edit.setText('GeneratedUnsaved')
        with patch.object(self.service, 'save_dictionary_entry', return_value=None):
            self.dialog.save_button.click()
        self.assertEqual(self.dialog.word_edit.text(), 'GeneratedUnsaved')
        self.assertIn('not queued', self.dialog.message_label.text().lower())

    def test_add_after_selecting_imported_row_creates_separate_local_entry(self):
        self.assertTrue(self.until(lambda: self.dialog.table.rowCount() == 2))
        self.dialog.table.selectRow(0)
        self.dialog.add_button.click()
        self.dialog.word_edit.setText('GeneratedNewTrigger')
        self.dialog.replacement_edit.setPlainText('Generated new expansion')
        self.dialog.snippet_check.setChecked(True)
        self.dialog.starred_check.setChecked(True)
        self.dialog.save_button.click()
        self.assertTrue(self.until(lambda: self.dialog.pending_operation is None))
        rows = self.store.list_dictionary()
        self.assertEqual(len(rows), 3)
        row = next(row for row in rows if row['word'] == 'GeneratedNewTrigger')
        self.assertEqual((row['origin'], row['is_snippet'], row['is_starred']), ('local', 1, 1))
        self.assertEqual(self.store.list_sessions(), [])

    def test_cancel_navigation_and_add_preserves_draft_and_reselects_original_row(self):
        self.assertTrue(self.until(lambda: self.dialog.table.rowCount() == 2))
        self.dialog.table.selectRow(0)
        original = self.dialog.word_edit.text()
        self.dialog.replacement_edit.setPlainText('Generated long draft expansion')
        with patch('local_voice.dictionary_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Cancel) as prompt:
            self.dialog.table.selectRow(1)
            self.dialog.add_button.click()
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(self.dialog.word_edit.text(), original)
        self.assertEqual(self.dialog.table.currentRow(), 0)
        self.assertEqual(self.dialog.replacement_edit.toPlainText(), 'Generated long draft expansion')
        with patch('local_voice.dictionary_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Cancel):
            self.dialog.search_edit.setText('GeneratedName')
            self.dialog.table.selectRow(0)
        self.assertEqual(self.dialog.word_edit.text(), original)
        self.assertEqual(self.dialog.table.item(self.dialog.table.currentRow(), 0).text(), original)

    def test_save_on_close_waits_for_commit_ack_and_failure_retains_visible_draft(self):
        self.dialog.word_edit.setText('GeneratedCloseDraft')
        with patch('local_voice.dictionary_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Save), \
                patch.object(self.service, 'save_dictionary_entry', return_value=987):
            self.assertFalse(self.dialog.close())
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(self.dialog.pending_operation, 987)
        snapshot = self.service.dictionary_snapshot()
        snapshot['results'][987] = False
        snapshot['message'] = 'The entry was not saved. Retry.'
        with patch.object(self.service, 'dictionary_snapshot', return_value=snapshot):
            self.dialog.refresh()
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(self.dialog.word_edit.text(), 'GeneratedCloseDraft')
        with patch.object(self.service, 'save_dictionary_entry', return_value=988):
            self.dialog.save_button.click()
        snapshot['results'][988] = True
        with patch.object(self.service, 'dictionary_snapshot', return_value=snapshot):
            self.dialog.refresh()
        self.assertTrue(self.until(lambda: not self.dialog.isVisible()))

    def test_discard_on_add_is_explicit_and_does_not_save_draft(self):
        self.dialog.word_edit.setText('GeneratedDiscardedDraft')
        with patch('local_voice.dictionary_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Discard):
            self.dialog.add_button.click()
        self.assertEqual(self.dialog.word_edit.text(), '')
        self.assertNotIn('GeneratedDiscardedDraft', self.store.personalization()['words'])

    def test_save_before_row_navigation_waits_for_ack_then_selects_requested_identity(self):
        self.assertTrue(self.until(lambda: self.dialog.table.rowCount() == 2))
        self.dialog.table.selectRow(0)
        original = self.dialog.word_edit.text()
        target = self.dialog.table.item(1, 0).text()
        self.dialog.replacement_edit.setPlainText('Generated edited expansion')
        with patch('local_voice.dictionary_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Save), \
                patch.object(self.service, 'save_dictionary_entry', return_value=989):
            self.dialog.table.selectRow(1)
        self.assertEqual(self.dialog.word_edit.text(), original)
        snapshot = self.service.dictionary_snapshot(); snapshot['results'][989] = True
        with patch.object(self.service, 'dictionary_snapshot', return_value=snapshot):
            self.dialog.refresh()
            self.assertTrue(self.until(lambda: self.dialog.word_edit.text() == target))
        self.assertEqual(self.dialog.table.currentRow(), 1)


if __name__ == '__main__': unittest.main()
