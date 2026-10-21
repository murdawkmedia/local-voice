"""Device-private, durable sessions. No connection is shared between operations."""

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid


KINDS = frozenset({'dictation', 'meeting', 'note'})
STATES = frozenset({'preparing', 'recording', 'captured', 'transcribing',
                    'complete', 'failed', 'interrupted'})
_NAMESPACE = uuid.UUID('e9558056-c83e-4696-813c-db62559182a3')
MAX_DICTIONARY_ENTRIES = 5000
MAX_TRANSCRIPT_SEGMENTS = 10_000
MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024


def _dictionary_text(value, maximum, multiline=False):
    value = _text(value)
    if (len(value.encode('utf-8')) > maximum or '\0' in value
            or (not multiline and any(c in value for c in '\r\n'))):
        raise ValueError('Invalid dictionary field')
    return value


class SegmentConflict(ValueError):
    """A segment replay or edit disagrees with retained data."""


def _text(value):
    if not isinstance(value, str):
        raise ValueError('Expected text')
    return value


def _transcript_text(value, maximum=MAX_TRANSCRIPT_BYTES):
    value = _text(value)
    if len(value) > maximum or '\0' in value or len(value.encode('utf-8')) > maximum:
        raise ValueError('Invalid transcript edit field')
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError('Expected finite timestamp')
    return value


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':'))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError('Expected JSON data') from exc


