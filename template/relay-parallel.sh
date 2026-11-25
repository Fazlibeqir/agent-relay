#!/usr/bin/env bash
# relay-parallel.sh - parallel mode for relay-files.sh. Sourced, never run.
#
# One parallel round:
#   1. the dispatcher (the primary AI) reads the open tasks and picks 2 or more that can be built at the same time, one per builder (relay-parallel.py checks the plan);
#   2. every builder works its task in its own git worktree (next to the project, named <project>-lanes/<task>), all at the same time;
#   3. one lane at a time: the relay merges the lane's commits, runs the project's full test suite and the reviewer judges the result, exactly as in a serial round.
# Builds overlap; merging, testing and review never do (the tests share ports and user folders). A lane whose merge conflicts or whose tests fail after merging is undone
# and its task goes back into the pool; a task that fails twice is parked for the person. If parallel mode cannot run (fewer than two builders or no two tasks
# that can go together), parallel_round returns 1 and relay-files.sh runs one task the normal way.
# Needs from relay-files.sh: log, LOG, BRANCH, TEST_CMD, TASK_FILE, REVIEW_FILE, QUEUE_FILE, COMMIT_FILE, BUILD_PROMPT, round, stalls, MAX_STALLS, commit_state,
# apply_builder_commit, handle_requests, verify_round, review_round; from relay-lib.sh: tool_run, codex_run, codex_pick, claude_run, provider_ready.

par_lanes_root() { printf '%s-lanes' "$(pwd -P)"; }
par_branch() { printf '%s-lane-%s' "$BRANCH" "$1"; }

par_cleanup_lane() {  # par_cleanup_lane <id>: remove a lane's worktree and branch (both are the relay's own)
  local dir branch
  dir="$(par_lanes_root)/$1"; branch=$(par_branch "$1")
  git worktree remove --force "$dir" >/dev/null 2>&1
  rm -rf "$dir" 2>/dev/null
  git branch -D "$branch" >/dev/null 2>&1
  return 0
}

par_sweep() {  # lanes left behind by a stopped run
  local b
  git worktree prune >/dev/null 2>&1
  for b in $(git for-each-ref --format='%(refname:short)' "refs/heads/$BRANCH-lane-*" 2>/dev/null); do par_cleanup_lane "${b#"$BRANCH"-lane-}"; done
  return 0
}

par_calls_today() {  # par_calls_today <tool>: backup calls counted today (shared with the serial fallback builders)
  local f="relay-fallback-$1.count"
  if [ -f "$f" ] && [ "$(head -n 1 "$f")" = "$(date +%F)" ]; then sed -n 2p "$f"; else echo 0; fi
}

par_count_call() { printf '%s\n%s\n' "$(date +%F)" "$(( $(par_calls_today "$1") + 1 ))" > "relay-fallback-$1.count"; }

par_workers() {  # the builders that can take a lane now: installed, not waiting for a usage reset, under the daily cap (log goes to stderr: the result is captured)
  local w out="" max="${FALLBACK_MAX_PER_DAY:-$S_FALLBACK_MAX}" used
  for w in $S_PARALLEL_WORKERS; do
    case "$w" in
      codex) [ -n "${CODEX_BIN:-}" ] || continue;;
      copilot) { [ -n "${COPILOT_BIN:-}" ] && [ -x "$COPILOT_BIN" ]; } || continue
        case "${COPILOT_MODEL:-$S_COPILOT_MODEL}" in auto|"") log "parallel: copilot skipped, it needs a named model (AIs & settings, backup models) so a review can tell who built the work" >&2; continue;; esac;;
      opencode) command -v "${OPENCODE_BIN:-opencode}" >/dev/null 2>&1 || continue;;
      *) continue;;
    esac
    if ! provider_ready "$w"; then log "parallel: $w is waiting for its usage limit to reset" >&2; continue; fi
    role_eligible builder "$w" auto >&2 || continue
    if [ "$w" != codex ]; then
      used=$(par_calls_today "$w")
      if [ "${used:-0}" -ge "$max" ]; then log "parallel: $w skipped, $used backup calls today (max $max)" >&2; continue; fi
    fi
    out="$out $w"
  done
  printf '%s' "${out# }"
}

