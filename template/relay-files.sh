#!/usr/bin/env bash
# relay-files.sh - the relay for projects whose tasks are markdown files (tasks/T00.md, T01.md, ...), as the Agent Relay planner writes them.
#
# The loop: a small script (relay-pick.py, no AI) picks the next task, the builder AI builds it, the relay runs
# the project's one test command, a second AI reviews and ticks the AUTO gates in the task file. HUMAN gates are never ticked: the relay puts
# one back if the reviewer ticks it. Which AI plays which role comes from relay.settings.json (relay-settings.py), as in relay.sh.
#
#   bash relay-files.sh [repo]       (needs: AGENTS.md, tasks/, relay.project.json with a testCommand; run on a work branch, never main)
#   Env: TEST_CMD, MAX_ROUNDS, MAX_STALLS, MAX_TASK_ROUNDS, SLEEP_ON_LIMIT, AGENT_TIMEOUT, CLAUDE_BIN, CODEX_BIN
set -uo pipefail
command -v python >/dev/null 2>&1 || python() { python3 "$@"; }      # Linux often has only python3

SLEEP_ON_LIMIT="${SLEEP_ON_LIMIT:-1800}"
AGENT_TIMEOUT_OVERRIDE="${AGENT_TIMEOUT:-}"
AGENT_TIMEOUT="${AGENT_TIMEOUT:-5400}"
MAX_STALLS="${MAX_STALLS:-3}"
MAX_ROUNDS="${MAX_ROUNDS:-0}"
MAX_TASK_ROUNDS="${MAX_TASK_ROUNDS:-6}"
LIMIT_RE="usage limit|session limit|weekly limit|rate limit (reached|exceeded)|too many requests|hit your [a-z ]*limit|limit (will )?resets?|try again (in|at|after) [0-9]|premium request|quota (exceeded|reached)"
CLAUDE_MODEL="${CLAUDE_MODEL:-}"; CLAUDE_EFFORT="${CLAUDE_EFFORT:-}"; CLAUDE_ADVISOR="${CLAUDE_ADVISOR:-}"
CODEX_MODELS_CACHE="${CODEX_HOME:-$HOME/.codex}/models_cache.json"
CODEX_PICK=(); CODEX_PICK_DESC="config default"
TASK_FILE="CURRENT_TASK.md"; REVIEW_FILE="REVIEW.md"; QUEUE_FILE="PLAYTEST_QUEUE.md"
LOG="relay.log"; TEST_LOG="relay-tests.log"; COMMIT_FILE="relay-commit.txt"; LOCK=".relay.lock"

# ---------------------------------------------------------------- preflight
cd "${1:-.}" || { echo "No such directory: ${1:-.}"; exit 1; }
cd "$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "Run this inside the project's git repository."; exit 1; }
[ -f AGENTS.md ] && [ -d tasks ] && [ -f relay.project.json ] || { echo "This project has no AGENTS.md, tasks/ or relay.project.json yet: make the plan first."; exit 1; }
for bin in git timeout python; do command -v "$bin" >/dev/null || { echo "Missing: $bin"; exit 1; }; done
BRANCH=$(git rev-parse --abbrev-ref HEAD)
case "$BRANCH" in main|master|HEAD) echo "Refusing to run on '$BRANCH'. Use a work branch, e.g.: git checkout -b agent-run"; exit 1;; esac

if [ -z "${CODEX_BIN:-}" ]; then
  CODEX_BIN=$(ls -d "/c/Program Files/WindowsApps/"OpenAI.Codex_*/app/resources/codex.exe 2>/dev/null | sort -V | tail -n 1)
  [ -n "$CODEX_BIN" ] || CODEX_BIN=$(command -v codex || true)
fi
CODEX_ARGS=(--sandbox workspace-write)
if command -v cygpath >/dev/null; then
  GIT_COMMON=$(cygpath -w "$(cd "$(git rev-parse --git-common-dir)" && pwd)"); GIT_OWN=$(cygpath -w "$(git rev-parse --absolute-git-dir)")
  CODEX_ARGS=(--sandbox workspace-write --add-dir "$GIT_COMMON"); [ "$GIT_OWN" = "$GIT_COMMON" ] || CODEX_ARGS+=(--add-dir "$GIT_OWN")
fi

