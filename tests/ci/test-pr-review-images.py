#!/usr/bin/env python3
"""Pinned image evidence regressions; mock external I/O, retain real YAML parsing."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError


spec = importlib.util.spec_from_file_location("oci", Path(__file__).resolve().parents[2] / "scripts/ci/pr-review-oci.py")
oci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oci)
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
REVISION = "a" * 40
LABELS = {"org.opencontainers.image.version": "1.2.3",
          "org.opencontainers.image.revision": REVISION,
          "org.opencontainers.image.source": "https://github.com/org/app"}
ROOT = "https://ghcr.io/v2/org/app"
UPSTREAM = "https://api.github.com/repos/org/app"


def blob(value, media_type):
    body = json.dumps(value).encode()
    return body, {"digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                  "size": len(body), "mediaType": media_type}


def fixture(platform=None, labels=None):
    platform = platform or {"os": "linux", "architecture": "amd64"}
    config, descriptor = blob({**platform, "config": {"Labels": LABELS if labels is None else labels,
                                                    "Env": ["PRIVATE=dummy-do-not-output"]}}, CONFIG)
    manifest, pin = blob({"schemaVersion": 2, "mediaType": MANIFEST, "config": descriptor, "layers": []}, MANIFEST)
    return {f"{ROOT}/blobs/{descriptor['digest']}": config,
            f"{ROOT}/manifests/{pin['digest']}": manifest}, pin


class Images(unittest.TestCase):
    def setUp(self):
        oci.deadline = None
        self.calls = []

    def inspect(self, data, pin, tag="1.2.3", release_revision=REVISION):
        def fetch(url, headers=None):
            self.calls.append((url, headers))
            if url.startswith("https://ghcr.io/token?"):
                self.assertIsNone(headers)
                return b'{"token":"dummy-registry-token"}'
            if url.startswith(ROOT):
                self.assertEqual(headers["Authorization"], "Bearer dummy-registry-token")
                value = data[url]
                if isinstance(value, Exception):
                    raise value
                return value
            self.assertTrue(url.startswith(UPSTREAM + "/"), url)
            self.assertNotIn("Authorization", headers)
            if url in data:
                value = data[url]
                if isinstance(value, Exception):
                    raise value
                return value
            if "/releases/tags/" in url:
                return json.dumps({"tag_name": tag, "draft": False}).encode()
            return json.dumps({"object": {"type": "commit", "sha": release_revision}}).encode()
        with patch.object(oci, "fetch", side_effect=fetch):
            result = oci.inspect_image(f"ghcr.io/org/app:{tag}@{pin['digest']}")
        self.assertNotIn("dummy-registry-token", json.dumps(result))
        self.assertNotIn("dummy-do-not-output", json.dumps(result))
        return result

    def test_exact_digest_and_release_match(self):
        data, pin = fixture()
        result = self.inspect(data, pin)
        self.assertEqual(result["status"], "available")
        entry = result["platforms"][0]
        self.assertEqual(entry["platform"], "linux/amd64")
        self.assertEqual(entry["manifest_digest"], pin["digest"])
        self.assertEqual(entry["comparison"], {"version": "match", "revision": "match"})
        self.assertFalse(any("/manifests/1.2.3" in url for url, _ in self.calls))

    def test_mismatch_missing_and_unusable_labels(self):
        cases = [
            ({**LABELS, "org.opencontainers.image.version": "9.9.9",
              "org.opencontainers.image.revision": "b" * 40}, "available", "mismatch", "mismatch"),
            ({}, "missing_metadata", "missing_metadata", "missing_metadata"),
            ({**LABELS, "org.opencontainers.image.revision": "short"}, "missing_metadata", "match", "missing_metadata"),
            ({**LABELS, "org.opencontainers.image.version": 123}, "missing_metadata", "missing_metadata", "match"),
            ({**LABELS, "org.opencontainers.image.version": "v1.2.3",
              "org.opencontainers.image.revision": "A" * 7}, "available", "match", "match"),
        ]
        for labels, status, version, revision in cases:
            with self.subTest(labels=labels):
                data, pin = fixture(labels=labels)
                entry = self.inspect(data, pin)["platforms"][0]
                self.assertEqual(entry["status"], status)
                self.assertEqual(entry["comparison"], {"version": version, "revision": revision})

    def test_index_platforms_variants_and_attestations(self):
        data, amd = fixture()
        arm_platform = {"os": "linux", "architecture": "arm64", "variant": "v8"}
        arm_data, arm = fixture(arm_platform, {**LABELS, "org.opencontainers.image.version": "9.9.9"})
        data.update(arm_data)
        index, pin = blob({"schemaVersion": 2, "mediaType": INDEX, "manifests": [
            {**arm, "platform": arm_platform},
            {**amd, "platform": {"os": "linux", "architecture": "amd64"}},
            {"annotations": {"vnd.docker.reference.type": "attestation-manifest"},
             "platform": {"os": "unknown", "architecture": "unknown"}},
        ]}, INDEX)
        data[f"{ROOT}/manifests/{pin['digest']}"] = index
        result = self.inspect(data, pin)
        self.assertEqual(result["status"], "available")
        self.assertEqual([e["platform"] for e in result["platforms"]], ["linux/arm64/v8", "linux/amd64"])
        self.assertEqual([e["comparison"]["version"] for e in result["platforms"]], ["mismatch", "match"])
        self.assertEqual(sum("/releases/tags/" in url for url, _ in self.calls), 1)

    def test_platform_mismatch_and_unsupported_indices(self):
        for platform, media, count in [({"os": "linux", "architecture": "arm64"}, MANIFEST, 1),
                                      ({}, MANIFEST, 1),
                                      ({"os": "linux", "architecture": "amd64"}, INDEX, 1),
                                      ({"os": "linux", "architecture": "amd64"}, MANIFEST, 5)]:
            with self.subTest(platform=platform, media=media, count=count):
                data, child = fixture()
                index, pin = blob({"schemaVersion": 2, "mediaType": INDEX, "manifests": [
                    {**child, "platform": platform, "mediaType": media}] * count}, INDEX)
                data[f"{ROOT}/manifests/{pin['digest']}"] = index
                self.assertEqual(self.inspect(data, pin)["status"], "unknown")

    def test_config_and_manifest_integrity(self):
        for kind in ("blobs", "manifests"):
            data, pin = fixture()
            url = next(url for url in data if f"/{kind}/" in url)
            data[url] += b" "
            result = self.inspect(data, pin)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["platforms"], [])
        for key, value in [("size", 1), ("size", None), ("mediaType", "application/vnd.cncf.helm.config.v1+json"),
                           ("digest", "https://evil.test/credentials")]:
            data, pin = fixture()
            manifest = json.loads(data[f"{ROOT}/manifests/{pin['digest']}"])
            manifest["config"][key] = value
            body, pin = blob(manifest, MANIFEST)
            data[f"{ROOT}/manifests/{pin['digest']}"] = body
            self.assertEqual(self.inspect(data, pin)["status"], "unknown")

    def test_registry_unavailable(self):
        failures = [HTTPError("", code, "private-error", {}, None) for code in (302, 401, 403, 404, 429, 503)]
        failures += [URLError("private-error"), TimeoutError("private-error")]
        for failure in failures:
            data, pin = fixture()
            data[f"{ROOT}/manifests/{pin['digest']}"] = failure
            result = self.inspect(data, pin)
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn("private-error", json.dumps(result))

    def test_release_missing_unavailable_and_annotated_tag(self):
        for code in (404, 403, 429):
            data, pin = fixture()
            for tag in ("1.2.3", "v1.2.3"):
                data[f"{UPSTREAM}/releases/tags/{tag}"] = HTTPError("", code, "private-error", {}, None)
            entry = self.inspect(data, pin)["platforms"][0]
            self.assertEqual(entry["status"], "available")
            self.assertEqual(entry["comparison"], {"version": "unknown", "revision": "unknown"})
        data, pin = fixture()
        data[f"{UPSTREAM}/releases/tags/1.2.3"] = HTTPError("", 404, "", {}, None)
        data[f"{UPSTREAM}/releases/tags/v1.2.3"] = b'{"tag_name":"v1.2.3","draft":false}'
        data[f"{UPSTREAM}/git/ref/tags/v1.2.3"] = json.dumps({"object": {"type": "tag", "sha": "b" * 40}}).encode()
        entry = self.inspect(data, pin)["platforms"][0]
        self.assertEqual(entry["release"]["revision"], REVISION)
        self.assertEqual(entry["comparison"], {"version": "match", "revision": "match"})
        self.assertTrue(any("/git/tags/" in url for url, _ in self.calls))

    def test_unsupported_refs_and_release_sources_do_not_fetch(self):
        with patch.object(oci, "fetch") as fetch:
            for ref in ["ghcr.io/org/app:latest", "ghcr.io/org/app@sha256:short",
                        "http://127.0.0.1/image", "ghcr.io/org/../app@sha256:" + "a" * 64,
                        "ghcr.io/user:password/app@sha256:" + "a" * 64,
                        "docker.io/library/python:3.15@sha256:" + "a" * 64]:
                self.assertEqual(oci.inspect_image(ref)["status"], "unknown")
            for source in ["https://evil.test/org/app", "https://github.com/org/..", "https://github.com.evil.test/org/app"]:
                self.assertEqual(oci.release_evidence(source, "1.2.3")["status"], "unknown")
            self.assertEqual(oci.release_evidence("https://github.com/org/app", "latest")["status"], "unknown")
            fetch.assert_not_called()

    def test_response_and_time_budgets(self):
        with patch.object(oci.request, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b"x" * (1024 * 1024 + 1)
            with self.assertRaises(ValueError):
                oci.fetch("https://ghcr.io/token")
        oci.deadline = 0
        with patch.object(oci.request, "build_opener") as opener:
            with self.assertRaises(TimeoutError):
                oci.fetch("https://ghcr.io/token")
            opener.return_value.open.assert_not_called()

    def test_config_cdn_redirect_drops_credentials_and_verifies_digest(self):
        body, descriptor = blob({"architecture": "amd64", "os": "linux"}, CONFIG)
        cdn = "https://pkg-containers.githubusercontent.com/blob?signature=dummy-signed-value"
        redirect = HTTPError("", 307, "", {"Location": cdn}, None)
        with patch.object(oci, "fetch", side_effect=[redirect, body]) as fetch:
            oci.registry_json("org/app", "blobs", descriptor["digest"], {"Authorization": "Bearer dummy"}, descriptor["size"])
            self.assertEqual(fetch.call_args.args, (cdn,))
        with patch.object(oci, "fetch", side_effect=[redirect, body + b" "]):
            with self.assertRaises(ValueError):
                oci.registry_json("org/app", "blobs", descriptor["digest"], {}, descriptor["size"])
        for location, kind in [("https://evil.test/blob", "blobs"),
                               ("https://pkg-containers.githubusercontent.com.evil.test/blob", "blobs"),
                               ("https://user@pkg-containers.githubusercontent.com/blob", "blobs"),
                               ("http://pkg-containers.githubusercontent.com/blob", "blobs"),
                               ("https://pkg-containers.githubusercontent.com:8443/blob", "blobs"),
                               (cdn, "manifests")]:
            with patch.object(oci, "fetch", side_effect=HTTPError("", 307, "", {"Location": location}, None)) as fetch:
                with self.assertRaises(HTTPError):
                    oci.registry_json("org/app", kind, descriptor["digest"], {})
                self.assertEqual(fetch.call_count, 1)


class Collection(unittest.TestCase):
    def setUp(self):
        self.head, self.base = "a" * 40, "b" * 40
        self.path = "kubernetes/apps/test/app/helmrelease.yaml"
        self.event = {"repository": {"full_name": "owner/repo"}, "pull_request": {
            "number": 1, "head": {"sha": self.head, "repo": {"full_name": "owner/repo"}},
            "base": {"sha": self.base}}}
        self.old = "ghcr.io/org/app:1.2.2@sha256:" + "1" * 64
        self.new = "ghcr.io/org/app:1.2.3@sha256:" + "2" * 64

    def collect(self, current, previous, files=None):
        def gh(endpoint):
            if "/pulls/" in endpoint:
                return json.dumps(files or [{"filename": self.path, "status": "modified"}])
            self.assertTrue(endpoint.endswith("?ref=" + self.head) or endpoint.endswith("?ref=" + self.base))
            value = current if endpoint.endswith(self.head) else previous
            return json.dumps({"content": base64.b64encode(value.encode()).decode()})
        with patch.object(oci, "gh", side_effect=gh), patch.object(oci, "inspect_image", return_value={"status": "fixture"}) as inspect:
            result = oci.collect(self.event)
        return result, [call.args[0] for call in inspect.call_args_list]

    def test_exact_head_base_and_common_image_shapes(self):
        current = f"""spec:
  image: {self.new}
  containers:
    app:
      image:
        repository: ghcr.io/org/app
        tag: 1.2.3@sha256:{'3' * 64}
        digest: sha256:{'2' * 64}
    unchanged:
      image: {self.old}
  config: 'image: ghcr.io/private/secret'
