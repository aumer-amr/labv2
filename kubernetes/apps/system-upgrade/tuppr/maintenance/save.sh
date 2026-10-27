#!/bin/sh
set -eu

fail() { echo "Minecraft save failed: $*" >&2; exit 1; }

pid=
for comm in /proc/[0-9]*/comm; do
    [ "$(cat "$comm" 2>/dev/null)" = java ] || continue
    [ -z "$pid" ] || fail "multiple Java processes"
    pid=${comm%/comm}
done
[ -n "$pid" ] || fail "no Java process"
[ -p "$pid/fd/0" ] || fail "Java stdin is not a pipe"

log=/data/logs/latest.log
inode=$(stat -c %i "$log")
offset=$(wc -c < "$log")
printf 'save-all flush\n' > "$pid/fd/0"

attempt=0
while [ "$attempt" -lt 60 ]; do
    [ "$(cat "$pid/comm")" = java ] || fail "Java process disappeared"
    [ "$(stat -c %i "$log")" = "$inode" ] || fail "log rotated"
    [ "$(wc -c < "$log")" -ge "$offset" ] || fail "log truncated"
    if tail -c "+$((offset + 1))" "$log" | awk '
        /All dimensions are saved/ { saved = 1 }
        saved && /Saved the game/ { confirmed = 1 }
        END { exit !confirmed }
    '; then
        echo MINECRAFT_SAVE_CONFIRMED
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 1
done
fail "no fresh save completion within 60 seconds"
