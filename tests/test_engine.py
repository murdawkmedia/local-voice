import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import signal
import subprocess
import time
import unittest
from unittest.mock import Mock, patch
import wave

from local_voice import engine


class EngineBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.model = self.root / "model"
        self.model.mkdir()
        files = {}
        for name in ("encoder-model.int8.onnx", "decoder_joint-model.int8.onnx", "vocab.txt", "config.json"):
            data = b"generated test asset"
            (self.model / name).write_bytes(data)
            files[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        (self.model / "manifest.json").write_text(json.dumps({"files": files}))
        self.wav = self.root / "audio.wav"
        with wave.open(str(self.wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x01\0" * 1600)
        self.binary = self.root / "fake-engine"

    def fake(self, body):
        self.binary.write_text("#!/usr/bin/python3\nimport sys,json,time,os\nfrom pathlib import Path\n" + body)
        self.binary.chmod(0o700)

    def make(self, **kw):
        self.assertTrue(hasattr(engine, "LocalEngine"), "Local engine adapter is missing")
        spec = engine.EngineSpec(kind="parakeet", binary=self.binary,
                                 model_dir=self.model, timeout=kw.get("timeout", 5))
        return engine.LocalEngine(spec, work_dir=self.root / "jobs")

    def success(self, text):
        payload = {"schemaVersion": 1, "status": "success" if text else "empty",
                   "text": text, "engine": "parakeet"}
        self.fake("p=Path(sys.argv[sys.argv.index('--result-json')+1]);p.write_text(" + repr(json.dumps(payload)) + ")\n")

    def test_machine_result_preserves_unicode_newlines_and_spaces(self):
        value = "  Caffè\n第二行  "
        self.success(value)
        e = self.make()
        self.assertEqual(e.transcribe(self.wav, threading.Event()), value)
        self.assertEqual(e.last_run["outcome"], "success")
        self.assertNotIn(value, json.dumps(e.last_run))

    def test_missing_or_modified_model_prevents_process_start(self):
        self.success("generated")
        (self.model / "encoder-model.int8.onnx").write_bytes(b"modified")
        self.assertTrue(hasattr(engine, "EngineError"), "Missing engine failure type")
        with self.assertRaises(engine.EngineError) as caught:
            self.make().transcribe(self.wav, threading.Event())
        self.assertEqual(caught.exception.category, "setup")

    def test_unknown_json_does_not_fall_back_to_stdout(self):
        self.fake("print('This is not a result protocol');Path(sys.argv[sys.argv.index('--result-json')+1]).write_text('{}')\n")
        e = self.make()
        with self.assertRaises(engine.EngineError) as caught:
            e.transcribe(self.wav, threading.Event())
        self.assertEqual(caught.exception.category, "protocol")

    def test_timeout_reaps_owned_process(self):
        pidfile = self.root / "pid"
        self.fake("Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid()));time.sleep(20)\n")
        e = self.make(timeout=0.2)
        with self.assertRaises(engine.EngineError) as caught:
            e.transcribe(self.wav, threading.Event())
        self.assertEqual(caught.exception.category, "timeout")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_pre_cancel_does_not_run_engine(self):
        self.success("must never return")
        cancel = threading.Event()
        cancel.set()
        e = self.make()
        with self.assertRaises(engine.EngineError) as caught:
            e.transcribe(self.wav, cancel)
        self.assertEqual(caught.exception.category, "cancelled")

    def test_empty_result_is_explicit(self):
        self.success("")
        e = self.make()
        self.assertEqual(e.transcribe(self.wav, threading.Event()), "")
        self.assertEqual(e.last_run["outcome"], "empty")

    def test_nonzero_exit_cannot_publish_success(self):
        self.success("generated")
        self.binary.write_text(self.binary.read_text() + "sys.exit(7)\n")
        e = self.make()
        with self.assertRaises(engine.EngineError) as caught:
            e.transcribe(self.wav, threading.Event())
        self.assertEqual(caught.exception.category, "process")

    def test_result_from_wrong_engine_is_rejected(self):
        self.fake("Path(sys.argv[sys.argv.index('--result-json')+1]).write_text(json.dumps({'schemaVersion':1,'status':'success','text':'generated','engine':'remote'}))\n")
        e = self.make()
        with self.assertRaises(engine.EngineError) as caught:
            e.transcribe(self.wav, threading.Event())
        self.assertEqual(caught.exception.category, "protocol")

    def test_fast_success_cannot_bypass_diagnostic_limit(self):
        self.success("generated")
        self.binary.write_text(self.binary.read_text() + "sys.stderr.write('x'*100000);sys.stderr.flush()\n")
        e = self.make()
        with self.assertRaises(engine.EngineError) as caught:
            e.transcribe(self.wav, threading.Event())
        self.assertEqual(caught.exception.category, "diagnostic-limit")

    def test_successful_worker_cannot_leave_diagnostic_holding_helper(self):
        pidfile = self.root / 'helper-pid'
        self.success('generated')
        self.binary.write_text(self.binary.read_text() +
            "\nimport signal\npid=os.fork()\nif pid == 0:\n signal.signal(signal.SIGTERM,signal.SIG_IGN)\n Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid()))\n time.sleep(20)\n os._exit(0)\nwhile not Path(" + repr(str(pidfile)) + ").exists(): time.sleep(.001)\n")
        e = self.make()
        started = time.monotonic()
        self.assertEqual(e.transcribe(self.wav, threading.Event()), 'generated')
        self.assertLess(time.monotonic() - started, 2)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_exit_race_during_kill_escalation_is_reaped(self):
        process = Mock(pid=987654)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('generated', 2), 0]
        with patch.object(engine.os, 'killpg', side_effect=[None, ProcessLookupError()]):
            self.assertTrue(engine.LocalEngine._stop(process))
        self.assertEqual(process.wait.call_count, 2)

    def test_whisper_word_hints_travel_only_in_private_configuration(self):
        self.fake("import tomllib\np=Path(sys.argv[sys.argv.index('--config')+1]);hint=tomllib.loads(p.read_text())['whisper']['initial_prompt'];assert p.stat().st_mode & 0o777 == 0o600;assert hint not in sys.argv;Path(sys.argv[sys.argv.index('--result-json')+1]).write_text(json.dumps({'schemaVersion':1,'status':'success','text':hint,'engine':'whisper'}))\n")
        data=b'generated weights';name='ggml-generated.bin'
        (self.model/name).write_bytes(data)
        (self.model/'manifest.json').write_text(json.dumps({'files':{name:{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}}}))
        spec=engine.EngineSpec('whisper',self.binary,self.model,prompt='GeneratedName, Caffè')
        e=engine.LocalEngine(spec,self.root/'jobs')
        self.assertEqual(e.transcribe(self.wav,threading.Event()),'GeneratedName, Caffè')
        self.assertNotIn('GeneratedName',json.dumps(e.last_run))

    def test_native_engine_has_hard_memory_scope(self):
        self.fake("group=next(line.split(':',2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'));limit=(Path('/sys/fs/cgroup')/group.lstrip('/')/'memory.max').read_text().strip();Path(sys.argv[sys.argv.index('--result-json')+1]).write_text(json.dumps({'schemaVersion':1,'status':'success','text':limit,'engine':'parakeet'}))\n")
        e=self.make()
        self.assertEqual(e.transcribe(self.wav,threading.Event()),str(2*1024**3))

    def test_exact_digital_silence_cannot_create_a_hallucinated_transcript(self):
        self.success('Generated hallucinated text')
        with wave.open(str(self.wav),'wb') as output:
            output.setparams((1,2,16000,0,'NONE','not compressed'))
            output.writeframes(b'\0\0'*1600)
        e=self.make()
        self.assertEqual(e.transcribe(self.wav,threading.Event()),'')
        self.assertEqual(e.last_run['reason'],'digital-silence')

    def whisper_model(self):
        data = b'generated weights'; name = 'ggml-generated.bin'
        (self.model / name).write_bytes(data)
        (self.model / 'manifest.json').write_text(json.dumps({'files': {
            name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}}))

    def test_verified_system_icd_and_gpu_index_reach_only_owned_child(self):
        icd = Path('/usr/share/vulkan/icd.d/nvidia_icd.json')
        if not icd.exists():
            self.skipTest('NVIDIA system manifest is not installed')
        self.whisper_model()
        self.fake("import tomllib\nc=tomllib.loads(Path(sys.argv[sys.argv.index('--config')+1]).read_text());value=json.dumps({'driver':os.environ.get('VK_DRIVER_FILES'),'device':c['whisper'].get('gpu_device'),'extra':[k for k in os.environ if k.startswith(('VK_', 'VOXTYPE_')) and k != 'VK_DRIVER_FILES']});Path(sys.argv[sys.argv.index('--result-json')+1]).write_text(json.dumps({'schemaVersion':1,'status':'success','text':value,'engine':'whisper'}))\n")
        spec = engine.EngineSpec('whisper', self.binary, self.model, vulkan_icd=icd)
        e = engine.LocalEngine(spec, self.root / 'jobs')
        with patch.dict(os.environ, {'VK_DRIVER_FILES': '/unreviewed/driver.json',
                                    'VK_ICD_FILENAMES': '/unreviewed/other.json',
                                    'VK_LOADER_DRIVERS_SELECT': '*',
                                    'VOXTYPE_MODEL': 'unreviewed'}):
            result = json.loads(e.transcribe(self.wav, threading.Event()))
            self.assertEqual(os.environ['VK_DRIVER_FILES'], '/unreviewed/driver.json')
        self.assertEqual(result, {'driver': str(icd), 'device': 0, 'extra': []})
        self.assertEqual(e.last_run['gpuSelection'], 'vulkan-device-0')

    def test_missing_corrupt_writable_or_non_system_icd_fails_before_launch(self):
        self.whisper_model(); self.success('must not run')
        missing = self.root / 'missing.json'
        corrupt = self.root / 'corrupt.json'; corrupt.write_text('{')
        user_file = self.root / 'user.json'
        user_file.write_text(json.dumps({'file_format_version': '1.0.1', 'ICD': {
            'library_path': 'libGLX_nvidia.so.0', 'api_version': '1.4.351'}}))
        user_file.chmod(0o444)
        for path in (missing, corrupt, user_file, Path('relative.json')):
            with self.subTest(path=path), patch.object(engine.subprocess, 'Popen') as spawn:
                with self.assertRaises(engine.EngineError) as caught:
                    engine.LocalEngine(engine.EngineSpec('whisper', self.binary,
                        self.model, vulkan_icd=path), self.root / 'jobs')
                self.assertEqual(caught.exception.category, 'setup')
                spawn.assert_not_called()

    def test_cpu_engine_rejects_vulkan_selection(self):
        self.success('must not run')
        with self.assertRaises(engine.EngineError):
            engine.LocalEngine(engine.EngineSpec('parakeet', self.binary,
                self.model, vulkan_icd=Path('/usr/share/vulkan/icd.d/nvidia_icd.json')),
                self.root / 'jobs')

    def test_installed_icd_is_revalidated_and_corruption_never_starts_worker(self):
        icd = Path('/usr/share/vulkan/icd.d/nvidia_icd.json')
        if not icd.exists():
            self.skipTest('NVIDIA system manifest is not installed')
        self.whisper_model(); self.success('must not run')
        e = engine.LocalEngine(engine.EngineSpec('whisper', self.binary,
            self.model, vulkan_icd=icd), self.root / 'jobs')
        original = Path.open
        for malformed in ('{', '{}', '{"file_format_version":"1.0.1","ICD":{"library_path":4,"api_version":"1.4.351"}}'):
            def read(path, *args, **kwargs):
                return io.BytesIO(malformed.encode()) if path == icd else original(path, *args, **kwargs)
            with self.subTest(manifest=malformed), patch.object(Path, 'open', read), \
                    patch.object(engine.subprocess, 'Popen') as spawn:
                with self.assertRaises(engine.EngineError) as caught:
                    e.transcribe(self.wav, threading.Event())
                self.assertEqual(caught.exception.category, 'setup')
                spawn.assert_not_called()

    def test_icd_read_is_bounded_even_if_manifest_grows_after_stat(self):
        icd = Path('/usr/share/vulkan/icd.d/nvidia_icd.json')
        if not icd.exists():
            self.skipTest('NVIDIA system manifest is not installed')
        self.whisper_model(); self.success('must not run')
        payload = b' ' * 65537 + icd.read_bytes()
        original = Path.open
        def read(path, *args, **kwargs):
            return io.BytesIO(payload) if path == icd else original(path, *args, **kwargs)
        with patch.object(Path, 'open', read):
            with self.assertRaises(engine.EngineError) as caught:
                engine.LocalEngine(engine.EngineSpec('whisper', self.binary,
                    self.model, vulkan_icd=icd), self.root / 'jobs')
        self.assertEqual(caught.exception.category, 'setup')

    def test_writable_or_user_owned_system_manifest_is_rejected(self):
        icd = Path('/usr/share/vulkan/icd.d/nvidia_icd.json')
        if not icd.exists():
            self.skipTest('NVIDIA system manifest is not installed')
        self.whisper_model(); self.success('must not run')
        original = Path.lstat
        for target, writable, uid in ((icd, 0o020, 0), (icd, 0, 1000),
                                       (icd.parent, 0o002, 0)):
            def metadata(path):
                info = original(path)
                if path != target:
                    return info
                values = list(info); values[0] |= writable; values[4] = uid
                return os.stat_result(values)
            with self.subTest(target=target, writable=writable, uid=uid), \
                    patch.object(Path, 'lstat', metadata):
                with self.assertRaises(engine.EngineError) as caught:
                    engine.LocalEngine(engine.EngineSpec('whisper', self.binary,
                        self.model, vulkan_icd=icd), self.root / 'jobs')
                self.assertEqual(caught.exception.category, 'setup')


if __name__ == "__main__":
    unittest.main()
