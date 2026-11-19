#!/usr/bin/env bash
# relay-lib.sh - shared by relay-files.sh: usage-limit waits, role settings, agent runners, fallback builders. Sourced, never run.
# Needs from the caller: log, LOG, LIMIT_RE, SLEEP_ON_LIMIT, AGENT_TIMEOUT, TASK_FILE, CODEX_BIN, CODEX_ARGS, CODEX_MODELS_CACHE, CODEX_PICK, CLAUDE_MODEL/EFFORT/ADVISOR.
# Keep the reported reset for provider cooldowns; individual sleeps are capped at eight hours.
limit_wait() {
  printf '%s' "$1" | python "$(dirname "${BASH_SOURCE[0]}")/relay-limits.py" "$SLEEP_ON_LIMIT" 2>/dev/null || echo "$SLEEP_ON_LIMIT"
}

# ---- settings (relay.settings.json, read by relay-settings.py before every agent call: a change needs no restart)
# Defaults below reproduce the behaviour from before the settings file existed. Env vars (CLAUDE_MODEL, CODEX_MODEL, FALLBACKS, ...) win.
S_DISPATCHER_PROVIDER=claude; S_DISPATCHER_MODEL=claude-sonnet-5-5; S_DISPATCHER_EFFORT=high
S_BUILDER_PROVIDER=codex; S_BUILDER_MODEL=auto; S_BUILDER_EFFORT=auto
S_REVIEWER_PROVIDER=claude; S_REVIEWER_MODEL=claude-sonnet-5-5; S_REVIEWER_EFFORT=high
S_CLEANER_PROVIDER=none; S_FALLBACKS="copilot opencode"; S_FALLBACK_MAX=10
S_DISPATCHER_FALLBACKS="codex copilot opencode"; S_REVIEWER_FALLBACKS="copilot opencode codex"
S_COPILOT_MODEL=auto; S_OPENCODE_MODEL=opencode/deepseek-v4.1-flash-free; S_ADVISOR=claude-opus-5-5; S_IDLE_CHORES="backend digest"
LAST_CFG_MSG=""
relay_runtime() {
  [ -f relay-runtime.py ] || return 0
  python relay-runtime.py "$@"
}
role_eligible() {
  local reason
  if reason=$(relay_runtime eligible "$1" "$2" "${3:-auto}" 2>&1); then return 0; fi
  log "$1 skips $2: $reason"
  relay_runtime event routing "$1 skipped $2: $reason" >/dev/null 2>&1
  return 1
}
relay_cli_path() {
  local resolved
  resolved=$(command -v "$1") || return 0
  if command -v cygpath >/dev/null; then cygpath -aw "$resolved"; else printf '%s\n' "$resolved"; fi
}
record_call_end() {
  local call_id=$1 rc=$2 outcome=success
  [ -n "$call_id" ] || return 0
  if tail -n 8 <<<"$LAST_OUT" | grep -qiE "$LIMIT_RE"; then outcome=quota
  elif [ "$rc" -eq 124 ]; then outcome=timeout
  elif [ "$rc" -ne 0 ]; then outcome=error; fi
  relay_runtime finish "$call_id" "$rc" "$outcome"
}
cfg_load() {
  local out msgs
  msgs=$(mktemp 2>/dev/null || echo ".relay-settings.err")
  if out=$(python relay-settings.py shell relay.settings.json 2>"$msgs") && [ -n "$out" ]; then eval "$out"; fi
  if [ "${AGENT_TIMEOUT_OVERRIDE+x}" = x ]; then AGENT_TIMEOUT="${AGENT_TIMEOUT_OVERRIDE:-${S_AGENT_TIMEOUT:-5400}}"; fi
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
  [ "$wait_s" -le 28800 ] || wait_s=28800
  log "$label hit a usage limit — sleeping ${wait_s}s (retry about $(date -d "+${wait_s} seconds" '+%H:%M'))"
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
    local out rc call_id=""
    local access_cli
    access_cli=$(relay_cli_path "$1")
    if ! RELAY_ACCESS_CLI="$access_cli" role_eligible "${ROLE_NOW:-builder}" "$label" "${CODEX_PICK_MODEL:-auto}"; then
      if [ "${ROLE_NOW:-}" = builder ] && fallback_build "$prompt"; then return 0; fi
      LAST_RC=1; relay_runtime event blocked "No eligible builder for this task"; return 1
    fi
    if [ -f relay-runtime.py ]; then
      call_id=$(RELAY_ACCESS_CLI="$access_cli" relay_runtime begin "${ROLE_NOW:-builder}" "$label" "${CODEX_PICK_MODEL:-auto}" "$live") || return 1
    fi
    date +%s > "relay-live-$label.start"
    printf '%s\n' "$prompt" | timeout --kill-after=60 "${AGENT_TIMEOUT:-$S_AGENT_TIMEOUT}" "$@" > "$live" 2>&1; rc=${PIPESTATUS[1]}
    echo 0 > "relay-live-$label.start"
    if [ "$label" = claude ]; then out=$(python relay-claude-result.py < "$live"); else out=$(cat "$live"); fi
    printf '%s\n' "===== $label  $(date '+%F %T')  rc=$rc =====" "$out" >> "$LOG"
    LAST_OUT=$out; LAST_RC=$rc
    record_call_end "$call_id" "$rc" || return 1
    if [ "${ROLE_NOW:-}" = builder ]; then printf '%s|%s\n' "$label" "${CODEX_PICK_MODEL:-auto}" >> relay-builder.identity; fi
    if tail -n 8 <<<"$out" | grep -qiE "$LIMIT_RE"; then
      provider_limited "$label" "$(tail -n 8 <<<"$out")"
      if [ "${ROLE_NOW:-}" = builder ] && fallback_build "$prompt"; then return 0; fi   # build with the next tool instead of waiting
      limit_sleep "$label" "$(tail -n 8 <<<"$out")"; continue
    fi
    [ "$rc" -eq 124 ] && log "$label timed out after ${AGENT_TIMEOUT}s"
    [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ] && log "$label exited with rc=$rc (see $LOG)"
    if [ "$rc" -ne 0 ]; then
      if [ "${ROLE_NOW:-}" = builder ] && fallback_build "$prompt"; then return 0; fi
      return 1
    fi
    return 0
  done
}

