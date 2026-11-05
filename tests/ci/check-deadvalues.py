#!/usr/bin/env python3
"""Exercise deadvalues against local charts without credentials or cluster access."""

from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from threading import Thread


def run(*args, status=0):
    result = subprocess.run(
        ["deadvalues", *args, "--output", "json"], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == status, (args, result.returncode, result.stderr)
    return json.loads(result.stdout)


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


@contextmanager
def repository(directory):
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(directory)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    chart = root / "chart"
    (chart / "templates").mkdir(parents=True)
    (chart / "values.yaml").write_text(
        "replicas: 1\nlabel: default\nfeature:\n  enabled: false\n  message: default\n",
    )
    template = '''apiVersion: v1
kind: ConfigMap
metadata:
  name: pilot
data:
  replicas: {{ .Values.replicas | quote }}
  label: {{ .Values.label | quote }}
{{- if .Values.feature.enabled }}
  message: {{ .Values.feature.message | quote }}
{{- end }}
'''
    for version in ("1.0.0", "2.0.0"):
        (chart / "Chart.yaml").write_text(f"apiVersion: v2\nname: pilot\nversion: {version}\n")
        (chart / "templates/config.yaml").write_text(
            template if version == "1.0.0" else template.replace(
                "  label: {{ .Values.label | quote }}\n", "",
            ),
        )
        with tarfile.open(root / f"pilot-{version}.tgz", "w:gz") as archive:
            archive.add(chart, arcname="pilot")
    (root / "index.yaml").write_text('''apiVersion: v1
entries:
  pilot:
    - apiVersion: v2
      name: pilot
      version: 1.0.0
      urls: [pilot-1.0.0.tgz]
    - apiVersion: v2
      name: pilot
      version: 2.0.0
      urls: [pilot-2.0.0.tgz]
''')
    values = root / "values.yaml"
    values.write_text(
        "replicas: 1\nlabel: configured\nfeature:\n  enabled: false\n  message: configured\n",
    )
    config = root / "configuration.yaml"
    config.write_text("{}\n")
    common = ["-f", str(values), "--config", str(config), "--cache-dir", str(root / "cache")]

    with repository(root) as url:
        target = ["--chart", "pilot", "--repo", url, *common]
        report = run("check", *target, "--version", "1.0.0")
        statuses = {key["key"]: key["status"] for key in report["keys"]}
        assert statuses["label"] == "ALIVE"
        assert statuses["replicas"] == "REDUNDANT"
        assert statuses["feature.message"] == "CONDITIONAL"

        values.write_text(values.read_text() + "typo: unused\n")
        report = run("check", *target, "--version", "1.0.0", status=1)
        assert any(key["key"] == "typo" and key["status"] == "DEAD" for key in report["keys"])

        # Only the reviewed key is ignored; a second typo must still fail.
        config.write_text("ignore:\n  - keys: [typo]\n")
        run("check", *target, "--version", "1.0.0")
        values.write_text(values.read_text() + "secondTypo: unused\n")
        run("check", *target, "--version", "1.0.0", status=1)
        config.write_text("{}\n")

        report = run("diff", *target, "--from", "1.0.0", "--to", "2.0.0", status=1)
        assert any(change["key"] == "label" and change["kind"] == "BREAKING"
                   for change in report["changes"])

        # Dots inside a label name belong to one segment, not a nested path.
        values.write_text("labels:\n  app.kubernetes.io/name: pilot\n")
        config.write_text('ignore:\n  - keys: ["labels.app?kubernetes?io/name"]\n')
        run("check", *target, "--version", "1.0.0")
        values.write_text(values.read_text() + "  app.kubernetes.io/instance: pilot\n")
        report = run("check", *target, "--version", "1.0.0", status=1)
        assert any(key["key"] == "labels.app.kubernetes.io/instance"
                   and key["status"] == "DEAD" and not key.get("ignored") for key in report["keys"])

        # A chart download failure is an error, never an empty successful scan.
        run("check", "--chart", "missing", "--repo", url, *common, status=2)

    unsupported = root / "unsupported"
    unsupported.mkdir()
    (unsupported / "resourceset.yaml").write_text('''apiVersion: fluxcd.controlplane.io/v1
kind: ResourceSet
metadata:
  name: generated-release
spec:
  inputs:
    - id: pilot
  resources:
    - apiVersion: helm.toolkit.fluxcd.io/v2
      kind: HelmRelease
      metadata:
        name: generated-release
      spec:
        chartRef:
          kind: OCIRepository
          name: pilot
''')
    report = run("scan", str(unsupported), "--repo-root", str(root), "--config", str(config))
    assert any(item.get("skipKind") == "unsupported" for item in report)

print("Deadvalues checks passed: classifications, typos, scoped ignores, chart upgrades, errors, and unsupported resources")
