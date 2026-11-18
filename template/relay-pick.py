"""Task picker for projects whose tasks are markdown files (tasks/T00.md, tasks/T01.md, ...), as the planner writes them. No AI, no tokens.

  python relay-pick.py pick [repo]              choose the next task, write CURRENT_TASK.md (or NO_TASKS), print the task id
  python relay-pick.py done T00 [repo]          exit 0 when that task is Done (all gates ticked), 1 otherwise
  python relay-pick.py list [repo]              print every task with its state
  python relay-pick.py guard <task.md> <backup> restore any HUMAN gate that is ticked now but was not in the backup; print what was restored

Task file shape (the planner writes it):
  # T01 - Title
  Status: Not started | Completion: 0% (0/3 gates)
  - [ ] AUTO: something a command proves
  - [ ] HUMAN: something the person tries
Rules: tasks go in id order and an earlier task with open AUTO gates blocks the later ones; a task whose AUTO gates are all ticked and whose HUMAN
gates are open waits for the person (it is skipped); a task listed as NEEDS OWNER in PLAYTEST_QUEUE.md is skipped too.
"""
import pathlib, re, sys

GATE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.*)$")


def key(name):
    m = re.match(r"T(\d+)([a-z]?)", name)
    return (int(m.group(1)), m.group(2)) if m else (10 ** 9, name)


def load(repo):
    tasks = []
    for f in sorted((repo / "tasks").glob("T*.md"), key=lambda p: key(p.stem)):
        text = f.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        title = lines[0].lstrip("# ").strip() if lines else f.stem
        gates = []
        for l in lines:
            m = GATE.match(l)
            if m: gates.append({"done": m.group(1) != " ", "text": m.group(2), "human": bool(re.match(r"\**HUMAN\b", m.group(2).strip(), re.I))})
        auto = [g for g in gates if not g["human"]]; human = [g for g in gates if g["human"]]
        open_auto = [g for g in auto if not g["done"]]; open_human = [g for g in human if not g["done"]]
        state = "done" if gates and not open_auto and not open_human else "owner" if gates and not open_auto else "todo"
        rec = ""
        for l in lines[:12]:
            m = re.match(r"(?i)^recommended ai:\s*(.*)$", l.strip())
            if m: rec = m.group(1)
        tasks.append({"id": f.stem, "file": f, "title": title, "text": text, "state": state, "rec": rec, "open_auto": len(open_auto), "gates": len(gates)})
    return tasks


def parked(repo):
    q = repo / "PLAYTEST_QUEUE.md"
    ids = set()
    if q.exists():
        for l in q.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^(T\d+[a-z]?)\b.*NEEDS OWNER", l)
            if m: ids.add(m.group(1))
    return ids


def pick(repo):
    tasks = load(repo); skip = parked(repo)
    cur = repo / "CURRENT_TASK.md"
    for t in tasks:
        if t["state"] == "todo" and t["id"] not in skip:
            head = "tasks/%s.md\n%s\nRECOMMENDED_AI: %s\n\n" % (t["id"], t["title"].replace(" - ", " — ", 1), t["rec"])
            cur.write_text(head + t["text"], encoding="utf-8")
            return t["id"]
        if t["state"] == "todo" and t["id"] in skip:
            continue    # parked for the person: later tasks may still go ahead only if they do not depend on it (not modelled: stop here to stay safe)
    cur.write_text("NO_TASKS\n", encoding="utf-8")
    return "NO_TASKS"


def guard(taskfile, backup):
    """A reviewer may tick AUTO gates; it may never tick a HUMAN gate. Put such a line back as it was."""
    now = pathlib.Path(taskfile).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    before = pathlib.Path(backup).read_text(encoding="utf-8", errors="replace").splitlines()
    open_human = {re.sub(r"\s+", " ", GATE.match(l).group(2)).strip() for l in before if GATE.match(l) and GATE.match(l).group(1) == " "
                  and re.match(r"\**HUMAN\b", GATE.match(l).group(2).strip(), re.I)}
    restored = []
    for i, l in enumerate(now):
        m = GATE.match(l.rstrip("\r\n"))
        if m and m.group(1) != " " and re.sub(r"\s+", " ", m.group(2)).strip() in open_human:
            now[i] = re.sub(r"\[[xX]\]", "[ ]", l, count=1); restored.append(m.group(2)[:60])
    if restored: pathlib.Path(taskfile).write_text("".join(now), encoding="utf-8")
    return restored


