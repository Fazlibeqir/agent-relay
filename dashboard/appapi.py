"""Actions behind the app's buttons: check this computer, install tools, create a project, put the relay into it, start and stop it.
Everything here is local and only reachable from this computer (server.py checks the caller). Standard library only; OS differences live in osops.py."""
import json, os, pathlib, re, shutil, subprocess, sys, time

import osops

HERE = pathlib.Path(__file__).parent
RES = pathlib.Path(getattr(sys, "_MEIPASS", HERE.parent))      # inside the packaged app the bundled files live here
TEMPLATE = RES / "template"
HOME = pathlib.Path.home()
IS_WIN = osops.IS_WIN
NO_WINDOW = osops.NO_WINDOW
APPDATA = (pathlib.Path(os.environ["APPDATA"]) if IS_WIN and os.environ.get("APPDATA") else pathlib.Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))) / "AgentRelay"
RELAY_FILES = ("relay-files.sh", "relay-lib.sh", "relay-pick.py", "relay-awake.ps1", "relay-idle.sh", "relay-digest.py", "relay-claude-result.py", "relay-settings.py")
GITIGNORE = ("relay.log", "relay-tests.log", "relay-live-*.log", "relay-live-*.start", "relay-console.log", "relay-console.err", ".relay.lock/",
             "relay.settings.json", "relay.providers.json", ".relay-settings-*.json", "relay-fallback-*.count", "relay-fallback-prompt.txt", "relay-idle-*.result",
             "relay-idle-*.stamp", "relay-idle-*.log", "relay-commit.txt", "CURRENT_TASK.md", "REVIEW.md", "OWNER-REVIEW-DIGEST.md")
KNOWN_PROVIDERS = ("claude", "codex", "copilot", "opencode")

INSTALL_STATE = {}
SIGNIN_CACHE = {}