par_plan() {  # par_plan "<workers>": ask the dispatcher; the validated lanes ('T05|codex' per line) go to .relay/parallel-plan.txt. 0 when there are two or more.
  local workers=$1 prompt
  mkdir -p .relay; : > .relay/parallel-plan.txt
  prompt=$(python relay-parallel.py prompt . "$workers" | tr -d '\r') || return 1
  LAST_OUT=""
  claude_run dispatcher "$prompt" || { log "round $round — parallel: the dispatcher could not plan"; return 1; }
  printf '%s' "$LAST_OUT" | python relay-parallel.py plan . "$workers" | tr -d '\r' > .relay/parallel-plan.txt
  [ "$(wc -l < .relay/parallel-plan.txt)" -ge 2 ]
}

par_prepare() {  # par_prepare <id> <dir> <branch>: a worktree on a new branch at HEAD, with the task as its CURRENT_TASK.md (and earlier review feedback)
  par_cleanup_lane "$1"
  mkdir -p "$(dirname "$2")" || return 1
  git worktree add -q -b "$3" "$2" HEAD >/dev/null 2>&1 || return 1
  python relay-parallel.py task . "$1" > "$2/$TASK_FILE" || return 1
  if [ -f ".relay/lane-feedback/$1.md" ]; then cp ".relay/lane-feedback/$1.md" "$2/$REVIEW_FILE"; fi
  return 0
}

