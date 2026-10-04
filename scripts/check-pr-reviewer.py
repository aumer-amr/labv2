#!/usr/bin/env python3
"""Check the review workflow's trust boundary. Run with mise exec -- python3."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]


def read_yaml(path):
    return json.loads(subprocess.check_output(
        ["yq", "-o=json", ".", str(ROOT / path)], text=True
    ))


workflow = read_yaml(".github/workflows/ai-pr-review.yaml")
assert set(workflow["on"]) == {"pull_request_target"}
job = workflow["jobs"]["review"]
assert "head.repo.full_name == github.repository" in job["if"]
assert job["permissions"]["contents"] == "read"
checkouts = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
assert len(checkouts) == 1
assert checkouts[0]["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
assert checkouts[0]["with"]["persist-credentials"] is False
review = job["steps"][-1]["with"]
assert review["publish-mode"] == "comment"
assert review["verdict-policy"] == "model"
assert review["deep-review"] == "false"
assert review["inline-findings"] == "false"
assert review["on-model-failure"] == "fail"
assert review["tool-max-tokens-per-turn"] == "400"
assert review["tool-turn-timeout-sec"] == "60"
assert review["skip-if-diff-unchanged"] == "false"
assert review["allow-approve"] == "false"
assert review["tool-enable-for-forks"] == "false"
assert review["evidence-providers-file"] == ".github/pr-review-providers.json"
assert review["evidence-enable-for-forks"] == "false"
providers = json.loads((ROOT / review["evidence-providers-file"]).read_text())
assert providers["providers"][0]["command"] == ["python3", "scripts/pr-review-oci.py"]
assert review["ai-base-url"].startswith("http://litellm.ai.svc.cluster.local:")
assert "http://konflate.flux-system.svc.cluster.local:" in review["tool-mcp-servers"]

release = read_yaml("kubernetes/apps/actions-runners/pr-reviewer/app/helmrelease.yaml")
values = release["spec"]["values"]
assert values["runnerScaleSetName"] == job["runs-on"]
assert values["minRunners"] == 0 and values["maxRunners"] == 1
pod = values["template"]["spec"]
assert pod["automountServiceAccountToken"] is False
assert pod["runtimeClassName"] == "gvisor"
assert "containerMode" not in values
runner = pod["containers"][0]
assert runner["securityContext"]["allowPrivilegeEscalation"] is False
assert {e["valueFrom"]["secretKeyRef"]["name"] for e in runner["env"]} == {"pr-reviewer-litellm"}

# Exercise the actual credential transport with dummy values, never real secrets.
credential_step = next(s for s in job["steps"] if s.get("id") == "model-key")
for value, succeeds in [("test-key", True), ("", False), ("x\ninjected=y", False), ("x\ry", False)]:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "output"
        result = subprocess.run(
            ["bash", "-c", credential_step["run"]], capture_output=True,
            env={**os.environ, "LITELLM_API_KEY": value, "GITHUB_OUTPUT": str(output)},
        )
        assert (result.returncode == 0) == succeeds
        if succeeds:
            assert output.read_text() == "key=test-key\n"
        else:
            assert not output.exists()

module_spec = importlib.util.spec_from_file_location("oci", ROOT / "scripts/pr-review-oci.py")
oci = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(oci)
spec = {"url": "oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack", "ref": {"tag": "91.7.1"}}
manifest = b'{"schemaVersion":2,"config":{"digest":"sha256:example"}}'
with patch.object(oci, "fetch", side_effect=[b'{"token":"dummy"}', manifest]) as fetch:
    result = oci.verify(spec)
    assert result["status"] == "published" and result["digest"].startswith("sha256:")
    assert fetch.call_args.args[0].endswith("/manifests/91.7.1")
    assert "dummy" not in json.dumps(result)
for code, status in [(404, "not_found"), (401, "unknown"), (403, "unknown"), (429, "unknown"), (503, "unknown")]:
    with patch.object(oci, "fetch", side_effect=[b'{"token":"dummy"}', HTTPError("", code, "", {}, None)]):
        assert oci.verify(spec)["status"] == status
for failure in [HTTPError("", 404, "", {}, None), URLError("private error detail")]:
    with patch.object(oci, "fetch", side_effect=failure):
        result = oci.verify(spec)
        assert result["status"] == "unknown" and "private error detail" not in json.dumps(result)
for url in ["http://127.0.0.1", "oci://ghcr.io.evil.test/a/b", "oci://ghcr.io/a/../b", "oci://user:password@ghcr.io/a/b"]:
    with patch.object(oci, "fetch") as fetch:
        assert oci.verify({**spec, "url": url})["status"] == "unknown"
        fetch.assert_not_called()
assert oci.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test") is None

# Exercise YAML parsing and exact-head API reads while mocking only external I/O.
head = "a" * 40
event = {"repository": {"full_name": "owner/repo"}, "pull_request": {
    "number": 1, "head": {"sha": head, "repo": {"full_name": "owner/repo"}},
}}
path = "kubernetes/apps/test/app/ocirepository.yaml"
raw = b"kind: OCIRepository\nspec:\n  url: oci://ghcr.io/org/chart\n  ref:\n    tag: 1.2.3\n"
with patch.object(oci, "gh", side_effect=[json.dumps([{"filename": path, "status": "modified"}]),
        json.dumps({"content": base64.b64encode(raw).decode()})]) as gh, patch.object(oci, "verify", return_value={"status": "published"}) as verify:
    result = oci.collect(event)
    assert result["head_sha"] == head and result["publication"][0]["status"] == "published"
    assert gh.call_args.args[0].endswith("?ref=" + head)
    assert verify.call_args.args[0]["ref"]["tag"] == "1.2.3"

print("PR review trust-boundary and OCI publication checks passed")
