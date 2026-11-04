#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

SKILL = "labv2-private"
STATE = "labv2-private-state"
ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 2_000_000


class Unavailable(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# Keep project credentials inside the existing Langfuse pod for workstation access.
RELAY = r"""
const input = [];
process.stdin.on('data', c => input.push(c));
process.stdin.on('end', async () => {
  try {
    const {path, method, data} = JSON.parse(Buffer.concat(input));
    if (!path.startsWith('/api/public/')) throw Error();
    const publicKey = process.env.LANGFUSE_INIT_PROJECT_PUBLIC_KEY;
    const secretKey = process.env.LANGFUSE_INIT_PROJECT_SECRET_KEY;
    if (!publicKey || !secretKey) {
      process.stdout.write(JSON.stringify({status: 401, body: null})); return;
    }
    const response = await fetch('http://127.0.0.1:3000' + path, {
      method, redirect: 'manual', signal: AbortSignal.timeout(15000),
      headers: {Authorization: 'Basic ' + Buffer.from(publicKey + ':' + secretKey).toString('base64'),
                'Content-Type': 'application/json'},
      body: data === null ? undefined : JSON.stringify(data)
    });
    process.stdout.write(JSON.stringify({status: response.status, body: await response.json()}));
  } catch { process.stderr.write('Langfuse request failed\n'); process.exitCode = 1; }
});
"""


def request(path, method="GET", data=None):
    public = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret = os.environ.get("LANGFUSE_SECRET_KEY")
    if public or secret:
        if not public or not secret:
            raise RuntimeError("Both Langfuse credential variables are required")
        base = os.environ.get("LANGFUSE_BASE_URL", "https://langfuse.aumer.dev").rstrip("/")
        if not base.startswith("https://") and base != "http://langfuse-web.ai.svc.cluster.local:3000":
            raise RuntimeError("Langfuse requires HTTPS or the internal service URL")
        auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
        req = Request(base + path, method=method,
                      headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"},
                      data=None if data is None else json.dumps(data).encode())
        try:
            with build_opener(NoRedirect()).open(req, timeout=20) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise RuntimeError("Langfuse response exceeds the size limit")
            return json.loads(raw)
        except HTTPError as error:
            code = error.code
            error.close()
            if code >= 500:
                raise Unavailable("Langfuse is unavailable") from None
            raise RuntimeError(f"Langfuse rejected the request (HTTP {code})") from None
        except (URLError, TimeoutError, OSError):
            raise Unavailable("Langfuse is unavailable") from None
    environment = os.environ.copy()
    common = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True)
    if common.returncode == 0 and not Path(environment.get("KUBECONFIG", "/nonexistent")).is_file():
        common_path = (ROOT / common.stdout.strip()).resolve()
        environment["KUBECONFIG"] = str(common_path.parent / "kubeconfig")
    try:
        result = subprocess.run(
            ["mise", "exec", "--", "kubectl", "exec", "-i", "--namespace", "ai",
             "deployment/langfuse-web", "--", "node", "-e", RELAY],
            input=json.dumps({"path": path, "method": method, "data": data}),
            capture_output=True, text=True, timeout=30, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Unavailable("Langfuse cluster access is unavailable") from None
    if result.returncode:
        if any(word in result.stderr for word in ("Forbidden", "Unauthorized")):
            raise RuntimeError("Langfuse cluster access was denied")
        raise Unavailable("Langfuse cluster access failed; configure credentials or restore cluster access")
    if len(result.stdout.encode()) > MAX_BYTES:
        raise RuntimeError("Langfuse response exceeds the size limit")
    response = json.loads(result.stdout)
    if response["status"] >= 500:
        raise Unavailable("Langfuse is unavailable")
    if not 200 <= response["status"] < 300:
        raise RuntimeError(f"Langfuse rejected the request (HTTP {response['status']})")
    return response["body"]


def validate_bundle(bundle):
    if (not isinstance(bundle, dict) or bundle.get("name") != SKILL or type(bundle.get("version")) is not int
            or bundle["version"] < 1):
        raise RuntimeError("Invalid private context version")
    files = bundle.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 200:
        raise RuntimeError("Invalid private context file list")
    paths = set()
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid private context file")
        name, content = item.get("path"), item.get("content")
        if (not isinstance(name, str) or not name or "\\" in name
                or PurePosixPath(name).is_absolute() or len(name) > 512
                or any(part in ("", ".", "..") for part in name.split("/"))
                or name in paths or name == "context.json" or not isinstance(content, str)):
            raise RuntimeError("Invalid private context file")
        digest = base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()
        if digest != item.get("sha256Hash"):
            raise RuntimeError("Private context checksum mismatch")
        paths.add(name)
        total += len(content.encode())
    if total > 1_000_000 or not {"SKILL.md", "AGENTS.md"} <= paths:
        raise RuntimeError("Private context is incomplete or exceeds the size limit")
    if not any(re.fullmatch(r"skills/[^/]+/SKILL.md", name) for name in paths):
        raise RuntimeError("Private context contains no skills")
    return files


def cached_bundle(destination):
    current = (destination / "current").resolve(strict=True)
    if current.parent != destination.resolve() or current.is_symlink():
        raise RuntimeError("Invalid private context cache path")
    metadata = current / "context.json"
    if metadata.is_symlink():
        raise RuntimeError("Invalid private context cache metadata")
    bundle = json.loads(metadata.read_text())
    for item in validate_bundle(bundle):
        path = current / item["path"]
        if (not path.resolve().is_relative_to(current) or path.is_symlink()
                or path.read_bytes() != item["content"].encode()):
            raise RuntimeError("Private context cache checksum mismatch")
    return bundle


def fetch(destination):
    destination = Path(destination)
    if destination.is_symlink():
        raise RuntimeError("Private context directory must not be a symlink")
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    destination.chmod(0o700)
    try:
        bundle = request(f"/api/public/skills/{SKILL}?label=production&includeContent=true")
    except Unavailable:
        bundle = cached_bundle(destination)
        print("Langfuse unavailable; using the validated private context cache", file=sys.stderr)
        return bundle
    files = validate_bundle(bundle)
    target = destination / f"version-{bundle['version']}"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise RuntimeError("Invalid private context version directory")
        for item in files:
            path = target / item["path"]
            if (path.is_symlink() or not path.resolve().is_relative_to(target.resolve())
                    or path.read_bytes() != item["content"].encode()):
                raise RuntimeError("Private context version differs from its cached files")
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".download-", dir=destination))
        try:
            for item in files:
                path = temporary / item["path"]
                path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                path.write_text(item["content"], encoding="utf-8")
                path.chmod(0o700 if path.suffix in (".py", ".sh") else 0o600)
            metadata = temporary / "context.json"
            metadata.write_text(json.dumps(bundle), encoding="utf-8")
            metadata.chmod(0o600)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    link = destination / ".current"
    if link.exists() or link.is_symlink():
        raise RuntimeError("A private context update is already pending")
    link.symlink_to(target.name, target_is_directory=True)
    os.replace(link, destination / "current")
    return bundle


