"""Explicit transcript corrections over cached worker state; no database I/O."""

from PyQt6.QtCore import Qt, QSignalBlocker, QTimer
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget,
                            QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)


class TranscriptDialog(QDialog):
    def __init__(self, service, session_id, parent=None):
        super().__init__(parent)
        self.service, self.session_id = service, session_id
        self.setWindowTitle('Edit transcript')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(850, 720)
        self.pending_operation = None
        self._submitted = None
        self._rows = []
        self._identity = None
        self._baseline = ''
        self._revision = -1
        self._session_state = None
        self._dirty = False
        self._after_save = self._app_ready = self._app_cancel = None
        layout = QVBoxLayout(self)
        label = QLabel('Corrections keep the original recognition and audio. Save each segment explicitly. '
                       'Existing summaries become out of date; edits never paste or copy automatically.')
        label.setWordWrap(True)
        layout.addWidget(label)
        self.segment_list = QListWidget()
        self.segment_list.setAccessibleName('Transcript segments by source and time')
        self.segment_list.currentRowChanged.connect(self._selected)
        layout.addWidget(self.segment_list, 1)
        self.identity_label = QLabel()
        self.identity_label.setTextFormat(Qt.TextFormat.PlainText)
        self.identity_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.identity_label)
        layout.addWidget(QLabel('Current transcript text'))
        self.text_edit = QPlainTextEdit()
        self.text_edit.setAccessibleName('Edited transcript segment')
        self.text_edit.textChanged.connect(self._edited)
        layout.addWidget(self.text_edit, 2)
        layout.addWidget(QLabel('Original processed recognition (before manual edits)'))
        self.recognized_view = QPlainTextEdit()
        self.recognized_view.setReadOnly(True)
        self.recognized_view.setMaximumHeight(90)
        layout.addWidget(self.recognized_view)
        layout.addWidget(QLabel('Raw recognition (before filler cleanup, dictionary replacements and snippets)'))
        self.raw_view = QPlainTextEdit()
        self.raw_view.setReadOnly(True)
        self.raw_view.setMaximumHeight(90)
        layout.addWidget(self.raw_view)
        self.message_label = QLabel('Loading saved transcript…')
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)
        actions = QHBoxLayout()
        self.save_button = QPushButton('Save segment')
        self.restore_button = QPushButton('Use raw recognition')
        self.refresh_button = QPushButton('Refresh')
        self.close_button = QPushButton('Close')
        for button, callback in ((self.save_button, self._save), (self.restore_button, self._restore),
                                 (self.refresh_button, self._request_refresh), (self.close_button, self.close)):
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.service.request_transcript(session_id)
        self.refresh()

    def _edited(self):
        self._dirty = self.text_edit.toPlainText() != self._baseline

    def _busy(self):
        ready = (self.pending_operation is None and self._identity is not None
                 and self._session_state in {'complete', 'failed', 'interrupted'})
        for widget in (self.text_edit, self.save_button, self.restore_button):
            widget.setEnabled(ready)
        self.segment_list.setEnabled(self.pending_operation is None)
        self.refresh_button.setEnabled(self.pending_operation is None)

    def _render(self):
        with QSignalBlocker(self.segment_list):
            self.segment_list.clear()
            for row in self._rows:
                source = {'computer': 'Computer', 'microphone': 'Microphone', 'legacy': 'Imported'}.get(row['source'], 'Source')
                start, end = int(row.get('start', 0)), int(row.get('end', 0))
                self.segment_list.addItem(f'{start // 60:02}:{start % 60:02}–{end // 60:02}:{end % 60:02} · {source}')
            self._restore_selection()

    def _restore_selection(self):
        with QSignalBlocker(self.segment_list):
            index = next((i for i, row in enumerate(self._rows) if row['id'] == self._identity), -1)
            self.segment_list.setCurrentRow(index)

    def _load(self, identity):
        row = next((row for row in self._rows if row['id'] == identity), None)
        if row is None:
            return
        self._identity, self._baseline = identity, row['text']
        with QSignalBlocker(self.text_edit):
            self.text_edit.setPlainText(row['text'])
        self.recognized_view.setPlainText(row.get('recognized_text', row['text']))
        self.raw_view.setPlainText(row.get('raw_text', row['text']))
        source = {'computer': 'Computer', 'microphone': 'Microphone', 'legacy': 'Imported'}.get(row['source'], 'Source')
        start = int(row.get('start', 0))
        self.identity_label.setText(f'{source} · {start // 60:02}:{start % 60:02}')
        self._dirty = False
        self._restore_selection()
        self._busy()

    def _selected(self, index):
        if self.pending_operation is not None or not 0 <= index < len(self._rows):
            return
        identity = self._rows[index]['id']
        if identity == self._identity:
            return
        action = lambda: self._load(identity)
        if self._allow_leave(action):
            self._complete_leave(action)
        else:
            self._restore_selection()

    def _restore(self):
        self.text_edit.setPlainText(self.raw_view.toPlainText())

    def _request_refresh(self):
        action = lambda: self.service.request_transcript(self.session_id)
        if self._allow_leave(action):
            self._complete_leave(action)

    def _allow_leave(self, action):
        if self.pending_operation is not None:
            self._after_save = action
            return False
        if not self._dirty:
            return True
        choice = QMessageBox.question(self, 'Unsaved transcript edit', 'Save this segment before leaving it?',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if choice == QMessageBox.StandardButton.Discard:
            self._load(self._identity)
            return True
        if choice == QMessageBox.StandardButton.Save:
            self._after_save = action
            self._save()
        else:
            cancelled = self._app_cancel
            self._after_save = self._app_ready = self._app_cancel = None
            self._restore_selection()
            if cancelled:
                cancelled()
        return False

    def _complete_leave(self, action):
        action = self._app_ready or action
        self._after_save = self._app_ready = self._app_cancel = None
        action()

    def prepare_app_close(self, ready, cancelled):
        self._app_ready, self._app_cancel = ready, cancelled
        if self._dirty or self.pending_operation is not None:
            self.show()
            self.raise_()
            self.activateWindow()
        if self._allow_leave(ready):
            self._after_save = self._app_ready = self._app_cancel = None
            return True
        return False

    def closeEvent(self, event):
        if self._allow_leave(self.close):
            ready = self._app_ready
            self._after_save = self._app_ready = self._app_cancel = None
            event.accept()
            if ready:
                QTimer.singleShot(0, ready)
        else:
            event.ignore()

    def reject(self):
        self.close()

    def _save(self):
        if self.pending_operation is not None or self._identity is None:
            return
        edit = {'id': self._identity, 'expected_text': self._baseline, 'text': self.text_edit.toPlainText()}
        operation = self.service.edit_transcript(self.session_id, [edit])
        if operation is None or operation is False:
            self.message_label.setText('The edit was not queued. Check text limits or wait for pending work, then retry.')
            return
        self._submitted = edit
        self.pending_operation = operation
        self.message_label.setText('Saving locally…')
        self._busy()

    def refresh(self):
        snapshot = self.service.transcript_snapshot()
        same_session = snapshot['sessionId'] == self.session_id
        if same_session and snapshot['revision'] != self._revision:
            self._revision = snapshot['revision']
            self._rows = snapshot['segments']
            self._session_state = snapshot['sessionState']
            self._render()
            if not self._dirty and self.pending_operation is None and self._rows:
                self._load(self._identity or self._rows[0]['id'])
        result = snapshot['results'].get(self.pending_operation) if self.pending_operation is not None else None
        if result is not None and result['sessionId'] == self.session_id:
            self.pending_operation = None
            if result['success']:
                edit = self._submitted
                for row in self._rows:
                    if row['id'] == edit['id']:
                        row['text'] = edit['text']
                self._baseline = edit['text']
                self._dirty = False
                action = self._app_ready or self._after_save
                self._after_save = self._app_ready = self._app_cancel = None
                if action:
                    QTimer.singleShot(0, action)
            self.message_label.setText(snapshot['message'] if same_session else
                                       'Saved locally.' if result['success'] else 'The transcript was not saved. Retry.')
        elif same_session and self.pending_operation is None and not self._dirty:
            self.message_label.setText(snapshot['message'] or 'Select a segment to edit. Changes stay on this laptop.')
        self._busy()
