#!/usr/bin/env bash
# relay-lib.sh - shared by relay-files.sh: usage-limit waits, role settings, agent runners, fallback builders. Sourced, never run.
# Needs from the caller: log, LOG, LIMIT_RE, SLEEP_ON_LIMIT, AGENT_TIMEOUT, TASK_FILE, CODEX_BIN, CODEX_ARGS, CODEX_MODELS_CACHE, CODEX_PICK, CLAUDE_MODEL/EFFORT/ADVISOR.
# seconds to sleep after a usage-limit hit: the reset time the CLI reports ("try again at 11:39 AM" or
# "try again in 2 hours 5 minutes") plus a 60 s margin, at most 8 h; $SLEEP_ON_LIMIT when the text names no time.
limit_wait() {
  LIMIT_TXT="$1" python -c '
import os, re, sys, datetime
t = os.environ.get("LIMIT_TXT", ""); dflt = int(sys.argv[1]); secs = None
m = re.search(r"try again at (\d{1,2}):(\d{2})\s*([AP])M", t, re.I)
if m:
    h = int(m.group(1)) % 12 + (12 if m.group(3).upper() == "P" else 0)
    now = datetime.datetime.now(); tgt = now.replace(hour=h, minute=int(m.group(2)), second=0, microsecond=0)
    if tgt <= now: tgt += datetime.timedelta(days=1)
    secs = int((tgt - now).total_seconds())
else:
    m = re.search(r"try again in (?:(\d+)\s*h\w*)?[ ,and]*(?:(\d+)\s*m\w*)?", t, re.I)
    if m and (m.group(1) or m.group(2)): secs = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
print(dflt if secs is None else max(120, min(secs + 60, 28800)))
' "$SLEEP_ON_LIMIT" 2>/dev/null || echo "$SLEEP_ON_LIMIT"
}

# ---- settings (relay.settings.json, read by relay-settings.py before every agent call: a change needs no restart)
# Defaults below reproduce the behaviour from before the settings file existed. Env vars (CLAUDE_MODEL, CODEX_MODEL, FALLBACKS, ...) win.
S_DISPATCHER_PROVIDER=claude; S_DISPATCHER_MODEL=claude-sonnet-5-5; S_DISPATCHER_EFFORT=high
S_BUILDER_PROVIDER=codex; S_BUILDER_MODEL=auto; S_BUILDER_EFFORT=auto
S_REVIEWER_PROVIDER=claude; S_REVIEWER_MODEL=claude-sonnet-5-5; S_REVIEWER_EFFORT=high
S_CLEANER_PROVIDER=none; S_FALLBACKS="copilot opencode"; S_FALLBACK_MAX=10
S_COPILOT_MODEL=auto; S_OPENCODE_MODEL=opencode/deepseek-v4.1-flash-free; S_ADVISOR=claude-opus-5-5; S_IDLE_CHORES="backend digest"
LAST_CFG_MSG=""
cfg_load() {
  local out msgs
  msgs=$(mktemp 2>/dev/null || echo ".relay-settings.err")
  if out=$(python relay-settings.py shell relay.settings.json 2>"$msgs") && [ -n "$out" ]; then eval "$out"; fi
  if [ -s "$msgs" ] && [ "$(cat "$msgs")" != "$LAST_CFG_MSG" ]; then LAST_CFG_MSG=$(cat "$msgs"); while IFS= read -r l; do log "$l"; done < "$msgs"; fi
  [ "$msgs" = ".relay-settings.err" ] || : > "$msgs"
  # the task's own recommendation (relay-files.sh sets TASK_BUILDER_*) applies while the builder model in the settings is 'auto'
  if [ -n "${TASK_BUILDER_PROVIDER:-}" ] && [ "$S_BUILDER_MODEL" = auto ]; then
    S_BUILDER_PROVIDER=$TASK_BUILDER_PROVIDER; S_BUILDER_MODEL=${TASK_BUILDER_MODEL:-auto}; S_BUILDER_EFFORT=${TASK_BUILDER_EFFORT:-auto}
  fi
}

