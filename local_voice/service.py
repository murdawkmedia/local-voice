"""Application coordination. Qt reads snapshots; workers own blocking work."""

import copy
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time

from .controller import Controller
from .desktop import DesktopOutput, pulse_devices, select_sources
from .hotkeys import HotkeyListener
from .playback import PlaybackDucker
from .readiness import ReadyNotifier
from . import __version__


class Service:
    def __init__(self, store, settings, state_dir, engine_factory, *,
                 controller_factory=Controller, output=None, devices_fn=pulse_devices,
                 ducker=None, summarizer=None, model_lock=None, background=True):
        self.store = store
        self.settings = settings
        self.state_dir = Path(state_dir)
        self.output = output or DesktopOutput()
        self.devices_fn = devices_fn
        self.ducker = ducker or PlaybackDucker(journal=self.state_dir / 'playback-outputs.json',
                                               legacy_journal=self.state_dir / 'playback.json')
        self.summarizer = summarizer
        self.model_lock = model_lock or threading.Lock()
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._delivery_lock = threading.Lock()
        self._playback_lock = threading.Lock()
        self._closed = threading.Event()
        self._commands = queue.Queue(maxsize=64)
        self._threads = []
        self._command_thread = None
        self._summary_thread = None
        self._summaries = queue.Queue(maxsize=1)
        self._startup_done = threading.Event()
        self._recovery_ok = False
        self._playback_done = threading.Event()
        self._playback_wakeup = threading.Event()
        self._playback_background = background
        self._capture_playback_signature = None
        self._playback_refresh_at = 0
        self._durable_failed = False
        self._pending_edits = {}
        self._committed_edits = {}
        self._edit_revision = 0
        self._dictionary_next = 0
        self._dictionary_cache = {'rows': [], 'revision': 0, 'completed': 0, 'pending': 0,
                                  'results': {}, 'state': 'not-loaded', 'message': ''}
        self._transcript_next = 0
        self._transcript_cache = {'sessionId': None, 'segments': [], 'sessionState': None,
                                  'revision': 0, 'pending': 0, 'results': {},
                                  'state': 'not-loaded', 'message': ''}
        self._selected = None
        self._query = ''
        self._devices = {}
        self._notice = ''
        self._focus = None
        self._focus_at = 0
        self._delivery = {}
        self._pending_start = None
        self._ducked = None
        self._hotkeys = None
        self._hotkey_request = None
        self._hotkey_active = None
        self._shortcut_status = {'state': 'disabled', 'devices': 0}
        self._cache = {'status': {'state': 'recovering', 'message': 'Checking playback recovery.'}, 'sessions': [],
                       'selected': None, 'settings': settings.get(), 'devices': {}, 'notice': ''}
        self._readiness = ReadyNotifier(lambda: self.output.notify_ready(),
                                        enabled=self._cache['settings']['readyNotification'])
        try:
            self.controller = controller_factory(store, self.state_dir, engine_factory, self.sources,
                                                 cleanup_provider=self._cleanup_enabled,
                                                 event_callback=self._observe_capture)
        except Exception:
            self._readiness.close()
            raise
        if background:
            self._ensure_command_worker()
            for target, name in [(self._monitor, 'voice-state'),
                                 (self._desktop_loop, 'voice-desktop'),
                                 (self._playback_loop, 'voice-playback')]:
                thread = threading.Thread(target=target, name=name, daemon=True)
                thread.start()
                self._threads.append(thread)
        else:
            self._recover_playback()
            self.tick()

    def _cleanup_enabled(self):
        values = self.settings.get()
        # These are English rules: 'um' is a meaningful word in other languages.
        return values['cleanFillers'] and values['language'].casefold().split('-', 1)[0] == 'en'

    def _observe_capture(self, state):
        self._readiness.observe(state)
        signature = (state.get('sessionId'), state.get('kind'), state.get('captureActive'))
        if signature != self._capture_playback_signature:
            self._capture_playback_signature = signature
            self._playback_wakeup.set()

    def _playback_loop(self):
        self._startup_done.wait()
        try:
            while not self._closed.is_set():
                self._playback_wakeup.clear()
                try:
                    self._playback_tick()
                except Exception:
                    self._tell('Playback restoration is pending. Your volume changes are preserved.')
                self._playback_wakeup.wait(.25)
        finally:
            try:
                with self._playback_lock:
                    self.ducker.stop()
                    self._ducked = None
                self._playback_done.set()
            except Exception:
                self._tell('Playback restoration is pending. Your volume changes are preserved.')

    def _playback_tick(self):
        state = self.controller.status()
        values = self.settings.get()
        with self._playback_lock:
            with self._lock:
                owner = self._hotkey_active
                held = (owner is not None and not owner['released'] and
                        owner['sid'] == state.get('sessionId'))
            should_duck = (held and not self._closed.is_set() and values['lowerPlayback']
                           and state.get('kind') == 'dictation' and state.get('captureActive', False))
            if should_duck:
                if self._ducked != state['sessionId']:
                    self.ducker.stop()
                    # Track a partially applied mutation too, so release retries it.
                    self._ducked = state['sessionId']
                    self.ducker.start()
                    self._playback_refresh_at = time.monotonic() + .25
                elif hasattr(self.ducker, 'refresh') and time.monotonic() >= self._playback_refresh_at:
                    self.ducker.refresh()
                    self._playback_refresh_at = time.monotonic() + .25
            elif self._ducked is not None or getattr(self.ducker, 'pending', False) or not self._recovery_ok:
                self.ducker.stop()
                self._ducked = None
                self._recovery_ok = True

    def _tell(self, message):
        with self._lock:
            self._notice = message

    def _submit(self, function, *args, _durable=False, **kwargs):
        with self._lock:
            if self._closed.is_set():
                return False
            try:
                self._commands.put_nowait((function, args, kwargs, _durable))
                return True
            except queue.Full:
                self._tell('The app is busy. Please retry this action.')
                return False

    def _ensure_command_worker(self):
        with self._lock:
            if self._command_thread is None or not self._command_thread.is_alive():
                self._command_thread = threading.Thread(target=self._command_loop,
                                                        name='voice-commands', daemon=True)
                self._threads.append(self._command_thread)
                self._command_thread.start()

    def _recover_playback(self):
        if self._startup_done.is_set():
            return
        try:
            with self._playback_lock:
                self.ducker.stop()
            self._recovery_ok = True
        except Exception:
            self._tell('Playback restoration is pending. Your volume changes are preserved.')
        finally:
            self._startup_done.set()

    def _command_loop(self):
        self._recover_playback()
        try:
            while not self._closed.is_set() or not self._commands.empty():
                try:
                    function, args, kwargs, durable = self._commands.get(timeout=.05)
                except queue.Empty:
                    continue
                try:
                    if durable or not self._closed.is_set():
                        function(*args, **kwargs)
                except Exception:
                    if durable and function != self._flush_edits:
                        self._durable_failed = True
                    self._tell('That action could not finish. Pending edits and saved audio are retained.')
                finally:
                    self._commands.task_done()
        finally:
            if self._closed.is_set():
                try:
                    with self._playback_lock:
                        self.ducker.stop()
                        self._ducked = None
                    self._playback_done.set()
                except Exception:
                    self._tell('Playback restoration is pending. Your volume changes are preserved.')

    def _monitor(self):
        self._startup_done.wait()
        while not self._closed.wait(.2):
            try:
                self.tick()
            except Exception:
                self._tell('Local storage is temporarily unavailable. Recording recovery files are retained.')

    def _desktop_loop(self):
        next_devices = 0
        while not self._closed.is_set():
            now = time.monotonic()
            if now >= next_devices:
                try:
                    self.refresh_devices()
                except Exception:
                    self._tell('Audio devices are unavailable. Check the selected microphone and computer audio.')
                next_devices = now + 10
            target = self.output.focus()
            with self._lock:
                self._focus = target
                self._focus_at = time.monotonic()
            self._closed.wait(.2)

    def refresh_devices(self):
        devices = self.devices_fn()
        values = self.settings.get()
        defaults = {}
        if not values['microphoneSource']:
            microphone = next((d for d in devices.get('sources', [])
                               if d['name'] == devices.get('defaultSource') and not d.get('monitor')), None)
            if microphone:
                defaults['microphoneSource'] = microphone['name']
        if defaults:
            self.settings.update(**defaults)
        with self._lock:
            self._devices = devices

    def sources(self, kind):
        values = self.settings.get()
        with self._lock:
            devices = copy.deepcopy(self._devices)
        # A meeting always records both sources in this edition. It cannot
        # silently degrade into microphone-only capture.
        return select_sources(devices, values['microphoneSource'], values['computerSource'],
                              meeting=kind == 'meeting', include_system=True)

    def snapshot(self):
        with self._lock:
            data = copy.deepcopy(self._cache)
            self._overlay_edits(data)
            data['pendingEdits'] = {sid: copy.deepcopy(item['fields']) for sid, item in self._pending_edits.items()}
            data['dictionary'] = copy.deepcopy(self._dictionary_cache)
            data['startupReady'] = self._startup_done.is_set()
            data['playbackRecovered'] = self._recovery_ok
            data['notice'] = self._notice
            return data

    def _overlay_edits(self, data):
        overlays = {sid: dict(fields) for sid, fields in self._committed_edits.items()}
        for sid, item in self._pending_edits.items():
            overlays.setdefault(sid, {}).update(item['fields'])
        for row in [*data.get('sessions', []), data.get('selected')]:
            if row is not None:
                row.update(overlays.get(row['id'], {}))

    def public_status(self):
        with self._lock:
            data = self._cache
            state = data['status']
            capture = state.get('capture') or {}
            tracks = {key: {field: value.get(field) for field in
                           ('ready', 'frames', 'rate', 'level', 'duration_seconds', 'first_frame_seconds', 'finalized', 'errors')}
                      for key, value in capture.get('tracks', {}).items()}
            return {'ok': True, 'version': __version__, 'state': state.get('state'),
                    'message': state.get('message', ''), 'captureActive': state.get('captureActive', False),
                    'queuedJobs': state.get('queuedJobs', 0), 'backlog': state.get('backlog', False),
                    'previewError': state.get('previewError', ''), 'tracks': tracks,
                    'sessionCount': len(data['sessions']), 'shortcuts': dict(self._shortcut_status),
                    'offline': True}

    def start(self, kind, title='', notes='', origin='button'):
        return self._queue_start(kind, title, notes, origin)

    def _queue_start(self, kind, title='', notes='', origin='button', owner=None):
        request = threading.Event()
        with self._lock:
            if self._pending_start is not None or self._closed.is_set():
                return False
            self._pending_start = request
            if owner is not None:
                owner['request'] = request
            for old in self._delivery.values():
                old['delivered'] = True
        if not self._submit(self._start_request, request, kind, title, notes, origin, owner):
            with self._lock:
                self._pending_start = None
            return False
        return True

    def _start_request(self, request, kind, title, notes, origin, owner=None):
        try:
            if not request.is_set() and not self._closed.is_set():
                self.start_sync(kind, title=title, notes=notes, origin=origin, request=request, owner=owner)
        finally:
            with self._lock:
                if self._pending_start is request:
                    self._pending_start = None

    def start_sync(self, kind, title='', notes='', origin='button', request=None, owner=None):
        if self._closed.is_set():
            return None
        if not title and kind == 'meeting':
            title = datetime.now().strftime('Meeting · %b %d, %H:%M')
        # Never make microphone startup wait for a compositor or Pulse probe.
        with self._delivery_lock:
            if self._closed.is_set() or (request is not None and request.is_set()):
                return None
            with self._lock:
                focus = self._focus if time.monotonic() - self._focus_at <= .6 else None
            sid = self.controller.start(kind, title=title, notes=notes)
            with self._lock:
                for old in self._delivery.values():
                    old['delivered'] = True
                self._delivery[sid] = {'focus': copy.deepcopy(focus), 'origin': origin,
                                      'kind': kind, 'delivered': kind != 'dictation'}
                while len(self._delivery) > 64:
                    self._delivery.pop(next(iter(self._delivery)))
                self._selected = sid
                if owner is not None:
                    owner['sid'] = sid
                    self._hotkey_active = owner
                if (kind == 'dictation' and origin == 'hotkey' and not self._closed.is_set()
                        and (request is None or not request.is_set())):
                    self._readiness.arm(sid)
                    # Frames may arrive before start() returns. Arm/read while
                    # release is excluded; neither operation does desktop I/O.
                    self._readiness.observe(self.controller.status())
                else:
                    self._readiness.cancel()
            if request is not None and request.is_set():
                self._readiness.cancel(sid)
                self.controller.stop()
        self._tell('')
        return sid

    def stop(self):
        with self._lock:
            if self._pending_start is not None:
                self._pending_start.set()
            self._readiness.cancel()
            if self._hotkey_active is not None:
                self._hotkey_active['released'] = True
        self._playback_wakeup.set()
        self.controller.stop()

    def select_session(self, sid):
        with self._lock:
            self._selected = sid

    def search(self, query):
        with self._lock:
            self._query = query[:1024]

    def save_session(self, sid, **fields):
        with self._lock:
            if self._closed.is_set() or not fields:
                return False
            if sid not in self._pending_edits and len(self._pending_edits) >= 64:
                self._tell('Too many edits are pending. Wait for saved changes before editing another session.')
                return False
            old = self._pending_edits.get(sid)
            self._edit_revision += 1
            item = {'fields': ({} if old is None else dict(old['fields'])) | copy.deepcopy(fields),
                    'revision': self._edit_revision, 'queued': True}
            self._pending_edits[sid] = item
            if old is None or not old['queued']:
                if not self._submit(self._flush_edits, sid, _durable=True):
                    if old is None:
                        self._pending_edits.pop(sid, None)
                    else:
                        self._pending_edits[sid] = old
                    return False
            return True

    def _flush_edits(self, sid):
        while True:
            with self._lock:
                item = self._pending_edits.get(sid)
                if item is None:
                    return
                fields, revision = copy.deepcopy(item['fields']), item['revision']
            try:
                self.store.update_session(sid, **fields)
            except Exception:
                with self._lock:
                    if sid in self._pending_edits:
                        self._pending_edits[sid]['queued'] = False
                raise
            with self._lock:
                self._committed_edits.setdefault(sid, {}).update(fields)
                latest = self._pending_edits.get(sid)
                if latest is not None and latest['revision'] == revision:
                    self._pending_edits.pop(sid)
                    return

    def copy_session(self, sid):
        return self._submit(self._copy, sid)

    def _copy(self, sid):
        text = self.store.get_session(sid)['transcript']
        with self._delivery_lock:
            if self._closed.is_set():
                return
            self.output.copy(text)
        self._tell('Transcript copied.')

    def retry(self, sid):
        return self._submit(self._retry, sid)

    def _retry(self, sid):
        with self._delivery_lock:
            if self._closed.is_set():
                return
            self.controller.retry(sid)
            with self._lock:
                self._selected = sid
                self._delivery.pop(sid, None)
        self._tell('Retrying retained audio locally.')

    def configure(self, **fields):
        return self._submit(self._save_settings, _durable=True, **fields)

    def _save_settings(self, **fields):
        values = self.settings.update(**fields)
        self._readiness.set_enabled(values['readyNotification'])

    def transcript_snapshot(self):
        """Private cached editor state; no database access on the Qt thread."""
        with self._lock:
            return copy.deepcopy(self._transcript_cache)

    def request_transcript(self, sid):
        if not isinstance(sid, str) or not 0 < len(sid) <= 256:
            return False
        with self._lock:
            if not self._submit(self._refresh_transcript, sid):
                return False
            cache = self._transcript_cache
            if cache['sessionId'] != sid:
                cache.update(sessionId=sid, segments=[], sessionState=None)
                cache['revision'] += 1
            cache.update(state='loading', message='Loading saved transcript…')
            return True

    def _refresh_transcript(self, sid):
        try:
            row = self.store.get_session(sid)
        except Exception:
            with self._lock:
                if self._transcript_cache['sessionId'] == sid:
                    self._transcript_cache.update(state='error', message='The saved transcript is unavailable. Retry Refresh.')
            return False
        with self._lock:
            cache = self._transcript_cache
            if cache['sessionId'] == sid:
                cache.update(segments=row['segments'], sessionState=row['state'], state='ready', message='')
                cache['revision'] += 1
        return True

    def edit_transcript(self, sid, edits):
        if (not isinstance(sid, str) or not 0 < len(sid) <= 256
                or not isinstance(edits, list) or not 0 < len(edits) <= 10000):
            return None
        size = 0
        identifiers = set()
        try:
            for edit in edits:
                if not isinstance(edit, dict) or set(edit) != {'id', 'expected_text', 'text'}:
                    return None
                for value in edit.values():
                    if not isinstance(value, str) or '\0' in value or len(value) > 2_097_152:
                        return None
                    size += len(value.encode('utf-8'))
                if size > 2_097_152 or not edit['id'] or edit['id'] in identifiers:
                    return None
                identifiers.add(edit['id'])
        except UnicodeError:
            return None
        with self._lock:
            if self._closed.is_set() or self._transcript_cache['pending'] >= 32:
                return None
            self._transcript_next += 1
            operation = self._transcript_next
            if not self._submit(self._write_transcript, operation, sid, copy.deepcopy(edits), _durable=True):
                return None
            self._transcript_cache['pending'] += 1
            return operation

    def _write_transcript(self, operation, sid, edits):
        success = False
        try:
            self.store.edit_transcript(sid, edits)
            success = True
        except (ValueError, KeyError):
            pass
        except Exception:
            self._durable_failed = True
        finally:
            refreshed = self._refresh_transcript(sid)
            with self._lock:
                cache = self._transcript_cache
                cache['pending'] -= 1
                cache['results'][operation] = {'sessionId': sid, 'success': success}
                while len(cache['results']) > 64:
                    cache['results'].pop(next(iter(cache['results'])))
                if cache['sessionId'] == sid:
                    cache.update(state='saved' if success else 'error', message=(
                        'Saved locally.' if refreshed else 'Saved locally. Refresh is unavailable; retry Refresh.') if success else
                        'The transcript was not saved. Keep your draft; Refresh to check for conflicting changes, then retry.')

    def dictionary_snapshot(self):
        """Private cached editor data; this method never opens the database."""
        with self._lock:
            return copy.deepcopy(self._dictionary_cache)

    def request_dictionary(self):
        return self._submit(self._refresh_dictionary)

    def _refresh_dictionary(self):
        try:
            rows = self.store.list_dictionary()
        except Exception:
            with self._lock:
                self._dictionary_cache.update(state='error', message='The local dictionary is unavailable. Please retry.')
            return False
        with self._lock:
            self._dictionary_cache['rows'] = rows
            self._dictionary_cache['revision'] += 1
            if not self._dictionary_cache['pending']:
                self._dictionary_cache.update(state='ready', message='')
        return True

    def save_dictionary_entry(self, **fields):
        allowed = {'word', 'replacement', 'is_snippet', 'is_starred', 'origin', 'entry_id'}
        if fields.keys() - allowed or 'word' not in fields:
            return None
        for key, value in fields.items():
            maximum = 8192 if key == 'replacement' else 512 if key == 'word' else 256
            if key in {'is_snippet', 'is_starred'}:
                if type(value) is not bool:
                    return None
            elif not isinstance(value, str) or len(value.encode('utf-8')) > maximum:
                return None
        return self._queue_dictionary('save', fields)

    def delete_dictionary_entry(self, origin, entry_id):
        if any(not isinstance(value, str) or len(value.encode('utf-8')) > 256 for value in (origin, entry_id)):
            return None
        return self._queue_dictionary('delete', {'origin': origin, 'entry_id': entry_id})

    def _queue_dictionary(self, action, fields):
        with self._lock:
            if self._closed.is_set() or self._dictionary_cache['pending'] >= 32:
                return None
            self._dictionary_next += 1
            operation = self._dictionary_next
            if not self._submit(self._write_dictionary, operation, action, dict(fields), _durable=True):
                return None
            self._dictionary_cache['pending'] += 1
            self._dictionary_cache.update(state='saving', message='Saving locally…')
            return operation

    def _write_dictionary(self, operation, action, fields):
        success = False
        try:
            if action == 'save':
                self.store.save_dictionary_entry(**fields)
            else:
                self.store.delete_dictionary_entry(fields['origin'], fields['entry_id'])
            success = True
        except (ValueError, KeyError):
            pass
        except Exception:
            self._durable_failed = True
        finally:
            refreshed = self._refresh_dictionary()
            with self._lock:
                cache = self._dictionary_cache
                cache['pending'] -= 1
                cache['completed'] = operation
                cache['results'][operation] = success
                while len(cache['results']) > 64:
                    cache['results'].pop(next(iter(cache['results'])))
                cache.update(state='saving' if cache['pending'] else 'saved' if success and refreshed else 'error',
                             message='Saving locally…' if cache['pending'] else
                             ('Saved locally.' if refreshed else 'Saved locally. Refresh is unavailable; retry Refresh.') if success else
                             'The entry was not saved. Check duplicate triggers and field limits, then retry.')

    def _hotkey_press(self):
        owner = {'request': None, 'sid': None, 'released': False}
        with self._lock:
            self._hotkey_request = owner
        return self._queue_start('dictation', origin='hotkey', owner=owner)

    def _hotkey_release(self):
        with self._lock:
            owner = self._hotkey_request
            self._hotkey_request = None
            if owner is None:
                return
            owner['released'] = True
            if owner['request'] is not None:
                owner['request'].set()
        self._playback_wakeup.set()
        if owner['sid'] is not None:
            self._readiness.cancel(owner['sid'])
        self._stop_owned_hotkey(owner)

    def _stop_owned_hotkey(self, owner):
        if owner is None or not owner['released'] or not self._delivery_lock.acquire(blocking=False):
            return
        try:
            state = self.controller.status()
            with self._lock:
                owns = self._hotkey_active is owner and owner['sid'] == state.get('sessionId')
            if owns and state.get('captureActive'):
                self._readiness.cancel(owner['sid'])
                self.controller.stop()
            with self._lock:
                if self._hotkey_active is owner:
                    self._hotkey_active = None
        finally:
            self._delivery_lock.release()

    def _hotkey_state(self, value):
        with self._lock:
            self._shortcut_status = value

    def _configure_hotkeys(self, enabled):
        enabled = enabled and not self._closed.is_set()
        if enabled and self._hotkeys is None:
            listener = HotkeyListener(self._hotkey_press, self._hotkey_release,
                                      status_callback=self._hotkey_state)
            self._hotkeys = listener
            listener.start()
        elif not enabled and self._hotkeys is not None:
            self._hotkeys.close()
            self._hotkeys = None
            self._shortcut_status = {'state': 'disabled', 'devices': 0}

    def tick(self):
        with self._tick_lock:
            if not self._startup_done.is_set():
                return
            with self._lock:
                hotkey = self._hotkey_active
            self._stop_owned_hotkey(hotkey)
            state = self.controller.status()
            values = self.settings.get()
            active = state.get('captureActive', state.get('state') in {'preparing', 'recording', 'stopping'})
            sid = state.get('sessionId')
            if not self._playback_background:
                self._playback_tick()
            if not self._closed.is_set():
                self._configure_hotkeys(values['hotkeyEnabled'])
            with self._lock:
                selected_id, query = self._selected, self._query
                deliveries = list(self._delivery.items())
                devices = copy.deepcopy(self._devices)
            for delivery_sid, delivery in deliveries:
                if delivery['delivered']:
                    continue
                row = self.store.get_session(delivery_sid)
                if row['state'] == 'complete':
                    text = row['transcript']
                    with self._delivery_lock:
                        current = self.controller.status()
                        with self._lock:
                            valid = (not self._closed.is_set() and self._delivery.get(delivery_sid) is delivery
                                     and not delivery['delivered'] and current.get('sessionId') == delivery_sid
                                     and not current.get('captureActive', False)
                                     and self._pending_start is None)
                            if valid:
                                delivery['delivered'] = True
                        if not valid:
                            continue
                        if not text.strip():
                            self._tell('No speech was recognized. Audio is retained for retry.')
                            continue
                        auto = values['autoPaste'] and delivery['origin'] == 'hotkey'
                        result = self.output.deliver(text, delivery['focus'], auto_paste=auto)
                    self._tell('Transcript pasted.' if result['pasted'] else 'Transcript copied. You can paste it into your text box.'
                               if result['copied'] else 'Transcript saved in History. Clipboard delivery is unavailable.')
            rows = self.store.list_sessions(query)
            if selected_id is None and rows:
                selected_id = rows[0]['id']
                with self._lock:
                    self._selected = selected_id
            try:
                selected = self.store.get_session(selected_id) if selected_id else None
            except KeyError:
                selected = None
            profile = self.store.personalization()
            # Storage and desktop operations above may have overlapped a new
            # start. Never publish the earlier attempt's stale state afterward.
            state = self.controller.status()
            active = state.get('captureActive', state.get('state') in {'preparing', 'recording', 'stopping'})
            with self._lock:
                if self._pending_start is not None and not self._pending_start.is_set() and not active:
                    state = state | {'state': 'preparing', 'message': 'Preparing audio capture.', 'captureActive': True}
                self._cache = {'status': state, 'sessions': rows, 'selected': selected,
                               'settings': values, 'devices': devices, 'notice': self._notice,
                               'dictionaryCounts': {k: len(v) for k, v in profile.items()}}
                raw_rows = {row['id']: row for row in rows}
                if selected is not None:
                    raw_rows[selected['id']] = selected
                confirmed = []
                for edited_sid, fields in self._committed_edits.items():
                    raw = raw_rows.get(edited_sid)
                    if raw is not None and all(raw.get(key) == value for key, value in fields.items()):
                        confirmed.append(edited_sid)
                self._overlay_edits(self._cache)
                for edited_sid in confirmed:
                    self._committed_edits.pop(edited_sid, None)
                if not self._recovery_ok and not active:
                    self._cache['status'] = state | {
                        'state': 'recovery-pending', 'message': 'Playback restoration is pending.'}

    def summarize(self, sid):
        with self._lock:
            if self._closed.is_set():
                return False
            if self.summarizer is None:
                self._tell('The optional local summary model is not installed.')
                return False
            try:
                self._summaries.put_nowait(sid)
            except queue.Full:
                self._tell('A local summary is already queued. Please wait before requesting another.')
                return False
            if self._summary_thread is None or not self._summary_thread.is_alive():
                self._summary_thread = threading.Thread(target=self._summary_loop,
                                                        name='voice-summary', daemon=True)
                self._threads.append(self._summary_thread)
                self._summary_thread.start()
            return True

    def _summary_loop(self):
        while not self._closed.is_set() or not self._summaries.empty():
            try:
                sid = self._summaries.get(timeout=.05)
            except queue.Empty:
                continue
            try:
                if not self._closed.is_set():
                    self._summarize(sid)
            except Exception:
                if not self._closed.is_set():
                    self._tell('The local summary could not finish. The transcript is retained.')
            finally:
                self._summaries.task_done()

    def _summarize(self, sid):
        if self.summarizer is None:
            self._tell('The optional local summary model is not installed.')
            return
        row = self.store.get_session(sid)
        if row['state'] != 'complete' or not row['segments']:
            self._tell('Save the transcript before requesting its summary.')
            return
        expected = [(segment['id'], segment['text']) for segment in row['segments']]
        self._tell('Summarizing locally. Your transcript is already saved.')
        while not self._closed.is_set():
            if self.model_lock.acquire(timeout=.1):
                try:
                    result = self.summarizer.summarize(row['segments'], self._closed)
                finally:
                    self.model_lock.release()
                if self._closed.is_set():
                    return
                if self.store.save_summary_if_current(sid, result, expected):
                    self._tell('Local summary saved.')
                else:
                    self._tell('The transcript changed while summarizing. The previous summary is retained; request a new summary.')
                return

    def export_session(self, sid, path, fmt):
        return self._submit(self.export_sync, sid, path, fmt, _durable=True)

    def export_sync(self, sid, path, fmt):
        row = self.store.get_session(sid)
        if fmt == 'json':
            text = json.dumps(row, ensure_ascii=False, indent=2)
        elif fmt in {'md', 'txt'}:
            prefix = '# ' if fmt == 'md' else ''
            lines = [prefix + (row['title'] or row['kind'].capitalize()), '', 'Notes', row['notes'], '', 'Transcript']
            for segment in row['segments']:
                lines.append(f'[{int(segment["start"] // 60):02}:{int(segment["start"] % 60):02}] {segment["source"]}: {segment["text"]}')
            summary = row.get('summary') or {}
            if summary:
                lines.extend(['', 'Summary'])
                if summary.get('stale'):
                    lines.append('This transcript was edited. The retained summary is out of date; summarize again.')
                for item in summary.get('items', []):
                    lines.append(item.get('text', '') + ' [' + ', '.join(item.get('segment_ids', [])) + ']')
                if summary.get('legacy'):
                    lines.append(str(summary['legacy']))
            text = '\n'.join(lines)
        else:
            raise ValueError('Unsupported export format')
        target = Path(path)
        if target.is_symlink():
            raise ValueError('Export destination cannot be a symlink')
        fd, name = tempfile.mkstemp(prefix='.local-voice-export-', dir=target.parent)
        try:
            with os.fdopen(fd, 'w') as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)
        self._tell('Session exported.')

    def close(self, timeout=10):
        deadline = time.monotonic() + max(0, timeout)
        with self._lock:
            self._closed.set()
            for sid, item in self._pending_edits.items():
                if not item['queued']:
                    try:
                        self._commands.put_nowait((self._flush_edits, (sid,), {}, True))
                        item['queued'] = True
                    except queue.Full:
                        break
        self.stop()
        self._configure_hotkeys(False)
        self._ensure_command_worker()
        clean = self.controller.close(timeout=max(0, deadline - time.monotonic()))
        ready_clean = self._readiness.close(timeout=max(0, deadline - time.monotonic()))
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))
        with self._lock:
            durable = (not self._pending_edits and not self._commands.unfinished_tasks
                       and not self._durable_failed)
        return (clean and ready_clean and durable and self._playback_done.is_set() and
                all(not t.is_alive() for t in threads if t is not threading.current_thread()))
