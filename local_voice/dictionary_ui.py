"""Native private dictionary editor over cached worker state; no database I/O."""

from PyQt6.QtCore import Qt, QSignalBlocker, QTimer
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout)


class DictionaryDialog(QDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle('Personal dictionary')
        self.resize(900, 650)
        self.pending_operation = None
        self._identity = None
        self._rows = []
        self._visible = []
        self._revision = -1
        self._dirty = False
        self._after_save = None
        self._app_ready = None
        self._app_cancel = None
        layout = QVBoxLayout(self)
        explanation = QLabel('Words without a replacement become private Whisper hints. Replacements correct whole tokens. '
                             'Snippets expand only when the complete dictation matches the trigger.')
        explanation.setTextFormat(Qt.TextFormat.PlainText)
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('Search words and replacements')
        self.search_edit.setMaxLength(512)
        self.search_edit.textChanged.connect(self._render)
        layout.addWidget(self.search_edit)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Word / trigger', 'Replacement', 'Type', 'Source', 'Status'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selected)
        layout.addWidget(self.table, 1)
        form = QFormLayout()
        self.word_edit = QLineEdit()
        self.word_edit.setMaxLength(512)
        self.replacement_edit = QPlainTextEdit()
        self.replacement_edit.setMaximumHeight(110)
        self.snippet_check = QCheckBox('Expand as a whole-dictation snippet')
        self.starred_check = QCheckBox('Favourite')
        form.addRow('Word / trigger', self.word_edit)
        form.addRow('Replacement / expansion', self.replacement_edit)
        form.addRow('', self.snippet_check)
        form.addRow('', self.starred_check)
        layout.addLayout(form)
        for widget in (self.word_edit, self.replacement_edit):
            widget.textChanged.connect(self._edited)
        self.snippet_check.toggled.connect(self._edited)
        self.starred_check.toggled.connect(self._edited)
        self.message_label = QLabel('Loading local dictionary…')
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)
        actions = QHBoxLayout()
        self.add_button = QPushButton('Add entry')
        self.save_button = QPushButton('Save entry')
        self.delete_button = QPushButton('Delete entry')
        self.refresh_button = QPushButton('Refresh')
        self.close_button = QPushButton('Close')
        for button, callback in ((self.add_button, self._new), (self.save_button, self._save),
                                 (self.delete_button, self._delete), (self.refresh_button, service.request_dictionary),
                                 (self.close_button, self.close)):
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self._busy(False)
        service.request_dictionary()
        self.refresh()

    def _edited(self, *args):
        self._dirty = True

    def _busy(self, busy):
        for widget in (self.add_button, self.save_button, self.table, self.word_edit,
                       self.replacement_edit, self.snippet_check, self.starred_check):
            widget.setEnabled(not busy)
        self.delete_button.setEnabled(not busy and self._identity is not None)

    def _new(self):
        if self._allow_leave(self._clear_form):
            self._complete_leave(self._clear_form)

    def _clear_form(self):
        self._identity = None
        with QSignalBlocker(self.table):
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
        for widget, value in ((self.word_edit, ''), (self.replacement_edit, '')):
            with QSignalBlocker(widget):
                if widget is self.word_edit: widget.setText(value)
                else: widget.setPlainText(value)
        for check in (self.snippet_check, self.starred_check):
            with QSignalBlocker(check): check.setChecked(False)
        self._dirty = False
        self._busy(self.pending_operation is not None)

    def _render(self, *args):
        query = self.search_edit.text().casefold()
        self._visible = sorted((row for row in self._rows if query in row['word'].casefold()
                                or query in row['replacement'].casefold()),
                               key=lambda row: (row['word'].casefold(), row['origin'], row['id']))
        with QSignalBlocker(self.table):
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
            self.table.setRowCount(len(self._visible))
            selected = None
            for index, row in enumerate(self._visible):
                kind = 'Snippet' if row['is_snippet'] else 'Replacement' if row['replacement'] else 'Word hint'
                status = ('Duplicate · used' if row['effective'] else 'Duplicate · inactive') if row['duplicate'] else 'Active'
                values = (row['word'], row['replacement'].replace('\n', ' ↵ ')[:120], kind,
                          'Local edit' if row['locally_edited'] else row['origin'], status)
                for column, value in enumerate(values):
                    self.table.setItem(index, column, QTableWidgetItem(value))
                if (row['origin'], row['id']) == self._identity:
                    selected = index
            if selected is not None:
                self.table.selectRow(selected)

    def _selected(self):
        index = self.table.currentRow()
        if self.pending_operation is not None or not 0 <= index < len(self._visible):
            return
        row = self._visible[index]
        if (row['origin'], row['id']) == self._identity:
            return
        action = lambda: self._load_row(row)
        if self._allow_leave(action):
            self._complete_leave(action)
        else:
            self._restore_selection()

    def _load_row(self, row):
        self._identity = (row['origin'], row['id'])
        with QSignalBlocker(self.word_edit): self.word_edit.setText(row['word'])
        with QSignalBlocker(self.replacement_edit): self.replacement_edit.setPlainText(row['replacement'])
        with QSignalBlocker(self.snippet_check): self.snippet_check.setChecked(bool(row['is_snippet']))
        with QSignalBlocker(self.starred_check): self.starred_check.setChecked(bool(row['is_starred']))
        self._dirty = False
        self._restore_selection()
        self._busy(False)

    def _restore_selection(self):
        if (self._identity is not None
                and not any((row['origin'], row['id']) == self._identity for row in self._visible)
                and any((row['origin'], row['id']) == self._identity for row in self._rows)):
            with QSignalBlocker(self.search_edit):
                self.search_edit.clear()
            self._render()
        with QSignalBlocker(self.table):
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
            for index, row in enumerate(self._visible):
                if (row['origin'], row['id']) == self._identity:
                    self.table.selectRow(index)
                    break

    def _allow_leave(self, action):
        if self.pending_operation is not None:
            self._after_save = action
            return False
        if not self._dirty:
            return True
        choice = QMessageBox.question(self, 'Unsaved dictionary entry',
            'Save this entry before leaving it?', QMessageBox.StandardButton.Save |
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if choice == QMessageBox.StandardButton.Discard:
            self._clear_form()
            return True
        if choice == QMessageBox.StandardButton.Save:
            self._after_save = action
            self._save()
        else:
            self._after_save = None
            self._restore_selection()
            cancelled = self._app_cancel
            self._app_ready = self._app_cancel = None
            if cancelled:
                cancelled()
        return False

    def _complete_leave(self, action):
        if self._app_ready is not None:
            action = self._app_ready
        self._after_save = self._app_ready = self._app_cancel = None
        action()

    def prepare_app_close(self, ready, cancelled):
        """Return true only when the parent can safely finish closing."""
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
        fields = {'word': self.word_edit.text(), 'replacement': self.replacement_edit.toPlainText(),
                  'is_snippet': self.snippet_check.isChecked(), 'is_starred': self.starred_check.isChecked()}
        if self._identity is not None:
            fields.update(origin=self._identity[0], entry_id=self._identity[1])
        self._queued(self.service.save_dictionary_entry(**fields))

    def _delete(self):
        if self._identity is not None:
            self._queued(self.service.delete_dictionary_entry(*self._identity))

    def _queued(self, operation):
        if operation is None or operation is False:
            self.message_label.setText('The change was not queued. Check field limits or wait for pending work, then retry.')
            return
        self.pending_operation = operation
        self.message_label.setText('Saving locally…')
        self._busy(True)

    def refresh(self):
        snapshot = self.service.dictionary_snapshot()
        if snapshot['revision'] != self._revision:
            self._revision = snapshot['revision']
            self._rows = snapshot['rows']
            self._render()
        if self.pending_operation is not None and self.pending_operation in snapshot['results']:
            success = snapshot['results'][self.pending_operation]
            self.pending_operation = None
            if success:
                action = self._app_ready or self._after_save
                self._clear_form()
                if action is not None:
                    self._after_save = self._app_ready = self._app_cancel = None
                    QTimer.singleShot(0, action)
            self._busy(False)
            self.message_label.setText(snapshot['message'])
        elif self.pending_operation is None and not self._dirty:
            self.message_label.setText(snapshot['message'] or f'{len(self._rows)} active entries. Changes stay on this device.')
