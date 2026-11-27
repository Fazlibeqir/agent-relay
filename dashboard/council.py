"""Plan review: one AI drafts a plan for a project, the other AIs read it and say what is missing, what could be better and what cannot be done yet,
and the drafter revises it, until every reviewer approves or the round limit is reached.

Nothing here touches the project. Every file lives in the app's own folder (APPDATA/plans/<project>/<run>/): a context pack the app builds from the
project's Markdown files (or from a remote repository, or from notes the person gives), then draft.md / review-<ai>.md / decisions.md per round.
The AIs get no shell: planner._provider_cmd gives them file tools only, and their working folder is the run folder.

The flow per round:  draft (round 1) or revise (later rounds)  ->  every reviewer, in parallel  ->  stop when all approve, else revise and repeat.
A reviewer that fails, hits a usage limit or writes nothing is recorded as skipped; it never fails the run."""
import json, os, pathlib, re, shutil, subprocess, threading, time, uuid

import appapi, planner

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
RUNS = {}                       # run id -> state (also saved as run.json in the run folder)
LOCK = threading.Lock()
MAX_ROUNDS = 4
CALL_TIMEOUT = 1500

# What goes into the context pack. The AIs read these files; nobody pastes the whole repository into a prompt.
MAX_FILE = 60_000               # bytes of one file kept (longer files keep their start and end)
MAX_TOTAL = 400_000             # bytes of all files together
MAX_FILES = 80
MAX_TASK_FILES = 20             # the task index lists every task; only this many task files are copied whole
TASK_INDEX_MAX = 400
SKIP_DIRS = {".git", "node_modules", ".godot", ".venv", "venv", "__pycache__", "dist", "build", "target", ".relay", "work", "graphify-out", "evidence", "assets", ".import"}
TEXT_EXT = (".md", ".markdown", ".txt", ".rst")
# Build and CI files the plan has to be checked against (a reviewer cannot say how something is built or released without them): top level only, plus the CI workflows.
CONFIG_NAMES = {"package.json", "pyproject.toml", "requirements.txt", "setup.py", "pom.xml", "build.gradle", "cargo.toml", "go.mod", "makefile", "dockerfile",
                "compose.yaml", "compose.yml", "docker-compose.yml", "project.godot", "export_presets.cfg", "relay.project.json", "tsconfig.json"}
CONFIG_PATTERNS = (re.compile(r"build[-_a-z0-9]*\.(sh|ps1|cmd|bat|py)$", re.I), re.compile(r"(run|test|release|deploy)[-_a-z0-9]*\.(sh|ps1)$", re.I))
FIRST = ("README", "AGENTS", "PLAN", "SPEC", "RESTART", "ROADMAP", "ARCHITECTURE", "DESIGN", "DECISIONS", "BACKLOG", "TODO")
BIG_LOGS = ("PROGRESS", "HANDOFF", "CHANGELOG", "PLAYTEST", "OWNER-REVIEW", "PENDING", "FOLLOW-UP")


# ---------------------------------------------------------------- prompts
DRAFT = """You are the lead planner (the dispatcher) for a software project. Your job is to write a complete, honest plan for it.

What the person wants from this plan:
<<<
{goal}
>>>

Everything you may read is in the folder {run}: start with context/INDEX.md, then read the files it lists under context/files/. {extra}
Do not write anywhere except the file named below. Do not run commands.

Write {run}/rounds/{n}/draft.md with these sections:
## 1. What the project is and where it stands
Only what the files show. Say plainly what you could not see.
## 2. Goal and what "done" looks like
## 3. Milestones, in order
For each: what is built, how we will know it works (a test or a thing the owner can try), what it depends on, who must decide something (the owner, or a person) before it can start.
## 4. What can start now, and what is blocked
Anything that cannot be built or checked yet: say exactly what unblocks it.
## 5. Risks and unknowns
## 6. Questions for the owner
At most 8, each with the assumption you are making until it is answered.

Be specific to this project: use its real names, tasks and files. Keep it under 250 lines."""

REVIEW = """You are one of several AIs reviewing a plan for a software project. Another AI wrote it; you are not the author, so say what you really think.

The plan is {run}/rounds/{n}/draft.md. The project's own files are described in {run}/context/INDEX.md and stored under {run}/context/files/. Read the plan first, then check it against those files.
{prior}
Write ONLY the file {run}/rounds/{n}/review-{who}.md with exactly these sections:
## Missing
Things the plan leaves out that the project needs. One bullet each, with the file or task that shows it.
## Ideas
Better ways to do something the plan already covers, or extra things worth adding. One bullet each, with why.
## Cannot be done yet
Items that cannot work until something else exists, is decided, or is fixed. For each: why, and what unblocks it.
## Disagree
Anything in the plan you think is wrong or too risky, with your reason. Write "none" if there is nothing.
## Verdict
One of: the plan is good enough to start, or it needs changes before it is. One sentence of reasoning.

The very last line of the file must be exactly one of these (nothing after it):
VERDICT: APPROVE
VERDICT: CHANGES <number of points you think must change>

Do not rewrite the plan. Do not run commands. Keep it under 120 lines."""

