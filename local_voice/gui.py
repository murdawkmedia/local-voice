"""Native view over cached service state. No recording or inference in Qt."""

from datetime import datetime
import copy
import json
import os
from pathlib import Path
import tempfile
import threading

from PyQt6.QtCore import Qt, QSignalBlocker, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTabWidget,
    QTextBrowser, QVBoxLayout, QWidget,
)

from .dictionary_ui import DictionaryDialog
from .transcript_ui import TranscriptDialog


def _button(text, callback, *, primary=False):
    button = QPushButton(text)
    button.setMinimumHeight(42)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        button.setProperty('primary', True)
    button.clicked.connect(callback)
    return button


def _plain(text):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _time(seconds):
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


def _session_date(row):
    summary = row.get('summary') or {}
    if isinstance(summary, dict) and summary.get('sourceTimezoneUnspecified') is True:
        source = summary.get('sourceCreatedAt')
        if isinstance(source, str) and len(source) <= 128:
            try:
                parsed = datetime.fromisoformat(source)
                if parsed.tzinfo is None:
                    return parsed.strftime('%Y-%m-%d · %H:%M') + ' · source time'
            except ValueError:
                pass
    return datetime.fromtimestamp(row.get('created', 0)).strftime('%b %d · %H:%M')


def _read_recovery(path):
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('Private recovery path is unavailable')
    if not path.exists():
        return {}
    with path.open('rb') as stream:
        payload = stream.read(8_388_609)
    if len(payload) > 8_388_608:
        raise ValueError('Private recovery data exceeds its bound')
    data = json.loads(payload)
    rows = data.get('sessions') if isinstance(data, dict) and data.get('schemaVersion') == 1 else None
    if not isinstance(rows, dict) or len(rows) > 128:
        raise ValueError('Private recovery data is invalid')
    for sid, fields in rows.items():
        if (not isinstance(sid, str) or not sid or len(sid) > 128 or not isinstance(fields, dict)
                or fields.keys() - {'title', 'notes'} or any(not isinstance(v, str) for v in fields.values())):
            raise ValueError('Private recovery fields are invalid')
    return rows