limit_sleep() {  # limit_sleep <label> <text with the limit message>: wait for the reset the tool reported, doing the idle chores meanwhile
  local label=$1 wait_s idle_t0 idle_spent
  wait_s=$(limit_wait "$2")
  log "$label hit a usage limit — sleeping ${wait_s}s (until about $(date -d "+${wait_s} seconds" '+%H:%M'))"
  idle_t0=$(date +%s)
  IDLE_CHORES="${IDLE_CHORES:-$S_IDLE_CHORES}" bash relay-idle.sh "$wait_s" 2>&1 | while IFS= read -r l; do log "$l"; done   # token-free chores while every builder is out of usage
  idle_spent=$(( $(date +%s) - idle_t0 ))
  [ "$idle_spent" -lt "$wait_s" ] && sleep $(( wait_s - idle_spent ))
  return 0
}

run() {  # run <label> <prompt> <command...> — the prompt goes in on stdin; retries while rate-limited
  # The agent's output goes to relay-live-<label>.log as it happens (relay-status.ps1 shows it live) and its
  # start time to relay-live-<label>.start (0 once it has finished); the full text is still appended to relay.log.
  local label=$1 prompt=$2; shift 2
  local live="relay-live-$label.log"
  while true; do
    local out rc
    date +%s > "relay-live-$label.start"
    printf '%s\n' "$prompt" | timeout --kill-after=60 "$AGENT_TIMEOUT" "$@" > "$live" 2>&1; rc=${PIPESTATUS[1]}
    echo 0 > "relay-live-$label.start"
    if [ "$label" = claude ]; then out=$(python relay-claude-result.py < "$live"); else out=$(cat "$live"); fi
    printf '%s\n' "===== $label  $(date '+%F %T')  rc=$rc =====" "$out" >> "$LOG"
    LAST_OUT=$out; LAST_RC=$rc
    if tail -n 8 <<<"$out" | grep -qiE "$LIMIT_RE"; then
      if [ "${ROLE_NOW:-}" = builder ] && fallback_build "$prompt"; then return 0; fi   # build with the next tool instead of waiting
      limit_sleep "$label" "$(tail -n 8 <<<"$out")"; continue
    fi
    [ "$rc" -eq 124 ] && log "$label timed out after ${AGENT_TIMEOUT}s"
    [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ] && log "$label exited with rc=$rc (see $LOG)"
    return 0
  done
}

