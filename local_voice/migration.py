"""Explicit import boundary; credentials never enter the offline runtime."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3


class MigrationError(RuntimeError):
    """An unsupported or unsafe import source; never includes source text."""


PREFERENCE_DEFAULTS = {
    "automaticPaste": True,
    "mutePlayback": False,
    "meetingSystemAudio": True,
    "meetingSummary": False,
}


def _read_json(path):
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 1_048_576:
        raise MigrationError("Unsupported metadata file")
    try:
        return json.loads(path.read_text())
    except (ValueError, UnicodeError, OSError) as exc:
        raise MigrationError("Native metadata could not be inspected") from exc


def _audio_reference(root, value):
    if not isinstance(value, str) or not value:
        return "missingAudio"
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root):
        return "externalAudio"
    return "availableAudio" if path.is_file() else "missingAudio"


def inspect_native(source):
    """Return counts/schema/preferences only; no text, audio or credential reads."""
    root = Path(source).resolve()
    result = {
        "databasePresent": (root / "history.db").is_file(),
        "tables": {}, "schema": {}, "orphanLinks": 0,
        "preferences": dict(PREFERENCE_DEFAULTS),
        "audio": {"files": 0, "bytes": 0},
        "recovery": {"entries": 0, "availableAudio": 0,
                     "missingAudio": 0, "externalAudio": 0},
    }
    settings = _read_json(root / "settings.json")
    if isinstance(settings, dict):
        for key in PREFERENCE_DEFAULTS:
            if type(settings.get(key)) is bool:
                result["preferences"][key] = settings[key]
    recordings = root / "recordings"
    if recordings.is_dir() and not recordings.is_symlink():
        for path in recordings.glob("*.wav"):
            if path.is_file() and not path.is_symlink():
                result["audio"]["files"] += 1
                result["audio"]["bytes"] += path.stat().st_size
    recovery = _read_json(root / "recovery.json")
    if recovery is not None:
        if not isinstance(recovery, list) or len(recovery) > 10_000:
            raise MigrationError("Unsupported recovery metadata")
        for item in recovery:
            if not isinstance(item, dict):
                raise MigrationError("Unsupported recovery entry")
            result["recovery"]["entries"] += 1
            result["recovery"][_audio_reference(root, item.get("audio"))] += 1
    pending = _read_json(root / "pending.json")
    if pending is not None:
        if not isinstance(pending, dict):
            raise MigrationError("Unsupported pending metadata")
        result["pending"] = {"present": True,
                             "audioState": _audio_reference(root, pending.get("audio"))}
    if not result["databasePresent"]:
        return result
    database = root / 'history.db'
    if database.is_symlink() or any((root / ('history.db' + suffix)).exists()
                                    for suffix in ('-wal', '-shm', '-journal')):
        raise MigrationError('Close the legacy app before inspecting its database')
    with database.open('rb') as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:16] != b'SQLite format 3\0' or header[18:20] != b'\x01\x01':
        raise MigrationError('Closed rollback-journal history is required')
    try:
        with closing(sqlite3.connect((root / "history.db").as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"transcripts": {"id", "created", "text"},
                        "notes": {"id", "created", "title", "body"}}
            for name, columns in required.items():
                actual = {row[1] for row in db.execute("PRAGMA table_info(" + name + ")")}
                if name not in names or not columns.issubset(actual):
                    raise MigrationError("Unsupported native history schema")
            for name in ("transcripts", "notes", "note_transcripts", "meetings"):
                if name not in names:
                    continue
                result["schema"][name] = [row[1] for row in db.execute("PRAGMA table_info(" + name + ")")]
                result["tables"][name] = db.execute("SELECT count(*) FROM " + name).fetchone()[0]
            if "note_transcripts" in names:
                result["orphanLinks"] = db.execute("""
                    SELECT count(*) FROM note_transcripts l
                    LEFT JOIN notes n ON n.id=l.note_id
                    LEFT JOIN transcripts t ON t.id=l.transcript_id
                    WHERE n.id IS NULL OR t.id IS NULL
                """).fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise MigrationError("Native history could not be inspected") from exc
    return result
