import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from local_voice.runtime import SpeechRuntime
from local_voice.engine import EngineError
from local_voice.settings import Settings
from local_voice.store import Store


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.settings=Settings(self.root/'config/settings.json')
        self.store=Store(self.root/'state/sessions.db')
        self.runtime=SpeechRuntime(self.root/'app',self.root/'state',self.settings,self.store,threading.Lock())

    def test_missing_binary_never_invokes_engine_or_downloads(self):
        with patch('local_voice.runtime.LocalEngine') as constructor:
            with self.assertRaises(EngineError):
                self.runtime.transcribe(self.root/'generated.wav',threading.Event())
            constructor.assert_not_called()
        self.assertFalse((self.root/'app').exists())

    def test_verified_selection_and_hints_are_private_to_engine_spec(self):
        data=b'generated executable';name='generated-engine'
        folder=self.root/'app/engines';folder.mkdir(parents=True)
        (folder/name).write_bytes(data);(folder/name).chmod(0o700)
        self.runtime.ENGINE_FILES={'whisper':(name,hashlib.sha256(data).hexdigest())}
        self.settings.update(engine='whisper')
        self.store.import_dictionary([{'id':'g','word':'GeneratedProperName','replacement':''}])
        with patch('local_voice.runtime.LocalEngine') as constructor:
            constructor.return_value.transcribe.return_value='Generated output'
            value=self.runtime.transcribe(self.root/'generated.wav',threading.Event())
            self.assertEqual(value,'Generated output')
            spec=constructor.call_args.args[0]
            self.assertEqual(spec.kind,'whisper')
            self.assertEqual(spec.vulkan_icd,Path('/usr/share/vulkan/icd.d/nvidia_icd.json'))
            self.assertIn('GeneratedProperName',spec.prompt)
            self.assertNotIn('GeneratedProperName',str(self.runtime.diagnostics()))

    def test_parakeet_cpu_does_not_request_a_vulkan_driver(self):
        data=b'generated executable';name='generated-cpu'
        folder=self.root/'app/engines';folder.mkdir(parents=True)
        (folder/name).write_bytes(data);(folder/name).chmod(0o700)
        self.runtime.ENGINE_FILES={'parakeet':(name,hashlib.sha256(data).hexdigest())}
        self.settings.update(engine='parakeet')
        with patch('local_voice.runtime.LocalEngine') as constructor:
            self.runtime.transcribe(self.root/'generated.wav',threading.Event())
            self.assertIsNone(constructor.call_args.args[0].vulkan_icd)

    def test_cancel_while_waiting_for_summary_lock_never_loads_speech(self):
        self.runtime.model_lock.acquire()
        cancel=threading.Event();cancel.set()
        with self.assertRaises(EngineError) as caught:
            self.runtime.transcribe(self.root/'generated.wav',cancel)
        self.runtime.model_lock.release()
        self.assertEqual(caught.exception.category,'cancelled')


if __name__=='__main__':unittest.main()
