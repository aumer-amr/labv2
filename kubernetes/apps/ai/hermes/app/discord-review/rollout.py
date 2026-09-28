"""Read-only post-merge observation; Kubernetes never receives a mutating request.

Flux's native GitHub notifier was evaluated first:
https://github.com/fluxcd/notification-controller/blob/main/internal/notifier/github.go
It ignores Progressing events and maps info/error to success/failure. With this
repository's wait:false Kustomizations that cannot establish workload readiness,
initialize pending feedback, or distinguish a skipped revision from a deployed one.
Reuse the existing observer identity instead of changing reconciliation semantics.
"""

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import json
import re
import ssl
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

SHA = re.compile(r"[0-9a-f]{40}")
KINDS = {
    "Kustomization": ("kustomize.toolkit.fluxcd.io/v1", "kustomizations"),
    "HelmRelease": ("helm.toolkit.fluxcd.io/v2", "helmreleases"),
    "Deployment": ("apps/v1", "deployments"),
    "StatefulSet": ("apps/v1", "statefulsets"),
    "DaemonSet": ("apps/v1", "daemonsets"),
    "ReplicaSet": ("apps/v1", "replicasets"),
    "Job": ("batch/v1", "jobs"),
    "Pod": ("v1", "pods"),
    "PersistentVolumeClaim": ("v1", "persistentvolumeclaims"),
    "HTTPRoute": ("gateway.networking.k8s.io/v1", "httproutes"),
    "Gateway": ("gateway.networking.k8s.io/v1", "gateways"),
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Kubernetes:
    def __init__(self):
        self.account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.opener = build_opener(NoRedirect(), HTTPSHandler(
            context=ssl.create_default_context(cafile=str(self.account / "ca.crt"))))

    def get(self, path):
        # Fixed destination, rotating projected token, GET only, no raw diagnostics.
        req = Request("https://kubernetes.default.svc" + path, method="GET", headers={
            "Authorization": "Bearer " + (self.account / "token").read_text().strip(),
        })
        try:
            with self.opener.open(req, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"Kubernetes observation unavailable (HTTP {exc.code})") from None


def api_path(kind):
    version, plural = KINDS[kind]
    return ("/api/" if version == "v1" else "/apis/") + version + "/" + plural


def identity(obj):
    meta = obj["metadata"]
    return meta.get("namespace", ""), meta["name"]


def affected(paths, kustomizations):
    paths = [p for p in paths if p.startswith(("kubernetes/", ".codex-private/"))]
    if not paths:
        return []
    if any(not p.startswith("kubernetes/apps/") for p in paths):
        return kustomizations  # Shared components/bootstrap can affect every child.
    selected = {}
    for changed in paths:
        candidates = []
        for obj in kustomizations:
            root = obj["spec"]["path"].removeprefix("./").rstrip("/")
            folder = str(PurePosixPath(changed).parent)
            if (changed.startswith(root + "/")
                    or changed.endswith("/ks.yaml") and root.startswith(folder + "/")
                    or len(PurePosixPath(changed).parts) == 4 and root.startswith(folder + "/")):
                candidates.append((root, obj))
        # A parent inventory includes unrelated children; prefer the actual app owners.
        for root, obj in candidates:
            if not any(other.startswith(root + "/") for other, _ in candidates):
                selected[identity(obj)] = obj
    if not selected:
        raise ValueError("Changed Kubernetes paths have no observable owner")
    return list(selected.values())


def conditions(obj):
    return {c["type"]: c for c in obj.get("status", {}).get("conditions", [])}


def ready(obj):
    kind, spec, status = obj["kind"], obj.get("spec", {}), obj.get("status", {})
    cond = conditions(obj)
    generation = obj["metadata"].get("generation", 1)
    if spec.get("suspend"):
        return "unavailable"
    if kind in {"Kustomization", "HelmRelease"}:
        c = cond.get("Ready", {})
        if c.get("observedGeneration", status.get("observedGeneration", 0)) != generation:
            return "pending"
        if cond.get("Reconciling", {}).get("status") == "True":
            return "pending"
        if c.get("status") == "True":
            return "ready"
        if c.get("reason") == "DependencyNotReady":
            return "pending"
        return "failed" if c.get("status") == "False" else "pending"
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"}:
        if status.get("observedGeneration", 0) < generation:
            return "pending"
        if cond.get("Progressing", {}).get("reason") == "ProgressDeadlineExceeded":
            return "failed"
        if kind == "DaemonSet":
            desired = status.get("desiredNumberScheduled")
            ok = (desired is not None and status.get("updatedNumberScheduled", 0) == desired
                  and status.get("numberReady", 0) == desired and status.get("numberUnavailable", 0) == 0)
        else:
            desired = spec.get("replicas", 1)
            ok = status.get("readyReplicas", 0) == desired and status.get("replicas", 0) == desired
            if kind != "ReplicaSet":
                ok = ok and status.get("updatedReplicas", 0) == desired
        return "ready" if ok else "pending"
    if kind == "Job":
        if cond.get("Failed", {}).get("status") == "True":
            return "failed"
        return "ready" if cond.get("Complete", {}).get("status") == "True" else "pending"
    if kind == "Pod":
        if status.get("phase") == "Failed":
            return "failed"
        if obj["metadata"].get("deletionTimestamp"):
            return "pending"
        if status.get("phase") == "Succeeded" or cond.get("Ready", {}).get("status") == "True":
            return "ready"
        return "pending"
    if kind == "PersistentVolumeClaim":
        return {"Bound": "ready", "Lost": "failed"}.get(status.get("phase"), "pending")
    if kind in {"HTTPRoute", "Gateway"}:
        groups = [status.get("conditions", [])]
        required = {"Accepted", "Programmed"}
        if kind == "HTTPRoute":
            parents = status.get("parents", [])
            def parent_key(ref):
                return (ref.get("group", "gateway.networking.k8s.io"), ref.get("kind", "Gateway"),
                        ref.get("namespace", obj["metadata"]["namespace"]), ref.get("name"),
                        ref.get("sectionName"), ref.get("port"))
            desired = {parent_key(p) for p in spec.get("parentRefs", [])}
            observed = {parent_key(p.get("parentRef", {})): p for p in parents}
            if not desired or not desired <= observed.keys():
                return "pending"
            groups = [observed[p].get("conditions", []) for p in desired]
            required = {"Accepted", "ResolvedRefs"}
        states = []
        for group in groups:
            current = {c["type"]: c["status"] for c in group if c.get("observedGeneration") == generation}
            states.append("failed" if any(current.get(t) == "False" for t in required)
                          else "ready" if all(current.get(t) == "True" for t in required) else "pending")
        return aggregate(states)
    return "unavailable"


def aggregate(states):
    for state in ("failed", "unavailable", "superseded", "pending"):
        if state in states:
            return state
    return "ready"


def revision(value):
    sha = value.rsplit(":", 1)[-1]
    return sha if SHA.fullmatch(sha) else None


def observe(api, kube, pr):
    sha = pr["merge_commit_sha"]
    if not SHA.fullmatch(sha) or not pr.get("merged") or pr["base"]["ref"] != "main":
        raise ValueError("Not a merged main revision")
    files = api.pages(f"pulls/{pr['number']}/files")
    paths = {p for f in files for p in (f["filename"], f.get("previous_filename", "")) if p}
    def objects(kind):
        return [{**o, "kind": kind} for o in kube.get(api_path(kind))["items"]]

    all_ks = objects("Kustomization")
    targets = affected(paths, all_ks)
    report = {"sha": sha, "reconciliation": "ready", "readiness": "ready", "resources": 0,
              "owners": len(targets), "details": [], "app_health": "not assessed"}
    if not targets:
        report.update(reconciliation="not applicable", readiness="not applicable")
        return report
    comparisons = {}
    states = []
    for obj in targets:
        status = obj.get("status", {})
        applied = revision(status.get("lastAppliedRevision", ""))
        attempted = revision(status.get("lastAttemptedRevision", ""))
        state = "pending"
        source = obj["spec"].get("sourceRef", {})
        if (source.get("kind"), source.get("name"), source.get("namespace", obj["metadata"]["namespace"])) != ("GitRepository", "flux-system", "flux-system"):
            state = "unavailable"
        elif (attempted or applied) == sha:
            state = ready(obj)
            if state == "ready" and applied != sha:
                state = "pending"
        elif applied or attempted:
            current = attempted or applied
            if current not in comparisons:
                comparisons[current] = api.request(f"compare/{sha}...{current}")["status"]
            if comparisons[current] == "ahead":
                state = "superseded"
            elif comparisons[current] == "diverged":
                state = "unavailable"
        states.append(state)
        if state != "ready":
            report["details"].append(f"Kustomization {'/'.join(identity(obj))}: {state}")
    report["reconciliation"] = aggregate(states)
    if report["reconciliation"] != "ready":
        report["readiness"] = "pending" if report["reconciliation"] in {"pending", "failed"} else "unavailable"
        return report

    owner_keys = {identity(obj) for obj in targets}
    inventory = set()
    for obj in targets:
        entries = obj.get("status", {}).get("inventory", {}).get("entries")
        if not isinstance(entries, list):
            report.update(readiness="unavailable", details=["Flux resource inventory unavailable."])
            return report
        for entry in entries:
            ns_name, group, kind = entry["id"].rsplit("_", 2)
            namespace, name = ns_name.split("_", 1)
            if kind in KINDS and kind != "Kustomization":
                inventory.add((kind, namespace, name))

    def belongs(obj, prefix, owners):
        labels = obj["metadata"].get("labels", {})
        return (labels.get(prefix + "/namespace"), labels.get(prefix + "/name")) in owners

    resources = []
    releases = objects("HelmRelease")
    releases = [o for o in releases if belongs(o, "kustomize.toolkit.fluxcd.io", owner_keys)
                or ("HelmRelease", *identity(o)) in inventory]
    resources.extend(releases)
    helm_keys = {identity(o) for o in releases}
    uids = set()
    for kind in KINDS:
        if kind in {"Kustomization", "HelmRelease"}:
            continue
        for obj in objects(kind):
            if (belongs(obj, "kustomize.toolkit.fluxcd.io", owner_keys)
                    or belongs(obj, "helm.toolkit.fluxcd.io", helm_keys)
                    or (kind, *identity(obj)) in inventory
                    or any(ref.get("uid") in uids for ref in obj["metadata"].get("ownerReferences", []))):
                if obj["metadata"].get("uid"):
                    uids.add(obj["metadata"]["uid"])
                resources.append(obj)
    observed = {(o["kind"], *identity(o)) for o in resources}
    states = []
    for missing in sorted(inventory - observed):
        states.append("pending")
        report["details"].append(f"{missing[0]} {missing[1]}/{missing[2]}: missing")
    for obj in resources:
        state = ready(obj)
        states.append(state)
        if state != "ready":
            report["details"].append(f"{obj['kind']} {'/'.join(identity(obj))}: {state}")
    report.update(readiness=aggregate(states) if states else "not applicable", resources=len(resources))
    current = {identity(o): o for o in objects("Kustomization")}
    if any(identity(o) not in current or
           current[identity(o)]["metadata"].get("resourceVersion") != o["metadata"].get("resourceVersion")
           for o in targets):
        report.update(reconciliation="pending", readiness="unavailable",
                      details=["Flux changed during observation; retrying a consistent snapshot."])
    return report


def feedback(api, kube, pr, previous=None):
    sha = pr["merge_commit_sha"]
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError("Invalid merged SHA")
    try:
        report = observe(api, kube, pr)
    except Exception:
        # API errors/manifests may contain credentials; publish no raw diagnostics.
        report = {"sha": sha, "reconciliation": "unavailable", "readiness": "unavailable",
                  "resources": 0, "owners": 0, "details": ["Observation unavailable; retrying."], "app_health": "not assessed"}
    expired = (datetime.now(timezone.utc) - datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00"))).total_seconds() >= 3600
    report["expired"] = expired
    report["details"] = report["details"][:8]
    if report != previous:
        for field in ("reconciliation", "readiness"):
            state = report[field]
            github_state = {"ready": "success", "not applicable": "success", "pending": "pending", "failed": "failure"}.get(state, "error")
            if expired and state == "pending":
                github_state = "error"
            description = f"{state}; {report['owners']} Flux owners, {report['resources']} resources; app health not assessed"
            if expired and state not in {"ready", "not applicable"}:
                description = f"Observation window ended: {state}; app health not assessed"
            try:
                api.request(f"statuses/{sha}", "POST", {
                    "context": "Flux / " + field, "state": github_state, "description": description[:140],
                    "target_url": f"https://github.com/aumer-amr/labv2/pull/{pr['number']}",
                })
            except Exception:
                report["publication_error"] = True
    done = expired or all(report[k] in {"ready", "not applicable"} for k in ("reconciliation", "readiness"))
    reason = (f"Merged.\nFlux reconciliation: **{report['reconciliation']}**\n"
              f"Resource readiness: **{report['readiness']}** ({report['resources']} observed)\n"
              "Application health: **not assessed**.\n"
              "Readiness covers Flux/Helm, workloads, Pods, PVCs and routes; other kinds are not assessed.")
    if report.get("publication_error"):
        done = False
        reason += "\nGitHub status publication unavailable; retrying."
    if expired and not all(report[k] in {"ready", "not applicable"} for k in ("reconciliation", "readiness")):
        reason += "\nObservation window ended; no rollback or cluster changes performed."
    if report["details"]:
        reason += "\n" + "\n".join(report["details"])
    return {"head": sha, "reason": reason, "rollout": report, "rollout_done": done}
