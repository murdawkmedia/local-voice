import hashlib
from contextlib import closing, contextmanager
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from local_voice import migration
from local_voice.store import Store


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as db, db:
        yield db


class NativeImportTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('local_voice.native_import'),
                             'Idempotent native import is not implemented')
        self.module = importlib.import_module('local_voice.native_import')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'old'
        self.source.mkdir()
        self.store = Store(self.root / 'new' / 'history.db')
        with database(self.source / 'history.db') as db:
            db.executescript('''
                CREATE TABLE transcripts(id TEXT PRIMARY KEY, created REAL, text TEXT,
                    kind TEXT, paste_status TEXT, operation_id TEXT);
                CREATE TABLE notes(id TEXT PRIMARY KEY,created REAL,updated REAL,title TEXT,body TEXT);
                CREATE TABLE note_transcripts(note_id TEXT,transcript_id TEXT);
                CREATE TABLE meetings(note_id TEXT PRIMARY KEY,operation_id TEXT,cloud_id TEXT,
                    include_system INTEGER,thoughts TEXT,status TEXT,summary TEXT,refined_transcript TEXT);
                INSERT INTO transcripts VALUES('t1',1,'Generated dictation','dictation','copied','o1');
                INSERT INTO transcripts VALUES('t2',2,'Generated meeting','meeting','saved','o2');
                INSERT INTO transcripts VALUES('t3',3,'Generated tail','meeting','saved','o3');
                INSERT INTO notes VALUES('n1',2,4,'Generated title','Typed generated note');
                INSERT INTO notes VALUES('n2',5,6,'Typed only','Preserve typed body');
                INSERT INTO note_transcripts VALUES('n1','t2');
                INSERT INTO note_transcripts VALUES('n1','t3');
                INSERT INTO meetings VALUES('n1','o2','EXCLUDED-CLOUD',1,'Generated thoughts',
                    'complete','Generated legacy summary','Generated refined transcript');
            ''')

    def run_import(self):
        return self.module.import_native(self.source, self.store)

    def by_key(self):
        return {s['legacy_key']: self.store.get_session(s['id']) for s in self.store.list_sessions()}

    def source_hashes(self):
        return {p.relative_to(self.source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.source.rglob('*') if p.is_file()}

    def test_links_notes_summaries_and_timestamps_are_retained_without_source_writes(self):
        (self.source / 'session.json').write_bytes(b'\xffCREDENTIAL FILE NEVER OPENED')
        before = self.source_hashes()
        with patch.object(migration, 'inspect_native', wraps=migration.inspect_native) as inspect:
            report = self.run_import()
            self.assertGreaterEqual(inspect.call_count, 1)
        sessions = self.by_key()
        self.assertEqual(report['sessions_created'], 3)
        self.assertEqual(report['segments_created'], 3)
        meeting = sessions['wispr:note:n1']
        self.assertEqual((meeting['kind'], meeting['created'], meeting['updated']), ('meeting', 2, 4))
        self.assertEqual(meeting['notes'], 'Typed generated note')
        self.assertEqual(meeting['transcript'], 'Generated meeting\nGenerated tail')
        self.assertEqual(meeting['summary']['legacy']['summary'], 'Generated legacy summary')
        self.assertEqual(meeting['summary']['legacy']['refined_transcript'], 'Generated refined transcript')
        self.assertEqual(sessions['wispr:note:n2']['kind'], 'note')
        self.assertEqual(sessions['wispr:transcript:t1']['kind'], 'dictation')
        self.assertNotIn('EXCLUDED-CLOUD', json.dumps(sessions))
        self.assertNotIn('Generated', json.dumps(report))
        self.assertEqual(before, self.source_hashes())

    def test_repeated_import_keeps_local_edits_and_source_conflicts_are_reported(self):
        self.run_import()
        meeting = self.by_key()['wispr:note:n1']
        self.store.update_session(meeting['id'], title='Local edit', notes='Keep my edit')
        again = self.run_import()
        self.assertEqual(again['sessions_created'], 0)
        self.assertEqual(again['sessions_unchanged'], 3)
        with database(self.source / 'history.db') as db:
            db.execute("UPDATE notes SET title='Changed source' WHERE id='n1'")
        changed = self.run_import()
        self.assertEqual(changed['conflicts'], 1)
        self.assertEqual(self.by_key()['wispr:note:n1']['title'], 'Local edit')
        self.assertEqual(self.by_key()['wispr:note:n1']['notes'], 'Keep my edit')
        self.assertEqual(len(self.by_key()['wispr:note:n1']['segments']), 2)

    def test_invalid_schema_and_orphan_links_fail_before_mutation(self):
        for statement in ("INSERT INTO note_transcripts VALUES('missing','t1')",
                          'CREATE TABLE unknown_records(value TEXT)'):
            with database(self.source / 'history.db') as db:
                db.execute(statement)
            with self.assertRaises(migration.MigrationError):
                self.run_import()
            self.assertEqual(self.store.list_sessions(), [])
            with database(self.source / 'history.db') as db:
                db.execute('DELETE FROM note_transcripts WHERE note_id=?', ('missing',))

    def test_orphan_meeting_metadata_is_rejected(self):
        with database(self.source / 'history.db') as db:
            db.execute("UPDATE meetings SET note_id='missing'")
        with self.assertRaises(migration.MigrationError):
            self.run_import()
        self.assertEqual(self.store.list_sessions(), [])

    def test_late_dictionary_failure_rolls_back_all_database_imports(self):
        (self.source / 'dictionary.json').write_text(json.dumps([
            {'id': 'ok', 'word': 'GeneratedWord'}, {'id': 'bad', 'word': 10}]))
        with self.assertRaises(migration.MigrationError):
            self.run_import()
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(self.store.personalization()['words'], [])

    def test_optional_dictionary_is_idempotent(self):
        (self.source / 'dictionary.json').write_text(json.dumps({'entries': [
            {'id': 'ok', 'word': 'GeneratedWord'}, {'id': 'deleted', 'word': 'Gone', 'is_deleted': True}]}))
        self.assertEqual(self.run_import()['dictionary']['inserted'], 2)
        self.assertEqual(self.run_import()['dictionary']['unchanged'], 2)
        self.assertEqual(self.store.personalization()['words'], ['GeneratedWord'])

    def test_audio_and_selected_recovery_state_are_private_verified_and_idempotent(self):
        recordings = self.source / 'recordings'
        recordings.mkdir()
        audio = recordings / 'generated.wav'
        audio.write_bytes(b'RIFF generated fixture bytes WAVE')
        (self.source / 'pending.json').write_text(json.dumps({
            'audio': str(audio), 'kind': 'meeting', 'noteId': 'n1', 'operationId': 'o2',
            'cloud': True, 'account': 'EXCLUDED-ACCOUNT', 'access_token': 'EXCLUDED-TOKEN'}))
        (self.source / 'recovery.json').write_text(json.dumps([
            {'audio': 'recordings/generated.wav', 'kind': 'meeting'},
            {'audio': 'recordings/missing.wav'}, {'audio': '/etc/shadow'}]))
        before = self.source_hashes()
        report = self.run_import()
        self.assertEqual(report['audio_copied'], 1)
        self.assertEqual(report['missing_audio'], 1)
        self.assertEqual(report['external_audio'], 1)
        imported = list((self.store.database_path.parent / 'imported-audio').glob('*.wav'))
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0].read_bytes(), audio.read_bytes())
        self.assertEqual(stat.S_IMODE(imported[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(imported[0].parent.stat().st_mode), 0o700)
        with self.store.transaction() as db:
            states = [json.loads(r[0]) for r in db.execute('SELECT payload FROM imported_state')]
        self.assertEqual(len(states), 4)
        self.assertNotIn('EXCLUDED-', json.dumps(states))
        self.assertNotIn('cloud', json.dumps(states))
        self.assertEqual({s['audio_state'] for s in states}, {'available', 'missing', 'external'})
        self.assertEqual(before, self.source_hashes())
        repeated = self.run_import()
        self.assertEqual(repeated['audio_copied'], 0)
        self.assertEqual(repeated['audio_existing'], 1)
        self.assertEqual(repeated['state_unchanged'], 4)

    def test_external_or_symlink_audio_is_never_copied(self):
        (self.source / 'recordings').mkdir()
        outside = self.root / 'outside.wav'
        outside.write_bytes(b'NEVER COPY')
        (self.source / 'recordings' / 'link.wav').symlink_to(outside)
        (self.source / 'pending.json').write_text(json.dumps({'audio': str(outside)}))
        report = self.run_import()
        self.assertEqual(report['audio_copied'], 0)
        self.assertGreaterEqual(report['external_audio'], 1)

    def test_wal_sidecars_are_rejected_before_preview(self):
        (self.source / 'history.db-wal').write_bytes(b'not opened')
        with patch.object(migration, 'inspect_native', wraps=migration.inspect_native) as inspect:
            with self.assertRaises(migration.MigrationError):
                self.run_import()
            inspect.assert_not_called()
        self.assertEqual(self.store.list_sessions(), [])

    def test_iso_dates_normalized_and_exact_source_metadata_retained(self):
        with database(self.source / 'history.db') as db:
            db.execute("UPDATE notes SET created='2026-10-20T12:00:00+00:00',updated='2026-10-20T12:02:00+00:00' WHERE id='n2'")
        self.run_import()
        note = self.by_key()['wispr:note:n2']
        self.assertEqual(note['created'], 1792497600)
        self.assertEqual(note['updated'] - note['created'], 120)
        self.assertEqual(note['summary']['legacy']['created'], '2026-10-20T12:00:00+00:00')

    def test_no_database_does_not_create_source_database(self):
        (self.source / 'history.db').unlink()
        report = self.run_import()
        self.assertEqual(report['sessions_created'], 0)
        self.assertFalse((self.source / 'history.db').exists())

    def test_unknown_optional_columns_fail_closed(self):
        with database(self.source / 'history.db') as db:
            db.execute('ALTER TABLE notes ADD COLUMN unrecognized TEXT')
        with self.assertRaises(migration.MigrationError):
            self.run_import()
        self.assertEqual(self.store.list_sessions(), [])

    def test_database_and_audio_rollback_when_copy_installation_fails(self):
        recordings = self.source / 'recordings'
        recordings.mkdir()
        (recordings / 'a.wav').write_bytes(b'generated track one')
        (recordings / 'b.wav').write_bytes(b'generated track two')
        link = self.module.os.link
        calls = []
        def fail_second(source, destination):
            calls.append(destination)
            if len(calls) == 2:
                raise OSError('generated failure')
            return link(source, destination)
        with patch.object(self.module.os, 'link', side_effect=fail_second):
            with self.assertRaises(migration.MigrationError):
                self.run_import()
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(list((self.store.database_path.parent / 'imported-audio').glob('*.wav')), [])

    def test_bounded_selected_text_fails_before_any_import(self):
        with patch.object(self.module, 'MAX_SELECTED_TEXT_BYTES', 20, create=True):
            with self.assertRaises(migration.MigrationError):
                self.run_import()
        self.assertEqual(self.store.list_sessions(), [])

    def test_consistent_sqlite_backup_is_readonly_and_private(self):
        backups = []
        connections = []
        real_connect = sqlite3.connect
        class Tracked(sqlite3.Connection):
            def backup(connection, destination, **kwargs):
                backups.append(connection.execute('PRAGMA query_only').fetchone()[0])
                return super().backup(destination, **kwargs)
            def close(connection):
                connections.append(connection)
                return super().close()
        def connect(*args, **kwargs):
            kwargs['factory'] = Tracked
            return real_connect(*args, **kwargs)
        with patch.object(self.module.sqlite3, 'connect', side_effect=connect):
            self.run_import()
        self.assertEqual(backups, [1])
        self.assertGreaterEqual(len(connections), 4)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')
        self.assertEqual(list(self.store.database_path.parent.glob('.native-import-*')), [])


if __name__ == '__main__':
    unittest.main()
