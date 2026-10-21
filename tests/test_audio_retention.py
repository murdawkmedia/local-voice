"""Generated audio only: successful text outlives its disposable WAV files."""
import importlib
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_voice.store import Store


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'history.db')
        self.recordings = self.root / 'recordings'
        self.recordings.mkdir()
        self.assertIsNotNone(importlib.util.find_spec('local_voice.audio_retention'),
                             'Successful recordings need a retention policy')
        self.module = importlib.import_module('local_voice.audio_retention')
        self.retention = self.module.AudioRetention(self.store, self.recordings)

    def session(self, *, kind='dictation', state='complete', text='Generated text.', legacy=None):
        sid = self.store.create_session(kind, title='Generated title', legacy_key=legacy)
        self.store.add_segment(sid, 'microphone', 0, 0, 1, text, raw_text='Um, generated text.')
        self.store.update_session(sid, state=state)
        directory = self.recordings / sid
        directory.mkdir()
        (directory / 'session.json').write_text(json.dumps(
            dict(schemaVersion=1, kind=kind, rate=48000, chunkFrames=576000)))
        (directory / 'microphone.wav').write_bytes(b'generated PCM fixture')
        (directory / 'chunks').mkdir()
        (directory / 'chunks/microphone-00000000.wav').write_bytes(b'generated chunk')
        if kind == 'meeting':
            (directory / 'computer.wav').write_bytes(b'generated computer fixture')
            (directory / 'chunks/computer-00000000.wav').write_bytes(b'generated chunk')
        return sid, directory

    def test_success_removes_raw_and_chunks_preserves_text_original_and_summary(self):
        sid, directory = self.session(kind='meeting')
        self.store.update_session(sid, summary={'items': [{'text': 'Generated summary'}]})
        (directory / 'operator-note.txt').write_text('keep this unrelated file')
        before = self.store.get_session(sid)
        self.assertTrue(self.retention.clean(sid))
        self.assertEqual(list(directory.rglob('*.wav')), [])
        after = self.store.get_session(sid)
        for key in ('state', 'transcript', 'segments', 'summary', 'title', 'updated'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after['audio_state'], 'removed')
        self.assertTrue((directory / 'session.json').is_file())
        self.assertTrue((directory / 'operator-note.txt').is_file())
        self.assertTrue(self.retention.clean(sid))

    def test_failed_empty_active_imported_and_note_sessions_are_preserved(self):
        for fields in ({'state': 'failed'}, {'state': 'interrupted'}, {'state': 'recording'},
                       {'text': '  '}, {'legacy': 'generated-import'}, {'kind': 'note'}):
            with self.subTest(fields=fields):
                sid, directory = self.session(**fields)
                before = {p.name: p.read_bytes() for p in directory.rglob('*.wav')}
                self.assertFalse(self.retention.clean(sid))
                self.assertEqual({p.name: p.read_bytes() for p in directory.rglob('*.wav')}, before)
                self.assertEqual(self.store.get_session(sid)['audio_state'], 'retained')

    def test_symlink_track_or_chunks_and_unknown_chunk_refuse_before_any_delete(self):
        outside = self.root / 'outside.wav'
        outside.write_bytes(b'outside fixture')
        for variant in ('track-link', 'chunk-link', 'unknown-chunk', 'linked-directory'):
            with self.subTest(variant=variant):
                sid, directory = self.session()
                if variant == 'track-link':
                    (directory / 'microphone.wav').unlink()
                    (directory / 'microphone.wav').symlink_to(outside)
                elif variant == 'chunk-link':
                    (directory / 'chunks/microphone-00000001.wav').symlink_to(outside)
                elif variant == 'unknown-chunk':
                    (directory / 'chunks/private.txt').write_text('preserve')
                else:
                    original = directory.with_name(directory.name + '-original')
                    directory.rename(original)
                    directory.symlink_to(original, target_is_directory=True)
                self.assertFalse(self.retention.clean(sid))
                self.assertTrue((directory / 'microphone.wav').exists())
                self.assertEqual(outside.read_bytes(), b'outside fixture')

    def test_delete_failure_keeps_successful_text_and_resumes_from_pending(self):
        sid, directory = self.session()
        with patch.object(self.module.os, 'unlink', side_effect=PermissionError('generated')):
            self.assertFalse(self.retention.clean(sid))
        row = self.store.get_session(sid)
        self.assertEqual((row['state'], row['transcript'], row['audio_state']),
                         ('complete', 'Generated text.', 'pending'))
        self.assertTrue(self.retention.clean(sid))
        self.assertEqual(list(directory.rglob('*.wav')), [])

    def test_cleanup_intent_requires_durable_text_and_storage_error_preserves_audio(self):
        sid, directory = self.session()
        with patch.object(self.store, 'begin_audio_cleanup', side_effect=sqlite3.OperationalError('generated storage')):
            self.assertFalse(self.retention.clean(sid))
        self.assertTrue((directory / 'microphone.wav').is_file())
        self.store.update_session(sid, state='failed')
        self.assertFalse(self.store.begin_audio_cleanup(sid))

    def test_startup_sweep_cleans_old_successes_without_touching_failed_audio(self):
        successful, directory = self.session()
        failed, kept = self.session(state='failed')
        self.retention.sweep()
        self.assertEqual(list(directory.rglob('*.wav')), [])
        self.assertTrue((kept / 'microphone.wav').is_file())
        self.assertEqual(self.store.get_session(failed)['audio_state'], 'retained')

    def test_inventory_limit_refuses_before_deletion_and_cancelled_sweep_is_inert(self):
        import threading
        sid, directory = self.session()
        (directory / 'chunks/microphone-00000001.wav').write_bytes(b'generated')
        with patch.object(self.retention, 'MAX_FILES', 1):
            self.assertFalse(self.retention.clean(sid))
        self.assertTrue((directory / 'microphone.wav').is_file())
        stopped = threading.Event()
        stopped.set()
        self.retention.sweep(stopped)
        self.assertTrue((directory / 'microphone.wav').is_file())

    def test_partial_unlink_failure_resumes_and_final_marker_failure_is_recoverable(self):
        sid, directory = self.session()
        unlink = self.module.os.unlink
        count = 0
        def partial(name, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise PermissionError('generated second unlink')
            return unlink(name, **kwargs)
        with patch.object(self.module.os, 'unlink', partial):
            self.assertFalse(self.retention.clean(sid))
        self.assertEqual(self.store.get_session(sid)['audio_state'], 'pending')
        with patch.object(self.store, 'finish_audio_cleanup', side_effect=sqlite3.OperationalError('generated')):
            self.assertFalse(self.retention.clean(sid))
        self.assertEqual(list(directory.rglob('*.wav')), [])
        self.assertTrue(self.retention.clean(sid))
        self.assertEqual(self.store.get_session(sid)['audio_state'], 'removed')


if __name__ == '__main__':
    unittest.main()
