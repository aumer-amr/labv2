#!/usr/bin/env python3
"""Regression checks for required PR gates; no credentials or cluster access."""

from contextlib import redirect_stdout
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hygiene", ROOT / "scripts/check-pr-hygiene.py")
hygiene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hygiene)


def yaml(path):
    return json.loads(subprocess.check_output(["yq", "-o=json", ".", str(ROOT / path)]))


workflow = yaml(".github/workflows/flate.yaml")
gate = workflow["jobs"]["success"]
assert set(gate["needs"]) == {"filter", "flate"}
assert gate["if"] == "${{ always() }}"
assert workflow["jobs"]["filter"]["steps"][0]["with"]["patterns"] == "kubernetes/**/*"
assert workflow["jobs"]["flate"]["if"] == "${{ needs.filter.outputs.changed-files != '[]' }}"
for filtered, validated, changed in itertools.product(
    ["success", "failure", "cancelled", "skipped"],
    ["success", "failure", "cancelled", "skipped"],
    ["[]", '["kubernetes/app.yaml"]', "", "null", "{}", "[1]"],
):
    result = subprocess.run(
        [sys.executable, "-c", gate["steps"][0]["run"]], capture_output=True,
        env={**os.environ, "FILTER_RESULT": filtered, "FLATE_RESULT": validated,
             "CHANGED_FILES": changed},
    )
    expected = filtered == "success" and (
        (changed == "[]" and validated == "skipped")
        or (changed == '["kubernetes/app.yaml"]' and validated == "success")
    )
    assert (result.returncode == 0) == expected, (filtered, validated, changed)

assert yaml(".github/workflows/pr-hygiene.yaml")["on"] == {"pull_request": {"branches": ["main"]}}
# Git --no-renames exposes both sides of a rename, including removed safety inputs.
for changed, existing, tools in [
    ([], [], []),
    (["plain.txt"], ["plain.txt"], []),
    (["old.yaml", "space name.yml"], ["space name.yml"], ["oxfmt"]),
    (["deleted.json"], [], []),
    ([".github/actions/demo/action.yml"], [".github/actions/demo/action.yml"], ["oxfmt", "zizmor"]),
    ([".github/workflows/test.yaml"], [".github/workflows/test.yaml"], ["oxfmt", "zizmor"]),
    (["scripts/check-pr-reviewer.py"], [], [sys.executable]),
    (["kubernetes/apps/actions-runners/pr-reviewer/app/helmrelease.yaml"], [], [sys.executable]),
    ([".mise/config.toml"], [".mise/config.toml"], [sys.executable]),
]:
    output = [b"".join(p.encode() + b"\0" for p in paths) for paths in (changed, existing)]
    with patch.object(hygiene.subprocess, "check_output", side_effect=output), \
         patch.object(hygiene.subprocess, "run") as run, redirect_stdout(io.StringIO()):
        hygiene.check("base", "head")
        assert run.call_args_list[0].args[0] == ["git", "diff", "--check", "base...head", "--"]
        assert [call.args[0][0] for call in run.call_args_list[1:]] == tools
        assert all(call.kwargs["check"] for call in run.call_args_list)
        if "space name.yml" in existing:
            assert run.call_args_list[1].args[0] == ["oxfmt", "--check", "./space name.yml"]

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    bad_json = root / "bad.json"
    bad_json.write_text('{"a":1,"b":2}\n')
    bad_workflow = root / "bad.yaml"
    bad_workflow.write_text('on: pull_request\njobs:\n  bad:\n    runs-on: ubuntu-latest\n'
                            '    steps:\n      - run: echo "${{ github.event.pull_request.title }}"\n')
    for command in (["oxfmt", "--check", str(bad_json)], ["zizmor", "--offline", str(bad_workflow)]):
        assert subprocess.run(command, capture_output=True).returncode != 0, command

    for name in ["scripts/check-pr-reviewer.py", ".github/workflows/ai-pr-review.yaml",
                 "kubernetes/apps/actions-runners/pr-reviewer/app/helmrelease.yaml"]:
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / name).read_bytes())
    reviewer = yaml(".github/workflows/ai-pr-review.yaml")
    reviewer["jobs"]["review"]["steps"][0]["with"]["persist-credentials"] = True
    (root / ".github/workflows/ai-pr-review.yaml").write_text(json.dumps(reviewer))
    assert subprocess.run(
        [sys.executable, str(root / "scripts/check-pr-reviewer.py")], capture_output=True,
    ).returncode != 0

print("PR CI checks passed: gate outcomes, file selection, and negative safety fixtures")
