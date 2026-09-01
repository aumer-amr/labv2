# TODO

## Remove unreliable local tool-calling models

- [x] Remove Hermes 3 3B and Qwen3 4B from the local inference stack because neither works reliably with tool calls.

## Fix Memini requests to Luna

- [x] Stop Memini from sending `temperature: 0` to `gpt-5.6-luna`; omit the parameter so the model uses its supported default value of `1`.
- [x] Verify Memini can complete a chat request through LiteLLM without a `400 Bad Request` response.

## Add a reranker to Memini

- Deploy a reranker model and configure Memini to use it for retrieval.
- Verify reranking improves result ordering without breaking or materially slowing retrieval.

## Fix Open WebUI Kopiur backups

- Change `KOPIUR_PUID` and `KOPIUR_PGID` in `kubernetes/apps/ai/open-webui/ks.yaml` from `0` to `65532`.
- Render and validate the affected Kustomization.
- Reconcile through Flux after the repository change is committed and pushed.
- Verify a new Open WebUI snapshot reaches `Succeeded` and completes quick/deep verification.
- Test restore behavior and resulting ownership before relying on the backup for recovery.

Open WebUI runs as UID `0`, but its data is owned by `0:65532` and is group-readable/writable where needed. Running the Kopiur mover as `65532:65532` should permit backup access without granting privileged movers across the entire `ai` namespace.

## Close monitoring gap

- Add an alert for any Kopiur `Snapshot` remaining `Pending` longer than 30 minutes.
- Verify the alert fires for authorization-blocked snapshots and resolves after a newer snapshot succeeds.

Do not annotate the `ai` namespace with `kopiur.home-operations.com/privileged-movers=true` unless a root mover is proven necessary. That grant has namespace-wide security implications.
