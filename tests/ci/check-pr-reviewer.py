#!/usr/bin/env python3
"""Check the review workflow's trust boundary. Run with mise exec -- python3."""

import base64
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[2]


def read_yaml(path, expression="."):
    return json.loads(subprocess.check_output(
        ["yq", "-o=json", expression, str(ROOT / path)], text=True
    ))


workflow = read_yaml(".github/workflows/ai-pr-review.yaml")
assert set(workflow["on"]) == {"pull_request_target", "workflow_dispatch"}
assert workflow["on"]["pull_request_target"]["branches"] == ["main"]
dispatch = workflow["on"]["workflow_dispatch"]["inputs"]["pr_number"]
assert dispatch["required"] is True and dispatch["type"] == "string"
assert workflow["concurrency"]["group"] == "${{ github.workflow }}-${{ github.event.pull_request.number || github.run_id }}"
assert workflow["concurrency"]["cancel-in-progress"] is True
job = workflow["jobs"]["review"]
assert job["concurrency"] == {
    "group": "${{ github.workflow }}-review-${{ needs.build-runtime.outputs.pr_number }}",
    "cancel-in-progress": "${{ github.event_name == 'pull_request_target' }}",
}
assert job["if"] == ("needs.build-runtime.outputs.base_sha != '' && needs.build-runtime.outputs.head_sha != '' "
                     "&& needs.build-runtime.outputs.pr_number != ''")
