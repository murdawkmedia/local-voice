"""Generated text and child processes only; no microphones or personal data."""

import json
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from local_voice import summary
from local_voice import summary_worker
from local_voice.summary_worker import RunnerClient


def segment(identifier="s1", text="A generated meeting agreed to ship Friday."):
    return {"id": identifier, "text": text, "source": "microphone", "start": 0,
            "end": 1, "raw_text": "must never enter the summary input"}


@contextmanager
def qualification_model_lease(root=None):
    root = Path(root or Path.home() / ".local/share/local-voice/qualification")
    if root.is_symlink():
        raise RuntimeError("qualification lease directory is a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(root / "model.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RuntimeError("invalid qualification lease file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


class SummaryBoundaryTests(unittest.TestCase):
    def setUp(self):
        forbidden = patch.object(summary.subprocess, "Popen", side_effect=AssertionError("Boundary tests cannot launch a model"))
        forbidden.start()
        self.addCleanup(forbidden.stop)

    def test_qualification_model_lease_rejects_overlap(self):
        with tempfile.TemporaryDirectory() as name:
            with qualification_model_lease(name):
                with self.assertRaises(BlockingIOError):
                    with qualification_model_lease(name):
                        self.fail("overlapping model qualification acquired the lease")
    def test_normalization_keeps_unicode_and_omits_raw_text(self):
        records = summary.normalize_segments([segment(text="Café\n東京")])
        self.assertEqual(records, [{"text": "Café\n東京", "segment_ids": ["s1"]}])

    def test_duplicate_ids_and_oversize_input_fail(self):
        for records in ([segment(), segment()], [segment(text="x" * 2_100_000)]):
            with self.assertRaises(summary.SummaryError):
                summary.normalize_segments(records)

    def test_empty_meeting_returns_empty_without_runner(self):
        client = summary.LocalSummarizer(enabled=True)
        self.assertEqual(client.summarize([segment(text=" \n")], threading.Event()), {"items": []})

    def test_summaries_default_off(self):
        with self.assertRaises(summary.SummaryError) as result:
            summary.LocalSummarizer().summarize([segment()], threading.Event())
        self.assertEqual(result.exception.category, "disabled")

    def test_cancel_before_setup(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(summary.SummaryError) as result:
            summary.LocalSummarizer(enabled=True).summarize([segment()], event)
        self.assertEqual(result.exception.category, "cancelled")

    def test_strict_references_and_output_shape(self):
        for value in ({"items": [{"text": "Decision", "segment_ids": ["invented"]}]},
                      {"items": [{"text": "Decision", "segment_ids": []}]},
                      {"items": [{"text": "", "segment_ids": ["s1"]}]},
                      {"items": [{"text": "Decision", "segment_ids": ["s1"], "command": "x"}]},
                      {"items": [], "extra": True}):
            with self.assertRaises(summary.SummaryError):
                summary.validate_result(value, {"s1"})

    def test_references_are_deduplicated_without_changing_text(self):
        result = summary.validate_result({"items": [{"text": "Café\nFriday", "segment_ids": ["s1", "s1"]}]}, {"s1"})
        self.assertEqual(result["items"][0], {"text": "Café\nFriday", "segment_ids": ["s1"]})

    def test_invalid_unicode_from_model_is_a_protocol_error(self):
        with self.assertRaises(summary.SummaryError):
            summary.validate_result({"items": [{"text": "\ud800", "segment_ids": ["s1"]}]}, {"s1"})

    def test_chunking_is_bounded_and_preserves_every_character(self):
        text = "東京 café\n" * 500
        chunks = summary.chunk_records(summary.normalize_segments([segment(text=text)]), max_bytes=500)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(r["text"] for c in chunks for r in c), text)
        self.assertTrue(all(len(json.dumps(c, ensure_ascii=False, separators=(",", ":")).encode()) <= 500 for c in chunks))
        self.assertTrue(all(r["segment_ids"] == ["s1"] for c in chunks for r in c))

    def test_hierarchy_covers_all_chunks_with_original_references(self):
        seen = []
        def generate(records):
            seen.extend(r["text"] for r in records)
            refs = list(dict.fromkeys(ref for r in records for ref in r["segment_ids"]))
            return {"items": [{"text": "Generated combined decision.", "segment_ids": refs}]}
        records = summary.normalize_segments([segment(f"s{i}", "A" * 300) for i in range(20)])
        result, calls = summary.summarize_records(records, generate, max_bytes=1200)
        self.assertGreater(calls, 1)
        self.assertEqual(set(result["items"][0]["segment_ids"]), {f"s{i}" for i in range(20)})
        self.assertEqual(sum(t == "A" * 300 for t in seen), 20)

    def test_hierarchy_rejects_nonshrinking_output(self):
        records = summary.normalize_segments([segment(f"s{i}", "A" * 200) for i in range(12)])
        with self.assertRaises(summary.SummaryError):
            summary.summarize_records(records, lambda r: {"items": r}, max_bytes=600)

    def test_missing_setup_fails_without_fallback(self):
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(summary.SummaryError) as result:
                summary.LocalSummarizer(summary.SummarySpec(model_dir=Path(name) / "absent"),
                                       enabled=True).summarize([segment()], threading.Event())
            self.assertEqual(result.exception.category, "setup")

    def test_unwritable_job_location_has_a_safe_setup_error(self):
        with tempfile.TemporaryDirectory() as name:
            occupied = Path(name) / "occupied"
            occupied.write_text("preserve generated sentinel")
            with patch.object(summary, "verified_manifest", return_value={}):
                with self.assertRaises(summary.SummaryError) as result:
                    summary.LocalSummarizer(work_dir=occupied, enabled=True).summarize([segment()], threading.Event())
            self.assertEqual(result.exception.category, "setup")
            self.assertEqual(occupied.read_text(), "preserve generated sentinel")

    def test_manifest_cannot_request_reads_outside_the_installed_runtime(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            model = root / "model.gguf"
            model.write_bytes(b"x")
            outside = root / "unrelated-sentinel"
            outside.write_bytes(b"y")
            model_info = {"file": model.name, "bytes": 1, "sha256": summary.sha256(model)}
            runtime_info = {"bytes": 1, "sha256": summary.sha256(outside)}
            (root / "manifest.json").write_text(json.dumps({
                "schemaVersion": 1, "backend": "llama-server-cuda", "model": model_info,
                "runner": str(outside), "cudaBackend": str(outside), "runtimeFiles": {str(outside): runtime_info}}))
            def checked_hash(path):
                if Path(path) == outside:
                    self.fail("manifest requested an unrelated file read")
                return model_info["sha256"]
            with patch.object(summary, "sha256", side_effect=checked_hash):
                with self.assertRaises(summary.SummaryError):
                    summary.verified_manifest(root)


class SummaryScopeTests(unittest.TestCase):
    def test_owner_death_kills_scoped_worker_and_server(self):
        self._owner_death_fixture()

    def test_partial_fixture_metadata_does_not_signal_readiness(self):
        self._owner_death_fixture(pause_publication=True)

    def _owner_death_fixture(self, pause_publication=False):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            worker = root / "worker.py"
            worker.write_text(
                "import json,os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                "child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'])\n"
                "cg=Path('/proc/self/cgroup').read_text().strip()\n"
                "limits=Path('/sys/fs/cgroup')/next(line[3:] for line in cg.splitlines() if line.startswith('0::/')).lstrip('/')\n"
                f"marker=Path({str(root / 'pids')!r})\n"
                "temporary=marker.with_suffix('.partial')\n"
                "data=json.dumps({'worker':os.getpid(),'parent':os.getppid(),'group':os.getpgrp(),'child':child.pid,'cgroup':cg,'memory':(limits/'memory.max').read_text(),'swap':(limits/'memory.swap.max').read_text()})\n"
                "with temporary.open('x') as stream:\n"
                " stream.write(data[:len(data)//2]);stream.flush()\n"
                f" if {pause_publication!r}:\n"
                "  deadline=time.monotonic()+5\n"
                "  while not marker.with_name('publish-allowed').exists() and time.monotonic()<deadline:time.sleep(.005)\n"
                "  if not marker.with_name('publish-allowed').exists():raise RuntimeError('Fixture publication timed out')\n"
                " stream.write(data[len(data)//2:]);stream.flush();os.fsync(stream.fileno())\n"
                "os.replace(temporary,marker)\n"
                "time.sleep(30)\n")
            owner = root / "owner.py"
            owner.write_text(
                "import os,subprocess,time\nfrom pathlib import Path\n"
                "from local_voice.summary import worker_command\n"
                f"command=worker_command(Path({str(root)!r}),1)\n"
                f"command[-2]={str(worker)!r}\n"
                "p=subprocess.Popen(command,start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                f"marker=Path({str(root / 'guard')!r})\n"
                "temporary=marker.with_suffix('.partial')\n"
                "with temporary.open('x') as stream:\n"
                " stream.write(str(p.pid));stream.flush();os.fsync(stream.fileno())\n"
                "os.replace(temporary,marker)\n"
                "time.sleep(30)\n")
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            app = subprocess.Popen([sys.executable, str(owner)], env=env)
            pids, identities = [], {}
            def identity(pid):
                try:
                    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                    return fields[19], fields[0]
                except (FileNotFoundError, ProcessLookupError):
                    return None
            try:
                deadline = time.monotonic() + 5
                if pause_publication:
                    while not (root / 'pids.partial').exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue((root / 'pids.partial').exists(), 'Fixture did not reach partial publication')
                    self.assertFalse((root / 'pids').exists(), 'Incomplete metadata signalled readiness')
                    with self.assertRaises(json.JSONDecodeError):
                        json.loads((root / 'pids.partial').read_text())
                    (root / 'publish-allowed').touch()
                while (not (root / "pids").exists() or not (root / "guard").exists()) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((root / "pids").exists(), "Mandatory private namespace/memory scope failed")
                self.assertTrue((root / "guard").exists(), "Owner did not publish its guard identity")
                info = json.loads((root / "pids").read_text())
                guard = int((root / "guard").read_text())
                self.assertEqual((info["parent"], info["group"]), (guard, guard))
                self.assertIn("local-voice-summary-", info["cgroup"])
                self.assertEqual(int(info["memory"]), 3 * 1024 ** 3)
                self.assertEqual(int(info["swap"]), 128 * 1024 ** 2)
                pids = [guard, info["worker"], info["child"]]
                identities = {pid: identity(pid) for pid in pids}
                app.kill()
                app.wait(timeout=2)
                def active(pid):
                    current = identity(pid)
                    original = identities.get(pid)
                    return current is not None and original is not None and current[0] == original[0] and current[1] != "Z"
                deadline = time.monotonic() + 3
                while any(active(pid) for pid in pids) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(any(active(pid) for pid in pids))
            finally:
                if app.poll() is None:
                    app.kill()
                    app.wait()
                for pid in pids:
                    try:
                        current, original = identity(pid), identities.get(pid)
                        if current is not None and original is not None and current[0] == original[0]:
                            os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


class SummaryProcessTests(unittest.TestCase):
    def run_fixture(self, code, *, timeout=3, cancel_after=None):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            script = root / "fixture.py"
            script.write_text("import json,os,sys,time\nfrom pathlib import Path\njob=Path(sys.argv[1])\n" + code)
            def command(job, _threads):
                return summary.guarded_command([sys.executable, str(script), str(job)])
            client = summary.LocalSummarizer(summary.SummarySpec(timeout=timeout), root / "jobs", enabled=True)
            event = threading.Event()
            timer = None if cancel_after is None else threading.Timer(cancel_after, event.set)
            if timer:
                timer.start()
            try:
                with patch.object(summary, "verified_manifest", return_value={}), patch.object(summary, "worker_command", side_effect=command):
                    result = client.summarize([segment()], event)
                self.assertEqual(list((root / "jobs").iterdir()), [])
                return result, client.last_run
            finally:
                if timer:
                    timer.cancel()

    def test_successful_generated_child_writes_validated_private_result(self):
        result, metadata = self.run_fixture(
            "data=json.loads((job/'input.json').read_text())\n"
            "assert 'raw_text' not in json.dumps(data)\n"
            "assert (job/'input.json').stat().st_mode & 0o777 == 0o600\n"
            "(job/'result.json').write_text(json.dumps({'items':[{'text':'Ship Friday.','segment_ids':['s1']}]}))\n")
        self.assertEqual(result["items"][0]["text"], "Ship Friday.")
        self.assertTrue(metadata["cleanupComplete"])

    def test_failed_child_and_missing_artifact_are_errors(self):
        for code, category in (("raise SystemExit(7)\n", "process"), ("pass\n", "protocol")):
            with self.assertRaises(summary.SummaryError) as result:
                self.run_fixture(code)
            self.assertEqual(result.exception.category, category)

    def test_child_cannot_fabricate_references(self):
        with self.assertRaises(summary.SummaryError) as result:
            self.run_fixture("(job/'result.json').write_text(json.dumps({'items':[{'text':'Invented.','segment_ids':['no']}]}))\n")
        self.assertEqual(result.exception.category, "protocol")

    def test_timeout_and_cancel_stop_ignoring_child(self):
        for timeout, cancel, category in ((.2, None, "timeout"), (3, .2, "cancelled")):
            began = time.monotonic()
            with self.assertRaises(summary.SummaryError) as result:
                self.run_fixture("import signal\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(30)\n",
                                 timeout=timeout, cancel_after=cancel)
            self.assertEqual(result.exception.category, category)
            self.assertLess(time.monotonic() - began, 3)

    def test_excess_diagnostics_are_bounded_and_stop_the_child(self):
        with self.assertRaises(summary.SummaryError) as result:
            self.run_fixture("os.write(2,b'x'*70000)\ntime.sleep(30)\n")
        self.assertEqual(result.exception.category, "process")


class SummaryRunnerBoundaryTests(unittest.TestCase):
    def test_hybrid_cgroup_uses_unified_controller_path(self):
        text = "1:net_cls:/\n0::/user.slice/local-voice-summary-generated.scope\n"
        self.assertEqual(summary_worker.cgroup_directory(text),
                         Path("/sys/fs/cgroup/user.slice/local-voice-summary-generated.scope"))

    def test_runner_receives_bounded_schema_and_only_existing_ids(self):
        client = RunnerClient(1)
        def request(path, payload):
            if path == "/apply-template":
                self.assertNotIn("raw_text", json.dumps(payload))
                return {"prompt": "Generated fixture prompt"}
            if path == "/tokenize":
                return {"tokens": [1, 2, 3]}
            self.assertEqual(payload["n_predict"], 1024)
            self.assertFalse(payload["cache_prompt"])
            refs = payload["json_schema"]["properties"]["items"]["items"]["properties"]["segment_ids"]["items"]["enum"]
            self.assertEqual(refs, ["s1"])
            self.assertEqual(payload["json_schema"]["properties"]["items"]["items"]["properties"]["segment_ids"]["maxItems"], 3)
            return {"content": '{"items":[{"text":"Ship Friday.","segment_ids":["s1"]}]}', "truncated": False}
        with patch.object(client, "request", side_effect=request):
            self.assertEqual(client.generate(summary.normalize_segments([segment()]))["items"][0]["segment_ids"], ["s1"])

    def test_runner_truncation_never_becomes_a_valid_summary(self):
        client = RunnerClient(1)
        for result in ({"truncated": True}, {"stop_type": "limit"}):
            with patch.object(client, "request", side_effect=[{"prompt": "fixture"}, {"tokens": [1]}, result]):
                with self.assertRaises(summary.SummaryError) as error:
                    client.generate(summary.normalize_segments([segment()]))
                self.assertEqual(error.exception.category, "limit")


@unittest.skipUnless(os.environ.get("LOCAL_VOICE_QUALIFY_SUMMARY") == "1", "Explicit local model qualification only")
class RealSummaryQualification(unittest.TestCase):
    def test_generated_meeting_on_verified_local_model(self):
        texts = [
            "This is a generated software planning meeting, not a real conversation. The team agreed to release the offline notes feature on Friday.",
            "Alex will complete the export bug fix by Thursday. The defect duplicated the last paragraph in exported notes.",
            "Sam will review the export tests on Thursday after Alex finishes the fix. No customer data may be used in the test fixtures.",
            "The team decided to keep cloud sync disabled. All meeting transcripts and summaries must remain local to the device.",
            "The design review moves from Tuesday to Monday at ten. Jo will send the revised agenda before Monday.",
            "A proposed automatic email feature was rejected. Do not describe automatic email as an approved action. The release remains Friday.",
        ]
        records = [segment(f"generated-{i}", text) for i, text in enumerate(texts)]
        client = summary.LocalSummarizer(enabled=True)
        try:
            with qualification_model_lease():
                result = client.summarize(records, threading.Event())
        finally:
            print(json.dumps({"qualificationMetadata": client.last_run}))
        self.assertGreater(len(result["items"]), 0)
        self.assertTrue(all(item["segment_ids"] for item in result["items"]))
        joined = " ".join(item["text"].lower() for item in result["items"])
        self.assertIn("friday", joined)
        self.assertIn("thursday", joined)
        self.assertIn("monday", joined)
        print(json.dumps({"generatedFixtureResult": result, "metadata": client.last_run}, ensure_ascii=False))

    def test_generated_long_meeting_hierarchy_with_uuid_ids(self):
        topics = {
            0: "The team approved the local notes release for Friday. The release must remain fully offline.",
            8: "Alex owns the export defect fix, due Thursday. The defect duplicated the final paragraph.",
            16: "Sam will review the export tests after Alex finishes. Customer data is forbidden in test fixtures.",
            24: "The team decided that cloud sync must remain disabled. Transcripts and summaries remain on the local device.",
            32: "The design review moves to Monday at ten. Jo will deliver the revised agenda before Monday.",
            39: "The proposed automatic email feature was rejected. Do not treat automatic email as an approved action.",
        }
        records = [segment(str(uuid.uuid5(uuid.NAMESPACE_DNS, f"local-voice-generated-summary-{i}")),
                           topics.get(i, ("We reviewed interface labels and checked the agenda. "
                                          "No changes were approved during this part of the discussion. ") * 3))
                   for i in range(40)]
        client = summary.LocalSummarizer(enabled=True)
        try:
            with qualification_model_lease():
                result = client.summarize(records, threading.Event())
        finally:
            print(json.dumps({"hierarchyQualificationMetadata": client.last_run}))
        self.assertGreaterEqual(client.last_run["modelCalls"], 3)
        self.assertTrue(result["items"])
        text = " ".join(item["text"].lower() for item in result["items"])
        self.assertIn("friday", text)
        self.assertIn("thursday", text)
        self.assertIn("monday", text)
        checks = {0: ("friday", "offline"), 8: ("alex", "thursday"),
                  16: ("sam", "customer"), 24: ("cloud", "disabled"),
                  32: ("monday", "jo"), 39: ("email", "reject")}
        for index, markers in checks.items():
            self.assertTrue(any(all(marker in item["text"].lower() for marker in markers)
                                and records[index]["id"] in item["segment_ids"]
                                for item in result["items"]), (index, markers, result))
        print(json.dumps({"generatedHierarchyResult": result, "metadata": client.last_run}, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
