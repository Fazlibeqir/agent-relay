#!/usr/bin/env bash
# Idle chores: token-free work the relay does instead of only sleeping when every builder is out of usage.
#   bash relay-idle.sh <seconds the relay will wait anyway>      (run from the repo root; prints one line per chore)
# Chores (IDLE_CHORES, default "backend digest"): the Java backend tests once per commit, then the owner review digest.
# Never edits tracked files, never touches git, never calls an AI. Results go to relay-idle-*.result (ignored by git).
command -v python >/dev/null 2>&1 || python() { python3 "$@"; }
avail=${1:-0}; chores="${IDLE_CHORES:-backend digest}"
[ "$avail" -ge 300 ] || { echo "idle chores skipped: only ${avail}s to wait"; exit 0; }
t0=$(date +%s)
for c in $chores; do
  left=$(( avail - ($(date +%s) - t0) - 240 ))   # leave a margin so the relay is back before the limit resets
  case "$c" in
    backend)
      head_sha=$(git rev-parse --short HEAD)
      if [ "$(cat relay-idle-backend.stamp 2>/dev/null)" = "$head_sha" ]; then echo "idle chore backend: already tested $head_sha"; continue; fi
      [ "$left" -ge 120 ] && command -v mvn >/dev/null && [ -f backend/pom.xml ] || { echo "idle chore backend: skipped (time, maven or pom missing)"; continue; }
      [ "$left" -gt 2400 ] && left=2400
      s=$(date +%s)
      (cd backend && timeout --kill-after=30 "$left" mvn -B -ntp test) > relay-idle-backend.log 2>&1; rc=$?
      d=$(( $(date +%s) - s )); sum=$(grep -aE "Tests run:.*Fail" relay-idle-backend.log | tail -n 1 | sed 's/\x1b\[[0-9;]*m//g')
      if [ $rc -eq 0 ]; then res="PASS"; elif [ $rc -eq 124 ]; then res="TIMED OUT after ${left}s (not a verdict)"; else res="FAIL (rc=$rc, see relay-idle-backend.log)"; fi
      case "$sum" in *"Skipped: "[1-9]*) sum="$sum -- the skipped ones need a PostgreSQL database (PROJECT_X_TEST_* variables) and did NOT run";; esac
      printf '%s\n' "Java backend tests on $head_sha, $(date +%F\ %H:%M): $res in ${d}s. ${sum}" > relay-idle-backend.result
      [ $rc -eq 124 ] || echo "$head_sha" > relay-idle-backend.stamp
      echo "idle chore backend: $res in ${d}s";;
    digest)
      python relay-digest.py . | sed 's/^/idle chore digest: /';;
    *) echo "idle chore $c: unknown";;
  esac
done
