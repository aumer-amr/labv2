#!/usr/bin/env python3
"""Read exact-head OCI publication and pinned image evidence without executing PR code."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib import error, parse, request


MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json"}
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json"}
CONFIG_TYPES = {"application/vnd.oci.image.config.v1+json",
                "application/vnd.docker.container.image.v1+json"}
DIGEST = r"sha256:[0-9a-f]{64}"
REPOSITORY = r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)+"
IMAGE = re.compile(rf"(?P<repository>{REPOSITORY})(?::(?P<tag>[\w][\w.-]{{0,127}}))?@(?P<digest>{DIGEST})", re.ASCII)
SOURCE = re.compile(r"https://github\.com/([A-Za-z0-9_-]+/[A-Za-z0-9_-][A-Za-z0-9_.-]*?)(?:\.git)?/?")
deadline = None


def timeout(maximum):
    remaining = maximum if deadline is None else min(maximum, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("evidence time budget exceeded")
    return remaining


def ghcr_headers(repo):
    token_url = "https://ghcr.io/token?" + parse.urlencode({"service": "ghcr.io", "scope": f"repository:{repo}:pull"})
    token = json.loads(fetch(token_url))["token"]
    if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
        raise ValueError("invalid token")
    return {"Authorization": "Bearer " + token, "Accept": ", ".join(sorted(MANIFEST_TYPES | INDEX_TYPES))}


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an anonymous registry token to another host.


def fetch(url, headers=None):
    with request.build_opener(NoRedirect).open(
        request.Request(url, headers=headers or {}), timeout=timeout(10)
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
        headers = ghcr_headers(repo)
        manifest_request = True
        body = fetch(f"https://ghcr.io/v2/{repo}/manifests/{tag}", headers)
        manifest = json.loads(body)
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2 or not (manifest.get("config") or manifest.get("manifests")):
            raise ValueError("invalid manifest")
        result.update(status="published", digest="sha256:" + hashlib.sha256(body).hexdigest())
    except error.HTTPError as exc:
        result.update(status="not_found" if manifest_request and exc.code == 404 else "unknown", reason=f"registry HTTP {exc.code}")
    except (OSError, ValueError, KeyError, TypeError):
        result["reason"] = "registry verification unavailable"
    return result


def registry_json(repo, kind, digest, headers, size=None):
    if not isinstance(digest, str) or not re.fullmatch(DIGEST, digest):
        raise ValueError("invalid digest")
    if size is not None and (type(size) is not int or not 0 < size <= 1024 * 1024):
        raise ValueError("invalid descriptor size")
    try:
        body = fetch(f"https://ghcr.io/v2/{repo}/{kind}/{digest}", headers)
    except error.HTTPError as exc:
        # GHCR serves config blobs through this CDN. Never forward the registry token.
        location = exc.headers.get("Location", "")
        target = parse.urlsplit(location)
        if (kind != "blobs" or exc.code not in {302, 307} or target.scheme != "https"
                or target.netloc != "pkg-containers.githubusercontent.com" or target.fragment):
            raise
        body = fetch(location)
    if "sha256:" + hashlib.sha256(body).hexdigest() != digest or (size is not None and len(body) != size):
        raise ValueError("digest or size mismatch")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("invalid registry object")
    return value


def platform_name(platform):
    if not isinstance(platform, dict):
        raise ValueError("missing platform")
    parts = [platform.get("os"), platform.get("architecture")]
    if platform.get("variant"):
        parts.append(platform["variant"])
    if not all(isinstance(p, str) and re.fullmatch(r"[a-z0-9_-]{1,32}", p) for p in parts):
        raise ValueError("invalid platform")
    return "/".join(parts)


def release_evidence(source, tag):
    """Public requests only: never forward the private repository's GitHub token."""
    match = SOURCE.fullmatch(source or "")
    if not match or not re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", tag or ""):
        return {"status": "unknown", "reason": "requires GitHub source and a versioned image tag"}
    repo = match[1]
    root = f"https://api.github.com/repos/{repo}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "labv2-image-evidence"}
    try:
        # A leading v is the only normalization; image flavor suffixes are not stripped.
        for candidate in (tag, tag[1:] if tag.startswith("v") else "v" + tag):
            try:
                release = json.loads(fetch(f"{root}/releases/tags/{parse.quote(candidate, safe='')}", headers))
                break
            except error.HTTPError as exc:
                if exc.code != 404:
                    raise
        else:
            return {"status": "unknown", "reason": "upstream release not found"}
        if release.get("tag_name") != candidate or release.get("draft") is not False:
            raise ValueError("invalid release")
        obj = json.loads(fetch(f"{root}/git/ref/tags/{parse.quote(candidate, safe='')}", headers))["object"]
        for _ in range(3):
            if not isinstance(obj, dict) or not re.fullmatch(r"[0-9a-f]{40}", obj.get("sha", "")):
                raise ValueError("invalid tag object")
            if obj.get("type") == "commit":
                return {"status": "available", "source": f"https://github.com/{repo}",
                        "tag": candidate, "revision": obj["sha"]}
            if obj.get("type") != "tag":
                raise ValueError("unsupported tag object")
            obj = json.loads(fetch(f"{root}/git/tags/{obj['sha']}", headers))["object"]
        return {"status": "unknown", "reason": "upstream tag nesting limit exceeded"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"status": "unknown", "reason": "upstream release verification unavailable"}


