---
name: rollout-network-policies
description: Stage and verify Kubernetes NetworkPolicy and CiliumNetworkPolicy changes with reversible audit-first enforcement. Use when applying netpols to a live cluster, planning rollout order, testing application and kubectl continuity, capturing recovery state, handling Hubble denials, or rolling back safely.
---

# Rollout Network Policies

Use an audit-first, allow-first, one-unit-at-a-time rollout. Keep a tested recovery path ready before the first live mutation.

## Guardrails

- Obtain explicit authorization for the exact live targets.
- Change Git desired state first. Render with Kustomize, run Flux-compatible strict substitution, inspect namespaces, and use server-side dry-run.
- Apply only policy objects with explicit namespaces. Never apply an application tree or rely on `default`.
- Record Flux owners, workload UIDs and restarts, Cilium endpoint IDs, audit settings, policy generations, and health baselines.
- Do not force field conflicts. Use the established field manager when ownership is already proven.
- Keep workstation `kubectl` access and `/readyz` checks in every stage.

## Stage safely

1. Enable policy audit mode on every current managed endpoint before applying the first policy. Re-resolve endpoints after any pod replacement.
2. Apply allow policies first. Validate policy conditions and workload health. Apply default-deny last.
3. In audit mode, exercise fresh DNS, API, probes, reconciles, hooks, Jobs, ingress, storage lifecycle, and cleanup paths that the application actually uses.
4. Query every Cilium agent. Classify expected DNS search-suffix noise separately; do not hide unexplained verdicts.
5. Enforce the least dangerous endpoint or node first. Put active storage or control-plane roles last. Change one unit, then run all gates before continuing.

## Gate every unit

Require all applicable checks:

- `/readyz` from the workstation after each mutation.
- Workloads Ready and Available with restart counts unchanged.
- Application-specific read, write, reconcile, route, probe, and cleanup behavior.
- Fresh Hubble flows with no unexplained `DROPPED` or `AUDIT` verdicts.
- Flux owners, Helm releases, Gateways, and routes at the current observed generation.
- Storage resources and backend objects absent after deletion, using delayed checks after detach completes.

Do not reuse established sockets as proof. Resolve active leaders and endpoints dynamically; do not hard-code pod names, roles, or storage pools.

## Roll back on any miss

1. Restore audit mode on every endpoint affected by the stage, including replacements.
2. If policies must be removed, delete default-deny first, then only the recorded test policies.
3. Restore controller-owned desired state through its owner. Do not patch generated Services or recreate managed resources unless the recovery plan explicitly requires it.
4. Recheck API access, workload health, routes, storage health, and Hubble before proceeding.

## Finish honestly

Leave no owner suspended and no temporary test resource behind. Report local desired-state changes, live-only changes, audit state, validation, residual trust, and untested disruptive operations. Treat uncommitted or unpushed policy changes as drift-prone, not GitOps-complete.