REVISE = """You are the lead planner. You wrote the plan {run}/rounds/{n}/draft.md and these reviewers read it:
{reviews}

Read each review file in {run}/rounds/{n}/. Then write two files and nothing else:
1. {run}/rounds/{m}/draft.md - the improved plan, same six sections as before. Take a suggestion when it is right; keep your own choice when it is better, and say so in decisions.md.
2. {run}/rounds/{m}/decisions.md - a table with one row per point the reviewers raised: who raised it, the point (short), ACCEPTED or REJECTED or DEFERRED, and the reason in one sentence. Then a list "Still blocked" and "Questions only the owner can answer".

Do not run commands. Keep the plan under 250 lines."""

# The same loop reviews the app's own design: the lead writes the findings, the others add, dispute and re-rank them, the lead merges.
UX_DRAFT = """You are the lead designer reviewing the user interface and user experience of a desktop and phone app called Agent Relay. It runs coding AIs unattended; its user is a busy owner
who checks it from time to time, often on a phone, and is not a programmer. Your findings will be handed to engineers to fix, so they must be specific and checkable.

What the person wants from this review:
<<<
{goal}
>>>

Everything you may read is in the folder {run}: start with context/INDEX.md. It lists the page's source files (HTML, CSS, JavaScript) and ui-snapshot.md, which the page produced about itself:
the text and controls of every tab, measured checks (unnamed controls, small tap targets, low contrast, overflow at phone and tablet width). Treat the measured checks as facts. You cannot see the screen: say so where a
judgement needs the picture, and base your findings on the source and the snapshot. {extra}
Do not write anywhere except the file named below. Do not run commands.

Write {run}/rounds/{n}/draft.md with these sections:
## 1. What works well
Three to six things to keep, so they are not broken by the fixes.
## 2. Findings
A table, most serious first: ID (U1, U2, ...), severity (blocker, major, minor, polish), where (tab and element), what is wrong, who it hurts and why, the fix (one concrete change), effort (S, M, L).
Cover: first-run clarity and wording, what the owner needs to know at a glance, labels and jargon, buttons that are unclear or in the wrong place, empty and error and loading states, status that can be wrong or stale,
information overload, keyboard use and focus, screen-reader names, colour contrast, tap size, phone width, dark mode, and anything that can lose the owner's work or start something expensive by accident.
## 3. Quick wins
The findings that take under an hour each.
## 4. Bigger changes
Anything that needs a redesign, with a sketch in words.
## 5. Questions for the owner
At most 6.

Be specific: quote the real label text and the element. Keep it under 250 lines."""

UX_REVIEW = """You are one of several AIs reviewing the user interface and user experience of the app Agent Relay. Another AI wrote the findings; you are not the author, so say what you really think.

The findings are in {run}/rounds/{n}/draft.md. The page's source and what the page measured about itself are described in {run}/context/INDEX.md (see ui-snapshot.md). You cannot see the screen.
{prior}
Write ONLY the file {run}/rounds/{n}/review-{who}.md with exactly these sections:
## Missing
Problems the findings do not mention (a screen, a state, a flow, an accessibility problem), each with where it is and the fix.
## Ideas
Better fixes than the ones proposed, or design ideas that would make the app clearer. One bullet each, with why.
## Cannot be judged yet
Findings you cannot verify without seeing the screen or running the app, and how to check them.
## Disagree
Findings that are wrong, overstated, or ranked at the wrong severity, with your reason. Write "none" if there is nothing.
## Verdict
One sentence.

The very last line of the file must be exactly one of these (nothing after it):
VERDICT: APPROVE
VERDICT: CHANGES <number of findings you think must change or be added>

Do not rewrite the findings. Do not run commands. Keep it under 120 lines."""

UX_REVISE = """You are the lead designer. You wrote the findings {run}/rounds/{n}/draft.md and these reviewers read them:
{reviews}

Read each review in {run}/rounds/{n}/. Then write two files and nothing else:
1. {run}/rounds/{m}/draft.md - the improved findings with the same sections: add the missing problems, fix severities, take better fixes, drop findings that were wrong.
2. {run}/rounds/{m}/decisions.md - a table with one row per point the reviewers raised: who, the point (short), ACCEPTED or REJECTED or DEFERRED, and the reason in one sentence.

Do not run commands. Keep the findings under 250 lines."""

