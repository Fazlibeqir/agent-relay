"""Agent relay dashboard: a local web page that shows what every AI model in an unattended relay is doing.

  python server.py --relay <relay-repo-dir> [--port 8765] [--host 127.0.0.1] [--providers providers.json]

Standard library only. It never calls an AI; the only file it writes is relay.settings.json (POST /api/settings, localhost only).
It reads relay.log (phase, task, rounds, limits), relay-live-<label>.log/.start (what each agent is doing right now),
relay-fallback-<tool>.count (fallback builds today), PLAYTEST_QUEUE.md (what waits for the owner), and each provider's own
usage source when it has one (Codex writes its rate limits into its session logs).
"""
import customai, existing, osops, planner, subprocess, threading, argparse, importlib.util, json, os, pathlib, re, statistics, tempfile, time, glob, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).parent
import sys
sys.path.insert(0, str(HERE))
import appapi
DEFAULTS = {"BASELINE": 600, "DISPATCH": 360, "BUILD": 900, "SUITE": 600, "REVIEW": 240}
PAIRS = {("BASELINE", "BASELINE_OK"): "BASELINE", ("DISPATCH", "BUILD"): "DISPATCH", ("BUILD", "SUITE"): "BUILD",
         ("SUITE", "REVIEW"): "SUITE", ("REVIEW", "ROUND_DONE"): "REVIEW"}
RED = re.compile(r"baseline tests FAIL|suite (FAIL|failed)|graphics FAIL|timed out|exited with rc|usage limit|dispatcher failed|"
                 r"dispatcher wrote no task|no new commits|stalled|uncommitted changes|refused to commit|out of usage|known timing flake", re.I)
LOG_LINE = re.compile(r"^\[(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$")


