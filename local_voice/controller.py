"""Headless asynchronous capture, bounded inference, and durable retry."""

from dataclasses import dataclass, field
import copy
import json
import math
import os
from pathlib import Path
import queue
import struct
import threading
import time
import uuid
import wave

from .capture import PulseRecorder
from .audio_retention import AudioRetention
from .personalization import apply_personalization


class ControllerError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__("Local session controller: " + category)


_MESSAGES = {
    "idle": "Ready.", "preparing": "Preparing audio capture.",
    "recording": "Listening.", "stopping": "Stopping and saving audio.",
    "captured": "Audio saved; waiting for transcription.",
    "transcribing": "Transcribing saved audio locally.", "complete": "Saved.",
    "cancelled-before-ready": "Recording cancelled before capture was ready.",
    "not-ready": "The required audio sources did not become ready. Available tracks are retained.",
    "interrupted": "The interrupted recording is retained for retry.",
    "queue-full": "Transcription could not keep up. Audio is retained for retry.",
    "engine": "Local transcription failed. Audio is retained for retry.",
    "capture": "Audio capture failed. Available tracks are retained.",
    "sources": "The required audio sources are unavailable.",
    "audio": "The retained audio is missing or invalid.",
    "audio-removed": "Audio was removed after transcription. Your saved text is still available.",
    "chunk-conflict": "Retained audio disagrees with an existing chunk.",
    "dictation-limit": "Dictation exceeded ten minutes. The audio is retained.",
    "storage": "The local session could not be saved.",
    "cancelled": "Work was interrupted. Audio is retained for retry.",
    "preview-engine": "Live transcription failed. Recording continues; retry after Stop.",
    "backlog": "Live transcription is behind. Recording continues; saved audio will finish after Stop.",
}


@dataclass
class _Session:
    sid: str
    kind: str
    directory: Path
    chunk_frames: int
    stop: threading.Event = field(default_factory=threading.Event)
    cancel: threading.Event = field(default_factory=threading.Event)
    state: str = "preparing"
    error: str = ""
    capture: dict = field(default_factory=dict)
    capture_done: bool = False
    producer_done: bool = False
    ready: bool = False
    pending: int = 0
    finishing: int = 0
    next: dict = field(default_factory=dict)
    existing: dict = field(default_factory=dict)
    planned: set = field(default_factory=set)
    profile: object = None
    clean_fillers: bool = False
    persistence: threading.Lock = field(default_factory=threading.Lock)
    backlog: bool = False
    preview_error: str = ""
    limit_reached: bool = False
    recording_started: float | None = None


@dataclass(frozen=True)
class _Job:
    session: _Session
    source: str
    ordinal: int
    start: int
    end: int
    path: Path


def _private_directory(path):
    path = Path(path).absolute()
    if ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ControllerError("storage")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _audio_frames(path):
    """Use actual complete PCM bytes, including a crash's uncheckpointed tail."""
    if path.is_symlink():
        raise ControllerError("audio")
    with path.open("rb") as audio:
        header = audio.read(44)
        if len(header) != 44:
            raise ControllerError("audio")
        fields = struct.unpack("<4sI4s4sIHHIIHH4sI", header)
        if (fields[0] != b"RIFF" or fields[2:11] !=
                (b"WAVE", b"fmt ", 16, 1, 1, 48000, 96000, 2, 16) or fields[11] != b"data"):
            raise ControllerError("audio")
        return (os.fstat(audio.fileno()).st_size - 44) // 2