class Store:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path).absolute()
        if self.database_path.is_symlink() or self.database_path.parent.is_symlink():
            raise ValueError('Private database paths cannot be symlinks')
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database_path.parent.chmod(0o700)
        # Create the file privately before sqlite can create a permissive one.
        import os
        fd = os.open(self.database_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.database_path.chmod(0o600)
        with self.transaction() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('Unsupported local storage schema')
            for statement in (
                '''CREATE TABLE IF NOT EXISTS sessions(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    title TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'preparing', error TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '{}', legacy_key TEXT UNIQUE)''',
                '''CREATE TABLE IF NOT EXISTS segments(
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    source TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    start REAL NOT NULL, end REAL NOT NULL,
                    text TEXT NOT NULL, raw_text TEXT NOT NULL,
                    UNIQUE(session_id, source, ordinal))''',
                '''CREATE TABLE IF NOT EXISTS dictionary(
                    origin TEXT NOT NULL, id TEXT NOT NULL, word TEXT NOT NULL,
                    replacement TEXT NOT NULL, is_deleted INTEGER NOT NULL,
                    is_snippet INTEGER NOT NULL, is_starred INTEGER NOT NULL,
                    PRIMARY KEY(origin, id))''',
                '''CREATE TABLE IF NOT EXISTS import_records(
                    legacy_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS imported_state(
                    legacy_key TEXT PRIMARY KEY, payload TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS dictionary_overrides(
                    origin TEXT NOT NULL, id TEXT NOT NULL, edited REAL NOT NULL,
                    PRIMARY KEY(origin,id))''',
                '''CREATE TABLE IF NOT EXISTS segment_overrides(
                    segment_id TEXT PRIMARY KEY REFERENCES segments(id),
                    text TEXT NOT NULL, edited REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS audio_cleanup(
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                    state TEXT NOT NULL CHECK(state IN ('pending','removed')))''',
                'CREATE INDEX IF NOT EXISTS segments_session_time ON segments(session_id, start, source, ordinal)',
            ):
                db.execute(statement)
            db.execute('PRAGMA user_version=1')

    @contextmanager
    def transaction(self):
        """A fresh, bounded, serialized write transaction; also used by the importer."""
        db = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=5000')
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _create_session(self, db, kind, title='', created=None, legacy_key=None, notes=''):
        if kind not in KINDS:
            raise ValueError('Unknown session kind')
        _text(title)
        _text(notes)
        if legacy_key is not None:
            _text(legacy_key)
            old = db.execute('SELECT id FROM sessions WHERE legacy_key=?', (legacy_key,)).fetchone()
            if old:
                return old['id']
        created = time.time() if created is None else _number(created)
        sid = str(uuid.uuid5(_NAMESPACE, legacy_key)) if legacy_key is not None else str(uuid.uuid4())
        db.execute('''INSERT INTO sessions(id,kind,created,updated,title,notes,legacy_key)
                      VALUES(?,?,?,?,?,?,?)''', (sid, kind, created, created, title, notes, legacy_key))
        return sid

    def create_session(self, kind, title='', created=None, legacy_key=None, notes=''):
        with self.transaction() as db:
            return self._create_session(db, kind, title, created, legacy_key, notes)

    def _add_segment(self, db, session_id, source, ordinal, start, end, text, raw_text=None):
        _text(source)
        if not source or type(ordinal) is not int or ordinal < 0:
            raise ValueError('Invalid segment identity')
        _number(start)
        _number(end)
        if start < 0 or end < start:
            raise ValueError('Invalid segment interval')
        _text(text)
        raw_text = text if raw_text is None else _text(raw_text)
        payload = (start, end, text, raw_text)
        old = db.execute('''SELECT * FROM segments WHERE session_id=? AND source=? AND ordinal=?''',
                         (session_id, source, ordinal)).fetchone()
        if old:
            if tuple(old[key] for key in ('start', 'end', 'text', 'raw_text')) != payload:
                raise SegmentConflict('Segment replay conflicts with retained data')
            return old['id']
        segment_id = str(uuid.uuid5(_NAMESPACE, _json([session_id, source, ordinal])))
        db.execute('''INSERT INTO segments(id,session_id,source,ordinal,start,end,text,raw_text)
                      VALUES(?,?,?,?,?,?,?,?)''', (segment_id, session_id, source, ordinal, *payload))
        db.execute('UPDATE sessions SET updated=? WHERE id=?', (time.time(), session_id))
        return segment_id

    def add_segment(self, session_id, source, ordinal, start, end, text, raw_text=None):
        with self.transaction() as db:
            return self._add_segment(db, session_id, source, ordinal, start, end, text, raw_text)

    def _summary(self, db, session_id, value):
        if value is None or value == '':
            value = {}
        if not isinstance(value, dict):
            raise ValueError('A summary must be structured data')
        encoded = _json(value)
        references = set()
        def visit(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    if key == 'segment_ids':
                        if not isinstance(child, list) or any(not isinstance(ref, str) for ref in child):
                            raise ValueError('Invalid summary source references')
                        references.update(child)
                    elif key == 'segment_id':
                        if not isinstance(child, str):
                            raise ValueError('Invalid summary source reference')
                        references.add(child)
                    else:
                        visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)
        visit(value)
        present = {r[0] for r in db.execute('SELECT id FROM segments WHERE session_id=?', (session_id,))}
        if references - present:
            raise ValueError('Summary references an unavailable source segment')
        return encoded

    def _update_session(self, db, session_id, **fields):
        if not fields or fields.keys() - {'title', 'notes', 'state', 'error', 'summary'}:
            raise ValueError('Unsupported session update')
        if not db.execute('SELECT 1 FROM sessions WHERE id=?', (session_id,)).fetchone():
            raise KeyError('Unknown session')
        values = {}
        for key, value in fields.items():
            if key == 'summary':
                values[key] = self._summary(db, session_id, value)
            else:
                values[key] = _text(value)
        if 'state' in values and values['state'] not in STATES:
            raise ValueError('Unknown session state')
        values['updated'] = time.time()
        db.execute('UPDATE sessions SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?',
                   (*values.values(), session_id))

    def update_session(self, session_id, **fields):
        with self.transaction() as db:
            self._update_session(db, session_id, **fields)

    def edit_transcript(self, session_id, edits):
        """Atomically compare and replace effective text; recognition stays immutable."""
        if not _transcript_text(session_id, 256):
            raise ValueError('Invalid transcript edit target')
        if not isinstance(edits, list) or len(edits) > MAX_TRANSCRIPT_SEGMENTS:
            raise ValueError('Invalid transcript edit batch')
        selected, seen, total = [], set(), 0
        for item in edits:
            if not isinstance(item, dict) or item.keys() != {'id', 'expected_text', 'text'}:
                raise ValueError('Invalid transcript edit')
            identifier = _transcript_text(item['id'], 256)
            expected = _transcript_text(item['expected_text'])
            text = _transcript_text(item['text'])
            total += sum(len(value.encode('utf-8')) for value in (identifier, expected, text))
            if not identifier or identifier in seen or total > MAX_TRANSCRIPT_BYTES:
                raise ValueError('Invalid transcript edit batch')
            seen.add(identifier)
            selected.append((identifier, expected, text))
        with self.transaction() as db:
            session = db.execute('SELECT state,summary FROM sessions WHERE id=?', (session_id,)).fetchone()
            if session is None:
                raise KeyError('Unknown session')
            if session['state'] not in {'complete', 'failed', 'interrupted'}:
                raise ValueError('Transcript is not editable in this state')
            changes = []
            for identifier, expected, text in selected:
                row = db.execute('''SELECT t.session_id,COALESCE(o.text,t.text) AS text
                    FROM segments t LEFT JOIN segment_overrides o ON o.segment_id=t.id
                    WHERE t.id=?''', (identifier,)).fetchone()
                if row is None or row['session_id'] != session_id:
                    raise ValueError('Transcript edit target is unavailable')
                if row['text'] != expected:
                    raise SegmentConflict('Transcript changed before this edit')
                if text != row['text']:
                    changes.append((identifier, text))
            summary = json.loads(session['summary'])
            if changes:
                now = time.time()
                for identifier, text in changes:
                    db.execute('''INSERT INTO segment_overrides VALUES(?,?,?)
                        ON CONFLICT(segment_id) DO UPDATE SET text=excluded.text,edited=excluded.edited''',
                               (identifier, text, now))
                if summary:
                    summary['stale'] = True
                db.execute('UPDATE sessions SET updated=?,summary=? WHERE id=?',
                           (now, _json(summary), session_id))
            return {'edited': len(changes), 'summaryStale': bool(summary.get('stale', False))}

    def save_summary_if_current(self, session_id, summary, expected_segments):
        """Save a model result only while its complete, effective input still matches."""
        if not _transcript_text(session_id, 256):
            raise ValueError('Invalid summary target')
        if (not isinstance(expected_segments, (list, tuple))
                or len(expected_segments) > MAX_TRANSCRIPT_SEGMENTS):
            raise ValueError('Invalid summary source snapshot')
        expected, seen, total = [], set(), 0
        for item in expected_segments:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError('Invalid summary source snapshot')
            identifier = _transcript_text(item[0], 256)
            text = _transcript_text(item[1])
            total += len(identifier.encode('utf-8')) + len(text.encode('utf-8'))
            if not identifier or identifier in seen or total > MAX_TRANSCRIPT_BYTES:
                raise ValueError('Invalid summary source snapshot')
            seen.add(identifier)
            expected.append((identifier, text))
        with self.transaction() as db:
            row = db.execute('SELECT state FROM sessions WHERE id=?', (session_id,)).fetchone()
            if row is None:
                raise KeyError('Unknown session')
            if row['state'] != 'complete':
                return False
            current = [tuple(row) for row in db.execute('''
                SELECT t.id,COALESCE(o.text,t.text) FROM segments t
                LEFT JOIN segment_overrides o ON o.segment_id=t.id
                WHERE t.session_id=? ORDER BY t.start,t.source,t.ordinal LIMIT ?''',
                (session_id, MAX_TRANSCRIPT_SEGMENTS + 1))]
            if current != expected:
                return False
            self._update_session(db, session_id, summary=summary)
            return True

    def _get_session(self, db, session_id):
        row = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
        if row is None:
            raise KeyError('Unknown session')
        result = dict(row)
        audio = db.execute('SELECT state FROM audio_cleanup WHERE session_id=?', (session_id,)).fetchone()
        result['audio_state'] = audio['state'] if audio else 'retained'
        result['summary'] = json.loads(result['summary'])
        result['segments'] = []
        for row in db.execute('''SELECT t.*,o.text AS override_text FROM segments t
                LEFT JOIN segment_overrides o ON o.segment_id=t.id
                WHERE t.session_id=? ORDER BY t.start,t.source,t.ordinal''', (session_id,)):
            segment = dict(row)
            override = segment.pop('override_text')
            segment['recognized_text'] = segment['text']
            segment['edited'] = override is not None
            if segment['edited']:
                segment['text'] = override
            result['segments'].append(segment)
        result['transcript'] = '\n'.join(s['text'] for s in result['segments'])
        return result

    def get_session(self, session_id):
        with self.transaction() as db:
            return self._get_session(db, session_id)

    def begin_audio_cleanup(self, session_id):
        """Commit intent only after a native success with nonempty saved recognition."""
        with self.transaction() as db:
            eligible = db.execute('''SELECT 1 FROM sessions s WHERE s.id=?
                AND s.state='complete' AND s.kind IN ('dictation','meeting')
                AND s.legacy_key IS NULL AND EXISTS(SELECT 1 FROM segments t
                    WHERE t.session_id=s.id AND LENGTH(TRIM(t.text, char(9)||char(10)||char(13)||' '))>0)''',
                (session_id,)).fetchone()
            if not eligible:
                return False
            db.execute("INSERT OR IGNORE INTO audio_cleanup VALUES(?,'pending')", (session_id,))
            return True

    def finish_audio_cleanup(self, session_id):
        with self.transaction() as db:
            db.execute("UPDATE audio_cleanup SET state='removed' WHERE session_id=?", (session_id,))

    def audio_cleanup_candidates(self, after='', limit=32):
        if not isinstance(after, str) or type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError('Invalid cleanup page')
        with self.transaction() as db:
            return [r[0] for r in db.execute('''SELECT s.id FROM sessions s
                LEFT JOIN audio_cleanup a ON a.session_id=s.id
                WHERE s.id>? AND s.state='complete' AND s.kind IN ('dictation','meeting')
                AND s.legacy_key IS NULL AND COALESCE(a.state,'retained')<>'removed'
                AND EXISTS(SELECT 1 FROM segments t WHERE t.session_id=s.id
                    AND LENGTH(TRIM(t.text, char(9)||char(10)||char(13)||' '))>0)
                ORDER BY s.id LIMIT ?''', (after, limit))]

    def list_sessions(self, query=''):
        _text(query)
        literal = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        with self.transaction() as db:
            return [dict(r) | {'summary': json.loads(r['summary'])} for r in db.execute('''
                SELECT s.* FROM sessions s
                WHERE s.title LIKE ? ESCAPE '\\' OR s.notes LIKE ? ESCAPE '\\'
                  OR EXISTS(SELECT 1 FROM segments t
                            LEFT JOIN segment_overrides o ON o.segment_id=t.id
                            WHERE t.session_id=s.id
                            AND (t.text LIKE ? ESCAPE '\\' OR t.raw_text LIKE ? ESCAPE '\\'
                                 OR o.text LIKE ? ESCAPE '\\'))
                ORDER BY s.created DESC,s.id''', (literal,) * 5)]

    def recover_interrupted(self):
        with self.transaction() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM sessions WHERE state IN ('recording','transcribing') ORDER BY id")]
            db.execute("UPDATE sessions SET state='interrupted',updated=? WHERE state IN ('recording','transcribing')",
                       (time.time(),))
            return ids

    def _import_dictionary(self, db, records, origin='wispr'):
        _dictionary_text(origin, 256)
        if not origin:
            raise ValueError('Invalid dictionary origin')
        counts = dict(inserted=0, updated=0, unchanged=0, tombstones=0)
        retained = db.execute('SELECT COUNT(*) FROM dictionary').fetchone()[0]
        for item in records:
            if not isinstance(item, dict):
                raise ValueError('Unsupported dictionary entry')
            word = _dictionary_text(item.get('word', ''), 512)
            replacement = _dictionary_text(item.get('replacement') or '', 8192, multiline=True)
            flags = []
            for name in ('is_deleted', 'is_snippet', 'is_starred'):
                value = item.get(name, False)
                if type(value) not in (bool, int) or value not in (0, 1):
                    raise ValueError('Unsupported dictionary flag')
                flags.append(int(value))
            identifier = item.get('id')
            if identifier is None or identifier == '':
                identity = _json([word, replacement, flags[1]])
                identifier = 'derived:' + hashlib.sha256(identity.encode()).hexdigest()
            elif not isinstance(identifier, (str, int)) or isinstance(identifier, bool):
                raise ValueError('Unsupported dictionary identity')
            identifier = str(identifier)
            _dictionary_text(identifier, 256)
            payload = (word, replacement, *flags)
            old = db.execute('SELECT * FROM dictionary WHERE origin=? AND id=?', (origin, identifier)).fetchone()
            if old is None:
                if retained >= MAX_DICTIONARY_ENTRIES:
                    raise ValueError('Dictionary entry limit reached')
                db.execute('INSERT INTO dictionary VALUES(?,?,?,?,?,?,?)', (origin, identifier, *payload))
                retained += 1
                counts['inserted'] += 1
            elif db.execute('SELECT 1 FROM dictionary_overrides WHERE origin=? AND id=?',
                            (origin, identifier)).fetchone():
                counts['unchanged'] += 1
            elif tuple(old[k] for k in ('word', 'replacement', 'is_deleted', 'is_snippet', 'is_starred')) == payload:
                counts['unchanged'] += 1
            else:
                db.execute('''UPDATE dictionary SET word=?,replacement=?,is_deleted=?,is_snippet=?,is_starred=?
                              WHERE origin=? AND id=?''', (*payload, origin, identifier))
                counts['updated'] += 1
            counts['tombstones'] += flags[0]
        return counts

    def import_dictionary(self, records, origin='wispr'):
        with self.transaction() as db:
            return self._import_dictionary(db, records, origin)

    def _dictionary_rows(self, db, include_deleted=False):
        rows = [dict(row) for row in db.execute('''SELECT d.*,o.edited AS locally_edited
            FROM dictionary d LEFT JOIN dictionary_overrides o ON d.origin=o.origin AND d.id=o.id
            ORDER BY CASE WHEN o.id IS NOT NULL THEN 0 ELSE 1 END,d.origin,d.id LIMIT ?''',
            (MAX_DICTIONARY_ENTRIES + 1,))]
        if len(rows) > MAX_DICTIONARY_ENTRIES:
            raise ValueError('Dictionary entry limit reached')
        counts = {}
        for row in rows:
            if not row['is_deleted'] and row['word']:
                key = row['word'].strip().casefold()
                counts[key] = counts.get(key, 0) + 1
        used = set()
        for row in rows:
            key = row['word'].strip().casefold()
            row['effective'] = bool(not row['is_deleted'] and key and key not in used)
            row['duplicate'] = bool(not row['is_deleted'] and counts.get(key, 0) > 1)
            row['locally_edited'] = row['locally_edited'] is not None
            if row['effective']:
                used.add(key)
        return [row for row in rows if include_deleted or not row['is_deleted']]

    def list_dictionary(self, include_deleted=False):
        """Private editor rows; identities survive edits and repeat imports."""
        with self.transaction() as db:
            return self._dictionary_rows(db, include_deleted)

    def save_dictionary_entry(self, *, word, replacement='', is_snippet=False,
                              is_starred=False, origin=None, entry_id=None):
        word = _dictionary_text(word, 512).strip()
        replacement = _dictionary_text(replacement, 8192, multiline=True)
        if (not word or type(is_snippet) is not bool or type(is_starred) is not bool
                or (is_snippet and not replacement.strip())):
            raise ValueError('Invalid dictionary entry')
        new = origin is None and entry_id is None
        if new:
            origin, entry_id = 'local', str(uuid.uuid4())
        elif origin is None or entry_id is None:
            raise ValueError('Invalid dictionary identity')
        _dictionary_text(origin, 256); _dictionary_text(entry_id, 256)
        with self.transaction() as db:
            old = db.execute('SELECT 1 FROM dictionary WHERE origin=? AND id=?', (origin, entry_id)).fetchone()
            if not new and old is None:
                raise KeyError('Unknown dictionary entry')
            rows = self._dictionary_rows(db, include_deleted=True)
            if new and len(rows) >= MAX_DICTIONARY_ENTRIES:
                raise ValueError('Dictionary entry limit reached')
            if any(not row['is_deleted'] and row['word'].strip().casefold() == word.casefold()
                   and (row['origin'], row['id']) != (origin, entry_id) for row in rows):
                raise ValueError('An active dictionary trigger already exists')
            db.execute('''INSERT INTO dictionary VALUES(?,?,?,?,0,?,?)
                ON CONFLICT(origin,id) DO UPDATE SET word=excluded.word,replacement=excluded.replacement,
                is_deleted=0,is_snippet=excluded.is_snippet,is_starred=excluded.is_starred''',
                (origin, entry_id, word, replacement, int(is_snippet), int(is_starred)))
            db.execute('INSERT OR REPLACE INTO dictionary_overrides VALUES(?,?,?)',
                       (origin, entry_id, time.time()))
            return next(row for row in self._dictionary_rows(db) if (row['origin'], row['id']) == (origin, entry_id))

    def delete_dictionary_entry(self, origin, entry_id):
        _dictionary_text(origin, 256); _dictionary_text(entry_id, 256)
        with self.transaction() as db:
            changed = db.execute('UPDATE dictionary SET is_deleted=1 WHERE origin=? AND id=?', (origin, entry_id))
            if not changed.rowcount:
                raise KeyError('Unknown dictionary entry')
            db.execute('INSERT OR REPLACE INTO dictionary_overrides VALUES(?,?,?)',
                       (origin, entry_id, time.time()))

    def personalization(self):
        """Return private vocabulary to an in-process caller; never diagnostics."""
        result = {'words': [], 'replacements': {}, 'snippets': {}}
        with self.transaction() as db:
            for item in self._dictionary_rows(db):
                if not item['effective']:
                    continue
                if item['is_snippet']:
                    result['snippets'][item['word']] = item['replacement']
                elif item['replacement']:
                    result['replacements'][item['word']] = item['replacement']
                elif item['word'] not in result['words']:
                    result['words'].append(item['word'])
        return result
