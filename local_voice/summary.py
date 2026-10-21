"""Optional offline summaries with references to preserved transcript segments."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from .process_guard import guarded_command


class SummaryError(RuntimeError):
    def __init__(self, category, reason=None):
        self.category = category
        self.reason = reason
        super().__init__({
            "disabled": "Local summaries are turned off.",
            "setup": "The verified local summary model or runtime is unavailable.",
            "input": "The meeting exceeds the local summary input limits.",
            "limit": "The meeting could not be reduced within the local summary limits.",
            "protocol": "The local summary returned invalid segment references or text.",
            "cancelled": "The local summary was cancelled. The transcript is retained.",
            "timeout": "The local summary exceeded its time limit. The transcript is retained.",
            "process": "The local summary worker failed. The transcript is retained.",
        }.get(category, "Local summary failed. The transcript is retained."))


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def normalize_segments(segments):
    if not isinstance(segments, (list, tuple)) or len(segments) > 10000:
        raise SummaryError("input")
    records, identifiers, size = [], set(), 0
    for segment in segments:
        if not isinstance(segment, dict):
            raise SummaryError("input")
        identifier, text = segment.get("id"), segment.get("text")
        if (not isinstance(identifier, str) or not 0 < len(identifier) <= 128
                or identifier in identifiers or not isinstance(text, str)):
            raise SummaryError("input")
        identifiers.add(identifier)
        try:
            size += len(text.encode("utf-8")) + len(identifier.encode("utf-8"))
        except UnicodeError as exc:
            raise SummaryError("input") from exc
        if size > 2_097_152:
            raise SummaryError("input")
        if text.strip():
            records.append({"text": text, "segment_ids": [identifier]})
    return records


def validate_result(value, allowed):
    if not isinstance(value, dict) or set(value) != {"items"} or not isinstance(value["items"], list):
        raise SummaryError("protocol")
    if len(value["items"]) > 12:
        raise SummaryError("protocol")
    items = []
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != {"text", "segment_ids"}:
            raise SummaryError("protocol")
        text, refs = item["text"], item["segment_ids"]
        if (not isinstance(text, str) or not text.strip() or len(text) > 2000
                or not isinstance(refs, list) or not refs or len(refs) > 10000
                or any(not isinstance(ref, str) or ref not in allowed for ref in refs)):
            raise SummaryError("protocol")
        try:
            text.encode("utf-8")
        except UnicodeError as exc:
            raise SummaryError("protocol") from exc
        items.append({"text": text, "segment_ids": list(dict.fromkeys(refs))})
    return {"items": items}


def chunk_records(records, *, max_bytes=6000):
    """Split Unicode text without dropping content or its original references."""
    chunks, current = [], []
    for record in records:
        text = record["text"]
        while text:
            candidate = {"text": text, "segment_ids": record["segment_ids"]}
            if len(_encoded([candidate])) > max_bytes:
                low, high = 0, len(text)
                while low < high:
                    mid = (low + high + 1) // 2
                    if len(_encoded([{**candidate, "text": text[:mid]}])) <= max_bytes:
                        low = mid
                    else:
                        high = mid - 1
                if not low:
                    raise SummaryError("limit")
                candidate["text"], text = text[:low], text[low:]
            else:
                text = ""
            if current and (len(current) >= 64 or len(_encoded(current + [candidate])) > max_bytes):
                chunks.append(current)
                current = []
            current.append(candidate)
    if current:
        chunks.append(current)
    if len(chunks) > 512:
        raise SummaryError("limit")
    return chunks


def summarize_records(records, generate, *, max_bytes=6000):
    allowed = {ref for record in records for ref in record["segment_ids"]}
    calls = 0
    for _level in range(8):
        chunks = chunk_records(records, max_bytes=max_bytes)
        if not chunks:
            return {"items": []}, calls
        reduced = []
        for chunk in chunks:
            calls += 1
            if calls > 512:
                raise SummaryError("limit")
            chunk_ids = {ref for record in chunk for ref in record["segment_ids"]}
            output = validate_result(generate(chunk), chunk_ids)
            if not output["items"]:
                raise SummaryError("protocol")
            reduced.extend(output["items"])
        if len(chunks) == 1:
            return validate_result({"items": reduced}, allowed), calls
        if len(_encoded(reduced)) >= len(_encoded(records)):
            raise SummaryError("limit")
        records = reduced
    raise SummaryError("limit")


@dataclass(frozen=True)
class SummarySpec:
    model_dir: Path = Path.home() / ".local/share/local-voice/models/qwen3-4b-instruct-2507-q4_k_m"
    timeout: float = 1200
    threads: int = 4


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while block := stream.read(1_048_576):
            digest.update(block)
    return digest.hexdigest()


def verified_manifest(model_dir):
    try:
        root = Path(model_dir)
        path = root / "manifest.json"
        if root.is_symlink() or path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError()
        manifest = json.loads(path.read_text())
        if manifest["schemaVersion"] != 1 or manifest["backend"] != "llama-server-cuda":
            raise ValueError()
        runtime_root = Path("/usr/lib/ollama")
        if (manifest["runner"] != str(runtime_root / "llama-server")
                or manifest["cudaBackend"] != str(runtime_root / "cuda_v13/libggml-cuda.so")
                or any(not Path(name).is_absolute() or not Path(name).resolve().is_relative_to(runtime_root)
                       for name in manifest["runtimeFiles"])):
            raise ValueError()
        model = manifest["model"]
        if Path(model["file"]).name != model["file"]:
            raise ValueError()
        files = [(root / model["file"], model), *[(Path(p), info) for p, info in manifest["runtimeFiles"].items()]]
        for file, info in files:
            if file.is_symlink() or not file.is_file() or file.stat().st_size != info["bytes"] or sha256(file) != info["sha256"]:
                raise ValueError()
        if manifest["runner"] not in manifest["runtimeFiles"] or manifest["cudaBackend"] not in manifest["runtimeFiles"]:
            raise ValueError()
        manifest["modelPath"] = str((root / model["file"]).resolve())
        return manifest
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SummaryError("setup") from exc


def worker_command(job, threads):
    cpus = ",".join(map(str, sorted(os.sched_getaffinity(0))[:threads]))
    worker = ["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect",
              "--unit=local-voice-summary-" + uuid.uuid4().hex + ".scope",
              "-p", "MemoryMax=3G", "-p", "MemorySwapMax=128M", "-p", "CPUQuota=400%",
              sys.executable, str(Path(__file__).with_name("summary_worker.py").resolve()), str(job)]
    return ["/usr/bin/unshare", "--user", "--map-root-user", "--net",
            "/usr/bin/taskset", "-c", cpus, *guarded_command(worker)]


class LocalSummarizer:
    def __init__(self, spec=None, work_dir=None, *, enabled=False):
        self.spec = spec or SummarySpec()
        self.work_dir = Path(work_dir or Path.home() / ".local/share/local-voice/jobs")
        self.enabled = enabled
        self.last_run = {}

    def summarize(self, segments, cancel_event):
        began = time.monotonic()
        self.last_run = {"outcome": "starting", "model": "qwen3-4b-instruct-2507-q4_k_m"}
        try:
            if not self.enabled:
                raise SummaryError("disabled")
            if cancel_event.is_set():
                raise SummaryError("cancelled")
            records = normalize_segments(segments)
            if not records:
                self.last_run["outcome"] = "empty"
                return {"items": []}
            if not 0 < self.spec.timeout <= 1200 or not 1 <= self.spec.threads <= 4:
                raise SummaryError("setup")
            manifest = verified_manifest(self.spec.model_dir)
            if self.work_dir.is_symlink():
                raise SummaryError("setup")
            self.work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.work_dir.chmod(0o700)
            with tempfile.TemporaryDirectory(prefix="summary-", dir=self.work_dir) as name:
                job = Path(name)
                payload = {"records": records, "manifest": manifest, "threads": self.spec.threads,
                           "hostNetworkNamespace": os.readlink("/proc/self/ns/net")}
                with (job / "input.json").open("x", encoding="utf-8") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(payload, stream, ensure_ascii=False)
                env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS") if key in os.environ}
                try:
                    process = subprocess.Popen(worker_command(job, self.spec.threads), env=env,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                               start_new_session=True)
                except OSError as exc:
                    raise SummaryError("setup") from exc
                os.set_blocking(process.stderr.fileno(), False)
                diagnostics = 0
                try:
                    while True:
                        for _ in range(16):
                            try:
                                block = os.read(process.stderr.fileno(), 8192)
                            except BlockingIOError:
                                break
                            if not block:
                                break
                            diagnostics += len(block)
                        if diagnostics > 65536:
                            raise SummaryError("process")
                        if cancel_event.is_set():
                            raise SummaryError("cancelled")
                        if time.monotonic() - began > self.spec.timeout:
                            raise SummaryError("timeout")
                        if process.poll() is not None:
                            break
                        time.sleep(.02)
                    self.last_run["returnCode"] = process.returncode
                    if process.returncode:
                        error_path = job / "error.json"
                        try:
                            if not error_path.is_symlink() and error_path.stat().st_size <= 1024:
                                failure = json.loads(error_path.read_text())
                                if failure.get("stage") in {"scope", "scope-limits", "input", "namespace", "network", "load", "generate", "result"}:
                                    self.last_run["workerStage"] = failure["stage"]
                                if failure.get("exception") in {"SummaryError", "OSError", "PermissionError", "KeyError", "ValueError", "TypeError", "TimeoutError", "JSONDecodeError", "ConnectionRefusedError"}:
                                    self.last_run["workerFailure"] = failure["exception"]
                                if failure.get("reason") in {"generation-truncated", "context", "no-progress", "call-limit"}:
                                    self.last_run["workerReason"] = failure["reason"]
                                if failure.get("category") in {"setup", "limit", "protocol", "timeout"}:
                                    raise SummaryError(failure["category"])
                        except (OSError, ValueError, TypeError, AttributeError):
                            pass
                        raise SummaryError("process")
                finally:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                            process.wait(timeout=2)
                        except (ProcessLookupError, subprocess.TimeoutExpired):
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired as exc:
                        raise SummaryError("process") from exc
                    finally:
                        process.stderr.close()
                    self.last_run["cleanupComplete"] = True
                    self.last_run["diagnosticBytes"] = min(diagnostics, 65537)
                try:
                    result = job / "result.json"
                    if result.is_symlink() or result.stat().st_size > 262144:
                        raise ValueError()
                    value = json.loads(result.read_text())
                except (OSError, ValueError) as exc:
                    raise SummaryError("protocol") from exc
                output = validate_result(value, {ref for record in records for ref in record["segment_ids"]})
                metadata_path = job / "metadata.json"
                if metadata_path.exists():
                    try:
                        if metadata_path.is_symlink() or metadata_path.stat().st_size > 1024:
                            raise ValueError()
                        metadata = json.loads(metadata_path.read_text())
                        fields = {"modelCalls", "hostMemoryPeakBytes", "memoryLimitBytes"}
                        if set(metadata) != fields or any(type(metadata[key]) is not int or metadata[key] < 0 for key in fields):
                            raise ValueError()
                        self.last_run.update(metadata)
                    except (OSError, ValueError, TypeError) as exc:
                        raise SummaryError("protocol") from exc
                if cancel_event.is_set():
                    raise SummaryError("cancelled")
                self.last_run["outcome"] = "success"
                return output
        except SummaryError as exc:
            self.last_run["outcome"] = exc.category
            raise
        except (OSError, ValueError, TypeError) as exc:
            self.last_run["outcome"] = "setup"
            raise SummaryError("setup") from exc
        finally:
            self.last_run["elapsedSeconds"] = round(time.monotonic() - began, 3)
