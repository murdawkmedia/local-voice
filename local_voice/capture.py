"""Independent local Pulse PCM readers with bounded, recoverable WAV output."""

from array import array
from dataclasses import dataclass, field
from enum import Enum
import math
import os
from pathlib import Path
import selectors
import signal
import struct
import subprocess
import threading
import time

from .process_guard import guarded_command


class Source(str, Enum):
    MICROPHONE = "microphone"
    COMPUTER = "computer"


class CaptureError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__("Local audio capture failed: " + category)


@dataclass
class _Track:
    source: str
    device: str
    path: Path
    process: object = None
    thread: object = None
    frames: int = 0
    level: float = 0.0
    first_frame_seconds: float | None = None
    last_frame_seconds: float | None = None
    errors: list = field(default_factory=list)
    returncode: int | None = None
    finalized: bool = False
    done: bool = False
    signal_stage: int = 0


def _header(frames, rate):
    size = frames * 2
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + size, b"WAVE",
                       b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16, b"data", size)


def _write_header(fd, frames, rate, *, initial=False):
    """Finish a bounded header write without disturbing the PCM offset."""
    data = _header(frames, rate)
    position = 0
    while position < len(data):
        remaining = data[position:]
        written = os.write(fd, remaining) if initial else os.pwrite(fd, remaining, position)
        if not 0 < written <= len(remaining):
            raise OSError("Header write made no valid progress")
        position += written


