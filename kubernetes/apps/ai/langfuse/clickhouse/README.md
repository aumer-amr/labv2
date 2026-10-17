# ClickHouse recovery

The shared Kopiur component protects the `langfuse-clickhouse` PVC. Before each
snapshot, its workload hook runs a synchronous native ClickHouse backup of the
`default` database. It replaces `/var/lib/clickhouse/backups/latest.zip` only after
the backup succeeds. A hook failure aborts the snapshot.

Kopiur restores the complete volume through the shared Restore/PVC-populator
workflow. The native archive is an additional recovery path if the restored live
database files cannot be used.

For archive recovery, keep Langfuse stopped and use an isolated, empty ClickHouse
instance with the same pinned image and backup configuration. Use a fresh data
volume and mount only the restored `backups` directory at
`/var/lib/clickhouse/backups`. With the existing client
credentials supplied through environment variables, restore into a separate
database:

```sql
RESTORE DATABASE default AS recovery_check
FROM File('/var/lib/clickhouse/backups/latest.zip');
```

Check the restored tables and row counts before planning a production cutover.
Never overwrite a running database. Credentials remain in ExternalSecrets; the
archive covers application data, not the 1Password items or PostgreSQL database.

The backup archive shares the 20 GiB volume with live data, so allow disk space for
live data, the previous archive, and the pending archive during each backup.
The native backup hook and offsite Kopiur snapshot were verified successfully.
Native archive recovery and the Kopiur restore path still require a live rehearsal
before this is considered verified recovery.
