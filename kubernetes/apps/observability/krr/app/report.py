"""Run a bounded, read-only KRR scan and deliver a sanitized Vikunja report."""

import hashlib
import json
import math
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


PROMETHEUS = "http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:9090"
MCP = "http://litellm.ai.svc.cluster.local:4000/mcp-rest/tools/call"
VIKUNJA = "http://vikunja.developer-tools.svc.cluster.local:3456/api/v2"
VIKUNJA_SERVER = "21d71e161501927cea03b5b1d3e39082"
WORKLOADS = {"mealie", "sendspin", "zigbee2mqtt"}
HOURS = 336
EXPECTED_POINTS = HOURS * 4
MAX_BYTES = 8_000_000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = build_opener(NoRedirect)


def request(url, body=None, token=None, method=None, content_type="application/json", raw=False):
    headers = {"Content-Type": content_type, "User-Agent": "LabV2-KRR"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None and not isinstance(body, bytes):
        body = json.dumps(body, allow_nan=False).encode()
    try:
        with HTTP.open(Request(url, data=body, headers=headers, method=method), timeout=30) as response:
            data = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        with exc:
            code = exc.code
        raise RuntimeError("Upstream HTTP " + str(code)) from None
    if len(data) > MAX_BYTES:
        raise ValueError("Upstream response exceeds report limit")
    return data if raw else json.loads(data)


def vikunja(name, arguments):
    result = request(MCP, {"server_id": VIKUNJA_SERVER, "name": name, "arguments": arguments},
                     os.environ["LITELLM_API_KEY"])
    if result.get("isError"):
        raise RuntimeError("Vikunja MCP failed")
    data = result.get("structuredContent")
    if data is None:
        data = json.loads(next(c["text"] for c in result["content"] if c["type"] == "text"))
    if not isinstance(data, dict):
        raise ValueError("Invalid Vikunja MCP result")
    return data


def query(expression):
    result = request(PROMETHEUS + "/api/v1/query?" + urlencode({"query": expression}))
    if result.get("status") != "success" or result.get("warnings"):
        raise ValueError("Prometheus query failed or returned warnings")
    return result["data"]["result"]


def number(value):
    if value is None or value == "?":
        return value
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid resource quantity")
    return value


def resources(value, recommended=False):
    return {section: {resource: number(value[section][resource]["value"] if recommended
                                      else value[section].get(resource))
                      for resource in ("cpu", "memory")} for section in ("requests", "limits")}


def evidence(obj):
    pods = [pod["name"] for pod in obj["pods"]]
    if not pods:
        return {"coverage": 0, "oom_events": None, "peak_cpu": None, "peak_memory": None}
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", pod) for pod in pods):
        raise ValueError("Invalid pod name")
    selector = 'namespace="home",container=' + json.dumps(obj["container"]) + ',pod=~' + json.dumps(
        "|".join(re.escape(pod) for pod in pods))
    cpu = "max(rate(container_cpu_usage_seconds_total{" + selector + "}[5m]))"
    memory = "max(container_memory_working_set_bytes{" + selector + "})"

    def scalar(expression):
        rows = query(expression)
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("Expected one evidence value")
        return number(float(rows[0]["value"][1]))

    counts = [scalar("count_over_time((" + metric + ")[14d:15m])") or 0 for metric in (cpu, memory)]
    return {"coverage": round(min(1, min(counts) / EXPECTED_POINTS), 3),
            "peak_cpu": scalar("max_over_time((" + cpu + ")[14d:15m])"),
            "peak_memory": scalar("max_over_time((" + memory + ")[14d:15m])"),
            "oom_events": scalar("sum(increase(container_oom_events_total{" + selector + "}[14d]))")}


def sanitize(data):
    if not isinstance(data, dict) or data.get("errors") or not data.get("scans"):
        raise ValueError("KRR returned errors or no scans")
    scans = []
    for scan in data["scans"]:
        obj = scan["object"]
        if obj["name"] not in WORKLOADS:
            continue
        if (obj["namespace"] != "home" or obj["kind"] != "Deployment"
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", obj["container"])):
            raise ValueError("KRR returned an unexpected target")
        current = resources(obj["allocations"])
        recommended = resources(scan["recommended"], recommended=True)
        observed = evidence(obj)
        gaps = []
        if observed["coverage"] < 0.8:
            gaps.append("Less than 80% of the 14-day CPU/memory sample window")
        if obj.get("hpa"):
            gaps.append("HPA present; review resource targets and scaling before any change")
        warning_text = {
            "NoPrometheusPods": "KRR: no historical pod list from Prometheus",
            "NoPrometheusCPUMetrics": "KRR: a CPU history loader returned no data",
            # KRR also sets this warning when its optional OOM-limit loader is empty.
            "NoPrometheusMemoryMetrics": "KRR: a memory loader returned no data, possibly optional OOM-limit history; check coverage and OOM evidence",
        }
        gaps.extend(warning_text.get(warning, "Unrecognized KRR warning; review evidence")
                    for warning in sorted(set(obj.get("warnings", []))))
        if any(value == "?" for group in recommended.values() for value in group.values()):
            gaps.append("KRR could not recommend at least one resource: insufficient data or HPA exclusion")
        if observed["oom_events"] is None:
            gaps.append("OOM counter history unavailable")
        elif observed["oom_events"] > 0:
            gaps.append("OOM events detected; review limits and incident history")
        scans.append({"namespace": "home", "kind": "Deployment", "workload": obj["name"],
                      "container": obj["container"], "current": current, "recommended": recommended,
                      "evidence": observed, "gaps": gaps})
    if {scan["workload"] for scan in scans} != WORKLOADS:
        raise ValueError("KRR did not scan every selected workload")
    return sorted(scans, key=lambda scan: (scan["workload"], scan["container"]))


def quantity(value, resource):
    if value is None:
        return "unset"
    if value == "?":
        return "unknown"
    return f"{math.ceil(value * 1000)}m" if resource == "cpu" else f"{math.ceil(value / 1048576)}Mi"


def report(scans, stamp):
    lines = ["Weekly Kubernetes resource review — " + stamp, "",
             "Scope: home/mealie, home/sendspin, home/zigbee2mqtt. KRR v1.31.1; 14-day history; "
             "CPU p95; memory peak +15%; experimental OOM-limit headroom +25%; CPU floor 10m; memory floor 100Mi. "
             "HPA overrides are disabled. Coverage and peaks below use 15-minute samples; short peaks may be missed.", "",
             "Requests reserve scheduler capacity. Limits cap usage. Values below are proposals, not changes.", "",
             "| Workload / container | Current CPU request / limit | Proposed CPU request / limit | "
             "Current memory request / limit | Proposed memory request / limit | Coverage | Evidence gaps |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for scan in scans:
        values = []
        for resource in ("cpu", "memory"):
            for key in ("current", "recommended"):
                values.append(" / ".join(quantity(scan[key][section][resource], resource)
                                         for section in ("requests", "limits")))
        lines.append("| " + " | ".join([scan["workload"] + " / " + scan["container"], *values,
                                        f'{scan["evidence"]["coverage"]:.0%}', "; ".join(scan["gaps"]) or "None detected"]) + " |")
    lines += ["", "No resource edit is approved by this report. Review OOMs, startup and peak demand, background/backup "
              "activity, and single-node drain headroom first. Detailed sampled peaks and OOM counts are in the JSON.", "",
              "AI/model workloads are excluded from this initial ordinary-application scan. Idle CPU/memory cannot establish "
              "cold-start or inference demand, GPU memory requirements, or safe drain capacity. Review those separately.", "",
              "JSON delivery is complete only when the attachment is present. A report-ready notification is sent only after "
              "the uploaded bytes have been downloaded and verified.", "", "Tracking: https://vikunja.aumer.dev/tasks/44"]
    return "\n".join(lines)


def upload(task_id, payload):
    token = os.environ["VIKUNJA_API_TOKEN"]
    boundary = "krr-" + hashlib.sha256(payload).hexdigest()
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="krr.json"\r\n'
            'Content-Type: application/json\r\n\r\n').encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    result = request(VIKUNJA + f"/tasks/{task_id}/attachments", body, token, "POST",
                     "multipart/form-data; boundary=" + boundary)
    attachments = result.get("success", [])
    if result.get("errors") or len(attachments) != 1 or not isinstance(attachments[0].get("id"), int):
        raise ValueError("Vikunja attachment upload failed")
    downloaded = request(VIKUNJA + f'/tasks/{task_id}/attachments/{attachments[0]["id"]}', token=token, raw=True)
    if downloaded != payload:
        raise ValueError("Attachment readback mismatch")


def notify(message, url):
    result = request("https://api.pushover.net/1/messages.json", urlencode({
        "token": os.environ["PUSHOVER_API_KEY"], "user": os.environ["PUSHOVER_USER_KEY"],
        "title": "Weekly KRR report", "message": message, "url": url,
        "url_title": "Open report",
    }).encode(), content_type="application/x-www-form-urlencoded")
    if result.get("status") != 1:
        raise ValueError("Pushover rejected notification")


def run():
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    projects = vikunja("projects_list", {"q": "LabV2", "format": "markdown"})
    matches = [p for p in projects["items"] if p["title"] == "LabV2" and not p.get("is_archived")]
    if len(matches) != 1 or projects.get("total_pages", 1) > 1:
        raise ValueError("Expected one active LabV2 project")
    # KRR also applies --selector to HPA discovery; scan the namespace before filtering report targets.
    args = ["python", "/app/krr.py", "simple", "--namespace", "home", "--resource", "Deployment",
            "--prometheus-url", PROMETHEUS, "--max-workers", "3", "--history_duration", str(HOURS),
            "--timeframe_duration", "1.25", "--cpu_percentile", "95", "--memory_buffer_percentage", "15",
            "--use_oomkill_data", "--oom_memory_buffer_percentage", "25", "--points_required", "100",
            "--formatter", "json", "--fileoutput", "/tmp/krr.json", "--quiet"]
    # KRR logs and raw JSON can include workload annotations/configuration; never publish them.
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "HOME", "LANG", "KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT"}}
    subprocess.run(args, check=True, timeout=1400, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    path = Path("/tmp/krr.json")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("KRR report exceeds size limit")
    scans = sanitize(json.loads(path.read_text()))
    payload = json.dumps({"generated_at": stamp, "krr_version": "1.31.1", "history_hours": HOURS,
                          "scans": scans}, allow_nan=False, indent=2).encode()
    task = vikunja("tasks_create", {"project": matches[0]["id"], "title": "KRR resource report " + stamp,
                                    "description": report(scans, stamp), "format": "markdown", "done": False})
    if (type(task.get("id")) is not int or task["id"] <= 0
            or task.get("project_id") != matches[0]["id"]):
        raise ValueError("Invalid report task")
    upload(task["id"], payload)
    url = "https://vikunja.aumer.dev/tasks/" + str(task["id"])
    notify("Resource recommendations and verified JSON are ready for review. No resources were changed.", url)
    print("KRR report delivered: " + url)


def main():
    try:
        run()
    except Exception:
        try:
            notify("Weekly resource report failed or delivery is incomplete. Review the observability/krr Job.",
                   "https://vikunja.aumer.dev/tasks/44")
        except Exception:
            pass
        raise RuntimeError("KRR reporting failed; inspect job status and upstream availability") from None


if __name__ == "__main__":
    main()
