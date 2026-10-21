"""Generated child processes only; these tests never open audio devices."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

try:
    from local_voice import process_guard
except ImportError:
    process_guard = None


def running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class ProcessGuardTests(unittest.TestCase):
    def test_proc_exit_between_open_and_read_is_already_stopped(self):
        from unittest.mock import patch
        with patch.object(Path, 'read_text', side_effect=ProcessLookupError):
            self.assertFalse(running(123456789))

    def guard(self, argv, **kwargs):
        self.assertIsNotNone(process_guard, "The owned process launcher is missing")
        return process_guard.guarded_command(argv, **kwargs)

    def test_preserves_worker_exit_status(self):
        command = self.guard([sys.executable, "-c", "raise SystemExit(7)"])
        result = subprocess.run(command, start_new_session=True, timeout=3,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stderr, b"")

    def test_wrong_owner_rejects_before_worker_exec(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "must-not-exist"
            command = self.guard([sys.executable, "-c",
                                  f"from pathlib import Path;Path({str(marker)!r}).touch()"],
                                 owner_pid=1)
            result = subprocess.run(command, start_new_session=True, timeout=3,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertEqual(result.stderr, b"")

    def inherited_helper_cleanup(self, terminate_worker):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            leaf = root / "leaf.py"
            leaf.write_text(
                "import os,signal,time\nfrom pathlib import Path\n"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                f"Path({str(root / 'leaf.ready')!r}).touch()\n"
                "time.sleep(30)\n")
            helper = root / "helper.py"
            helper.write_text(
                "import json,os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                f"p=subprocess.Popen([sys.executable,{str(leaf)!r}])\n"
                f"while not Path({str(root / 'leaf.ready')!r}).exists():time.sleep(.005)\n"
                f"Path({str(root / 'helper.pids')!r}).write_text(json.dumps([os.getpid(),p.pid]))\n"
                "time.sleep(30)\n")
            worker = root / "worker.py"
            worker.write_text(
                "import os,subprocess,sys,time\nfrom pathlib import Path\n"
                f"p=subprocess.Popen([sys.executable,{str(helper)!r}])\n"
                f"Path({str(root / 'worker.pid')!r}).write_text(str(os.getpid()))\n"
                f"while not Path({str(root / 'release')!r}).exists():time.sleep(.005)\n")
            process = subprocess.Popen(self.guard([sys.executable, str(worker)]),
                                       start_new_session=True, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            pids = []
            try:
                self.assertTrue(until(lambda: (root / "helper.pids").exists()))
                pids = json.loads((root / "helper.pids").read_text())
                pids.append(int((root / "worker.pid").read_text()))
                began = time.monotonic()
                if terminate_worker:
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    (root / "release").touch()
                try:
                    output, diagnostics = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    self.fail("The exited direct worker left active helpers holding output pipes")
                self.assertLess(time.monotonic() - began, 2)
                self.assertEqual((output, diagnostics), (b"", b""))
                self.assertEqual(process.returncode, 143 if terminate_worker else 0)
                self.assertFalse(any(running(pid) for pid in pids), pids)
                # The supervisor must reap every adopted helper, including
                # the next generation adopted after its parent is killed.
                self.assertFalse(any(Path(f"/proc/{pid}").exists() for pid in pids), pids)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=3)

    def test_sigterm_of_worker_cleans_ignoring_helpers_holding_output(self):
        self.inherited_helper_cleanup(terminate_worker=True)

    def test_successful_worker_exit_cleans_and_reaps_ignoring_helpers(self):
        self.inherited_helper_cleanup(terminate_worker=False)

    def test_owner_dies_during_launcher_setup_worker_never_executes(self):
        command = self.guard(["/bin/true"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "must-not-exist"
            owner = root / "owner.py"
            # Keep the same launcher PID but delay the actual guard setup.
            # The owner dies in the otherwise vulnerable pre-prctl interval.
            owner.write_text(
                "import os,subprocess,sys,time\nfrom pathlib import Path\n"
                "from local_voice.process_guard import guarded_command\n"
                f"cmd=guarded_command([sys.executable,'-c',\"from pathlib import Path;Path({str(marker)!r}).touch()\"])\n"
                "delay='import os,sys,time;time.sleep(.2);os.execv(sys.argv[1],sys.argv[1:])'\n"
                "p=subprocess.Popen([sys.executable,'-c',delay,*cmd],start_new_session=True)\n"
                f"Path({str(root / 'guard')!r}).write_text(str(p.pid))\n"
                "time.sleep(30)\n")
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            app = subprocess.Popen([sys.executable, str(owner)], env=env)
            guard = None
            try:
                self.assertTrue(until(lambda: (root / "guard").exists()))
                guard = int((root / "guard").read_text())
                app.kill()
                app.wait(timeout=3)
                self.assertTrue(until(lambda: not running(guard)))
                self.assertFalse(marker.exists())
            finally:
                if app.poll() is None:
                    app.kill()
                    app.wait()
                if guard is not None and running(guard):
                    os.killpg(guard, signal.SIGKILL)

    def test_owner_death_kills_worker_and_its_same_group_child(self):
        self.guard(["/bin/true"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "worker.py"
            worker.write_text(
                "import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
                f"Path({str(root / 'pids')!r}).write_text(json.dumps([os.getpid(),child.pid]))\n"
                "time.sleep(30)\n")
            owner = root / "owner.py"
            owner.write_text(
                "import signal,subprocess,sys,time\nfrom pathlib import Path\n"
                "from local_voice.process_guard import guarded_command\n"
                "signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGUSR1})\n"
                f"p=subprocess.Popen(guarded_command([sys.executable,{str(worker)!r}]),start_new_session=True)\n"
                f"Path({str(root / 'guard')!r}).write_text(str(p.pid))\n"
                "time.sleep(30)\n")
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            app = subprocess.Popen([sys.executable, str(owner)], env=env)
            pids = []
            try:
                self.assertTrue(until(lambda: (root / "pids").exists()))
                pids = json.loads((root / "pids").read_text())
                pids.append(int((root / "guard").read_text()))
                app.kill()
                app.wait(timeout=3)
                self.assertTrue(until(lambda: not any(running(pid) for pid in pids)), pids)
            finally:
                if app.poll() is None:
                    app.kill()
                    app.wait()
                for pid in pids:
                    if running(pid):
                        os.kill(pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