assert job["permissions"]["contents"] == "read"
checkouts = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
assert len(checkouts) == 1
assert checkouts[0]["with"]["ref"] == "${{ needs.build-runtime.outputs.base_sha }}"
assert checkouts[0]["with"]["persist-credentials"] is False
build_job = workflow["jobs"]["build-runtime"]
assert build_job["permissions"] == {"contents": "read", "pull-requests": "read"}
assert build_job["if"] == (
    "${{ (github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main') || "
    "(!github.event.pull_request.draft && "
    "github.event.pull_request.head.repo.full_name == github.repository && "
    "github.event.pull_request.user.login == 'labv2-bot[bot]' && "
    "startsWith(github.event.pull_request.head.ref, 'renovate/')) }}"
)
assert build_job["outputs"] == {
    "base_sha": "${{ steps.dispatch.outputs.base_sha || github.event.pull_request.base.sha }}",
    "head_sha": "${{ steps.dispatch.outputs.head_sha || github.event.pull_request.head.sha }}",
    "pr_number": "${{ steps.dispatch.outputs.pr_number || github.event.pull_request.number }}",
}
assert job["needs"] == "build-runtime"
build_checkouts = [s for s in build_job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
assert len(build_checkouts) == 2
assert build_checkouts[0]["with"]["ref"] == build_job["outputs"]["base_sha"]
assert build_checkouts[0]["with"]["persist-credentials"] is False
assert build_checkouts[1]["with"] == {
    "repository": "misospace/pr-reviewer-action",
    "ref": "768a1320d67f14b99737633a5f8ba58449c3caeb",
    "path": ".github/actions/pr-reviewer-runtime",
    "persist-credentials": False,
}
assert job["steps"][-1]["uses"] == "./.github/actions/pr-reviewer-runtime"
assert job["steps"][-1]["env"] == {
    "LABV2_REVIEW_HEAD_SHA": "${{ github.event_name == 'workflow_dispatch' && needs.build-runtime.outputs.head_sha || '' }}",
    "LABV2_REVIEW_BASE_SHA": "${{ github.event_name == 'workflow_dispatch' && needs.build-runtime.outputs.base_sha || '' }}",
}
review = job["steps"][-1]["with"]
assert review["pr-number"] == "${{ needs.build-runtime.outputs.pr_number }}"
assert review["force-review"] == "${{ github.event_name == 'workflow_dispatch' && 'true' || 'false' }}"
assert review["publish-mode"] == "comment"
assert review["verdict-policy"] == "model"
assert review["deep-review"] == "false"
assert review["inline-findings"] == "false"
assert review["on-model-failure"] == "fail"
assert job["timeout-minutes"] == 60
assert review["ai-model"] == "qwen3.5-9b-fallback"
assert review["ai-tokens-param"] == "max_tokens"
assert review["ai-max-tokens"] == "4096"
assert review["model-context-tokens"] == "32768"
assert review["ai-request-timeout-sec"] == "1800"
assert review["ai-stream"] == "false"
assert review["tool-max-tokens-per-turn"] == "1024"
assert review["tool-turn-timeout-sec"] == "600"
assert "tool-max-requests" not in review, "Use the pinned runtime's PR-size request budget"
assert "tool-max-rounds" not in review, "Let rounds scale with the request budget"
assert review["tool-failure-enforcement"] == "true"
assert review["tool-min-successful-requests"] == "1"
assert review["skip-if-diff-unchanged"] == "false"
assert review["allow-approve"] == "false"
assert review["tool-enable-for-forks"] == "false"
assert review["evidence-providers-file"] == ".github/pr-review-providers.json"
assert review["evidence-enable-for-forks"] == "false"
providers = json.loads((ROOT / review["evidence-providers-file"]).read_text())
assert providers["providers"][0]["command"] == ["python3", "scripts/ci/pr-review-oci.py"]
assert review["ai-base-url"].startswith("http://litellm.ai.svc.cluster.local:")
assert "http://konflate.flux-system.svc.cluster.local:" in review["tool-mcp-servers"]

# Run the actual manual gate with a fake read-only GitHub response.
manual = build_job["steps"][0]
assert manual["id"] == "dispatch" and manual["if"] == "github.event_name == 'workflow_dispatch'"
assert manual["env"]["REVIEW_REQUESTER"] == "${{ github.triggering_actor }}"
repo = "owner/repo"
base, head = "b" * 40, "a" * 40
eligible_pr = {
    "state": "open", "draft": False, "user": {"login": "labv2-bot[bot]"},
    "base": {"ref": "main", "sha": base, "repo": {"full_name": repo}},
    "head": {"ref": "renovate/chart", "sha": head, "repo": {"full_name": repo}},
}
with tempfile.TemporaryDirectory() as directory:
    directory = Path(directory)
    fake_gh = directory / "gh"
    fake_gh.write_text(
        "#!/bin/bash\ncase \"$2\" in\n"
        "  repos/owner/repo/collaborators/operator/permission) printf '%s' \"$ROLE_JSON\" ;;\n"
        "  repos/owner/repo/pulls/123) printf '%s' \"$PR_JSON\" ;;\n"
        "  *) exit 1 ;;\nesac\n"
    )
    fake_gh.chmod(0o700)
    cases = [("maintain", "123", None, True), ("admin", "123", None, True),
             ("write", "123", None, False), ("maintain", "123; whoami", None, False)]
    cases.extend(("maintain", "123", (keys, value), False) for keys, value in [
        (("state",), "closed"), (("draft",), True), (("base", "ref"), "release"),
        (("base", "repo", "full_name"), "other/repo"), (("head", "repo", "full_name"), "fork/repo"),
        (("head", "ref"), "feature/chart"), (("head", "sha"), "invalid"), (("base", "sha"), None),
        (("user", "login"), "someone-else"),
    ])
    for role, number, mutation, allowed in cases:
        pr = deepcopy(eligible_pr)
        if mutation:
            keys, value = mutation
            obj = pr
            for key in keys[:-1]:
                obj = obj[key]
            obj[keys[-1]] = value
        output = directory / "output"
        output.unlink(missing_ok=True)
        result = subprocess.run(["bash", "-c", manual["run"]], capture_output=True, env={
            **os.environ, "PATH": f"{directory}:{os.environ['PATH']}", "REVIEW_REQUESTER": "operator",
            "GITHUB_REPOSITORY": repo, "PR_NUMBER": number, "GITHUB_OUTPUT": str(output),
            "PR_JSON": json.dumps(pr), "ROLE_JSON": json.dumps({"role_name": role}),
        })
        assert (result.returncode == 0) == allowed, (role, number, mutation, result.stderr)
        if allowed:
            assert output.read_text() == f"base_sha={base}\nhead_sha={head}\npr_number=123\n"
        else:
            assert not output.exists()

# Mise's signed release verification needs Sigstore trust metadata on cold runners.
policy = read_yaml("kubernetes/apps/actions-runners/pr-reviewer/app/networkpolicy.yaml",
                   'select(.kind == "CiliumNetworkPolicy")')
assert policy["spec"]["endpointSelector"]["matchLabels"] == {
    "app.kubernetes.io/name": job["runs-on"],
    "app.kubernetes.io/instance": job["runs-on"],
    "app.kubernetes.io/component": "runner",
}
sigstore = {"matchName": "tuf-repo-cdn.sigstore.dev"}
egress = policy["spec"]["egress"]
for rule, protocol in zip(egress[:2], ["UDP", "TCP"], strict=True):
    assert rule["toPorts"][0]["ports"] == [{"port": "53", "protocol": protocol}]
    assert sigstore in rule["toPorts"][0]["rules"]["dns"]
assert sigstore in egress[2]["toFQDNs"]
assert egress[2]["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]

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

module_spec = importlib.util.spec_from_file_location("oci", ROOT / "scripts/ci/pr-review-oci.py")
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

def chart_fixture(values):
    package = io.BytesIO()
    with tarfile.open(fileobj=package, mode="w:gz") as archive:
        entry = tarfile.TarInfo("chart/values.yaml")
        entry.size = len(values)
        archive.addfile(entry, io.BytesIO(values))
    body = package.getvalue()
    layer = "sha256:" + hashlib.sha256(body).hexdigest()
    manifest = json.dumps({
        "schemaVersion": 2, "config": {"digest": "unused"},
        "layers": [{"mediaType": oci.CHART_LAYER, "size": len(body), "digest": layer}],
    }).encode()
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    return digest, manifest, layer, body


def github_content(raw):
    return json.dumps({"content": base64.b64encode(raw).decode()})


chart_yaml = b"""kind: OCIRepository
metadata:
  name: chart
spec:
  url: oci://ghcr.io/org/chart
  ref:
    tag: TAG
"""
release_path = "kubernetes/apps/test/app/helmrelease.yaml"
release_yaml = b"""kind: HelmRelease
metadata:
  name: workload
spec:
  chartRef:
    kind: OCIRepository
    name: chart
  values:
    nested:
      oldKey: example-secret-value
      changedType: true
      stillPresent: 2
      customKey: true
    unchanged: [a]
  valuesFrom:
    - kind: Secret
      name: do-not-read
"""
chart_repo = "https://ghcr.io/v2/org/chart"
registry = {}
for tag, defaults in [
    ("1.0.0", b"nested: {oldKey: value, changedType: true, stillPresent: 1}\nunchanged: [old]\nunused: false\n"),
    ("2.0.0", b"nested: {changedType: string, stillPresent: 3}\nunchanged: [new]\n"),
]:
    digest, manifest, layer, body = chart_fixture(defaults)
    registry[f"{chart_repo}/manifests/{tag}"] = manifest
    registry[f"{chart_repo}/manifests/{digest}"] = manifest
    registry[f"{chart_repo}/blobs/{layer}"] = body

event["pull_request"]["base"] = {"sha": base}
api = {
    "repos/owner/repo/pulls/1/files?per_page=100": json.dumps([{"filename": path, "status": "modified"}]),
    f"repos/owner/repo/contents/{path}?ref={head}": github_content(chart_yaml.replace(b"TAG", b"2.0.0")),
    f"repos/owner/repo/contents/{path}?ref={base}": github_content(chart_yaml.replace(b"TAG", b"1.0.0")),
    f"repos/owner/repo/contents/{release_path}?ref={head}": github_content(release_yaml),
}


def fetch_chart(url, headers=None, *, limit=oci.JSON_LIMIT):
    if url.startswith("https://ghcr.io/token?"):
        return b'{"token":"dummy"}'
    assert headers["Authorization"] == "Bearer dummy"
    assert limit == (oci.CHART_LIMIT if "/blobs/" in url else oci.JSON_LIMIT)
    return registry[url]


with patch.object(oci, "gh", side_effect=api.__getitem__), patch.object(oci, "fetch", side_effect=fetch_chart):
    result = oci.collect(event)
assert result["chart_upgrades"] == [{
    "file": path, "status": "compared", "old_tag": "1.0.0", "new_tag": "2.0.0",
    "release_file": release_path, "changes": [
        {"release": "workload", "key": ("nested", "oldKey"), "change": "removed"},
        {"release": "workload", "key": ("nested", "changedType"), "change": "type_changed",
         "old_type": "bool", "new_type": "str"},
    ], "omitted_changes": 0,
}]
assert "example-secret-value" not in json.dumps(result)

# A failed digest check leaves publication evidence intact and chart evidence unknown.
registry[f"{chart_repo}/blobs/{layer}"] = b"tampered"
with patch.object(oci, "gh", side_effect=api.__getitem__), patch.object(oci, "fetch", side_effect=fetch_chart):
    result = oci.collect(event)
assert result["publication"][0]["status"] == "published"
assert result["chart_upgrades"][0]["status"] == "unknown"
assert "changes" not in result["chart_upgrades"][0]

print("PR review trust-boundary, OCI publication, and chart evidence checks passed")
subprocess.run([sys.executable, str(ROOT / "tests/ci/test-pr-review-images.py")], check=True)
