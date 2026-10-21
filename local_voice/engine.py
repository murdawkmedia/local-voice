"""Supervised local-only speech process; the GUI never parses terminal text."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import time
import uuid
import wave

from .process_guard import guarded_command


class EngineError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__({
            "setup": "The local speech engine or verified model is unavailable.",
            "audio": "The saved audio cannot be processed by this engine.",
            "protocol": "The local engine returned an invalid result. Audio is retained.",
            "process": "The local speech engine failed. Audio is retained.",
            "timeout": "The local speech job exceeded its time limit. Audio is retained.",
            "cancelled": "The local speech job was cancelled. Audio is retained.",
            "diagnostic-limit": "The local engine exceeded its diagnostic limit.",
        }.get(category, "Local speech processing failed."))


@dataclass(frozen=True)
class EngineSpec:
    kind: str
    binary: Path
    model_dir: Path
    timeout: float = 90
    threads: int = 4
    language: str = "en"
    prompt: str = ""
    vulkan_icd: Path | None = None


class LocalEngine:
    def __init__(self, spec, work_dir):
        self.spec = spec
        self.work_dir = Path(work_dir)
        self.last_run = {}
        self._model_state = None
        if spec.kind not in {"whisper", "parakeet"} or not 0 < spec.timeout <= 600:
            raise EngineError("setup")
        if not 1 <= spec.threads <= 8 or not spec.binary.is_file() or not os.access(spec.binary, os.X_OK):
            raise EngineError("setup")
        if not isinstance(spec.prompt, str) or len(spec.prompt) > 4096:
            raise EngineError("setup")
        self._verify_vulkan_icd()
        self._verify_model()

    def _verify_vulkan_icd(self):
        """Accept only a bounded, administrator-installed loader manifest."""
        self._vulkan_icd = None
        if self.spec.vulkan_icd is None:
            return
        try:
            path = Path(self.spec.vulkan_icd)
            if (self.spec.kind != 'whisper' or not path.is_absolute()
                    or len(str(path)) > 4096 or ':' in str(path)
                    or path.parent not in {Path('/usr/share/vulkan/icd.d'), Path('/etc/vulkan/icd.d')}
                    or path.suffix != '.json' or path.resolve(strict=True) != path):
                raise ValueError('system manifest')
            for ancestor in (path, *path.parents):
                info = ancestor.lstat()
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise ValueError('writable manifest')
            if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > 65536:
                raise ValueError('manifest size')
            with path.open('rb') as stream:
                payload = stream.read(65537)
            if len(payload) > 65536:
                raise ValueError('manifest size')
            manifest = json.loads(payload)
            driver = manifest['ICD']
            if (manifest.get('file_format_version') not in {'1.0.0', '1.0.1'}
                    or not isinstance(driver['library_path'], str)
                    or not 0 < len(driver['library_path']) <= 4096
                    or not re.fullmatch(r'\d+\.\d+\.\d+', driver['api_version'])):
                raise ValueError('manifest schema')
            self._vulkan_icd = path
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise EngineError('setup') from exc

    def _verify_model(self):
        root = Path(self.spec.model_dir)
        manifest = root / "manifest.json"
        try:
            if manifest.is_symlink() or manifest.stat().st_size > 65536:
                raise ValueError("manifest")
            files = json.loads(manifest.read_text())["files"]
            required = ({"encoder-model.int8.onnx", "decoder_joint-model.int8.onnx", "vocab.txt", "config.json"}
                        if self.spec.kind == "parakeet" else
                        {n for n in files if n.startswith("ggml-") and n.endswith(".bin")})
            if not required or (self.spec.kind == "whisper" and len(required) != 1) or not required.issubset(files):
                raise ValueError("model")
            if any(Path(name).name != name or not name for name in files):
                raise ValueError("path")
            state = {name: ((root / name).stat().st_size, (root / name).stat().st_mtime_ns)
                     for name in files}
            state["manifest.json"] = (manifest.stat().st_size, manifest.stat().st_mtime_ns)
            if state != self._model_state:
                for name, info in files.items():
                    path = root / name
                    if path.is_symlink() or path.stat().st_size != info["bytes"]:
                        raise ValueError("size")
                    h = hashlib.sha256()
                    with path.open("rb") as f:
                        while chunk := f.read(1_048_576):
                            h.update(chunk)
                    if h.hexdigest() != info["sha256"]:
                        raise ValueError("checksum")
                self._model_state = state
            self._model_path = (root if self.spec.kind == "parakeet" else root / next(iter(required))).resolve()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EngineError("setup") from exc

    def _configuration(self):
        spec = self.spec
        model = json.dumps(str(self._model_path), ensure_ascii=False)
        text = (f'engine = "{spec.kind}"\n[hotkey]\nenabled = false\n'
                '[vad]\nenabled = false\n[whisper]\nmode = "local"\n'
                f'model = {model}\nlanguage = {json.dumps(spec.language)}\n'
                f'threads = {spec.threads}\ngpu_isolation = false\nstreaming = false\n')
        if spec.kind == 'whisper' and spec.prompt:
            text += f'initial_prompt = {json.dumps(spec.prompt, ensure_ascii=False)}\n'
        if self._vulkan_icd is not None:
            text += 'gpu_device = 0\n'
        if spec.kind == "parakeet":
            text += f'[parakeet]\nmodel = {model}\nmodel_type = "tdt"\nstreaming = false\n'
        return text

    @staticmethod
    def _stop(process):
        if process.poll() is not None:
            return True
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return False
        return True

    def transcribe(self, wav_path, cancel_event):
        start = time.monotonic()
        self.last_run = {"engine": self.spec.kind, "mode": "local-file", "outcome": "starting"}
        try:
            if cancel_event.is_set():
                raise EngineError("cancelled")
            self._verify_vulkan_icd()
            if self._vulkan_icd is not None:
                self.last_run.update(gpuSelection='vulkan-device-0', vulkanIcd=self._vulkan_icd.name)
            self._verify_model()
            try:
                with wave.open(str(wav_path), "rb") as audio:
                    if not 0 < audio.getnframes() / audio.getframerate() <= 600:
                        raise ValueError("duration")
                    if audio.getsampwidth() == 2:
                        expected = audio.getnframes() * audio.getnchannels() * 2
                        read = 0
                        silent = True
                        while chunk := audio.readframes(16384):
                            read += len(chunk)
                            if chunk.strip(b'\0'):
                                silent = False
                                break
                            if cancel_event.is_set():
                                raise EngineError('cancelled')
                        if silent:
                            if read != expected:
                                raise ValueError('truncated')
                            self.last_run.update(outcome='empty', reason='digital-silence', cleanupComplete=True)
                            return ''
            except (OSError, ValueError, wave.Error, EOFError) as exc:
                raise EngineError("audio") from exc
            if self.work_dir.is_symlink():
                raise EngineError("setup")
            self.work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.work_dir, 0o700)
            with tempfile.TemporaryDirectory(prefix="speech-", dir=self.work_dir) as name:
                job = Path(name)
                config = job / "config.toml"
                config.write_text(self._configuration())
                config.chmod(0o600)
                result_path = job / "result.json"
                cpus = ",".join(map(str, sorted(os.sched_getaffinity(0))[:self.spec.threads]))
                worker = [str(self.spec.binary.resolve()), "--config", str(config),
                          "--quiet", "--no-auto-submit", "transcribe",
                          str(Path(wav_path).resolve()), "--engine", self.spec.kind,
                          "--result-json", str(result_path)]
                worker = ["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect",
                          "--unit=local-voice-speech-" + uuid.uuid4().hex + ".scope",
                          "-p", "MemoryMax=2G", "-p", "MemorySwapMax=128M",
                          "-p", f"CPUQuota={self.spec.threads * 100}%", *worker]
                command = ["/usr/bin/unshare", "--user", "--map-root-user", "--net",
                           "/usr/bin/taskset", "-c", cpus, *guarded_command(worker)]
                env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "XDG_RUNTIME_DIR", "LD_LIBRARY_PATH")
                       if k in os.environ}
                env.update({"RUST_LOG": "off", "OMP_NUM_THREADS": str(self.spec.threads),
                            "OPENBLAS_NUM_THREADS": str(self.spec.threads)})
                if self._vulkan_icd is not None:
                    env['VK_DRIVER_FILES'] = str(self._vulkan_icd)
                diagnostics = bytearray()
                too_much = False
                try:
                    process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.PIPE, env=env,
                                               start_new_session=True)
                except OSError as exc:
                    raise EngineError("setup") from exc

                # Read the file descriptor directly. A BufferedReader held by a
                # descendant must never make the supervisor's close() block.
                os.set_blocking(process.stderr.fileno(), False)

                def drain():
                    nonlocal too_much
                    # Bound work per poll even if a failed engine writes forever.
                    for _ in range(16):
                        try:
                            chunk = os.read(process.stderr.fileno(), 8192)
                        except BlockingIOError:
                            break
                        if not chunk:
                            break
                        remaining = max(0, 65536 - len(diagnostics))
                        diagnostics.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            too_much = True

                try:
                    while True:
                        drain()
                        if cancel_event.is_set():
                            raise EngineError("cancelled")
                        if time.monotonic() - start > self.spec.timeout:
                            raise EngineError("timeout")
                        if too_much:
                            raise EngineError("diagnostic-limit")
                        if process.poll() is not None:
                            break
                        time.sleep(0.02)
                    self.last_run["returnCode"] = process.returncode
                    if process.returncode:
                        raise EngineError("process")
                finally:
                    try:
                        self.last_run["cleanupComplete"] = self._stop(process)
                    finally:
                        try:
                            drain()
                        finally:
                            process.stderr.close()
                            self.last_run["diagnosticBytesRetained"] = len(diagnostics)
                            # Retain only a fixed-format device identity, never
                            # general diagnostic lines or recognized speech.
                            if self._vulkan_icd is not None:
                                device = re.search(rb'^ggml_vulkan: 0 = (NVIDIA [A-Za-z0-9 .()_+/-]{1,120})',
                                                   diagnostics, re.MULTILINE)
                                if device:
                                    self.last_run['vulkanDevice'] = device.group(1).decode('ascii').strip()
                if too_much:
                    raise EngineError("diagnostic-limit")
                if not self.last_run["cleanupComplete"]:
                    raise EngineError("process")
                if cancel_event.is_set():
                    raise EngineError("cancelled")
                try:
                    if result_path.is_symlink() or result_path.stat().st_size > 1_048_576:
                        raise ValueError("size")
                    result = json.loads(result_path.read_text())
                    if (result.get("schemaVersion") != 1 or result.get("engine") != self.spec.kind
                            or result.get("status") not in {"success", "empty"}
                            or not isinstance(result.get("text"), str)
                            or (result["status"] == "empty") != (result["text"] == "")):
                        raise ValueError("schema")
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    raise EngineError("protocol") from exc
                self.last_run["outcome"] = result["status"]
                return result["text"]
        except EngineError as exc:
            self.last_run["outcome"] = exc.category
            raise
        finally:
            self.last_run["elapsedSeconds"] = round(time.monotonic() - start, 3)