def compare_release(labels, release):
    result = {}
    for key in ("version", "revision"):
        actual = labels.get(key)
        if not actual:
            result[key] = "missing_metadata"
        elif release["status"] != "available":
            result[key] = "unknown"
        elif key == "version":
            result[key] = "match" if actual.removeprefix("v") == release["tag"].removeprefix("v") else "mismatch"
        else:
            result[key] = "match" if release["revision"].startswith(actual.lower()) else "mismatch"
    return result


def image_config(repo, manifest, digest, headers, expected=None):
    if manifest.get("schemaVersion") != 2 or manifest.get("mediaType") not in MANIFEST_TYPES:
        raise ValueError("unsupported image manifest")
    descriptor = manifest["config"]
    if descriptor["mediaType"] not in CONFIG_TYPES or type(descriptor["size"]) is not int:
        raise ValueError("not a container image config")
    config = registry_json(repo, "blobs", descriptor["digest"], headers, descriptor["size"])
    platform = platform_name(config)
    if expected:
        if any(config.get(k) != expected.get(k) for k in ("os", "architecture")):
            raise ValueError("platform mismatch")
        if config.get("variant") and config["variant"] != expected.get("variant"):
            raise ValueError("platform variant mismatch")
        platform = platform_name(expected)
    raw = (config.get("config") or {}).get("Labels") or {}
    if not isinstance(raw, dict):
        raise ValueError("invalid labels")
    labels = {}
    patterns = {"version": r"[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}",
                "revision": r"[0-9a-fA-F]{7,40}", "source": SOURCE.pattern}
    for key, pattern in patterns.items():
        value = raw.get("org.opencontainers.image." + key)
        if isinstance(value, str) and len(value) <= 256 and re.fullmatch(pattern, value):
            labels[key] = value
    missing = [key for key in patterns if key not in labels]
    return {"platform": platform, "manifest_digest": digest, "config_digest": descriptor["digest"],
            "status": "missing_metadata" if missing else "available", "labels": labels,
            "missing_or_unusable_labels": missing}


def inspect_image(reference):
    match = IMAGE.fullmatch(reference) if isinstance(reference, str) and len(reference) <= 512 else None
    if not match:
        return {"status": "unknown", "reason": "requires a supported sha256-pinned image reference"}
    result = {"image": reference, "status": "unknown", "platforms": []}
    if not match["repository"].startswith("ghcr.io/"):
        return {**result, "reason": "unsupported registry; public GHCR only"}
    repo = match["repository"].removeprefix("ghcr.io/")
    releases = {}
    try:
        headers = ghcr_headers(repo)
        manifest = registry_json(repo, "manifests", match["digest"], headers)
        if manifest.get("mediaType") in INDEX_TYPES and manifest.get("schemaVersion") == 2:
            descriptors = manifest["manifests"]
            if not isinstance(descriptors, list):
                raise ValueError("invalid image index")
            # BuildKit attestations are not runnable platform images.
            descriptors = [d for d in descriptors if d.get("annotations", {}).get(
                "vnd.docker.reference.type") != "attestation-manifest"]
            if not 0 < len(descriptors) <= 4:
                return {**result, "reason": "image platform budget exceeded or empty index"}
            for descriptor in descriptors:
                platform_name(descriptor.get("platform"))
                if descriptor["mediaType"] not in MANIFEST_TYPES or type(descriptor["size"]) is not int:
                    raise ValueError("unsupported nested image index")
                child = registry_json(repo, "manifests", descriptor["digest"], headers, descriptor["size"])
                result["platforms"].append(image_config(repo, child, descriptor["digest"], headers, descriptor["platform"]))
        else:
            result["platforms"].append(image_config(repo, manifest, match["digest"], headers))
        for entry in result["platforms"]:
            source = entry["labels"].get("source")
            if source not in releases:
                releases[source] = release_evidence(source, match["tag"])
            entry["release"] = releases[source]
            entry["comparison"] = compare_release(entry["labels"], releases[source])
        result["status"] = "available"
    except error.HTTPError as exc:
        result["reason"] = f"registry evidence unavailable (HTTP {exc.code})"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        result["reason"] = "registry evidence unavailable or invalid digest/platform data"
    return result