PROMPTS = {"plan": (DRAFT, REVIEW, REVISE), "ux": (UX_DRAFT, UX_REVIEW, UX_REVISE)}
UI_FILES = ("index.html", "phone.html", "dialogs.css", "dialogs.js", "review.js", "council.js")
UI_FILE_MAX = 260_000


# ---------------------------------------------------------------- the context pack
class PackError(Exception):
    pass


def safe_repo_url(text):
    """A remote repository the person names: https://host/path, or owner/repo meaning GitHub. Anything else is refused."""
    t = (text or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,100}", t) and not t.startswith(("-", ".")):
        return "https://github.com/" + t.removesuffix(".git") + ".git"
    if re.fullmatch(r"https://[A-Za-z0-9.-]{1,100}(:\d{1,5})?/[A-Za-z0-9_./~%+-]{1,200}", t) and ".." not in t:
        return t
    raise PackError("Give the repository as owner/repo or as an https:// address.")


def clone_remote(url, dest):
    """A shallow, read-only copy of a remote repository. No hooks, no prompts, no submodules; nothing is ever pushed."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ); env["GIT_TERMINAL_PROMPT"] = "0"
    empty = dest.parent / "_notemplate"; empty.mkdir(exist_ok=True)
    r = subprocess.run(["git", "clone", "--depth", "1", "--no-tags", "--filter=blob:limit=2m", "--template=" + str(empty), "-c", "core.hooksPath=" + str(empty),
                        "--", url, str(dest)], capture_output=True, text=True, timeout=300, env=env, creationflags=NO_WINDOW)
    shutil.rmtree(empty, ignore_errors=True)
    if r.returncode:
        raise PackError("Could not copy the repository (private, or the address is wrong): " + (r.stderr or "").strip().splitlines()[-1:][0][:200] if (r.stderr or "").strip() else "Could not copy the repository.")
    shutil.rmtree(dest / ".git", ignore_errors=True)
    return dest


def _walk_text(root):
    """Every Markdown/text file under root, skipping generated or huge folders: (relative posix path, absolute path, size)."""
    out = []
    root = pathlib.Path(root)
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") or d in (".github",))
        for f in sorted(files):
            rel_dir = pathlib.Path(dirpath).relative_to(root).as_posix()
            top = rel_dir == "."
            config = (top and (f.lower() in CONFIG_NAMES or any(c.match(f) for c in CONFIG_PATTERNS))) or (rel_dir == ".github/workflows" and f.lower().endswith((".yml", ".yaml")))
            if not (f.lower().endswith(TEXT_EXT) or config): continue
            p = pathlib.Path(dirpath) / f
            try: size = p.stat().st_size
            except OSError: continue
            out.append((p.relative_to(root).as_posix(), p, size))
    return out


def _rank(rel):
    """Lower is read first: the files that explain the project come before the logs."""
    name = pathlib.PurePosixPath(rel).name.upper(); depth = rel.count("/")
    if any(name.startswith(k) for k in BIG_LOGS): return (4, depth, rel)
    if rel.startswith("tasks/"): return (3, depth, rel)
    if any(name.startswith(k) for k in FIRST): return (0, depth, rel)
    if rel.startswith("docs/") or rel.startswith("planning/"): return (1, depth, rel)
    if not name.endswith(TEXT_EXT) or rel.startswith(".github/"): return (1, depth + 1, rel)           # build and CI files
    return (2, depth, rel)


def _clip(data):
    if len(data) <= MAX_FILE: return data, False
    half = MAX_FILE // 2
    return data[:half] + "\n\n[... the middle of this file was left out: it is %d bytes long ...]\n\n" % len(data) + data[-half:], True


def task_index(root):
    """One line per task file (id, title, status, completion, dependencies, whether a person must sign off): the whole backlog in a few KB instead of 180 files."""
    rows = []
    for p in sorted(pathlib.Path(root, "tasks").glob("T*.md"), key=lambda x: (len(x.stem), x.stem)) if pathlib.Path(root, "tasks").is_dir() else []:
        try: text = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError: continue
        lines = text.splitlines()
        title = re.sub(r"^#\s*", "", lines[0]).strip()[:110] if lines else p.stem
        status = (re.search(r"^Status:\s*(.+)$", text, re.M) or [None, ""])[1].strip()[:70]
        deps = (re.search(r"^Dependencies:\s*(.+)$", text, re.M) or [None, ""])[1].strip()[:60]
        human = "sign-off needed" if re.search(r"-\s*\[x\]\s*Human signoff required", text, re.I) else ""
        rows.append("- %s | %s | %s%s%s" % (title, status or "no status line", ("deps: " + deps + " | ") if deps else "", human, ""))
        if len(rows) >= TASK_INDEX_MAX: rows.append("- (more task files not listed)"); break
    return rows


def build_pack(source_dirs, out, notes="", note_files=()):
    """context/INDEX.md plus context/files/...: the project's Markdown (ranked, capped), a task index and the person's own notes. Returns a summary."""
    out = pathlib.Path(out); files_dir = out / "context" / "files"; files_dir.mkdir(parents=True, exist_ok=True)
    listed, total, skipped, tasks_seen = [], 0, [], 0
    for label, root in source_dirs:
        root = pathlib.Path(root)
        cand = sorted(_walk_text(root), key=lambda t: _rank(t[0]))
        for rel, p, size in cand:
            if rel.startswith("tasks/"):
                tasks_seen += 1
                if tasks_seen > MAX_TASK_FILES: continue
            if len(listed) >= MAX_FILES or total >= MAX_TOTAL: skipped.append("%s/%s" % (label, rel)); continue
            try: data = p.read_text(encoding="utf-8-sig", errors="replace")
            except OSError: continue
            data, clipped = _clip(data)
            if total + len(data.encode("utf-8")) > MAX_TOTAL: skipped.append("%s/%s" % (label, rel)); continue
            dest = files_dir / label / rel; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_text(data, encoding="utf-8")
            total += len(data.encode("utf-8")); listed.append(("files/%s/%s" % (label, rel), size, clipped))
    for i, nf in enumerate(note_files):
        nf = pathlib.Path(nf)
        try: data = nf.read_text(encoding="utf-8-sig", errors="replace")
        except OSError: continue
        data, clipped = _clip(data)
        dest = files_dir / "notes" / ("%02d-%s" % (i + 1, re.sub(r"[^A-Za-z0-9._-]", "_", nf.name)[:60])); dest.parent.mkdir(parents=True, exist_ok=True); dest.write_text(data, encoding="utf-8")
        listed.append((dest.relative_to(out / "context").as_posix(), nf.stat().st_size, clipped)); total += len(data.encode("utf-8"))
    if notes.strip():
        n = files_dir / "notes" / "owner-notes.md"; n.parent.mkdir(parents=True, exist_ok=True); n.write_text(notes.strip() + "\n", encoding="utf-8")
        listed.insert(0, ("files/notes/owner-notes.md", n.stat().st_size, False))
    index = ["# Context pack", "",
             "Built by the app from the project's own Markdown files. %d files, %d KB. A file marked (clipped) keeps its start and end only." % (len(listed), total // 1024), "",
             "## Files (read in this order)"]
    index += ["- context/%s (%d KB%s)" % (rel, max(1, size // 1024), ", clipped" if clipped else "") for rel, size, clipped in listed]
    if tasks_seen > MAX_TASK_FILES: index += ["", "## Task files: %d of %d copied whole; every task is in the task index below" % (MAX_TASK_FILES, tasks_seen)]
    if skipped: index += ["", "## Not included (over the size limit): %d files" % len(skipped)] + ["- " + s for s in skipped[:40]]
    for label, root in source_dirs:
        rows = task_index(root)
        if rows: index += ["", "## Task index (%s): title | status | dependencies | sign-off" % label] + rows
        top = sorted(x.name + ("/" if x.is_dir() else "") for x in pathlib.Path(root).iterdir() if not x.name.startswith(".") and x.name not in SKIP_DIRS)[:60]
        index += ["", "## Top-level layout (%s)" % label, ", ".join(top) or "(empty)"]
    (out / "context" / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    return {"files": len(listed), "kilobytes": total // 1024, "skipped": len(skipped)}


def build_ui_pack(app_dir, out, snapshot=""):
    """context/files/ui/...: the page's own source files whole (they are the thing under review) and ui-snapshot.md, what the page measured about itself."""
    out = pathlib.Path(out); ui = out / "context" / "files" / "ui"; ui.mkdir(parents=True, exist_ok=True)
    listed = []
    for name in UI_FILES:
        p = pathlib.Path(app_dir) / name
        try: data = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError: continue
        clipped = len(data) > UI_FILE_MAX
        if clipped: data = data[:UI_FILE_MAX] + "\n[... clipped ...]\n"
        (ui / name).write_text(data, encoding="utf-8"); listed.append((name, len(data), clipped))
    snap = (snapshot or "").strip()
    if snap: (ui / "ui-snapshot.md").write_text(snap[:200_000] + "\n", encoding="utf-8")
    index = ["# Context pack: the app's user interface", "",
             "The app is one HTML page (index.html) with its scripts and styles, plus a separate phone page (phone.html). Tabs: Overview, Plan, Tasks, AIs & settings.", "",
             "## Files (read ui-snapshot.md first, then the source)"]
    if snap: index.append("- context/files/ui/ui-snapshot.md (what the page measured about itself: text, controls and checks per tab)")
    else: index.append("- (no snapshot was sent: judge from the source only, and say so)")
    index += ["- context/files/ui/%s (%d KB%s)" % (n, max(1, s // 1024), ", clipped" if c else "") for n, s, c in listed]
    (out / "context" / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    return {"files": len(listed) + (1 if snap else 0), "kilobytes": sum(s for _, s, _ in listed) // 1024, "skipped": 0}


# ---------------------------------------------------------------- running the AIs
def run_ai(provider, found, model, effort, prompt, cwd, timeout=CALL_TIMEOUT):
    """One AI call with file tools only. Returns (exit code or None, last 1500 characters of output)."""
    cmd, env = planner._provider_cmd(provider, found, model, cwd, effort)
    if not cmd: return None, "unknown AI " + str(provider)
    if provider == "codex" and "--skip-git-repo-check" not in cmd:
        cmd.insert(cmd.index("exec") + 1, "--skip-git-repo-check")      # the run folder is in the app's data, not a git repository; Codex refuses to start in one otherwise
    e = dict(os.environ); e.update(env)
    try:
        invocation = cmd + ["-p", prompt] if provider == "copilot" else cmd
        r = subprocess.run(invocation, input="" if provider == "copilot" else prompt, text=True, encoding="utf-8", errors="replace", capture_output=True,
                           cwd=str(cwd), env=e, timeout=timeout, creationflags=NO_WINDOW)
        return r.returncode, ((r.stdout or "") + (r.stderr or ""))[-1500:]
    except subprocess.TimeoutExpired:
        return None, "took longer than %d minutes and was stopped" % (timeout // 60)
    except OSError as ex:
        return None, str(ex)


def parse_verdict(text):
    """('APPROVE', 0) / ('CHANGES', n) from the last line of a review, or (None, 0) when there is none."""
    for line in reversed([l.strip() for l in (text or "").splitlines() if l.strip()][-3:]):
        m = re.fullmatch(r"VERDICT:\s*(APPROVE|CHANGES)(?:\s+(\d+))?", line, re.I)
        if m: return m.group(1).upper(), int(m.group(2) or 0)
    return None, 0


def limit_hit(out):
    return bool(re.search(r"usage limit|rate.?limit|quota|too many requests|limit reached|exhausted", out or "", re.I))


def usable_ais(relay):
    """The AIs that are installed and signed in right now: [(id, path)]. OpenCode needs no sign-in for its free models."""
    res = []
    for t in appapi.detect_setup()["tools"]:
        if t["found"] and t["id"] in planner.PLANNER_PROVIDERS and (t["signedIn"] is True or t["id"] == "opencode"):
            # Codex: the desktop app's copy when there is one (the relay and the access check use it); an older npm copy on PATH can lack the account's default model
            found = (appapi.access_launcher("codex") or t["found"]) if t["id"] == "codex" else t["found"]
            res.append((t["id"], found))
    return res


def pick_model(settings, provider):
    """The model the person already set for this AI in the settings (any role), else its default."""
    roles = (settings or {}).get("roles") or {}
    for role in ("dispatcher", "builder", "reviewer"):
        r = roles.get(role) or {}
        if r.get("provider") == provider and r.get("model"):
            model, effort = r["model"], r.get("effort", "")
            if model.lower() == "auto" and provider != "copilot": model = ""      # 'auto' in the settings means the AI's own default, not a model name
            if str(effort).lower() == "auto": effort = ""
            return model, effort
    if provider == "opencode": return "opencode/deepseek-v4.1-flash-free", ""
    return "", ""


def plan_calls(n_reviewers, rounds):
    """(fewest, most) AI calls: one draft, then per round the reviews, and a revision unless everyone approves."""
    return 1 + n_reviewers, 1 + rounds * n_reviewers + (rounds - 1)


# ---------------------------------------------------------------- the run
def _runs_root(relay):
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", pathlib.Path(str(relay)).name)[:40] or "project"
    return appapi.APPDATA / "plans" / slug


def _save(run):
    try: (pathlib.Path(run["dir"]) / "run.json").write_text(json.dumps(run, indent=1, default=str), encoding="utf-8")
    except OSError: pass


def _step(run, label):
    run["step"] = label; run["updated"] = time.time(); _save(run)


def _read(p):
    try: return pathlib.Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError: return ""


def _one_round(run, n, drafter, reviewers, revising_from=None):
    """Round n: the draft (or revision) exists already; run every reviewer in parallel and record the verdicts."""
    rd = pathlib.Path(run["dir"]); round_dir = rd / "rounds" / str(n)
    rec = {"n": n, "reviews": [], "revise": None}
    run["rounds"].append(rec)
    prior = ""
    if n > 1:
        prior = "This is round %d. The plan was revised after the last round; %s/rounds/%d/decisions.md says what was accepted and rejected. Judge the new plan on its merits and do not repeat points that were answered." % (n, str(rd).replace(chr(92), "/"), n)
    threads = []
    for prov, found, model, effort in reviewers:
        item = {"provider": prov, "model": model or "default", "state": "waiting", "verdict": None, "points": 0, "note": ""}
        rec["reviews"].append(item)

        def work(prov=prov, found=found, model=model, effort=effort, item=item):
            if run.get("stop"): item.update(state="skipped", note="stopped"); return
            item["state"] = "reading"; item["started"] = time.time(); _save(run)
            rcode, out = run_ai(prov, found, model, effort, PROMPTS[run.get("kind", "plan")][1].format(run=str(rd).replace(chr(92), "/"), n=n, who=prov, prior=prior), rd)
            f = round_dir / ("review-%s.md" % prov)
            text = _read(f)
            if not text.strip():
                item.update(state="skipped", note=("usage limit: " if limit_hit(out) else "no review written: ") + (out or "")[-160:].strip().replace("\n", " "))
            else:
                v, k = parse_verdict(text)
                if v is None:
                    v, k = "CHANGES", 1; item["note"] = "no VERDICT line: counted as wanting changes"
                item.update(state="done", verdict=v, points=k)
            item["seconds"] = int(time.time() - item["started"]); _save(run)
        t = threading.Thread(target=work, daemon=True); threads.append(t); t.start()
    for t in threads: t.join()
    return rec


def _drive(run, relay_path, goal, drafter, reviewers, max_rounds, start_round=1):
    rd = pathlib.Path(run["dir"])
    dprov, dfound, dmodel, deffort = drafter
    try:
        n = start_round
        while n <= max_rounds and not run.get("stop"):
            round_dir = rd / "rounds" / str(n); round_dir.mkdir(parents=True, exist_ok=True)
            if n == start_round and not (round_dir / "draft.md").exists():
                run["draft"] = {"provider": dprov, "state": "writing", "started": time.time()}; _step(run, "%s is drafting the plan" % dprov)
                extra = "The person's notes, if any, are in context/files/notes/." if any((rd / "context/files/notes").glob("*")) else ""
                rcode, out = run_ai(dprov, dfound, dmodel, deffort, PROMPTS[run.get("kind", "plan")][0].format(goal=goal.strip() or "(a complete plan for the project)", run=str(rd).replace(chr(92), "/"), n=n, extra=extra), rd)
                if not _read(round_dir / "draft.md").strip():
                    run["draft"].update(state="failed", note=out[-300:]); run.update(state="failed", error="The drafting AI wrote no plan: " + out[-300:].strip()); return
                run["draft"].update(state="done")
            _step(run, "round %d: %s reading the plan" % (n, ", ".join(r[0] for r in reviewers)))
            rec = _one_round(run, n, drafter, reviewers)
            done = [r for r in rec["reviews"] if r["state"] == "done"]
            if run.get("stop"): break
            if not done:
                run.update(state="done", outcome="No reviewer answered, so this plan has had no second opinion.", final=str((round_dir / "draft.md").relative_to(rd).as_posix())); return
            if all(r["verdict"] == "APPROVE" for r in done):
                run.update(state="done", outcome="Every reviewer who answered (%d) approved the plan in round %d." % (len(done), n), final=(round_dir / "draft.md").relative_to(rd).as_posix()); return
            if n >= max_rounds:
                run.update(state="done", outcome="Round limit reached: %d reviewer(s) still wanted changes. The last plan and their points are below." % sum(1 for r in done if r["verdict"] != "APPROVE"),
                           final=(round_dir / "draft.md").relative_to(rd).as_posix()); return
            m = n + 1; (rd / "rounds" / str(m)).mkdir(parents=True, exist_ok=True)
            _step(run, "round %d: %s is revising the plan from the reviews" % (n, dprov))
            names = "\n".join("- review-%s.md (%s: %s)" % (r["provider"], r["provider"], r["verdict"]) for r in done)
            run_ai(dprov, dfound, dmodel, deffort, PROMPTS[run.get("kind", "plan")][2].format(run=str(rd).replace(chr(92), "/"), n=n, m=m, reviews=names), rd)
            if not _read(rd / "rounds" / str(m) / "draft.md").strip():
                run.update(state="done", outcome="The drafting AI did not write the revised plan; the last plan that exists is shown.", final=(round_dir / "draft.md").relative_to(rd).as_posix()); return
            rec["revise"] = "done"; n = m
        if run.get("stop"):
            last = max((int(p.parent.name) for p in (rd / "rounds").glob("*/draft.md")), default=0)
            run.update(state="stopped", outcome="Stopped.", final=("rounds/%d/draft.md" % last) if last else "")
    except Exception as ex:                                     # a bug must show as a failed run, not a silent thread death
        run.update(state="failed", error="%s: %s" % (type(ex).__name__, ex))
    finally:
        run["ended"] = time.time(); run["step"] = ""; _save(run)


def start(relay, goal, source, drafter_id, reviewer_ids, rounds, models=None, remote="", notes="", note_paths=(), settings=None, kind="plan", app_dir=None, snapshot=""):
    """Start a plan review. source: 'project' (this project's folder), 'remote' (a repository address) or 'notes' (only what the person gives)."""
    rounds = max(1, min(int(rounds or 2), MAX_ROUNDS))
    ais = dict(usable_ais(relay)); models = models or {}
    if drafter_id not in ais: return {"ok": False, "error": "%s is not installed or not signed in, so it cannot draft the plan." % drafter_id}
    rev_ids = [r for r in dict.fromkeys(reviewer_ids or []) if r != drafter_id and r in ais]
    if not rev_ids: return {"ok": False, "error": "Pick at least one other AI that is installed and signed in to review the plan."}
    rid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    rd = _runs_root(relay) / rid; rd.mkdir(parents=True, exist_ok=True)
    sources = []
    try:
        if kind == "ux":
            if not app_dir: return {"ok": False, "error": "The app's own files are not available."}
            summary = build_ui_pack(app_dir, rd, snapshot)
        elif source == "project":
            if not relay or not pathlib.Path(str(relay)).is_dir(): return {"ok": False, "error": "Choose a project first."}
            sources.append(("project", pathlib.Path(str(relay))))
        elif source == "remote":
            sources.append(("remote", clone_remote(safe_repo_url(remote), rd / "_clone")))
        elif source != "notes":
            return {"ok": False, "error": "Choose where the plan should come from."}
        if kind != "ux":
            files = [p for p in (note_paths or ()) if pathlib.Path(p).is_file()]
            if source == "notes" and not (notes.strip() or files): return {"ok": False, "error": "Paste your notes or name the files first."}
            summary = build_pack(sources, rd, notes, files)
    except PackError as ex:
        shutil.rmtree(rd, ignore_errors=True); return {"ok": False, "error": str(ex)}
    except (subprocess.TimeoutExpired, OSError) as ex:
        shutil.rmtree(rd, ignore_errors=True); return {"ok": False, "error": "Could not prepare the project files: %s" % ex}
    shutil.rmtree(rd / "_clone", ignore_errors=True)
    def conf(pid):
        m, e = pick_model(settings, pid); return (pid, ais[pid], models.get(pid) or m, e)
    drafter = conf(drafter_id); reviewers = [conf(r) for r in rev_ids]
    low, high = plan_calls(len(reviewers), rounds)
    run = {"id": rid, "dir": str(rd), "state": "running", "kind": kind, "source": "app" if kind == "ux" else source, "goal": goal, "drafter": drafter_id, "reviewers": rev_ids, "maxRounds": rounds,
           "rounds": [], "step": "starting", "started": time.time(), "pack": summary, "calls": [low, high], "final": "", "outcome": "", "error": ""}
    with LOCK: RUNS[rid] = run
    _save(run)
    threading.Thread(target=_drive, args=(run, relay, goal, drafter, reviewers, rounds), daemon=True).start()
    return {"ok": True, "run": rid, "pack": summary, "calls": [low, high]}


def extend(relay, rid, settings=None):
    """One more round on a finished run: when the last round's reviewers wanted changes, the lead revises first; then the reviewers read the latest plan again."""
    run = RUNS.get(rid) or load(relay, rid)
    if not run: return {"ok": False, "error": "unknown run"}
    if run["state"] == "running": return {"ok": False, "error": "This run is still going."}
    ais = dict(usable_ais(relay))
    if run["drafter"] not in ais: return {"ok": False, "error": "%s is not available." % run["drafter"]}
    rd = pathlib.Path(run["dir"])
    last = max((int(p.parent.name) for p in (rd / "rounds").glob("*/draft.md")), default=0)
    if not last: return {"ok": False, "error": "There is no plan to continue from."}
    if last >= MAX_ROUNDS + 3: return {"ok": False, "error": "That is enough rounds for one run. Start a new one."}

    def conf(pid):
        m, e = pick_model(settings, pid); return (pid, ais[pid], m, e)
    reviewers = [conf(r) for r in run["reviewers"] if r in ais]
    if not reviewers: return {"ok": False, "error": "None of the reviewing AIs is available now."}
    prev = run["rounds"][-1] if run["rounds"] else None
    needs_revise = bool(prev and prev["n"] == last and any(r["state"] == "done" and r["verdict"] != "APPROVE" for r in prev["reviews"])
                        and not (rd / "rounds" / str(last + 1) / "draft.md").exists())
    run.update(state="running", outcome="", error="", stop=False, ended=None)
    drafter = conf(run["drafter"])

    def go():
        n = last
        try:
            if needs_revise:
                m = n + 1; (rd / "rounds" / str(m)).mkdir(parents=True, exist_ok=True)
                _step(run, "%s is revising from the reviews" % drafter[0])
                names = chr(10).join("- review-%s.md (%s: %s)" % (r["provider"], r["provider"], r["verdict"]) for r in prev["reviews"] if r["state"] == "done")
                run_ai(*drafter, PROMPTS[run.get("kind", "plan")][2].format(run=str(rd).replace(chr(92), "/"), n=n, m=m, reviews=names), rd)
                if _read(rd / "rounds" / str(m) / "draft.md").strip(): n = m
            run["maxRounds"] = n
            _drive(run, relay, run["goal"], drafter, reviewers, n, start_round=n)
        except Exception as ex:
            run.update(state="failed", error="%s: %s" % (type(ex).__name__, ex)); _save(run)
    with LOCK: RUNS[rid] = run
    threading.Thread(target=go, daemon=True).start()
    return {"ok": True, "run": rid}


def stop(rid):
    run = RUNS.get(rid)
    if not run or run["state"] != "running": return {"ok": False, "error": "That run is not running."}
    run["stop"] = True; return {"ok": True}


def load(relay, rid):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{4}", rid or ""): return None
    f = _runs_root(relay) / rid / "run.json"
    try: run = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError): return None
    if run.get("state") == "running" and rid not in RUNS: run.update(state="stopped", outcome="The app was closed while this run was going.")
    return run


def public(run):
    """The run as the page sees it: no folder path, no internal flags, plus the list of documents."""
    rd = pathlib.Path(run["dir"])
    docs = sorted(p.relative_to(rd).as_posix() for p in (rd / "rounds").glob("*/*.md")) if (rd / "rounds").is_dir() else []
    keep = ("id", "state", "kind", "source", "goal", "drafter", "reviewers", "maxRounds", "rounds", "step", "started", "ended", "pack", "calls", "final", "outcome", "error", "draft")
    out = {k: run.get(k) for k in keep}; out["docs"] = docs; out["now"] = time.time(); out["ok"] = True
    return out


def status(relay, rid):
    run = RUNS.get(rid) or load(relay, rid)
    return public(run) if run else {"ok": False, "error": "unknown run"}


def list_runs(relay):
    root = _runs_root(relay); out = []
    if root.is_dir():
        for d in sorted(root.iterdir(), reverse=True)[:15]:
            run = RUNS.get(d.name) or load(relay, d.name)
            if run: out.append({"id": run["id"], "state": run["state"], "goal": (run.get("goal") or "")[:100], "started": run.get("started"), "outcome": run.get("outcome", ""), "drafter": run.get("drafter")})
    return {"ok": True, "runs": out}


def read_doc(relay, rid, name):
    """One document of a run, by a name from its own list (never an arbitrary path)."""
    run = RUNS.get(rid) or load(relay, rid)
    if not run: return {"ok": False, "error": "unknown run"}
    if not re.fullmatch(r"rounds/\d+/[A-Za-z0-9._-]+\.md", name or ""): return {"ok": False, "error": "unknown document"}
    p = pathlib.Path(run["dir"]) / name
    return {"ok": True, "name": name, "text": _read(p)} if p.is_file() else {"ok": False, "error": "that document was not written"}


def save_to_project(relay, rid):
    """Copy the final plan into the project as PLAN-REVIEW.md (only when the person asks). Returns the path; it is NOT committed."""
    run = RUNS.get(rid) or load(relay, rid)
    if not run or not run.get("final"): return {"ok": False, "error": "This run has no final plan yet."}
    text = _read(pathlib.Path(run["dir"]) / run["final"])
    if not text.strip(): return {"ok": False, "error": "The plan file is empty."}
    dest = pathlib.Path(str(relay)) / "PLAN-REVIEW.md"
    if dest.exists(): return {"ok": False, "error": "PLAN-REVIEW.md already exists in the project: move or rename it first."}
    dest.write_text(text, encoding="utf-8")
    return {"ok": True, "path": "PLAN-REVIEW.md"}
