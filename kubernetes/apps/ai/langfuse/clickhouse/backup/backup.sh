#!/bin/sh
set -eu

backup_dir=/var/lib/clickhouse/backups
mkdir -p "$backup_dir"
pending="$backup_dir/pending-$$.zip"
trap 'rm -f "$pending"' EXIT

# Keep the last completed archive until the native database backup succeeds.
clickhouse-client --query "BACKUP DATABASE default TO File('$pending')"
mv -f "$pending" "$backup_dir/latest.zip"
