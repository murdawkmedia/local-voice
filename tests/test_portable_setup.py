import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_voice.engine_selection import register_engine, select_engine
from local_voice.engine import EngineError


class PortableSetupTests(unittest.TestCase):
    def test_model_registration_rejects_linked_parent_without_writing(self):
        from tools.register_model import main
        state=self.root/'.local/share/local-voice';state.mkdir(parents=True)
        redirect=self.root/'elsewhere';model=redirect/'whisper-turbo-q5_0';model.mkdir(parents=True)
        (model/'ggml-generated.bin').write_bytes(b'generated model fixture')
        (state/'models').symlink_to(redirect, target_is_directory=True)
        with patch.object(Path,'home',return_value=self.root), patch('sys.argv',['register_model','--engine','whisper']):
            with self.assertRaises(ValueError): main()
        self.assertFalse((model/'manifest.json').exists())

    def test_model_registration_records_bytes_and_preserves_existing_manifest(self):
        from tools.register_model import main
        model=self.root/'.local/share/local-voice/models/whisper-turbo-q5_0';model.mkdir(parents=True)
        fixture=model/'ggml-generated.bin';fixture.write_bytes(b'generated model fixture')
        with patch.object(Path,'home',return_value=self.root), patch('sys.argv',['register_model','--engine','whisper']):
            main()
            before=(model/'manifest.json').read_bytes()
            with self.assertRaises(ValueError): main()
        data=json.loads(before)
        self.assertEqual(data['files'][fixture.name]['bytes'],fixture.stat().st_size)
        self.assertEqual((model/'manifest.json').read_bytes(),before)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.app = self.root/'app'
        self.source = self.root/'generated-engine'; self.source.write_bytes(b'generated executable')
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.fallback = {'whisper': ('qualified-engine', 'a'*64)}

    def test_explicit_local_registration_selects_verified_cpu_binary(self):
        register_engine(self.app, 'whisper', self.source, self.sha)
        name, expected, icd = select_engine(self.app, 'whisper', self.fallback, Path('/system/driver.json'))
        self.assertEqual(expected, self.sha); self.assertIsNone(icd)
        self.assertEqual((self.app/'engines'/name).read_bytes(), self.source.read_bytes())

    def test_mismatch_and_collision_preserve_existing_bytes(self):
        with self.assertRaises(ValueError): register_engine(self.app, 'whisper', self.source, 'b'*64)
        self.assertFalse(self.app.exists())
        register_engine(self.app, 'whisper', self.source, self.sha)
        self.source.write_bytes(b'different generated executable')
        with self.assertRaises(ValueError):
            register_engine(self.app, 'whisper', self.source, hashlib.sha256(self.source.read_bytes()).hexdigest())
        name, _, _ = select_engine(self.app, 'whisper', self.fallback, None)
        self.assertEqual((self.app/'engines'/name).read_bytes(), b'generated executable')

    def test_absent_manifest_preserves_qualified_fallback(self):
        self.assertEqual(select_engine(self.app, 'whisper', self.fallback, Path('/system/driver.json')),
                         ('qualified-engine', 'a'*64, Path('/system/driver.json')))

    def test_bad_manifest_and_symlinks_fail_closed(self):
        register_engine(self.app, 'whisper', self.source, self.sha)
        path = self.app/'engines/manifest.json'
        data = json.loads(path.read_text()); data['engines']['whisper']['file'] = '../escape'
        path.write_text(json.dumps(data))
        with self.assertRaises(EngineError): select_engine(self.app, 'whisper', self.fallback, None)
        path.unlink(); path.symlink_to(self.source)
        with self.assertRaises(EngineError): select_engine(self.app, 'whisper', self.fallback, None)
