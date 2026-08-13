---
name: verify-helm-secret-injection
description: Verify and diagnose the path from ExternalSecret through a generated Kubernetes Secret and Helm values into a rendered workload. Use when secret-backed environment variables are missing, a HelmRelease appears not to consume a Secret, chart value placement is uncertain, or an external-secrets Flux dependency may be wrong.
---

# Verify Helm Secret Injection

Trace configuration to rendered workload. Never inspect or output secret values.

1. Read `AGENTS.md`, ExternalSecret, provider reference, HelmRelease values, chart source and version, Flux dependencies, and rendered workload.
2. Confirm ExternalSecret reports `Ready=True` and targets expected namespace and Secret name.
3. Confirm generated Secret exists and contains only expected key names. Do not print, decode, or log values.
4. Render HelmRelease and verify Deployment, StatefulSet, DaemonSet, or Job contains expected `envFrom`, `secretKeyRef`, or volume reference.
5. Treat rendered workload output as proof of chart value placement; Secret existence alone proves nothing about consumption.
6. Confirm a Flux child depending on external secrets uses an explicit cross-namespace dependency on `onepassword-connect` in namespace `external-secrets`.
7. Compare live workload generation and pod template only when cluster inspection is relevant. Obtain permission before reconciliation, restart, patch, or apply.

Report status at each boundary: ExternalSecret, generated Secret, rendered workload, Flux dependency, and live workload. Identify key names only.
