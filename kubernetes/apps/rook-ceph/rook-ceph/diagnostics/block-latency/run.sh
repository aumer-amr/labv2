#!/usr/bin/env bash
set -uo pipefail
export LC_ALL=C TZ=UTC

duration=${1:-0}
post_seconds=${2:-120}
if [[ ! $duration =~ ^(0|[1-9][0-9]{0,5})$ || ! $post_seconds =~ ^[1-9][0-9]{0,2}$ ]] || ((duration > 604680 || post_seconds > 120)); then
  printf '%s\n' '{"event":"invalid","reason":"duration_out_of_range"}'
  exit 1
fi

directory=$(mktemp -d /tmp/ceph-capture.XXXXXX) || exit 1
fifo=$directory/events
trap 'rm -f "$fifo"; rmdir "$directory"' EXIT
mkfifo -m 600 "$fifo" || exit 1
reason=deadline
invalid=0
started=0
armed=0
startup=$SECONDS
stopped=0
deadline_seen=0
signal_at=-1
last_heartbeat=$SECONDS
post_deadline=-1
bytes=0
budget_started=$SECONDS
second=$SECONDS
records=0
snapshot=0
logical_count=0
device_count=0
logical_age=0
device_age=0
logical_done=0
device_done=0

request_stop() {
  if ((signal_at < 0)); then
    reason=$1
    signal_at=$SECONDS
    kill -INT "$tracer" 2>/dev/null || true
  fi
}

emit() {
  # Bound daily output without imposing an eventual lifetime cutoff.
  if ((SECONDS - budget_started >= 86400)); then bytes=0; budget_started=$SECONDS; fi
  bytes=$((bytes + ${#1} + 1))
  if ((bytes > 104857600)); then
    invalid=1
    request_stop output_budget
    return
  fi
  if ((second != SECONDS)); then second=$SECONDS; records=0; fi
  records=$((records + 1))
  if ((records > 256)); then
    invalid=1
    request_stop output_rate
    return
  fi
  printf '%s\n' "$1"
}

candidate() {
  if ((post_deadline < 0)); then
    post_deadline=$((SECONDS + post_seconds))
    emit "{\"event\":\"candidate\",\"post_seconds\":$post_seconds,\"requires_ceph_correlation\":true}"
  fi
}

# No pod API access or direct log-server connection: the existing collector reads stdout.
bpftrace -B none -k /capture/capture.bt "$duration" >"$fifo" 2>&1 &
tracer=$!
trap 'invalid=1; request_stop terminated' TERM INT
exec 3<"$fifo"
while :; do
  if IFS= read -r -t 1 line <&3; then
    if [[ $line =~ ^@logical_start\[.*\]:[[:space:]]+([0-9]+)$ ]]; then
      age=$(((snapshot - BASH_REMATCH[1]) / 1000000))
      logical_count=$((logical_count + 1))
      if ((age > logical_age)); then logical_age=$age; fi
    elif [[ $line =~ ^@device_start\[.*\]:[[:space:]]+([0-9]+)$ ]]; then
      age=$(((snapshot - BASH_REMATCH[1]) / 1000000))
      device_count=$((device_count + 1))
      if ((age > device_age)); then device_age=$age; fi
    elif [[ $line =~ ^@coverage\[([0-9]+),[[:space:]]*([0-9]+)\]:[[:space:]]+([0-9]+)$ ]]; then
      emit "{\"event\":\"coverage\",\"op\":${BASH_REMATCH[1]},\"reason\":${BASH_REMATCH[2]},\"count\":${BASH_REMATCH[3]}}"
    elif [[ $line =~ ^@operations\[([0-9]+),[[:space:]]*([0-9]+)\]:[[:space:]]+([0-9]+)$ ]]; then
      emit "{\"event\":\"operations\",\"layer\":${BASH_REMATCH[1]},\"op\":${BASH_REMATCH[2]},\"count\":${BASH_REMATCH[3]}}"
    elif [[ $line =~ ^@stats\[([0-9]+)\]:[[:space:]]+([0-9]+)$ ]]; then
      key=${BASH_REMATCH[1]}; value=${BASH_REMATCH[2]}
      if ((key == 1)); then logical_done=$value; fi
      if ((key == 3)); then device_done=$value; fi
      emit "{\"event\":\"counter\",\"counter\":$key,\"count\":$value}"
      if (((key == 8 || key == 10) && value > 0)); then
        invalid=1
        request_stop unsupported_lifecycle
      fi
    elif [[ $line == @* ]]; then
      # Unknown map output might contain pointer keys; never forward it.
      invalid=1
      request_stop unexpected_map_output
    elif [[ $line == *'"event":"snapshot_end"'* ]]; then
      emit "{\"event\":\"inflight\",\"logical_count\":$logical_count,\"logical_oldest_ms\":$logical_age,\"device_count\":$device_count,\"device_oldest_ms\":$device_age}"
      if ((logical_age >= 5000 || device_age >= 5000)); then candidate; fi
    elif [[ -n $line ]]; then
      emit "$line"
      if [[ $line == *'"event":"start"'* ]]; then started=1; last_heartbeat=$SECONDS; fi
      if [[ $line == *'"event":"armed"'* ]]; then armed=1; fi
      if [[ $line == *'"event":"stop"'* ]]; then stopped=1; fi
      if [[ $line == *'"event":"stop","deadline":1'* ]]; then deadline_seen=1; fi
      if [[ $line =~ \"monotonic_ns\":([0-9]+) ]]; then
        snapshot=${BASH_REMATCH[1]}
        logical_count=0; device_count=0; logical_age=0; device_age=0
        last_heartbeat=$SECONDS
      fi
      if [[ $line == *'"trigger":1'* ]]; then candidate; fi
      if [[ $line == *WARNING:* || $line == *ERROR:* || $line =~ [Ll]ost[[:space:]]+[1-9][0-9]*[[:space:]]+events ]]; then
        invalid=1
        request_stop tracer_error_or_loss
      fi
    fi
  else
    read_status=$?
    if ((read_status == 1)); then break; fi
  fi
  if ((post_deadline >= 0 && SECONDS >= post_deadline)); then request_stop candidate_captured; fi
  if ((!armed && SECONDS - startup > 90)); then invalid=1; request_stop attachment_not_ready; fi
  if ((SECONDS - last_heartbeat > 90)); then invalid=1; request_stop heartbeat_missing; fi
  if ((signal_at >= 0 && SECONDS - signal_at > 10)); then
    kill -KILL "$tracer" 2>/dev/null || true
    invalid=1
    reason=shutdown_timeout
    break
  fi
done
exec 3<&-
wait "$tracer"
status=$?
if ((signal_at < 0 && (!deadline_seen || duration == 0))); then invalid=1; reason=unexpected_exit; fi
if ((!armed && signal_at < 0)); then reason=attachment_not_ready; fi
if ((status != 0 || !started || !armed || !stopped || logical_done == 0 || device_done == 0)); then invalid=1; fi
printf '{"event":"result","reason":"%s","invalid":%d,"tracer_exit":%d,"logical_done":%d,"device_done":%d}\n' "$reason" "$invalid" "$status" "$logical_done" "$device_done"
exit "$invalid"