class PulseRecorder:
    """Own one child, pipe reader and private WAV per requested source.

    start() is asynchronous. wait_ready() requires real complete frames from
    EVERY requested source and returns false on capture failure or timeout.
    Inspect status()["errors"] to distinguish an error from slow preparation.
    stop() is synchronous/idempotent and belongs outside the Qt event loop.
    """

    TAIL_SECONDS = .120
    READ_BYTES = 32768

    def __init__(self, session_directory, *, sources, rate=48000,
                 executable="/usr/bin/parec", server=None):
        directory = Path(session_directory)
        if ".." in directory.parts or not sources or len(sources) > 2 or rate != 48000:
            raise CaptureError("configuration")
        self.directory = directory.absolute()
        self.rate = rate
        self.executable = os.fspath(executable)
        self.server = server or os.environ.get("PULSE_SERVER") or (
            "unix:" + str(Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) /
                          "pulse/native"))
        if (not self.server.startswith("unix:/") or "\0" in self.server or
                any(char.isspace() for char in self.server) or ";" in self.server or
                ".." in Path(self.server[5:]).parts):
            raise CaptureError("local-server")
        self._tracks = {}
        for label, device in sources.items():
            try:
                label = Source(label).value
            except (ValueError, TypeError) as exc:
                raise CaptureError("source") from exc
            if not isinstance(device, str) or not device or "\0" in device or "\n" in device:
                raise CaptureError("source")
            self._tracks[label] = _Track(label, device, self.directory / (label + ".wav"))
        self._changed = threading.Condition(threading.RLock())
        self._stop_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._abort = threading.Event()
        self._started = None
        self._stop_time = None
        self._hard_deadline = None
        self._result = None
        self._state = "new"

    def _error(self, track, category):
        with self._changed:
            if category not in track.errors:
                track.errors.append(category)
            self._changed.notify_all()

    def start(self):
        """Reserve all files exclusively, then start independent readers."""
        with self._changed:
            if self._state != "new" or self._stop_requested.is_set():
                raise CaptureError("state")
            if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
                raise CaptureError("session-path")
            opened = []
            try:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                # Refuse the complete session before starting any producer.
                if any(t.path.exists() or t.path.is_symlink() for t in self._tracks.values()):
                    raise OSError("Existing track")
                for track in self._tracks.values():
                    fd = os.open(track.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                    opened.append((track, fd))
                    try:
                        _write_header(fd, 0, self.rate, initial=True)
                        os.fsync(fd)
                    except OSError as exc:
                        self._error(track, "io")
                        raise CaptureError("io") from exc
            except (OSError, CaptureError) as exc:
                for _, fd in opened:
                    os.close(fd)
                if isinstance(exc, CaptureError):
                    raise
                raise CaptureError("session-path") from exc
            self._started = time.monotonic()
            self._state = "preparing"
            for track, fd in opened:
                track.thread = threading.Thread(target=self._read_track, args=(track, fd),
                                                name="capture-" + track.source, daemon=True)
                track.thread.start()

    def _command(self, track):
        return guarded_command([
            self.executable, "--record", "--raw", "--format=s16le", "--channels=1",
            f"--rate={self.rate}", "--latency-msec=80", "--process-time-msec=20",
            "--server=" + self.server, "--device=" + track.device,
            "--client-name=Local Voice", "--stream-name=" + track.source,
        ])

    def _read_track(self, track, fd):
        carry = b""
        written_bytes = 0
        process = None
        try:
            if self._stop_requested.is_set():
                return
            try:
                process = subprocess.Popen(self._command(track), stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                           start_new_session=True, bufsize=0)
            except OSError:
                self._error(track, "spawn")
                return
            with self._changed:
                track.process = process
                if self._hard_deadline is not None and time.monotonic() >= self._hard_deadline:
                    os.killpg(process.pid, signal.SIGKILL)
                    self._error(track, "stop-timeout")
                self._changed.notify_all()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    if not selector.select(timeout=.05):
                        if self._abort.is_set():
                            self._error(track, "stop-timeout")
                            break
                        continue
                    chunk = os.read(process.stdout.fileno(), self.READ_BYTES)
                    if not chunk:
                        break
                    data = carry + chunk
                    end = len(data) & ~1
                    carry = data[end:]
                    if not end:
                        continue
                    data = data[:end]
                    if track.frames + len(data) // 2 > (0xFFFFFFFF - 36) // 2:
                        self._error(track, "wav-limit")
                        break
                    position = 0
                    while position < len(data):
                        written = os.write(fd, data[position:])
                        if not written:
                            raise OSError("Write failed")
                        position += written
                        written_bytes += written
                    frames = written_bytes // 2
                    # pwrite leaves the data append offset unchanged. Every
                    # published frame count has a matching readable WAV header.
                    _write_header(fd, frames, self.rate)
                    samples = array("h", data)
                    now = time.monotonic() - self._started
                    with self._changed:
                        track.frames = frames
                        track.level = max(abs(value) for value in samples) / 32768.0
                        if track.first_frame_seconds is None:
                            track.first_frame_seconds = now
                        track.last_frame_seconds = now
                        self._changed.notify_all()
            if carry:
                self._error(track, "incomplete-sample")
            if not self._stop_requested.is_set():
                self._error(track, "unexpected-eof")
            # A producer can close stdout before exiting. Stop still supervises
            # its process rather than mistaking EOF for completed ownership.
            track.returncode = process.wait()
            if track.returncode not in (0, 128 + signal.SIGINT, 128 + signal.SIGTERM):
                self._error(track, "process")
        except OSError:
            self._error(track, "io")
        except Exception:
            self._error(track, "capture")
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                track.returncode = process.returncode
                process.stdout.close()
            if not written_bytes:
                self._error(track, "no-frames")
            try:
                # Even a failed checkpoint/partial write retains every complete
                # sample that reached the file before the failure.
                frames = written_bytes // 2
                track.frames = frames
                os.ftruncate(fd, 44 + frames * 2)
                _write_header(fd, frames, self.rate)
                os.fsync(fd)
                track.finalized = True
            except OSError:
                self._error(track, "finalize")
            finally:
                os.close(fd)
            with self._changed:
                track.done = True
                self._changed.notify_all()

    @staticmethod
    def _timeout(timeout):
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("A positive finite timeout is required")
        return timeout

    def wait_ready(self, timeout=3):
        deadline = time.monotonic() + self._timeout(timeout)
        with self._changed:
            while True:
                if (self._stop_requested.is_set() or self._started is None or
                        any(t.errors or t.done for t in self._tracks.values())):
                    return False
                if all(t.frames for t in self._tracks.values()):
                    self._state = "listening"
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)

    def status(self):
        with self._changed:
            errors = [{"source": t.source, "category": error}
                      for t in self._tracks.values() for error in t.errors]
            ready = (self._started is not None and not errors and
                     not self._stop_requested.is_set() and
                     all(t.frames and not t.done for t in self._tracks.values()))
            state = "error" if errors else ("listening" if ready else self._state)
            return {"state": state, "ready": ready, "finalized": all(
                t.finalized for t in self._tracks.values()), "errors": errors,
                "elapsed_seconds": 0 if self._started is None else max(0.,
                    (self._stop_time or time.monotonic()) - self._started),
                "tracks": {t.source: {
                    "path": t.path, "ready": bool(t.frames and not t.errors),
                    "frames": t.frames, "rate": self.rate, "level": t.level,
                    "duration_seconds": t.frames / self.rate,
                    "first_frame_seconds": t.first_frame_seconds,
                    "last_frame_seconds": t.last_frame_seconds,
                    "errors": list(t.errors), "returncode": t.returncode,
                    "finalized": t.finalized,
                } for t in self._tracks.values()}}

    def stop(self, timeout=5):
        """Request Stop now, allow 120 ms client tail, drain and fsync.

        Timeout escalates INT -> TERM -> KILL only inside owned groups. A
        timed-out I/O finalizer is explicit; its live file is never replaced.
        """
        timeout = self._timeout(timeout)
        with self._stop_lock:
            if self._result is not None:
                return self._result
            began = time.monotonic()
            deadline = began + timeout
            # This flag must not wait for a slow session-file fsync holding
            # the lifecycle condition. Readers see Stop during startup too.
            self._hard_deadline = deadline
            self._stop_requested.set()
            with self._changed:
                self._stop_time = began
                self._state = "stopping"
                self._changed.notify_all()
            tail_end = began + min(self.TAIL_SECONDS, timeout * .2)
            term_at = tail_end + min(1., (deadline - tail_end) * .45)
            kill_at = term_at + min(.5, (deadline - term_at) * .5)
            while time.monotonic() < deadline:
                now = time.monotonic()
                stage = 0 if now < tail_end else (1 if now < term_at else (2 if now < kill_at else 3))
                with self._changed:
                    if all(t.done or t.thread is None for t in self._tracks.values()):
                        break
                    for track in self._tracks.values():
                        process = track.process
                        if (not stage or stage <= track.signal_stage or process is None or
                                process.poll() is not None):
                            continue
                        if stage > 1:
                            self._error(track, "stop-escalated")
                        try:
                            os.killpg(process.pid, {1: signal.SIGINT, 2: signal.SIGTERM,
                                                    3: signal.SIGKILL}[stage])
                        except ProcessLookupError:
                            pass
                        track.signal_stage = stage
                    self._changed.wait(min(.01, max(0, deadline - now)))
            self._abort.set()
            for track in self._tracks.values():
                process = track.process
                if process is not None and process.poll() is None:
                    self._error(track, "stop-timeout")
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            for track in self._tracks.values():
                if track.thread is None:
                    continue
                track.thread.join(timeout=max(.15, deadline - time.monotonic()))
                if track.thread.is_alive():
                    self._error(track, "stop-timeout")
            with self._changed:
                self._state = "stopped"
                self._result = self.status()
            return self._result
