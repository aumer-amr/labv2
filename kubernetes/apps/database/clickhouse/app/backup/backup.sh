#!/bin/sh
set -eu

backup_dir=/var/lib/clickhouse/backups
mkdir -p "$backup_dir"
pending="$backup_dir/pending-$$.zip"
trap 'rm -f "$pending"' EXIT

# Keep the last completed archive until the native database backup succeeds.
clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --query "BACKUP TABLE system.users, TABLE system.roles, TABLE system.settings_profiles, TABLE system.row_policies, TABLE system.quotas, ALL EXCEPT DATABASES system, information_schema, INFORMATION_SCHEMA TO File('$pending')"
mv -f "$pending" "$backup_dir/latest.zip"