for f in "$LOG" "$TEST_LOG" "$COMMIT_FILE" "$LOCK/" "relay-requests/" ".relay/"; do grep -qxF "$f" .gitignore 2>/dev/null || echo "$f" >> .gitignore; done
worker_pid=$$
if command -v cygpath >/dev/null; then worker_pid=$(ps -l -p "$$" | awk 'NR==2 {print $4}'); fi
[[ "$worker_pid" =~ ^[0-9]+$ ]] || { echo "Could not identify this worker's PID"; exit 1; }
python "$(dirname "$0")/relay-runtime.py" claim "$worker_pid" || exit 1
trap 'rm -f "$LOCK/owner.json"; rmdir "$LOCK" 2>/dev/null' EXIT
trap 'exit 130' INT TERM
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# shared: usage-limit waits, settings, run/tool_run, fallback builders, builder_run, claude_run (dispatcher/reviewer), codex_pick
# shellcheck source=relay-lib.sh
source "$(dirname "$0")/relay-lib.sh"
# parallel mode (settings: "mode": "parallel"); the file is optional, a project without it always runs one task at a time
# shellcheck source=relay-parallel.sh
[ -f "$(dirname "$0")/relay-parallel.sh" ] && source "$(dirname "$0")/relay-parallel.sh"

TEST_CMD="${TEST_CMD:-$(python -c "import json;print(json.load(open('relay.project.json')).get('testCommand',''))" 2>/dev/null)}"

# ---------------------------------------------------------------- loop state and rules
commit_state() {  # keep the loop files and the task files committed, so every review is one clean commit
  local f paths=()
  for f in "$TASK_FILE" "$REVIEW_FILE" "$QUEUE_FILE" tasks; do
    if [ -e "$f" ] || git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      if [ "$f" = tasks ]; then git add -A -- "$f"; else git add -f -A -- "$f"; fi
      paths+=("$f")
    fi
  done
  [ "${#paths[@]}" -gt 0 ] || return 0
  git diff --cached --quiet -- "${paths[@]}" || git commit -q -m "relay: loop state — $1" -- "${paths[@]}"
}

