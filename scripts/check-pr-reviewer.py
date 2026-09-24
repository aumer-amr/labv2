#!/usr/bin/env python3
"""Check the review workflow's trust boundary. Run with mise exec -- python3."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

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
assert review["publish_mode"] == "comment"
assert review["allow_approve"] == "false"
assert review["tool_enable_for_forks"] == "false"
assert "evidence_providers_file" not in review
assert review["ai_base_url"].startswith("http://litellm.ai.svc.cluster.local:")
assert "http://konflate.flux-system.svc.cluster.local:" in review["tool_mcp_servers"]

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

print("PR review trust-boundary checks passed")
