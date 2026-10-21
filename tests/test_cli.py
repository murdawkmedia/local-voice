import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import contextlib
import io
import threading
import types
from local_voice.ipc import send_command


class CliTests(unittest.TestCase):
    def test_cancel_quit_clears_same_event_and_does_not_repeat_prompt(self):
        from local_voice import cli
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication, QMainWindow
        done = threading.Event()
        class Window(QMainWindow):
            close_cancelled_by_user = False
            attempts = 0
            def __init__(self, service):
                super().__init__()
                done.set()
                QTimer.singleShot(550, QApplication.instance().quit)
            def request_close(self):
                self.attempts += 1
                self.close_cancelled_by_user = True
                return False
        with patch.dict(os.environ, QT_QPA_PLATFORM='offscreen'), patch('local_voice.gui.MainWindow', Window):
            result, window = cli._run_gui(types.SimpleNamespace(minimized=False), object(),
                                          done, done.set, lambda callback: None)
        self.assertEqual(result, 0)
        self.assertEqual(window.attempts, 1)
        self.assertFalse(done.is_set())

    def test_quit_during_window_initialization_is_checked_before_event_loop(self):
        from local_voice import cli
        from PyQt6.QtWidgets import QMainWindow
        done = threading.Event()
        class Window(QMainWindow):
            shutdown_edits_accepted = True
            closed = False
            def __init__(self, service):
                super().__init__()
                done.set()
            def request_close(self):
                self.closed = True
                return True
        with patch.dict(os.environ, QT_QPA_PLATFORM='offscreen'), patch('local_voice.gui.MainWindow', Window):
            result, window = cli._run_gui(types.SimpleNamespace(minimized=False), object(),
                                          done, done.set, lambda callback: None)
        self.assertEqual(result, 0)
        self.assertTrue(window.closed)

    def test_incomplete_headless_shutdown_returns_failure_without_private_diagnostics(self):
        from local_voice import cli
        class Instance:
            def __init__(self, *args): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def serve(self, callback): callback('quit')
        class Runtime:
            def __init__(self, *args, **kwargs): pass
        class Service:
            def __init__(self, *args, **kwargs): pass
            def close(self): return False
        modules = {}
        for name, fields in [('runtime', {'SpeechRuntime': Runtime}), ('service', {'Service': Service}),
                             ('summary', {'LocalSummarizer': Runtime, 'SummarySpec': Runtime})]:
            module = types.ModuleType('local_voice.' + name)
            module.__dict__.update(fields)
            modules[module.__name__] = module
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, modules), patch.object(
                cli, 'Instance', Instance), patch.object(cli.signal, 'signal'), contextlib.redirect_stderr(io.StringIO()) as error:
            root = Path(directory)
            result = cli.main(['--headless', '--state-dir', str(root / 'state'),
                               '--config-dir', str(root / 'config'), '--runtime-dir', str(root / 'runtime')])
        self.assertEqual(result, 1)
        self.assertIn('Shutdown is incomplete', error.getvalue())

    def test_ipc_quit_during_qt_import_is_permanent_and_rejects_later_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'startup.py'
            script.write_text('''import builtins,sys,time,types\nfrom pathlib import Path\n'''
                + f'root=Path({str(root)!r})\n'
                + '''class Runtime:
 def __init__(self,*args):pass
 def diagnostics(self):return {}
class Service:
 def __init__(self,*args,**kwargs):self.state_dir=root/'state'
 def public_status(self):return {'ok':True}
 def snapshot(self):return {'status':{},'sessions':[],'settings':{},'devices':{}}
 def start(self,*args,**kwargs):(root/'started').touch();return True
 def stop(self):pass
 def close(self,*args,**kwargs):return True
class Summary:
 def __init__(self,*args,**kwargs):pass
for name,items in [('runtime',{'SpeechRuntime':Runtime}),('service',{'Service':Service}),('summary',{'LocalSummarizer':Summary,'SummarySpec':Summary})]:
 module=types.ModuleType('local_voice.'+name);module.__dict__.update(items);sys.modules[module.__name__]=module
original=builtins.__import__
def gated(name,*args,**kwargs):
 if name=='PyQt6.QtCore':
  (root/'importing').touch()
  while not (root/'release').exists():time.sleep(.005)
 return original(name,*args,**kwargs)
builtins.__import__=gated
from local_voice.cli import main
raise SystemExit(main(['--state-dir',str(root/'state'),'--config-dir',str(root/'config'),'--runtime-dir',str(root/'runtime')]))
''')
            env = dict(os.environ, QT_QPA_PLATFORM='offscreen',
                       PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            process = subprocess.Popen([sys.executable, str(script)], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline and not (root / 'importing').exists():
                    time.sleep(.01)
                self.assertTrue((root / 'importing').exists())
                self.assertTrue(send_command(root / 'runtime', 'quit')['ok'])
                self.assertFalse(send_command(root / 'runtime', 'start').get('ok'))
                (root / 'release').touch()
                try:
                    _, error = process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    self.fail('Quit during GUI import was forgotten before app.exec()')
                self.assertEqual(process.returncode, 0, error)
                self.assertFalse((root / 'started').exists())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=3)

    def test_version_and_missing_status_do_not_create_user_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            command=[sys.executable,'-m','local_voice.cli','--state-dir',str(root/'state'),
                     '--config-dir',str(root/'config'),'--runtime-dir',str(root/'runtime')]
            p=subprocess.run(command+['--version'],capture_output=True,text=True,timeout=3)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertIn('0.1.0',p.stdout)
            p=subprocess.run(command+['--control','status'],capture_output=True,text=True,timeout=3)
            self.assertFalse(json.loads(p.stdout)['ok'])
            self.assertFalse((root/'state').exists())
            self.assertFalse((root/'config').exists())

    def test_headless_single_instance_safe_status_and_normal_quit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            command=[sys.executable,'-m','local_voice.cli','--headless','--state-dir',str(root/'state'),
                     '--config-dir',str(root/'config'),'--runtime-dir',str(root/'runtime')]
            env=dict(os.environ,PULSE_SERVER='unix:'+str(root/'nonexistent-pulse'))
            env.pop('HYPRLAND_INSTANCE_SIGNATURE',None)
            process=subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    status=send_command(root/'runtime','status')
                    if status.get('ok'):break
                    if process.poll() is not None:break
                    time.sleep(.05)
                self.assertTrue(status.get('ok'))
                self.assertFalse(status['captureActive'])
                self.assertEqual(status['tracks'],{})
                self.assertTrue(status['offline'])
                self.assertNotIn('transcript',status)
                self.assertTrue(send_command(root/'runtime','quit')['ok'])
                out,err=process.communicate(timeout=5)
                self.assertEqual(process.returncode,0,err)
                self.assertFalse((root/'runtime/control.sock').exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)


if __name__=='__main__':unittest.main()