apply_builder_commit() {  # the builder writes $COMMIT_FILE (subject, body, 'FILES:' and one path per line); the relay commits exactly those files
  [ -s "$COMMIT_FILE" ] || return 0
  local msg f ok=() files=()
  msg=$(tr -d '\r' < "$COMMIT_FILE" | sed '1s/^\xEF\xBB\xBF//' | sed '/^FILES:[[:space:]]*$/,$d' | awk 'NR == 2 && $0 != "" { print "" } { print }')
  mapfile -t files < <(tr -d '\r' < "$COMMIT_FILE" | sed -n '/^FILES:[[:space:]]*$/,$p' | tail -n +2 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; /^$/d')
  rm -f "$COMMIT_FILE"
  for f in "${files[@]}"; do
    case "$f" in
      "$COMMIT_FILE"|.relay*|relay-*.log|relay-live-*|relay-fallback-*|relay-idle-*|relay-requests/*|"$LOG"|"$TEST_LOG"|"$TASK_FILE"|"$REVIEW_FILE"|"$QUEUE_FILE"|tasks/*|.git/*|/*|[A-Za-z]:*|*..*)
        log "refused to commit '$f' (house rules)"; continue;;
    esac
    if [ -e "$f" ] || git ls-files --error-unmatch -- "$f" >/dev/null 2>&1; then ok+=("$f"); else log "the builder asked to commit '$f', which does not exist"; fi
  done
  # files that may hold secrets are never committed on the AI's say-so: ask the person first
  local held=() keep=() sf
  mapfile -t held < <(printf '%s\n' "${ok[@]}" | python relay-pick.py sensitive | tr -d '\r')
  if [ "${#held[@]}" -gt 0 ]; then
    for f in "${ok[@]}"; do sf=0; for h in "${held[@]}"; do [ "$f" = "$h" ] && sf=1; done; [ "$sf" -eq 0 ] && keep+=("$f"); done
    ok=("${keep[@]}")
    REQ_MESSAGE="$msg" python relay-pick.py req new commit-sensitive "${task:-}" "Save these files in the project history" "The builder listed files that may contain passwords or keys. Once saved in git they are hard to remove." "${held[@]}" >/dev/null
    log "held ${#held[@]} sensitive file(s) for your decision: ${held[*]}"
  fi
  [ -n "$msg" ] && [ "${#ok[@]}" -gt 0 ] || { log "the builder's commit request had no message or no usable files"; return 0; }
  local ident=(); git config user.email >/dev/null 2>&1 || ident=(-c user.name="Agent Relay" -c user.email="agent-relay@localhost")
  git add -A -- "${ok[@]}" && git "${ident[@]}" commit -q -F - -- "${ok[@]}" <<<"$msg" && log "committed for the builder: $(git log -1 --format='%h %s') (${#ok[@]} files)"
}

judge_pending() {  # approvals.mode = auto: an AI judge answers each undecided request (approve, deny, or ask = leave it for the person)
  [ "${S_APPROVALS_MODE:-ask}" = auto ] || return 0
  local id typ _a prompt out v dec reason verb
  while IFS='|' read -r id typ _a; do
    [ -n "$id" ] || continue
    if { [ "$typ" = commit-sensitive ] || [ "$typ" = push ]; } && [ "${S_APPROVALS_SECRETS:-0}" != 1 ]; then continue; fi   # secrets and uploads stay with the person
    prompt=$(python relay-pick.py req judge-prompt "$id" | tr -d '\r')
    if [ -n "${S_JUDGE_PROVIDER:-}" ]; then ROLE_NOW=judge tool_run "$S_JUDGE_PROVIDER" "$prompt" "${S_JUDGE_MODEL:-auto}" "${S_JUDGE_EFFORT:-auto}"; out=$LAST_OUT
    else claude_run reviewer "$prompt"; out=$LAST_OUT; fi
    v=$(printf '%s' "$out" | python relay-pick.py req verdict | tr -d '\r')
    dec=${v%%|*}; reason=${v#*|}
    case "$dec" in
      approve|deny) python relay-pick.py req decide "$id" "$dec" "AI judge" "$reason" >/dev/null; [ "$dec" = approve ] && verb=approved || verb=denied; log "AI judge $verb request $id ($typ): $reason";;
      ask) log "AI judge left request $id for you: $reason";;
      *) log "AI judge gave no usable answer for request $id: it stays for you";;
    esac
  done < <(python relay-pick.py req pending-list | tr -d '\r')
}

handle_requests() {  # handle_requests [wait]: with 'wait', pause (up to APPROVAL_WAIT s) while the person has undecided requests; then carry out what was decided
  local pend waited=0 line id typ rc files msg res by
  judge_pending
  if [ "${1:-}" = wait ]; then
    pend=$(python relay-pick.py req pending | tr -d '\r')
    if [ "${pend:-0}" -gt 0 ]; then log "waiting for your decision on $pend request(s) — open the app and press Accept or Deny (up to ${APPROVAL_WAIT:-900}s)"; fi
    while [ "${pend:-0}" -gt 0 ] && [ "$waited" -lt "${APPROVAL_WAIT:-900}" ]; do sleep 10; waited=$((waited + 10)); pend=$(python relay-pick.py req pending | tr -d '\r'); done
    [ "${pend:-0}" -gt 0 ] && log "$pend request(s) still undecided: carrying on without them"
  fi
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    id=$(python -c "import json,sys;print(json.loads(sys.argv[1])['id'])" "$line" | tr -d '\r')
    typ=$(python -c "import json,sys;print(json.loads(sys.argv[1])['type'])" "$line" | tr -d '\r')
    st=$(python -c "import json,sys;print(json.loads(sys.argv[1])['status'])" "$line" | tr -d '\r')
    mapfile -t files < <(python -c "import json,sys;print(chr(10).join(json.loads(sys.argv[1]).get('files',[])))" "$line" | tr -d '\r')
    msg=$(python -c "import json,sys;print(json.loads(sys.argv[1]).get('message') or 'Save approved files')" "$line" | tr -d '\r')
    by=$(python -c "import json,sys;print(json.loads(sys.argv[1]).get('decidedBy') or 'you')" "$line" | tr -d '\r')
    res=""
    case "$typ:$st" in
      commit-sensitive:approved)
        git add -- "${files[@]}" 2>/dev/null && git commit -q -m "$msg" -- "${files[@]}" 2>/dev/null && res="committed ${files[*]}" || res="could not commit ${files[*]}"
        log "approved by $by — $res";;
      commit-sensitive:denied)
        for f in "${files[@]}"; do grep -qxF "$f" .gitignore 2>/dev/null || echo "$f" >> .gitignore; done
        git add -- .gitignore 2>/dev/null && git commit -q -m "relay: never commit the files you refused (${files[*]})" -- .gitignore 2>/dev/null
        res="added to .gitignore: ${files[*]}"; log "denied by $by — $res";;
      push:approved)
        if git remote get-url origin >/dev/null 2>&1; then
          res=$(git push origin "$BRANCH" 2>&1 | tail -n 3 | tr '\n' ' '); log "approved by $by — push of $BRANCH: $res"
        else res="no online repository is connected (no 'origin')"; log "approved push could not run: $res"; fi;;
      push:denied) res="not pushed"; log "denied by $by — push";;
      *:approved) res="approved: the builder is told in its next prompt"; log "approved by $by — $typ";;
      *:denied) res="denied: the builder is told in its next prompt"; log "denied by $by — $typ";;
    esac
    python relay-pick.py req done "$id" "$res" >/dev/null
  done < <(python relay-pick.py req take | tr -d '\r')
}

HOUSE_RULES="PROJECT RULES:
- AGENTS.md in the repo root is binding: read it first (stack, folder layout, the one command that runs all tests).
- Work only on the current git branch ($BRANCH). Never push, merge, rebase, reset, stash, switch branches, delete branches or force anything.
- No paid services, deployment or publishing. Never put secrets, passwords or keys in files.
- Never weaken or delete a test to make it pass. Never claim a test passed that was not run.
- New values you invent (limits, timings, names the person may care about) are PROPOSED: say so in your final message.
- Never tick a gate whose text starts with HUMAN, and never edit anything under tasks/ or the loop files ($TASK_FILE, $REVIEW_FILE, $QUEUE_FILE): the reviewer owns them.
- Stage nothing yourself: list your files in $COMMIT_FILE and the relay commits them.
- NEEDING PERMISSION: never read, print, copy or commit secrets (.env files, keys, tokens, passwords), never push or upload anything, never install programs system-wide,
  never use paid services or send data to outside services. If a task truly needs one of these, do NOT do it: write a small JSON file relay-requests/<short-name>.json with the keys
  type, action, why and details (type is other, or push to ask for an upload of the work branch; action says what you want to do, why says why the task needs it, details names the files,
  commands or services involved), then carry on with everything else, or start your final message with BLOCKED. The person decides in the app; their decisions are listed at the end of your next prompt."

BUILD_PROMPT="You are the implementer for this project. Follow AGENTS.md exactly.
Continue any existing work on the current task; do not discard edits left by an earlier attempt.
Read $TASK_FILE (the task and its gates) and, if it exists, $REVIEW_FILE. Fix every issue in $REVIEW_FILE first; otherwise work on the open AUTO gates.
For each open AUTO gate, add or extend automated tests so that the command named in the gate proves it, and make sure the project's single test command
($TEST_CMD) runs them. Run that command yourself before you finish and fix every failure.
COMMITTING: do NOT run git add or git commit. When your work and its tests are done, write the file $COMMIT_FILE in the repo root:
  line 1: the commit subject, starting with the task id (for example 'T01: ...')
  then optional body lines
  then a line 'FILES:' followed by one repo-relative path per line: every file you created, changed or deleted, nothing else.
The relay commits exactly those files on $BRANCH, runs the test command, and a reviewer judges the result.
NO BUSYWORK: a commit must change code, tests or test setup that an open gate needs. If the open AUTO gates cannot be advanced that way (they need a
decision from the person, an account, a service, hardware or a test that cannot run here), do NOT write $COMMIT_FILE. Start your final message with
'BLOCKED: <which gate> — <why> — <what the person must do>' instead.
Your final message: what you built, the files in $COMMIT_FILE, and the test results.
For a visual workflow, tell the reviewer how to run the project and capture the final behavior from the tested commit.
$HOUSE_RULES"

# verify_round: run the project's tests for the current task; 0 when they pass and the review may start. Uses: task, TEST_CMD, TEST_LOG. Sets: suite, test_rc.
verify_round() {
  if [ -n "$TEST_CMD" ]; then
    timeout --kill-after=60 3600 bash -c "$TEST_CMD" > "$TEST_LOG" 2>&1; test_rc=$?
    if [ "$test_rc" -eq 0 ]; then suite="PASS"; else suite="FAIL (exit $test_rc)"; fi
  else suite="not run (no test command in relay.project.json)"; : > "$TEST_LOG"; fi
  if [ "${test_rc:-1}" -ne 0 ]; then
    relay_runtime event blocked "Tests did not pass for $task; evidence cannot close automatic gates"
    log "tests did not pass; task retained for a fix, without accepting a review"
    return 1
  fi
  relay_runtime checkpoint reviewing "$task" "$TEST_CMD" 0 || return 1
}

# review_round: the reviewer judges the round ($before..$after) and updates the task file; 0 when the review was accepted. Uses: round, task, task_path, before, after, suite, builder_said.
review_round() {
  dirty=$(git status --porcelain | grep -vE "relay|CURRENT_TASK|REVIEW.md|PLAYTEST_QUEUE|^\?\? \.relay" | head -n 20)
  # who built this task and with which model: the runtime's call records (the model the tool reported), else the builder identity file
  built_by=$(relay_runtime summary 2>/dev/null | tr -d '\r' | python -c '
import json, sys
try: s = json.load(sys.stdin)
except ValueError: s = {}
seen = []
for c in s.get("calls", []):
    if c.get("role") == "builder" and c.get("task") == s.get("task") and c.get("outcome") == "success":
        m = c.get("reportedModel") or c.get("requestedModel")
        e = "%s (%s)" % (c.get("provider"), "chosen automatically by Copilot" if m == "copilot-auto" else m if m and m != "auto" else "model not reported")
        if e not in seen: seen.append(e)
print(", ".join(seen))' 2>/dev/null)
  [ -n "$built_by" ] || built_by=$(awk -F'|' 'NF >= 2 { m = ($2 == "" || $2 == "auto") ? "model not reported" : $2; s = s (s ? ", " : "") $1 " (" m ")" } END { print s }' relay-builder.identity 2>/dev/null | tr -d '\r')
  log "round $round — test command $suite; Claude reviewing"

  task_reviews=$(( $(git log --format=%s | awk -v r="relay: loop state — review $task" -v d="relay: loop state — dispatch $task" \
    '$0 == d { exit } $0 == r { n++ } END { print n + 0 }') + 1 ))
  backup=$(mktemp 2>/dev/null || echo ".relay-task-backup"); cp "$task_path" "$backup" 2>/dev/null || : > "$backup"
  claude_run reviewer "THIS ROUND: task '$task' (file $task_path) — review number $task_reviews for this task (limit $MAX_TASK_ROUNDS).
Commits by the implementer: $before..$after ($(git rev-list --count "$before..$after") commits).
Built by: ${built_by:-unknown}
Test command ($TEST_CMD) on HEAD $(git rev-parse --short HEAD): $suite.
Last test lines:
$(tail -n 12 "$TEST_LOG")
Uncommitted files the implementer left (should be none): ${dirty:-none}
Implementer's last words:
$builder_said

You are the reviewer. Do not write application code. Read $TASK_FILE and inspect this round's commits (git log / git show over the range above).
Never trust the implementer's claims: the test output above and $TEST_LOG are this run's evidence.
You get ONE turn: do not start background commands, monitors, schedules or wake-ups, and do not finish until every file update below is done.
Update the task file $task_path directly:
  1. Tick '- [x]' only the AUTO gates whose named check passed in this run's evidence; untick AUTO gates whose check failed or is missing.
     NEVER tick a gate whose text starts with HUMAN.
  2. Rewrite its 'Status:' line as: 'Status: In progress | Completion: P% (a/N gates)' (P = ticked gates / all gates, rounded). When only HUMAN gates are open
     use 'Status: Waiting for you | Completion: P% (a/N gates)'. Never write 'Done': only the person finishes a task.
  3. Append to the end of the file, under a '## Review log' heading (create it if missing): 'Relay review <date> — commit <hash> — tests PASS/FAIL — gates a/N — <one sentence>'. Start that sentence with 'Built by <who>.' (the 'Built by' line above: the AI and its model).
Then decide:
  - Open AUTO gates remain: overwrite $REVIEW_FILE with a numbered list of concrete, verifiable issues (what to fix, or the next piece of the open gate).
    Its first line is 'failed reviews: N'. Increment N ONLY for a FAILED round (tests red, no commit, a house rule broken, an earlier issue not fixed,
    or a commit that changed nothing the gates need). A green, committed round with real progress keeps N unchanged.
  - BLOCKED: if the implementer's message starts with 'BLOCKED:' and you agree no open AUTO gate can be advanced by code or tests that run here, or if N reaches 3,
    append '<task id> — <name> — NEEDS OWNER: <reason>' to $QUEUE_FILE and delete $TASK_FILE and $REVIEW_FILE.
  - Round limit: if this is review number $MAX_TASK_ROUNDS or more for this task and AUTO gates are still open, do the same with the reason 'still open after <n> rounds: <what is left>'.
  - All AUTO gates pass and only HUMAN gates remain: append '<task id> — <name> — <exactly what the person should try and what to judge>' to $QUEUE_FILE and delete $TASK_FILE and $REVIEW_FILE.
  - For a visual HUMAN gate, capture a fresh screenshot or clip from the tested build if the project's tooling supports it. Keep the capture under .relay/captures and attach it with:
    python relay-runtime.py --build $(git rev-parse HEAD) artifact <task-id> <repository-relative-file> <short-note>
    Attach only footage you verified came from that build. If you cannot capture it, give the person concrete playthrough steps in the queue entry.
$HOUSE_RULES" || { relay_runtime restore; relay_runtime event blocked "Review failed; task restored for retry"; log "reviewer failed: keeping the current task for a retry"; return 1; }
  relay_runtime guard "$TEST_CMD" || { log "review evidence rejected; task retained"; return 1; }
  restored=$(python relay-pick.py guard "$task_path" "$backup" 2>/dev/null)
  [ -z "$restored" ] || log "the reviewer ticked a HUMAN gate — put back: $(tr '\n' ' ' <<<"$restored")"
  rm -f "$backup" 2>/dev/null
  commit_state "review $task"
  log "round $round — review done: $(git log -1 --format=%s)"
  relay_runtime checkpoint idle "$task" "$TEST_CMD" || return 1
}

# ---------------------------------------------------------------- start
cfg_load
RESUME_PHASE=$(relay_runtime resume "$TEST_CMD") || { log "checkpoint recovery failed; nothing was discarded"; exit 1; }
[ "$RESUME_PHASE" != blocked ] || { log "checkpoint needs attention: open Relay's activity panel"; exit 1; }
log "agents: Claude ${CLAUDE_MODEL:-$S_DISPATCHER_MODEL}, effort ${CLAUDE_EFFORT:-$S_DISPATCHER_EFFORT}, advisor ${CLAUDE_ADVISOR:-$S_ADVISOR} (reviewer); tasks come from tasks/*.md (relay-pick.py, no AI)"
log "relay started on branch $BRANCH (task files; test command: ${TEST_CMD:-none})"
if [ "$RESUME_PHASE" = idle ] && [ -n "$TEST_CMD" ] && python relay-pick.py done T00 . ; then
  log "test command: $TEST_CMD"
  log "running baseline test suite (about a few minutes)"
  timeout --kill-after=60 3600 bash -c "$TEST_CMD" > "$TEST_LOG" 2>&1; base_rc=$?
  if [ "$base_rc" -ne 0 ]; then log "baseline tests FAIL on $BRANCH — fix them first. Reviews are meaningless on a red baseline."; tail -n 15 "$TEST_LOG" | tee -a "$LOG" >/dev/null; exit 1; fi
  log "baseline tests pass"
elif [ "$RESUME_PHASE" = idle ]; then
  log "baseline skipped: T00 (the project skeleton and the test command) is not done yet"
fi

stalls=0; round=0
while true; do
  round=$((round + 1))
  if [ "$MAX_ROUNDS" -gt 0 ] && [ "$round" -gt "$MAX_ROUNDS" ]; then log "reached MAX_ROUNDS=$MAX_ROUNDS — stopping"; exit 0; fi

  if [ "$RESUME_PHASE" = idle ] && [ ! -s "$TASK_FILE" ]; then
    cfg_load
    # parallel mode: when it cannot run (fewer than two builders or tasks that can go together) this round falls through to one task at a time
    if [ "${S_MODE:-serial}" = parallel ] && declare -F parallel_round >/dev/null && parallel_round; then continue; fi
    log "round $round — choosing next task from tasks/"
    python relay-pick.py pick . >/dev/null
  fi
  if grep -qx 'NO_TASKS' "$TASK_FILE" 2>/dev/null; then
    rm -f "$TASK_FILE"; commit_state "no tasks"
    log "no task the agents can advance — everything is done or waiting for you (see $QUEUE_FILE). Stopping."
    exit 0
  fi
  task=$(sed -n 2p "$TASK_FILE"); task_path=$(sed -n 1p "$TASK_FILE")
  [ "$RESUME_PHASE" != idle ] || commit_state "dispatch $task"

  # the task's recommended AI (planner's 'Recommended AI:' line) is used while the builder in the settings is 'auto'; never the reviewer's own model
  TASK_BUILDER_PROVIDER=""; TASK_BUILDER_MODEL=""; TASK_BUILDER_EFFORT=""
  cfg_load
  IFS='|' read -r rec_p rec_m rec_e < <(python relay-pick.py rec "$TASK_FILE" . 2>/dev/null | tr -d '\r')
  if [ -n "${rec_p:-}" ]; then
    ok_rec=1
    { [ "$rec_p" = "$S_REVIEWER_PROVIDER" ] && { [ -z "${rec_m:-}" ] || [ "$rec_m" = "$S_REVIEWER_MODEL" ]; }; } && { ok_rec=0; log "round $round — recommended $rec_p ${rec_m:-} skipped: the reviewer must be a different model"; }
    case "$rec_p" in
      claude) command -v "${CLAUDE_BIN:-claude}" >/dev/null 2>&1 || ok_rec=0;;
      codex) [ -n "${CODEX_BIN:-}" ] || ok_rec=0;;
      copilot) [ -n "${COPILOT_BIN:-}" ] || ok_rec=0;;
      opencode) command -v "${OPENCODE_BIN:-opencode}" >/dev/null 2>&1 || ok_rec=0;;
    esac
    [ "$ok_rec" -eq 1 ] && { TASK_BUILDER_PROVIDER=$rec_p; TASK_BUILDER_MODEL=${rec_m:-}; TASK_BUILDER_EFFORT=${rec_e:-}; log "round $round — recommended AI for $task: $rec_p ${rec_m:-} ${rec_e:+($rec_e reasoning)}"; }
  fi
  cfg_load
  if [ "$S_BUILDER_PROVIDER" = codex ]; then codex_pick; BUILDER_DESC="Codex model: $CODEX_PICK_DESC"; BUILDER_WHO=Codex
  else BUILDER_DESC="builder: $S_BUILDER_PROVIDER (${S_BUILDER_MODEL})"; BUILDER_WHO="$S_BUILDER_PROVIDER builder"; fi
  case "$RESUME_PHASE" in
    idle|building)
      rm -f "$COMMIT_FILE"
      LAST_OUT=""
      handle_requests
      relay_runtime checkpoint building "$task" "$TEST_CMD" || exit 1
      log "round $round — $BUILDER_DESC"
      log "round $round — $task — $BUILDER_WHO building"
      before=$(git rev-parse HEAD)
      builder_run "$BUILD_PROMPT
$(python relay-pick.py req prompt | tr -d '\r')" || { relay_runtime event blocked "Builder could not finish $task"; exit 1; }
      builder_said=$(tail -n 40 <<<"$LAST_OUT")
      ;;
    *)
      before=$(relay_runtime summary | python -c 'import json,sys; print(json.load(sys.stdin).get("beforeHead","HEAD"))')
      builder_said="Recovered from the task checkpoint; inspect the recorded commits and call history."
      ;;
  esac
  case "$RESUME_PHASE" in
    idle|building|build-ready)
      apply_builder_commit
      handle_requests wait
      after=$(git rev-parse HEAD)
      log "round $round — saved $(git rev-list --count "$before..$after") build commit(s); running the full suite for the review"
      relay_runtime checkpoint testing "$task" "$TEST_CMD" || exit 1
      ;;
    *) after=$(git rev-parse HEAD);;
  esac

  if [ "$RESUME_PHASE" != reviewing ]; then verify_round || exit 1; else suite=PASS; fi
  RESUME_PHASE=idle
  review_round || exit 1

  if [ "$after" = "$before" ] && ! grep -q '^BLOCKED:' <<<"$builder_said"; then
    stalls=$((stalls + 1)); log "no new commits from the builder this round ($stalls of $MAX_STALLS)"
    if [ "$stalls" -ge "$MAX_STALLS" ]; then log "stalled: $MAX_STALLS rounds in a row without a new commit — stopping"; exit 2; fi
  else stalls=0; fi
done