"""
        result, refs = self.collect(current, f"image: {self.old}")
        self.assertEqual(refs, [self.new])
        self.assertEqual(result["head_sha"], self.head)
        self.assertEqual(len(result["images"]), 1)

    def test_renamed_added_removed_and_unavailable_base(self):
        for status in ("renamed", "added", "removed"):
            files = [{"filename": self.path, "status": status, "previous_filename": "kubernetes/old.yaml"}]
            result, refs = self.collect(f"image: {self.new}", f"image: {self.old}", files)
            self.assertEqual(refs, [] if status == "removed" else [self.new])
        del self.event["pull_request"]["base"]
        result, refs = self.collect(f"image: {self.new}", "")
        self.assertEqual(refs, [])
        self.assertEqual(result["images"][0]["reason"], "base image comparison unavailable")

    def test_image_budget_and_fork_boundary(self):
        result, refs = self.collect("\n".join(f"- image: ghcr.io/org/app:{i}@sha256:{'2' * 64}" for i in range(5)), "{}")
        self.assertEqual(len(refs), 3)
        self.assertEqual(result["images"][-1]["reason"], "image budget exceeded")
        self.event["pull_request"]["head"]["repo"]["full_name"] = "fork/repo"
        with patch.object(oci, "gh") as gh:
            self.assertEqual(oci.collect(self.event)["reason"], "fork evidence disabled")
            gh.assert_not_called()

    def test_file_budget_and_listing_limit_are_explicit(self):
        files = [{"filename": f"kubernetes/apps/test/{i}.yaml", "status": "added"} for i in range(100)]
        result, refs = self.collect("{}", "{}", files)
        self.assertEqual(refs, [])
        self.assertEqual(result["images"][-1]["reason"], "YAML file budget exceeded")
        self.assertEqual(result["publication"][-1]["reason"], "changed-file listing capped at 100")

    def test_image_file_budget_does_not_suppress_publication(self):
        files = [{"filename": f"kubernetes/apps/test/{i}.yaml", "status": "added"} for i in range(11)]
        files.append({"filename": "kubernetes/apps/test/ocirepository.yaml", "status": "added"})
        with patch.object(oci, "verify", return_value={"status": "published"}):
            result, _ = self.collect("kind: OCIRepository\nspec: {}", "{}", files)
        self.assertTrue(any(e["file"].endswith("ocirepository.yaml") and e["status"] == "published"
                            for e in result["publication"]))


if __name__ == "__main__":
    unittest.main()
