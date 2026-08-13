---
name: recover-helm-namespace-apply
description: Inspect and safely recover an interrupted or mistaken HelmRelease or application apply made in the wrong Kubernetes namespace. Use when duplicate workloads appear in default, cluster-scoped RBAC ownership may have moved, an intended release lost objects, or cleanup could damage the valid release.
---

# Recover Helm Namespace Apply

Build an exact ownership and recovery map before any mutation. Never delete or recreate resources without explicit permission and a verified recovery source.

## Inspect read-only

1. Read `AGENTS.md`, owning Flux objects, intended release manifests, events, Helm storage, Gateway status, and proxy configuration.
2. List exact resource names in `default` and intended namespace.
3. Map namespaced and cluster-scoped resources to Helm release ownership using labels, annotations, and stored manifests.
4. Compare mistaken release, intended release, and known-good stored manifest. Identify objects that are duplicate, missing, adopted, or still correctly owned.
5. Verify workload, Gateway, route, proxy, and cluster-scoped RBAC health before proposing cleanup.

## Plan recovery

- Remove only objects proven to belong solely to mistaken apply.
- Restore only proven missing objects from a known-good release manifest.
- Do not broadly replace RBAC, remove a release blindly, force reconciliation, or recreate managed resources.
- State action order, exact targets, ownership impact, rollback source, and post-action checks.

Obtain explicit permission for exact delete, apply, patch, or reconcile operations. After authorized recovery, verify both namespaces, Helm ownership, Flux health, Gateways, routes, proxy behavior, and intended application behavior.
