"""Parallel mode helper for relay-files.sh: which open tasks can run at the same time, and a check of the dispatcher's plan. No AI, no tokens.

  python relay-parallel.py candidates [repo]            JSON: open tasks that could start now (every task they depend on is finished)
  python relay-parallel.py count [repo]                 how many there are
  python relay-parallel.py prompt [repo] "<workers>"    the text sent to the dispatcher (candidates, builders, the reply format)
  python relay-parallel.py plan [repo] "<workers>"      read the dispatcher's reply on stdin; print one 'TASK|builder' line per valid lane (exit 1: fewer than two)
  python relay-parallel.py task [repo] T05              print the body of CURRENT_TASK.md for that task

The dispatcher chooses; this script only refuses what is unsafe to run together: a task that is not open, parked for the person, or waiting on an
unfinished task it names ('Depends on T10, T97'), a builder that is not allowed, two lanes for one builder or one task. The dispatcher's own
judgement covers what the files cannot say (two tasks that edit the same code).
"""
import importlib.util, json, pathlib, re, sys

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("relay_pick", HERE / "relay-pick.py")
pick = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pick)

ID = re.compile(r"\bT\d+[a-z]?\b")
DEPENDS = re.compile(r"(?i)\bdepend(?:s|encies| on)\b")
MAX_CANDIDATES = 12


def depends_on(task):
    """Task ids a task names after 'Depends on' (that line and the one after it)."""
    lines = task["text"].splitlines()
    found = set()
    for i, line in enumerate(lines):
        if DEPENDS.search(line):
            found.update(ID.findall(" ".join(lines[i:i + 2])[DEPENDS.search(line).start():]))
    found.discard(task["id"])
    return sorted(found, key=pick.key)


def candidates(repo):
    repo = pathlib.Path(repo)
    tasks = pick.load(repo)
    parked = pick.parked(repo)
    state = {t["id"]: t["state"] for t in tasks}
    out = []
    for t in tasks:
        if t["state"] != "todo" or t["id"] in parked: continue
        blockers = [d for d in depends_on(t) if state.get(d) == "todo"]
        if blockers: continue
        out.append({"id": t["id"], "title": t["title"], "rec": t["rec"], "depends": depends_on(t), "openGates": t["open_auto"]})
        if len(out) >= MAX_CANDIDATES: break
    return out


def prompt(repo, workers):
    rows = candidates(repo)
    lines = ["PARALLEL ROUND. You are the dispatcher. Several builders can work at the same time, each on a DIFFERENT task, each in its own copy of the repository.",
             "Builders you may use (at most one task each): " + ", ".join(workers) + ".",
             "Open tasks that could start now (earlier ones first):"]
    for r in rows:
        lines.append("- %s | %s | open AUTO gates: %s%s%s" % (r["id"], r["title"], r["openGates"],
                     " | depends on: " + ", ".join(r["depends"]) if r["depends"] else "", " | planner suggests: " + r["rec"] if r["rec"] else ""))
    lines += ["",
              "Choose between 2 and %d tasks that can be built at the same time without getting in each other's way:" % len(workers),
              "  - they must not edit the same files or depend on each other's results (read the task files if the titles are not enough);",
              "  - when unsure, prefer earlier tasks and fewer lanes; never choose a task that needs a decision from the person;",
              "  - give each task to a different builder; match strong builders to harder tasks. Builders may only be: " + ", ".join(workers) + ".",
              "Do not write code and do not edit any file. Reply with ONLY one JSON object, no other text:",
              '{"lanes": [{"task": "T05", "builder": "%s"}, {"task": "T07", "builder": "%s"}], "reason": "one sentence"}' % (workers[0], workers[-1])]
    return "\n".join(lines)


def extract_json(text):
    """The first JSON object in the reply that has a 'lanes' list (the dispatcher may wrap it in prose or a code fence)."""
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try: obj, _ = decoder.raw_decode(text[m.start():])
        except ValueError: continue
        if isinstance(obj, dict) and isinstance(obj.get("lanes"), list): return obj
    return None


def plan(repo, workers, reply):
    allowed = [w for w in workers if w in ("codex", "copilot", "opencode")]
    obj = extract_json(reply)
    if not obj: return []
    open_now = {c["id"] for c in candidates(repo)}
    lanes, used_tasks, used_builders = [], set(), set()
    for lane in obj["lanes"]:
        if not isinstance(lane, dict): continue
        task, builder = str(lane.get("task", "")).strip(), str(lane.get("builder", "")).strip().lower()
        if task not in open_now or builder not in allowed: continue
        if task in used_tasks or builder in used_builders: continue
        lanes.append((task, builder)); used_tasks.add(task); used_builders.add(builder)
        if len(lanes) >= len(allowed): break
    return lanes if len(lanes) >= 2 else []


def task_body(repo, task_id):
    for t in pick.load(pathlib.Path(repo)):
        if t["id"] == task_id:
            return "tasks/%s.md\n%s\nRECOMMENDED_AI: %s\n\n" % (t["id"], t["title"].replace(" - ", " — ", 1), t["rec"]) + t["text"]
    return None


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    repo = argv[2] if len(argv) > 2 else "."
    if cmd == "candidates": print(json.dumps(candidates(repo), indent=1))
    elif cmd == "count": print(len(candidates(repo)))      # no AI call is worth making for fewer than two
    elif cmd == "prompt": print(prompt(repo, (argv[3] if len(argv) > 3 else "codex copilot opencode").split()))
    elif cmd == "plan":
        lanes = plan(repo, (argv[3] if len(argv) > 3 else "codex copilot opencode").split(), sys.stdin.read())
        for task, builder in lanes: print("%s|%s" % (task, builder))
        return 0 if lanes else 1
    elif cmd == "task":
        body = task_body(repo, argv[3] if len(argv) > 3 else "")
        if body is None: return 1
        sys.stdout.write(body)
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.exit(main(sys.argv))