def install_skills(destination):
    current = (destination / "current").resolve()
    skills = ROOT / ".agents/skills"
    if skills.is_symlink():
        raise RuntimeError("Private skill directory must not be a symlink")
    skills.mkdir(parents=True, mode=0o700, exist_ok=True)
    for source in sorted((current / "skills").iterdir()):
        if not source.is_dir() or not (source / "SKILL.md").is_file():
            raise RuntimeError("Invalid private skill directory")
        target = skills / source.name
        if target.exists() and not target.is_symlink():
            tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--", str(target)],
                                     capture_output=True, text=True, check=True)
            if tracked.stdout:
                raise RuntimeError("A private skill would replace tracked files")
            previous = destination / "previous-skills" / source.name
            if previous.parent.is_symlink():
                raise RuntimeError("Private skill backup directory must not be a symlink")
            previous.parent.mkdir(mode=0o700, exist_ok=True)
            if previous.exists() or previous.is_symlink():
                raise RuntimeError("A previous private skill backup already exists")
            os.replace(target, previous)
        temporary = skills / ("." + source.name)
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError("A private skill update is already pending")
        temporary.symlink_to(source, target_is_directory=True)
        os.replace(temporary, target)


def read_state():
    record = request(f"/api/public/v2/prompts/{quote(STATE, safe='')}?label=production&resolve=false")
    state = json.loads(record["prompt"])
    validate_state(state)
    return state


def validate_state(state):
    if (not isinstance(state, dict) or set(state) != {"last_checked", "last_incorporated"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value)
                   for value in state.values())):
        raise RuntimeError("Invalid private assessment state")


def write_state(state):
    validate_state(state)
    request("/api/public/v2/prompts", "POST", {
        "name": STATE, "type": "text", "prompt": json.dumps(state, sort_keys=True),
        "labels": ["production"], "commitMessage": "Update private assessment state",
    })
    if read_state() != state:
        raise RuntimeError("Private assessment state update did not persist")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("fetch")
    command.add_argument("--destination", type=Path, default=ROOT / ".private/agent-context")
    command.add_argument("--install-skills", action="store_true")
    commands.add_parser("state-read")
    commands.add_parser("state-write")
    args = parser.parse_args()
    try:
        if args.command == "fetch":
            bundle = fetch(args.destination)
            if args.install_skills:
                install_skills(args.destination)
            print(f"Private context version {bundle['version']} ready: {(args.destination / 'current').resolve()}")
        elif args.command == "state-read":
            print(json.dumps(read_state()))
        else:
            write_state(json.load(sys.stdin))
    except (RuntimeError, OSError, ValueError, KeyError, TypeError):
        raise SystemExit("Private context failed. Restore Langfuse access or repair the validated cache. Private work must stop.") from None


if __name__ == "__main__":
    main()
