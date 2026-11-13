# Proactive cluster monitoring

ZeroClaw investigates persistent metric signals and cluster alerts through LiteLLM. ZeroClaw schedules monitoring every 15 minutes through its native scheduler. Langfuse records investigation summaries. Qualified metric findings create tasks in the LabV2 project in Vikunja without an approval screen. Task creation does not authorize remediation.

## Responsibilities

| Profile or component   | Responsibility                                                                              |
| ---------------------- | ------------------------------------------------------------------------------------------- |
| `main`                 | Provide the internal assistant behind Kanidm and Envoy, with its existing tools and memory. |
| `alerts`               | Investigate Alertmanager webhooks with read-only monitoring tools.                          |
| `investigator`         | Investigate collected signals with read-only monitoring tools.                              |
| `monitor`              | Own the monitoring schedule and invoke only `monitor_cycle__run`.                           |
| `github_hooks`         | Own the hourly maintenance schedule and invoke only `github_hooks__run`.                    |
| Local automation tools | Enforce evidence, publication, Cloudflare validation, and recovery rules.                   |
| Langfuse               | Store investigation summaries and correlation identifiers.                                  |

All profiles share one ZeroClaw daemon. Each profile has its own tool grants and SQLite memory. No profile receives shell or delegation access. The two scheduled profiles use isolated sessions without memory injection.

The scheduler uses the local model to dispatch each fixed tool. Python performs all validation and external writes. The tools accept no arguments and cannot run arbitrary commands. Persisted attempt records prevent repeated execution within the same schedule interval. Native run history records the agent response. The tool's dispatch record is the authority for whether the workflow completed.

The Python tool server runs beside ZeroClaw in the same pod and listens only on loopback. It has no Service or external route. Only this container mounts publication, Cloudflare, and Langfuse credentials. Its subprocesses receive only the credentials needed for their workflow. Profiles control tool access, but they do not provide separate operating-system isolation inside the ZeroClaw process.

## Collection and evidence

The collector queries Prometheus for container restarts, memory pressure, storage growth, unavailable replicas, failed scrapes, and Flux readiness failures.

A metric finding requires three consecutive observations over at least 30 minutes. Missing metrics and incomplete query results block publication. Successful investigations have a six-hour cooldown. Failed investigations remain eligible for a later cycle.

A periodic review compares VictoriaLogs aggregates with the preceding interval. The review reports gaps and limits its backlog to 24 hours. Log reviews do not independently create tasks because aggregate counts alone do not identify a persistent workload problem.

Each cycle permits at most two sequential investigations within a shared deadline. Each investigation has at most 360 seconds, including one formatting retry. The collector calls the `investigator` profile through loopback. Before dispatch, it stores an active investigation marker. If a response is lost, it requests cancellation and keeps that marker. Later monitoring runs stop until an operator resolves the unfinished investigation. The tool process has a hard ten-minute deadline. A timeout also preserves the marker when an investigation remains unfinished.

The investigator uses only `qwen3.5-9b` and its existing read-only LiteLLM toolset. Shell, file, browser, messaging, scheduling, and delegation tools are denied. The model process does not receive publisher or Langfuse credentials. Network policies restrict service access.

The collector saves the actual tool events from ZeroClaw's chat stream with atomic writes. ZeroClaw's own chat transcript omits those events. It rejects errors, incomplete output, and answers without successful query evidence. The stream adapter matches the pinned ZeroClaw version. Run the container integration test before upgrading that version.

## Automatic publication

A task requires fresh, complete collection and a persistent active metric finding. It also requires a successful assessment, a Langfuse trace, and a recorded query for the affected resource over the exact six-hour window.

The publisher stores and reads back its intent before creating a task. It resolves the active project named `LabV2` through LiteLLM. It searches the observation marker before every write. A lost response therefore recovers an existing task without creating a duplicate.

Existing tasks remain unchanged, including completed tasks. Previously rejected draft hashes remain rejected. Changed, expired, resolved, uncertain, or incomplete findings do not publish. Model interpretation remains distinct from query evidence and can contain errors.

Raw session evidence expires after 30 days. Published task references and observation state remain. State files use atomic replacement and filesystem synchronization. A process-scoped file lock prevents overlapping collectors and releases when the process exits.