# ---------------------------------------------------------------- config
def load_config():
    try: return json.loads((APPDATA / "app.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}


def save_config(cfg):
    APPDATA.mkdir(parents=True, exist_ok=True)
    (APPDATA / "app.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- the AI tools and the basics
def _copilot_found():
    """The Copilot CLI, not the VS Code stub that only says it is missing."""
    cands = [osops.which("copilot")] if osops.which("copilot") else []
    g = osops.glob_first([str(HOME / "AppData/Local/github-copilot-sdk/cli/*/copilot.exe")]) if IS_WIN else None
    if g: cands.insert(0, g)
    for c in cands:
        try:
            rc, out = osops.run(c, ["--version"], 15)
            if "copilot" in out.lower() and "not found" not in out.lower(): return c
        except Exception:
            continue
    return None


def tool_specs():
    claude_found = osops.which("claude") or (osops.glob_first([str(HOME / ".local/bin/claude.exe")]) if IS_WIN else None)
    codex_found = osops.which("codex") or (osops.glob_first([r"C:\Program Files\WindowsApps\OpenAI.Codex_*\app\resources\codex.exe"]) if IS_WIN else None)
    return [
        {"id": "claude", "name": "Claude Code", "role": "Reviews every task", "needed": True, "found": claude_found, "install": {"npm": "@anthropic-ai/claude-code"},
         "login": [], "loginNote": "A terminal opens: follow the sign-in steps (your browser opens). When it says you are in, type /exit."},
        {"id": "codex", "name": "Codex", "role": "Builds the tasks", "needed": False, "found": codex_found, "install": {"npm": "@openai/codex"},
         "login": ["login"], "loginNote": "A terminal opens: follow the sign-in steps in your browser."},
        {"id": "copilot", "name": "GitHub Copilot", "role": "Backup builder", "needed": False, "found": _copilot_found(),
         "install": {"winget": "GitHub.CopilotApp"} if IS_WIN else {"npm": "@github/copilot"},
         "login": ["login"], "loginNote": "A terminal opens: follow the sign-in steps in your browser (use the GitHub account that has Copilot; the student pack works)."},
        {"id": "opencode", "name": "OpenCode (free models)", "role": "Backup builder", "needed": False, "found": osops.which("opencode"), "install": {"npm": "opencode-ai"},
         "login": ["auth", "login"], "loginNote": "Free models need no sign-in."},
    ]


def basic_specs():
    py = shutil.which("python") or shutil.which("python3")
    out = [
        {"id": "git", "name": "Git", "found": osops.which("git"), "needed": True, "install": {"winget": "Git.Git", "apt": "git", "dnf": "git", "pacman": "git", "brew": "git"}},
        {"id": "node", "name": "Node.js (needed to install the AI tools)", "found": osops.which("node"), "needed": True,
         "install": {"winget": "OpenJS.NodeJS.LTS", "apt": "nodejs npm", "dnf": "nodejs npm", "pacman": "nodejs npm", "brew": "node"}},
        {"id": "python", "name": "Python 3 (the relay uses it)", "found": py, "needed": True,
         "install": {"winget": "Python.Python.3.12", "apt": "python3 python3-pip", "dnf": "python3 python3-pip", "pacman": "python python-pip", "brew": "python"}},
    ]
    if IS_WIN:
        out.insert(1, {"id": "bash", "name": "Git Bash (comes with Git)", "found": osops.glob_first([r"C:\Program Files\Git\usr\bin\bash.exe", r"C:\Program Files (x86)\Git\usr\bin\bash.exe"]),
                       "needed": True, "install": {"winget": "Git.Git"}})
    return out


# the tools a project may need, by the short names the planner uses in relay.project.json "requires"
TOOLCHAINS = {
    "node": {"name": "Node.js", "detect": ["node"], "install": {"winget": "OpenJS.NodeJS.LTS", "apt": "nodejs npm", "dnf": "nodejs npm", "pacman": "nodejs npm", "brew": "node"}},
    "python": {"name": "Python 3", "detect": ["python", "python3"], "install": {"winget": "Python.Python.3.12", "apt": "python3 python3-pip python3-venv", "dnf": "python3 python3-pip", "pacman": "python python-pip", "brew": "python"}},
    "java": {"name": "Java 21 (JDK)", "detect": ["java"], "install": {"winget": "EclipseAdoptium.Temurin.21.JDK", "apt": "openjdk-21-jdk", "dnf": "java-21-openjdk-devel", "pacman": "jdk21-openjdk", "brew": "openjdk@21"}},
    "maven": {"name": "Maven", "detect": ["mvn"], "install": {"winget": "Apache.Maven", "apt": "maven", "dnf": "maven", "pacman": "maven", "brew": "maven"}},
    "dotnet": {"name": ".NET SDK", "detect": ["dotnet"], "install": {"winget": "Microsoft.DotNet.SDK.8", "apt": "dotnet-sdk-8.0", "dnf": "dotnet-sdk-8.0", "brew": "dotnet-sdk"}},
    "go": {"name": "Go", "detect": ["go"], "install": {"winget": "GoLang.Go", "apt": "golang-go", "dnf": "golang", "pacman": "go", "brew": "go"}},
    "rust": {"name": "Rust", "detect": ["cargo"], "install": {"winget": "Rustlang.Rustup", "script": "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"}},
    "php": {"name": "PHP", "detect": ["php"], "install": {"winget": "PHP.PHP.8.3", "apt": "php-cli", "dnf": "php-cli", "pacman": "php", "brew": "php"}},
    "ruby": {"name": "Ruby", "detect": ["ruby"], "install": {"winget": "RubyInstallerTeam.Ruby.3.3", "apt": "ruby", "dnf": "ruby", "pacman": "ruby", "brew": "ruby"}},
    "docker": {"name": "Docker", "detect": ["docker"], "install": {"winget": "Docker.DockerDesktop", "apt": "docker.io", "dnf": "docker", "pacman": "docker", "brew": "docker"}},
    "godot": {"name": "Godot engine", "detect": ["godot", "godot4"], "install": {"winget": "GodotEngine.GodotEngine"}, "url": "https://godotengine.org/download"},
    "gh": {"name": "GitHub CLI", "detect": ["gh"], "install": {"winget": "GitHub.cli", "apt": "gh", "dnf": "gh", "pacman": "github-cli", "brew": "gh"}},
    "glab": {"name": "GitLab CLI", "detect": ["glab"], "install": {"winget": "GLab.GLab", "apt": "glab", "dnf": "glab", "pacman": "glab", "brew": "glab"}},
}


def toolchain_status(tid):
    t = TOOLCHAINS.get(tid)
    if not t: return None
    found = next((osops.which(n) for n in t["detect"] if osops.which(n)), None)
    line = osops.install_line(t["install"])
    return {"id": tid, "name": t["name"], "found": found, "canInstall": bool(line), "url": t.get("url", ""), "how": line or ("see " + t.get("url", "the tool's website"))}


def detect_setup():
    tools = tool_specs()
    for t in tools:
        t["how"] = osops.install_line(t["install"]) or "install it from its website"
        t["signedIn"] = signed_in(t["id"], t["found"])
        t.pop("install", None)
    basics = basic_specs()
    for b in basics:
        b["how"] = osops.install_line(b["install"]) or "install it from its website"; b.pop("install", None)
    return {"os": "windows" if IS_WIN else "linux", "basics": basics, "tools": tools, "installing": dict(INSTALL_STATE)}


def signed_in(tool_id, found):
    """True / False when the tool can say, None when it cannot. Cached for a minute. Only the yes/no is read: no account details are kept."""
    if not found: return None
    if load_config().get("manualSignedIn", {}).get(tool_id): return True
    c = SIGNIN_CACHE.get(tool_id)
    if c and time.time() - c[0] < 60: return c[1]
    res = None
    try:
        if tool_id == "claude":
            rc, out = osops.run(found, ["auth", "status"], 20)
            try: res = bool(json.loads(out[out.index("{"):out.rindex("}") + 1]).get("loggedIn"))
            except ValueError: res = None
        elif tool_id == "codex":
            rc, out = osops.run(found, ["login", "status"], 20); res = rc == 0 and "logged in" in out.lower()
        elif tool_id == "opencode":
            rc, out = osops.run(found, ["auth", "list"], 20); res = ("\u25cf" in out) or ("credentials" in out.lower() and "0 credentials" not in out.lower())
    except Exception:
        res = None
    SIGNIN_CACHE[tool_id] = (time.time(), res)
    return res


def mark_signed_in(tool_id, value=True):
    cfg = load_config(); m = cfg.setdefault("manualSignedIn", {}); m[tool_id] = bool(value); save_config(cfg)
    SIGNIN_CACHE.pop(tool_id, None)
    return {"ok": True}


def login_tool(tool_id):
    """Open a terminal window running the tool's own sign-in so the person can follow it. The page re-checks afterwards."""
    item = next((t for t in tool_specs() if t["id"] == tool_id), None)
    if not item or not item["found"]: return {"ok": False, "error": "That tool is not installed yet."}
    line = " ".join(osops.quote(a) for a in [item["found"]] + item["login"])
    SIGNIN_CACHE.pop(tool_id, None)
    ok, msg = osops.open_terminal_keep_shell("Sign in - " + item["name"], line)
    return {"ok": ok, "message": item["loginNote"] if ok else msg}


def install_tool(tool_id):
    """Run the install command in a terminal window (the page asks first). The page then waits until the tool is found."""
    s = next((x for x in tool_specs() + basic_specs() if x["id"] == tool_id), None)
    if not s: return {"ok": False, "error": "unknown tool"}
    if s["found"]: return {"ok": True, "message": "already installed"}
    line = osops.install_line(s["install"])
    if not line: return {"ok": False, "error": "This computer has no package manager the app knows. Install it from the tool's website."}
    if line.startswith("npm ") and not osops.which("npm"): return {"ok": False, "error": "Install Node.js first (it is in the list above), then try again."}
    INSTALL_STATE[tool_id] = {"state": "running", "cmd": line, "since": time.time()}
    ok, msg = osops.open_terminal("Install - " + s["name"], line)
    return {"ok": ok, "message": ("A terminal window opened and is installing " + s["name"] + ". This page updates when it is found.") if ok else msg}


def install_toolchain(tid):
    st = toolchain_status(tid)
    if not st: return {"ok": False, "error": "unknown tool"}
    if st["found"]: return {"ok": True, "message": "already installed"}
    if not st["canInstall"]:
        return {"ok": False, "error": "The app cannot install this one automatically. Download it from " + (st["url"] or "its website") + ".", "url": st["url"]}
    INSTALL_STATE["tc-" + tid] = {"state": "running", "cmd": st["how"], "since": time.time()}
    ok, msg = osops.open_terminal("Install - " + st["name"], st["how"])
    return {"ok": ok, "message": ("A terminal window opened and is installing " + st["name"] + ".") if ok else msg}


# ---------------------------------------------------------------- git hosts (GitHub, GitLab)
def githost_status():
    out = {}
    for hid, tid, auth in (("github", "gh", ["auth", "status"]), ("gitlab", "glab", ["auth", "status"])):
        tc = toolchain_status(tid)
        signed = None
        if tc["found"]:
            try: rc, txt = osops.run(tc["found"], auth, 20); signed = rc == 0
            except Exception: signed = None
        out[hid] = {"id": hid, "toolId": tid, "name": "GitHub" if hid == "github" else "GitLab", "found": tc["found"], "signedIn": signed, "how": tc["how"], "canInstall": tc["canInstall"]}
    return out


def githost_login(hid):
    tid = "gh" if hid == "github" else "glab"
    tc = toolchain_status(tid)
    if not tc["found"]: return {"ok": False, "error": "Install the " + tc["name"] + " first."}
    line = " ".join(osops.quote(a) for a in [tc["found"], "auth", "login"])
    ok, msg = osops.open_terminal_keep_shell("Sign in - " + tc["name"], line)
    return {"ok": ok, "message": "A terminal opens: choose your account and follow the steps in your browser." if ok else msg}


def githost_create_repo(hid, project, name):
    """Create a PRIVATE empty repository and add it as 'origin'. Nothing is pushed: the relay never pushes."""
    tid = "gh" if hid == "github" else "glab"
    tc = toolchain_status(tid)
    if not tc["found"]: return {"ok": False, "error": "The " + tc["name"] + " is not installed."}
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "project"
    args = ["repo", "create", slug, "--private", "--source", ".", "--remote", "origin"] if hid == "github" else ["repo", "create", slug, "--private"]
    try: rc, out = osops.run(tc["found"], args, 120, cwd=str(project))
    except Exception as ex: return {"ok": False, "error": str(ex)}
    return {"ok": rc == 0, "output": out[-400:], "error": "" if rc == 0 else out[-300:]}


# ---------------------------------------------------------------- places and projects
def environment():
    ds = osops.drives(); rec = osops.recommended_drive(ds)
    return {"os": "windows" if IS_WIN else "linux", "drives": ds, "recommended": rec["path"] if rec else "", "home": str(HOME),
            "projectsRoot": osops.projects_root(rec["path"]) if rec else str(HOME / "Projects")}


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)


def _commit(path, msg, files):
    _git(path, "add", "--", *files)
    ident = [] if _git(path, "config", "user.email").stdout.strip() else ["-c", "user.name=Agent Relay", "-c", "user.email=agent-relay@localhost"]
    return subprocess.run(["git", "-C", str(path), *ident, "commit", "-q", "-m", msg], capture_output=True, text=True, creationflags=NO_WINDOW)


def folder_name(name):
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", (name or "").strip())).strip(" .")[:60]


def create_project(name, root):
    """Make <root>/<name> a git repository on the working branch, with the relay in it. Checks the disk has room."""
    nm = folder_name(name)
    if not nm: return {"ok": False, "error": "Give the project a name."}
    base = pathlib.Path(root or "").expanduser()
    if not str(root or "").strip(): return {"ok": False, "error": "Choose where to put it."}
    try: base.mkdir(parents=True, exist_ok=True)
    except OSError as ex: return {"ok": False, "error": "Cannot create " + str(base) + ": " + str(ex)}
    try: free = shutil.disk_usage(base).free / 1024 ** 3
    except OSError: free = 99
    if free < 1: return {"ok": False, "error": "That disk has less than 1 GB free. Choose another place."}
    path = base / nm
    if path.exists() and any(path.iterdir()): return {"ok": False, "error": "A folder with that name already exists there and is not empty. Pick another name."}
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("# %s\n\nStarted with Agent Relay.\n" % nm, encoding="utf-8")
    r = _git(path, "init", "-q", "-b", "agent-run")
    if r.returncode != 0: return {"ok": False, "error": "git is not working: " + (r.stderr or r.stdout)[-200:]}
    c = _commit(path, "Start project", ["README.md"])
    if c.returncode != 0: return {"ok": False, "error": "first commit failed: " + (c.stderr or c.stdout)[-200:]}
    res = install_relay(str(path), commit=True)
    cfg = load_config(); cfg["project"] = str(path.resolve()); save_config(cfg)
    return {"ok": True, "path": str(path.resolve()), "freeGb": round(free), "relay": res}


def install_relay(target, commit=False):
    """Copy the relay into a git repo. Never overwrites an existing file."""
    t = pathlib.Path(target)
    if not (t / ".git").exists(): return {"ok": False, "error": "that folder is not a git repository (no .git)."}
    log, new = [], []
    for f in RELAY_FILES:
        dst = t / f
        if dst.exists(): log.append("kept " + f)
        elif (TEMPLATE / f).exists(): shutil.copyfile(TEMPLATE / f, dst); log.append("copied " + f); new.append(f)
    s = t / "relay.settings.json"
    ex = RES / "relay.settings.example.json"
    if not s.exists() and ex.exists(): shutil.copyfile(ex, s); log.append("created relay.settings.json")
    gi = t / ".gitignore"
    have = gi.read_text(encoding="utf-8", errors="replace").splitlines() if gi.exists() else []
    add = [x for x in GITIGNORE if x not in have]
    if add:
        with open(gi, "a", encoding="utf-8", newline="\n") as fh:
            if have and have[-1] != "": fh.write("\n")
            fh.write("\n".join(add) + "\n")
        log.append("added %d line(s) to .gitignore" % len(add))
    if commit and (new or add):
        _commit(t, "Add the Agent Relay scripts", new + [".gitignore"])
    return {"ok": True, "log": log}


# ---------------------------------------------------------------- starting and stopping
def relay_running(relay):
    return (pathlib.Path(relay) / ".relay.lock").exists()


def relay_script(r):
    r = pathlib.Path(r)
    if (r / "relay-files.sh").exists() and (r / "relay.project.json").exists() and (r / "tasks").is_dir(): return "relay-files.sh"
    return "relay.sh" if (r / "relay.sh").exists() else ""


def relay_tree(relay):
    """The launcher process of THIS project's relay and everything below it."""
    rows = osops.process_table(); me = os.getpid()
    roots = osops.relay_roots(rows, relay, me)
    tree, frontier = set(roots), list(roots)
    while frontier:
        cur = frontier.pop()
        for p in rows.values():
            if p["ppid"] == cur and p["pid"] not in tree and p["pid"] != me: tree.add(p["pid"]); frontier.append(p["pid"])
    return sorted(tree), roots


def start_relay(relay):
    r = pathlib.Path(relay)
    script = relay_script(r)
    if not script: return {"ok": False, "error": "This project has no plan yet (no tasks and relay.project.json). Finish the setup steps first."}
    if relay_running(r): return {"ok": False, "error": "A relay is already running here (the lock exists)."}
    osops.start_background(r, script)
    return {"ok": True, "message": "Starting. It runs the project's tests first, then works through the tasks by itself."}


def stop_relay(relay):
    """Stop THIS project's relay: its launcher and every process below it, by explicit process id. Removes the lock when nothing is left."""
    r = pathlib.Path(relay)
    tree, roots = relay_tree(r)
    if not roots:
        return {"ok": False, "killed": [], "left": [], "error": "Could not find this relay's launcher (it was probably started by hand in a terminal). Close that terminal window to stop it."}
    for pid in sorted(tree, reverse=True): osops.kill_pid(pid)
    time.sleep(2)
    left, _ = relay_tree(r)
    if not left and (r / ".relay.lock").exists():
        try: (r / ".relay.lock").rmdir()
        except OSError: pass
    return {"ok": not left, "killed": tree, "left": left}


def pick_folder(title="Choose a folder"):
    return osops.pick_folder(title)


# ---------------------------------------------------------------- task recommendations (which AI, which reasoning, per task)
REC_LINE = re.compile(r"^\s*recommended ai:\s*(.*)$", re.I)
EFFORT_WORD = re.compile(r"\b(none|minimal|low|medium|high|xhigh|max|ultra)\b\s+reasoning", re.I)


def parse_rec(raw):
    """'codex gpt-6.1-sol, high reasoning' -> provider, model, effort. Any part may be empty."""
    raw = (raw or "").strip()
    words = raw.replace(",", " ").split()
    prov = words[0].lower() if words and words[0].lower() in KNOWN_PROVIDERS else ""
    if not prov:
        m = re.search(r"\b(gpt-[0-9.]+-\w+)", raw, re.I)
        prov = "codex" if m else ""
    eff = EFFORT_WORD.search(raw)
    rest = [w for w in words[1:] if not re.fullmatch(r"(?i)reasoning|none|minimal|low|medium|high|xhigh|max|ultra|with|and|effort", w)] if prov and words and words[0].lower() == prov else \
           [w for w in words if not re.fullmatch(r"(?i)reasoning|none|minimal|low|medium|high|xhigh|max|ultra", w)]
    model = rest[0] if rest else ""
    return {"provider": prov, "model": model, "effort": (eff.group(1).lower() if eff else ""), "raw": raw}


def list_tasks(project):
    out = []
    d = pathlib.Path(project) / "tasks"
    if not d.is_dir(): return out
    def key(p):
        m = re.match(r"T(\d+)([a-z]?)", p.stem); return (int(m.group(1)), m.group(2)) if m else (10 ** 9, p.stem)
    for f in sorted(d.glob("T*.md"), key=key):
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        title = lines[0].lstrip("# ").strip() if lines else f.stem
        status = next((l for l in lines[:6] if l.startswith("Status:")), "")
        rec = next((parse_rec(m.group(1)) for l in lines[:14] for m in [REC_LINE.match(l)] if m), None)
        out.append({"id": f.stem, "title": title, "status": status, "rec": rec or {"provider": "", "model": "", "effort": "", "raw": ""}})
    return out


def set_task_rec(project, task_id, provider, model, effort):
    f = pathlib.Path(project) / "tasks" / (task_id + ".md")
    if not re.fullmatch(r"T\d+[a-z]?", task_id) or not f.exists(): return {"ok": False, "error": "unknown task"}
    for v in (provider, model, effort):
        if v and not re.fullmatch(r"[A-Za-z0-9._/:@+-]{1,80}", v): return {"ok": False, "error": "that value has characters that are not allowed"}
    line = "Recommended AI: " + " ".join(x for x in (provider, model) if x) + ((", %s reasoning" % effort) if effort and effort != "auto" else "")
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    idx = next((i for i, l in enumerate(lines[:14]) if REC_LINE.match(l)), None)
    if idx is not None: lines[idx] = line
    else:
        at = next((i for i, l in enumerate(lines[:8]) if l.startswith("Status:")), 0) + 1
        lines[at:at] = ["", line]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True}
