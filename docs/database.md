# Application Databases

Use the shared CloudNativePG component to create one PostgreSQL cluster in the
same namespace as each application. The component owns the cluster, database,
role, credentials, backups, monitoring, and workload network policies.

## Prerequisites

Before adding manifests:

- Create a dedicated R2 bucket for the application and configure its lifecycle
  to delete objects older than 35 days. The component retains CloudNativePG
  backups for 30 days.
- Add these fields to the application's 1Password item named exactly like the
  application:
    - `POSTGRES_PASSWORD`
    - `R2_ACCESS_KEY_ID`
    - `R2_SECRET_ACCESS_KEY`
- Confirm `CLOUDFLARE_ACCOUNT_ID` is available through the existing
  `cluster-secrets` substitution Secret.
- Ensure the application Pods have the label
  `app.kubernetes.io/name: <app>`. The component's NetworkPolicy uses that
  label to permit PostgreSQL access.

Never put credential values in Git or print generated Secret data.

## Add the component

In `kubernetes/apps/<namespace>/<app>/ks.yaml`, add the CNPG component and its
direct dependencies. Pin the PostgreSQL major explicitly because the upgrade
command requires it.

```yaml
spec:
    components:
        - ../../../../components/cnpg
    dependsOn:
        - name: barman-cloud
          namespace: database
        - name: onepassword-connect
          namespace: external-secrets
        - name: postgresql-images
          namespace: database
        - name: rook-ceph-cluster
          namespace: rook-ceph
    postBuild:
        substituteFrom:
            - kind: Secret
              name: cluster-secrets
        substitute:
            APP: example
            NAMESPACE: apps
            PG_BACKUP_BUCKET: example-postgres
            PG_BACKUP_MINUTE: "5"
            PG_INSTANCES: "1"
            PG_MAJOR: "18"
            PG_SSLMODE: verify-full
            PG_SYNC_REPLICAS: "0"
    prune: false
```

Replace `example`, `apps`, and `example-postgres`. Keep `prune: false`: removing
the component or restoring an accidentally removed manifest does not make CNPG
restore a deleted cluster from R2 automatically.

The defaults suit non-critical applications:

| Setting            | Default       | Purpose                          |
| ------------------ | ------------- | -------------------------------- |
| `PG_INSTANCES`     | `1`           | PostgreSQL instances             |
| `PG_MAJOR`         | `18`          | Reviewed image catalog major     |
| `PG_SYNC_REPLICAS` | `0`           | Required synchronous standbys    |
| `PG_SSLMODE`       | `verify-full` | Application TLS verification     |
| `PG_BACKUP_MINUTE` | `0`           | Minute of the daily 02:00 backup |

For an important application, use three instances and one synchronous replica:

```yaml
PG_INSTANCES: "3"
PG_SYNC_REPLICAS: "1"
```

The component currently provisions a 5 GiB `ceph-block` volume per instance.
Three instances tolerate one database Pod or node loss; they do not make the
application itself highly available.

## Connect the application

The component creates `${APP}-postgres`, a `kubernetes.io/basic-auth` Secret
with these keys:

- `host`: `${APP}-rw.${NAMESPACE}.svc.cluster.local`
- `port`: `5432`
- `dbname`
- `username`
- `password`
- `sslmode`

Map those keys to the environment variable names expected by the application.
Do not assume `envFrom` uses the names the application expects.

`verify-full` also requires the application to trust CNPG's server CA. Mount
`${APP}-ca` and configure the PostgreSQL driver to use its `ca.crt` file. For a
libpq-compatible application, the relevant settings are equivalent to:

```text
PGHOST=<value from APP-postgres/host>
PGPORT=<value from APP-postgres/port>
PGDATABASE=<value from APP-postgres/dbname>
PGUSER=<value from APP-postgres/username>
PGPASSWORD=<value from APP-postgres/password>
PGSSLMODE=<value from APP-postgres/sslmode>
PGSSLROOTCERT=/etc/postgresql/ca/ca.crt
```

Mount the CA without copying it into another Secret:

```yaml
volumes:
    - name: postgres-ca
      secret:
          secretName: ${APP}-ca
          items:
              - key: ca.crt
                path: ca.crt
containers:
    - name: app
      volumeMounts:
          - name: postgres-ca
            mountPath: /etc/postgresql/ca
            readOnly: true
```

Use the `-rw` service for normal application traffic. The `-ro` service targets
replicas, and `-r` targets every instance. The component only permits clients
with the application label in the same namespace.

## Validate before rollout

From the repository root, build both the application Kustomization and the
Flux result with substitutions:

```sh
namespace=<namespace>
app=<app>
app_dir="kubernetes/apps/$namespace/$app"
rendered="$(mktemp)"

mise exec -- kustomize build \
  --load-restrictor=LoadRestrictionsNone \
  "$app_dir/app"

mise exec -- flux build kustomization "$app" \
  --namespace "$namespace" \
  --path "$app_dir/app" \
  --kustomization-file "$app_dir/ks.yaml" \
  --strict-substitute >"$rendered"

mise exec -- kubectl apply \
  --server-side \
  --dry-run=server \
  --namespace "$namespace" \
  --filename "$rendered"

rm -f -- "$rendered"
```

Inspect the rendered kinds, names, and namespaces before committing. Commit and
push only when requested; Flux cannot deploy a local commit.

## Verify after rollout

Do not treat a Running Pod as sufficient proof. Check the Flux child, cluster,
storage, credentials, backup object, monitoring, and recent logs:

```sh
mise exec -- flux get kustomization <app> --namespace <namespace>
mise exec -- kubectl get clusters.postgresql.cnpg.io --namespace <namespace>
mise exec -- kubectl get pods,pvc --namespace <namespace>
mise exec -- kubectl get externalsecrets.external-secrets.io --namespace <namespace>
mise exec -- kubectl get objectstores.barmancloud.cnpg.io --namespace <namespace>
mise exec -- kubectl get scheduledbackups.postgresql.cnpg.io --namespace <namespace>
mise exec -- kubectl logs deployment/cloudnative-pg --namespace database --since=10m
```

Verify the application can connect with TLS and that the first backup completes
successfully. Also inspect Prometheus targets and Hubble for policy denials.

## Upgrade PostgreSQL

Upgrade one cluster at a time after reviewing the target major in the shared
image catalog:

```sh
just cnpg::upgrade <namespace> <app> <target-major>
```

The command validates cluster health and disk capacity, creates pre- and
post-upgrade backups, updates `PG_MAJOR`, commits and pushes the change after an
explicit confirmation, waits for CNPG, and verifies the application Pods.

## Remove or restore

Do not remove the component as ordinary application cleanup. With pruning
disabled, deletion is intentionally manual. Before deleting a Cluster, verify a
successful R2 backup and its recovery path; deleting the Cluster can also remove
its PVCs.

Reapplying the normal manifests creates a fresh cluster. Restoring existing data
requires an explicit CNPG recovery bootstrap from the R2 ObjectStore.
