# Repository Instructions

## Persistent Skills

- At the first user command in every session, load and activate the installed `caveman` and `ponytail` skills in `full` mode before handling the command. Keep both active until the user explicitly changes their intensity or disables them. Discover their installed locations rather than copying machine-specific paths.
- Caveman applies to every response, including non-coding work. Brevity never overrides clarity, safety warnings, exact commands, required evidence, or complete answers explicitly requested by the user.
- Ponytail governs coding, architecture, dependency, and implementation decisions. It does not reduce required analysis, validation, security controls, accessibility, recovery behavior, or repository-mandated coverage.
- These preferences remain subordinate to platform/runtime instructions and repository safeguards. If a required skill is unavailable, report the gap rather than claiming activation.
- `stop caveman` disables only Caveman. `stop ponytail` disables only Ponytail. For `normal mode`, ask which skill the user wants disabled.

## Environment

- Use the repository kubeconfig configured by `.mise/config.toml`; the cluster is remote.
- Always prioritize the LiteLLM MCP server for supported operations. Discover and call its tools directly; use scripts, direct APIs, or other clients only when the required MCP capability is unavailable.
- Never print, decode, log, or quote secret values. Treat `age.key`, `kubeconfig`, `deploy.key`, and provider credentials as sensitive.

## Authorization

- Inspecting files and cluster state is allowed when relevant.
- Obtain explicit permission before any live state change unless the current request authorizes the exact action and target. This includes apply, patch, annotate, scale, restart, delete, suspend, resume, and forced reconciliation.
- Never commit or push unless explicitly requested. Permission to commit does not permit pushing.
- Open or modify pull requests and publish review actions only when explicitly requested.
- Ask when intent is materially ambiguous or a proposed choice materially changes scope, behavior, interfaces, persistence, authentication, dependencies, compatibility, cost, or external state beyond the current authorization. Proceed with low-risk choices supported by repository conventions and state the assumption briefly.
- Never delete or recreate a resource without explicit permission and a verified recovery path.
- Always prefer non-root containers. If root is the only viable option for a new application, warn the user and explain why before applying or committing it.

## Evidence and Change Scope

- Inspect affected execution paths, callers, configuration, installed versions, and regression risks before behavioral, dependency, security, data, or infrastructure changes.
- Make the smallest complete change using existing helpers, dependencies, structure, and error handling. Add dependencies only when necessary and authorized; report adjacent issues separately unless they block the requested result.
- For review, debugging, and analysis requests, do not force code changes after the question is answered with evidence.
- Never fabricate paths, commits, APIs, configuration, capabilities, test results, or evidence. State uncertainty and verification gaps explicitly.
- Before claiming completion, inspect the final diff, verify the requested result, update required tracking, and report relevant checks and any known unintended effects.

## Current Documentation

- For library, framework, SDK, API, CLI, or cloud-service questions, identify the installed or requested version and use Context7 when available. Resolve the library ID, then query with the user's full question. Discover these tools through LiteLLM first.
- Verify high-risk, disputed, license, security, and exact-version claims against matching official documentation or upstream source. If Context7 is unavailable or lacks the version, use official documentation and state the fallback. Purpose-built official documentation tools take precedence for their own products.
- Context7 is not required for business-logic debugging, general programming concepts, code review, refactoring without API questions, or scripts using only language-standard features.

## Issue Tracking