# Fallback and alternative builders (owner, 17 Nov 2026). tool_run runs one prompt through codex, copilot or opencode; the hard rules are
# enforced with deny lists (no push, merge, rebase, reset, stash, checkout, switch, branch, clean, worktree). When the builder is out of
# usage the build goes to the next tool in the fallback chain instead of sleeping.
GIT_DENY=(push merge rebase reset stash checkout switch branch clean worktree)
if [ -z "${COPILOT_BIN:-}" ]; then COPILOT_BIN=$(ls -d "$HOME"/AppData/Local/github-copilot-sdk/cli/*/copilot.exe 2>/dev/null | sort -V | tail -n 1); fi

tool_run() {  # tool_run <tool> <prompt> <model> [effort] — 0 ran, 1 tool not available, 2 out of usage. Output in relay-live-<tool>.log, LAST_OUT, LAST_RC.
  local tool=$1 prompt=$2 model=${3:-} effort=${4:-} cmd=() live rc out g deny
  live="relay-live-$tool.log"
  case "$tool" in
    codex)
      [ -n "${CODEX_BIN:-}" ] || return 1
      cmd=("$CODEX_BIN" exec); [ -n "$model" ] && [ "$model" != auto ] && cmd+=(-m "$model")
      [ -n "$effort" ] && [ "$effort" != auto ] && cmd+=(-c "model_reasoning_effort=$effort"); cmd+=("${CODEX_ARGS[@]}" -);;
    copilot)
      [ -n "$COPILOT_BIN" ] && [ -x "$COPILOT_BIN" ] || return 1
      [ -n "$model" ] && [ "$model" != auto ] || model=${COPILOT_MODEL:-$S_COPILOT_MODEL}
      printf '%s\n' "$prompt" > relay-fallback-prompt.txt
      cmd=("$COPILOT_BIN" -p "Your complete task is in the file relay-fallback-prompt.txt in the current folder. Read it and carry it out exactly, following every rule in it. Never commit that file." --allow-all --model "$model" -s)
      for g in "${GIT_DENY[@]}"; do cmd+=("--deny-tool=shell(git $g)"); done;;
    opencode)
      command -v opencode >/dev/null || return 1
      [ -n "$model" ] && [ "$model" != auto ] || model=${OPENCODE_MODEL:-$S_OPENCODE_MODEL}
      cmd=(opencode run --auto -m "$model");;
    *)  # a custom AI the person added (relay.providers.json): its command comes from the settings helper
      local spec=()
      mapfile -t spec < <(python relay-settings.py cmd relay.settings.json "$tool" "${model:-auto}" "${effort:-auto}" 2>/dev/null | tr -d '\r')
      [ "${#spec[@]}" -ge 2 ] || return 1
      cmd=("${spec[@]:1}")
      command -v "${cmd[0]}" >/dev/null 2>&1 || [ -x "${cmd[0]}" ] || return 1
      [ "${spec[0]}" = FILE ] && printf '%s\n' "$prompt" > relay-fallback-prompt.txt;;
  esac
  deny='"*":"allow"'; for g in "${GIT_DENY[@]}"; do deny="$deny,\"git $g*\":\"deny\""; done
  date +%s > "relay-live-$tool.start"
  printf '%s\n' "$prompt" | OPENCODE_CONFIG_CONTENT="{\"permission\":{\"bash\":{$deny}}}" timeout --kill-after=60 "$AGENT_TIMEOUT" "${cmd[@]}" > "$live" 2>&1; rc=${PIPESTATUS[1]}
  echo 0 > "relay-live-$tool.start"
  out=$(cat "$live")
  printf '%s\n' "===== $tool  $(date '+%F %T')  rc=$rc =====" "$out" >> "$LOG"
  LAST_OUT=$out; LAST_RC=$rc
  [ "$rc" -eq 124 ] && log "$tool timed out after ${AGENT_TIMEOUT}s"
  if tail -n 8 <<<"$out" | grep -qiE "$LIMIT_RE"; then return 2; fi
  return 0
}

fallback_build() {  # fallback_build <prompt> — 0 when some fallback ran the build (the caller then skips the sleep), 1 when none could
  local prompt=$1 tool cnt_file day n chain max rc
  chain="${FALLBACKS-$S_FALLBACKS}"; max="${FALLBACK_MAX_PER_DAY:-$S_FALLBACK_MAX}"
  for tool in $chain; do
    [ "$tool" = "$S_BUILDER_PROVIDER" ] && continue
    cnt_file="relay-fallback-$tool.count"; day=$(date +%F); n=0
    [ -f "$cnt_file" ] && [ "$(head -n 1 "$cnt_file")" = "$day" ] && n=$(sed -n 2p "$cnt_file")
    if [ "${n:-0}" -ge "$max" ]; then log "fallback $tool skipped: $n builds today (max $max)"; continue; fi
    log "$S_BUILDER_PROVIDER is out of usage — fallback builder: $tool, build $((n+1)) of $max today"
    tool_run "$tool" "$prompt" auto; rc=$?
    if [ "$rc" -eq 1 ]; then log "fallback $tool skipped: not installed"; continue; fi
    if [ "$rc" -eq 2 ]; then log "fallback $tool is out of usage too"; continue; fi
    printf '%s\n%s\n' "$day" "$((n+1))" > "$cnt_file"
    return 0
  done
  return 1
}

builder_run() {  # builder_run <prompt> — the builder role (settings), then the fallback chain, then a wait for the reset
  local rc
  cfg_load; ROLE_NOW=builder
  if [ "$S_BUILDER_PROVIDER" = codex ]; then codex_run "$1"; ROLE_NOW=""; return 0; fi
  while true; do
    tool_run "$S_BUILDER_PROVIDER" "$1" "$S_BUILDER_MODEL" "$S_BUILDER_EFFORT"; rc=$?
    if [ "$rc" -eq 2 ]; then fallback_build "$1" && break; limit_sleep "$S_BUILDER_PROVIDER" "$(tail -n 8 <<<"$LAST_OUT")"; continue; fi
    [ "$rc" -eq 1 ] && { log "builder $S_BUILDER_PROVIDER is not installed"; LAST_OUT=""; LAST_RC=1; }
    break
  done
  ROLE_NOW=""
}

# both CLIs read the prompt from stdin: long multi-line prompts survive Windows argument quoting
# claude -p gets one turn: tools that wait or schedule for later would end it before the work is done.
claude_run() {  # claude_run <dispatcher|reviewer> <prompt> — the role's provider from the settings (Claude unless marked experimental)
  local role=$1 prov model effort
  cfg_load; ROLE_NOW=""
  if [ "$role" = dispatcher ]; then prov=$S_DISPATCHER_PROVIDER; model=${CLAUDE_MODEL:-$S_DISPATCHER_MODEL}; effort=${CLAUDE_EFFORT:-$S_DISPATCHER_EFFORT}
  else prov=$S_REVIEWER_PROVIDER; model=${CLAUDE_MODEL:-$S_REVIEWER_MODEL}; effort=${CLAUDE_EFFORT:-$S_REVIEWER_EFFORT}; fi
  if [ "$prov" = claude ]; then
    run claude "$2" "${CLAUDE_BIN:-claude}" -p --model "$model" --effort "$effort" --settings "{\"advisorModel\":\"${CLAUDE_ADVISOR:-$S_ADVISOR}\"}"   --output-format stream-json --verbose --dangerously-skip-permissions --disallowedTools ScheduleWakeup CronCreate Monitor RemoteTrigger
    return 0
  fi
  local rc
  while true; do   # experimental: another provider plays this role
    tool_run "$prov" "$2" "$model" "$effort"; rc=$?
    if [ "$rc" -eq 2 ]; then limit_sleep "$prov" "$(tail -n 8 <<<"$LAST_OUT")"; continue; fi
    [ "$rc" -eq 1 ] && { log "$role provider $prov is not installed"; LAST_OUT=""; LAST_RC=1; }
    break
  done
}
codex_run()  { run codex  "$1" "$CODEX_BIN" exec "${CODEX_PICK[@]}" "${CODEX_ARGS[@]}" -; }

codex_pick() {  # choose Codex's model and reasoning effort from this task's 'RECOMMENDED_AI:' line
  CODEX_PICK=(); CODEX_PICK_DESC="config default"
  local rec model effort
  rec=$(grep -a -m1 '^RECOMMENDED_AI:' "$TASK_FILE" 2>/dev/null | sed 's/^RECOMMENDED_AI:[[:space:]]*//; s/
$//')
  local fixed_model="" fixed_effort=""
  [ "$S_BUILDER_PROVIDER" = codex ] && [ "$S_BUILDER_MODEL" != auto ] && fixed_model=$S_BUILDER_MODEL
  [ "$S_BUILDER_PROVIDER" = codex ] && [ "$S_BUILDER_EFFORT" != auto ] && fixed_effort=$S_BUILDER_EFFORT
  model="${CODEX_MODEL:-${fixed_model:-$(grep -aoiE 'gpt-[0-9.]+-(astra|luna|sol|terra)' <<<"$rec" | head -n 1 | tr 'A-Z' 'a-z')}}"
  # owner, 17 Nov 2026: astra costs too many tokens, so tasks that recommend it run on CODEX_ASTRA_AS (default gpt-6.1-sol)
  # optional cost rule: CODEX_ASTRA_AS=gpt-6.1-sol runs tasks that name an astra model on that cheaper model instead
  case "$model" in *-astra) [ -n "${CODEX_ASTRA_AS:-}" ] && { log "task recommends $model; using $CODEX_ASTRA_AS instead (token cost)"; model="$CODEX_ASTRA_AS"; };; esac
  effort="${CODEX_EFFORT:-${fixed_effort:-$(grep -aoiE '(low|medium|high|xhigh|max)[[:space:]]+reasoning' <<<"$rec" | head -n 1 | awk '{print tolower($1)}')}}"
  if [ -n "$model" ] && [ -f "$CODEX_MODELS_CACHE" ] && ! grep -aqF "\"$model\"" "$CODEX_MODELS_CACHE"; then
    log "the task recommends '$model', which this Codex does not list — using the config default instead"; model=""
  fi
  [ -n "$model" ] && CODEX_PICK+=(-m "$model")
  [ -n "$effort" ] && CODEX_PICK+=(-c "model_reasoning_effort=$effort")
  CODEX_PICK_DESC="${model:-config default} / reasoning ${effort:-config default}"
  [ -n "$rec" ] || CODEX_PICK_DESC="$CODEX_PICK_DESC (no RECOMMENDED_AI line in $TASK_FILE)"
}

