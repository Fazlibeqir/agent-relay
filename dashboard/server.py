"""Agent relay dashboard: a local web page that shows what every AI model in an unattended relay is doing.

  python server.py --relay <relay-repo-dir> [--port 8765] [--host 127.0.0.1] [--providers providers.json]

Standard library only. It never calls an AI; the only file it writes is relay.settings.json (POST /api/settings, localhost only).
It reads relay.log (phase, task, rounds, limits), relay-live-<label>.log/.start (what each agent is doing right now),
relay-fallback-<tool>.count (fallback builds today), PLAYTEST_QUEUE.md (what waits for the owner), and each provider's own
usage source when it has one (Codex writes its rate limits into its session logs).
"""
import customai, digest, existing, osops, planner, subprocess, threading, argparse, importlib.util, json, os, pathlib, re, statistics, tempfile, time, glob, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).parent
import sys
sys.path.insert(0, str(HERE))
import appapi
import copilot_usage as copilot_quota
import phone
import reviewmedia
DEFAULTS = {"BASELINE": 600, "DISPATCH": 360, "BUILD": 900, "SUITE": 600, "REVIEW": 240}
PAIRS = {("BASELINE", "BASELINE_OK"): "BASELINE", ("DISPATCH", "BUILD"): "DISPATCH", ("BUILD", "SUITE"): "BUILD",
         ("SUITE", "REVIEW"): "SUITE", ("REVIEW", "ROUND_DONE"): "REVIEW"}
RED = re.compile(r"baseline tests FAIL|suite (FAIL|failed)|graphics FAIL|timed out|exited with rc|usage limit|dispatcher failed|"
                 r"dispatcher wrote no task|no new commits|stalled|uncommitted changes|refused to commit|out of usage|known timing flake", re.I)
LOG_LINE = re.compile(r"^\[(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$")


def kind_of(m):
    for pat, k in ((r"^running baseline test suite", "BASELINE"), (r"^baseline tests pass", "BASELINE_OK"), (r"^baseline tests FAIL", "STOPPED"),
                   (r"choosing next task|^role dispatcher running|parallel mode: the dispatcher plans", "DISPATCH"), (r"(Codex|builder) building|builds in its own worktree", "BUILD"),
                   (r"(made|merged) \d+ commit\(s\)[^;]*; running the full suite", "SUITE"), (r"Claude reviewing|reviewing", "REVIEW"),
                   (r"review done", "ROUND_DONE"), (r"^no task the agents can advance", "IDLE"), (r"hit a usage limit", "LIMIT"),
                   (r"^role reviewer running", "REVIEW"),
                   (r"out of usage — fallback builder", "FALLBACK"), (r"^dispatcher failed", "DISPATCH_FAIL"),
                   (r"^stalled|^dispatcher wrote no task|^reached MAX_ROUNDS|^uncommitted changes under", "STOPPED")):
        if re.search(pat, m):
            return k
    return None


def tail_bytes(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - n))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_epoch(path):
    try:
        return int(pathlib.Path(path).read_text().strip().split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def short(t, n):
    t = re.sub(r"\s+", " ", str(t)).strip()
    return t if len(t) <= n else t[: n - 3] + "..."


def parse_claude_live(relay):
    """stream-json from `claude -p`: tokens, state and the last tool calls."""
    start = read_epoch(relay / "relay-live-claude.start")
    out = {"running": start > 0, "start": start, "tokens": 0, "state": "idle", "actions": []}
    if start <= 0:
        return out
    toks, state = {}, "Thinking"
    for line in tail_bytes(relay / "relay-live-claude.log", 400_000).splitlines():
        if line.startswith('{"type":"assistant"'):
            try: e = json.loads(line)
            except ValueError: continue
            msg = e.get("message", {}); mid = msg.get("id", "")
            ot = (msg.get("usage") or {}).get("output_tokens", 0)
            if mid and toks.get(mid, -1) < ot: toks[mid] = ot
            for b in msg.get("content") or []:
                if b.get("type") == "tool_use":
                    i = b.get("input") or {}
                    arg = i.get("command") or os.path.basename(i.get("file_path", "")) or i.get("pattern") or i.get("description") or i.get("url") or i.get("query") or ""
                    name = str(b.get("name", "")).split("__")[-1]
                    out["actions"].append(short(f"{name}: {arg}", 100)); state = f"Running {name}"
                elif b.get("type") == "text" and b.get("text"):
                    out["actions"].append("says: " + short(b["text"], 100)); state = "Writing"
        elif line.startswith('{"type":"user"'):
            state = "Thinking"
        elif line.startswith('{"type":"result"'):
            state = "Finishing"
    out["tokens"] = sum(toks.values()); out["state"] = state; out["actions"] = out["actions"][-4:]
    return out


def parse_codex_like_live(relay, label="codex"):
    """Codex's plain-text exec output (also fine for the fallback tools: unknown lines are ignored)."""
    start = read_epoch(relay / f"relay-live-{label}.start")
    out = {"running": start > 0, "start": start, "tokens": None, "state": "idle", "actions": []}
    text = tail_bytes(relay / f"relay-live-{label}.log", 400_000).splitlines()
    state, expect = "Thinking", ""
    for raw in text:
        t = raw.rstrip()
        if expect == "cmd":
            out["actions"].append(short("ran: " + re.sub(r'^"?[A-Za-z]:[^"]*powershell\.exe"?\s+-Command\s+', "", t), 100)); state = "Running a command"; expect = ""
        elif expect == "msg":
            if t.strip(): out["actions"].append("says: " + short(t, 100)); expect = ""
        elif expect == "tok":
            if t.strip(): out["tokens"] = t.strip(); expect = ""; state = "Finishing"
        elif t == "exec": expect = "cmd"
        elif t == "codex": expect = "msg"; state = "Writing"
        elif t == "apply patch": state = "Editing files"; out["actions"].append("editing files (apply patch)")
        elif t.startswith("patch: completed"): state = "Thinking"
        elif t == "tokens used": expect = "tok"
        else:
            m = re.match(r"^\s*(succeeded|exited (\d+)) in \d+ms", t)
            if m:
                state = "Thinking"
                if m.group(2) and m.group(2) != "0" and out["actions"]: out["actions"][-1] += f"  [exit {m.group(2)}]"
    if start > 0: out["state"] = state
    out["actions"] = out["actions"][-4:]
    return out


def codex_usage():
    """Codex records its rate limits in every session log: take the newest one."""
    base = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")) / "sessions"
    files = sorted(glob.glob(str(base / "**" / "rollout-*.jsonl"), recursive=True), key=os.path.getmtime, reverse=True)[:3]
    for f in files:
        txt = tail_bytes(f, 600_000)
        i = txt.rfind('"rate_limits":')
        while i >= 0:
            try: rl, _ = json.JSONDecoder().raw_decode(txt[i + len('"rate_limits":'):])
            except ValueError: i = txt.rfind('"rate_limits":', 0, i); continue
            if not isinstance(rl, dict) or not rl.get("primary"): i = txt.rfind('"rate_limits":', 0, i); continue
            def win(w):
                if not w: return None
                return {"usedPercent": w.get("used_percent"), "windowMinutes": w.get("window_minutes"),
                        "resetsAt": w.get("resets_at"), "label": "5-hour" if w.get("window_minutes") == 300 else ("weekly" if w.get("window_minutes") == 10080 else f"{w.get('window_minutes')} min")}
            return {"windows": [x for x in (win(rl.get("primary")), win(rl.get("secondary"))) if x], "source": "Codex session log", "asOf": os.path.getmtime(f)}
    return None


def claude_usage(relay):
    """Claude reports its plan usage inside every `claude -p` call (a rate_limit_event). Newer versions list every window (5-hour,
    weekly) with its utilization in each event; older ones report one window, with a percentage only above a warning level. Keep the
    newest per window between calls (the live log is overwritten by the next call), and keep a reset time even without a percentage."""
    labels = {"five_hour": "5-hour", "seven_day": "weekly", "seven_day_opus": "weekly (Opus)", "seven_day_sonnet": "weekly (Sonnet)"}
    store = appapi.APPDATA / "claude-usage.json"
    try: known = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError): known = {}
    if not isinstance(known, dict): known = {}
    f = relay / "relay-live-claude.log"
    try: seen_at = f.stat().st_mtime
    except OSError: seen_at = 0
    now = time.time()
    changed = False
    for line in tail_bytes(f, 1_000_000).splitlines():
        if '"rate_limit_event"' not in line: continue
        try: event = json.loads(line)
        except ValueError: continue
        if not isinstance(event, dict) or event.get("type") != "rate_limit_event": continue
        info = event.get("rate_limit_info") or {}
        if not isinstance(info, dict): continue
        windows = {k: w for k, w in (info.get("unifiedWindows") or {}).items() if isinstance(w, dict)} if isinstance(info.get("unifiedWindows"), dict) else {}
        if isinstance(info.get("rateLimitType"), str): windows.setdefault(info["rateLimitType"], info)
        for kind, w in windows.items():
            reset_at, utilization = w.get("resetsAt"), w.get("utilization")
            if kind not in labels: continue
            if type(reset_at) not in (int, float) or not now < reset_at < float("inf"): continue
            previous = known.get(kind) if isinstance(known.get(kind), dict) else {}
            if previous.get("seenAt", 0) > seen_at: continue
            snapshot = {"resetsAt": reset_at, "label": labels[kind], "seenAt": seen_at}
            if type(utilization) in (int, float) and 0 <= utilization <= 1:
                snapshot["usedPercent"] = round(utilization * 100)
            elif previous.get("resetsAt") == reset_at and "usedPercent" in previous:
                snapshot["usedPercent"] = previous["usedPercent"]
            if previous != snapshot:
                known[kind] = snapshot
                changed = True
    if changed:
        try: appapi.APPDATA.mkdir(parents=True, exist_ok=True); store.write_text(json.dumps(known), encoding="utf-8")
        except OSError: pass
    active = [w for w in known.values() if isinstance(w, dict) and type(w.get("resetsAt")) in (int, float)
              and now < w["resetsAt"] < float("inf") and "label" in w and "seenAt" in w]      # a window that has already reset is no longer true
    if not active: return None
    earliest = min(active, key=lambda w: w["resetsAt"])
    newest = max(w["seenAt"] for w in active)
    return {"windows": [w for w in active if "usedPercent" in w],
            "reset": {"label": earliest["label"], "resetsAt": earliest["resetsAt"]},
            "source": "Reported by Claude at " + time.strftime("%H:%M", time.localtime(newest)), "asOf": newest}


