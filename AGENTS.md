# Repository Instructions

## Environment

- Run every repository and cluster command in WSL Bash. Never use PowerShell for this repository.
- Start commands from `/mnt/e/code/private/labv2`.
- Run repository tools through Mise, for example `mise exec -- kubectl ...`.
- Use the repository kubeconfig configured by `.mise/config.toml`. The cluster is remote; never assume a local Kubernetes context.
- Never print, decode, log, or quote secret values. Files such as `age.key`, `kubeconfig`, `deploy.key`, and provider credentials are sensitive.

## Authorization

- Inspecting files and cluster state is allowed when relevant.
- Before any live state change, obtain explicit permission unless the user's current message already authorizes that exact action and target. This includes apply, patch, annotate, scale, restart, delete, suspend, resume, and forced reconcile operations.
- Never commit or push unless explicitly requested. A request to commit does not authorize a push.
- Never delete or recreate a resource to fix it without explicit permission and a verified recovery path.

## GitOps Workflow

1. Inspect the owning Flux `Kustomization`, existing resource, related events, controller status, and established repository pattern.
2. Make the smallest repository change first. Git is the desired-state source; live patches are temporary tests.
3. Render locally with Kustomize and then run Flux-compatible environment substitution. Use `flux envsubst --strict`, not plain GNU `envsubst`, because manifests use Flux substitutions such as `${SECRET_DOMAIN/./-}`.
4. Inspect rendered kinds, names, and namespaces before any apply. Always pass the explicit namespace for namespaced resources; never rely on the current or `default` namespace.
5. Run local validation before requesting or performing a live update.
6. For a live test of Flux-owned resources, suspend only the exact owning child `Kustomization` after permission. Record every suspended object.
7. Apply only the rendered resources in scope. Do not apply an app tree or Helm release into `default`.
8. Verify resource conditions, workload availability, routes, logs, and application behavior before committing.
9. Resume recorded Flux objects only after permission. Resume `network/envoy-gateway` first and wait for `Ready=True` before dependents.
10. Confirm Flux applied the expected remote Git revision. A local commit is invisible to Flux until pushed.

Do not leave resources suspended without reporting them. After testing, report local changes, live-only changes, suspended resources, validation results, and anything still unverified.

## Validation

Run at minimum:

```bash
git diff --check
```

- Run `oxfmt --check` on changed YAML, JSON, and Markdown files.
- Build every touched Kustomize directory with `--load-restrictor=LoadRestrictionsNone`.
- Before live apply, pass rendered output through `flux envsubst --strict` with substitution values loaded securely and without echoing them.
- Prefer server-side dry-run or `kubectl diff` before apply when supported by the resource.
- Never bypass Lefthook with `--no-verify`. If `oxfmt` reports `Permission denied`, ensure its required Linux Node runtime is available in WSL and rerun the hook.

After live changes, verify at least:

```bash
mise exec -- flux get kustomizations --all-namespaces
mise exec -- kubectl get gateways.gateway.networking.k8s.io --namespace network
mise exec -- kubectl get httproutes.gateway.networking.k8s.io --all-namespaces
```

Also inspect affected HelmRelease, deployment/DaemonSet, pod, route `Accepted` and `ResolvedRefs` conditions, and recent error logs. A running pod alone is not proof that Flux is healthy.

## Repository Layout

- Place applications under `kubernetes/apps/<namespace>/<app>/`.
- Follow existing namespace roots: `namespace.yaml`, root `kustomization.yaml`, child `<app>/ks.yaml`, and `<app>/app/kustomization.yaml`.
- Keep each Flux child `Kustomization` target namespace aligned with its directory namespace.
- Cross-namespace `dependsOn` entries must include `namespace`. Example: dependencies on `onepassword-connect` use `namespace: external-secrets`.
- Reuse existing OCIRepository, HelmRelease, ExternalSecret, HTTPRoute, monitoring, and resource conventions before adding new structure.

## Availability Tiers

