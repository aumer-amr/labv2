#!/usr/bin/env python3
"""Exercise route schema validation and isolated Flux substitution."""

from pathlib import Path
import runpy
import shutil
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
validator = runpy.run_path(str(ROOT / "scripts/ci/check-route-schemas.py"))

ROOT_KUSTOMIZATION = """apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: cluster-apps
  namespace: flux-system
spec:
  interval: 1h
  path: ./apps
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
"""
CHILD_KUSTOMIZATION = """apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: route-test
  namespace: flux-system
spec:
  interval: 1h
  path: ./route-test
  targetNamespace: route-space
  patches:
    - target:
        kind: HTTPRoute
      patch: |
        - op: add
          path: /metadata/annotations
          value:
            fixture.example.invalid/patched: "true"
  postBuild:
    substitute:
      ROUTE_TEST_HOST: route.example.invalid
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
"""
ROUTE = """apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: route-test
spec:
  hostnames:
    - ${ROUTE_TEST_HOST}
"""


def main():
    flux = Path(shutil.which("flux")).absolute()
    render_routes = validator["render_routes"]
    validate_routes = validator["validate_routes"]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        root_manifest = root / validator["FLUX_ROOT"]
        root_manifest.parent.mkdir(parents=True)
        root_manifest.write_text(ROOT_KUSTOMIZATION)
        (root / "apps").mkdir()
        (root / "apps/ks.yaml").write_text(CHILD_KUSTOMIZATION)
        (root / "route-test").mkdir()
        (root / "route-test/kustomization.yaml").write_text("resources: [route.yaml]\n")
        route_manifest = root / "route-test/route.yaml"

        route_manifest.write_text(ROUTE)
        with patch.dict("os.environ", {"ROUTE_TEST_HOST": "caller.example.invalid"}):
            routes, = render_routes(root, flux)
        assert "route.example.invalid" in routes, routes
        assert "caller.example.invalid" not in routes, routes
        assert "namespace: route-space" in routes, routes
        assert "fixture.example.invalid/patched:" in routes, routes
        assert validate_routes(routes) == 0

        route_manifest.write_text(ROUTE.replace("hostnames:", "invalidField:"))
        routes, = render_routes(root, flux)
        assert validate_routes(routes) != 0, "Invalid HTTPRoute passed schema validation"

        route_manifest.write_text(ROUTE.replace("ROUTE_TEST_HOST", "ROUTE_TEST_CALLER_ONLY"))
        with patch.dict("os.environ", {"ROUTE_TEST_CALLER_ONLY": "caller.example.invalid"}):
            try:
                list(render_routes(root, flux))
            except SystemExit as error:
                assert str(error) == "Flux substitution failed in ./route-test", error
            else:
                raise AssertionError("Caller environment hid a missing Flux substitution")

    print("Route schema and Flux substitution checks passed")


if __name__ == "__main__":
    main()