def kind_of(m):
    for pat, k in ((r"^running baseline test suite", "BASELINE"), (r"^baseline tests pass", "BASELINE_OK"), (r"^baseline tests FAIL", "STOPPED"),
                   (r"choosing next task", "DISPATCH"), (r"(Codex|builder) building", "BUILD"),
                   (r"made \d+ commit\(s\); running the full suite", "SUITE"), (r"Claude reviewing|reviewing", "REVIEW"),
                   (r"review done", "ROUND_DONE"), (r"^no task the agents can advance", "IDLE"), (r"hit a usage limit", "LIMIT"),
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
    """Claude reports its plan usage inside every `claude -p` call (a rate_limit_event) once it passes a warning level. Read the newest per
    window from the relay's last Claude call and remember it between calls (the live log is overwritten by the next call)."""
    labels = {"five_hour": "5-hour", "seven_day": "weekly", "seven_day_opus": "weekly (Opus)", "seven_day_sonnet": "weekly (Sonnet)"}
    store = appapi.APPDATA / "claude-usage.json"
    try: known = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError): known = {}
    f = relay / "relay-live-claude.log"
    changed = False
    for line in tail_bytes(f, 1_000_000).splitlines():
        if not line.startswith('{"type":"rate_limit_event"'): continue
        try: info = json.loads(line).get("rate_limit_info") or {}
        except ValueError: continue
        kind = info.get("rateLimitType")
        if kind and info.get("utilization") is not None:
            known[kind] = {"usedPercent": round(float(info["utilization"]) * 100), "resetsAt": info.get("resetsAt"), "label": labels.get(kind, kind),
                           "windowMinutes": None, "seenAt": os.path.getmtime(f)}
            changed = True
    if changed:
        try: appapi.APPDATA.mkdir(parents=True, exist_ok=True); store.write_text(json.dumps(known), encoding="utf-8")
        except OSError: pass
    now = time.time()
    wins = [w for w in known.values() if (w.get("resetsAt") or 0) > now]       # a window that has already reset is no longer true
    if not wins: return None
    newest = max(w["seenAt"] for w in wins)
    return {"windows": wins, "source": "from the last Claude call at " + time.strftime("%H:%M", time.localtime(newest)) + " (Claude only reports above 75%)", "asOf": newest}


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
    cur = markers[-1] if markers else None
    state = {"now": now, "alive": alive, "relay": str(relay), "branch": "", "task": "", "round": "", "phase": None, "estimates": est}
    for e in run:
        m = re.match(r"relay started on branch (\S+)", e["msg"])
        if m: state["branch"] = m.group(1); state["startedAt"] = e["t"]
        m = re.match(r"round (\d+) — (T\d+[a-z]?[^—]*— [^—]*?) — (?:Codex|\w+) building", e["msg"])
        if m: state["round"], state["task"] = m.group(1), m.group(2)
        m = re.match(r"round (\d+) — choosing", e["msg"])
        if m: state["round"] = m.group(1)
    pm = {"BASELINE": ("Baseline test suite", "BASELINE"), "DISPATCH": ("Claude picks the next task", "DISPATCH"), "BUILD": ("Building", "BUILD"),
          "SUITE": ("Full test suite", "SUITE"), "REVIEW": ("Claude reviews", "REVIEW")}
    if cur:
        k = cur["kind"]
        if k in pm and alive:
            label, ek = pm[k]
            elapsed = now - cur["t"]
            state["phase"] = {"kind": k, "label": label, "since": cur["t"], "elapsed": elapsed, "estimate": est[ek], "remaining": est[ek] - elapsed}
        elif k == "LIMIT" and alive:
            m = re.search(r"sleeping (\d+)s(?: \(until about (\d\d:\d\d)\))?", cur["msg"])
            secs = int(m.group(1)) if m else 1800
            state["phase"] = {"kind": "LIMIT", "label": "Waiting for a usage limit to reset", "since": cur["t"], "elapsed": now - cur["t"], "estimate": secs,
                              "remaining": secs - (now - cur["t"]), "until": m.group(2) if m and m.group(2) else None}
        elif not alive:
            state["phase"] = {"kind": "STOPPED", "label": "Relay stopped", "since": cur["t"], "elapsed": now - cur["t"], "estimate": 0, "remaining": 0}
    order = ["DISPATCH", "BUILD", "SUITE", "REVIEW"]
    if state["phase"] and state["phase"]["kind"] in order:
        i = order.index(state["phase"]["kind"]); rem = max(0, state["phase"]["remaining"]); nxt = []
        acc = rem
        for k in order[i + 1:]:
            acc += est[k]; nxt.append({"label": {"BUILD": "Codex builds", "SUITE": "Full test suite", "REVIEW": "Claude reviews"}[k], "inSeconds": acc})
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
        m = re.match(r"(\w+) hit a usage limit — sleeping (\d+)s(?: \(until about (\d\d:\d\d)\))?", e["msg"])
        if m: limits[m.group(1)] = {"at": e["t"], "ts": e["ts"], "until": m.group(3)}
    fb = fallback_counts(relay)
    state["providers"] = []
    for p in providers:
        row = dict(p)
        pid = p["id"]
        row["model"] = {"claude": claude_model, "codex": codex_model}.get(pid, p.get("defaultModel", ""))
        row["live"] = live.get(pid) or {"running": False, "state": "idle", "actions": []}
        row["lastLimit"] = limits.get(pid)
        row["fallbackBuildsToday"] = fb.get(pid)
        row["usage"] = codex_usage() if p.get("usage") == "codex-session-log" else (claude_usage(relay) if p.get("usage") == "claude-stream" else None)
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
    return state


