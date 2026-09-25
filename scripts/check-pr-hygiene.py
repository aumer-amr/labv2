#!/usr/bin/env python3
"""Check the PR diff using the same tools as Lefthook, without rewriting files."""

from pathlib import PurePosixPath
import subprocess
import sys


def check(base, head):
    revision = f"{base}...{head}"
    subprocess.run(["git", "diff", "--check", revision, "--"], check=True)
    changed = subprocess.check_output([
        "git", "diff", "--name-only", "--no-renames", "-z", revision, "--",
    ]).decode().split("\0")[:-1]
    existing = subprocess.check_output([
        "git", "diff", "--name-only", "--no-renames", "--diff-filter=ACMT", "-z", revision, "--",
    ]).decode().split("\0")[:-1]
    formatted = [f"./{p}" for p in existing if PurePosixPath(p).suffix in {
        ".yaml", ".yml", ".json", ".json5", ".jsonc", ".md", ".markdown", ".mdx",
    }]
    workflows = [f"./{p}" for p in existing if (
        p.startswith(".github/workflows/") and PurePosixPath(p).suffix in {".yaml", ".yml"}
    ) or (
        p.startswith(".github/actions/") and PurePosixPath(p).name in {"action.yaml", "action.yml"}
    )]
    if formatted:
        subprocess.run(["oxfmt", "--check", *formatted], check=True)
    if workflows:
        subprocess.run(["zizmor", "--offline", *workflows], check=True)
    if any(p.startswith("kubernetes/") or p in {
        "scripts/check-kubernetes-conventions.py", "scripts/check-kubernetes-conventions-test.py",
        "scripts/check-pr-hygiene.py", ".github/workflows/pr-hygiene.yaml",
        ".mise/config.toml", ".mise/mise.lock",
    } for p in changed):
        subprocess.run([sys.executable, "scripts/check-kubernetes-conventions.py", base, head], check=True)
    if any(p in {
        ".github/workflows/ai-pr-review.yaml", ".github/workflows/pr-hygiene.yaml",
        "scripts/check-pr-reviewer.py", "scripts/check-pr-hygiene.py",
        ".lefthook.toml", ".mise.toml", ".mise/config.toml", ".mise/mise.lock",
    } or p.startswith("kubernetes/apps/actions-runners/pr-reviewer/") for p in changed):
        subprocess.run([sys.executable, "scripts/check-pr-reviewer.py"], check=True)
    print("PR hygiene checks passed")


if __name__ == "__main__":
    check(*sys.argv[1:])
