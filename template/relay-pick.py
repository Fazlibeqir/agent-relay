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