- `platform.home.arpa/tier: "0"` identifies a workload whose existing service must remain available during a planned single-node drain.
- Never infer or assign Tier 0 silently. If a new or existing workload appears to require Tier 0 availability, explain the failure impact and ask the user to confirm the classification before changing it.
- After the user confirms Tier 0, add the label to the workload's pod template and add a per-workload PodDisruptionBudget when the controller type and replica topology make a PDB effective.
- Do not select multiple independent workloads with one shared Tier 0 PodDisruptionBudget. Each availability unit needs its own budget and stable workload-specific selector.
- A PDB is not high availability. Before adding one, ensure the workload has enough replicas on distinct nodes; do not add a blocking PDB to a singleton. DaemonSets normally do not need PDBs because node drains do not evict their pods through the eviction API.
- Use `maxUnavailable: 1` for an approved two-or-more-replica Tier 0 workload unless the user explicitly approves a different disruption policy.
- Rook-generated Ceph MON, MGR, MDS, and OSD budgets remain operator-owned; do not duplicate or override them.

## Namespace and Helm Safety

- Namespace omission is high risk. Rendering a HelmRelease or application into `default` can create duplicate workloads and cluster-scoped RBAC ownership conflicts.
- Before applying, verify every namespaced object resolves to its intended namespace.
- After an interrupted or mistaken apply, check the exact resource names in `default` and the intended namespace.
- Never remove an accidental Helm release blindly. First inspect Helm ownership, cluster-scoped RBAC, Gateway status, proxy configuration, and the intended release's stored manifest.
- Restore only proven missing objects from a known-good release manifest; do not broadly replace RBAC or force reconciliation.

## Networking Architecture

- Envoy Gateway is owned by `network/envoy-gateway` and serves `envoy-external` and `envoy-internal`.
- Cilium L7 policy support is separate from Envoy Gateway. Preserve current Cilium intent unless explicitly redesigning it:
    - `l7Proxy: true`
    - `envoy.enabled: false`
    - `gatewayAPI.enabled: false`
    - Hubble, relay, and UI enabled
- Do not enable Cilium's Envoy or Gateway API merely because Envoy Gateway exists in `network`; these are different controllers and data planes.
- Internal routes attach to `network/envoy-internal`; external routes attach to `network/envoy-external`.
- After Envoy changes, require both Gateways to report `Accepted=True` and `Programmed=True`, then verify affected HTTPRoutes.

## Gatus Conventions

- Gatus lives in the correctly spelled `observability` namespace.
- Keep it stateless with memory storage and `TZ: Europe/Amsterdam` unless explicitly changed.
- Use Pushover as the default alert provider. Keep the `external-tlb` group on Discord through `DISCORD_TLB_WEBHOOK_URL`; do not add another Discord webhook integration unless explicitly requested.
- Monitoring is per-resource opt-in through `gatus.home-operations.com/endpoint` annotations.
- External Gateway defaults group endpoints as `external` and uses its configured public DNS resolver.
- Internal Gateway defaults are `group: internal`, `guarded: true`, and UI `hide-hostname`/`hide-url` enabled. Preserve these defaults.
- Do not assume `guarded: false` overrides inherited `guarded: true`; it did not for Hubble. For an exception, disable Gatus discovery on the HTTPRoute and define the complete endpoint on the backing Service.
- Disable monitoring on Gatus's own HTTPRoute to avoid self-monitoring.
- After annotation changes, verify generated Gatus configuration and endpoint behavior. Do not assume a sidecar restart or HTTPRoute recreation is needed; inspect first.

## External Secrets and Helm Values

- Confirm an ExternalSecret is `Ready=True`, its generated Secret exists, and the Helm-rendered workload contains the expected `envFrom` reference.
- Check secret key names only; never output values.
- Chart value placement must be verified against the rendered Deployment, not inferred from a Secret existing.
- A Flux child depending on the external-secrets namespace must use an explicit cross-namespace dependency.

## OCI and DNS Diagnosis

- Distinguish failure layers before changing configuration:
    - HTTP `503` or connection reset before headers indicates DNS, routing, TLS, proxy, or upstream reachability.
    - HTTP `401` indicates the registry was reached and authentication or bypass policy rejected the request.
- Registry `bypassNetworks` must match the source address observed by the registry. Pod CIDRs may be SNATed to a node or egress address.
- Test resolution and registry access from the component that performs the request, usually Flux source-controller, before changing router, DNS, or authentication settings.

## Git Conventions

- Preserve unrelated user changes and stage explicit paths.
- Split commits by Kubernetes namespace when practical.
- Use Conventional Commits with the namespace as scope for `feat`, `fix`, and `chore`, for example `feat(observability): deploy Gatus sidecar`.
- Keep subjects imperative and concise. Add a body only when the reason is not obvious.
- Re-run validation and inspect the staged diff before every commit. Never bypass hooks or signing.