# Fallback and alternative builders (owner, 17 Nov 2026). tool_run runs one prompt through codex, copilot or opencode; the hard rules are
# enforced with deny lists (no push, merge, rebase, reset, stash, checkout, switch, branch, clean, worktree). When the builder is out of
# usage the build goes to the next tool in the fallback chain instead of sleeping.
GIT_DENY=(push merge rebase reset stash checkout switch branch clean worktree)
if [ -z "${COPILOT_BIN:-}" ]; then COPILOT_BIN=$(ls -d "$HOME"/AppData/Local/github-copilot-sdk/cli/*/copilot.exe 2>/dev/null | sort -V | tail -n 1); fi

tool_run() {  # 0 success, 1 unavailable, 2 quota, 3 failed, 4 policy-blocked.
  local tool=$1 prompt=$2 model=${3:-} effort=${4:-} cmd=() live rc out g deny call_id=""
  live="relay-live-$tool.log"
  case "$tool" in
    claude)
      command -v "${CLAUDE_BIN:-claude}" >/dev/null 2>&1 || return 1
      [ -n "$model" ] && [ "$model" != auto ] || model=${CLAUDE_MODEL:-claude-sonnet-5-5}
      [ -n "$effort" ] && [ "$effort" != auto ] || effort=high
      cmd=("${CLAUDE_BIN:-claude}" -p --model "$model" --effort "$effort" --settings "{\"advisorModel\":\"${CLAUDE_ADVISOR:-$S_ADVISOR}\"}"
        --output-format stream-json --verbose --dangerously-skip-permissions --disallowedTools ScheduleWakeup CronCreate Monitor RemoteTrigger);;
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
  local access_cli
  access_cli=$(relay_cli_path "${cmd[0]}")
  RELAY_ACCESS_CLI="$access_cli" role_eligible "${ROLE_NOW:-builder}" "$tool" "${model:-auto}" || return 4
  if [ -f relay-runtime.py ]; then
    call_id=$(RELAY_ACCESS_CLI="$access_cli" relay_runtime begin "${ROLE_NOW:-builder}" "$tool" "${model:-auto}" "$live") || return 4
  fi
  deny='"*":"allow"'; for g in "${GIT_DENY[@]}"; do deny="$deny,\"git $g*\":\"deny\""; done
  date +%s > "relay-live-$tool.start"
  printf '%s\n' "$prompt" | OPENCODE_CONFIG_CONTENT="{\"permission\":{\"bash\":{$deny}}}" timeout --kill-after=60 "$AGENT_TIMEOUT" "${cmd[@]}" > "$live" 2>&1; rc=${PIPESTATUS[1]}
  echo 0 > "relay-live-$tool.start"
  if [ "$tool" = claude ]; then out=$(python relay-claude-result.py < "$live"); else out=$(cat "$live"); fi
  printf '%s\n' "===== $tool  $(date '+%F %T')  rc=$rc =====" "$out" >> "$LOG"
  LAST_OUT=$out; LAST_RC=$rc
  record_call_end "$call_id" "$rc" || return 3
  [ "$rc" -eq 124 ] && log "$tool timed out after ${AGENT_TIMEOUT}s"
  if [ "${ROLE_NOW:-}" = builder ]; then printf '%s|%s\n' "$tool" "${model:-auto}" >> relay-builder.identity; fi
  if tail -n 8 <<<"$out" | grep -qiE "$LIMIT_RE"; then provider_limited "$tool" "$(tail -n 8 <<<"$out")"; return 2; fi
  if [ "$rc" -ne 0 ]; then log "$tool exited with rc=$rc (see $LOG)"; return 3; fi
  return 0
}

provider_limited() {
  local wait_s
  wait_s=$(limit_wait "$2")
  printf '%s\n' "$(( $(date +%s) + wait_s ))" > "relay-provider-$1.limit"
}


provider_ready() {
  local until
  until=$(cat "relay-provider-$1.limit" 2>/dev/null || true)
  [[ "$until" =~ ^[0-9]+$ ]] || return 0
  [ "$until" -le "$(date +%s)" ]
}


fallback_build() {  # fallback_build <prompt> — 0 when some fallback ran the build (the caller then skips the sleep), 1 when none could
  local prompt=$1 tool cnt_file day n chain max rc
  chain="${FALLBACKS-$S_FALLBACKS}"; max="${FALLBACK_MAX_PER_DAY:-$S_FALLBACK_MAX}"
  for tool in $chain; do
    [ "$tool" = "$S_BUILDER_PROVIDER" ] && continue
    provider_ready "$tool" || continue
    cnt_file="relay-fallback-$tool.count"; day=$(date +%F); n=0
    [ -f "$cnt_file" ] && [ "$(head -n 1 "$cnt_file")" = "$day" ] && n=$(sed -n 2p "$cnt_file")
    if [ "${n:-0}" -ge "$max" ]; then log "fallback $tool skipped: $n builds today (max $max)"; continue; fi
    log "$S_BUILDER_PROVIDER is out of usage — fallback builder: $tool, build $((n+1)) of $max today"
    tool_run "$tool" "$prompt" auto; rc=$?
    if [ "$rc" -eq 1 ]; then log "fallback $tool skipped: not installed"; continue; fi
    if [ "$rc" -eq 4 ]; then log "fallback $tool skipped: routing policy"; continue; fi
    printf '%s\n%s\n' "$day" "$((n+1))" > "$cnt_file"
    if [ "$rc" -eq 2 ]; then log "fallback $tool is out of usage too"; continue; fi
    if [ "$rc" -ne 0 ]; then log "fallback $tool failed"; continue; fi
    return 0
  done
  return 1
}

builder_run() {  # builder_run <prompt> — the builder role (settings), then the fallback chain, then a wait for the reset
  local rc
  cfg_load; ROLE_NOW=builder
  : > relay-builder.identity
  if [ "$S_BUILDER_PROVIDER" = codex ]; then codex_run "$1"; rc=$?; ROLE_NOW=""; return "$rc"; fi
  while true; do
    tool_run "$S_BUILDER_PROVIDER" "$1" "$S_BUILDER_MODEL" "$S_BUILDER_EFFORT"; rc=$?
    if [ "$rc" -eq 2 ]; then fallback_build "$1" && break; limit_sleep "$S_BUILDER_PROVIDER" "$(tail -n 8 <<<"$LAST_OUT")"; continue; fi
    if [ "$rc" -ne 0 ]; then
      fallback_build "$1" && { rc=0; break; }
      log "builder $S_BUILDER_PROVIDER could not finish"; LAST_RC=1
    fi
    break
  done
  ROLE_NOW=""
  return "$rc"
}

# both CLIs read the prompt from stdin: long multi-line prompts survive Windows argument quoting
# claude -p gets one turn: tools that wait or schedule for later would end it before the work is done.
reviewer_independent() {
  local provider=$1 model=$2 built_provider built_model
  if [ -s relay-builder.identity ]; then
    while IFS='|' read -r built_provider built_model; do
      if [ "$provider" = "$built_provider" ] && { [ "$model" = auto ] || [ "$built_model" = auto ] || [ "$model" = "$built_model" ]; }; then return 1; fi
    done < relay-builder.identity
  elif [ "$provider" = "$S_BUILDER_PROVIDER" ] && { [ "$model" = auto ] || [ "$S_BUILDER_MODEL" = auto ] || [ "$model" = "$S_BUILDER_MODEL" ]; }; then
    return 1
  fi
  return 0
}

claude_run() {  # public entry point retained for existing project scripts
  local role=$1 prompt=$2 primary model effort chain tool chosen rc wait_for_limit seen eligible_count
  ROLE_NOW="$role"
  while true; do
    cfg_load
    case "$role" in
      dispatcher) primary=$S_DISPATCHER_PROVIDER; model=$S_DISPATCHER_MODEL; effort=$S_DISPATCHER_EFFORT; chain=$S_DISPATCHER_FALLBACKS;;
      reviewer) primary=$S_REVIEWER_PROVIDER; model=$S_REVIEWER_MODEL; effort=$S_REVIEWER_EFFORT; chain=$S_REVIEWER_FALLBACKS;;
      *) log "unknown relay role: $role"; LAST_RC=1; ROLE_NOW=""; return 1;;
    esac
    [ "$primary" != claude ] || { model=${CLAUDE_MODEL:-$model}; effort=${CLAUDE_EFFORT:-$effort}; }
    wait_for_limit=0
    seen=" "
    eligible_count=0
    for tool in $primary $chain; do
      [[ "$seen" != *" $tool "* ]] || continue
      seen="$seen$tool "
      chosen=$model
      if [ "$tool" != "$primary" ]; then
        case "$tool" in
          claude) chosen=${CLAUDE_MODEL:-claude-sonnet-5-5};;
          copilot) chosen=${COPILOT_MODEL:-$S_COPILOT_MODEL};;
          opencode) chosen=${OPENCODE_MODEL:-$S_OPENCODE_MODEL};;
          *) chosen=auto;;
        esac
      fi
      role_eligible "$role" "$tool" "${chosen:-auto}" || continue
      eligible_count=$((eligible_count+1))
      if ! provider_ready "$tool"; then wait_for_limit=1; log "$role skips $tool until its usage cooldown ends"; continue; fi
      if [ "$role" = reviewer ] && ! reviewer_independent "$tool" "${chosen:-auto}"; then
        log "reviewer skips $tool $chosen: it built this round"
        continue
      fi
      if [ "$tool" != "$primary" ]; then
        local count_file="relay-fallback-$tool.count" day n=0
        day=$(date +%F)
        if [ -f "$count_file" ] && [ "$(head -n 1 "$count_file")" = "$day" ]; then n=$(sed -n 2p "$count_file"); fi
        if [ "${n:-0}" -ge "$S_FALLBACK_MAX" ]; then log "fallback $role $tool skipped: daily cap reached"; continue; fi
        log "$role fallback: $tool $chosen"
      fi
      log "role $role running $tool ${chosen:-auto}"
      tool_run "$tool" "$prompt" "$chosen" "$([ "$tool" = "$primary" ] && printf '%s' "$effort" || printf auto)"; rc=$?
      if [ "$tool" != "$primary" ] && [ "$rc" -ne 1 ] && [ "$rc" -ne 4 ]; then printf '%s\n%s\n' "$day" "$((n+1))" > "$count_file"; fi
      case "$rc" in
        0) ROLE_NOW=""; return 0;;
        1) log "$role provider $tool is not installed";;
        2) wait_for_limit=1; log "$role provider $tool is out of usage";;
        *) log "$role provider $tool failed (exit ${LAST_RC:-1})";;
      esac
    done
    if [ "$wait_for_limit" -eq 0 ] || [ "$eligible_count" -eq 0 ]; then
      log "$role failed: no available independent provider"
      relay_runtime event blocked "$role: no eligible independent provider; check routing and call limits"
      LAST_RC=1; ROLE_NOW=""; return 1
    fi
    limit_sleep "$primary" ""
  done
}

codex_run()  { run codex  "$1" "$CODEX_BIN" exec "${CODEX_PICK[@]}" "${CODEX_ARGS[@]}" -; }

codex_pick() {  # choose Codex's model and reasoning effort from this task's 'RECOMMENDED_AI:' line
  CODEX_PICK=(); CODEX_PICK_DESC="config default"; CODEX_PICK_MODEL=auto
  local rec model effort
  rec=$(grep -a -m1 '^RECOMMENDED_AI:' "$TASK_FILE" 2>/dev/null | tr -d '\r' | sed 's/^RECOMMENDED_AI:[[:space:]]*//')
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
  CODEX_PICK_MODEL="${model:-auto}"
  [ -n "$model" ] && CODEX_PICK+=(-m "$model")
  [ -n "$effort" ] && CODEX_PICK+=(-c "model_reasoning_effort=$effort")
  CODEX_PICK_DESC="${model:-config default} / reasoning ${effort:-config default}"
  [ -n "$rec" ] || CODEX_PICK_DESC="$CODEX_PICK_DESC (no RECOMMENDED_AI line in $TASK_FILE)"
}