- Track all issues for this repository in the **LabV2** project in [Vikunja](https://vikunja.aumer.dev), instead of GitHub Issues.
- Use the Vikunja MCP tools through LiteLLM to search, create, update, and close issues. Resolve the project named `LabV2` before creating tasks; use that project rather than Inbox.
- When starting work on an issue or task, move it from **To-Do** to **Doing**. When the work and required verification are complete, move it to **Done** and mark it complete; keep its board column and completion state consistent.
- Mark subtasks and checklist items done as soon as their work is complete, rather than waiting for the parent task to finish.
- Always keep the Vikunja task up to date throughout the work: update its status, board column, completion state, subtasks, and checklists as they change. Add comments promptly for substantial progress, decisions and their rationale, blockers, scope changes, and verification results; do not defer updates until the final response.
- Never create documentation files unless the user specifically requests them. Record relevant findings, plans, implementation details, decisions, and verification evidence in the Vikunja task description or comments instead.
- Remember durable task context, decisions, and the Vikunja task link in memini under relevant topic tags so future work can recover the connection. Keep current status and checklists in Vikunja.

## Desired State and Namespace Safety

- Git is desired state; make the smallest repository change first. Treat live patches as temporary tests.
- A local commit is invisible to Flux until pushed. Confirm Flux applied the expected remote revision before calling a rollout complete.
- Use `flux envsubst --strict`, not GNU `envsubst`, for Flux manifests.
- Pass an explicit namespace for every namespaced operation. Never rely on `default` or apply an application tree or Helm release there.
- Suspend only the exact owning Flux child after permission. Record every suspended object and never leave one suspended without reporting it.

## Required Validation

- Preserve regression tests and update them when behavior changes. Use coverage proportional to the affected logic, boundaries, and runnable workflows; minimalism does not reduce required coverage.
- Never weaken assertions, narrow required scope, reduce coverage, or skip required checks to obtain a pass. Report pre-existing failures separately and state exactly what remains unverified when full validation is impractical.
- Run `git diff --check` for every change.
- Run `oxfmt --check` on changed YAML, JSON, and Markdown files.
- Build every touched Kustomize directory with `--load-restrictor=LoadRestrictionsNone`.
- Before a live apply, render locally, load substitutions without exposing secrets, run `flux envsubst --strict`, inspect rendered kinds, names, and namespaces, then prefer server-side dry-run or `kubectl diff`.
- Never bypass Lefthook or hooks. Commit signing is not required. If `oxfmt` reports `Permission denied`, provide its Linux Node runtime in WSL and rerun it.
- After a live change, inspect affected Flux Kustomizations, HelmReleases, workloads, pods, routes, conditions, and recent error logs. A running pod alone is not proof of health.
- After a live change, run at least:

```bash
flux get kustomizations --all-namespaces
kubectl get gateways.gateway.networking.k8s.io --namespace network
kubectl get httproutes.gateway.networking.k8s.io --all-namespaces
```

## Repository Layout

- Place applications under `kubernetes/apps/<namespace>/<app>/`.
- Follow existing namespace roots: `namespace.yaml`, root `kustomization.yaml`, child `<app>/ks.yaml`, and `<app>/app/kustomization.yaml`.
- Keep each Flux child `Kustomization` target namespace aligned with its directory namespace.
- Cross-namespace `dependsOn` entries must include `namespace`; dependencies on `onepassword-connect` use `namespace: external-secrets`.
- Reuse existing OCIRepository, HelmRelease, ExternalSecret, HTTPRoute, monitoring, and resource conventions before adding structure.

## Availability Tiers

- `platform.home.arpa/tier: "0"` means the existing service must remain available during a planned single-node drain. Never infer or assign Tier 0.
- If a workload affected by a proposed commit appears to require Tier 0 but lacks the classification, pause before committing, explain the drain failure impact, and ask whether to classify it and add the required protection.
- For confirmed Tier 0, label the pod template and add one workload-specific PodDisruptionBudget with a stable selector only when the controller has at least two replicas on distinct nodes. A PDB is not high availability.
- Use `maxUnavailable: 1` unless the user approves another policy. Never add a blocking PDB to a singleton or a shared PDB across independent workloads.
- DaemonSets normally need no PDB. Rook-generated Ceph MON, MGR, MDS, and OSD PDBs remain operator-owned.

## Networking Architecture

- Preserve each namespace's dedicated `network-policies` Flux child and its `dependsOn` gates. Keep namespace default-deny there; keep workload policies with their applications.
- Envoy Gateway is owned by `network/envoy-gateway` and serves `envoy-external` and `envoy-internal`.
- Preserve Cilium intent unless explicitly redesigning it: `l7Proxy: true`, `envoy.enabled: false`, `gatewayAPI.enabled: false`, with Hubble, relay, and UI enabled. Envoy Gateway and Cilium's Envoy/Gateway API are separate controllers and data planes.
- Attach internal routes to `network/envoy-internal` and external routes to `network/envoy-external`.
- Before implementing or changing an external route not classified as `external-tlb`, ask whether Kanidm must protect it.
- Never share a Kanidm OAuth2/OIDC client between independent applications. Store credentials through the existing ExternalSecret provider; never place them in manifests or output their values.

## Gatus Defaults

- Gatus lives in `observability`, remains stateless with memory storage, and uses `TZ: Europe/Amsterdam` unless explicitly changed.
- Use Pushover by default. Keep `external-tlb` on Discord through `DISCORD_TLB_WEBHOOK_URL`; do not add another Discord integration without explicit permission.
- Monitoring is opt-in through `gatus.home-operations.com/endpoint` annotations.
- External Gateway endpoints default to group `external` and its configured public DNS resolver. Internal endpoints default to `group: internal`, `guarded: true`, and hidden hostname/URL.
- Disable monitoring on Gatus's own HTTPRoute.

## Git Conventions

- Preserve unrelated changes and stage explicit paths.
- Never force-push to `main` or `master`.
- Never stage, commit, or push documentation unless explicitly authorized; generic commit or push permission does not include documentation.
- Split commits by Kubernetes namespace when practical.
- Use Conventional Commits with namespace scope for `feat`, `fix`, and `chore`, for example `feat(observability): deploy Gatus sidecar`.
- Keep subjects imperative and concise; add a body only when the reason is not obvious.
- Re-run validation and inspect the staged diff before each commit.

## Skill Routing

Load and follow all matching repository skills before task-specific work:

- `create-network-policy`: author a Kubernetes or Cilium network policy.
- `review-network-policies`: review policy correctness, evidence, or enforcement readiness.
- `rollout-network-policies`: stage, enforce, verify, or roll back live network policies.
- `rollout-gitops-change`: stage or verify any other live Flux-managed change.
- `manage-gateway-route`: add, change, secure, or verify Gateway API routes.
- `manage-gatus-monitoring`: add, change, troubleshoot, or verify Gatus monitoring.
- `verify-helm-secret-injection`: diagnose or verify ExternalSecret-to-Helm workload injection.
- `diagnose-oci-dns`: diagnose Flux OCI, registry, DNS, routing, TLS, or authentication failures.
- `recover-helm-namespace-apply`: inspect or recover a Helm/application apply made in the wrong namespace.