def image_references(value):
    """Only image fields, including app-template repository/tag/digest maps; never config text."""
    found = set()
    if isinstance(value, dict):
        image = value.get("image")
        if isinstance(image, str):
            found.add(image)
        elif isinstance(image, dict) and isinstance(image.get("repository"), str):
            reference = image["repository"]
            if isinstance(image.get("tag"), (str, int, float)):
                reference += ":" + str(image["tag"])
            if isinstance(image.get("digest"), str) and image["digest"]:
                reference = reference.split("@", 1)[0] + "@" + image["digest"]
            found.add(reference)
        for child in value.values():
            found.update(image_references(child))
    elif isinstance(value, list):
        for child in value:
            found.update(image_references(child))
    return found


def gh(endpoint):
    return subprocess.check_output(["gh", "api", endpoint], text=True, timeout=timeout(20), stderr=subprocess.DEVNULL)


def read_docs(repo, path, ref):
    if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise ValueError("invalid source SHA")
    content = json.loads(gh(f"repos/{repo}/contents/{parse.quote(path, safe='/')}?ref={ref}"))
    raw = base64.b64decode(content["content"], validate=False)
    if len(raw) > 1024 * 1024:
        raise ValueError("manifest file too large")
    parsed = subprocess.check_output(["yq", "eval-all", "-o=json", "[.]", "-"], input=raw,
                                     timeout=timeout(10), stderr=subprocess.DEVNULL)
    return json.loads(parsed)


def collect(event):
    global deadline
    deadline = time.monotonic() + 150
    pr = event["pull_request"]
    repo = event["repository"]["full_name"]
    head = pr["head"]["sha"]
    if pr["head"]["repo"]["full_name"] != repo:
        return {"head_sha": head, "status": "unknown", "reason": "fork evidence disabled"}
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("invalid head SHA")
    results, images = [], []
    seen_images = set()
    visited = 0
    # Bound API work; unvisited files are explicitly reported, never silently certified.
    files = json.loads(gh(f"repos/{repo}/pulls/{int(pr['number'])}/files?per_page=100"))
    for changed in files:
        path = changed["filename"]
        if changed["status"] == "removed" or not path.startswith("kubernetes/") or not path.endswith((".yaml", ".yml")):
            continue
        is_oci = path.endswith("ocirepository.yaml")
        if not is_oci:
            visited += 1
            if visited > 10:
                if visited == 11:
                    images.append({"status": "unknown", "reason": "YAML file budget exceeded"})
                continue
        if is_oci and len(results) >= 5:
            if len(results) == 5:
                results.append({"status": "unknown", "reason": "OCI file budget exceeded"})
            continue
        try:
            docs = read_docs(repo, path, head)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            (results if path.endswith("ocirepository.yaml") else images).append(
                {"file": path, "status": "unknown", "reason": "exact-head YAML unavailable"})
            continue
        for doc in docs:
            if is_oci and isinstance(doc, dict) and doc.get("kind") == "OCIRepository":
                results.append({"file": path, **verify(doc.get("spec") or {})})
        references = image_references(docs)
        if references and changed["status"] != "added":
            try:
                base_docs = read_docs(repo, changed.get("previous_filename", path), pr.get("base", {}).get("sha"))
                references -= image_references(base_docs)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                images.append({"file": path, "status": "unknown", "reason": "base image comparison unavailable"})
                continue
        for reference in sorted(references - seen_images):
            if len(seen_images) >= 3:
                images.append({"status": "unknown", "reason": "image budget exceeded"})
                break
            seen_images.add(reference)
            images.append({"file": path, **inspect_image(reference)})
    if len(files) == 100:
        results.append({"status": "unknown", "reason": "changed-file listing capped at 100"})
    return {"head_sha": head, "publication": results, "images": images,
            "scope": "Publication proves existence only. Image evidence covers changed literal image fields in YAML, "
                     "including repository/tag/digest maps, public GHCR, sha256 and up to four platforms per image. "
                     "Match platform evidence to workload scheduling using Konflate; no runner-platform assumption. "
                     "GitHub release comparison requires a versioned image tag and a usable source label. "
                     "Labels are publisher claims, not signed provenance or runtime compatibility proof."}


if __name__ == "__main__":
    try:
        evidence = collect(json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        evidence = {"status": "unknown", "reason": "OCI evidence collection unavailable"}
    print(json.dumps(evidence))
