# GitOps

Durable Kubernetes changes belong under `kubernetes/apps/<namespace>/<app>` and must reach Git remote before Flux can apply them.

## Application Workflow

1. Follow an existing application in same namespace.
2. Render each touched Kustomization locally.
3. Run Flux environment substitution for placeholders such as `${SECRET_DOMAIN}`.
4. Validate manifests and inspect rendered namespaces.
5. Test live only after explicit approval and suspend only exact owning Flux Kustomization when required.
6. Verify workloads, routes, logs, and application behavior.
7. Commit and push only when requested.
8. Resume every suspended Kustomization and confirm applied Git revision.

Force Flux to pull current Git state when needed:

```sh
just kube reconcile
```

Inspect current state:

```sh
flux get sources git -A
flux get ks -A
flux get hr -A
```

Never treat a local commit as deployed; Flux can only see pushed revisions.

## Networking

- Attach public routes to `network/envoy-external`.
- Attach private routes to `network/envoy-internal`.
- Internal DNS for `aumer.dev` must resolve through configured cluster DNS path.
- Cilium provides networking, L7 policy support, and Hubble. Envoy Gateway remains Gateway API controller.

Useful checks:

```sh
kubectl -n network get gateway envoy-external envoy-internal
kubectl get httproutes -A
kubectl -n network get deploy envoy-gateway envoy-external envoy-internal
```

After Envoy changes, both Gateways must report `Accepted=True` and `Programmed=True`. Affected HTTPRoutes must report `Accepted=True` and `ResolvedRefs=True`.

## Renovate

Renovate manages dependency updates through `.renovaterc.json5`. Review generated Flux diffs and rollout impact before merging. After an update reaches Git, verify affected HelmRelease, workloads, and application behavior rather than relying only on CI status.

See [maintenance](maintenance.md) for operational checks and troubleshooting.
