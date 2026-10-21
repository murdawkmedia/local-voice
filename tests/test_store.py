import concurrent.futures
from contextlib import closing
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
import hashlib


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('local_voice.store'),
                             'Durable session store is not implemented')
        self.module = importlib.import_module('local_voice.store')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'private' / 'sessions.db'
        self.store = self.module.Store(self.path)

    def test_transcript_override_retains_recognition_identity_and_base_replay(self):
        sid = self.store.create_session('meeting', legacy_key='generated:edited')
        args = (sid, 'microphone', 0, 1, 3, 'Processed original', 'Raw recognition')
        segment = self.store.add_segment(*args)
        self.store.update_session(sid, state='complete')
        result = self.store.edit_transcript(sid, [
            {'id': segment, 'expected_text': 'Processed original', 'text': 'Corrected café\n第二行'}])
        self.assertEqual(result, {'edited': 1, 'summaryStale': False})
        retained = self.module.Store(self.path).get_session(sid)
        row = retained['segments'][0]
        self.assertEqual((row['id'], row['source'], row['ordinal'], row['start'], row['end']),
                         (segment, 'microphone', 0, 1, 3))
        self.assertEqual((row['text'], row['recognized_text'], row['raw_text'], row['edited']),
                         ('Corrected café\n第二行', 'Processed original', 'Raw recognition', True))
        self.assertEqual(retained['transcript'], 'Corrected café\n第二行')
        self.assertEqual(self.store.add_segment(*args), segment)
        self.assertEqual(self.store.create_session('meeting', legacy_key='generated:edited'), sid)
        self.assertEqual(self.store.get_session(sid)['segments'][0]['text'], row['text'])
        with self.store.transaction() as db:
            base = db.execute('SELECT text,raw_text FROM segments WHERE id=?', (segment,)).fetchone()
            self.assertEqual(tuple(base), ('Processed original', 'Raw recognition'))

    def test_transcript_change_marks_retained_summary_stale_and_noop_preserves_time(self):
        sid = self.store.create_session('meeting')
        segment = self.store.add_segment(sid, 'computer', 0, 0, 1, 'Original')
        summary = {'items': [{'text': 'Existing summary', 'segment_ids': [segment]}]}
        self.store.update_session(sid, state='complete', summary=summary)
        untouched = self.store.get_session(sid)
        self.assertEqual(self.store.edit_transcript(sid, [
            {'id': segment, 'expected_text': 'Original', 'text': 'Original'}]),
            {'edited': 0, 'summaryStale': False})
        self.assertEqual(self.store.get_session(sid)['updated'], untouched['updated'])
        self.assertFalse(self.store.get_session(sid)['segments'][0]['edited'])
        self.assertEqual(self.store.edit_transcript(sid, [
            {'id': segment, 'expected_text': 'Original', 'text': ''}]),
            {'edited': 1, 'summaryStale': True})
        before = self.store.get_session(sid)
        self.assertEqual(before['summary'], summary | {'stale': True})
        self.assertEqual(before['segments'][0]['text'], '')
        self.assertEqual(before['segments'][0]['raw_text'], 'Original')
        self.assertEqual(self.store.edit_transcript(sid, [
            {'id': segment, 'expected_text': '', 'text': ''}]),
            {'edited': 0, 'summaryStale': True})
        self.assertEqual(self.store.get_session(sid)['updated'], before['updated'])

    def test_transcript_cas_wrong_session_and_active_state_reject_whole_batch(self):
        sid = self.store.create_session('meeting')
        first = self.store.add_segment(sid, 'microphone', 0, 0, 1, 'First')
        second = self.store.add_segment(sid, 'computer', 0, 1, 2, 'Second')
        other = self.store.create_session('meeting')
        foreign = self.store.add_segment(other, 'computer', 0, 0, 1, 'Foreign')
        self.store.update_session(sid, state='complete')
        for bad in ({'id': second, 'expected_text': 'Wrong', 'text': 'Changed'},
                    {'id': foreign, 'expected_text': 'Foreign', 'text': 'Changed'},
                    {'id': 'missing', 'expected_text': '', 'text': 'Changed'}):
            with self.assertRaises(ValueError):
                self.store.edit_transcript(sid, [
                    {'id': first, 'expected_text': 'First', 'text': 'Must roll back'}, bad])
            self.assertEqual(self.store.get_session(sid)['transcript'], 'First\nSecond')
        edit = {'id': first, 'expected_text': 'First', 'text': 'Changed'}
        for state in ('preparing', 'recording', 'captured', 'transcribing'):
            self.store.update_session(sid, state=state)
            with self.assertRaises(ValueError):
                self.store.edit_transcript(sid, [edit])
        with self.assertRaises(KeyError):
            self.store.edit_transcript('missing', [edit])
        for state in ('failed', 'interrupted', 'complete'):
            self.store.update_session(sid, state=state)
            current = self.store.get_session(sid)['segments'][0]['text']
            self.store.edit_transcript(sid, [edit | {'expected_text': current, 'text': state}])
        self.assertEqual(self.store.get_session(other)['transcript'], 'Foreign')

    def test_transcript_batch_limits_and_duplicate_ids_reject_without_writes(self):
        sid = self.store.create_session('dictation')
        segment = self.store.add_segment(sid, 'microphone', 0, 0, 1, 'Original')
        self.store.update_session(sid, state='complete')
        edit = {'id': segment, 'expected_text': 'Original', 'text': 'Correction'}
        for batch in ((edit,), [edit, edit], [edit] * 10001,
                      [edit | {'id': 'x' * 257}], [edit | {'text': None}],
                      [edit | {'text': 'bad\0text'}], [edit | {'extra': 'ignored?'}],
                      [edit | {'text': '界' * (2 * 1024**2 // 3 + 1)}],
                      [edit | {'expected_text': 'x' * 1024**2, 'text': 'x' * 1024**2}]):
            with self.subTest(kind=type(batch).__name__), self.assertRaises(ValueError):
                self.store.edit_transcript(sid, batch)
        row = self.store.get_session(sid)['segments'][0]
        self.assertEqual((row['text'], row['recognized_text'], row['edited']),
                         ('Original', 'Original', False))

    def test_transcript_search_includes_literal_override_base_and_raw_without_segment_metadata(self):
        sid = self.store.create_session('dictation')
        segment = self.store.add_segment(sid, 'microphone', 0, 0, 1, 'ProcessedNeedle', 'RawNeedle')
        self.store.create_session('note', title='Different result')
        self.store.update_session(sid, state='complete')
        self.store.edit_transcript(sid, [{'id': segment, 'expected_text': 'ProcessedNeedle',
                                         'text': 'Corrected café %_'}])
        for query in ('ProcessedNeedle', 'RawNeedle', 'Corrected café', '%', '_'):
            rows = self.store.list_sessions(query)
            self.assertEqual([row['id'] for row in rows], [sid])
            self.assertFalse({'segments', 'recognized_text', 'raw_text', 'edited'} & rows[0].keys())

    def test_schema_one_reopen_adds_overrides_without_rewriting_recognition(self):
        sid = self.store.create_session('dictation')
        segment = self.store.add_segment(sid, 'microphone', 0, 0, 1, 'Base', 'Raw')
        self.store.update_session(sid, state='complete')
        with self.store.transaction() as db:
            db.execute('DROP TABLE segment_overrides')
        reopened = self.module.Store(self.path)
        row = reopened.get_session(sid)['segments'][0]
        self.assertEqual((row['recognized_text'], row['raw_text'], row['edited']), ('Base', 'Raw', False))
        reopened.edit_transcript(sid, [{'id': segment, 'expected_text': 'Base', 'text': 'Edited'}])
        with reopened.transaction() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO segment_overrides VALUES(?,?,?)', ('missing', 'Text', 1))

    def test_concurrent_transcript_cas_allows_only_one_editor(self):
        sid = self.store.create_session('dictation')
        segment = self.store.add_segment(sid, 'microphone', 0, 0, 1, 'Base', 'Raw')
        self.store.update_session(sid, state='complete')
        def edit(text):
            try:
                self.module.Store(self.path).edit_transcript(sid, [
                    {'id': segment, 'expected_text': 'Base', 'text': text}])
                return True
            except self.module.SegmentConflict:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(edit, ['First editor', 'Second editor'])), [False, True])
        row = self.store.get_session(sid)['segments'][0]
        self.assertIn(row['text'], ('First editor', 'Second editor'))
        self.assertEqual((row['recognized_text'], row['raw_text']), ('Base', 'Raw'))

    def test_summary_cas_rejects_model_result_after_edit_without_overwriting_stale_summary(self):
        sid = self.store.create_session('meeting')
        segment = self.store.add_segment(sid, 'microphone', 0, 0, 1, 'Original')
        old = {'items': [{'text': 'Old summary', 'segment_ids': [segment]}]}
        new = {'items': [{'text': 'New summary', 'segment_ids': [segment]}]}
        self.store.update_session(sid, state='complete', summary=old)
        snapshot = [(segment, 'Original')]
        self.store.edit_transcript(sid, [{'id': segment, 'expected_text': 'Original', 'text': 'Corrected'}])
        before = self.store.get_session(sid)
        self.assertFalse(self.store.save_summary_if_current(sid, new, snapshot))
        rejected = self.store.get_session(sid)
        self.assertEqual((rejected['summary'], rejected['updated']), (old | {'stale': True}, before['updated']))
        self.assertTrue(self.store.save_summary_if_current(sid, new, ((segment, 'Corrected'),)))
        self.assertEqual(self.store.get_session(sid)['summary'], new)

    def test_summary_cas_requires_complete_ordered_bounded_snapshot_and_valid_references(self):
        sid = self.store.create_session('meeting')
        a = self.store.add_segment(sid, 'microphone', 0, 0, 1, 'A')
        b = self.store.add_segment(sid, 'computer', 0, 1, 2, 'B')
        snapshot = [(a, 'A'), (b, 'B')]
        self.store.update_session(sid, state='failed')
        self.assertFalse(self.store.save_summary_if_current(sid, {'items': []}, snapshot))
        self.store.update_session(sid, state='complete')
        self.assertFalse(self.store.save_summary_if_current(sid, {'items': []}, list(reversed(snapshot))))
        for invalid in ([('missing', 'Text')] * 10001, [('x', 'x' * (2 * 1024**2))],
                        [(a, None)], [(a, 'A', 'extra')], 'unstructured'):
            with self.assertRaises(ValueError):
                self.store.save_summary_if_current(sid, {}, invalid)
        with self.assertRaises(ValueError):
            self.store.save_summary_if_current(sid, {'items': [{'segment_ids': ['foreign']}]}, snapshot)
        self.assertEqual(self.store.get_session(sid)['summary'], {})

    def test_transcript_override_survives_repeat_native_import_without_source_changes(self):
        from local_voice.native_import import import_native
        source = Path(self.tmp.name) / 'generated-native'
        source.mkdir()
        database = source / 'history.db'
        with closing(sqlite3.connect(database)) as db:
            db.executescript('''
                CREATE TABLE transcripts(id TEXT PRIMARY KEY,created REAL,text TEXT,
                    kind TEXT,paste_status TEXT,operation_id TEXT);
                CREATE TABLE notes(id TEXT PRIMARY KEY,created REAL,updated REAL,title TEXT,body TEXT);
                CREATE TABLE note_transcripts(note_id TEXT,transcript_id TEXT);
                INSERT INTO transcripts VALUES('generated',1,'Native base','dictation','copied','generated-op');
            ''')
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        self.assertEqual(import_native(source, self.store)['sessions_created'], 1)
        sid = self.store.list_sessions()[0]['id']
        segment = self.store.get_session(sid)['segments'][0]['id']
        self.store.edit_transcript(sid, [{'id': segment, 'expected_text': 'Native base', 'text': 'Local correction'}])
        self.assertEqual(import_native(source, self.store)['sessions_unchanged'], 1)
        saved = self.store.get_session(sid)['segments'][0]
        self.assertEqual((saved['text'], saved['recognized_text'], saved['raw_text']),
                         ('Local correction', 'Native base', 'Native base'))
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)

    def test_transcript_override_survives_repeat_account_import_and_source_conflict(self):
        from tools.account_meetings import import_meetings
        rows = [{'id': '11111111-1111-4111-8111-111111111111', 'title': 'Generated',
                 'created': 1, 'notes': '', 'summary': 'Imported summary', 'transcript': 'Account base'}]
        self.assertEqual(import_meetings(self.store, rows)['created'], 1)
        sid = self.store.list_sessions()[0]['id']
        segment = self.store.get_session(sid)['segments'][0]['id']
        self.store.edit_transcript(sid, [{'id': segment, 'expected_text': 'Account base', 'text': 'Local correction'}])
        self.assertEqual(import_meetings(self.store, rows)['unchanged'], 1)
        self.assertEqual(import_meetings(self.store, [rows[0] | {'transcript': 'Changed source'}])['conflicts'], 1)
        saved = self.store.get_session(sid)
        self.assertEqual(saved['segments'][0]['text'], 'Local correction')
        self.assertEqual(saved['segments'][0]['raw_text'], 'Account base')
        self.assertTrue(saved['summary']['stale'])

    def test_local_dictionary_edit_and_tombstone_survive_restart_and_reimport(self):
        imported = [{'id': 'edit', 'word': 'GeneratedWrong', 'replacement': 'First'},
                    {'id': 'delete', 'word': 'GeneratedGone'}]
        self.store.import_dictionary(imported, origin='wispr')
        self.store.save_dictionary_entry(origin='wispr', entry_id='edit', word='GeneratedWrong',
                                         replacement='Locally corrected', is_snippet=True)
        self.store.delete_dictionary_entry('wispr', 'delete')
        restarted = self.module.Store(self.path)
        restarted.import_dictionary(imported, origin='wispr')
        rows = restarted.list_dictionary(include_deleted=True)
        self.assertEqual(len(rows), 2)
        edited = next(row for row in rows if row['id'] == 'edit')
        self.assertEqual((edited['origin'], edited['replacement'], edited['is_snippet']),
                         ('wispr', 'Locally corrected', 1))
        self.assertTrue(next(row for row in rows if row['id'] == 'delete')['is_deleted'])
        self.assertEqual(restarted.personalization()['snippets'], {'GeneratedWrong': 'Locally corrected'})
        self.assertNotIn('GeneratedGone', restarted.personalization()['words'])

    def test_new_dictionary_identity_duplicate_prevention_and_bounded_fields(self):
        row = self.store.save_dictionary_entry(word='GeneratedName')
        self.assertEqual(row['origin'], 'local')
        import uuid
        self.assertEqual(str(uuid.UUID(row['id'])), row['id'])
        for fields in ({'word': ' generatedname ', 'replacement': 'Conflict'},
                       {'word': ''}, {'word': 'x'*513}, {'word': 'ok', 'replacement': 'x'*8193},
                       {'word': 'multiline\ntrigger'}, {'word': 'snippet', 'is_snippet': True}):
            with self.subTest(fields=list(fields)), self.assertRaises(ValueError):
                self.store.save_dictionary_entry(**fields)
        self.assertEqual(len(self.store.list_dictionary()), 1)
        self.store.delete_dictionary_entry(row['origin'], row['id'])
        replacement = self.store.save_dictionary_entry(word='generatedname', replacement='Generated output')
        self.assertNotEqual(replacement['id'], row['id'])

    def test_imported_duplicate_trigger_has_one_visible_effective_winner(self):
        self.store.import_dictionary([{'id': 'a', 'word': 'GeneratedTrigger', 'replacement': 'First'},
                                      {'id': 'b', 'word': 'generatedtrigger', 'replacement': 'Second'}])
        rows = self.store.list_dictionary()
        self.assertEqual(sum(bool(row['effective']) for row in rows), 1)
        self.assertTrue(all(row['duplicate'] for row in rows))
        self.assertEqual(len(self.store.personalization()['replacements']), 1)
        self.store.delete_dictionary_entry('wispr', 'a')
        self.assertEqual(self.store.personalization()['replacements'], {'generatedtrigger': 'Second'})

    def test_derived_import_identity_preserves_exact_original_payload(self):
        word = ' Generated source word '
        self.store.import_dictionary([{'word': word}])
        identity = json.dumps([word, '', 0], ensure_ascii=False, separators=(',', ':'))
        expected = 'derived:' + hashlib.sha256(identity.encode()).hexdigest()
        self.assertEqual(self.store.list_dictionary()[0]['id'], expected)

    def test_dictionary_limit_rolls_back_whole_import(self):
        with patch.object(self.module, 'MAX_DICTIONARY_ENTRIES', 2):
            self.store.import_dictionary([{'id': 'a', 'word': 'GeneratedA'}, {'id': 'b', 'word': 'GeneratedB'}])
            with self.assertRaises(ValueError):
                self.store.import_dictionary([{'id': 'a', 'word': 'Must roll back'},
                                               {'id': 'c', 'word': 'GeneratedC'}])
            self.assertEqual(set(self.store.personalization()['words']), {'GeneratedA', 'GeneratedB'})

    def test_restart_retains_title_notes_timestamps_and_raw_text(self):
        sid = self.store.create_session('meeting', title='Generated title', created=123,
                                        notes='Typed note', legacy_key='fixture:1')
        segment = self.store.add_segment(sid, 'microphone', 0, 0, 1, 'clean text', 'raw text')
        self.store.update_session(sid, title='Edited title', notes='Edited note', state='complete')
        result = self.module.Store(self.path).get_session(sid)
        self.assertEqual((result['created'], result['title'], result['notes']),
                         (123, 'Edited title', 'Edited note'))
        self.assertEqual(result['segments'][0]['id'], segment)
        self.assertEqual(result['segments'][0]['raw_text'], 'raw text')
        self.assertEqual(result['legacy_key'], 'fixture:1')
        self.assertEqual(result['state'], 'complete')

    def test_unequal_tracks_are_sorted_by_time_source_and_ordinal(self):
        sid = self.store.create_session('meeting')
        for source, ordinal, start, end in [('microphone', 0, 2, 7), ('computer', 2, 1, 3),
                                            ('computer', 1, 1, 2), ('microphone', 1, 1, 4)]:
            self.store.add_segment(sid, source, ordinal, start, end, f'{source}:{ordinal}')
        result = self.store.get_session(sid)
        self.assertEqual([(s['source'], s['ordinal']) for s in result['segments']],
                         [('computer', 1), ('computer', 2), ('microphone', 1), ('microphone', 0)])

    def test_exact_segment_replay_is_idempotent_and_conflict_retains_original(self):
        sid = self.store.create_session('dictation')
        args = (sid, 'microphone', 0, 0, 2, 'Generated', 'Original')
        first = self.store.add_segment(*args)
        self.assertEqual(first, self.store.add_segment(*args))
        for changes in [(0, 3, 'Generated', 'Original'), (0, 2, 'Changed', 'Original'),
                        (0, 2, 'Generated', 'Changed')]:
            with self.assertRaises(self.module.SegmentConflict):
                self.store.add_segment(sid, 'microphone', 0, *changes)
        self.assertEqual(len(self.store.get_session(sid)['segments']), 1)
        self.assertEqual(self.store.get_session(sid)['segments'][0]['raw_text'], 'Original')

    def test_interruption_recovers_only_active_recording_and_transcription(self):
        ids = {}
        for state in ('preparing', 'recording', 'captured', 'transcribing', 'complete', 'failed'):
            ids[state] = self.store.create_session('dictation')
            self.store.update_session(ids[state], state=state)
        self.assertEqual(set(self.store.recover_interrupted()), {ids['recording'], ids['transcribing']})
        self.assertEqual(self.store.recover_interrupted(), [])
        self.assertEqual(self.store.get_session(ids['captured'])['state'], 'captured')

    def test_search_uses_literal_bound_values_in_all_text_fields(self):
        title = self.store.create_session('note', title="100% _ ' OR 1=1 --")
        notes = self.store.create_session('note', notes='Generated needle')
        transcript = self.store.create_session('dictation')
        self.store.add_segment(transcript, 'microphone', 0, 0, 1, 'Other needle')
        self.assertEqual({r['id'] for r in self.store.list_sessions('needle')}, {notes, transcript})
        self.assertEqual([r['id'] for r in self.store.list_sessions("' OR 1=1 --")], [title])
        self.assertEqual([r['id'] for r in self.store.list_sessions('%')], [title])
        self.assertEqual([r['id'] for r in self.store.list_sessions('_')], [title])

    def test_summary_validates_references_without_harming_transcript(self):
        sid = self.store.create_session('meeting')
        segment = self.store.add_segment(sid, 'microphone', 0, 0, 1, 'Source')
        summary = {'items': [{'text': 'Generated summary', 'segment_ids': [segment]}]}
        self.store.update_session(sid, summary=summary)
        other = self.store.create_session('meeting')
        with self.assertRaises(ValueError):
            self.store.update_session(other, summary=summary)
        with self.assertRaises(ValueError):
            self.store.update_session(sid, title='Must roll back',
                                       summary={'items': [{'segment_ids': ['missing']}]})
        self.assertEqual(self.store.get_session(sid)['summary'], summary)
        self.assertEqual(self.store.get_session(sid)['title'], '')
        self.assertEqual(self.store.get_session(sid)['transcript'], 'Source')
        with self.assertRaises(ValueError):
            self.store.update_session(sid, summary='unstructured summary')

    def test_invalid_metadata_and_missing_parents_fail(self):
        with self.assertRaises(ValueError):
            self.store.create_session('cloud')
        sid = self.store.create_session('note')
        for fields in ({'state': 'unknown'}, {'created': 0}, {'title': None}):
            with self.assertRaises(ValueError):
                self.store.update_session(sid, **fields)
        with self.assertRaises((KeyError, sqlite3.IntegrityError)):
            self.store.add_segment('missing', 'microphone', 0, 0, 1, 'text')
        with self.assertRaises(ValueError):
            self.store.add_segment(sid, 'microphone', 0, float('nan'), 1, 'text')

    def test_private_permissions_and_database_durability_settings(self):
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0], 2)
            self.assertGreater(db.execute('PRAGMA busy_timeout').fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_concurrent_writers_use_independent_connections(self):
        sid = self.store.create_session('meeting')
        def add(ordinal):
            return self.store.add_segment(sid, 'microphone', ordinal, ordinal, ordinal + 1, str(ordinal))
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            ids = list(executor.map(add, list(range(30)) * 2))
        self.assertEqual(len(set(ids)), 30)
        self.assertEqual(len(self.store.get_session(sid)['segments']), 30)

    def test_dictionary_preserves_ids_tombstones_and_repeated_import(self):
        records = [{'id': 'word', 'word': 'GeneratedName', 'is_starred': True},
                   {'id': 'replace', 'word': 'wrong', 'replacement': 'right'},
                   {'id': 'snippet', 'word': 'trigger', 'replacement': 'A full phrase', 'is_snippet': True},
                   {'id': 'gone', 'word': 'RemovedName', 'is_deleted': True},
                   {'word': 'DerivedName'}]
        first = self.store.import_dictionary(records)
        self.assertEqual(first['inserted'], 5)
        self.assertEqual(self.store.import_dictionary(records)['unchanged'], 5)
        profile = self.store.personalization()
        self.assertEqual(set(profile['words']), {'GeneratedName', 'DerivedName'})
        self.assertEqual(profile['replacements'], {'wrong': 'right'})
        self.assertEqual(profile['snippets'], {'trigger': 'A full phrase'})
        self.assertNotIn('GeneratedName', json.dumps(first))
        self.store.import_dictionary([{'id': 'word', 'word': 'GeneratedName', 'is_deleted': True}])
        self.assertNotIn('GeneratedName', self.store.personalization()['words'])
        with self.store.transaction() as db:
            rows = {r['id']: dict(r) for r in db.execute('SELECT * FROM dictionary')}
        self.assertEqual(rows['gone']['is_deleted'], 1)
        self.assertEqual(rows['snippet']['is_snippet'], 1)
        self.assertTrue(any(key.startswith('derived:') for key in rows))


if __name__ == '__main__':
    unittest.main()