SENSITIVE = [r"(^|/)\.env(\.[^/]*)?$", r"\.(pem|key|p12|pfx|jks|keystore|ppk)$", r"(^|/)id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$", r"(^|/)\.(npmrc|pypirc|netrc|pgpass|git-credentials)$",
             r"(^|/)(credentials|secrets?)(\.[^/]*)?$", r"(^|/)service[-_]?account[^/]*\.json$", r"\.tfstate(\.backup)?$", r"(^|/)\.aws/", r"(^|/)\.ssh/", r"(password|passwd)[^/]*$"]
SAFE_NAMES = re.compile(r"\.(example|sample|template|dist|md|txt)$", re.I)
REQ_DIR = "relay-requests"


def is_sensitive(path):
    p = path.replace(chr(92), "/")
    if SAFE_NAMES.search(p) and not p.lower().endswith((".pem", ".key")): return False
    return any(re.search(rx, p, re.I) for rx in SENSITIVE)


def _req_files(repo):
    d = pathlib.Path(repo) / REQ_DIR
    return sorted(d.glob("*.json")) if d.is_dir() else []


def _req_load(f):
    import json
    try: r = json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError): return None
    if not isinstance(r, dict): return None
    r.setdefault("id", f.stem); r.setdefault("type", "other"); r.setdefault("status", "pending"); r.setdefault("executed", False)
    r["id"] = f.stem
    return r


def _req_save(repo, r):
    import json
    d = pathlib.Path(repo) / REQ_DIR; d.mkdir(exist_ok=True)
    (d / (r["id"] + ".json")).write_text(json.dumps(r, indent=1), encoding="utf-8", newline="\n")


def req_command(a, repo="."):
    import json, os, time
    sub = a[0] if a else ""
    if sub == "new":        # req new <type> <task> <action> <why> [files...]   (commit message in $REQ_MESSAGE)
        typ, task, action, why = a[1], a[2], a[3], a[4]
        rid = time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^a-z0-9]+", "-", typ.lower())
        n = 1; base = rid
        while (pathlib.Path(repo) / REQ_DIR / (rid + ".json")).exists(): n += 1; rid = "%s-%d" % (base, n)
        r = {"id": rid, "type": typ, "task": task, "action": action, "why": why, "files": a[5:], "message": os.environ.get("REQ_MESSAGE", ""), "status": "pending",
             "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"), "executed": False}
        _req_save(repo, r); print(rid)
    elif sub == "pending":
        print(sum(1 for f in _req_files(repo) if (_req_load(f) or {}).get("status") == "pending"))
    elif sub == "take":     # decided but not yet carried out: one JSON object per line
        for f in _req_files(repo):
            r = _req_load(f)
            if r and r["status"] in ("approved", "denied") and not r.get("executed"): print(json.dumps(r))
    elif sub == "done":     # req done <id> <result>
        for f in _req_files(repo):
            r = _req_load(f)
            if r and r["id"] == a[1]: r["executed"] = True; r["result"] = a[2] if len(a) > 2 else ""; _req_save(repo, r)
    elif sub == "prompt":   # what the person decided, for the builder's next prompt
        lines = []
        for f in _req_files(repo)[-30:]:
            r = _req_load(f)
            if not r or r["status"] == "pending": continue
            what = r.get("action") or r["type"]
            files = (" (" + ", ".join(r.get("files", [])[:6]) + ")") if r.get("files") else ""
            note = (" The person said: " + r["note"]) if r.get("note") else ""
            lines.append("- %s: %s%s.%s" % ("APPROVED" if r["status"] == "approved" else "DENIED: do not try again, find another way", what, files, note))
        if lines: print("DECISIONS BY THE PERSON (they own this project; respect them):" + chr(10) + chr(10).join(lines[-12:]))
    elif sub == "decide":   # req decide <id> <approve|deny> <by> <reason>
        for f in _req_files(repo):
            r = _req_load(f)
            if r and r["id"] == a[1] and r["status"] == "pending":
                r["status"] = "approved" if a[2] == "approve" else "denied"; r["decidedBy"] = a[3]; r["note"] = (a[4] if len(a) > 4 else "")[:300]
                r["decidedAt"] = time.strftime("%Y-%m-%d %H:%M:%S"); r["executed"] = False; _req_save(repo, r)
    elif sub == "verdict":  # reads an AI's answer on stdin, prints  decision|reason  (decision may be approve, deny or ask; nothing printed when unreadable)
        text = sys.stdin.read()
        for m in re.finditer(r"\{[^{}]*\}", text):
            try: v = json.loads(m.group(0))
            except ValueError: continue
            d = str(v.get("decision", "")).lower().strip()
            if d in ("approve", "deny", "ask"): print("%s|%s" % (d, str(v.get("reason", "")).replace(chr(10), " ")[:250])); break
    elif sub == "judge-prompt":   # req judge-prompt <id>
        for f in _req_files(repo):
            r = _req_load(f)
            if not (r and r["id"] == a[1]): continue
            rules = ""
            try: rules = "".join(open(pathlib.Path(repo) / "AGENTS.md", encoding="utf-8", errors="replace").readlines()[:40])
            except OSError: pass
            print("You are the safety judge for an automated coding relay. A coding AI asked for permission to do something it is not allowed to do on its own. Decide.\n\n"
                  "REQUEST\n  kind: %s\n  task: %s\n  action: %s\n  why: %s\n  details: %s\n  files: %s\n\nPROJECT RULES (start of AGENTS.md)\n%s\n\n"
                  "HOW TO DECIDE\n- approve only if it is clearly necessary for the named task, low risk, reversible, stays inside the project folder, sends no secret or data outside, "
                  "spends no money, makes no system-wide change and destroys nothing;\n- the explanation must be specific, not vague;\n- files that look like secrets (.env, keys, tokens, passwords) "
                  "or an upload of the work branch: approve only when it is plainly safe and wanted, otherwise deny;\n- deny when unsure;\n- answer ask when only the person can know "
                  "(for example it needs their account, their money, or their taste).\n\nAnswer with exactly one line of JSON and nothing else: "
                  "{\"decision\": \"approve\" or \"deny\" or \"ask\", \"reason\": \"one short sentence in plain words\"}"
                  % (r["type"], r.get("task", ""), r.get("action", ""), r.get("why", ""), r.get("details", ""), ", ".join(r.get("files", [])) or "(none)", rules))
    elif sub == "pending-list":
        for f in _req_files(repo):
            r = _req_load(f)
            if r and r["status"] == "pending": print("%s|%s|%s" % (r["id"], r["type"], (r.get("action") or "")[:80]))


