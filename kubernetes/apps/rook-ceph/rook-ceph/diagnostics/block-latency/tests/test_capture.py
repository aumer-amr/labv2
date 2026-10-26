#!/usr/bin/env python3
"""Supervisor tests; --kernel also executes the actual probe bodies on local fixtures."""

import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "quay.io/iovisor/bpftrace@sha256:ceeff3fb27b644dcec07f0da4fd725a1209a07a0b1a12079b99c80324a3568b2"
KERNEL = "--kernel" in sys.argv
if KERNEL:
    sys.argv.remove("--kernel")


def records(output):
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


class RenderTests(unittest.TestCase):
    def test_strict_render_preserves_scripts_and_workload_bounds(self):
        for directory, name, seconds, deadline in [
            (ROOT, "ceph-block-latency", "0", None),
            (ROOT.parent / "block-latency-validation", "ceph-block-latency-validation", "60", 120),
        ]:
            with self.subTest(directory=directory):
                raw = subprocess.check_output(["kustomize", "build", str(directory), "--load-restrictor=LoadRestrictionsNone"])
                escaped = subprocess.check_output(
                    ["yq", '(select(.kind == "ConfigMap").data[]) |= sub("\\\\$", "$$$$")'], input=raw,
                )
                rendered = subprocess.check_output(["flux", "envsubst", "--strict"], input=escaped)
                def documents(data):
                    output = subprocess.check_output(["yq", "-o=json", "-I=0"], input=data)
                    return [json.loads(line) for line in output.splitlines()]
                self.assertEqual(documents(raw), documents(rendered))
                resources = documents(rendered)
                self.assertEqual(sorted(x["kind"] for x in resources), ["ConfigMap", "Job"])
                self.assertTrue(all(x["metadata"]["namespace"] == "rook-ceph" for x in resources))
                config = next(x for x in resources if x["kind"] == "ConfigMap")
                self.assertEqual(config["data"]["capture.bt"], (ROOT / "capture.bt").read_text())
                self.assertEqual(config["data"]["run.sh"], (ROOT / "run.sh").read_text())
                job = next(x for x in resources if x["kind"] == "Job")
                self.assertEqual(job["metadata"]["name"], name)
                if deadline is None:
                    self.assertNotIn("activeDeadlineSeconds", job["spec"])
                else:
                    self.assertEqual(job["spec"]["activeDeadlineSeconds"], deadline)
                pod = job["spec"]["template"]["spec"]
                self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "epyon"})
                self.assertEqual(pod["containers"][0]["args"], ["/capture/run.sh", seconds])
                self.assertEqual(pod["volumes"][0]["configMap"]["name"], config["metadata"]["name"])


