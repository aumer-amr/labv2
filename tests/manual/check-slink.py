#!/usr/bin/env python3
"""Exercise Slink's route boundary and deployment contract with local YAML tools."""

import json
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "kubernetes/apps/developer-tools/slink/app"
ID = "019a55b1-0c38-7000-8000-000000000001"


def documents(path):
    result = subprocess.check_output(
        ["yq", "ea", "-o=json", "-I=0", ".", str(path)], text=True
    )
    return [json.loads(line) for line in result.splitlines() if line != "null"]


def permits(route, method, path):
    for rule in route["spec"]["rules"]:
        for match in rule.get("matches", [{}]):
            if match.get("method", method) != method:
                continue
            pattern = match.get("path", {"type": "PathPrefix", "value": "/"})
            value = pattern["value"]
            if pattern["type"] == "Exact" and path == value:
                return True
            if pattern["type"] == "RegularExpression" and re.fullmatch(value, path):
                return True
            if pattern["type"] == "PathPrefix" and (
                path == value or path.startswith(value.rstrip("/") + "/")
            ):
                return True
    return False


class SlinkContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.internal, cls.external = documents(APP / "httproute.yaml")
        cls.values = documents(APP / "helmrelease.yaml")[0]["spec"]["values"]

    def test_public_shares(self):
        for method, path in [
            ("GET", "/i/Abcd1234"),
            ("HEAD", "/i/Abcd1234"),
            ("GET", f"/image/{ID}.png"),
            ("HEAD", f"/image/{ID}.png"),
            ("GET", f"/share/locked/{ID}"),
            ("GET", f"/share/locked/{ID}/__data.json"),
            ("GET", "/image/unavailable"),
            ("GET", "/_app/immutable/entry/start.js"),
            ("GET", "/icons/icon-192x192.png"),
            ("GET", "/favicon.svg"),
            ("POST", f"/api/share/{ID}/unlock"),
        ]:
            with self.subTest(method=method, path=path):
                self.assertTrue(permits(self.external, method, path))

    def test_public_management_and_uploads_are_blocked(self):
        paths = [
            "/", "/history", "/admin", "/admin/settings", "/profile/login",
            "/profile/signup", "/upload", "/integrations", "/preferences",
            "/profile/sso/login/kanidm", "/profile/sso/callback",
            "/api/auth/sso/providers", "/api/auth/sso/authorize", "/api/auth/sso/token",
            "/api/health", "/api/upload", "/api/upload/chunked",
            "/api/external/upload", "/api/auth/login", "/api/auth/signup",
            "/api/user/api-keys", "/api/settings", "/api/images/history",
            "/api/images", "/api/shares", "/sse", "/api/share/not-a-share/unlock",
            f"/api/share/{ID}/publish", f"/api/share/{ID}/password",
            f"/image/{ID}/detail", f"/image/{ID}.png/extra",
            "/_app-not-static/admin", "/icons-admin", "/i/Abcd1234/upload",
        ]
        for method in ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]:
            for path in paths:
                with self.subTest(method=method, path=path):
                    self.assertFalse(permits(self.external, method, path))
                    self.assertTrue(permits(self.internal, method, path))
        for method in ["POST", "PUT", "PATCH", "DELETE"]:
            self.assertFalse(permits(self.external, method, f"/image/{ID}.png"))
        self.assertFalse(permits(self.external, "GET", f"/api/share/{ID}/unlock"))

    def test_split_dns_and_gateways(self):
        for route, gateway in [(self.internal, "envoy-internal"), (self.external, "envoy-external")]:
            self.assertEqual(route["spec"]["hostnames"], ["screenshots.aumer.dev"])
            self.assertEqual(route["spec"]["parentRefs"], [{"name": gateway, "namespace": "network"}])
            for rule in route["spec"]["rules"]:
                self.assertEqual(rule["backendRefs"], [{"name": "slink", "port": 3000}])
        annotations = self.external["metadata"]["annotations"]
        self.assertEqual(self.external["metadata"]["labels"]["k8s-gateway.dns/ignore"], "true")
        self.assertEqual(annotations["external-dns.kubernetes.io/gateway-hostname-source"], "annotation-only")
        self.assertNotIn("external-dns.kubernetes.io/hostname", annotations)
        self.assertNotIn("external-dns.alpha.kubernetes.io/hostname", annotations)
        endpoint = documents(APP / "dnsendpoint.yaml")[0]["spec"]["endpoints"]
        self.assertEqual(endpoint, [{"dnsName": "screenshots.aumer.dev", "recordType": "CNAME", "targets": ["external.aumer.dev"]}])

    def test_storage_and_non_root_startup(self):
        pod = self.values["defaultPodOptions"]
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["securityContext"]["runAsUser"], 1000)
        self.assertFalse(pod["automountServiceAccountToken"])
        controller = self.values["controllers"]["slink"]
        self.assertEqual(controller["strategy"], "Recreate")
        self.assertEqual(controller["replicas"], 1)
        app = controller["containers"]["app"]
        self.assertIn("@sha256:", app["image"]["tag"])
        self.assertFalse(app["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(app["securityContext"]["capabilities"], {"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]})
        for key in ["USER_ALLOW_REGISTRATION", "IMAGE_ALLOW_GUEST_UPLOADS", "USER_REQUIRE_AUTH_FOR_MEDIA_SHARES"]:
            self.assertEqual(app["env"][key], "false")
        self.assertEqual(app["env"]["STORAGE_PROVIDER"], "s3")
        self.assertEqual(app["env"]["AMAZON_S3_BUCKET"], "pg-slink")
        self.assertEqual(app["envFrom"], [{"secretRef": {"name": "slink-secret"}}])
        secret = documents(APP / "externalsecret.yaml")[0]["spec"]
        fields = {entry["secretKey"]: entry["remoteRef"] for entry in secret["data"]}
        self.assertEqual(fields["AMAZON_S3_ACCESS_KEY_ID"], {"key": "slink", "property": "R2_ACCESS_KEY_ID"})
        self.assertEqual(fields["AMAZON_S3_SECRET_ACCESS_KEY"], {"key": "slink", "property": "R2_SECRET_ACCESS_KEY"})
        mounts = self.values["persistence"]["data"]
        self.assertEqual(mounts["existingClaim"], "slink")
        self.assertEqual({m["path"] for m in mounts["globalMounts"]}, {"/app/var/data", "/data"})
        init = controller["initContainers"]["runtime-directories"]
        self.assertEqual(init["command"], ["/bin/sh", "-ec", "mkdir -p /data/redis /data/caddy"])

    def test_r2_dns_and_https_boundary(self):
        policies = documents(APP / "networkpolicy.yaml")
        rules = next(p for p in policies if p["kind"] == "CiliumNetworkPolicy")["spec"]["egress"]
        host = "${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
        names = [host] + [host + "." + suffix for suffix in [
            "developer-tools.svc.cluster.local", "svc.cluster.local", "cluster.local",
        ]]
        names += ["idm.aumer.dev"] + ["idm.aumer.dev." + suffix for suffix in [
            "developer-tools.svc.cluster.local", "svc.cluster.local", "cluster.local",
        ]]
        for rule, protocol in zip(rules[:2], ["UDP", "TCP"]):
            self.assertEqual(rule["toEndpoints"], [{"matchLabels": {
                "k8s:io.kubernetes.pod.namespace": "kube-system", "k8s:k8s-app": "kube-dns",
            }}])
            self.assertEqual(rule["toPorts"], [{"ports": [{"port": "53", "protocol": protocol}],
                "rules": {"dns": [{"matchName": name} for name in names]}}])
        self.assertEqual(rules[2], {"toFQDNs": [{"matchName": host}],
            "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]})
        self.assertEqual(rules[3], {"toFQDNs": [{"matchName": "idm.aumer.dev"}],
            "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]})
        self.assertEqual(len(rules), 4)

    def test_kanidm_secret_and_gateway_path(self):
        self.assertEqual(self.values["defaultPodOptions"]["labels"], {
            "network.home.arpa/to-envoy-external": "allow",
        })
        secret = documents(APP / "externalsecret.yaml")[0]["spec"]
        field = next(f for f in secret["data"] if f["secretKey"] == "KANIDM_CLIENT_SECRET")
        self.assertEqual(field["remoteRef"], {"key": "slink", "property": "KANIDM_CLIENT_SECRET"})
        dependencies = documents(APP.parent / "ks.yaml")[0]["spec"]["dependsOn"]
        self.assertIn({"name": "kanidm", "namespace": "security"}, dependencies)


if __name__ == "__main__":
    unittest.main()
