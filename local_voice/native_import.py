"""Explicit bounded legacy import. Original state is retained; reports contain counts."""

from datetime import datetime
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time

from . import migration


MAX_DATABASE_BYTES = 512 * 1024 * 1024
MAX_ROWS = 100_000
MAX_AUDIO_FILES = 10_000
MAX_IMPORT_BYTES = 12 * 1024 ** 3
MAX_METADATA_BYTES = 1024 * 1024
MAX_SELECTED_TEXT_BYTES = 64 * 1024 * 1024
_SCHEMA = {
    'transcripts': {'id', 'created', 'text', 'kind', 'paste_status', 'operation_id'},
    'notes': {'id', 'created', 'updated', 'title', 'body'},
    'note_transcripts': {'note_id', 'transcript_id'},
    'meetings': {'note_id', 'operation_id', 'cloud_id', 'include_system', 'thoughts',
                 'status', 'summary', 'refined_transcript'},
}


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':'))


def _fingerprint(value):
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _timestamp(value):
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Ambiguous legacy timestamp')
        return parsed.timestamp()
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError('Unsupported legacy timestamp')
    return value


def _local_path(root, path):
    """Reject symlinks, including ancestors, without opening their targets."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(candidate))
    if not candidate.is_relative_to(root):
        return None
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return None
    return candidate


def _metadata(root, name):
    path = _local_path(root, name)
    if path is None:
        raise migration.MigrationError('Unsupported metadata path')
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_METADATA_BYTES:
        raise migration.MigrationError('Unsupported metadata file')
    with path.open('rb') as stream:
        payload = stream.read(MAX_METADATA_BYTES + 1)
    if len(payload) > MAX_METADATA_BYTES:
        raise migration.MigrationError('Unsupported metadata file')
    return json.loads(payload)


def _closed_database(root):
    for suffix in ('', '-wal', '-shm', '-journal'):
        path = root / ('history.db' + suffix)
        if path.is_symlink():
            raise migration.MigrationError('Unsupported native database path')
        if suffix and path.exists():
            raise migration.MigrationError('Close the legacy app and provide a checkpointed database')
    path = root / 'history.db'
    if path.exists() and (not path.is_file() or path.stat().st_size > MAX_DATABASE_BYTES):
        raise migration.MigrationError('Native database exceeds the supported import boundary')
    if path.exists():
        # A WAL-mode header may cause even mode=ro SQLite to create shared memory.
        with path.open('rb') as stream:
            header = stream.read(20)
        if len(header) >= 20 and header[18:20] != b'\x01\x01':
            raise migration.MigrationError('A closed rollback-journal source database is required')


def _snapshot(root, staging):
    _closed_database(root)
    original = root / 'history.db'
    if not original.exists():
        return None
    destination = staging / 'snapshot.db'
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)
    deadline = time.monotonic() + 10
    page_size = 0
    def progress(status, remaining, total):
        if time.monotonic() > deadline or total * page_size > MAX_DATABASE_BYTES:
            raise migration.MigrationError('Native snapshot exceeded its bounded read')
    # A rollback-journal database uses shared read locks; backup sees one consistent
    # committed snapshot. Neither this connection nor the preview can write data.
    with closing(sqlite3.connect(original.as_uri() + '?mode=ro', uri=True, timeout=2)) as source:
        source.execute('PRAGMA query_only=ON')
        page_size = source.execute('PRAGMA page_size').fetchone()[0]
        if source.execute('PRAGMA page_count').fetchone()[0] * page_size > MAX_DATABASE_BYTES:
            raise migration.MigrationError('Native snapshot is too large')
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target, pages=256, progress=progress, sleep=0.02)
    if destination.stat().st_size > MAX_DATABASE_BYTES:
        raise migration.MigrationError('Native snapshot is too large')
    return destination


def _load_snapshot(path):
    if path is None:
        return {key: [] for key in _SCHEMA}
    result = {}
    selected_bytes = 0
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names - set(_SCHEMA) - {'sqlite_sequence'} or not {'transcripts', 'notes'}.issubset(names):
            raise migration.MigrationError('Unsupported native history schema')
        for name, expected in _SCHEMA.items():
            if name not in names:
                result[name] = []
                continue
            actual = {r[1] for r in db.execute('PRAGMA table_info(' + name + ')')}
            if actual != expected:
                raise migration.MigrationError('Unsupported native history schema')
            if db.execute('SELECT count(*) FROM ' + name).fetchone()[0] > MAX_ROWS:
                raise migration.MigrationError('Native table exceeds the supported import boundary')
            # Do not select a meeting's account/cloud ID.
            columns = sorted(expected - {'cloud_id'})
            result[name] = []
            for row in db.execute('SELECT ' + ','.join(columns) + ' FROM ' + name):
                for value in row:
                    if isinstance(value, str):
                        if len(value) > MAX_METADATA_BYTES:
                            raise migration.MigrationError('Native text exceeds the supported import boundary')
                        size = len(value.encode('utf-8'))
                        if size > MAX_METADATA_BYTES:
                            raise migration.MigrationError('Native text exceeds the supported import boundary')
                        selected_bytes += size
                        if selected_bytes > MAX_SELECTED_TEXT_BYTES:
                            raise migration.MigrationError('Native text exceeds the supported import boundary')
                result[name].append(dict(row))
    for name, key in (('transcripts', 'id'), ('notes', 'id'), ('meetings', 'note_id')):
        identities = [str(r[key]) for r in result[name] if r[key] is not None]
        if len(identities) != len(result[name]) or len(set(identities)) != len(identities):
            raise migration.MigrationError('Invalid native record identities')
    note_ids = {str(r['id']) for r in result['notes']}
    transcript_ids = {str(r['id']) for r in result['transcripts']}
    if any(str(r['note_id']) not in note_ids or str(r['transcript_id']) not in transcript_ids
           for r in result['note_transcripts']):
        raise migration.MigrationError('Native history contains orphan links')
    if any(str(r['note_id']) not in note_ids for r in result['meetings']):
        raise migration.MigrationError('Native history contains orphan meetings')
    return result


def _sessions(rows):
    transcripts = {str(t['id']): t for t in rows['transcripts']}
    meetings = {str(m['note_id']): m for m in rows['meetings']}
    links = {}
    linked = set()
    for link in rows['note_transcripts']:
        note_id, transcript_id = str(link['note_id']), str(link['transcript_id'])
        links.setdefault(note_id, set()).add(transcript_id)
        linked.add(transcript_id)
    result = []
    for note in rows['notes']:
        note_id = str(note['id'])
        segments = [transcripts[tid] for tid in links.get(note_id, set())]
        segments.sort(key=lambda t: (_timestamp(t['created']), str(t['id'])))
        meeting = meetings.get(note_id)
        legacy = {'created': note['created'], 'updated': note['updated']}
        if meeting:
            legacy.update(meeting)
        legacy['transcripts'] = [{k: v for k, v in t.items() if k != 'text'} for t in segments]
        status = meeting['status'] if meeting else 'complete'
        state = 'complete' if status == 'complete' else 'failed' if status == 'failed' else 'interrupted'
        result.append(dict(key='wispr:note:' + note_id, kind='meeting' if segments or meeting else 'note',
                           created=_timestamp(note['created']), updated=_timestamp(note['updated']),
                           title=note['title'], notes=note['body'], state=state,
                           summary={'legacy': legacy}, segments=segments))
    for transcript_id, transcript in transcripts.items():
        if transcript_id in linked:
            continue
        result.append(dict(key='wispr:transcript:' + transcript_id, kind='dictation',
                           created=_timestamp(transcript['created']), updated=_timestamp(transcript['created']),
                           title='', notes='', state='complete',
                           summary={'legacy': {k: v for k, v in transcript.items() if k != 'text'}},
                           segments=[transcript]))
    return result


def _recovery(root):
    entries = []
    pending = _metadata(root, 'pending.json')
    if pending is not None:
        if not isinstance(pending, dict):
            raise migration.MigrationError('Unsupported pending metadata')
        entries.append(('wispr:pending', pending))
    recovery = _metadata(root, 'recovery.json')
    if recovery is not None:
        if not isinstance(recovery, list) or len(recovery) > MAX_AUDIO_FILES:
            raise migration.MigrationError('Unsupported recovery metadata')
        for index, entry in enumerate(recovery):
            if not isinstance(entry, dict):
                raise migration.MigrationError('Unsupported recovery entry')
            entries.append(('wispr:recovery:' + str(index), entry))
    return entries


def _stage_audio(root, staging, recovery, report):
    candidates = set()
    for _, entry in recovery:
        reference = entry.get('audio')
        if isinstance(reference, str) and reference:
            path = _local_path(root, reference)
            if path is not None and path.suffix.lower() == '.wav' and path.is_file():
                candidates.add(path)
    recordings = _local_path(root, 'recordings')
    if recordings is not None and recordings.is_dir():
        for index, path in enumerate(recordings.glob('*.wav')):
            if index >= MAX_AUDIO_FILES:
                raise migration.MigrationError('Too many native recordings')
            safe = _local_path(root, path)
            if safe is not None and safe.is_file():
                candidates.add(safe)
    if len(candidates) > MAX_AUDIO_FILES:
        raise migration.MigrationError('Too many native recordings')
    sizes = sum(path.stat().st_size for path in candidates)
    if sizes > MAX_IMPORT_BYTES:
        raise migration.MigrationError('Native audio exceeds the supported import boundary')
    # Root's migration operation supplies its larger operating reserve/capacity gate.
    # This local gate additionally prevents staging beyond presently available space.
    if shutil.disk_usage(staging).free < sizes + 2 * MAX_DATABASE_BYTES:
        raise migration.MigrationError('Insufficient free space for the bounded import')
    copied = {}
    for index, source in enumerate(sorted(candidates)):
        before = source.stat()
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        target = staging / ('audio-' + str(index) + '.wav')
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, 'rb') as reader:
            if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                raise migration.MigrationError('Unsupported audio file')
            with target.open('xb') as writer:
                target.chmod(0o600)
                while block := reader.read(1024 * 1024):
                    size += len(block)
                    if size > before.st_size:
                        raise migration.MigrationError('Native audio changed during import')
                    digest.update(block)
                    writer.write(block)
                writer.flush()
                os.fsync(writer.fileno())
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
            raise migration.MigrationError('Native audio changed during import')
        checksum = digest.hexdigest()
        if _file_hash(target) != checksum:
            raise migration.MigrationError('Imported audio verification failed')
        copied[source] = (target, checksum)
    states = []
    for key, entry in recovery:
        reference = entry.get('audio')
        path = _local_path(root, reference) if isinstance(reference, str) and reference else None
        status = 'external' if path is None and reference else 'available' if path in copied else 'missing'
        if status != 'available':
            report[status + '_audio'] += 1
        kept = {'audio_state': status}
        if path in copied:
            kept['audio'] = 'imported-audio/' + copied[path][1] + '.wav'
            kept['sha256'] = copied[path][1]
        for name in ('kind', 'noteId', 'operationId'):
            value = entry.get(name)
            if value is None or isinstance(value, (str, int)) and not isinstance(value, bool):
                kept[name] = value
        states.append((key, kept))
    return copied, states


def _file_hash(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_IMPORT_BYTES:
        raise migration.MigrationError('Unsupported imported audio file')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _import_sessions(store, db, sessions, report):
    for item in sessions:
        key = item['key']
        fingerprint = _fingerprint(item)
        old = db.execute('SELECT fingerprint FROM import_records WHERE legacy_key=?', (key,)).fetchone()
        existing = db.execute('SELECT id FROM sessions WHERE legacy_key=?', (key,)).fetchone()
        if old or existing:
            if old and existing and old[0] == fingerprint:
                report['sessions_unchanged'] += 1
            else:
                report['conflicts'] += 1
            continue
        sid = store._create_session(db, item['kind'], item['title'], item['created'], key, item['notes'])
        for ordinal, segment in enumerate(item['segments']):
            start = max(0, _timestamp(segment['created']) - item['created'])
            store._add_segment(db, sid, 'legacy', ordinal, start, start, segment['text'])
            report['segments_created'] += 1
        store._update_session(db, sid, state=item['state'], summary=item['summary'])
        db.execute('UPDATE sessions SET updated=? WHERE id=?', (item['updated'], sid))
        db.execute('INSERT INTO import_records VALUES(?,?)', (key, fingerprint))
        report['sessions_created'] += 1


def import_native(source_dir, store):
    """Import known history/recovery only; never account/session/credential files.

    Source must be closed, checkpointed and in rollback-journal mode. All database
    records commit together. Verified audio is installed first, so a crash can
    leave an unreferenced copy but cannot commit references to uninstalled audio.
    """
    original = Path(source_dir).absolute()
    root = original.resolve()
    if original.is_symlink() or not root.is_dir() or store.database_path.is_relative_to(root):
        raise migration.MigrationError('Separate source and private destination are required')
    _closed_database(root)
    preview = migration.inspect_native(root)
    if preview['orphanLinks']:
        raise migration.MigrationError('Native history contains orphan links')
    report = dict(sessions_created=0, sessions_unchanged=0, segments_created=0, conflicts=0,
                  audio_copied=0, audio_existing=0, missing_audio=0, external_audio=0,
                  state_created=0, state_unchanged=0, state_conflicts=0,
                  dictionary=dict(inserted=0, updated=0, unchanged=0, tombstones=0))
    promoted = []
    committed = False
    try:
        with tempfile.TemporaryDirectory(prefix='.native-import-', dir=store.database_path.parent) as temporary:
            staging = Path(temporary)
            staging.chmod(0o700)
            sessions = _sessions(_load_snapshot(_snapshot(root, staging)))
            recovery = _recovery(root)
            dictionary = _metadata(root, 'dictionary.json')
            if isinstance(dictionary, dict):
                if set(dictionary) != {'entries'}:
                    raise migration.MigrationError('Unsupported native dictionary wrapper')
                dictionary = dictionary['entries']
            if dictionary is not None and (not isinstance(dictionary, list) or len(dictionary) > MAX_ROWS):
                raise migration.MigrationError('Unsupported native dictionary')
            copied, states = _stage_audio(root, staging, recovery, report)
            with store.transaction() as db:
                _import_sessions(store, db, sessions, report)
                if dictionary is not None:
                    # Dictionary snapshot imports cannot overwrite later local changes.
                    fingerprint = _fingerprint(dictionary)
                    old = db.execute("SELECT fingerprint FROM import_records WHERE legacy_key='wispr:dictionary'").fetchone()
                    if old:
                        if old[0] == fingerprint:
                            report['dictionary']['unchanged'] = len(dictionary)
                            report['dictionary']['tombstones'] = sum(bool(r.get('is_deleted')) for r in dictionary)
                        else:
                            report['conflicts'] += 1
                    else:
                        report['dictionary'] = store._import_dictionary(db, dictionary)
                        db.execute('INSERT INTO import_records VALUES(?,?)', ('wispr:dictionary', fingerprint))
                for key, state in states:
                    payload = _encoded(state)
                    old = db.execute('SELECT payload FROM imported_state WHERE legacy_key=?', (key,)).fetchone()
                    if old:
                        report['state_unchanged' if old[0] == payload else 'state_conflicts'] += 1
                    else:
                        db.execute('INSERT INTO imported_state VALUES(?,?)', (key, payload))
                        report['state_created'] += 1
                destinations = {}
                for staged, checksum in copied.values():
                    destinations.setdefault(checksum, staged)
                if destinations:
                    directory = store.database_path.parent / 'imported-audio'
                    if directory.is_symlink():
                        raise migration.MigrationError('Unsupported imported audio directory')
                    directory.mkdir(mode=0o700, exist_ok=True)
                    directory.chmod(0o700)
                    for checksum, staged in destinations.items():
                        final = directory / (checksum + '.wav')
                        if final.is_symlink():
                            raise migration.MigrationError('Unsupported imported audio path')
                        if final.exists():
                            if not final.is_file() or _file_hash(final) != checksum:
                                raise migration.MigrationError('Existing imported audio failed verification')
                            final.chmod(0o600)
                            report['audio_existing'] += 1
                        else:
                            os.link(staged, final)
                            promoted.append(final)
                            report['audio_copied'] += 1
                    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            committed = True
        return report
    except migration.MigrationError:
        raise
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        # Causes can contain invalid source values (for example an ISO timestamp).
        raise migration.MigrationError('Native data could not be safely imported') from None
    finally:
        if not committed:
            for path in promoted:
                path.unlink(missing_ok=True)
