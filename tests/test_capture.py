"""Exercise real subprocess pipes and WAV files with generated PCM only."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

try:
    from local_voice import capture
except ImportError:
    capture = None


PRODUCER = '''#!/usr/bin/python3
import json,os,signal,struct,subprocess,sys,time
from pathlib import Path
source=next(a.split("=",1)[1] for a in sys.argv if a.startswith("--device="))
cfg=json.loads(Path(__file__).with_suffix(".json").read_text())[source]
Path(__file__).with_name(source+".args").write_text(json.dumps(sys.argv[1:]))
Path(__file__).with_name(source+".pid").write_text(str(os.getpid()))
stop=False
def stopping(sig,frame):
    global stop
    stop=True
signal.signal(signal.SIGINT,signal.SIG_IGN if cfg.get("ignore") else stopping)
signal.signal(signal.SIGTERM,signal.SIG_IGN if cfg.get("ignore") else stopping)
if cfg.get("helper"):
    marker=Path(__file__).with_name(source+".helper")
    code="import os,signal,time;from pathlib import Path;signal.signal(signal.SIGINT,signal.SIG_IGN);signal.signal(signal.SIGTERM,signal.SIG_IGN);Path("+repr(str(marker))+").write_text(str(os.getpid()));time.sleep(30)"
    subprocess.Popen([sys.executable,"-c",code])
    while not marker.exists():time.sleep(.005)
time.sleep(cfg.get("delay",0))
if cfg.get("fail"):
    os.write(2,b"synthetic diagnostic, never retained"*4000)
    raise SystemExit(3)
def output(count,value):
    data=struct.pack("<h",value)*count
    while data:
        n=os.write(1,data[:8192]);data=data[n:]
output(cfg.get("first",31),1234)
Path(__file__).with_name(source+".delivered").touch()
if cfg.get("exit_after_first"):
    raise SystemExit(3)
while not stop:
    time.sleep(.005)
time.sleep(cfg.get("tail_delay",0))
output(cfg.get("tail",0),-2345)
if cfg.get("odd"):
    os.write(1,b"x")
'''


def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return predicate()


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.producer = self.root / "producer"
        self.producer.write_text(PRODUCER)
        self.producer.chmod(0o700)
        self.recorders = []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for recorder in self.recorders:
            recorder.stop(timeout=2)

    def make(self, configs, **kw):
        self.assertIsNotNone(capture, "The independent source recorder is missing")
        self.producer.with_suffix(".json").write_text(json.dumps(configs))
        sources = {"microphone": "mic"}
        if "monitor" in configs:
            sources["computer"] = "monitor"
        recorder = capture.PulseRecorder(self.root / "session", sources=sources,
                                         executable=str(self.producer), **kw)
        self.recorders.append(recorder)
        return recorder

    def wav(self, track, frames):
        self.assertTrue(track["finalized"])
        self.assertEqual(track["frames"], frames)
        with wave.open(str(track["path"]), "rb") as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()),
                             (1, 2, 48000))
            self.assertEqual(audio.getnframes(), frames)
            self.assertEqual(len(audio.readframes(frames + 10)), frames * 2)

    def test_both_sources_must_deliver_frames_then_independent_tails_are_drained(self):
        recorder = self.make({"mic": {"first": 4321, "tail": 121, "tail_delay": .02},
                              "monitor": {"delay": .25, "first": 123, "tail": 7654,
                                          "tail_delay": .17}})
        recorder.start()
        self.assertFalse(recorder.wait_ready(timeout=.08))
        self.assertTrue(recorder.wait_ready(timeout=2))
        status = recorder.status()
        self.assertTrue(all(t["ready"] for t in status["tracks"].values()))
        self.assertGreater(status["tracks"]["microphone"]["level"], 0)
        # A crash/recovery reader can already open the periodically patched header.
        with wave.open(str(status["tracks"]["microphone"]["path"]), "rb") as audio:
            self.assertEqual(audio.getnframes(), 4321)
        result = recorder.stop()
        self.assertTrue(result["finalized"])
        self.assertEqual(result["errors"], [])
        self.wav(result["tracks"]["microphone"], 4442)
        self.wav(result["tracks"]["computer"], 7777)
        self.assertGreater(result["tracks"]["computer"]["first_frame_seconds"],
                           result["tracks"]["microphone"]["first_frame_seconds"])
        args = json.loads((self.root / "mic.args").read_text())
        self.assertIn("--format=s16le", args)
        self.assertIn("--channels=1", args)
        self.assertIn("--latency-msec=80", args)
        self.assertIn("--process-time-msec=20", args)
        self.assertTrue(any(a.startswith("--server=unix:/") for a in args))

    def test_stop_before_ready_is_bounded_and_preserves_what_arrives(self):
        recorder = self.make({"mic": {"delay": .3, "first": 89, "tail": 11}})
        recorder.start()
        self.assertTrue(until(lambda: (self.root / "mic.pid").exists()))
        self.assertFalse(recorder.wait_ready(timeout=.01))
        started = time.monotonic()
        result = recorder.stop(timeout=2)
        self.assertLess(time.monotonic() - started, 2)
        self.wav(result["tracks"]["microphone"], 100)

    def test_stop_during_file_setup_prevents_new_producer_launch(self):
        recorder = self.make({"mic": {"first": 89}})
        entered = threading.Event()
        release = threading.Event()
        requested = threading.Event()
        real_fsync = os.fsync
        first = True
        def slow_first_fsync(fd):
            nonlocal first
            if first:
                first = False
                entered.set()
                release.wait(timeout=2)
            return real_fsync(fd)
        results = []
        def stop():
            requested.set()
            results.append(recorder.stop(timeout=2))
        with patch.object(capture.os, "fsync", slow_first_fsync):
            starter = threading.Thread(target=recorder.start)
            starter.start()
            self.assertTrue(entered.wait(timeout=2))
            stopper = threading.Thread(target=stop)
            stopper.start()
            self.assertTrue(requested.wait(timeout=2))
            # Only the file-sync timing is controlled: production subprocess
            # launch, PCM, WAV and Stop all remain the actual implementation.
            time.sleep(.03)
            release.set()
            starter.join(timeout=2)
            stopper.join(timeout=3)
        self.assertFalse(starter.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertFalse((self.root / "mic.pid").exists())
        self.wav(results[0]["tracks"]["microphone"], 0)
        self.assertGreaterEqual(results[0]["elapsed_seconds"], 0)

    def test_large_tail_bursts_are_drained_completely_and_keep_sample_content(self):
        recorder = self.make({"mic": {"first": 67, "tail": 150001},
                              "monitor": {"first": 109, "tail": 89003, "tail_delay": .08}})
        recorder.start()
        self.assertTrue(recorder.wait_ready())
        result = recorder.stop()
        for label, first, tail in (("microphone", 67, 150001), ("computer", 109, 89003)):
            track = result["tracks"][label]
            self.wav(track, first + tail)
            with wave.open(str(track["path"]), "rb") as audio:
                self.assertEqual(audio.readframes(first), b"\xd2\x04" * first)
                self.assertEqual(audio.readframes(tail), b"\xd7\xf6" * tail)

    def test_process_failure_after_frames_retains_track_and_cannot_publish_ready(self):
        recorder = self.make({"mic": {"first": 40001, "exit_after_first": True}})
        recorder.start()
        self.assertTrue(until(lambda: recorder.status()["tracks"]["microphone"]["finalized"]))
        self.assertFalse(recorder.wait_ready())
        result = recorder.stop()
        self.wav(result["tracks"]["microphone"], 40001)
        self.assertIn("process", result["tracks"]["microphone"]["errors"])

    def test_temporary_header_update_failure_retains_already_written_complete_samples(self):
        recorder = self.make({"mic": {"first": 501}})
        real_pwrite = os.pwrite
        fail = True
        def fail_first_header(fd, data, offset):
            nonlocal fail
            if fail:
                fail = False
                raise OSError("Generated header failure")
            return real_pwrite(fd, data, offset)
        with patch.object(capture.os, "pwrite", fail_first_header):
            recorder.start()
            self.assertTrue(until(lambda: recorder.status()["tracks"]["microphone"]["finalized"]))
            result = recorder.stop()
        self.wav(result["tracks"]["microphone"], 501)
        self.assertIn("io", result["tracks"]["microphone"]["errors"])

    def test_short_initial_header_writes_keep_pcm_at_byte_44(self):
        recorder = self.make({"mic": {"first": 113, "tail": 23}})
        real_write = os.write
        def short_initial_header(fd, data):
            position = os.lseek(fd, 0, os.SEEK_CUR)
            if position < 44:
                return real_write(fd, data[:min(7, 44 - position)])
            return real_write(fd, data)
        with patch.object(capture.os, "write", short_initial_header):
            recorder.start()
            self.assertTrue(recorder.wait_ready())
            result = recorder.stop()
        track = result["tracks"]["microphone"]
        self.wav(track, 136)
        self.assertEqual(track["path"].read_bytes()[44:], b"\xd2\x04" * 113 + b"\xd7\xf6" * 23)

    def test_short_checkpoint_and_final_header_writes_complete_without_moving_pcm_offset(self):
        recorder = self.make({"mic": {"first": 117, "tail": 29}})
        real_pwrite = os.pwrite
        def short_header(fd, data, offset):
            return real_pwrite(fd, data[:5], offset)
        with patch.object(capture.os, "pwrite", short_header):
            recorder.start()
            self.assertTrue(recorder.wait_ready())
            path = recorder.status()["tracks"]["microphone"]["path"]
            try:
                with wave.open(str(path), "rb") as audio:
                    checkpoint_frames = audio.getnframes()
            except (EOFError, wave.Error) as exc:
                self.fail("Short writes left an invalid checkpoint header: " + type(exc).__name__)
            self.assertEqual(checkpoint_frames, 117)
            result = recorder.stop()
        track = result["tracks"]["microphone"]
        self.wav(track, 146)
        self.assertEqual(track["path"].read_bytes()[44:], b"\xd2\x04" * 117 + b"\xd7\xf6" * 29)

    def test_zero_progress_initial_header_is_explicit_io_failure_before_producer(self):
        recorder = self.make({"mic": {"first": 127}})
        with patch.object(capture.os, "write", return_value=0):
            with self.assertRaises(capture.CaptureError) as error:
                recorder.start()
        self.assertEqual(error.exception.category, "io")
        self.assertFalse(recorder.status()["finalized"])
        self.assertFalse((self.root / "mic.pid").exists())

    def test_zero_progress_final_header_never_reports_finalized_or_loses_pcm(self):
        recorder = self.make({"mic": {"first": 131, "tail": 31}})
        recorder.start()
        self.assertTrue(recorder.wait_ready())
        with patch.object(capture.os, "pwrite", return_value=0):
            result = recorder.stop()
        track = result["tracks"]["microphone"]
        self.assertFalse(result["finalized"])
        self.assertFalse(track["finalized"])
        self.assertIn("finalize", track["errors"])
        self.assertEqual(track["frames"], 162)
        self.assertEqual(track["path"].read_bytes()[44:], b"\xd2\x04" * 131 + b"\xd7\xf6" * 31)

    def test_app_death_leaves_no_active_recorders_and_readable_recovery_headers(self):
        self.make({"mic": {"first": 99}, "monitor": {"first": 197}})
        app_script = self.root / "app.py"
        app_script.write_text(
            "import time\nfrom pathlib import Path\nfrom local_voice.capture import PulseRecorder\n"
            f"r=PulseRecorder(Path({str(self.root / 'session')!r}),sources={{'microphone':'mic','computer':'monitor'}},executable={str(self.producer)!r})\n"
            "r.start()\nassert r.wait_ready()\n"
            f"Path({str(self.root / 'ready')!r}).touch()\n"
            "time.sleep(30)\n")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        app = subprocess.Popen([sys.executable, str(app_script)], env=env)
        pids = []
        try:
            self.assertTrue(until(lambda: (self.root / "ready").exists()))
            pids = [int((self.root / (source + ".pid")).read_text()) for source in ("mic", "monitor")]
            app.kill()
            app.wait(timeout=3)
            def active(pid):
                try:
                    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
                except FileNotFoundError:
                    return False
            self.assertTrue(until(lambda: not any(active(pid) for pid in pids)))
            for label, count in (("microphone", 99), ("computer", 197)):
                with wave.open(str(self.root / "session" / (label + ".wav")), "rb") as audio:
                    self.assertEqual(audio.getnframes(), count)
        finally:
            if app.poll() is None:
                app.kill()
                app.wait()

    def test_one_failed_source_never_becomes_ready_or_silently_disappears(self):
        recorder = self.make({"mic": {"first": 44, "tail": 12}, "monitor": {"fail": True}})
        recorder.start()
        self.assertFalse(recorder.wait_ready(timeout=2))
        result = recorder.stop()
        self.assertEqual(set(result["tracks"]), {"microphone", "computer"})
        self.assertIn("process", result["tracks"]["computer"]["errors"])
        self.wav(result["tracks"]["microphone"], 56)
        self.wav(result["tracks"]["computer"], 0)
        self.assertNotIn("synthetic diagnostic", str(result))

    def test_odd_byte_is_reported_and_complete_frames_survive_repeated_stop(self):
        recorder = self.make({"mic": {"first": 51, "tail": 32, "odd": True}})
        recorder.start()
        self.assertTrue(recorder.wait_ready())
        result = recorder.stop()
        self.wav(result["tracks"]["microphone"], 83)
        self.assertIn("incomplete-sample", result["tracks"]["microphone"]["errors"])
        self.assertEqual(recorder.stop(), result)
        self.assertEqual(result["tracks"]["microphone"]["path"].stat().st_mode & 0o777, 0o600)

    def test_ignoring_stop_is_escalated_without_losing_delivered_frames(self):
        recorder = self.make({"mic": {"first": 9001, "ignore": True}})
        recorder.start()
        self.assertTrue(recorder.wait_ready())
        result = recorder.stop(timeout=1.2)
        self.wav(result["tracks"]["microphone"], 9001)
        self.assertIn("stop-escalated", result["tracks"]["microphone"]["errors"])
        pid = int((self.root / "mic.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_stop_reaps_ignoring_helper_after_producer_exits_and_closes_held_pipe(self):
        recorder = self.make({"mic": {"first": 137, "tail": 37, "helper": True}})
        recorder.start()
        self.assertTrue(recorder.wait_ready())
        helper = int((self.root / "mic.helper").read_text())
        try:
            began = time.monotonic()
            result = recorder.stop(timeout=1.2)
            self.assertLess(time.monotonic() - began, 1.2)
            self.assertFalse(Path(f"/proc/{helper}").exists(), "Output-holding helper was not reaped")
            self.wav(result["tracks"]["microphone"], 174)
            self.assertEqual(result["errors"], [])
        finally:
            try:
                os.kill(helper, 9)
            except ProcessLookupError:
                pass

    def test_source_labels_and_existing_paths_cannot_escape_or_overwrite(self):
        self.assertIsNotNone(capture, "The independent source recorder is missing")
        with self.assertRaises((ValueError, capture.CaptureError)):
            capture.PulseRecorder(self.root / "session", sources={"../escape": "mic"})
        session = self.root / "session"
        session.mkdir()
        original = session / "microphone.wav"
        original.write_bytes(b"preserved")
        recorder = self.make({"mic": {}})
        with self.assertRaises(capture.CaptureError):
            recorder.start()
        self.assertEqual(original.read_bytes(), b"preserved")
        self.assertFalse((self.root / "mic.pid").exists())

    def test_nonlocal_pulse_server_and_traversing_session_are_rejected(self):
        self.assertIsNotNone(capture, "The independent source recorder is missing")
        with self.assertRaises(capture.CaptureError):
            self.make({"mic": {}}, server="tcp:127.0.0.1:4713")
        with self.assertRaises(capture.CaptureError):
            self.make({"mic": {}}, server="unix:/missing tcp:127.0.0.1:4713")
        with self.assertRaises(capture.CaptureError):
            capture.PulseRecorder(self.root / ".." / "escape", sources={"microphone": "mic"})


if __name__ == "__main__":
    unittest.main()
