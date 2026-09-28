#!/usr/bin/env python3
"""Read exact-head OCIRepository data; verify public GHCR tags without executing PR code."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib import error, parse, request


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an anonymous registry token to another host.


def fetch(url, headers=None):
    with request.build_opener(NoRedirect).open(
        request.Request(url, headers=headers or {}), timeout=10
    ) as response:
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("response too large")
        return body


def verify(spec):
    if not isinstance(spec, dict) or not isinstance(spec.get("ref", {}), dict):
        return {"status": "unknown", "reason": "invalid OCIRepository spec"}
    repository = spec.get("url", "")
    ref = spec.get("ref") or {}
    tag = ref.get("tag", "")
    # Fixed HTTPS host and constrained paths prevent PR-controlled SSRF/credential forwarding.
    if not isinstance(repository, str) or not re.fullmatch(
        r"oci://ghcr\.io/[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)+", repository
    ):
        return {"status": "unknown", "reason": "unsupported registry or repository URL"}
    if not isinstance(tag, str) or not re.fullmatch(r"[\w][\w.-]{0,127}", tag, re.ASCII) or ref.get("digest"):
        return {"status": "unknown", "reason": "requires an exact tag without a digest override"}
    repo = repository.removeprefix("oci://ghcr.io/")
    result = {"repository": repository, "tag": tag, "status": "unknown"}
    manifest_request = False
    try:
        token_url = "https://ghcr.io/token?" + parse.urlencode({"service": "ghcr.io", "scope": f"repository:{repo}:pull"})
        token = json.loads(fetch(token_url))["token"]
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise ValueError("invalid token")
        manifest_request = True
        body = fetch(f"https://ghcr.io/v2/{repo}/manifests/{tag}", {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json",
        })
        manifest = json.loads(body)
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2 or not (manifest.get("config") or manifest.get("manifests")):
            raise ValueError("invalid manifest")
        result.update(status="published", digest="sha256:" + hashlib.sha256(body).hexdigest())
    except error.HTTPError as exc:
        result.update(status="not_found" if manifest_request and exc.code == 404 else "unknown", reason=f"registry HTTP {exc.code}")
    except (OSError, ValueError, KeyError, TypeError):
        result["reason"] = "registry verification unavailable"
    return result


def gh(endpoint):
    return subprocess.check_output(["gh", "api", endpoint], text=True, timeout=20, stderr=subprocess.DEVNULL)


def collect(event):
    pr = event["pull_request"]
    repo = event["repository"]["full_name"]
    head = pr["head"]["sha"]
    if pr["head"]["repo"]["full_name"] != repo:
        return {"head_sha": head, "status": "unknown", "reason": "fork evidence disabled"}
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("invalid head SHA")
    results = []
    # Bound API work; unvisited files are explicitly reported, never silently certified.
    files = json.loads(gh(f"repos/{repo}/pulls/{int(pr['number'])}/files?per_page=100"))
    for changed in files:
        path = changed["filename"]
        if changed["status"] == "removed" or not path.startswith("kubernetes/") or not path.endswith("ocirepository.yaml"):
            continue
        if len(results) >= 5:
            results.append({"status": "unknown", "reason": "OCI file budget exceeded"})
            break
        content = json.loads(gh(f"repos/{repo}/contents/{parse.quote(path, safe='/')}?ref={head}"))
        raw = base64.b64decode(content["content"], validate=False)
        if len(raw) > 1024 * 1024:
            results.append({"file": path, "status": "unknown", "reason": "manifest file too large"})
            continue
        parsed = subprocess.check_output(["yq", "eval-all", "-o=json", "[.]", "-"], input=raw, timeout=10, stderr=subprocess.DEVNULL)
        for doc in json.loads(parsed):
            if isinstance(doc, dict) and doc.get("kind") == "OCIRepository":
                results.append({"file": path, **verify(doc.get("spec") or {})})
    if len(files) == 100:
        results.append({"status": "unknown", "reason": "changed-file listing capped at 100"})
    return {"head_sha": head, "publication": results, "scope": "Artifact existence only; not render or runtime compatibility."}


if __name__ == "__main__":
    try:
        evidence = collect(json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        evidence = {"status": "unknown", "reason": "OCI evidence collection unavailable"}
    print(json.dumps(evidence))