class Controller:
    QUEUE_LIMIT = 32
    MAX_PRODUCERS = 2
    RATE = 48000
    MAX_DICTATION_FRAMES = RATE * 600
    MAX_MEETING_SECONDS = 7200

    def __init__(self, store, state_dir, engine_factory, source_provider,
                 profile_provider=None, event_callback=None, chunk_seconds=12,
                 cleanup_provider=None):
        if (isinstance(chunk_seconds, bool) or not math.isfinite(chunk_seconds) or
                not .001 <= chunk_seconds <= 600):
            raise ValueError("Chunk duration must be between .001 and 600 seconds")
        self.store = store
        self.state_dir = _private_directory(state_dir)
        self.recordings = _private_directory(self.state_dir / "recordings")
        self.audio_retention = AudioRetention(store, self.recordings)
        self.engine_factory = engine_factory
        self.source_provider = source_provider
        self.profile_provider = profile_provider or store.personalization
        self.cleanup_provider = cleanup_provider or (lambda: True)
        self.event_callback = event_callback
        self.chunk_frames = max(1, round(chunk_seconds * self.RATE))
        self._changed = threading.Condition(threading.RLock())
        self._jobs = queue.Queue(maxsize=self.QUEUE_LIMIT)
        self._closed = threading.Event()
        self._sessions = {}
        self._current = None
        self._active = None
        self._producers = 0
        self._pending = 0
        self._finishing = 0
        for row in self.store.list_sessions():
            if row["state"] in {"preparing", "recording", "captured", "transcribing"}:
                self.store.update_session(row["id"], state="interrupted", error="interrupted")
        self._worker = threading.Thread(target=self._infer, name="local-inference", daemon=True)
        self._worker.start()
        self._cleanup_thread = threading.Thread(target=self.audio_retention.sweep,
                                                args=(self._closed,),
                                                name="audio-cleanup", daemon=True)
        self._cleanup_thread.start()

    def _context(self, sid, kind, chunk_frames):
        try:
            if str(uuid.UUID(sid)) != sid or kind not in {"dictation", "meeting"}:
                raise ValueError("identity")
        except (ValueError, TypeError, AttributeError) as exc:
            raise ControllerError("session") from exc
        return _Session(sid, kind, self.recordings / sid, chunk_frames,
                        clean_fillers=kind == 'dictation' and bool(self.cleanup_provider()))

    def start(self, kind="dictation", title="", notes=""):
        if kind not in {"dictation", "meeting"}:
            raise ControllerError("kind")
        with self._changed:
            if self._closed.is_set():
                raise ControllerError("closed")
            if self._active is not None or self._producers >= self.MAX_PRODUCERS:
                raise ControllerError("active")
            sid = self.store.create_session(kind, title=title, notes=notes)
            ctx = self._context(sid, kind, self.chunk_frames)
            self._retire_current()
            self._sessions[sid] = ctx
            self._current = self._active = ctx
            self._producers += 1
            thread = threading.Thread(target=self._record, args=(ctx,),
                                      name="session-" + sid[:8], daemon=True)
            thread.start()
        self._publish(ctx)
        return sid

    def stop(self):
        """Publish Stop immediately; capture draining never runs in the caller."""
        with self._changed:
            ctx = self._active
            if ctx is None:
                return
            ctx.stop.set()
            if not ctx.error:
                ctx.state = "stopping"
            self._changed.notify_all()
        self._publish(ctx)

    def status(self):
        with self._changed:
            ctx = self._current
            capture_active = ctx is not None and ctx is self._active and not ctx.capture_done
            message = ("idle" if ctx is None else ctx.error or
                       ("preview-engine" if ctx.preview_error and capture_active else
                        "backlog" if ctx.backlog and capture_active else ctx.state))
            return {"sessionId": None if ctx is None else ctx.sid,
                    "kind": None if ctx is None else ctx.kind,
                    "state": "idle" if ctx is None else ctx.state,
                    "message": _MESSAGES.get(message,
                                             "The session failed. Audio is retained."),
                    "capture": {} if ctx is None else copy.deepcopy(ctx.capture),
                    "captureActive": capture_active,
                    "backlog": False if ctx is None else ctx.backlog,
                    "previewError": None if ctx is None else (ctx.preview_error or None),
                    "limitReached": False if ctx is None else ctx.limit_reached,
                    "canStart": not self._closed.is_set() and self._active is None and
                                self._producers < self.MAX_PRODUCERS,
                    "queuedJobs": self._jobs.qsize()}

    def _publish(self, ctx):
        with self._changed:
            current = ctx is self._current
        if current and self.event_callback is not None:
            try:
                self.event_callback(self.status())
            except Exception:
                pass

    def _state(self, ctx, state):
        with ctx.persistence:
            with self._changed:
                if ctx.error or (state == "recording" and ctx.stop.is_set()):
                    return
                ctx.state = state
                self._changed.notify_all()
            self.store.update_session(ctx.sid, state=state, error=ctx.preview_error)
        self._publish(ctx)

    def _preview_failed(self, ctx):
        with self._changed:
            live = ctx.kind == "meeting" and not ctx.capture_done and not ctx.error
            if live:
                ctx.preview_error = "engine"
                ctx.backlog = True
        if not live:
            self._fail(ctx, "engine")
            return
        try:
            with ctx.persistence:
                with self._changed:
                    still_live = not ctx.capture_done and not ctx.error
                if still_live:
                    self.store.update_session(ctx.sid, state="recording", error="engine")
            if not still_live:
                self._fail(ctx, "engine")
        except Exception:
            self._fail(ctx, "storage")
        self._publish(ctx)

    def _fail(self, ctx, category):
        with self._changed:
            if not ctx.error:
                ctx.error = category
            ctx.state = ("interrupted" if ctx.error in {
                "cancelled", "cancelled-before-ready", "interrupted", "not-ready"} else "failed")
            ctx.stop.set()
            self._changed.notify_all()
        try:
            with ctx.persistence:
                self.store.update_session(ctx.sid, state=ctx.state, error=ctx.error)
        except Exception:
            with self._changed:
                ctx.error = "storage"
                ctx.state = "failed"
        self._publish(ctx)

    def _manifest(self, ctx):
        _private_directory(ctx.directory)
        path = ctx.directory / "session.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump({"schemaVersion": 1, "kind": ctx.kind, "rate": self.RATE,
                       "chunkFrames": ctx.chunk_frames}, output)
            output.flush()
            os.fsync(output.fileno())

    def _record(self, ctx):
        recorder = None
        try:
            sources = self.source_provider(ctx.kind)
            if ctx.stop.is_set() or self._closed.is_set():
                self._fail(ctx, "cancelled-before-ready")
                return
            expected = {"microphone", "computer"} if ctx.kind == "meeting" else {"microphone"}
            if not isinstance(sources, dict) or set(sources) != expected:
                raise ControllerError("sources")
            self._manifest(ctx)
            recorder = PulseRecorder(ctx.directory, sources=sources)
            if ctx.stop.is_set():
                self._fail(ctx, "cancelled-before-ready")
                return
            recorder.start()
            ctx.recording_started = time.monotonic()
            while not ctx.stop.is_set():
                metadata = recorder.status()
                with self._changed:
                    ctx.capture = metadata
                if metadata.get("errors"):
                    raise ControllerError("capture")
                if (ctx.kind == "meeting" and not metadata.get("ready") and
                        time.monotonic() - ctx.recording_started >= self.MAX_MEETING_SECONDS):
                    ctx.limit_reached = True
                    self._fail(ctx, "not-ready")
                    break
                if recorder.wait_ready(timeout=.025):
                    with self._changed:
                        if ctx.stop.is_set() or self._closed.is_set():
                            break
                        ctx.ready = True
                        ctx.capture = recorder.status()
                    self._state(ctx, "recording")
                    break
            if not ctx.ready:
                self._fail(ctx, "cancelled-before-ready")
            while ctx.ready and not ctx.stop.is_set():
                metadata = recorder.status()
                with self._changed:
                    ctx.capture = metadata
                if metadata.get("errors"):
                    raise ControllerError("capture")
                if ctx.kind == "meeting":
                    if (max(t["frames"] for t in metadata["tracks"].values()) >=
                            self.MAX_MEETING_SECONDS * self.RATE or
                            time.monotonic() - ctx.recording_started >= self.MAX_MEETING_SECONDS):
                        ctx.limit_reached = True
                        ctx.stop.set()
                    elif not ctx.preview_error:
                        if ctx.backlog and self._jobs.qsize() < self.QUEUE_LIMIT // 2:
                            ctx.backlog = False
                        if not ctx.backlog:
                            try:
                                self._schedule_tracks(ctx, metadata["tracks"], final=False)
                            except ControllerError as exc:
                                if exc.category != "queue-full":
                                    raise
                                ctx.backlog = True
                elif metadata["tracks"]["microphone"]["frames"] > self.MAX_DICTATION_FRAMES:
                    raise ControllerError("dictation-limit")
                self._publish(ctx)
                ctx.stop.wait(.025)
        except ControllerError as exc:
            self._fail(ctx, exc.category)
        except Exception:
            self._fail(ctx, "capture")
        finally:
            if recorder is not None:
                try:
                    metadata = recorder.stop(timeout=5)
                    with self._changed:
                        ctx.capture = metadata
                    if ctx.ready and (metadata.get("errors") or not metadata.get("finalized")):
                        self._fail(ctx, "capture")
                except Exception:
                    self._fail(ctx, "capture")
            ctx.capture_done = True
            with self._changed:
                if self._active is ctx:
                    self._active = None
                self._changed.notify_all()
            try:
                if ctx.preview_error and not ctx.error:
                    self._fail(ctx, ctx.preview_error)
                if ctx.ready and not ctx.error and not self._closed.is_set():
                    self._state(ctx, "captured")
                    self._schedule_tracks(ctx, ctx.capture["tracks"], final=True, retry=True)
                    ctx.backlog = False
                elif self._closed.is_set() and not ctx.error:
                    self._fail(ctx, "cancelled")
            except ControllerError as exc:
                self._fail(ctx, exc.category)
            except Exception:
                self._fail(ctx, "storage")
            self._producer_finished(ctx)

    def _clip(self, ctx, source, ordinal, start, end):
        directory = _private_directory(ctx.directory / "chunks")
        path = directory / f"{source}-{ordinal:08d}.wav"
        raw = ctx.directory / (source + ".wav")
        if path.exists() or path.is_symlink():
            if _audio_frames(path) != end - start:
                raise ControllerError("chunk-conflict")
            with raw.open("rb") as original, path.open("rb") as clip:
                original.seek(44 + start * 2)
                clip.seek(44)
                remaining = (end - start) * 2
                while remaining:
                    size = min(32768, remaining)
                    if original.read(size) != clip.read(size):
                        raise ControllerError("chunk-conflict")
                    remaining -= size
            return path
        temporary = directory / (".chunk-" + uuid.uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output, raw.open("rb") as original:
                original.seek(44 + start * 2)
                with wave.open(output, "wb") as audio:
                    audio.setparams((1, 2, self.RATE, 0, "NONE", "not compressed"))
                    remaining = (end - start) * 2
                    while remaining:
                        size = min(32768, remaining)
                        data = original.read(size)
                        if len(data) != size:
                            raise ControllerError("audio")
                        audio.writeframesraw(data)
                        remaining -= size
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def _schedule_tracks(self, ctx, tracks, *, final, retry=False):
        for source in sorted(tracks):
            frames = tracks[source]["frames"]
            if ctx.kind == "dictation":
                if frames > self.MAX_DICTATION_FRAMES:
                    raise ControllerError("dictation-limit")
                if final and frames:
                    self._schedule(ctx, source, 0, 0, frames, retry=retry)
                elif final:
                    raise ControllerError("audio")
                continue
            ordinal = ctx.next.get(source, 0)
            while (ordinal + 1) * ctx.chunk_frames <= frames and not ctx.error:
                self._schedule(ctx, source, ordinal, ordinal * ctx.chunk_frames,
                               (ordinal + 1) * ctx.chunk_frames, retry=retry)
                ordinal += 1
                ctx.next[source] = ordinal
            if final and not ctx.error and ordinal * ctx.chunk_frames < frames:
                self._schedule(ctx, source, ordinal, ordinal * ctx.chunk_frames, frames, retry=retry)
                ctx.next[source] = ordinal + 1

    def _schedule(self, ctx, source, ordinal, start, end, *, retry):
        key = source, ordinal
        old = ctx.existing.get(key)
        if old is not None:
            if abs(old["start"] - start / self.RATE) > 1e-9 or abs(old["end"] - end / self.RATE) > 1e-9:
                raise ControllerError("chunk-conflict")
            return
        if key in ctx.planned or ctx.error:
            return
        if self._closed.is_set():
            raise ControllerError("cancelled")
        path = self._clip(ctx, source, ordinal, start, end)
        job = _Job(ctx, source, ordinal, start, end, path)
        with self._changed:
            if self._closed.is_set():
                raise ControllerError("cancelled")
            ctx.planned.add(key)
            ctx.pending += 1
            self._pending += 1
        while True:
            try:
                self._jobs.put(job, block=retry, timeout=.05 if retry else None)
                return
            except queue.Full:
                if retry and not self._closed.is_set() and not ctx.error:
                    continue
                with self._changed:
                    ctx.pending -= 1
                    self._pending -= 1
                    ctx.planned.discard(key)
                raise ControllerError("cancelled" if self._closed.is_set() else "queue-full")

    def _infer(self):
        engine = None
        engine_session = None
        while True:
            with self._changed:
                if self._closed.is_set() and not self._pending and not self._producers:
                    return
            try:
                job = self._jobs.get(timeout=.05)
            except queue.Empty:
                continue
            ctx = job.session
            try:
                if self._closed.is_set():
                    self._fail(ctx, "cancelled")
                elif not ctx.error and not ctx.preview_error:
                    if ctx.capture_done:
                        self._state(ctx, "transcribing")
                    if ctx.profile is None:
                        ctx.profile = self.profile_provider()
                    if engine is None or engine_session is not ctx:
                        engine = self.engine_factory()
                        engine_session = ctx
                    raw = engine.transcribe(job.path, ctx.cancel)
                    if self._closed.is_set() or ctx.cancel.is_set():
                        raise ControllerError("cancelled")
                    processed, original = apply_personalization(raw, ctx.profile,
                                                               clean_fillers=ctx.clean_fillers)
                    self.store.add_segment(ctx.sid, job.source, job.ordinal,
                                           job.start / self.RATE, job.end / self.RATE,
                                           processed, raw_text=original)
            except ControllerError as exc:
                engine = None
                self._fail(ctx, exc.category)
            except Exception:
                engine = None
                self._preview_failed(ctx)
            finally:
                with self._changed:
                    self._finishing += 1
                    ctx.finishing += 1
                    ctx.pending -= 1
                    self._pending -= 1
                self._jobs.task_done()
                try:
                    self._complete(ctx)
                    self._publish(ctx)
                finally:
                    with self._changed:
                        self._finishing -= 1
                        ctx.finishing -= 1
                        self._retire_finished(ctx)
                        self._changed.notify_all()

    def _producer_finished(self, ctx):
        with self._changed:
            self._finishing += 1
            ctx.finishing += 1
            ctx.producer_done = True
            self._producers -= 1
            if self._active is ctx:
                self._active = None
        try:
            self._complete(ctx)
        finally:
            with self._changed:
                self._finishing -= 1
                ctx.finishing -= 1
                self._retire_finished(ctx)
                self._changed.notify_all()

    def _complete(self, ctx):
        with self._changed:
            if self._sessions.get(ctx.sid) is not ctx:
                return
            done = ctx.producer_done and not ctx.pending
        if done and not ctx.error:
            try:
                self._state(ctx, "complete")
            except Exception:
                self._fail(ctx, "storage")
            else:
                self.audio_retention.clean(ctx.sid)
    def _retire_finished(self, ctx):
        # Both producer and inference completion may be in flight. Keep the
        # identity registered until the final callback has stopped writing.
        if (ctx.producer_done and not ctx.pending and not ctx.finishing and
                ctx is not self._current and self._sessions.get(ctx.sid) is ctx):
            self._sessions.pop(ctx.sid, None)

    def retry(self, sid):
        with self.audio_retention.lock, self._changed:
            if self._closed.is_set():
                raise ControllerError("closed")
            previous = self._sessions.get(sid)
            if self._producers or (previous is not None and (
                    previous.pending or previous.finishing or not previous.producer_done)):
                raise ControllerError("active")
            row = self.store.get_session(sid)
            if row.get('audio_state', 'retained') != 'retained':
                raise ControllerError('audio-removed')
            ctx = self._context(sid, row["kind"], self.chunk_frames)
            self._retire_current()
            ctx.capture_done = True
            ctx.existing = {(s["source"], s["ordinal"]): s for s in row["segments"]}
            self._sessions[sid] = self._current = ctx
            self._producers += 1
            thread = threading.Thread(target=self._retry, args=(ctx,),
                                      name="retry-" + sid[:8], daemon=True)
            thread.start()
        return sid

    def _retire_current(self):
        old = self._current
        if (old is not None and old.producer_done and not old.pending and not old.finishing and
                self._sessions.get(old.sid) is old):
            self._sessions.pop(old.sid, None)

    def _retry(self, ctx):
        try:
            if any(p.is_symlink() for p in (ctx.directory, *ctx.directory.parents)):
                raise ControllerError("audio")
            manifest = ctx.directory / "session.json"
            if manifest.is_symlink() or manifest.stat().st_size > 4096:
                raise ControllerError("audio")
            data = json.loads(manifest.read_text())
            frames = data["chunkFrames"]
            if (data.get("schemaVersion") != 1 or data.get("kind") != ctx.kind or
                    data.get("rate") != self.RATE or type(frames) is not int or
                    not 1 <= frames <= self.MAX_DICTATION_FRAMES):
                raise ControllerError("audio")
            ctx.chunk_frames = frames
            sources = ("microphone", "computer") if ctx.kind == "meeting" else ("microphone",)
            tracks = {}
            for source in sources:
                path = ctx.directory / (source + ".wav")
                count = _audio_frames(path)
                if not count:
                    raise ControllerError("audio")
                tracks[source] = {"path": path, "frames": count, "rate": self.RATE}
            ctx.capture = {"tracks": tracks, "ready": False, "recovered": True}
            self._state(ctx, "captured")
            self._schedule_tracks(ctx, tracks, final=True, retry=True)
        except ControllerError as exc:
            self._fail(ctx, exc.category)
        except Exception:
            self._fail(ctx, "audio")
        finally:
            self._producer_finished(ctx)

    def wait_idle(self, timeout):
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("A finite nonnegative timeout is required")
        deadline = time.monotonic() + timeout
        with self._changed:
            while self._producers or self._pending or self._finishing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)
            return True

    def close(self, timeout=5):
        with self._changed:
            self._closed.set()
        self.stop()
        with self._changed:
            for ctx in self._sessions.values():
                ctx.cancel.set()
        deadline = time.monotonic() + max(0, timeout)
        idle = self.wait_idle(max(0, deadline - time.monotonic()))
        self._worker.join(timeout=max(0, deadline - time.monotonic()))
        self._cleanup_thread.join(timeout=max(0, deadline - time.monotonic()))
        return idle and not self._worker.is_alive() and not self._cleanup_thread.is_alive()