CATALOG = {"at": 0, "data": None}
CLAUDE_MODELS = ["claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
GENERIC_EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def model_catalog():
    """Models and reasoning levels each provider offers, for the settings dropdowns. Cached for 10 minutes."""
    if CATALOG["data"] and time.time() - CATALOG["at"] < 600: return CATALOG["data"]
    cat = {"claude": {"models": [{"id": m, "label": m} for m in CLAUDE_MODELS], "efforts": GENERIC_EFFORTS},
           "copilot": {"models": [{"id": "auto", "label": "auto (Copilot picks)"}], "efforts": GENERIC_EFFORTS},
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
        info = {}
        try: info = json.loads((r / "relay.project.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError): pass
        tasks = appapi.list_tasks(r)
        remote = ""
        try:
            rc = subprocess.run(["git", "-C", str(r), "remote", "get-url", "origin"], capture_output=True, text=True, creationflags=appapi.NO_WINDOW)
            remote = rc.stdout.strip() if rc.returncode == 0 else ""
        except OSError: pass
        proj = {"path": str(r), "exists": True, "name": info.get("name") or r.name, "idea": info.get("idea", ""), "stack": info.get("stack", []),
                "testCommand": info.get("testCommand", ""), "hasProposal": (r / "PROPOSAL.md").exists(), "hasPlan": bool(tasks) and (r / "AGENTS.md").exists(),
                "taskCount": len(tasks), "tasksReady": existing.formatted_tasks(r)[0], "hasSource": (r / existing.SOURCE_NAME).exists(), "hasRelay": bool(appapi.relay_script(r)), "script": appapi.relay_script(r), "running": appapi.relay_running(r), "remote": remote,
                "requires": [s for s in (appapi.toolchain_status(x) for x in info.get("requires", []) if isinstance(x, str)) if s]}
    return {"project": proj, "githost": appapi.githost_status()}


class Handler(BaseHTTPRequestHandler):
    relay = None; providers = []

    def log_message(self, *a): pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)

    def _json_body(self):
        try: return json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 20000)) or b"{}")
        except ValueError: return {}

    def _reviewer_model(self):
        try:
            cfg, _p, _ok = settings_module(self.relay).effective(str(self.relay / "relay.settings.json"))
            return cfg["roles"]["reviewer"].get("model", "claude-sonnet-5-5")
        except Exception:
            return "claude-sonnet-5-5"

    def do_POST(self):
        # every action here changes this computer: accepted from this computer only, never over the network
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return self._send(403, json.dumps({"error": "actions can only be used from this computer"}), "application/json")
        body = self._json_body(); path = self.path
        def reply(obj, code=200): self._send(code, json.dumps(obj), "application/json")
        def need_project():
            if Handler.relay is None: reply({"ok": False, "error": "Choose or create a project first."}, 400); return True
            return False
        try:
            if path == "/api/quit":
                reply({"ok": True}); threading.Thread(target=Handler.srv.shutdown, daemon=True).start(); return
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
                r = appapi.create_project(body.get("name", ""), body.get("root", ""))
                if not r.get("ok"): return reply(r, 400)
                Handler.relay = pathlib.Path(r["path"])
                host = body.get("host", "local")
                if host in ("github", "gitlab") and body.get("createRemote"):
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
            if path == "/api/task/recommend":
                if need_project(): return
                return reply(appapi.set_task_rec(Handler.relay, body.get("task", ""), body.get("provider", ""), body.get("model", ""), body.get("effort", "")))
            if need_project(): return
            if path == "/api/relay/start": return reply(appapi.start_relay(Handler.relay))
            if path == "/api/relay/stop": return reply(appapi.stop_relay(Handler.relay))
            if path == "/api/settings":
                ok, problems = save_settings(self.relay, body)
                return reply({"saved": ok, **problems}, 200 if ok else 400)
        except Exception as ex:
            return reply({"ok": False, "saved": False, "errors": [str(ex)], "error": str(ex)}, 400)
        self._send(404, "not found", "text/plain")

    def do_GET(self):
        p = self.path
        def reply(obj, code=200): self._send(code, json.dumps(obj), "application/json")
        try:
            if p == "/api/models": return reply(with_custom(model_catalog(), self.relay))
            if p.startswith("/api/plan/status"): return reply(planner.job_status(p.split("job=")[-1] if "job=" in p else ""))
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
            if p == "/api/tasks": return reply({"tasks": appapi.list_tasks(self.relay) if self.relay else []})
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
            if p == "/digest":
                f = self.relay / "OWNER-REVIEW-DIGEST.md"
                return self._send(200, f.read_text(encoding="utf-8", errors="replace") if f.exists() else "No digest yet.", "text/plain; charset=utf-8")
        except Exception as ex:
            return reply({"error": str(ex)}, 500)
        self._send(404, "not found", "text/plain")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", default=""); ap.add_argument("--open", action="store_true", help="open the page in the browser"); ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 only on a network you trust: there is no login")
    ap.add_argument("--providers", default=str(HERE / "providers.json"))
    a = ap.parse_args()
    proj = a.relay or appapi.load_config().get("project", "")
    Handler.relay = pathlib.Path(proj).resolve() if proj and pathlib.Path(proj).is_dir() else None
    Handler.providers = json.loads(pathlib.Path(a.providers).read_text(encoding="utf-8"))["providers"]
    srv = ThreadingHTTPServer((a.host, a.port), Handler); Handler.srv = srv
    print(f"Agent relay dashboard for {Handler.relay}\n  http://{a.host}:{a.port}/   (Ctrl+C to stop)")
    if a.open:
        import webbrowser; webbrowser.open(f"http://{a.host}:{a.port}/")
    try: srv.serve_forever()
    except KeyboardInterrupt: pass


if __name__ == "__main__":
    main()