def next_local_midnight():
    d = datetime.date.today() + datetime.timedelta(days=1)
    return datetime.datetime.combine(d, datetime.time.min).timestamp()


def read_settings(relay):
    try: return json.loads((relay / "relay.settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}


# The keys the AIs & settings page shows and sends. Everything else in relay.settings.json (mode, parallel, copilotMonthlyPremium, ...) is set elsewhere.
MANAGED_SETTINGS = ("roles", "fallbackBuilders", "fallbackRoles", "fallbackMaxPerDay", "idleChores", "capabilities", "limits", "models", "approvals")


def keep_unmanaged_settings(relay, body):
    """A save from the settings page replaced the whole file with the keys the page knows, so saving any setting silently turned parallel mode off
    (and erased copilotMonthlyPremium). Keys the page does not manage are carried over from the file unless the body names them itself."""
    if not isinstance(body, dict): return body
    current = read_settings(relay)
    if not isinstance(current, dict): return body
    return {**body, **{k: v for k, v in current.items() if k not in MANAGED_SETTINGS and k not in body}}


def backup_window(relay, calls):
    """How many backup-AI calls the relay made today against its own daily limit (the relay stops using a backup tool at that limit)."""
    cap = read_settings(relay).get("fallbackMaxPerDay", 10)
    if not isinstance(cap, int) or cap <= 0: return None
    return {"label": "today", "usedPercent": min(100, round(100 * calls / cap)), "resetsAt": next_local_midnight(), "windowMinutes": None,
            "detail": "%d of %d backup calls today" % (calls, cap)}


def backup_usage(relay, calls):
    w = backup_window(relay, calls or 0)
    return {"windows": [w], "source": "the relay's own count and daily limit (this tool has no plan limit to read)", "asOf": time.time()} if w else None


_COPILOT_SEEN = {}      # events file -> (mtime, size, requests, when): sessions that have not changed are not read again


def copilot_month_requests(month):
    """Premium requests Copilot recorded in its session files on this PC during the month (YYYY-MM, UTC). A session logs its running total at a
    checkpoint and again at shutdown, so the highest value per session is its real total. None when Copilot keeps no sessions here."""
    base = pathlib.Path(os.environ.get("COPILOT_HOME", pathlib.Path.home() / ".copilot")) / "session-state"
    if not base.is_dir(): return None
    total = 0
    for f in glob.glob(str(base / "*" / "events.jsonl")):
        try: st = os.stat(f)
        except OSError: continue
        hit = _COPILOT_SEEN.get(f)
        if not hit or hit[:2] != (st.st_mtime, st.st_size):
            best, when = 0, ""
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if "totalPremiumRequests" not in line: continue
                        try: e = json.loads(line)
                        except ValueError: continue
                        n = (e.get("data") or {}).get("totalPremiumRequests")
                        if isinstance(n, (int, float)) and n >= best: best, when = n, str(e.get("timestamp") or "")
            except OSError: continue
            hit = (st.st_mtime, st.st_size, best, when); _COPILOT_SEEN[f] = hit
        if hit[3].startswith(month): total += hit[2]
    return total


def copilot_usage(relay, calls):
    now = datetime.datetime.now(datetime.timezone.utc)
    nxt = datetime.datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=datetime.timezone.utc).timestamp()
    wins = []
    quota = copilot_quota.cached()          # asked through the Copilot CLI in the background; None until it first answers
    if quota and quota.get("windows"):
        for w in quota["windows"]:
            wins.append({"label": w["label"], "usedPercent": w["usedPercent"], "resetsAt": nxt if w["label"] == "Premium" else None, "windowMinutes": None,
                         "detail": "%g%% of your %s used (your Copilot account)" % (w["usedPercent"], "monthly premium requests" if w["label"] == "Premium" else w["label"])})
        b = backup_window(relay, calls or 0)
        if b: wins.append(b)
        return {"windows": wins, "source": "your Copilot account, asked through the Copilot CLI", "asOf": time.time()}
    used = copilot_month_requests(now.strftime("%Y-%m"))
    if used is not None:
        allowance = read_settings(relay).get("copilotMonthlyPremium", 300)
        if not isinstance(allowance, (int, float)) or allowance <= 0: allowance = 300
        wins.append({"label": "month", "usedPercent": min(100, round(100 * used / allowance)), "resetsAt": nxt, "windowMinutes": None,
                     "detail": "%g of %g premium requests this month" % (used, allowance)})
    b = backup_window(relay, calls or 0)
    if b: wins.append(b)
    if not wins: return None
    return {"windows": wins, "estimate": True, "source": "Copilot sessions on this PC (the account could not be asked); the monthly allowance is assumed to be 300 (Copilot Pro) unless copilotMonthlyPremium is set in relay.settings.json", "asOf": time.time()}


def fallback_counts(relay):
    out = {}
    for p in relay.glob("relay-fallback-*.count"):
        try:
            day, n = p.read_text().split()[:2]
            if day == datetime.date.today().isoformat(): out[p.stem.replace("relay-fallback-", "")] = int(n)
        except (OSError, ValueError):
            pass
    return out


def parse_events(relay):
    ev = []
    for line in tail_bytes(relay / "relay.log", 1_500_000).splitlines():
        m = LOG_LINE.match(line)
        if m:
            try: t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            except ValueError: continue
            ev.append({"t": t, "ts": m.group(1)[11:16], "msg": m.group(2)})
    return ev


def estimates(markers):
    samples = {k: [] for k in DEFAULTS}
    for a, b in zip(markers, markers[1:]):
        k = PAIRS.get((a["kind"], b["kind"]))
        if k and b["t"] - a["t"] > 5: samples[k].append(b["t"] - a["t"])
    return {k: (statistics.median(samples[k][-5:]) if samples[k] else DEFAULTS[k]) for k in DEFAULTS}


def build_state(relay, providers):
    if relay is None:
        return {"noProject": True, "now": time.time(), "providers": [], "alive": False}
    now = time.time()
    ev = parse_events(relay)
    markers = [dict(e, kind=kind_of(e["msg"])) for e in ev if kind_of(e["msg"])]
    start_idx = max([i for i, e in enumerate(ev) if e["msg"].startswith("relay started")] or [0])
    run = ev[start_idx:]
    est = estimates(markers)
    alive = (relay / ".relay.lock").exists()
    last_marker = markers[-1] if markers else None
    run_start = run[0]["t"] if run else 0
    # While the relay is running, only a phase marker logged since its latest start counts. An older run's marker would show
    # that run's phase with days of "elapsed" (a resumed test run writes no marker of its own).
    cur = last_marker if last_marker and (not alive or last_marker["t"] >= run_start) else None
    state = {"now": now, "alive": alive, "relay": str(relay), "branch": "", "task": "", "round": "", "phase": None, "estimates": est}
    for e in run:
        m = re.match(r"relay started on branch (\S+)", e["msg"])
        if m: state["branch"] = m.group(1); state["startedAt"] = e["t"]
        m = re.match(r"round (\d+) — (T\d+[a-z]?[^—]*— [^—]*?) — (?:Codex|\w+) building", e["msg"])
        if m: state["round"], state["task"] = m.group(1), m.group(2)
        m = re.match(r"round (\d+) — choosing", e["msg"])
        if m: state["round"] = m.group(1)
    lanes = []      # parallel mode: who builds which task in the latest round
    plan_at = max([i for i, e in enumerate(run) if "parallel mode: the dispatcher plans" in e["msg"]] or [-1])
    for e in (run[plan_at:] if plan_at >= 0 else []):
        m = re.search(r"(T\d+[a-z]?): (\w+) builds in its own worktree", e["msg"])
        if m: lanes.append({"task": m.group(1), "provider": m.group(2)})
    state["lanes"] = lanes
    state["mode"] = read_settings(relay).get("mode", "serial")
    pm = {"BASELINE": ("Baseline test suite", "BASELINE"), "DISPATCH": ("Choosing the next task", "DISPATCH"), "BUILD": ("Building", "BUILD"),
          "SUITE": ("Full test suite", "SUITE"), "REVIEW": ("Reviewing", "REVIEW")}
    if cur:
        k = cur["kind"]
        if k in pm and alive:
            label, ek = pm[k]
            elapsed = now - cur["t"]
            state["phase"] = {"kind": k, "label": label, "since": cur["t"], "elapsed": elapsed, "estimate": est[ek], "remaining": est[ek] - elapsed}
        elif k == "LIMIT" and alive:
            m = re.search(r"sleeping (\d+)s(?: \((?:until|retry) about (\d\d:\d\d)\))?", cur["msg"])
            secs = int(m.group(1)) if m else 1800
            state["phase"] = {"kind": "LIMIT", "label": "Waiting to retry available AI tools", "since": cur["t"], "elapsed": now - cur["t"], "estimate": secs,
                              "remaining": secs - (now - cur["t"]), "retryAt": cur["t"] + secs}
        elif not alive:
            state["phase"] = {"kind": "STOPPED", "label": "Relay stopped", "since": cur["t"], "elapsed": now - cur["t"], "estimate": 0, "remaining": 0}
    elif alive and run:
        state["phase"] = {"kind": "START", "label": "Starting", "since": run_start, "elapsed": now - run_start, "estimate": 0, "remaining": 0}
    order = ["DISPATCH", "BUILD", "SUITE", "REVIEW"]
    if state["phase"] and state["phase"]["kind"] in order:
        i = order.index(state["phase"]["kind"]); rem = max(0, state["phase"]["remaining"]); nxt = []
        acc = rem
        for k in order[i + 1:]:
            acc += est[k]; nxt.append({"label": {"BUILD": "Building", "SUITE": "Full test suite", "REVIEW": "Reviewing"}[k], "inSeconds": acc})
        state["next"] = nxt; state["roundDoneIn"] = acc
    # what each agent runs on
    claude_model = ""; codex_model = ""
    for e in run:
        m = re.search(r"agents: Claude (\S+), effort (\w+)", e["msg"])
        if m: claude_model = f"{m.group(1)} {m.group(2)}"
        m = re.search(r"Codex model: (.+)$", e["msg"])
        if m: codex_model = re.sub(r" \(no RECOMMENDED_AI.*$", "", m.group(1))
    live = {pr["id"]: (parse_claude_live(relay) if pr["id"] == "claude" else parse_codex_like_live(relay, pr["id"])) for pr in providers}
    limits = {}
    for e in ev[-400:]:
        m = re.match(r"(\w+) hit a usage limit — sleeping (\d+)s", e["msg"])
        if m: limits[m.group(1)] = {"at": e["t"], "ts": e["ts"], "retryAt": e["t"] + int(m.group(2))}
    active_roles = {}
    for e in run:
        m = re.match(r"role (dispatcher|reviewer) running ([\w-]+) (\S+)", e["msg"])
        if m: active_roles[m.group(2)] = (m.group(1), m.group(3))
    fb = fallback_counts(relay)
    state["providers"] = []
    for p in providers:
        row = dict(p)
        pid = p["id"]
        row["model"] = {"claude": claude_model, "codex": codex_model}.get(pid, p.get("defaultModel", ""))
        row["live"] = live.get(pid) or {"running": False, "state": "idle", "actions": []}
        limit = limits.get(pid)
        row["lastLimit"] = limit if limit and (limit.get("retryAt") or 0) > now else None
        if pid in active_roles:
            row["role"], row["model"] = active_roles[pid]
        row["fallbackBuildsToday"] = fb.get(pid)
        kind = p.get("usage")
        row["usage"] = (codex_usage() if kind == "codex-session-log" else claude_usage(relay) if kind == "claude-stream"
                        else copilot_usage(relay, fb.get(pid)) if kind == "copilot-sessions" else backup_usage(relay, fb.get(pid)) if kind == "backup-calls" else None)
        state["providers"].append(row)
    state["done"] = [{"ts": e["ts"], "msg": short(e["msg"], 150)} for e in run if kind_of(e["msg"]) == "ROUND_DONE" or e["msg"].startswith("committed for")][-8:]
    state["broke"] = [{"ts": e["ts"], "msg": short(e["msg"], 160)} for e in run if RED.search(e["msg"]) and not e["msg"].startswith("test command")][-6:]
    q = relay / "PLAYTEST_QUEUE.md"
    ids = []
    if q.exists():
        for line in q.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^(T\d+[a-z]?)\s+[—-]\s+(.*?)\s+[—-]", line)
            if m: ids.append(m.group(1))
    state["ownerQueue"] = ids
    digest = relay / "OWNER-REVIEW-DIGEST.md"
    state["digest"] = digest.exists()
    idle = sorted(relay.glob("relay-idle-*.result"))
    state["idleChores"] = [p.read_text(encoding="utf-8", errors="replace").strip()[:300] for p in idle]
    # test progress
    t = tail_bytes(relay / "relay-tests.log", 200_000)
    checks = re.findall(r"^\[checks\] (.+)$", t, re.M)
    state["testCheck"] = {"count": len(checks), "label": checks[-1] if checks else ""}
    state["requests"] = appapi.list_requests(relay)[-14:]
    state["queue"] = appapi.next_tasks(relay, 5)
    state["runtime"] = appapi.runtime_state(relay)
    return state


CATALOG = {"at": 0, "data": None}
CLAUDE_MODELS = ["claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
GENERIC_EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def model_catalog():
    """Models and reasoning levels each provider offers, for the settings dropdowns. Cached for 10 minutes."""
    if CATALOG["data"] and time.time() - CATALOG["at"] < 600: return CATALOG["data"]
    cat = {"claude": {"models": [{"id": m, "label": m} for m in CLAUDE_MODELS], "efforts": GENERIC_EFFORTS},
           "copilot": {"models": [{"id": "auto", "label": "auto (Copilot picks)"}], "efforts": []},
           "codex": {"models": [], "efforts": GENERIC_EFFORTS},
           "opencode": {"models": [], "efforts": []}}
    try:
        cache = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")) / "models_cache.json"
        for m in json.loads(cache.read_text(encoding="utf-8")).get("models", []):
            if m.get("visibility") != "list": continue
            levels = [l.get("effort") for l in (m.get("supported_reasoning_levels") or []) if isinstance(l, dict) and l.get("effort")]
            cat["codex"]["models"].append({"id": m["slug"], "label": m.get("display_name") or m["slug"], "efforts": levels or GENERIC_EFFORTS})
    except Exception:
        pass
    if not cat["codex"]["models"]: cat["codex"]["models"] = [{"id": "gpt-6.1-sol", "label": "GPT-6.1-Sol", "efforts": GENERIC_EFFORTS}]
    try:
        found = appapi_found("opencode")
        if found:
            rc, out = osops.run(found, ["models"], timeout=45)
            ids = [l.strip() for l in out.splitlines() if "/" in l and " " not in l.strip() and not l.startswith("[")]
            free = sorted(i for i in ids if i.endswith("-free")); rest = sorted(i for i in ids if not i.endswith("-free"))
            cat["opencode"]["models"] = [{"id": i, "label": i + (" (free)" if i in free else ""), "group": "Free" if i in free else "Other"} for i in free + rest]
    except Exception:
        pass
    if not cat["opencode"]["models"]: cat["opencode"]["models"] = [{"id": "opencode/deepseek-v4.1-flash-free", "label": "opencode/deepseek-v4.1-flash-free (free)", "group": "Free"}]
    CATALOG["at"], CATALOG["data"] = time.time(), cat
    return cat


def with_custom(cat, relay):
    out = dict(cat)
    if relay is not None:
        for x in customai.load(relay):
            out[x["id"]] = {"models": [{"id": m, "label": m} for m in x.get("models", [])] or [{"id": "auto", "label": "auto (tool default)"}], "efforts": x.get("efforts", [])}
    return out


def custom_cards(relay):
    if relay is None: return []
    return [{"id": x["id"], "name": x.get("name", x["id"]), "role": "Custom AI", "color": "#8b949e", "usage": None, "defaultModel": ""} for x in customai.load(relay)]


def available_ai_text(relay):
    """The AIs that are installed and usable right now, with their models and reasoning levels, for the planner."""
    cat = with_custom(model_catalog(), relay)
    lines = []
    for t in appapi.detect_setup()["tools"]:
        if not t["found"]: continue
        if not (t["signedIn"] is True or t["id"] == "opencode"): continue
        c = cat.get(t["id"])
        if not c: continue
        models = [m["id"] for m in c["models"]]
        if t["id"] == "opencode": models = [m for m in models if m.endswith("-free")][:8] or models[:8]
        if t["id"] == "codex": models = models[:6]
        eff = c.get("efforts") or ["(no reasoning setting)"]
        lines.append("   - %s: models %s; reasoning %s" % (t["id"], ", ".join(models) or "(its default)", ", ".join(eff)))
    return chr(10).join(lines)


def normalize_task_recs(relay, reviewer_model="claude-sonnet-5-5"):
    """The planner may invent a model name. Snap every task's 'Recommended AI' to something that exists on this computer: a usable AI, one of its
    real models, a reasoning level it supports, and never the reviewer's own model. Returns a list of what was changed."""
    import difflib
    cat = with_custom(model_catalog(), relay)
    usable = [t["id"] for t in appapi.detect_setup()["tools"] if t["found"] and (t["signedIn"] is True or t["id"] == "opencode")]
    usable += [x["id"] for x in customai.load(relay) if customai.find_bin(x["bin"])]
    builders = [u for u in usable if u != "claude"] or usable
    changes = []
    order = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    for tk in appapi.list_tasks(relay):
        rec = tk["rec"]; prov, model, eff = rec["provider"], rec["model"], rec["effort"]
        new = (prov, model, eff)
        if prov not in usable:
            prov = builders[0] if builders else ""; model = ""; eff = eff or "medium"
        c = cat.get(prov, {"models": [], "efforts": []})
        ids = [m["id"] for m in c.get("models", [])]
        if ids and model not in ids:
            close = difflib.get_close_matches(model, ids, 1, 0.75)
            model = close[0] if close else (next((i for i in ids if i.endswith("-free")), ids[0]) if prov == "opencode" else ids[0])
        if prov == "claude" and model == reviewer_model:
            alt = [i for i in ids if i != reviewer_model]
            if alt: model = alt[0]
        if prov == "codex":
            m = next((x for x in c.get("models", []) if x["id"] == model), None); efforts = (m or {}).get("efforts") or c.get("efforts", [])
        else: efforts = c.get("efforts", [])
        if efforts:
            if eff not in efforts:
                eff = min(efforts, key=lambda e: abs(order.index(e) - order.index(eff)) if eff in order and e in order else 99) if eff in order else ("high" if "high" in efforts else efforts[0])
        else: eff = ""
        if (prov, model, eff) != new and prov:
            appapi.set_task_rec(relay, tk["id"], prov, model, eff)
            changes.append("%s: %s %s %s -> %s %s %s" % (tk["id"], new[0] or "-", new[1] or "-", new[2] or "-", prov, model, eff or "-"))
    return changes


def appapi_found(tool_id):
    return next((x["found"] for x in appapi.detect_setup()["tools"] if x["id"] == tool_id), None)


def settings_module(relay):
    spec = importlib.util.spec_from_file_location("relay_settings", relay / "relay-settings.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def settings_state(relay):
    mod = settings_module(relay)
    cfg, problems, ok = mod.effective(str(relay / "relay.settings.json"))
    raw = None
    f = relay / "relay.settings.json"
    if f.exists():
        try: raw = json.loads(f.read_text(encoding="utf-8-sig"))
        except ValueError: raw = None
    return {"effective": cfg, "valid": ok, "errors": problems["errors"], "warnings": problems["warnings"], "file": raw, "providers": mod.all_providers(str(relay / "relay.settings.json")), "builtin": list(mod.PROVIDERS)}


def parallel_support(relay):
    """Parallel mode needs the standard task-file runner with the parallel scripts installed in the project."""
    r = pathlib.Path(relay)
    script = appapi.relay_script(r)
    if script != "relay-files.sh":
        return {"available": False, "reason": "This project uses its own runner (%s). Parallel mode works with the standard task-file runner." % (script or "none")}
    if not (r / "relay-parallel.sh").is_file():
        return {"available": False, "reason": "Press Update Relay first: this project's runner does not have parallel mode yet."}
    return {"available": True, "reason": ""}


def set_mode(relay, mode):
    """Switch between one task at a time and parallel mode: only the mode key of relay.settings.json changes, validated like any other save."""
    if mode not in ("serial", "parallel"): return False, {"errors": ["mode must be serial or parallel"], "warnings": []}
    support = parallel_support(relay)
    if mode == "parallel" and not support["available"]: return False, {"errors": [support["reason"]], "warnings": []}
    f = pathlib.Path(relay) / "relay.settings.json"
    raw = {}
    if f.exists():
        try: raw = json.loads(f.read_text(encoding="utf-8-sig"))
        except ValueError: return False, {"errors": ["relay.settings.json cannot be read. Fix it under AIs & settings first."], "warnings": []}
        if not isinstance(raw, dict): return False, {"errors": ["relay.settings.json must be an object"], "warnings": []}
    raw["mode"] = mode
    return save_settings(relay, raw)


def save_settings(relay, body):
    """Validate a candidate settings object with the same rules the relay uses; write atomically only if it passes."""
    mod = settings_module(relay)
    fd, name = tempfile.mkstemp(suffix=".json", prefix=".relay-settings-", dir=str(relay)); os.close(fd)
    tmp = pathlib.Path(name)
    try:
        tmp.write_text(json.dumps(body, indent=1), encoding="utf-8")
        cfg, problems = mod.load(str(tmp)); mod.validate(cfg, problems)
        if problems["errors"]:
            return False, problems
        os.replace(str(tmp), str(relay / "relay.settings.json"))
        return True, problems
    finally:
        if tmp.exists(): tmp.unlink()


def wizard_state(relay):
    """Everything the step-by-step page needs about the current project."""
    proj = {"path": "", "exists": False}
    if relay is not None:
        r = pathlib.Path(relay)
        info = appapi.project_metadata(r)
        tasks = appapi.list_tasks(r)
        script = appapi.relay_script(r)
        start_error = appapi.relay_setup_error(r, script, info, len(tasks))
        tasks_ready = appapi.formatted_tasks(r)[0] if not script else False
        remote = ""
        try:
            rc = subprocess.run(["git", "-C", str(r), "remote", "get-url", "origin"], capture_output=True, text=True, creationflags=appapi.NO_WINDOW)
            remote = rc.stdout.strip() if rc.returncode == 0 else ""
        except OSError: pass
        proj = {"path": str(r), "exists": True, "name": info.get("name") or r.name, "idea": info.get("idea", ""), "stack": info.get("stack", []),
                "testCommand": info.get("testCommand", ""), "localWorkspace": info.get("localWorkspace") is True,
                "hasProposal": (r / "PROPOSAL.md").exists(), "hasPlan": bool(tasks) and not start_error,
                "taskCount": len(tasks), "hasSource": (r / existing.SOURCE_NAME).exists(),
                "hasRelay": bool(script), "script": script, "running": appapi.relay_running(r), "remote": remote,
                "canStart": not start_error, "startError": start_error,
                "canConfigureSavedTasks": tasks_ready and (r / "AGENTS.md").is_file() and not (r / "relay.project.json").exists(),
                "requires": [s for s in (appapi.toolchain_status(x) for x in info.get("requires", []) if isinstance(x, str)) if s],
                "runnerUpdate": appapi.runner_update_available(r), "parallel": parallel_support(r), "mode": read_settings(r).get("mode", "serial")}
    return {"project": proj, "githost": appapi.githost_status(), "plannerProviders": planner.PLANNER_PROVIDERS}


ASSETS = HERE / "assets" if (HERE / "assets").exists() else HERE.parent / "assets"
ASSET_OK = re.compile(r"^(agent-relay[A-Za-z0-9._-]*|flux-logo\.(png|svg))$")
EXTERNAL_OK = ("https://flux.mk", "https://www.flux.mk", "https://claude.ai/settings/usage")      # the only addresses the page may ask the app to open in the normal browser


def flux_logo():
    for ext in ("svg", "png"):
        if (ASSETS / ("flux-logo." + ext)).is_file(): return "/assets/flux-logo." + ext
    return ""


def phone_summary(relay, providers):
    """What the phone page shows: a trimmed copy of the dashboard state (no logs, paths or settings)."""
    if relay is None: return {"noProject": True}
    s = build_state(relay, providers)
    titles = {t["id"]: t.get("title", "") for t in appapi.list_tasks(relay)}
    mode = appapi.relay_script(relay)
    cards = []
    for p in s.get("providers", []):
        live, u = p.get("live") or {}, p.get("usage") or {}
        cards.append({"name": p.get("name"), "color": p.get("color"), "role": p.get("role"), "model": p.get("model"),
                      "running": bool(live.get("running")), "state": live.get("state", "idle"), "lastLimit": p.get("lastLimit"),
                      "usage": [{k: w.get(k) for k in ("label", "usedPercent", "resetsAt", "detail")} for w in u.get("windows", [])]})
    reqs = [{k: r.get(k) for k in ("id", "type", "action", "why", "task", "files")} for r in appapi.list_requests(relay) if r.get("status") == "pending"]
    ph = s.get("phase") or {}
    return {"project": relay.name, "alive": s.get("alive"), "task": s.get("task"), "round": s.get("round"),
            "phase": {k: ph.get(k) for k in ("label", "elapsed", "remaining", "estimate")} if ph else None,
            "roundDoneIn": s.get("roundDoneIn"), "providers": cards, "requests": reqs,
            "review": [{"id": t, "title": titles.get(t, "")} for t in s.get("ownerQueue", [])],
            "mode": "files" if mode == "relay-files.sh" else "board" if mode == "relay.sh" else "none",
            "done": s.get("done", [])[-5:], "broke": s.get("broke", [])[-5:], "now": time.time()}


class Handler(BaseHTTPRequestHandler):
    relay = None; providers = []; on_quit = None
    on_notify = None
    native_notification_shown_at = None
    notification_lock = threading.Lock()
    notification_seen = set()

    def log_message(self, *a): pass

    def _send(self, code, body, ctype, headers=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items(): self.send_header(key, str(value))
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass

    def _json_body(self):
        try: return json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 20000)) or b"{}")
        except ValueError: return {}

    def _reviewer_model(self):
        try:
            cfg, _p, _ok = settings_module(self.relay).effective(str(self.relay / "relay.settings.json"))
            return cfg["roles"]["reviewer"].get("model", "claude-sonnet-5-5")
        except Exception:
            return "claude-sonnet-5-5"

    is_phone = False
    device = None

    def _get_gate(self):
        return True

    def _post_gate(self):
        # every action here changes this computer: accepted from this computer only (phones use their own listener, see PhoneHandler)
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._send(403, json.dumps({"error": "actions can only be used from this computer"}), "application/json"); return False
        port = self.server.server_port
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        origin = self.headers.get("Origin")
        if self.headers.get("Host", "").lower() not in allowed_hosts or (origin and origin not in {"http://" + h for h in allowed_hosts}):
            self._send(403, json.dumps({"error": "Open Relay on its own localhost page to change it."}), "application/json"); return False
        return True

    def do_POST(self):
        if not self._post_gate(): return
        path = self.path
        if path.startswith("/api/recordings/save?"):
            from urllib.parse import parse_qs, urlsplit, unquote
            try:
                if Handler.relay is None: raise ValueError("Choose a project first")
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= reviewmedia.MAX_CLIP_BYTES:
                    self.close_connection = True
                    return self._send(413, json.dumps({"ok": False, "error": "Choose a nonempty clip of 45 MB or less"}), "application/json")
                query = parse_qs(urlsplit(path).query)
                # A broken uploader cannot leave a request thread blocked indefinitely.
                self.connection.settimeout(30)
                item = reviewmedia.save(Handler.relay, query.get("task", [""])[0], self.rfile, length, self.headers.get("Content-Type", ""),
                                        unquote(self.headers.get("X-Review-Note", "")), self.headers.get("X-Clip-Duration"),
                                        self.headers.get("X-Clip-Commit", ""), self.headers.get("X-Clip-Task-Hash", ""), self.headers.get("X-Clip-Source", "upload"), self.headers.get("X-Clip-Project", ""),
                                        revision=self.headers.get("X-Review-Revision", ""), build=self.headers.get("X-Review-Build", ""))
                return self._send(200, json.dumps({"ok": True, "artifact": item}), "application/json")
            except (ValueError, OSError) as exc:
                self.close_connection = True
                return self._send(400, json.dumps({"ok": False, "error": str(exc)}), "application/json")
        body = self._json_body()
        def reply(obj, code=200): self._send(code, json.dumps(obj), "application/json")
        def need_project():
            if Handler.relay is None: reply({"ok": False, "error": "Choose or create a project first."}, 400); return True
            return False
        try:
            if path == "/api/phone/pair":
                if not self.is_phone: return reply({"ok": False, "error": "Pair from the phone."}, 404)
                r = phone.redeem(body.get("code", ""), body.get("name", ""), self.client_address[0])
                return reply(r, 200 if r["ok"] else 403)
            if path == "/api/phone/enable": return reply(phone.set_enabled(bool(body.get("enabled")), PhoneHandler))
            if path == "/api/phone/pair/start":
                r = phone.start_pairing(body.get("scope", "control"))
                if r["ok"]:
                    r["url"] = phone.pair_url(r["code"])
                    r["qr"] = phone.qr_svg(r["url"]) if r["url"] else ""
                return reply(r, 200 if r["ok"] else 400)
            if path == "/api/phone/pair/cancel": return reply(phone.cancel_pairing())
            if path == "/api/phone/revoke": return reply(phone.revoke(str(body.get("id", ""))))
            if path == "/api/phone/tailscale":
                act = body.get("action")
                if act == "install": return reply(phone.tailscale_install())
                if act == "signin": return reply(phone.tailscale_signin())
                if act in ("on", "off"):
                    r = phone.tailscale_serve(act == "on")
                    return reply(r, 200 if r.get("ok") or r.get("needsEnable") else 400)
                return reply({"ok": False, "error": "unknown action"}, 400)
            if path == "/api/quit":
                reply({"ok": True}); threading.Thread(target=(Handler.on_quit or Handler.srv.shutdown), daemon=True).start(); return
            if path == "/api/notifications/show":
                notify = Handler.on_notify
                if notify is None: return reply({"ok": False, "error": "Native notifications are unavailable here."}, 400)
                if body.get("test") is True:
                    notify("Agent Relay", "Desktop notifications are enabled.")
                    return reply({"ok": True})
                if need_project(): return
                event = next((item for item in appapi.runtime_state(self.relay).get("events", [])
                              if item["id"] == body.get("id") and item["kind"] in ("blocked", "needs_review", "task_done", "review_clip")), None)
                if event is None: return reply({"ok": False, "error": "No matching notification event."}, 400)
                event_key = (str(self.relay), event["id"])
                # Release the lock before dispatching to the native UI thread.
                with Handler.notification_lock:
                    if event_key in Handler.notification_seen: return reply({"ok": True})
                    Handler.notification_seen.add(event_key)
                try:
                    notify("Agent Relay" + (" · " + event["task"] if event["task"] else ""), event["message"][:240])
                except Exception:
                    with Handler.notification_lock: Handler.notification_seen.discard(event_key)
                    raise
                return reply({"ok": True})
            if path == "/api/open-external":
                url = str(body.get("url", ""))
                if url.rstrip("/") not in EXTERNAL_OK and not re.fullmatch(r"https://login\.tailscale\.com/[A-Za-z0-9/?&=._%-]+", url):
                    return reply({"ok": False, "error": "not allowed"}, 400)
                import webbrowser; webbrowser.open(url); return reply({"ok": True})
            if path == "/api/pick-folder": return reply(appapi.pick_folder(body.get("title", "Choose a folder")))
            if path == "/api/pick-file": return reply(osops.pick_file(body.get("title", "Choose your task list")))
            if path == "/api/project/inspect": return reply(existing.inspect(body.get("path", "")))
            if path == "/api/requests/decide":
                if need_project(): return
                return reply(appapi.decide_request(Handler.relay, body.get("id", ""), body.get("decision", ""), body.get("note", "")))
            if path == "/api/project/attach":
                r = existing.attach(body.get("path", ""), bool(body.get("initGit")))
                if r.get("ok"): Handler.relay = pathlib.Path(r["path"])
                return reply(r, 200 if r.get("ok") or r.get("needsGit") else 400)
            if path == "/api/project/configure-saved-tasks":
                if need_project(): return
                project = Handler.relay
                if body.get("project") != str(project):
                    return reply({"ok": False, "error": "The open project changed. Try again from its dashboard."}, 409)
                if any(job["path"] == str(project) and job["state"] == "running" for job in list(planner.JOBS.values())):
                    return reply({"ok": False, "error": "Wait for the planning job to finish."}, 409)
                result = existing.configure_saved_tasks(project, body.get("testCommand"))
                return reply(result, 200 if result["ok"] else 400)
            if path == "/api/project/import":
                if need_project(): return
                r = existing.import_source(Handler.relay, body.get("path", ""), body.get("text", ""))
                return reply(r, 200 if r.get("ok") else 400)
            if path == "/api/mark-signed-in": return reply(appapi.mark_signed_in(body.get("id", ""), body.get("value", True)))
            if path == "/api/install-tool": return reply(appapi.install_tool(body.get("id", "")))
            if path == "/api/install-toolchain": return reply(appapi.install_toolchain(body.get("id", "")))
            if path == "/api/githost/login": return reply(appapi.githost_login(body.get("id", "")))
            if path == "/api/login-tool":
                cx = next((x for x in (customai.load(Handler.relay) if Handler.relay else []) if x["id"] == body.get("id")), None)
                if cx:
                    line = " ".join(osops.quote(a) for a in [cx["bin"]] + list(cx.get("loginArgs", ["login"])))
                    ok, msg = osops.open_terminal_keep_shell("Sign in - " + cx.get("name", cx["id"]), line)
                    return reply({"ok": ok, "message": "In the window: follow the sign-in steps for " + cx.get("name", cx["id"]) + "." if ok else msg})
                return reply(appapi.login_tool(body.get("id", "")))
            if path == "/api/custom/probe": return reply(customai.probe(body.get("bin", "")))
            if path in ("/api/custom/add", "/api/custom/remove", "/api/custom/refresh"):
                if need_project(): return
                if path.endswith("add"):
                    a = body.get("args", ""); args = a.split() if isinstance(a, str) else list(a or [])
                    return reply(customai.add(Handler.relay, body.get("name", ""), body.get("bin", ""), None, {"args": args}))
                if path.endswith("remove"): return reply(customai.remove(Handler.relay, body.get("id", "")))
                return reply(customai.refresh_models(Handler.relay, body.get("id", "")))
            if path == "/api/project/create":
                workspace = body.get("localWorkspace") is True
                r = appapi.create_project(body.get("name", ""), body.get("root", ""),
                                          workspace_idea=str(body.get("idea") or "") if workspace else None)
                if not r.get("ok"): return reply(r, 400)
                Handler.relay = pathlib.Path(r["path"])
                host = body.get("host", "local")
                if not workspace and host in ("github", "gitlab") and body.get("createRemote"):
                    r["remote"] = appapi.githost_create_repo(host, r["path"], appapi.folder_name(body.get("name", "")))
                return reply(r)
            if path == "/api/project":
                raw = str(body.get("path", "")).strip()
                d = pathlib.Path(raw).expanduser()
                if not raw or not d.is_dir(): return reply({"ok": False, "error": "that folder does not exist"}, 400)
                Handler.relay = d.resolve(); cfg = appapi.load_config(); cfg["project"] = str(Handler.relay); appapi.save_config(cfg)
                return reply({"ok": True, "project": str(Handler.relay), "hasRelay": bool(appapi.relay_script(Handler.relay))})
            if path == "/api/project/forget":
                Handler.relay = None; cfg = appapi.load_config(); cfg.pop("project", None); appapi.save_config(cfg)
                return reply({"ok": True})
            if path == "/api/install-relay":
                target = body.get("path") or (str(Handler.relay) if Handler.relay else "")
                return reply(appapi.install_relay(target))
            if path == "/api/project/update-runner":
                if need_project(): return
                project = Handler.relay
                if body.get("project") != str(project):
                    return reply({"ok": False, "error": "The open project changed. Try again from that project's dashboard."}, 409)
                if any(job["path"] == str(project) and job["state"] == "running" for job in list(planner.JOBS.values())):
                    return reply({"ok": False, "error": "Wait for planning to finish before updating Relay."}, 409)
                result = appapi.update_runner(project)
                return reply(result, 200 if result["ok"] else 409)
            if path in ("/api/project/access/check", "/api/project/access/policy"):
                if need_project(): return
                project = Handler.relay
                if body.get("project") != str(project):
                    return reply({"ok": False, "error": "The open project changed. Try again from its settings."}, 409)
                if path.endswith("/check"):
                    result = appapi.check_project_access(project, body.get("provider"))
                    return reply(result, 200 if result["ok"] else 409)
                if not appapi.supports_project_access(project) or not isinstance(body.get("checkedOnly"), bool):
                    return reply({"ok": False, "error": "This project's access setting could not be saved."}, 400)
                appapi.runtime_for(project).set_access_policy(body["checkedOnly"])
                return reply({"ok": True, "access": appapi.project_access(project)})
            if path == "/api/plan/start":
                if need_project(): return
                prov = body.get("provider", "claude")
                found = next((x["found"] for x in appapi.detect_setup()["tools"] if x["id"] == prov), None)
                det = existing.detect_project(Handler.relay) if body.get("existing") else {}
                hint = "; ".join(x for x in ((("stack: " + ", ".join(det["stack"])) if det.get("stack") else ""), (("test command: " + det["testCommand"]) if det.get("testCommand") else "")) if x)
                r = planner.start_job(body.get("phase", ""), str(Handler.relay), found, prov, body.get("model", ""), body.get("idea", ""), body.get("stack", ""),
                                      bool(body.get("suggest", True)), body.get("notes", ""), body.get("effort", ""), available_ai_text(Handler.relay), self._reviewer_model(),
                                      body.get("goal", ""), hint, bool(body.get("existing")), body.get("source", ""))
                return reply(r, 200 if r.get("ok") else 400)
            if path == "/api/plan/commit":
                if need_project(): return
                ch = normalize_task_recs(Handler.relay, self._reviewer_model())
                r = planner.commit_plan(str(Handler.relay)); r["normalized"] = ch
                return reply(r)
            if path == "/api/task/add":
                if need_project(): return
                if body.get("project") != str(Handler.relay):
                    return reply({"ok": False, "error": "The open project changed. Open its Tasks page and try again."}, 409)
                if any(job["path"] == str(Handler.relay) and job["state"] == "running" for job in list(planner.JOBS.values())):
                    return reply({"ok": False, "error": "Wait for the plan to finish before adding a task."}, 409)
                r = appapi.add_task(Handler.relay, body.get("title", ""), body.get("description", ""), body.get("review", ""))
                return reply(r, 200 if r.get("ok") else 400)
            if path in ("/api/task/approve", "/api/task/changes", "/api/task/next", "/api/task/cancel"):
                if need_project(): return
                mode = appapi.relay_script(Handler.relay)
                if mode == "relay-files.sh":
                    if path.endswith(("approve", "changes")):
                        result = appapi.decide_review(Handler.relay, body.get("task", ""), body.get("revision", ""),
                                                      path.rsplit("/", 1)[1], body.get("note", ""))
                        return reply(result, 200 if result["ok"] else 409)
                    if path.endswith("next"): return reply(appapi.queue_next(Handler.relay, body.get("task", "")))
                    return reply({"ok": False, "error": "nothing to cancel here"}, 400)
                if mode == "relay.sh":      # a project whose board is elsewhere: leave a request for its dispatcher
                    if path.endswith("cancel"): return reply(appapi.owner_cancel(Handler.relay, body.get("task", ""), body.get("kind", "")))
                    kind = {"approve": "approved", "changes": "fix", "next": "next"}[path.rsplit("/", 1)[1]]
                    if kind != "next":
                        result = appapi.decide_board_review(Handler.relay, body.get("task", ""), body.get("revision", ""),
                                                           kind, body.get("note", ""))
                        return reply(result, 200 if result["ok"] else 409)
                    return reply(appapi.owner_request(Handler.relay, kind, body.get("task", ""), body.get("note", "")))
                return reply({"ok": False, "error": "This project has no relay yet."}, 400)
            if path == "/api/task/recommend":
                if need_project(): return
                return reply(appapi.set_task_rec(Handler.relay, body.get("task", ""), body.get("provider", ""), body.get("model", ""), body.get("effort", "")))
            if path == "/api/task/feedback":
                if need_project(): return
                if appapi.relay_script(Handler.relay) != "relay-files.sh":
                    return reply({"ok": False, "error": "This project uses a custom task workflow."}, 400)
                return reply(appapi.request_work_changes(Handler.relay, body.get("task", ""), body.get("note", "")))
            if need_project(): return
            if path == "/api/relay/start": return reply(appapi.start_relay(Handler.relay, bool(body.get("saveAside"))))
            if path == "/api/relay/stop": return reply(appapi.stop_relay(Handler.relay))
            if path == "/api/worktrees/create":
                if need_project(): return
                return reply(appapi.create_worktree(Handler.relay))
            if path == "/api/artifacts/add":
                if need_project(): return
                item = appapi.runtime_for(Handler.relay).add_artifact(str(body.get("task", "")), str(body.get("path", "")), str(body.get("note", "")),
                                                                     revision=body.get("revision", ""), build=body.get("build", ""))
                return reply({"ok": True, "artifact": item})
            if path == "/api/runtime/retry":
                if need_project(): return
                if appapi.relay_running(Handler.relay): return reply({"ok": False, "error": "Stop the worker before approving a retry."}, 409)
                appapi.runtime_for(Handler.relay).approve_retry()
                return reply({"ok": True})
            if path == "/api/settings":
                ok, problems = save_settings(self.relay, keep_unmanaged_settings(self.relay, body))
                return reply({"saved": ok, **problems}, 200 if ok else 400)
            if path == "/api/settings/mode":
                ok, problems = set_mode(self.relay, str(body.get("mode", "")))
                return reply({"saved": ok, "mode": str(body.get("mode", "")), **problems}, 200 if ok else 400)
        except Exception as ex:
            return reply({"ok": False, "saved": False, "errors": [str(ex)], "error": str(ex)}, 400)
        self._send(404, "not found", "text/plain")

    def do_GET(self):
        if not self._get_gate(): return
        p = self.path
        def reply(obj, code=200): self._send(code, json.dumps(obj), "application/json")
        try:
            if p == "/phone": return self._send(200, (HERE / "phone.html").read_bytes(), "text/html; charset=utf-8")
            if p == "/phone.webmanifest":
                return self._send(200, json.dumps({"name": "Agent Relay", "short_name": "Relay", "start_url": "/phone", "display": "standalone",
                                                   "background_color": "#0b0f14", "theme_color": "#0b0f14",
                                                   "icons": [{"src": "/assets/agent-relay-512.png", "sizes": "512x512", "type": "image/png"}]}),
                                  "application/manifest+json")
            if p == "/api/phone/summary": return reply(phone_summary(self.relay, self.providers + custom_cards(self.relay)))
            if p == "/api/phone/me": return reply({"device": phone.public_device(self.device) if self.device else None, "local": not self.is_phone})
            if p == "/api/phone/status": return reply(phone.status())
            if p == "/api/models": return reply(with_custom(model_catalog(), self.relay))
            if p == "/api/runtime": return reply(appapi.runtime_state(self.relay))
            if p == "/api/project/access":
                return reply(appapi.project_access(self.relay) if self.relay else {"supported": False})
            if p == "/api/notifications/status":
                return reply({"native": Handler.on_notify is not None, "shownAt": Handler.native_notification_shown_at})
            if p == "/review.js": return self._send(200, (HERE / "review.js").read_bytes(), "text/javascript; charset=utf-8")
            if p in ("/dialogs.js", "/dialogs.css"):
                mime = "text/javascript" if p.endswith(".js") else "text/css"
                return self._send(200, (HERE / p[1:]).read_bytes(), mime + "; charset=utf-8")
            if p.startswith("/api/recordings/context?"):
                from urllib.parse import parse_qs, urlsplit
                try:
                    if self.relay is None: raise ValueError("Choose a project first")
                    return reply(reviewmedia.context(self.relay, parse_qs(urlsplit(p).query).get("task", [""])[0]))
                except ValueError as exc: return reply({"error": str(exc)}, 400)
            if p.startswith("/api/artifact?"):
                from urllib.parse import parse_qs, urlsplit
                import mimetypes
                query = parse_qs(urlsplit(p).query)
                try:
                    file = appapi.artifact_file(self.relay, query.get("task", [""])[0], query.get("path", [""])[0])
                    if file.stat().st_size > 50 * 1024 * 1024:
                        return reply({"error": "Artifact exceeds the 50 MB preview limit; open it locally."}, 413)
                    size = file.stat().st_size
                    try:
                        start, end, partial = reviewmedia.byte_range(self.headers.get("Range"), size)
                    except ValueError as exc:
                        return self._send(416, str(exc), "text/plain", {"Content-Range": f"bytes */{size}"})
                    with file.open("rb") as stream:
                        stream.seek(start)
                        data = stream.read(max(0, end - start + 1))
                    headers = {"Accept-Ranges": "bytes"}
                    if partial: headers["Content-Range"] = f"bytes {start}-{end}/{size}"
                    return self._send(206 if partial else 200, data, mimetypes.guess_type(file.name)[0] or "application/octet-stream", headers)
                except (ValueError, OSError) as exc:
                    return reply({"error": str(exc)}, 400)
            if p.startswith("/api/plan/status"): return reply(planner.job_status(p.split("job=")[-1] if "job=" in p else ""))
            if p == "/api/brand": return reply({"fluxLogo": flux_logo(), "credit": "Driven by Flux", "url": "https://flux.mk"})
            if p.startswith("/assets/"):
                name = p[len("/assets/"):].split("?")[0]
                f = ASSETS / name
                if ASSET_OK.match(name) and f.is_file():
                    ctype = "image/svg+xml" if name.endswith(".svg") else "image/png" if name.endswith(".png") else "image/x-icon" if name.endswith(".ico") else "application/octet-stream"
                    return self._send(200, f.read_bytes(), ctype)
                return self._send(404, "not found", "text/plain")
            if p == "/api/env": return reply(appapi.environment())
            if p == "/api/requests": return reply({"requests": appapi.list_requests(self.relay) if self.relay else []})
            if p == "/api/docs":
                d = {"proposal": "", "plan": ""}
                if self.relay:
                    for k, f in (("proposal", "PROPOSAL.md"), ("plan", "PLAN.md")):
                        fp = self.relay / f
                        if fp.exists(): d[k] = fp.read_text(encoding="utf-8", errors="replace")
                return reply(d)
            if p == "/api/wizard": return reply(wizard_state(self.relay))
            if p == "/api/tasks":
                nt = appapi.next_tasks(self.relay, 1) if self.relay else {}
                mode = appapi.relay_script(self.relay) if self.relay else ""
                tasks = appapi.list_tasks(self.relay) if self.relay else []
                queued = []
                if mode == "relay-files.sh":
                    pf = self.relay / "relay-priority.txt"
                    if pf.exists(): queued = [l.strip() for l in pf.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
                    for t in tasks: t["queuedNext"] = t["id"] in queued; t["approved"] = False
                elif mode == "relay.sh":
                    reqs = appapi.owner_requests(self.relay)
                    for t in tasks:
                        mine = [r for r in reqs if r["task"] == t["id"]]
                        t["queuedNext"] = any(r["kind"] == "next" for r in mine); t["fixRequested"] = any(r["kind"] == "fix" for r in mine)
                        t["approved"] = any(r["kind"] == "approved" for r in mine)
                return reply({"tasks": tasks, "project": str(self.relay) if self.relay else "",
                              "canAdd": mode == "relay-files.sh" and not (self.relay / "relay.sh").exists(),
                              "current": nt.get("current", ""), "mode": "files" if mode == "relay-files.sh" else "board" if mode == "relay.sh" else "none",
                              "actions": mode in ("relay-files.sh", "relay.sh")})
            if p == "/api/setup":
                s = appapi.detect_setup()
                manual = appapi.load_config().get("manualSignedIn", {})
                for x in (customai.load(self.relay) if self.relay else []):
                    s["tools"].append({"id": x["id"], "name": x.get("name", x["id"]), "role": "Custom AI you added", "needed": False, "custom": True,
                                       "found": customai.find_bin(x["bin"]), "how": "install it yourself, then press Find it again", "signedIn": True if manual.get(x["id"]) else None})
                s.update({"project": str(self.relay) if self.relay else "", "hasRelay": bool(self.relay and appapi.relay_script(self.relay)),
                          "running": bool(self.relay and appapi.relay_running(self.relay))})
                return reply(s)
            if p.startswith("/api/state"): return reply(build_state(self.relay, self.providers + custom_cards(self.relay)))
            if p in ("/", "/index.html"): return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            if p == "/api/settings": return reply(settings_state(self.relay))
            if p in ("/digest", "/digest/"):
                if not self.relay: return self._send(200, "<p>Choose a project first.</p>", "text/html; charset=utf-8")
                return self._send(200, digest.page(self.relay), "text/html; charset=utf-8")
            if p == "/digest.md":
                f = self.relay / "OWNER-REVIEW-DIGEST.md"
                return self._send(200, f.read_text(encoding="utf-8", errors="replace") if f.exists() else "No digest yet.", "text/plain; charset=utf-8")
        except Exception as ex:
            return reply({"error": str(ex)}, 500)
        self._send(404, "not found", "text/plain")


class PhoneHandler(Handler):
    """Requests from phones on the local network: a device token on everything except the pairing page, and only the endpoints in phone.py."""
    is_phone = True

    def _deny(self, code, msg):
        self._send(code, json.dumps({"ok": False, "error": msg}), "application/json")
        return False

    def _authed(self, need_control):
        auth = self.headers.get("Authorization", "")
        dev = phone.device_for(auth[7:].strip() if auth.startswith("Bearer ") else "")
        if not dev: return self._deny(401, "This phone is not paired, or was removed. Pair it again from the computer.")
        if need_control and dev.get("scope") != "control": return self._deny(403, "This phone can only view. Pair it again with control to act.")
        self.device = dev
        ip = self.client_address[0]
        phone.touch(dev["id"], "Tailscale" if ip in ("127.0.0.1", "::1") else ip)      # serve forwards from this computer
        return True

    def _get_gate(self):
        path = self.path.split("?", 1)[0]
        if path in phone.PUBLIC_GET or path.startswith("/assets/"): return True
        if path in phone.VIEW_GET: return self._authed(False)
        return self._deny(403, "Not available from a phone.")

    def _post_gate(self):
        origin = self.headers.get("Origin")
        if origin and origin not in ("http://" + self.headers.get("Host", ""), "https://" + self.headers.get("Host", "")):
            return self._deny(403, "Use the Agent Relay phone page.")
        path = self.path.split("?", 1)[0]
        if path in phone.PUBLIC_POST: return True
        if path in phone.CONTROL_POST: return self._authed(True)
        return self._deny(403, "Not available from a phone.")


def setup(argv=None):
    """Parse the options and bind the server (not serving yet). Returns (server, options)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", default=""); ap.add_argument("--open", action="store_true", help="open the page in the browser"); ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 only on a network you trust: there is no login")
    ap.add_argument("--providers", default=str(HERE / "providers.json"))
    a = ap.parse_args(argv)
    proj = a.relay or appapi.load_config().get("project", "")
    Handler.relay = pathlib.Path(proj).resolve() if proj and pathlib.Path(proj).is_dir() else None
    Handler.providers = json.loads(pathlib.Path(a.providers).read_text(encoding="utf-8"))["providers"]
    srv = ThreadingHTTPServer((a.host, a.port), Handler); Handler.srv = srv
    if phone._load()["enabled"]: phone.start_listener(PhoneHandler)
    return srv, a


def main():
    srv, a = setup()
    print(f"Agent relay dashboard for {Handler.relay}\n  http://{a.host}:{a.port}/   (Ctrl+C to stop)")
    if a.open and not os.environ.get("AGENT_RELAY_NO_BROWSER"):
        import webbrowser; webbrowser.open(f"http://{a.host}:{a.port}/")
    try: srv.serve_forever()
    except KeyboardInterrupt: pass


if __name__ == "__main__":
    main()
