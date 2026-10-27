"""Local regression tests; never contact Kubernetes or the Minecraft server."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest


DIRECTORY = Path(__file__).parent
RUN = "upgrade-uid:4"
ANNOTATION = "maintenance.home.arpa/tuppr-"
BASE = {
    "uid": "deployment-uid", "version": "1", "replicas": 1,
    "annotations": {}, "pods": True, "ready": True, "restarts": 0,
    "calls": [],
}

KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
path = Path(os.environ["FAKE_CLUSTER"])
state = json.loads(path.read_text())
args = sys.argv[1:]
assert args[0] == "--namespace" and args[1] in ("home", "system-upgrade")
assert args[3] == "--cache-dir=/tmp/kube-cache"
args = args[4:]
state["calls"].append(args[:3])
def finish(output="", code=0):
    path.write_text(json.dumps(state))
    print(output, end="")
    sys.exit(code)
annotation = "maintenance.home.arpa/tuppr-"
if args[:3] == ["get", "talosupgrade", "talos"]:
    assert "status.startedAt" not in args[-1], "completed upgrades can have no startedAt"
    finish("upgrade-uid:4")
if args[:3] == ["get", "deployment", "minecraft"]:
    finish("|".join(str(x) for x in [state["uid"], state["version"], state["replicas"],
        state["annotations"].get(annotation + "run", ""),
        state["annotations"].get(annotation + "replicas", "")]))
if args[:2] == ["get", "pods"]:
    if not state["pods"]:
        finish()
    if "range" not in args[-1]:
        finish("pod-uid")
    pod = "minecraft-current|pod-uid||" + str(state["ready"]).lower() + "|0"
    finish(pod + ("\nminecraft-other|other-uid||true|0" if state.get("extra_pod") else ""))
if args[:2] == ["get", "pod"]:
    if state.get("pod_moved"):
        finish(code=1)
    finish("pod-uid||true|" + str(state["restarts"]))
if args[0] == "exec":
    assert args[1] == "minecraft-current" and args[2:7] == ["--container", "app", "--", "timeout", "90"]
    if state.get("save_failure"):
        finish(code=1)
    if state.get("restart_during_save"):
        state["restarts"] += 1
    if state.get("relocate_during_save"):
        state["pod_moved"] = True
    finish("MINECRAFT_SAVE_CONFIRMED\n")
if args[:3] == ["patch", "deployment", "minecraft"]:
    patch = json.loads(args[-1])
    assert patch["metadata"]["resourceVersion"] == state["version"]
    if state.get("conflict"):
        finish(code=1)
    for key, value in patch["metadata"].get("annotations", {}).items():
        if value is None:
            state["annotations"].pop(key, None)
        else:
            state["annotations"][key] = value
    if "spec" in patch:
        state["replicas"] = patch["spec"]["replicas"]
        state["pods"] = bool(state["replicas"])
    state["version"] = str(int(state["version"]) + 1)
    finish()
if args[:3] == ["rollout", "status", "deployment/minecraft"]:
    finish(code=int(state.get("restore_failure", False)))
raise AssertionError(args)
'''


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cluster = self.root / "cluster.json"
        self.cluster.write_text(json.dumps(BASE))
        for name, script in {"kubectl": KUBECTL, "sleep": "#!/bin/sh\nexit 0\n"}.items():
            executable = self.root / name
            executable.write_text(script)
            executable.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{self.root}:{os.environ['PATH']}",
                    "FAKE_CLUSTER": str(self.cluster)}

    def state(self, **changes):
        state = json.loads(self.cluster.read_text())
        state.update(changes)
        self.cluster.write_text(json.dumps(state))
        return state

    def run_hook(self, mode, success=True):
        result = subprocess.run(["sh", str(DIRECTORY / "maintenance.sh"), mode],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return self.state()

    def test_save_stop_restore(self):
        state = self.run_hook("pre")
        self.assertEqual(state["replicas"], 0)
        self.assertEqual(state["annotations"][ANNOTATION + "replicas"], "1")
        self.assertEqual(state["annotations"][ANNOTATION + "run"], RUN)
        state = self.run_hook("post")
        self.assertEqual(state["replicas"], 1)
        self.assertEqual(state["annotations"], {})
        self.assertTrue(any(call[0] == "rollout" for call in state["calls"]))

    def test_no_save_or_stop_when_not_ready(self):
        self.state(ready=False)
        state = self.run_hook("pre", False)
        self.assertFalse(any(call[0] in ("exec", "patch") for call in state["calls"]))

    def test_missing_pod_blocks_stop(self):
        self.state(pods=False)
        state = self.run_hook("pre", False)
        self.assertEqual(state["replicas"], 1)
        self.assertFalse(any(call[0] == "patch" for call in state["calls"]))

    def test_save_failure_leaves_server_running(self):
        self.state(save_failure=True)
        state = self.run_hook("pre", False)
        self.assertEqual(state["replicas"], 1)
        self.assertEqual(state["annotations"], {})
        self.run_hook("post")

    def test_pod_restart_during_save_blocks_stop(self):
        self.state(restart_during_save=True)
        state = self.run_hook("pre", False)
        self.assertEqual(state["replicas"], 1)
        self.assertFalse(any(call[0] == "patch" for call in state["calls"]))

    def test_extra_pod_blocks_save(self):
        self.state(extra_pod=True)
        state = self.run_hook("pre", False)
        self.assertFalse(any(call[0] == "exec" for call in state["calls"]))

    def test_relocation_during_save_blocks_stop(self):
        self.state(relocate_during_save=True)
        state = self.run_hook("pre", False)
        self.assertEqual(state["replicas"], 1)
        self.assertFalse(any(call[0] == "patch" for call in state["calls"]))

    def test_concurrent_deployment_change_blocks_stop(self):
        self.state(conflict=True)
        state = self.run_hook("pre", False)
        self.assertEqual(state["replicas"], 1)

    def test_stopped_server_stays_stopped(self):
        self.state(replicas=0, pods=False)
        self.run_hook("pre")
        state = self.run_hook("post")
        self.assertEqual(state["replicas"], 0)
        self.assertFalse(any(call[0] == "exec" for call in state["calls"]))

    def test_interrupted_pre_is_idempotent(self):
        self.run_hook("pre")
        state = self.run_hook("pre")
        self.assertEqual(sum(call[0] == "exec" for call in state["calls"]), 1)

    def test_restoration_failure_retains_recovery_state(self):
        self.run_hook("pre")
        self.state(restore_failure=True)
        state = self.run_hook("post", False)
        self.assertEqual(state["annotations"][ANNOTATION + "replicas"], "1")
        self.state(restore_failure=False)
        state = self.run_hook("post")
        self.assertEqual(state["annotations"], {})

    def test_stale_run_cannot_change_server(self):
        self.state(annotations={ANNOTATION + "run": "old-run", ANNOTATION + "replicas": "1"})
        for mode in ("pre", "post"):
            state = self.run_hook(mode, False)
            self.assertFalse(any(call[0] == "patch" for call in state["calls"]))

    def test_restart_during_maintenance_blocks_pre_retry(self):
        self.run_hook("pre")
        self.state(replicas=1, pods=True)
        self.run_hook("pre", False)

    def test_incomplete_recovery_state_blocks_changes(self):
        self.state(annotations={ANNOTATION + "run": RUN})
        for mode in ("pre", "post"):
            state = self.run_hook(mode, False)
            self.assertFalse(any(call[0] == "patch" for call in state["calls"]))


class SaveTests(unittest.TestCase):
    def save(self, completion=None, multiple=False, pipe=True, log_change=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            process = root / "proc/42"
            (process / "fd").mkdir(parents=True)
            (process / "comm").write_text("java\n")
            log = root / "latest.log"
            log.write_text("All dimensions are saved\nSaved the game\n")
            stdin = process / "fd/0"
            if pipe:
                os.mkfifo(stdin)
            else:
                stdin.write_text("")
            if multiple:
                (root / "proc/43").mkdir()
                (root / "proc/43/comm").write_text("java\n")
            command = []
            if pipe and not multiple:
                def server():
                    with stdin.open() as stream:
                        command.append(stream.readline())
                    if log_change == "rotate":
                        log.rename(root / "old.log")
                        log.write_text("")
                    elif log_change == "truncate":
                        log.write_text("")
                    if completion:
                        with log.open("a") as stream:
                            stream.write(completion)
                thread = threading.Thread(target=server, daemon=True)
                thread.start()
            script = (DIRECTORY / "save.sh").read_text().replace("/proc/", f"{root}/proc/").replace(
                "/data/logs/latest.log", str(log)).replace("sleep 1", "sleep 0.01")
            result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=5)
            if command:
                self.assertEqual(command, ["save-all flush\n"])
            return result

    def test_fresh_ordered_completion(self):
        result = self.save("All dimensions are saved\nSaved the game\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "MINECRAFT_SAVE_CONFIRMED")

    def test_historical_completion_is_rejected(self):
        self.assertNotEqual(self.save().returncode, 0)

    def test_partial_or_reversed_completion_is_rejected(self):
        for completion in ("All dimensions are saved\n", "Saved the game\n",
                           "Saved the game\nAll dimensions are saved\n"):
            with self.subTest(completion=completion):
                self.assertNotEqual(self.save(completion).returncode, 0)

    def test_multiple_java_processes_are_rejected(self):
        self.assertNotEqual(self.save(multiple=True).returncode, 0)

    def test_non_pipe_stdin_is_rejected(self):
        self.assertNotEqual(self.save(pipe=False).returncode, 0)

    def test_rotated_or_truncated_logs_are_rejected(self):
        for change in ("rotate", "truncate"):
            with self.subTest(change=change):
                self.assertNotEqual(self.save(log_change=change).returncode, 0)


class WiringTests(unittest.TestCase):
    @staticmethod
    def build(directory):
        built = subprocess.check_output(["kustomize", "build", str(directory),
                                         "--load-restrictor=LoadRestrictionsNone"], text=True)
        documents = subprocess.check_output(["yq", "ea", "-o=json", "-I=0", ".", "-"],
                                             input=built, text=True)
        return [json.loads(line) for line in documents.splitlines() if line]

    def test_hooks_mount_current_scripts_and_keep_health_checks(self):
        app = self.build(DIRECTORY.parent / "app")
        config = next(doc for doc in app if doc["kind"] == "ConfigMap")
        self.assertEqual(config["metadata"]["name"], "tuppr-minecraft-maintenance")
        self.assertEqual(config["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/substitute"], "disabled")
        for name in ("maintenance.sh", "save.sh"):
            self.assertEqual(config["data"][name], (DIRECTORY / name).read_text())
        upgrade = next(doc for doc in self.build(DIRECTORY.parent / "upgrades")
                       if doc["kind"] == "TalosUpgrade")
        for phase, deadline in (("pre", 600), ("post", 2400)):
            hook, = upgrade["spec"]["hooks"][phase]
            self.assertEqual(hook["args"], [phase])
            self.assertEqual(hook["command"], ["sh", "/maintenance/maintenance.sh"])
            self.assertEqual(hook["serviceAccountName"], config["metadata"]["name"])
            self.assertEqual(hook["activeDeadlineSeconds"], deadline)
            self.assertEqual(hook["backoffLimit"], 0)
            self.assertIn("@sha256:", hook["image"])
            self.assertEqual(hook["volumes"][0]["configMap"]["name"], config["metadata"]["name"])
            self.assertEqual(hook["volumeMounts"][0]["mountPath"], "/maintenance")
        checks = upgrade["spec"]["healthChecks"]
        self.assertEqual([check["kind"] for check in checks[:3]], ["Snapshot", "Restore", "CephCluster"])
        self.assertFalse(any(check.get("skipAfterPreHooks") for check in checks))
        self.assertEqual(checks[-1]["namespace"], "home")
        self.assertIn("object.spec.replicas == 0", checks[-1]["expr"])

    def test_rbac_and_dependency_namespaces(self):
        home = DIRECTORY.parents[2] / "home/minecraft/app"
        documents = self.build(home)
        role = next(doc for doc in documents if doc["kind"] == "Role")
        deployment_rule = next(rule for rule in role["rules"] if rule["resources"] == ["deployments"])
        self.assertEqual(deployment_rule["resourceNames"], ["minecraft"])
        self.assertFalse(any("secrets" in rule["resources"] for rule in role["rules"]))
        binding = next(doc for doc in documents if doc["kind"] == "RoleBinding")
        self.assertEqual(binding["subjects"][0]["namespace"], "system-upgrade")
        app = self.build(DIRECTORY.parent / "app")
        cluster_role = next(doc for doc in app if doc["kind"] == "ClusterRole")
        self.assertEqual(cluster_role["rules"], [{"apiGroups": ["tuppr.home-operations.com"],
            "resources": ["talosupgrades"], "resourceNames": ["talos"], "verbs": ["get"]}])
        namespace = self.build(DIRECTORY.parents[1])
        child = next(doc for doc in namespace if doc["metadata"]["name"] == "tuppr-upgrades")
        self.assertIn({"name": "minecraft", "namespace": "home"}, child["spec"]["dependsOn"])
        self.assertIn({"name": "network-policies"}, child["spec"]["dependsOn"])


if __name__ == "__main__":
    unittest.main()
