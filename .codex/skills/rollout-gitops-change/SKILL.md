---
name: rollout-gitops-change
description: Stage, verify, and recover Flux-managed Kubernetes changes with explicit namespaces and reversible live testing. Use when planning or performing a live GitOps rollout, applying rendered manifests temporarily, suspending or resuming a Flux Kustomization, verifying reconciliation, or reporting live drift. Use rollout-network-policies instead for NetworkPolicy enforcement.
---

# Roll Out GitOps Change

Keep Git as desired state and live testing narrow, authorized, and reversible.

## Prepare

1. Read `AGENTS.md`, owning Flux `Kustomization`, source, existing resource, events, controller status, and repository pattern.
2. Record current Git revision, Flux owner, resource generation, health conditions, and recovery source.
3. Make the smallest repository change first.
4. Build each touched Kustomize directory with unrestricted load handling.
5. Load substitutions securely, run `flux envsubst --strict`, and inspect every rendered kind, name, and namespace without exposing secret values.
6. Run repository validation, then server-side dry-run or `kubectl diff` with explicit namespaces.

## Test live

1. Obtain permission for every live action and exact target.
2. If Flux ownership would race the test, suspend only owning child `Kustomization` and record it.
3. Apply only rendered resources in scope with explicit namespaces. Never apply an application tree or Helm release into `default`.
4. Verify observed generation, conditions, workload availability, routes, logs, and application behavior.
5. Treat live-only changes as temporary drift until Git contains and Flux applies them.

## Restore ownership

1. Remove temporary resources or restore recorded state only through the verified recovery path.
2. Obtain permission before resuming each recorded owner.
3. When involved, resume `network/envoy-gateway` first and wait for `Ready=True` before dependents.
4. Confirm Flux applied the expected remote Git revision; a local commit is insufficient.

Report repository changes, live-only changes, suspended objects, validation results, recovery state, and remaining uncertainty. Never leave an owner suspended without saying so.