par_lane_build() {  # par_lane_build <id> <provider> <dir> <main log>: runs in a background subshell; the exit status is the build's (75 = out of usage)
  local id=$1 prov=$2 dir=$3 main_log=$4 rc=0 prompt common own
  cd "$dir" || exit 70
  log() { echo "[$(date '+%F %T')] [$id] $*" | tee -a "$main_log"; }
  limit_sleep() { exit 75; }          # a builder out of usage ends its lane; the round goes on without it
  fallback_build() { return 1; }      # never borrow another lane's builder
  if command -v cygpath >/dev/null; then
    common=$(cygpath -w "$(cd "$(git rev-parse --git-common-dir)" && pwd)"); own=$(cygpath -w "$(git rev-parse --absolute-git-dir)")
    CODEX_ARGS=(--sandbox workspace-write --add-dir "$common"); [ "$own" = "$common" ] || CODEX_ARGS+=(--add-dir "$own")
  fi
  prompt=${BUILD_PROMPT//"Run that command yourself before you finish and fix every failure."/"Other builders work at the same time on other tasks, so do NOT run the whole suite: run only the checks for the gates you changed (the relay runs the full suite after merging your work) and fix every failure."}
  ROLE_NOW=builder; : > relay-builder.identity; rm -f "$COMMIT_FILE"; LAST_OUT=""
  if [ "$prov" = codex ]; then codex_pick; codex_run "$prompt"; rc=$?
  else tool_run "$prov" "$prompt" auto; rc=$?; [ "$rc" -ne 2 ] || rc=75; fi
  tail -n 40 <<<"$LAST_OUT" > .relay-lane-said
  [ "$rc" -eq 0 ] || log "$prov ended with exit $rc"
  exit "$rc"
}

par_failed() {  # par_failed <id> <why>: count a failed attempt; the second one parks the task for the person so the rounds can go on
  local id=$1 why=$2 f n title
  mkdir -p .relay/lane-fails; f=".relay/lane-fails/$id"
  n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$f"
  log "round $round — $id did not make it in parallel mode (attempt $n of 2): $why"
  if [ "$n" -ge 2 ]; then
    title=$(head -n 1 "tasks/$id.md" 2>/dev/null | sed 's/^# *//' | tr -d '\r')
    printf '%s — NEEDS OWNER: parallel build failed twice (%s)\n' "${title:-$id}" "$why" >> "$QUEUE_FILE"
    rm -f "$f"
    commit_state "parked $id"
  fi
}

par_return() {  # par_return <id>: the task goes back into the pool (no current task, loop files committed, runtime idle)
  rm -f "$TASK_FILE"
  commit_state "returned $1"
  relay_runtime checkpoint idle "${task:-$1}" "$TEST_CMD" || exit 1
}

par_integrate() {  # par_integrate <id> <provider> <dir> <branch> <rc>: merge, test and review one finished lane. 0 when the work was merged and reviewed.
  local id=$1 prov=$2 dir=$3 branch=$4 rc=$5 n ident=()
  git config user.email >/dev/null 2>&1 || ident=(-c user.name="Agent Relay" -c user.email="agent-relay@localhost")
  cp "$dir"/relay-provider-*.limit . 2>/dev/null
  builder_said=$(cat "$dir/.relay-lane-said" 2>/dev/null)
  if [ "$rc" -eq 75 ]; then log "round $round — $id: $prov is out of usage; the task stays open"; return 1; fi
  if [ "$rc" -ne 0 ]; then par_failed "$id" "$prov could not finish the build (exit $rc)"; return 1; fi
  cp relay-pick.py "$dir/" 2>/dev/null        # apply_builder_commit asks it which listed files may hold secrets
  (cd "$dir" && task="$id" apply_builder_commit)
  if [ -d "$dir/relay-requests" ]; then mkdir -p relay-requests; cp -n "$dir"/relay-requests/*.json relay-requests/ 2>/dev/null; fi
  n=$(git rev-list --count "HEAD..$branch" 2>/dev/null || echo 0)
  if [ "$n" -eq 0 ] && ! grep -q '^BLOCKED:' <<<"$builder_said"; then par_failed "$id" "the builder made no commit"; return 1; fi
  # the task becomes the current task of the main tree, as in a serial round
  python relay-parallel.py task . "$id" > "$TASK_FILE" || { log "round $round — $id: its task file is gone"; rm -f "$TASK_FILE"; return 1; }
  task=$(sed -n 2p "$TASK_FILE" | tr -d '\r'); task_path=$(sed -n 1p "$TASK_FILE" | tr -d '\r')
  rm -f "$REVIEW_FILE"; if [ -f ".relay/lane-feedback/$id.md" ]; then cp ".relay/lane-feedback/$id.md" "$REVIEW_FILE"; fi
  cp "$dir/relay-builder.identity" relay-builder.identity 2>/dev/null || : > relay-builder.identity
  rm -f "$COMMIT_FILE"
  commit_state "dispatch $task"
  before=$(git rev-parse HEAD)
  relay_runtime checkpoint building "$task" "$TEST_CMD" || exit 1
  handle_requests
  # the build is recorded as a builder call of this task, so the reviewer rules (a different model from every builder) can see who built it
  local bin model call=""
  case "$prov" in codex) bin=${CODEX_BIN:-codex};; copilot) bin=${COPILOT_BIN:-copilot};; *) bin=${OPENCODE_BIN:-opencode};; esac
  model=$(awk -F'|' -v p="$prov" '$1 == p { m = $2 } END { print m }' "$dir/relay-builder.identity" 2>/dev/null | tr -d '')
  if [ -f relay-runtime.py ]; then
    call=$(RELAY_ACCESS_CLI="$(relay_cli_path "$bin")" relay_runtime begin builder "$prov" "${model:-auto}" "relay-live-$prov.log" | tr -d '')       || { par_return "$id"; par_failed "$id" "the routing policy refused the $prov build"; return 1; }
  fi
  if [ "$n" -gt 0 ] && ! git "${ident[@]}" merge --no-ff --no-edit -m "relay: merge $id ($prov build)" "$branch" >/dev/null 2>&1; then
    git merge --abort >/dev/null 2>&1
    [ -z "$call" ] || relay_runtime finish "$call" 0 interrupted >/dev/null
    log "round $round — $id conflicts with work merged earlier this round; it is redone on the new base"
    par_return "$id"; return 1
  fi
  [ -z "$call" ] || relay_runtime finish "$call" 0 success >/dev/null
  handle_requests wait
  after=$(git rev-parse HEAD)
  log "round $round — $task — $prov builder: merged $(git rev-list --count "$before..$after") commit(s); running the full suite for the review"
  relay_runtime checkpoint testing "$task" "$TEST_CMD" || exit 1
  if ! verify_round; then
    git reset -q --hard "$before"
    mkdir -p .relay/lane-feedback
    printf 'failed reviews: 1\n1. After merging this task the project tests failed. Fix them. Last lines: %s\n' "$(tail -n 8 "$TEST_LOG" | tr '\n\r' '  ')" > ".relay/lane-feedback/$id.md"
    par_return "$id"; par_failed "$id" "the project tests failed after merging"
    return 1
  fi
  review_round || exit 1       # a failed review stops the relay, as in a serial round
  mkdir -p .relay/lane-feedback
  if [ -f "$TASK_FILE" ]; then   # open AUTO gates remain: the feedback goes to the next lane of this task, which starts from a fresh round
    if [ -f "$REVIEW_FILE" ]; then cp "$REVIEW_FILE" ".relay/lane-feedback/$id.md"; fi
    par_return "$id"
  else rm -f ".relay/lane-feedback/$id.md" ".relay/lane-fails/$id"; fi
  return 0
}

parallel_round() {  # 0 when a parallel round ran, 1 when it cannot (the caller then runs one task the normal way)
  local workers lanes=() line id prov dir branch i merged=0 pids=() ids=() provs=() dirs=() branches=() rcs=()
  [ -n "${TEST_CMD:-}" ] || return 1
  workers=$(par_workers)
  set -- $workers
  if [ "$#" -lt 2 ]; then log "round $round — parallel mode needs two available builders (have: ${workers:-none}); one task at a time this round"; return 1; fi
  par_sweep
  log "round $round — parallel mode: the dispatcher plans a round for $workers"
  par_plan "$workers" || { log "round $round — no two tasks can be built together right now; one task at a time this round"; return 1; }
  mapfile -t lanes < .relay/parallel-plan.txt
  for line in "${lanes[@]}"; do
    line=${line%$'\r'}; id=${line%%|*}; prov=${line##*|}
    dir="$(par_lanes_root)/$id"; branch=$(par_branch "$id")
    if ! par_prepare "$id" "$dir" "$branch"; then log "round $round — $id: could not prepare its worktree"; par_cleanup_lane "$id"; continue; fi
    [ "$prov" = codex ] || par_count_call "$prov"
    log "round $round — $id: $prov builds in its own worktree"
    ( RELAY_LIVE_DIR="$PWD" par_lane_build "$id" "$prov" "$dir" "$PWD/$LOG" ) &
    pids+=($!); ids+=("$id"); provs+=("$prov"); dirs+=("$dir"); branches+=("$branch")
  done
  [ "${#pids[@]}" -gt 0 ] || return 1
  for i in "${!pids[@]}"; do wait "${pids[$i]}"; rcs[$i]=$?; done
  for i in "${!ids[@]}"; do
    if par_integrate "${ids[$i]}" "${provs[$i]}" "${dirs[$i]}" "${branches[$i]}" "${rcs[$i]}"; then merged=$((merged + 1)); fi
    par_cleanup_lane "${ids[$i]}"
  done
  if [ "$merged" -eq 0 ]; then
    stalls=$((stalls + 1)); log "round $round — no lane produced reviewed work ($stalls of $MAX_STALLS)"
    if [ "$stalls" -ge "$MAX_STALLS" ]; then log "stalled: $MAX_STALLS rounds in a row without new work — stopping"; exit 2; fi
  else stalls=0; fi
  return 0
}