class SupervisorTests(unittest.TestCase):
    def run_capture(self, lines=(), stay=False, duration="60", early=False, armed=True):
        with tempfile.TemporaryDirectory() as directory:
            tracer = Path(directory) / "bpftrace"
            tracer.write_text(
                "#!/usr/bin/env python3\n"
                "import json, signal, sys, time\n"
                "def stop(*_):\n"
                " print(json.dumps({'event':'stop','deadline':int(not _)}, separators=(',', ':')), flush=True)\n"
                " print('@stats[1]: 2', flush=True)\n"
                " print('@stats[3]: 3', flush=True)\n"
                " sys.exit(0)\n"
                "signal.signal(signal.SIGINT, stop)\n"
                "print('{\"event\":\"start\"}', flush=True)\n"
                + ("print('{\"event\":\"armed\"}', flush=True)\n" if armed else "")
                + f"for line in {list(lines)!r}: print(line, flush=True)\n"
                + (
                    "while True: time.sleep(0.1)\n"
                    if stay
                    else ("stop(1)\n" if early else "stop()\n")
                )
            )
            tracer.chmod(0o755)
            env = dict(os.environ, PATH=f"{directory}:{os.environ['PATH']}")
            return subprocess.run(
                ["bash", str(ROOT / "run.sh"), duration, "1"],
                env=env, text=True, capture_output=True, timeout=15,
            )

    def test_pending_ages_and_coverage_without_pointer_leak(self):
        result = self.run_capture([
            '{"event":"heartbeat","monotonic_ns":10000000000}',
            "@logical_start[3735928559]: 9900000000",
            "@device_start[3735928559]: 9950000000",
            "@coverage[2, 1]: 5", "@operations[2, 2]: 4",
            '{"event":"snapshot_end"}',
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("3735928559", result.stdout)
        events = records(result.stdout)
        inflight = next(x for x in events if x["event"] == "inflight")
        self.assertEqual((inflight["logical_oldest_ms"], inflight["device_oldest_ms"]), (100, 50))
        self.assertIn({"event": "coverage", "op": 2, "reason": 1, "count": 5}, events)

    def test_trigger_stops_after_post_window(self):
        result = self.run_capture(['{"event":"logical","trigger":1}'], stay=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        events = records(result.stdout)
        self.assertTrue(any(x["event"] == "candidate" for x in events))
        self.assertEqual(events[-1]["reason"], "candidate_captured")

    def test_inflight_request_can_trigger_without_completion(self):
        result = self.run_capture([
            '{"event":"heartbeat","monotonic_ns":10000000000}',
            "@logical_start[123]: 1000000000", '{"event":"snapshot_end"}',
        ], stay=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(records(result.stdout)[-1]["reason"], "candidate_captured")

    def test_losses_and_unsupported_lifecycles_invalidate_capture(self):
        for line in ["Lost 2 events", "WARNING: map_update_elem failed", "@stats[8]: 1"]:
            with self.subTest(line=line):
                result = self.run_capture([line], stay=True)
                self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
                self.assertEqual(records(result.stdout)[-1]["invalid"], 1)

    def test_unknown_map_is_rejected_without_leaking_pointer(self):
        result = self.run_capture(["@unexpected[0xdeadbeef]: 42"], stay=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("deadbeef", result.stdout)
        self.assertEqual(records(result.stdout)[-1]["reason"], "unexpected_map_output")

    def test_output_storm_is_bounded_and_invalid(self):
        result = self.run_capture(['{"event":"device"}'] * 300, stay=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(records(result.stdout)[-1]["reason"], "output_rate")
        self.assertLess(len(result.stdout.splitlines()), 270)

    def test_duration_is_bounded(self):
        for duration in ["604801", "-1", "bad", "08", "999999999999999999999999"]:
            with self.subTest(duration=duration):
                self.assertEqual(self.run_capture(duration=duration).returncode, 1)

    def test_unlimited_capture_stops_only_after_candidate(self):
        result = self.run_capture(['{"event":"logical","trigger":1}'], stay=True, duration="0")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(records(result.stdout)[-1]["reason"], "candidate_captured")

    def test_unlimited_capture_rejects_unexpected_deadline(self):
        result = self.run_capture(duration="0")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(records(result.stdout)[-1]["reason"], "unexpected_exit")

    def test_output_budget_renews_daily_and_still_bounds_bursts(self):
        setup = (ROOT / "run.sh").read_text().split("# No pod API access")[0]
        result = subprocess.run(["bash", "-c", setup + '''
request_stop() { reason=$1; }
bytes=104857600
budget_started=$((SECONDS - 86400))
emit '{"event":"budget_renewed"}'
if ((invalid || bytes >= 104857600)); then exit 2; fi
bytes=104857600
emit '{"event":"over_budget"}'
[[ $invalid == 1 && $reason == output_budget ]]
'''], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(records(result.stdout), [{"event": "budget_renewed"}])

    def test_unexpected_clean_exit_is_invalid(self):
        result = self.run_capture(early=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(records(result.stdout)[-1]["reason"], "unexpected_exit")

    def test_unarmed_capture_is_invalid(self):
        result = self.run_capture(armed=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(records(result.stdout)[-1]["reason"], "attachment_not_ready")


@unittest.skipUnless(KERNEL, "pass --kernel for actual local eBPF lifecycle tests")
class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        markers = {
            "rawtracepoint:block_io_start": "logical_start",
            "rawtracepoint:block_rq_issue": "issue",
            "rawtracepoint:block_rq_complete": "complete",
            "rawtracepoint:block_rq_requeue": "requeue",
            "rawtracepoint:block_io_done": "logical_done",
            "rawtracepoint:block_rq_merge": "merge",
            "kprobe:__blk_mq_free_request": "release",
            "interval:s:60": "tick",
        }
        source = (
            "struct fixture_disk { unsigned long long major, first_minor; };\n"
            "struct fixture_queue { struct fixture_disk *disk; };\n"
            "struct fixture_request { struct fixture_queue *q; unsigned long long cmd_flags, __data_len, rq_flags; };\n"
        )
        for part in re.split(r"(?m)(?=^(?:BEGIN|END|rawtracepoint:|kprobe:|interval:))", (ROOT / "capture.bt").read_text()):
            header = part.partition("\n")[0]
            if header in markers:
                part = part.replace(header, f"uprobe:/tmp/lifecycle:{markers[header]}", 1)
                part = part.replace("(struct request *)arg0", "uptr((struct fixture_request *)arg0)")
                part = re.sub(r"\bnsecs\b", "((uint64)arg3)", part)
            source += part

        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "lifecycle"
            subprocess.run(["cc", "-static", "-O0", "-g", "-o", str(binary), str(ROOT / "tests/lifecycle.c")], check=True)
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w") as archive:
                archive.add(binary, arcname="lifecycle")
                archive.add("/sys/kernel/btf/vmlinux", arcname="vmlinux")
                data = source.encode()
                entry = tarfile.TarInfo("fixture.bt")
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
            command = [
                "docker", "run", "--rm", "-i", "--network", "none",
                "--cap-drop", "ALL", "--cap-add", "BPF", "--cap-add", "PERFMON",
                "--cap-add", "SYS_ADMIN",
                "--security-opt", "seccomp=unconfined", "--security-opt", "no-new-privileges",
                "--security-opt", "apparmor=unconfined",
                "--tmpfs", "/sys/kernel/btf:rw,mode=1777,size=16m",
                "--entrypoint", "/bin/sh", IMAGE, "-c",
                "set -e; tar --no-same-owner -xf - -C /tmp; "
                "mount -t tracefs tracefs /sys/kernel/tracing; "
                "cp /tmp/vmlinux /sys/kernel/btf/vmlinux; "
                "bpftrace -B none -k /tmp/fixture.bt 0 -c /tmp/lifecycle",
            ]
            result = subprocess.run(command, input=payload.getvalue(), capture_output=True, timeout=45)
        cls.output = result.stdout.decode() + result.stderr.decode()
        if result.returncode:
            raise AssertionError(cls.output)
        if "WARNING: arithmetic" in cls.output:
            raise AssertionError(cls.output)
        cls.events = records(result.stdout.decode())
        cls.logical = {x["id"] // 1000000: x for x in cls.events if x["event"] == "logical"}

    def test_normal_and_requeued_splits(self):
        for request, total, driver, outside in [(1000, 165, 140, 25), (2000, 230, 180, 50), (3000, 180, 160, 20)]:
            with self.subTest(request=request):
                event = self.logical[request]
                self.assertEqual((event["duration_ms"], event["driver_ms"], event["outside_driver_ms"], event["reason"]), (total, driver, outside, 0))
        self.assertEqual(self.logical[2000]["requeues"], 1)

    def test_flush_request_is_captured_without_logical_start(self):
        flushes = [x for x in self.events if x["event"] == "device" and x["op"] == 2]
        self.assertEqual(len(flushes), 2)
        self.assertTrue(all(x["duration_ms"] == 120 and x["valid"] == 1 for x in flushes))
        self.assertEqual(self.logical[4000]["reason"], 1)
        self.assertEqual(self.logical[4500]["duration_ms"], 250)
        self.assertEqual(self.logical[4500]["driver_ms"], -1)

    def test_missing_and_repeated_events_are_explicit(self):
        self.assertEqual(self.logical[7000]["reason"], 2)
        self.assertEqual(self.logical[8000]["reason"], 3)
        self.assertEqual(self.logical[9000]["reason"], 4)

    def test_repeated_issue_records_lifecycle_without_pointer(self):
        event = next(x for x in self.events if x["event"] == "lifecycle_error")
        self.assertEqual(event["reason"], "device_issue_repeat")
        self.assertEqual(event["id"], 8010000000)
        self.assertEqual(event["age_ns"], 10000000)
        self.assertEqual(event["logical_id"], 8000000000)
        self.assertEqual((event["previous_flags"], event["flags"]), (1, 1))
        self.assertEqual((event["previous_bytes"], event["bytes"]), (4096, 4096))
        self.assertEqual((event["attempts"], event["ends"], event["requeues"]), (1, 0, 0))
        self.assertNotIn("pointer", event)

    def test_overwritten_start_records_previous_request(self):
        event = next(x for x in self.events if x.get("reason") == "logical_start_overwrite")
        self.assertEqual(event["id"], 11000000000)
        self.assertEqual(event["age_ns"], 10000000)
        self.assertEqual((event["previous_flags"], event["flags"]), (1, 1))
        self.assertEqual((event["previous_bytes"], event["bytes"]), (4096, 4096))

    def test_filter_merge_free_and_slow_trigger(self):
        self.assertNotIn(500, self.logical)
        self.assertNotIn("@coverage[0, 9]", self.output)
        self.assertTrue(any(x["event"] == "armed" for x in self.events))
        self.assertNotIn(10000, self.logical)
        self.assertNotIn(5000, self.logical)
        self.assertNotIn(6000, self.logical)
        self.assertIn("@coverage[1, 7]: 1", self.output)
        self.assertIn("@coverage[1, 6]: 2", self.output)
        self.assertEqual(self.logical[30000]["trigger"], 1)
        self.assertEqual(self.logical[30000]["duration_ms"], 6020)


if __name__ == "__main__":
    unittest.main()
