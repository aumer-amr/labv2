#!/usr/bin/env python3
"""Render with Flate in a read-only job; publish only allowlisted evidence in another."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

MARKER = "<!-- labv2:rendered-kubernetes-summary -->"
WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController", "Job", "CronJob", "Pod"}
RBAC = {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}
REPLICATED = {"Deployment", "StatefulSet", "ReplicaSet", "ReplicationController"}
LIMIT = 100
IMAGE = r"(?:[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/)?[a-z0-9]+(?:[._/-][a-z0-9]+)*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[0-9a-f]{64})?"


def run(args, **kwargs):
    # Renderer diagnostics can contain manifest values. Never forward them to CI.
    return subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=600, **kwargs)


def token(value, pattern):
    if not isinstance(value, str) or len(value) > 512 or not re.fullmatch(pattern, value):
        raise ValueError("invalid summary field")
    return value


def revisions(event):
    pr = event["pull_request"]
    return tuple(token(pr[side]["sha"], r"[0-9a-f]{40}") for side in ("base", "head"))


def inventory(docs):
    if not isinstance(docs, list):
        raise ValueError("invalid render")
    result = {}
    for doc in docs:
        kind = token(doc["kind"], r"[A-Za-z][A-Za-z0-9]{0,100}")
        # Defense in depth: even if Flate's output filter changes, ignore Secret bodies.
        if kind == "Secret":
            continue
        metadata = doc["metadata"]
        namespace = token(metadata.get("namespace") or "cluster", r"[a-z0-9][a-z0-9.-]{0,252}")
        name = metadata["name"]
        if isinstance(name, str) and "PLACEHOLDER_" in name:
            # SOPS-derived names cannot identify real resources offline.
            result[(kind, namespace, "<unavailable>")] = None
            continue
        key = (kind, namespace, token(name, r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}"))
        spec = doc.get("spec") or {}
        replicas = spec.get("replicas", 1) if kind in REPLICATED else "n/a"
        if kind in REPLICATED and (type(replicas) is not int or not 0 <= replicas <= 2147483647):
            raise ValueError("invalid replicas")
        pod = spec
        if kind == "CronJob":
            pod = spec["jobTemplate"]["spec"]["template"]["spec"]
        elif kind in WORKLOADS - {"Pod"}:
            pod = spec["template"]["spec"]
        images = sorted({token(c["image"], IMAGE) for field in ("containers", "initContainers", "ephemeralContainers")
                         for c in pod.get(field, [])}) if kind in WORKLOADS else []
        # Hashes stay in process memory; only identity, replicas and images cross jobs.
        value = (hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).digest(), replicas, images)
        if key in result and result[key] != value:
            value = None  # Multiple producers disagree; do not guess which definition wins.
        result[key] = value
    return result


def compare(before, after):
    rows = []
    ambiguous = 0
    unknown_scopes = {key[:2] for key in before.keys() | after.keys() if key[2] == "<unavailable>"}
    for key in sorted(before.keys() | after.keys()):
        if (key in before and before[key] is None) or (key in after and after[key] is None):
            ambiguous += 1
            continue
        old, new = before.get(key), after.get(key)
        if key[:2] in unknown_scopes and (old is None or new is None):
            ambiguous += 1
            continue
        if old == new:
            continue
        status = "added" if old is None else "removed" if new is None else "changed"
        rows.append([*key, status, old[1] if old else "absent", new[1] if new else "absent",
                     old[2] if old else [], new[2] if new else []])
    # Bound the job output and comment; omissions must be visible, never a clean bill.
    return {"rows": rows[:LIMIT], "omitted": max(0, len(rows) - LIMIT), "ambiguous": ambiguous}


def collect(event):
    base, head = revisions(event)
    evidence = {"base": base, "head": head, "status": "unavailable"}
    if event["pull_request"]["head"]["repo"]["full_name"] != event["repository"]["full_name"]:
        return evidence  # Never render fork-controlled sources in a privileged trigger.
    try:
        # Fetch by the repository's PR ref, then require the exact event SHA.
        run(["git", "fetch", "--no-tags", "origin", f"refs/pull/{int(event['number'])}/head"])
        actual = run(["git", "rev-parse", "FETCH_HEAD"]).decode().strip()
        if actual != head:
            return evidence
        renders = []
        with tempfile.TemporaryDirectory() as directory:
            for side, sha in (("base", base), ("head", head)):
                tree = Path(directory) / side
                run(["git", "worktree", "add", "--detach", str(tree), sha])
                try:
                    env = {**os.environ, "FLATE_BASE": ""}
                    raw = run(["flate", "build", "all", "--path", str(tree / "kubernetes/flux/cluster"),
                               "--output", "json", "--skip-secrets", "--skip-crds=false", "--restrict-egress",
                               "--cache-dir", str(Path(directory) / "cache")], env=env)
                    renders.append(inventory(json.loads(raw)))
                finally:
                    run(["git", "worktree", "remove", "--force", str(tree)])
        evidence.update(status="rendered", **compare(*renders))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        # A partial render is not evidence of deletions. Do not emit partial inventories.
        pass
    return evidence


def body(event, evidence):
    base, head = revisions(event)
    lines = [MARKER, "## Rendered Kubernetes changes", f"Base: `{base}`", f"Head: `{head}`"]
    if evidence.get("base") != base or evidence.get("head") != head or evidence.get("status") != "rendered":
        return "\n\n".join(lines + ["**Rendering evidence unavailable.** Rendering failed, timed out, was skipped, or did not match this revision. No change categories can be verified. Renderer diagnostics are withheld to protect secrets."])
    rows = evidence["rows"]
    omitted = evidence["omitted"]
    ambiguous = evidence["ambiguous"]
    if not isinstance(rows, list) or len(rows) > LIMIT or any(type(n) is not int or n < 0 for n in (omitted, ambiguous)):
        raise ValueError("invalid summary")
    namespaces, workloads, images, replicas, deletions, rbac = [], [], [], [], [], []
    for row in rows:
        kind, namespace, name, status, old_count, new_count, old_images, new_images = row
        kind = token(kind, r"[A-Za-z][A-Za-z0-9]{0,100}")
        namespace = token(namespace, r"[a-z0-9][a-z0-9.-]{0,252}")
        name = token(name, r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}")
        if status not in {"added", "removed", "changed"}:
            raise ValueError("invalid status")
        for count in (old_count, new_count):
            if not (type(count) is int and 0 <= count <= 2147483647) and count not in ("absent", "n/a"):
                raise ValueError("invalid count")
        for values in (old_images, new_images):
            if not isinstance(values, list) or len(values) > 50:
                raise ValueError("invalid images")
            for value in values:
                token(value, IMAGE)
        ref = f"`{kind} {namespace}/{name}`"
        namespaces.append(namespace)
        if kind in WORKLOADS:
            workloads.append(f"{ref}: {status}")
            if kind in REPLICATED:
                replicas.append(f"{ref}: {old_count} → {new_count}")
            if old_images != new_images:
                images.append(f"{ref}: {', '.join('`' + i + '`' for i in old_images) or 'absent'} → {', '.join('`' + i + '`' for i in new_images) or 'absent'}")
        if status == "removed":
            deletions.append(ref)
        if kind in RBAC:
            rbac.append(f"{ref}: {status}")
    lines.append("Namespaces: " + (", ".join(f"`{n}`" for n in sorted(set(namespaces))) or "none"))
    for title, items in (("Workloads", workloads), ("Images", images), ("Replica counts", replicas),
                         ("Resource deletions", deletions), ("RBAC changes", rbac)):
        lines.append(f"### {title}\n\n" + ("\n".join(f"- {item}" for item in items) or "None observed in the available render."))
    lines.append("**Evidence limits:** Offline Flate output only. Secret objects and values are excluded; SOPS decryption, live-generated values and suspended producers are unavailable. Replica defaults are 1 where omitted; autoscaler/runtime counts are unavailable. Deletions mean absent rendered objects, not confirmed live deletion. RBAC lists affected objects, not permission analysis.")
    if omitted:
        lines.append(f"**Partial summary:** {omitted} additional changed resources omitted; category lists are incomplete.")
    if ambiguous:
        lines.append(f"**Unavailable evidence:** {ambiguous} resource identities or scopes have conflicting rendered definitions or unresolved secret-derived names. They are excluded; additions/deletions within an unresolved kind/namespace are suppressed.")
    result = "\n\n".join(lines)
    if len(result) > 60000:
        raise ValueError("summary too large")
    return result


def gh(endpoint, payload=None):
    args = ["gh", "api", endpoint]
    if payload is not None:
        args += ["--method", "PATCH" if "/issues/comments/" in endpoint else "POST", "--input", "-"]
    return json.loads(run(args, input=json.dumps(payload).encode() if payload is not None else None))


def publish(event, evidence):
    base, head = revisions(event)
    repo = token(event["repository"]["full_name"], r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
    number = int(event["number"])
    current = gh(f"repos/{repo}/pulls/{number}")
    if current["state"] != "open" or revisions({"pull_request": current}) != (base, head):
        return  # Never overwrite a newer revision's summary with a late job.
    try:
        comment = body(event, evidence)
    except (ValueError, KeyError, TypeError):
        comment = body(event, {})
    comments = []
    page = 1
    while True:
        batch = gh(f"repos/{repo}/issues/{number}/comments?per_page=100&page={page}")
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    existing = next((c for c in comments if c["user"]["login"] == "github-actions[bot]"
                     and c["body"].startswith(MARKER)), None)
    # Recheck after pagination, immediately before the write.
    current = gh(f"repos/{repo}/pulls/{number}")
    if current["state"] != "open" or revisions({"pull_request": current}) != (base, head):
        return
    endpoint = f"repos/{repo}/issues/comments/{int(existing['id'])}" if existing else f"repos/{repo}/issues/{number}/comments"
    gh(endpoint, {"body": comment})


if __name__ == "__main__":
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        if sys.argv[1] == "collect":
            evidence = json.dumps(collect(event), separators=(",", ":"))
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write("summary=" + evidence + "\n")
        elif sys.argv[1] == "publish":
            try:
                evidence = json.loads(os.environ.get("RENDERED_SUMMARY") or "{}")
                if not isinstance(evidence, dict):
                    evidence = {}
            except ValueError:
                evidence = {}
            publish(event, evidence)
        else:
            raise ValueError("invalid command")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        sys.exit("Rendered summary operation failed; diagnostics withheld to protect secrets.")
