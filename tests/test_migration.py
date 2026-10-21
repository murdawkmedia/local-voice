import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_voice import migration


class MigrationPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        db = sqlite3.connect(self.root / "history.db")
        db.executescript("""
            CREATE TABLE transcripts(id TEXT PRIMARY KEY, created REAL, text TEXT,
              kind TEXT, paste_status TEXT, operation_id TEXT);
            CREATE TABLE notes(id TEXT PRIMARY KEY, created REAL, updated REAL,
              title TEXT, body TEXT);
            CREATE TABLE note_transcripts(note_id TEXT, transcript_id TEXT);
            CREATE TABLE meetings(note_id TEXT PRIMARY KEY, operation_id TEXT,
              cloud_id TEXT, include_system INTEGER, thoughts TEXT, status TEXT,
              summary TEXT, refined_transcript TEXT);
            INSERT INTO transcripts VALUES('t1',1,'generated phrase','dictation','copied','o1');
            INSERT INTO transcripts VALUES('t2',2,'generated meeting','meeting','saved','o2');
            INSERT INTO notes VALUES('n1',1,2,'generated title','generated note');
            INSERT INTO note_transcripts VALUES('n1','t2');
            INSERT INTO meetings VALUES('n1','o2',NULL,1,'','complete','','');
        """)
        db.commit()
        db.close()

    def inspect(self):
        self.assertTrue(callable(getattr(migration, "inspect_native", None)),
                        "The read-only migration preview is not implemented")
        return migration.inspect_native(self.root)

    def test_counts_without_returning_personal_text_or_changing_source(self):
        before = hashlib.sha256((self.root / "history.db").read_bytes()).hexdigest()
        (self.root / "session.json").write_bytes(b"\xffNEVER READ CREDENTIALS")
        result = self.inspect()
        self.assertEqual(result["tables"]["transcripts"], 2)
        self.assertEqual(result["tables"]["notes"], 1)
        self.assertEqual(result["orphanLinks"], 0)
        self.assertEqual(hashlib.sha256((self.root / "history.db").read_bytes()).hexdigest(), before)
        self.assertNotIn("generated", json.dumps(result))
        self.assertNotIn("NEVER READ", json.dumps(result))

    def test_missing_database_does_not_create_one(self):
        (self.root / "history.db").unlink()
        result = self.inspect()
        self.assertFalse(result["databasePresent"])
        self.assertFalse((self.root / "history.db").exists())

    def test_settings_copy_only_known_boolean_preferences(self):
        (self.root / "settings.json").write_text(json.dumps({
            "mutePlayback": False, "automaticPaste": False,
            "meetingSummary": False, "unrelated": "private", "access_token": "secret"
        }))
        result = self.inspect()
        self.assertFalse(result["preferences"]["mutePlayback"])
        self.assertFalse(result["preferences"]["automaticPaste"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))

    def test_unknown_schema_fails_without_creating_migration_data(self):
        db = sqlite3.connect(self.root / "history.db")
        db.execute("ALTER TABLE transcripts RENAME TO unsupported_records")
        db.close()
        self.assertTrue(hasattr(migration, "MigrationError"), "Missing migration failure type")
        with self.assertRaises(migration.MigrationError):
            self.inspect()

    def test_missing_and_external_audio_references_are_reported_not_read(self):
        self.assertTrue(hasattr(migration, "inspect_native"), "Missing migration preview")
        (self.root / "recordings").mkdir()
        (self.root / "recordings" / "retained.wav").write_bytes(b"generated wav metadata fixture")
        (self.root / "recovery.json").write_text(json.dumps([
            {"audio": str(self.root / "recordings" / "retained.wav")},
            {"audio": str(self.root / "recordings" / "missing.wav")},
            {"audio": "/etc/shadow"}
        ]))
        result = self.inspect()
        self.assertEqual(result["recovery"]["entries"], 3)
        self.assertEqual(result["recovery"]["missingAudio"], 1)
        self.assertEqual(result["recovery"]["externalAudio"], 1)
        self.assertEqual(result["audio"]["files"], 1)

    def test_preview_refuses_wal_before_creating_shared_memory_sidecars(self):
        db=sqlite3.connect(self.root/'history.db')
        db.execute('PRAGMA journal_mode=WAL')
        db.close()
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()}
        with self.assertRaises(migration.MigrationError):
            self.inspect()
        after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()}
        self.assertEqual(before,after)


if __name__ == "__main__":
    unittest.main()