def _write_recovery(path, rows):
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or len(rows) > 128:
        raise ValueError('Private recovery path is unavailable')
    payload = json.dumps({'schemaVersion': 1, 'sessions': rows}, ensure_ascii=False).encode('utf-8')
    if len(payload) > 8_388_608:
        raise ValueError('Private recovery data exceeds its bound')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.gui-edits-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class MainWindow(QMainWindow):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self.selected_id = None
        self.dictation_id = None
        self._dictionary_dialog = None
        self._transcript_dialog = None
        self._selection_revision = None
        self._listing = None
        self._loading = False
        self._local_edits = {}
        self._edit_revision = 0
        self._closing_requested = False
        self._dictionary_close_pending = False
        self._transcript_close_pending = False
        self.close_cancelled_by_user = False
        self.shutdown_edits_accepted = False
        self._journal_revision = -1
        self._journal_done = threading.Event()
        self._journal_done.set()
        self._journal_error = False
        self._recovery_done = threading.Event()
        self._recovery_applied = False
        self._recovery_error = False
        self._recovered = {}
        state_dir = getattr(service, 'state_dir', None)
        self.recovery_path = Path(state_dir) / 'gui-edits-recovery.json' if state_dir is not None else None
        def load_recovery():
            try:
                if self.recovery_path is not None:
                    self._recovered = _read_recovery(self.recovery_path)
            except Exception:
                self._recovery_error = True
            finally:
                self._recovery_done.set()
        threading.Thread(target=load_recovery, name='gui-edit-recovery', daemon=True).start()
        self.setWindowTitle('Local Voice')
        self.resize(1120, 800)
        self.setMinimumSize(760, 600)
        self.setFont(QFont('Inter', 11))
        self.setStyleSheet('''
            QMainWindow, QWidget { background: #141b24; color: #e4eaf1; }
            QLabel#brand { font-size: 25px; font-weight: 650; }
            QLabel#muted { color: #a8b6c7; }
            QLabel#status { color: #8edbc6; font-size: 15px; }
            QTabWidget::pane { border: 0; }
            QTabBar::tab { padding: 12px 24px; color: #a8b6c7; border: 0; }
            QTabBar::tab:selected { color: #9de5d2; border-bottom: 2px solid #75cbb4; }
            QPushButton { background: #263342; border: 1px solid #354659;
                border-radius: 8px; padding: 8px 16px; }
            QPushButton:hover { background: #314256; }
            QPushButton:pressed { background: #1c2936; }
            QPushButton[primary="true"] { background: #8edbc6; color: #112722;
                font-weight: 600; border: 0; }
            QPushButton[primary="true"]:hover { background: #a3e7d5; }
            QPushButton:disabled { color: #788491; background: #1c2631; }
            QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QListWidget {
                background: #1c2632; border: 1px solid #344353;
                border-radius: 7px; padding: 8px; selection-background-color: #375d65; }
            QLineEdit:focus, QPlainTextEdit:focus { border-color: #8edbc6; }
            QListWidget::item { padding: 12px 8px; border-radius: 5px; }
            QListWidget::item:selected { background: #2b4650; }
            QProgressBar { background: #293746; border: 0; border-radius: 4px;
                max-height: 8px; min-height: 8px; }
            QProgressBar::chunk { background: #8edbc6; border-radius: 4px; }
            QCheckBox { spacing: 10px; padding: 6px 0; }
            QSplitter::handle { background: #141b24; width: 16px; }
        ''')
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(28, 22, 28, 20)
        outer.setSpacing(16)
        heading = QHBoxLayout()
        brand = _plain('Local Voice')
        brand.setObjectName('brand')
        heading.addWidget(brand)
        heading.addStretch()
        offline = _plain('ON THIS DEVICE · OFFLINE')
        offline.setWordWrap(False)
        offline.setObjectName('muted')
        heading.addWidget(offline)
        outer.addLayout(heading)
        status_row = QHBoxLayout()
        self.status_label = _plain('Ready')
        self.status_label.setObjectName('status')
        self.duration_label = _plain('00:00')
        self.duration_label.setFont(QFont('monospace', 12))
        self.stop_button = _button('Stop and save', service.stop)
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.duration_label)
        status_row.addWidget(self.stop_button)
        outer.addLayout(status_row)
        levels = QHBoxLayout()
        self.levels = {}
        for key, title in [('microphone', 'Microphone'), ('computer', 'Computer audio')]:
            levels.addWidget(_plain(title))
            level = QProgressBar()
            level.setRange(0, 1000)
            level.setTextVisible(False)
            level.setAccessibleName(title + ' level')
            levels.addWidget(level, 1)
            self.levels[key] = level
        outer.addLayout(levels)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self._build_dictation()
        self._build_library()
        self._build_settings()
        self.notice_label = _plain('')
        self.notice_label.setObjectName('muted')
        outer.addWidget(self.notice_label)
        self.setCentralWidget(central)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(180)
        self.save_timer.timeout.connect(self.save_edits)
        self.title_edit.textEdited.connect(self._edited)
        self.notes_edit.textChanged.connect(self._edited)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def _build_dictation(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 20, 0, 0)
        title = _plain('Your words, kept here.')
        title.setFont(QFont('Inter', 20, QFont.Weight.DemiBold))
        layout.addWidget(title)
        layout.addWidget(_plain('Start, wait for Listening, then speak. Stop saves your audio and text to History.'))
        row = QHBoxLayout()
        self.dictate_button = _button('Start dictation', lambda: self.service.start('dictation'), primary=True)
        row.addWidget(self.dictate_button)
        row.addStretch()
        self.shortcut_label = _plain('Ctrl + Super · enable in Settings')
        self.shortcut_label.setObjectName('muted')
        row.addWidget(self.shortcut_label)
        layout.addLayout(row)
        self.dictation_view = QTextBrowser()
        self.dictation_view.setPlaceholderText('Your latest dictation will appear here. Saved audio remains available if recognition fails.')
        self.dictation_view.setAccessibleName('Latest dictation')
        self.dictation_view.setOpenExternalLinks(False)
        layout.addWidget(self.dictation_view, 1)
        row = QHBoxLayout()
        self.dictation_copy_button = _button('Copy text', lambda: self.service.copy_session(self.dictation_id) if self.dictation_id else None)
        row.addWidget(self.dictation_copy_button)
        row.addWidget(_button('Open History', lambda: self.tabs.setCurrentIndex(1)))
        row.addStretch()
        layout.addLayout(row)
        self.tabs.addTab(page, 'Dictate')

    def _build_library(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 16, 0, 0)
        row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('Search titles, notes and transcripts')
        self.search_edit.setAccessibleName('Search saved sessions')
        self.search_edit.textChanged.connect(self.service.search)
        row.addWidget(self.search_edit, 1)
        self.filter_combo = QComboBox()
        for text, data in [('All sessions', ''), ('Meetings', 'meeting'), ('Dictation', 'dictation'), ('Notes', 'note')]:
            self.filter_combo.addItem(text, data)
        self.filter_combo.currentIndexChanged.connect(lambda: self._render_listing(self.service.snapshot()))
        row.addWidget(self.filter_combo)
        self.meeting_button = _button('Record meeting', lambda: self.service.start('meeting'), primary=True)
        row.addWidget(self.meeting_button)
        layout.addLayout(row)
        splitter = QSplitter()
        splitter.setHandleWidth(16)
        self.session_list = QListWidget()
        self.session_list.setAccessibleName('Saved sessions')
        self.session_list.currentItemChanged.connect(self._select_item)
        splitter.addWidget(self.session_list)
        detail = QWidget()
        details = QVBoxLayout(detail)
        details.setContentsMargins(0, 0, 0, 0)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText('Meeting or note title')
        self.title_edit.setAccessibleName('Saved session title')
        details.addWidget(self.title_edit)
        self.session_state = _plain('Select a session to read, rename or retry it.')
        self.session_state.setObjectName('muted')
        details.addWidget(self.session_state)
        self.detail_tabs = QTabWidget()
        self.transcript_view = QTextBrowser()
        self.transcript_view.setAccessibleName('Saved transcript')
        self.transcript_view.setOpenExternalLinks(False)
        self.transcript_view.setPlaceholderText('Live meeting segments appear here while you record. Both sources are labeled.')
        self.detail_tabs.addTab(self.transcript_view, 'Transcript')
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText('Type your own meeting notes. Changes save as you type.')
        self.notes_edit.setAccessibleName('Typed meeting notes')
        self.detail_tabs.addTab(self.notes_edit, 'Your notes')
        self.summary_view = QTextBrowser()
        self.summary_view.setPlaceholderText('Summaries are optional. Choose Summarize locally after the transcript is saved.')
        self.summary_view.setAccessibleName('Local meeting summary')
        self.summary_view.setOpenExternalLinks(False)
        self.detail_tabs.addTab(self.summary_view, 'Summary')
        details.addWidget(self.detail_tabs, 1)
        row = QHBoxLayout()
        row.addWidget(_button('Copy', lambda: self.service.copy_session(self.selected_id) if self.selected_id else None))
        row.addWidget(_button('Export…', self._export))
        self.edit_transcript_button = _button('Edit transcript', self.open_transcript)
        row.addWidget(self.edit_transcript_button)
        self.retry_button = _button('Retry audio', lambda: self.service.retry(self.selected_id) if self.selected_id else None)
        row.addWidget(self.retry_button)
        self.summary_button = _button('Summarize locally', lambda: self.service.summarize(self.selected_id) if self.selected_id else None)
        row.addWidget(self.summary_button)
        details.addLayout(row)
        splitter.addWidget(detail)
        splitter.setSizes([290, 710])
        layout.addWidget(splitter, 1)
        self.tabs.addTab(page, 'Meetings / History')

    def _build_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.addWidget(_plain('Everything is processed on this laptop. Models must be installed before use.'))
        layout.addWidget(_plain('Audio is deleted after a transcript is saved. Failed or empty attempts keep audio for Retry.\nHistory, original recognition and meeting summaries stay saved.'))
        form = QFormLayout()
        self.microphone_combo = QComboBox()
        self.computer_combo = QComboBox()
        self.engine_combo = QComboBox()
        self.engine_combo.addItem('Parakeet v3 · CPU', 'parakeet')
        self.engine_combo.addItem('Whisper large v3 turbo · GPU', 'whisper')
        self.language_edit = QLineEdit()
        self.language_edit.setPlaceholderText('en')
        self.language_edit.setMaximumWidth(160)
        for label, widget in [('Microphone', self.microphone_combo), ('Computer audio', self.computer_combo),
                              ('Speech model', self.engine_combo), ('Language code', self.language_edit)]:
            form.addRow(label, widget)
        layout.addLayout(form)
        self.checks = {}
        for key, text in [('autoPaste', 'Paste dictation when the original window still has focus'),
                          ('cleanFillers', 'Remove um, uh and clear hesitation ahs from English dictation'),
                          ('readyNotification', 'Show a silent Listening notification when the shortcut is ready'),
                          ('lowerPlayback', 'Lower playing apps while dictating'),
                          ('hotkeyEnabled', 'Enable Ctrl + Super hold shortcut')]:
            check = QCheckBox(text)
            check.toggled.connect(lambda value, key=key: self.service.configure(**{key: value}) if not self._loading else None)
            self.checks[key] = check
            layout.addWidget(check)
        layout.addWidget(_plain('Meetings record the selected microphone and computer audio together.\nMeeting summaries stay optional. Recording a meeting does not start a summary.\nNo cloud account or meeting allowance is used.'))
        self.microphone_combo.activated.connect(lambda: self.service.configure(microphoneSource=self.microphone_combo.currentData()))
        self.computer_combo.activated.connect(lambda: self.service.configure(computerSource=self.computer_combo.currentData()))
        self.engine_combo.activated.connect(lambda: self.service.configure(engine=self.engine_combo.currentData()))
        self.language_edit.editingFinished.connect(lambda: self.service.configure(language=self.language_edit.text().strip()))
        self.vocabulary_label = _plain('Personal dictionary: not loaded')
        self.vocabulary_label.setObjectName('muted')
        layout.addWidget(self.vocabulary_label)
        self.dictionary_button = _button('Edit personal dictionary', self.open_dictionary)
        layout.addWidget(self.dictionary_button)
        layout.addStretch()
        self.tabs.addTab(page, 'Settings')

    def open_dictionary(self):
        if self._dictionary_dialog is None:
            self._dictionary_dialog = DictionaryDialog(self.service, self)
        self._dictionary_dialog.show()
        self._dictionary_dialog.raise_()
        self._dictionary_dialog.activateWindow()

    def open_transcript(self):
        row = self.service.snapshot().get('selected')
        if not row or row.get('state') not in {'complete', 'failed', 'interrupted'} or not row.get('segments'):
            return
        if self._transcript_dialog is not None and self._transcript_dialog.session_id != row['id']:
            if not self._transcript_dialog.close():
                self._transcript_dialog.show()
                return
            self._transcript_dialog.deleteLater()
            self._transcript_dialog = None
        if self._transcript_dialog is None:
            self._transcript_dialog = TranscriptDialog(self.service, row['id'], self)
        else:
            self.service.request_transcript(row['id'])
        self._transcript_dialog.show()
        self._transcript_dialog.raise_()
        self._transcript_dialog.activateWindow()

    def _edited(self):
        if not self._loading and self.selected_id:
            self._local_edits[self.selected_id] = {'title': self.title_edit.text(),
                                                   'notes': self.notes_edit.toPlainText()}
            self._edit_revision += 1
            self.save_timer.start()

    def save_edits(self):
        self.save_timer.stop()
        if self.selected_id and not self._loading and self.selected_id in self._local_edits:
            return self.service.save_session(self.selected_id, **self._local_edits[self.selected_id])
        return True

    def flush_all_edits(self):
        self.save_timer.stop()
        accepted = True
        for sid, fields in list(self._local_edits.items()):
            try:
                accepted = bool(self.service.save_session(sid, **fields)) and accepted
            except Exception:
                accepted = False
        self.shutdown_edits_accepted = accepted
        return accepted

    def _apply_recovery(self):
        if not self._recovery_done.is_set() or self._recovery_applied:
            return
        self._recovery_applied = True
        if self._recovery_error:
            return
        for sid, fields in self._recovered.items():
            if sid not in self._local_edits:
                self._local_edits[sid] = fields
                self._edit_revision += 1
                self.service.save_session(sid, **fields)

    def request_close(self):
        if self._dictionary_close_pending or self._transcript_close_pending:
            return False
        return self.close()

    def _dictionary_ready_to_close(self):
        self._dictionary_close_pending = False
        self._transcript_close_pending = False
        self.request_close()

    def _dictionary_cancelled_close(self):
        self._dictionary_close_pending = False
        self._transcript_close_pending = False
        self._closing_requested = False
        self.close_cancelled_by_user = True
        callback = getattr(self, 'close_cancel_callback', None)
        if callback is not None:
            callback()

    def clear_recovery(self):
        """Called after the event loop and a verified clean Service.close()."""
        if self.recovery_path is not None and not self._recovery_error:
            if self.recovery_path.is_symlink():
                raise ValueError('Private recovery path is unavailable')
            self.recovery_path.unlink(missing_ok=True)
            if self.recovery_path.parent.exists():
                directory = os.open(self.recovery_path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)

    def _select_item(self, item, _previous):
        if item and not self._loading:
            self.save_edits()
            self.service.select_session(item.data(Qt.ItemDataRole.UserRole))

    def _render_listing(self, snapshot):
        rows = snapshot.get('sessions', [])
        kind = self.filter_combo.currentData()
        rows = [r for r in rows if not kind or r['kind'] == kind]
        signature = [(r['id'], r.get('title'), r.get('state'), r.get('updated'), _session_date(r)) for r in rows]
        if signature == self._listing:
            return
        self._listing = signature
        with QSignalBlocker(self.session_list):
            self.session_list.clear()
            for row in rows:
                title = row.get('title') or ('Meeting' if row['kind'] == 'meeting' else 'Dictation' if row['kind'] == 'dictation' else 'Note')
                date = _session_date(row)
                item = QListWidgetItem(f'{title}\n{date}  ·  {row.get("state", "")}')
                item.setData(Qt.ItemDataRole.UserRole, row['id'])
                self.session_list.addItem(item)
                if row['id'] == self.selected_id:
                    self.session_list.setCurrentItem(item)

    def _export(self):
        if not self.selected_id:
            return
        name, chosen = QFileDialog.getSaveFileName(self, 'Export saved session', str(Path.home() / 'Documents' / 'meeting.md'),
                                                   'Markdown (*.md);;Plain text (*.txt);;JSON (*.json)')
        if name:
            fmt = 'json' if 'JSON' in chosen else 'txt' if 'Plain' in chosen else 'md'
            self.service.export_session(self.selected_id, name, fmt)

    def refresh(self):
        self._apply_recovery()
        snapshot = self.service.snapshot()
        state = snapshot.get('status', {})
        phase = state.get('state', 'idle')
        self.status_label.setText(state.get('message') or phase.capitalize())
        capture = state.get('capture') or {}
        self.duration_label.setText(_time(capture.get('elapsed_seconds', 0)))
        capturing = state.get('captureActive', phase in {'preparing', 'recording', 'listening', 'stopping'})
        self.stop_button.setEnabled(capturing)
        self.dictate_button.setEnabled(state.get('canStart', not capturing))
        self.meeting_button.setEnabled(state.get('canStart', not capturing))
        for key, bar in self.levels.items():
            track = capture.get('tracks', {}).get(key, {})
            bar.setValue(min(1000, int(float(track.get('level', 0)) * 1000)))
            bar.setToolTip('Listening' if track.get('ready') else 'Waiting for audio' if capturing else 'Idle')
        self.notice_label.setText(snapshot.get('notice', ''))
        self._render_listing(snapshot)
        selected = snapshot.get('selected')
        if selected:
            sid = selected['id']
            pending = self._local_edits.get(sid)
            if pending:
                if (all(selected.get(key, '') == value for key, value in pending.items()) and
                        sid not in snapshot.get('pendingEdits', {})):
                    self._local_edits.pop(sid, None)
                else:
                    selected = selected | pending
            changed = sid != self.selected_id
            if changed:
                self.save_edits()
                self.selected_id = sid
                if selected['kind'] == 'meeting':
                    self.tabs.setCurrentIndex(1)
            revision = (sid, selected.get('updated'), len(selected.get('segments', [])), selected.get('audio_state'))
            if changed or revision != self._selection_revision:
                self._loading = True
                if changed or (not self.title_edit.hasFocus() and not self.save_timer.isActive()):
                    self.title_edit.setText(selected.get('title', ''))
                if changed or (not self.notes_edit.hasFocus() and not self.save_timer.isActive()):
                    self.notes_edit.setPlainText(selected.get('notes', ''))
                self._loading = False
                blocks = []
                for segment in selected.get('segments', []):
                    source = {'computer': 'Computer', 'microphone': 'Microphone', 'legacy': 'Imported'}.get(segment['source'], segment['source'])
                    blocks.append(f'[{_time(segment.get("start", 0))}] {source}\n{segment["text"]}')
                transcript = '\n\n'.join(blocks) or selected.get('transcript', '')
                position = self.transcript_view.verticalScrollBar().value()
                at_end = position >= self.transcript_view.verticalScrollBar().maximum() - 4
                self.transcript_view.setPlainText(transcript)
                self.transcript_view.verticalScrollBar().setValue(self.transcript_view.verticalScrollBar().maximum() if at_end else position)
                if selected['kind'] == 'dictation':
                    self.dictation_id = sid
                    self.dictation_view.setPlainText(selected.get('transcript', ''))
                summary = selected.get('summary') or {}
                summary_text = '\n\n'.join(item.get('text', '') for item in summary.get('items', []))
                if not summary_text and summary.get('legacy'):
                    summary_text = 'Imported summary\n\n' + str(summary['legacy'])
                if summary.get('stale'):
                    summary_text = 'This transcript was edited. The retained summary is out of date; summarize again.\n\n' + summary_text
                self.summary_view.setPlainText(summary_text)
                audio_label = {'removed': ' · Audio removed', 'pending': ' · Audio cleanup pending'}.get(selected.get('audio_state'), '')
                self.session_state.setText(selected.get('state', '').capitalize() + (' · ' + selected['error'] if selected.get('error') else '') + audio_label)
                self._selection_revision = revision
            self.retry_button.setEnabled(selected.get('state') not in {'preparing', 'recording'}
                                         and selected.get('audio_state', 'retained') == 'retained')
            self.summary_button.setEnabled(bool(selected.get('segments')) and selected.get('state') == 'complete')
            self.edit_transcript_button.setEnabled(bool(selected.get('segments')) and selected.get('state') in {'complete', 'failed', 'interrupted'})
        else:
            self.retry_button.setEnabled(False)
            self.summary_button.setEnabled(False)
            self.edit_transcript_button.setEnabled(False)
        settings = snapshot.get('settings', {})
        self._loading = True
        for key, check in self.checks.items():
            check.setChecked(bool(settings.get(key, False)))
        for combo, key, rows in [(self.microphone_combo, 'microphoneSource', [d for d in snapshot.get('devices', {}).get('sources', []) if not d.get('monitor')]),
                                  (self.computer_combo, 'computerSource', [d for d in snapshot.get('devices', {}).get('sources', []) if d.get('monitor')])]:
            choices = [(d['name'], d.get('description') or d['name']) for d in rows]
            if key == 'computerSource':
                choices.insert(0, ('', 'Follow default output'))
            if [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())] != choices:
                combo.clear()
                for value, label in choices:
                    combo.addItem(label, value)
            index = combo.findData(settings.get(key, ''))
            combo.setCurrentIndex(index)
        self.engine_combo.setCurrentIndex(max(0, self.engine_combo.findData(settings.get('engine', 'parakeet'))))
        if not self.language_edit.hasFocus():
            self.language_edit.setText(settings.get('language', 'en'))
        self._loading = False
        self.shortcut_label.setText('Hold Ctrl + Super to dictate' if settings.get('hotkeyEnabled') else 'Ctrl + Super · enable in Settings')
        counts = snapshot.get('dictionaryCounts', {})
        self.vocabulary_label.setText('Personal dictionary · ' + ' · '.join(f'{counts.get(k, 0)} {k}' for k in ['words', 'replacements', 'snippets']))
        if self._closing_requested:
            self.dictate_button.setEnabled(False)
            self.meeting_button.setEnabled(False)
            if self._recovery_done.is_set() and self._journal_done.is_set() and not self._journal_error:
                QTimer.singleShot(0, self.request_close)

    def closeEvent(self, event):
        if self._dictionary_close_pending or self._transcript_close_pending:
            event.ignore()
            return
        self.close_cancelled_by_user = False
        if self._dictionary_dialog is not None:
            self._dictionary_close_pending = True
            if not self._dictionary_dialog.prepare_app_close(self._dictionary_ready_to_close,
                                                              self._dictionary_cancelled_close):
                event.ignore()
                return
            self._dictionary_close_pending = False
        if self._transcript_dialog is not None:
            self._transcript_close_pending = True
            if not self._transcript_dialog.prepare_app_close(self._dictionary_ready_to_close,
                                                              self._dictionary_cancelled_close):
                event.ignore()
                return
            self._transcript_close_pending = False
        self._closing_requested = True
        self._apply_recovery()
        if not self._recovery_done.is_set() or self._recovery_error:
            self.notice_label.setText('Pending edits could not be recovered yet. The window remains open.')
            event.ignore()
            return
        self.flush_all_edits()
        if not self._journal_done.is_set():
            event.ignore()
            return
        if not self._local_edits or self._journal_revision == self._edit_revision:
            super().closeEvent(event)
            return
        if self._journal_done.is_set():
            if self.recovery_path is None:
                self.notice_label.setText('Pending edits could not be retained. Keep this window open and retry saving.')
                event.ignore()
                return
            fields, revision = copy.deepcopy(self._local_edits), self._edit_revision
            self._journal_done.clear()
            self._journal_error = False
            def preserve():
                try:
                    _write_recovery(self.recovery_path, fields)
                    self._journal_revision = revision
                except Exception:
                    self._journal_error = True
                finally:
                    self._journal_done.set()
            threading.Thread(target=preserve, name='gui-edit-save', daemon=True).start()
        self.notice_label.setText('Retaining pending edits before closing.')
        event.ignore()