KNOWN = ("claude", "codex", "copilot", "opencode")
EFFORT = re.compile(r"\b(none|minimal|low|medium|high|xhigh|max|ultra)\b\s+reasoning", re.I)


def recommended(taskfile, repo="."):
    """The 'Recommended AI:' line of a task as provider|model|effort (any part may be empty). Example line: Recommended AI: codex gpt-6.1-sol, high reasoning"""
    custom = set()
    try:
        import json
        for x in json.loads((pathlib.Path(repo) / "relay.providers.json").read_text(encoding="utf-8-sig")).get("providers", []): custom.add(x.get("id", ""))
    except (OSError, ValueError): pass
    raw = ""
    for l in pathlib.Path(taskfile).read_text(encoding="utf-8", errors="replace").splitlines()[:16]:
        m = re.match(r"(?i)^recommended ai:\s*(.*)$", l.strip())
        if m: raw = m.group(1); break
    words = raw.replace(",", " ").split()
    prov = words[0].lower() if words and (words[0].lower() in KNOWN or words[0].lower() in custom) else ""
    if not prov and re.search(r"gpt-[0-9.]+-\w+", raw, re.I): prov = "codex"
    eff = EFFORT.search(raw)
    skip = re.compile(r"(?i)^(reasoning|none|minimal|low|medium|high|xhigh|max|ultra|with|and|effort)$")
    rest = [w for w in (words[1:] if prov and words and words[0].lower() == prov else words) if not skip.match(w)]
    model = rest[0] if rest else ""
    if prov == "codex" and not model:
        m = re.search(r"gpt-[0-9.]+-\w+", raw, re.I); model = m.group(0).lower() if m else ""
    return "%s|%s|%s" % (prov, model, eff.group(1).lower() if eff else "")


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else "list"
    if cmd == "sensitive":
        for line in sys.stdin.read().splitlines():
            if line.strip() and is_sensitive(line.strip()): print(line.strip())
        sys.exit(0)
    if cmd == "req":
        req_command(a[1:]); sys.exit(0)
    if cmd == "rec":
        print(recommended(a[1], a[2] if len(a) > 2 else ".")); sys.exit(0)
    if cmd == "guard":
        r = guard(a[1], a[2]); print("\n".join(r)); sys.exit(0)
    repo = pathlib.Path(a[-1] if len(a) > (2 if cmd == "done" else 1) else ".").resolve()
    if cmd == "pick": print(pick(repo))
    elif cmd == "done":
        t = next((x for x in load(repo) if x["id"] == a[1]), None)
        sys.exit(0 if t and t["state"] == "done" else 1)
    elif cmd == "list":
        for t in load(repo): print("%-6s %-6s open AUTO %d / %d gates  %s" % (t["id"], t["state"], t["open_auto"], t["gates"], t["title"]))
    else: sys.exit("usage: relay-pick.py pick|done|list|guard")