## Alerts and authentication

The internal hostname is `zeroclaw.aumer.dev`. Envoy authenticates browser requests through a dedicated Kanidm client. The application accepts traffic only from the internal Envoy gateway. Native pairing is disabled because Envoy owns browser authentication.

Alertmanager uses the exact `POST /sop/alertmanager` route with a separate bearer token. Envoy copies the Authorization header into X-Webhook-Secret. ZeroClaw compares the complete bearer value with its configured secret. The alert agent has only the read-only monitoring key. Alert investigations stay in durable ZeroClaw procedure runs and do not bypass the metric publication rules. Existing Pushover notifications continue. ZeroClaw runs one alert procedure at a time. A webhook deferred while busy waits for a later Alertmanager notification.

## Deployment and recovery

This branch prepares the migration. It does not record a completed rollout.

Before enabling ZeroClaw schedules, stop the old Windmill schedules and wait for active runs to finish. Do not run both publishers or Cloudflare updaters concurrently.

The automation server creates fresh state on its first start. It preserves existing ZeroClaw state on later starts and rejects corrupt state. No old observations, rejections, pending notifications, or schedule history are imported. New metric findings must pass the normal observation thresholds.

State lives in `/data/state` in the `automation` container, under the `automation` directory on the backed-up volume. ZeroClaw mounts the separate `agent` directory.

After credential setup and retirement, enable `cron.cluster_monitor` and `cron.github_hooks` in the configuration. Both jobs remain disabled in Git until this step is complete. The local tools also enforce that switch, including manual requests. The native scheduler runs one job at a time and skips missed runs after startup.

If `active-investigation.json` contains a session identifier, inspect the retained session and request cancellation. Confirm that the turn stopped, or restart the ZeroClaw daemon. Only then remove the marker from the automation state directory. Removing the marker before the turn stops can allow overlapping investigations. A completed stream clears the marker automatically. An uncertain stream never permits task publication.

Provision a dedicated Kanidm client named `zeroclaw`. Store `OIDC_CLIENT_SECRET` and `HOOK_TOKEN` in the existing 1Password provider's `zeroclaw` item. Grant the intended user group access to this client. Set `mcp_tool_search_enabled` on both new LiteLLM keys before enabling automation. The operator does not expose this field. Confirm that each key exposes `search_tools`, `get_tool_schema`, and `call_tool` with its intended toolset.

ZeroClaw uses SQLite on backed-up volumes. Its v0.9.0 PostgreSQL backend cannot use TLS, which the shared database component requires. No new PostgreSQL instance is created.

The Hermes and Windmill application trees are removed, including their storage, database, backup schedules, and obsolete credentials. Flux prunes their managed resources after merge and reconciliation. No historical data is retained for rollback. Storage or backup retention policies can leave physical data for separate cleanup during the authorized rollout.

ZeroClaw owns the Cloudflare ExternalSecret, encrypted webhook configuration, and restricted LiteLLM publication key. Their resource names and all consumers use the `zeroclaw-` prefix. Credential payloads and tool restrictions remain unchanged.

Reverting manifests cannot recover deleted historical state. Reinstalling the old applications requires fresh state or an independently available backup.

## Verification

Run the Python regression suite:

```bash
python3 -m unittest discover -s tests/ci/zeroclaw -p 'test_*.py'
```

Run `python3 tests/manual/check-zeroclaw-runtime.py` with Docker available. It builds a local test image containing the pinned binary and Python fixtures. Container networking is disabled. It exercises native schedules, profile tool grants, the gateway, and persisted query evidence without real provider calls.

Live authentication, webhook delivery, fresh state initialization, policy enforcement, and model behavior still require deployment verification.

## Repository sources

- [Monitoring workflow](../kubernetes/apps/ai/zeroclaw/workflow/monitor.py)
- [Durable state](../kubernetes/apps/ai/zeroclaw/workflow/state.py)
- [Profiles and schedules](../kubernetes/apps/ai/zeroclaw/app/config/config.toml)
- [Pinned runtime test](../tests/manual/check-zeroclaw-runtime.py)
- [Langfuse deployment](../kubernetes/apps/ai/langfuse/app/helmrelease.yaml)
