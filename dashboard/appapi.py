"""Actions behind the app's buttons: check this computer, install tools, create a project, put the relay into it, start and stop it.
Everything here is local and only reachable from this computer (server.py checks the caller). Standard library only; OS differences live in osops.py."""
import json, os, pathlib, re, secrets, shutil, subprocess, sys, tempfile, threading, time

import osops

HERE = pathlib.Path(__file__).parent
RES = pathlib.Path(getattr(sys, "_MEIPASS", HERE.parent))      # inside the packaged app the bundled files live here
TEMPLATE = RES / "template"
HOME = pathlib.Path.home()
IS_WIN = osops.IS_WIN
NO_WINDOW = osops.NO_WINDOW
APPDATA = (pathlib.Path(os.environ["APPDATA"]) if IS_WIN and os.environ.get("APPDATA") else pathlib.Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))) / "AgentRelay"
RELAY_FILES = ("relay-files.sh", "relay-lib.sh", "relay-limits.py", "relay-runtime.py", "relay-pick.py", "relay-parallel.py", "relay-parallel.sh", "relay-awake.ps1", "relay-idle.sh", "relay-digest.py", "relay-claude-result.py", "relay-settings.py")
GITIGNORE = ("relay.log", "relay-tests.log", "relay-live-*.log", "relay-live-*.start", "relay-console.log", "relay-console.err", ".relay.lock/",
             "relay.settings.json", "relay.providers.json", "relay-requests/", "relay-priority.txt", "OWNER-REQUESTS.md", "OWNER-REQUESTS.json", ".relay-settings-*.json", "relay-fallback-*.count", "relay-fallback-prompt.txt", "relay-idle-*.result",
             "relay-idle-*.stamp", "relay-idle-*.log", "relay-commit.txt", "relay-provider-*.limit", "relay-builder.identity", ".relay/", "CURRENT_TASK.md", "REVIEW.md", "OWNER-REVIEW-DIGEST.md")
KNOWN_PROVIDERS = ("claude", "codex", "copilot", "opencode")

INSTALL_STATE = {}
SIGNIN_CACHE = {}
TASK_ADD_LOCK = threading.Lock()
RUNNER_UPDATE_LOCK = threading.Lock()
RUNNER_UPDATE = ".relay/runner-update.json"
RUNNER_INSTALL_COMMIT = "Add the Agent Relay scripts"
RUNNER_UPDATE_COMMIT = "Update Agent Relay scripts"


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
            if rc == 0 and "copilot" in out.lower() and "not found" not in out.lower(): return c
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
        {"id": "copilot", "name": "GitHub Copilot", "role": "Planner, builder or reviewer", "needed": False, "found": _copilot_found(),
         "install": {"winget": "GitHub.CopilotApp"} if IS_WIN else {"npm": "@github/copilot"},
         "login": ["login"], "loginNote": "A terminal opens: follow the sign-in steps in your browser (use the GitHub account that has Copilot; the student pack works)."},
        {"id": "opencode", "name": "OpenCode (free models)", "role": "Backup AI", "needed": False, "found": osops.which("opencode"), "install": {"npm": "opencode-ai"},
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


def _commit(path, msg, files, *, only=False):
    staged = _git(path, "add", "--", *files)
    if staged.returncode:
        return staged
    ident = [] if _git(path, "config", "user.email").stdout.strip() else ["-c", "user.name=Agent Relay", "-c", "user.email=agent-relay@localhost"]
    scope = ["--only", "--", *files] if only else []
    return subprocess.run(["git", "-C", str(path), *ident, "commit", "-q", "-m", msg, *scope], capture_output=True, text=True, creationflags=NO_WINDOW)


def folder_name(name):
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", (name or "").strip())).strip(" .")[:60]


def create_project(name, root, *, workspace_idea=None):
    """Make <root>/<name> a git repository on the working branch, with the relay in it. Checks the disk has room."""
    nm = folder_name(name)
    if not nm: return {"ok": False, "error": "Give the project a name."}
    if workspace_idea is not None and len(workspace_idea) > 3000:
        return {"ok": False, "error": "Keep your project description to 3,000 characters."}
    if not shutil.which("git"):
        return {"ok": False, "error": "Install the project history tool from Tools, then try again."}
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
    first_files = ["README.md"]
    if workspace_idea is not None:
        config = {"name": nm, "idea": workspace_idea.strip(), "localWorkspace": True}
        (path / "relay.project.json").write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (path / "tasks").mkdir()
        first_files.append("relay.project.json")
    r = _git(path, "init", "-q", "-b", "agent-run")
    if r.returncode != 0: return {"ok": False, "error": "git is not working: " + (r.stderr or r.stdout)[-200:]}
    c = _commit(path, "Start project", first_files)
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
        elif (TEMPLATE / f).exists():
            data = (TEMPLATE / f).read_bytes()
            if f.endswith((".sh", ".py")): data = data.replace(b"\r\n", b"\n")      # scripts must keep Unix line endings or bash fails on Linux and Git Bash
            dst.write_bytes(data); log.append("copied " + f); new.append(f)
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
        _commit(t, RUNNER_INSTALL_COMMIT, new + ([".gitignore"] if add else []), only=True)
    return {"ok": True, "log": log}


def _local_runner(r):
    return relay_script(r) == "relay-files.sh" and not (r / "relay.sh").exists()


ACCESS_TOOLS = {"codex": "Codex", "claude": "Claude Code", "opencode": "OpenCode", "copilot": "GitHub Copilot"}


def supports_project_access(r):
    if not _local_runner(r) or (r / ".relay").resolve() != r / ".relay":
        return False
    try:
        return "def project_access(" in (r / "relay-runtime.py").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False


def access_launcher(provider):
    override = os.environ.get(provider.upper() + "_BIN")
    if override and pathlib.Path(override).is_file():
        return override
    if IS_WIN and provider == "codex":
        desktop = osops.glob_first([r"C:\Program Files\WindowsApps\OpenAI.Codex_*\app\resources\codex.exe"])
        if desktop:
            return desktop
    if provider == "copilot":
        return _copilot_found()
    return osops.which(provider)


def project_access(relay):
    r = pathlib.Path(relay).resolve()
    supported = supports_project_access(r)
    if not supported:
        return {"supported": False, "updateNeeded": _local_runner(r)}
    runtime = runtime_for(r)
    data = runtime.project_access()
    tools = []
    for provider, name in ACCESS_TOOLS.items():
        launcher = access_launcher(provider)
        status = runtime.access_status(provider, launcher, data) if launcher else "missing"
        tools.append({"id": provider, "name": name, "status": status})
    return {"supported": True, "checkedOnly": data["checkedOnly"], "tools": tools}


def _run_access_check(provider, launcher, folder, prompt, model=""):
    env = None
    if provider == "codex":
        args = ["exec", "--sandbox", "workspace-write", "--ephemeral", "-"]
    elif provider == "claude":
        (folder / "mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
        args = ["-p", "--tools", "Read,Write", "--permission-mode", "acceptEdits",
                "--strict-mcp-config", "--mcp-config", "mcp.json", "--no-session-persistence"]
    elif provider == "opencode":
        args = ["run", "--pure", "--model", model, prompt]
        env = {**os.environ, "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_PERMISSION": json.dumps({
            "*": "deny", "read": {"*input.txt": "allow"}, "edit": {"*result.txt": "allow"}
        })}
        prompt = ""
    else:
        args = ["--no-ask-user", "--no-auto-update", "--trust-folder", "--disable-builtin-mcps", "--available-tools=view,create,edit,apply_patch",
                "--allow-tool=read", "--allow-tool=write", "-p", prompt]
        env = {**os.environ, "COPILOT_ALLOW_ALL": "false", "COPILOT_ASSISTED_APPROVAL": "false"}
        if model and model != "auto":
            args.extend(["--model", model])
        prompt = ""
    command = [launcher, *args]
    if IS_WIN and launcher.lower().endswith((".cmd", ".bat")):
        command = ["cmd.exe", "/d", "/c", *command]
    with subprocess.Popen(command, cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, creationflags=NO_WINDOW) as child:
        try:
            child.communicate(prompt.encode("utf-8"), timeout=75)
        except subprocess.TimeoutExpired:
            if IS_WIN:
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=10)
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            return False
        return child.returncode == 0


def check_project_access(relay, provider):
    r = pathlib.Path(relay).resolve()
    if not supports_project_access(r) or provider not in ACCESS_TOOLS:
        return {"ok": False, "error": "An access check is unavailable for this project or AI tool."}
    launcher = access_launcher(provider)
    if not launcher:
        return {"ok": False, "error": "Install the AI tool first."}
    before = pathlib.Path(launcher).stat()
    runtime = runtime_for(r)
    model = ""
    if provider in ("copilot", "opencode"):
        defaults = json.loads((RES / "relay.settings.example.json").read_text(encoding="utf-8"))["models"]
        model = os.environ.get(provider.upper() + "_MODEL") or runtime.config().get("models", {}).get(provider) or defaults[provider]
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_./:-]+", model):
            return {"ok": False, "error": "Choose a valid backup model in AIs & settings before checking access."}
    if runtime.directory.resolve() != runtime.directory:
        return {"ok": False, "error": "This project's Relay files are linked to another folder."}
    if not runtime.release_stale_lock() or relay_tree(r)[1]:
        return {"ok": False, "error": "Stop Relay for this project before checking access."}
    runtime.claim(os.getpid())
    try:
        with tempfile.TemporaryDirectory(prefix="access-", dir=runtime.directory) as scratch:
            folder = pathlib.Path(scratch)
            if _git(folder, "init", "-q", "--template=").returncode:
                raise ValueError("Relay couldn't prepare a temporary project for the check.")
            challenge = secrets.token_hex(16)
            (folder / "input.txt").write_text(challenge, encoding="utf-8")
            prompt = (
                "This is a one-step project access check in a disposable folder. Read input.txt. "
                "Using your file tools, create result.txt containing exactly the text from input.txt. "
                "Do not open or change other files, use connectors, or commit anything. Then stop."
            )
            try:
                ran = _run_access_check(provider, launcher, folder, prompt, model)
            except (OSError, subprocess.TimeoutExpired):
                ran = False
            output = folder / "result.txt"
            passed = bool(ran and not output.is_symlink() and output.is_file()
                          and output.stat().st_size <= 64 and output.read_bytes().strip() == challenge.encode())
        after = pathlib.Path(launcher).stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return {"ok": False, "error": "The AI tool was updated during the check. Try again."}
        runtime.record_access(provider, launcher, passed)
        return {"ok": True, "passed": passed, "access": project_access(r)}
    finally:
        (r / ".relay.lock/owner.json").unlink()
        (r / ".relay.lock").rmdir()


def _file_bytes(r, name):
    path = r / name
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Relay found a linked or special file at {name}.")
    return path.read_bytes() if path.exists() else None


def _runner_changes(r):
    changes = {}
    for name in RELAY_FILES:
        current = _file_bytes(r, name)
        wanted = (TEMPLATE / name).read_bytes().replace(b"\r\n", b"\n")
        if not _same_runner_bytes(current, wanted):
            changes[name] = (current, wanted)
    if changes:
        current = _file_bytes(r, ".gitignore")
        present = (current or b"").splitlines()
        missing = [rule.encode() for rule in GITIGNORE if rule.encode() not in present]
        if missing:
            newline = b"\r\n" if b"\r\n" in (current or b"") else b"\n"
            prefix = current or b""
            if prefix and not prefix.endswith(b"\n"):
                prefix += newline
            changes[".gitignore"] = (current, prefix + newline.join(missing) + newline)
    return changes


def runner_update_available(r):
    if not _local_runner(r):
        return False
    try:
        return (r / RUNNER_UPDATE).is_file() or bool(_runner_changes(r))
    except (ValueError, OSError):
        return True


def _replace_bytes(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = pathlib.Path(stream.name)
        try:
            stream.write(data)
            stream.close()
            if path.exists():
                shutil.copymode(path, temp)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def _git_blob(r, key):
    result = subprocess.run(["git", "-C", str(r), "show", key], capture_output=True, creationflags=NO_WINDOW)
    return result.stdout if result.returncode == 0 else None


def _same_runner_bytes(left, right):
    if left is None or right is None:
        return left == right
    return left.replace(b"\r\n", b"\n") == right.replace(b"\r\n", b"\n")


def _runner_update_record(r, record):
    """Close a committed update or restore only the files recorded before an interrupted update."""
    journal = r / RUNNER_UPDATE
    if record["branch"] != _git(r, "branch", "--show-current").stdout.strip():
        raise ValueError("Return to the branch where the Relay update started.")
    changes = {}
    for name, pair in record["files"].items():
        if name not in (*RELAY_FILES, ".gitignore"):
            raise ValueError("Unexpected file in the saved Relay update.")
        changes[name] = tuple(bytes.fromhex(value) if value is not None else None for value in pair)
    head = _git(r, "rev-parse", "HEAD").stdout.strip()
    if head != record["head"]:
        committed = _git(r, "log", "-1", "--format=%P%n%s").stdout.splitlines()
        if committed != [record["head"], RUNNER_UPDATE_COMMIT] or any(
            not _same_runner_bytes(_git_blob(r, f"HEAD:{name}"), wanted)
            for name, (_, wanted) in changes.items()
        ):
            raise ValueError("Project history changed during the update. The saved Relay update is still available.")
        journal.unlink()
        return True
    staged = []
    for name, (before, wanted) in changes.items():
        if _file_bytes(r, name) not in (before, wanted):
            raise ValueError(f"{name} changed after the update started. The saved Relay update is still available.")
        index = _git_blob(r, f":{name}")
        if index != _git_blob(r, f"HEAD:{name}"):
            if not _same_runner_bytes(index, wanted):
                raise ValueError(f"{name} was staged separately. The saved Relay update is still available.")
            staged.append(name)
    if staged and _git(r, "restore", "--staged", "--", *staged).returncode:
        raise ValueError("Could not restore Relay's files in the Git index.")
    for name, (before, _) in changes.items():
        if _file_bytes(r, name) == before:
            continue
        if before is None:
            (r / name).unlink(missing_ok=True)
        else:
            _replace_bytes(r / name, before)
    journal.unlink()
    return False


def update_runner(relay):
    r = pathlib.Path(relay).resolve()
    journal = r / RUNNER_UPDATE
    with RUNNER_UPDATE_LOCK:
        try:
            if not _local_runner(r):
                raise ValueError("This project uses a custom Relay setup. Update it through that project's maintainers.")
            if journal.parent.resolve() != journal.parent or journal.is_symlink():
                raise ValueError("This project's Relay data is linked to another location.")
            runtime = runtime_for(r)
            if not runtime.release_stale_lock() or relay_tree(r)[1]:
                raise ValueError("Stop Relay for this project before updating.")
            runtime.claim(os.getpid())
            try:
                if journal.exists():
                    if _runner_update_record(r, json.loads(journal.read_bytes())):
                        return {"ok": True, "message": "Relay is up to date."}
                if runtime.state().get("phase") != "idle":
                    raise ValueError("Finish or resolve the saved Relay task before updating.")
                changes = _runner_changes(r)
                if not changes:
                    return {"ok": True, "message": "Relay is up to date."}
                branch = _git(r, "branch", "--show-current").stdout.strip()
                if branch in ("", "main", "master"):
                    raise ValueError("Open the project's Relay work branch before updating.")
                status = _git(r, "status", "--porcelain", "--untracked-files=all", "--ignored=matching", "--", *changes)
                if status.returncode:
                    raise ValueError("Relay couldn't check the project's saved files.")
                if status.stdout.strip():
                    raise ValueError("Some Relay files have unsaved changes. Save those changes and check again.")
                for name, (before, _) in changes.items():
                    if name == ".gitignore" or before is None:
                        continue
                    installed = _git(r, "log", "-1", "--format=%H", f"--grep=^{RUNNER_INSTALL_COMMIT}$",
                                     f"--grep=^{RUNNER_UPDATE_COMMIT}$", "--", name).stdout.strip()
                    if not installed or _git_blob(r, f"{installed}:{name}") != _git_blob(r, f"HEAD:{name}"):
                        raise ValueError(f"{name} has custom changes. Relay left it in place.")
                record = {
                    "head": _git(r, "rev-parse", "HEAD").stdout.strip(),
                    "branch": branch,
                    "files": {name: [before.hex() if before is not None else None, wanted.hex()]
                              for name, (before, wanted) in changes.items()},
                }
                _replace_bytes(journal, json.dumps(record).encode("utf-8"))
                try:
                    for name, (_, wanted) in changes.items():
                        _replace_bytes(r / name, wanted)
                    commit = _commit(r, RUNNER_UPDATE_COMMIT, list(changes), only=True)
                    if commit.returncode:
                        raise ValueError("Git couldn't save the Relay update.")
                except (OSError, ValueError):
                    if not _runner_update_record(r, record):
                        raise
                else:
                    _runner_update_record(r, record)
                return {"ok": True, "message": "Relay updated for this project.", "files": list(changes)}
            finally:
                (r / ".relay.lock/owner.json").unlink()
                (r / ".relay.lock").rmdir()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------- starting and stopping
def relay_running(relay):
    return (pathlib.Path(relay) / ".relay.lock").exists()


def relay_script(r):
    r = pathlib.Path(r)
    if (r / "relay.sh").is_file():
        return "relay.sh"
    if (r / "relay-files.sh").is_file() and (r / "relay.project.json").is_file() and (r / "tasks").is_dir():
        return "relay-files.sh"
    return ""


def project_metadata(r):
    try:
        info = json.loads((r / "relay.project.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def relay_setup_error(r, script, project, task_count=None):
    """The same setup check serves Start and the dashboard."""
    if script == "relay.sh":
        return ""
    if task_count is None:
        task_count = sum(1 for _ in (r / "tasks").glob("T*.md"))
    if not script:
        if task_count:
            can_configure = (r / "AGENTS.md").is_file() and formatted_tasks(r)[0]
            if not can_configure:
                return "These tasks use a project-specific format. Restore this project's custom relay.sh to run them."
            if (r / "relay.project.json").exists():
                return "The runner configuration is saved. Restore relay-files.sh to start."
            return "Set the test command to configure the local runner for these tasks."
        return "Finish setting up a task list and its Relay runner before starting."
    command = project.get("testCommand")
    if not task_count or not (r / "AGENTS.md").is_file() or not isinstance(command, str) or not command.strip():
        if project.get("localWorkspace"):
            return "Use Plan with AI and approve the plan before starting work."
        return "Finish runner setup with task files, AGENTS.md and a test command in relay.project.json."
    return ""


def relay_tree(relay):
    """The launcher process of THIS project's relay and everything below it."""
    rows = osops.process_table(); me = os.getpid()
    roots = osops.relay_roots(rows, relay, me)
    owner_file = pathlib.Path(relay) / ".relay.lock/owner.json"
    try:
        owner = json.loads(owner_file.read_text(encoding="utf-8"))
        pid = owner.get("pid")
        if (owner.get("root") == str(pathlib.Path(relay).resolve()) and pid in rows and pid != me
                and rows[pid]["name"].lower() in ("bash", "sh", "bash.exe", "sh.exe")
                and owner.get("identity") and runtime_for(relay).process_identity(pid) == owner["identity"]):
            roots = list(set(roots + [pid]))
    except (OSError, ValueError, TypeError):
        pass
    tree, frontier = set(roots), list(roots)
    while frontier:
        cur = frontier.pop()
        for p in rows.values():
            if p["ppid"] == cur and p["pid"] not in tree and p["pid"] != me: tree.add(p["pid"]); frontier.append(p["pid"])
    return sorted(tree), roots


# Files the relay itself and common tools create; they never count as unfinished work.
NOT_WORK = re.compile(r"(^|/)(relay[^/]*|CURRENT_TASK\.md|REVIEW\.md|PLAYTEST_QUEUE[^/]*|OWNER-REQUESTS\.[a-z]+|graphify-out|\.agent-relay[^/]*|\.relay[^/]*)(/|$)")


def relay_ignore_additions(project):
    result = _git(project, "diff", "--no-ext-diff", "--no-textconv", "--unified=0", "--", ".gitignore")
    if result.returncode:
        return False
    lines = result.stdout.splitlines()
    first_hunk = next((index for index, line in enumerate(lines) if line.startswith("@@ ")), len(lines))
    changes = [line for line in lines[first_hunk:] if line.startswith(("+", "-"))]
    return bool(changes) and all(line.startswith("+") and (not line[1:] or line[1:] in GITIGNORE) for line in changes)


def unfinished_work(r):
    """Uncommitted changes in the project (what a stopped run left half-done). The relay refuses to start on top of them. -> [(status, path)]"""
    if not (pathlib.Path(r) / ".git").exists(): return []
    out = _git(r, "status", "--porcelain", "-uall")
    rows = []
    for line in out.stdout.splitlines():
        if len(line) < 4: continue
        st, path = line[:2], line[3:].strip().strip('"')
        if NOT_WORK.search(path): continue
        if st == " M" and path == ".gitignore" and relay_ignore_additions(r): continue
        if st == "??" and path == ".gitignore":
            rules = (pathlib.Path(r) / path).read_text(encoding="utf-8", errors="replace").splitlines()
            if rules and all(not rule or rule in GITIGNORE for rule in rules):
                continue
        rows.append((st, path))
    return rows


def save_work_aside(r, rows):
    """Keep the unfinished changes in a folder next to the project (a patch for edited files, copies of new files), then restore the project to its last commit. -> (folder, error)"""
    r = pathlib.Path(r)
    if any(" -> " in p for _, p in rows): return None, "A file was renamed in the unfinished work; save or commit it by hand first."
    dest = r.parent / "relay-saved-work" / ("%s-%s" % (r.name, time.strftime("%Y%m%d-%H%M%S")))
    try: dest.mkdir(parents=True, exist_ok=True)
    except OSError as e: return None, "Could not create %s (%s)." % (dest, e)
    new = [p for st, p in rows if st == "??" or st[0] == "A"]
    tracked = [p for st, p in rows if p not in new]
    if tracked:
        d = _git(r, "diff", "HEAD", "--binary", "--", *tracked)
        if d.returncode != 0: return None, "git diff failed: " + (d.stderr or "").strip()[:200]
        (dest / "unfinished-changes.patch").write_bytes(d.stdout.encode("utf-8"))
    for p in new:
        src = r / p
        if src.is_file():
            tgt = dest / "new-files" / p; tgt.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, tgt)
    (dest / "README.txt").write_text("Saved by Agent Relay before starting a run.\nunfinished-changes.patch: apply with  git apply <this file>  (from the project folder).\nnew-files/: files that were not committed yet; copy them back to the same place.\n", encoding="utf-8")
    if tracked and not (dest / "unfinished-changes.patch").exists(): return None, "Could not write the saved copy; nothing was changed."
    for p in new:
        if (_git(r, "ls-files", "--error-unmatch", p).returncode == 0): _git(r, "rm", "-f", "-q", "--", p)
        else:
            try: (r / p).unlink()
            except OSError: pass
    if tracked:
        c = _git(r, "checkout", "HEAD", "--", *tracked)
        if c.returncode != 0: return str(dest), "Saved a copy, but restoring failed: " + (c.stderr or "").strip()[:200]
    return str(dest), ""


def start_relay(relay, save_aside=False):
    r = pathlib.Path(relay)
    script = relay_script(r)
    if (r / RUNNER_UPDATE).exists():
        return {"ok": False, "error": "Finish Update Relay before starting this project."}
    project = project_metadata(r)
    error = relay_setup_error(r, script, project)
    if error:
        return {"ok": False, "error": error}
    if (r / "relay-runtime.py").exists(): runtime_for(r).release_stale_lock()
    if relay_running(r): return {"ok": False, "error": "A relay is already running here (the lock exists)."}
    rows = unfinished_work(r)
    saved = ""
    approved_retry = False
    resumed_build = False
    if (r / "relay-runtime.py").exists():
        runtime = runtime_for(r)
        state = runtime.state()
        if _local_runner(r) and state.get("phase") == "building":
            if relay_tree(r)[1]:
                return {"ok": False, "error": "The previous Relay launcher is still running. Wait for it to stop before resuming."}
            cmd = os.environ.get("TEST_CMD") or project.get("testCommand") or ""
            if runtime.build_resume_phase(state, cmd) == "blocked":
                return {"ok": False, "error": "The saved task or project files changed. Review the files, then use Approve task retry under Technical details."}
            resumed_build = True
        current = r / "CURRENT_TASK.md"
        import hashlib
        approved_retry = bool(state.get("retryApproved") and current.is_file() and state.get("head") == _git(r, "rev-parse", "HEAD").stdout.strip()
                              and hashlib.sha256(current.read_bytes()).hexdigest() == state.get("taskHash"))
    if rows and not approved_retry and not resumed_build:
        if not save_aside:
            return {"ok": False, "needsDecision": "unfinished", "files": [p for _, p in rows][:40], "count": len(rows),
                    "error": "A previous run left %d unfinished file change(s) in the project, and a new run cannot start on top of them." % len(rows)}
        saved, err = save_work_aside(r, rows)
        if err: return {"ok": False, "error": err, "saved": saved}
    osops.start_background(r, script)
    msg = "Continuing the saved task." if resumed_build else "Starting. It runs the project's tests first, then works through the tasks by itself."
    if saved: msg = "Your unfinished changes are saved in " + saved + ". " + msg
    return {"ok": True, "message": msg, "saved": saved}


def stop_relay(relay):
    """Stop THIS project's relay: its launcher and every process below it, by explicit process id. Removes the lock when nothing is left."""
    r = pathlib.Path(relay)
    tree, roots = relay_tree(r)
    if not roots:
        return {"ok": False, "killed": [], "left": [], "error": "Could not find this relay's launcher (it was probably started by hand in a terminal). Close that terminal window to stop it."}
    for pid in sorted(tree, reverse=True): osops.kill_pid(pid)
    time.sleep(2)
    left, _ = relay_tree(r)
    if not left and (r / ".relay.lock/owner.json").exists():
        if not runtime_for(r).release_stale_lock():
            return {"ok": False, "killed": tree, "left": [], "error": "The worker lock is still active or unverified; it was preserved for inspection."}
    if not left and (r / ".relay.lock").exists():
        try: (r / ".relay.lock").rmdir()
        except OSError: pass
    return {"ok": not left, "killed": tree, "left": left}


def runtime_for(relay):
    import importlib.util
    spec = importlib.util.spec_from_file_location("relay_runtime", TEMPLATE / "relay-runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Runtime(relay)


def runtime_state(relay):
    if not relay: return {"phase": "idle", "calls": [], "events": []}
    try:
        state = runtime_for(relay).summary()
        directory = pathlib.Path(relay)
        script = relay_script(directory)
        state["supported"] = (directory / "relay-runtime.py").exists() and (
            script == "relay-files.sh" or (script == "relay.sh" and "relay_runtime board-resume" in (directory / script).read_text(encoding="utf-8")))
        return state
    except (OSError, ValueError, KeyError) as exc:
        return {"error": str(exc), "calls": [], "events": []}


def create_worktree(relay):
    """Create a new local branch/worktree; never move, clean or copy unsaved source edits."""
    import uuid
    source = pathlib.Path(relay).resolve()
    branch = "relay/" + time.strftime("%Y%m%d-") + uuid.uuid4().hex[:8]
    destination = source.parent / ".agent-relay-worktrees" / (source.name + "-" + branch.split("/")[-1])
    destination.parent.mkdir(exist_ok=True)
    result = _git(source, "worktree", "add", "-b", branch, str(destination), "HEAD")
    if result.returncode:
        return {"ok": False, "error": (result.stderr or result.stdout).strip()}
    # Copy only Relay configuration, not credentials, source edits, caches, or provider sessions.
    for name in ("relay.settings.json", "relay.project.json"):
        if (source / name).is_file(): shutil.copyfile(source / name, destination / name)
    if relay_script(source) != "relay.sh":
        installed = install_relay(destination)
        if not installed["ok"]: return {"ok": False, "path": str(destination), "error": installed["error"]}
        for name in RELAY_FILES:
            (destination / name).write_bytes((TEMPLATE / name).read_bytes().replace(b"\r\n", b"\n"))
    return {"ok": True, "path": str(destination), "branch": branch,
            "message": "Created from the last commit. Unsaved edits remain in the original checkout. No worker was started."}


def artifact_file(relay, task, path):
    runtime = runtime_for(relay)
    item = next((x for x in runtime.state().get("artifacts", {}).get(task, []) if x["path"] == path), None)
    if not item: raise ValueError("Artifact is not registered for this task")
    file = runtime.safe_path(path)
    if not file.is_file(): raise ValueError("Artifact is missing")
    import hashlib
    with file.open("rb") as stream:
        checksum = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""): checksum.update(chunk)
        actual = checksum.hexdigest()
    if actual != item["sha256"]: raise ValueError("Artifact changed after it was attached; attach the new version")
    return file


def pick_folder(title="Choose a folder"):
    return osops.pick_folder(title)


# ---------------------------------------------------------------- task recommendations (which AI, which reasoning, per task)
GATE_LINE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.*)$")
HUMAN_GATE = re.compile(r"^\**\s*(human|owner)\b", re.I)
REC_LINE = re.compile(r"^\s*recommended ai:\s*(.*)$", re.I)
EFFORT_WORD = re.compile(r"\b(none|minimal|low|medium|high|xhigh|max|ultra)\b\s+reasoning", re.I)


def formatted_tasks(project):
    """Recognize explicit runner gates in a saved task list."""
    files = sorted((pathlib.Path(project) / "tasks").glob("T*.md"))
    for task in files:
        gates = [GATE_LINE.match(line) for line in task.read_text(encoding="utf-8", errors="replace").splitlines()]
        labels = [re.match(r"(AUTO|HUMAN):\s*\S", gate.group(2).strip(), re.I) for gate in gates if gate]
        if any(label is None for label in labels) or {label.group(1).upper() for label in labels} != {"AUTO", "HUMAN"}:
            return False, len(files)
    return bool(files), len(files)


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
        gates = [GATE_LINE.match(l) for l in lines]; gates = [g for g in gates if g]
        human = [g for g in gates if HUMAN_GATE.match(g.group(2).strip())]; auto = [g for g in gates if g not in human]
        done_n = sum(1 for g in gates if g.group(1) != " ")
        open_auto = sum(1 for g in auto if g.group(1) == " "); open_human = sum(1 for g in human if g.group(1) == " ")
        state = "done" if (gates and not open_auto and not open_human) or re.match(r"(?i)^status:\s*done\b", status) else ("owner" if gates and not open_auto else "todo")
        fix_open = any(g.group(1) == " " and "Fix what the owner asked" in g.group(2) for g in gates)
        out.append({"id": f.stem, "title": title, "status": status, "fixRequested": fix_open, "rec": rec or {"provider": "", "model": "", "effort": "", "raw": ""},
                    "state": state, "gates": len(gates), "gatesDone": done_n, "completion": round(100 * done_n / len(gates)) if gates else 0})
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


# ---------------------------------------------------------------- permission requests (the AI asks, the person decides)
def list_requests(project):
    """Requests in <project>/relay-requests/*.json, newest last. Written by the relay (sensitive files) or by an AI that needs something outside its rules."""
    d = pathlib.Path(project) / "relay-requests"
    out = []
    if not d.is_dir(): return out
    for f in sorted(d.glob("*.json")):
        try: r = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError): continue
        if not isinstance(r, dict): continue
        r["id"] = f.stem; r.setdefault("type", "other"); r.setdefault("status", "pending"); r.setdefault("files", []); r.setdefault("executed", False)
        for k in ("action", "why", "details", "task", "note", "result", "decidedBy", "decidedAt"): r[k] = str(r.get(k, ""))[:500]
        if not isinstance(r["files"], list): r["files"] = []
        r["files"] = [str(x)[:200] for x in r["files"]][:30]
        out.append(r)
    return out


def decide_request(project, rid, decision, note=""):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", rid or "") or decision not in ("approve", "deny"): return {"ok": False, "error": "bad request"}
    f = pathlib.Path(project) / "relay-requests" / (rid + ".json")
    if not f.exists(): return {"ok": False, "error": "that request no longer exists"}
    try: r = json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError): return {"ok": False, "error": "that request could not be read"}
    if r.get("status", "pending") != "pending": return {"ok": False, "error": "that request was already decided"}
    r["status"] = "approved" if decision == "approve" else "denied"
    r["decidedAt"] = time.strftime("%Y-%m-%d %H:%M:%S"); r["note"] = (note or "")[:300]; r["executed"] = False; r["decidedBy"] = "you"
    with open(f, "w", encoding="utf-8", newline="\n") as fh: json.dump(r, fh, indent=1)
    return {"ok": True, "status": r["status"]}


def next_tasks(project, n=5):
    """The first n tasks still to do, in task order, with the one being built right now first. Tasks parked for the person (PLAYTEST_QUEUE.md) are marked, not hidden."""
    p = pathlib.Path(project)
    tasks = list_tasks(p)
    current = ""
    try:
        cur = (p / "CURRENT_TASK.md").read_text(encoding="utf-8", errors="replace").splitlines()
        m = re.match(r"(T\d+[a-z]?)", cur[1].strip()) if len(cur) > 1 else None
        current = m.group(1) if m else ""
    except OSError:
        pass
    parked = set()
    q = p / "PLAYTEST_QUEUE.md"
    if q.exists():
        for line in q.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^(T\d+[a-z]?)\b", line)
            if m: parked.add(m.group(1))
    todo = [t for t in tasks if t["state"] == "todo" and t["id"] not in parked]
    todo.sort(key=lambda t: (t["id"] != current,))           # stable: the current task first, the rest keep task order
    out = []
    for t in todo[:n]:
        out.append({"id": t["id"], "title": re.sub(r"^T\d+[a-z]?\s*[-\u2014]\s*", "", t["title"]), "completion": t["completion"], "rec": t["rec"], "current": t["id"] == current})
    return {"next": out, "current": current, "todoCount": len(todo), "total": len(tasks), "doneCount": sum(1 for t in tasks if t["state"] == "done"),
            "waitingCount": sum(1 for t in tasks if t["state"] == "owner" or t["id"] in parked)}


# ---------------------------------------------------------------- the person's actions on a task (task-file projects only)
def add_task(project, title, description, review=""):
    root = pathlib.Path(project).resolve()
    if relay_script(root) != "relay-files.sh" or (root / "relay.sh").exists():
        return {"ok": False, "error": "Create tasks in this project's own task board."}
    if not all(isinstance(value, str) for value in (title, description, review)):
        return {"ok": False, "error": "Enter a title and describe the work."}
    title, description, review = " ".join(title.split()), description.strip(), " ".join(review.split())
    if not title or not description:
        return {"ok": False, "error": "Enter a title and describe the work."}
    if len(title) > 100 or len(description) > 3000 or len(review) > 300:
        return {"ok": False, "error": "Use up to 100 characters for the title, 3,000 for the work and 300 for the review note."}
    tasks = root / "tasks"
    if tasks.resolve() != tasks:
        return {"ok": False, "error": "The task folder must be inside this project."}
    with TASK_ADD_LOCK:
        running = relay_running(root)
        if not running and (root / ".relay/state.json").is_file() and runtime_for(root).state().get("phase") != "idle":
            return {"ok": False, "error": "Resume the saved work before adding another task. This keeps its test results available."}
        numbers = (int(match[1]) for path in tasks.iterdir() if (match := re.fullmatch(r"T(\d+)[a-z]?\.md", path.name)))
        task = f"T{max(numbers, default=0) + 1:02d}"
        relative = f"tasks/{task}.md"
        text = (
            f"# {task} - {title}\n\nStatus: Not started | Completion: 0% (0/2 gates)\n\n"
            "- [ ] AUTO: Add or update a check for the requested behavior and pass the project's test command.\n"
            f"- [ ] HUMAN: {review or 'Try the requested change and confirm it works as described.'}\n\n"
            "## What to do\n" + "\n".join("> " + line for line in description.splitlines()) + "\n"
        )
        with tempfile.NamedTemporaryFile("w", dir=tasks, encoding="utf-8", newline="\n", delete=False, suffix=".tmp") as file:
            temporary = pathlib.Path(file.name)
            file.write(text)
        try:
            temporary.replace(root / relative)
        finally:
            temporary.unlink(missing_ok=True)
        if not running:
            result = _commit(root, f"{task}: add task", [relative], only=True)
            if result.returncode:
                return {"ok": True, "task": task, "warning": "Task saved. Relay could not save it to project history. The task is still in your list."}
        return {"ok": True, "task": task}


def _task_file(project, task_id):
    if not re.fullmatch(r"T\d+[a-z]?", task_id or ""): return None
    f = pathlib.Path(project) / "tasks" / (task_id + ".md")
    return f if f.exists() else None


def _status_line(gates_done, gates_all, open_auto, open_human, words=None):
    pct = round(100 * gates_done / gates_all) if gates_all else 0
    if words: label = words
    elif gates_all and not open_auto and not open_human: label = "Done"
    elif not open_auto: label = "Waiting for you"
    else: label = "In progress"
    return "Status: %s | Completion: %d%% (%d/%d gates)" % (label, pct, gates_done, gates_all)


def _rewrite_status(lines):
    gates = [GATE_LINE.match(l) for l in lines]; gates = [g for g in gates if g]
    human = [g for g in gates if HUMAN_GATE.match(g.group(2).strip())]
    open_auto = sum(1 for g in gates if g not in human and g.group(1) == " "); open_human = sum(1 for g in human if g.group(1) == " ")
    done_n = sum(1 for g in gates if g.group(1) != " ")
    new = _status_line(done_n, len(gates), open_auto, open_human)
    for i, l in enumerate(lines[:8]):
        if l.startswith("Status:"): lines[i] = new; return
    lines.insert(1, new)


def _after_edit(project, task_id, message):
    """The relay commits the task files itself between rounds; when it is not running, commit here so the project stays clean."""
    if not relay_running(project):
        _commit(pathlib.Path(project), message, ["tasks/%s.md" % task_id], only=True)


def queue_next(project, task_id):
    """Put a task at the top of the person's 'do this next' list (relay-priority.txt)."""
    if not _task_file(project, task_id): return {"ok": False, "error": "unknown task"}
    pf = pathlib.Path(project) / "relay-priority.txt"
    ids = [l.strip() for l in pf.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()] if pf.exists() else []
    ids = [task_id] + [i for i in ids if i != task_id]
    with open(pf, "w", encoding="utf-8", newline="\n") as fh: fh.write("\n".join(ids) + "\n")
    return {"ok": True, "queue": ids}


def decide_review(project, task_id, revision, action, note=""):
    runtime = runtime_for(project)
    with runtime.lock():
        state = runtime.state()
        if state.get("phase") == "reviewing" and state.get("task") == task_id:
            return {"ok": False, "error": "Wait for the AI review to finish saving, then reopen this review."}
        try:
            review = runtime.require_review(state, task_id, revision)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        label = f"Review {review['number']} (build {review['build']})"
        feedback = " ".join((note or "").split())[:1000]
        if action == "approve":
            result = _approve_task(project, task_id, label)
        else:
            result = _request_changes(project, task_id, feedback, label)
        if result["ok"]:
            review.update(status="approved" if action == "approve" else "changes requested",
                          feedback=feedback, decidedAt=time.time())
            runtime.save(state)
        return result


def request_work_changes(project, task_id, note):
    runtime = runtime_for(project)
    with runtime.lock():
        state = runtime.state()
        if state.get("phase") == "reviewing" and state.get("task") == task_id:
            return {"ok": False, "error": "Wait for the AI review to finish saving."}
        reviews = state.get("reviews", {}).get(task_id, [])
        if reviews and reviews[-1]["status"] == "pending":
            try:
                runtime.require_review(state, task_id, reviews[-1]["id"])
            except ValueError:
                pass
            else:
                return {"ok": False, "error": "Open the pending review to request changes for its build."}
        return _request_changes(project, task_id, note, "Task feedback")


def _approve_task(project, task_id, label):
    """The person says the task is fine: tick its HUMAN gate(s). Only possible when the AI has finished every AUTO gate."""
    f = _task_file(project, task_id)
    if not f: return {"ok": False, "error": "unknown task"}
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    gates = [(i, GATE_LINE.match(l)) for i, l in enumerate(lines)]; gates = [(i, g) for i, g in gates if g]
    auto_open = [g for i, g in gates if not HUMAN_GATE.match(g.group(2).strip()) and g.group(1) == " "]
    if auto_open: return {"ok": False, "error": "The AI has not finished this task yet (%d automatic check(s) still open)." % len(auto_open)}
    ticked = 0
    for i, g in gates:
        if HUMAN_GATE.match(g.group(2).strip()) and g.group(1) == " ": lines[i] = lines[i].replace("[ ]", "[x]", 1); ticked += 1
    if not ticked: return {"ok": False, "error": "There is nothing waiting for you in this task."}
    _rewrite_status(lines)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if not any(l.strip() == "## Review log" for l in lines): lines += ["", "## Review log"]
    lines.append("%s approved by you in the app on %s." % (label, stamp))
    _replace_bytes(f, ("\n".join(lines) + "\n").encode("utf-8"))
    _after_edit(project, task_id, "%s: approved by you in the app" % task_id)
    return {"ok": True}


def _request_changes(project, task_id, note, label):
    """The person wants changes: add their note as a new automatic gate (so the AI must address it and the reviewer checks it), and do the task next."""
    f = _task_file(project, task_id)
    note = " ".join((note or "").split())
    if not f: return {"ok": False, "error": "unknown task"}
    if len(note) < 3: return {"ok": False, "error": "Say what should be changed."}
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, line in enumerate(lines):
        gate = GATE_LINE.match(line)
        if gate and HUMAN_GATE.match(gate.group(2).strip()):
            lines[i] = re.sub(r"\[[xX]\]", "[ ]", line, count=1)
    last = max((i for i, l in enumerate(lines) if GATE_LINE.match(l)), default=None)
    gate = "- [ ] AUTO: Fix what the owner asked for and prove it with a check: %s" % note[:300]
    if last is None: return {"ok": False, "error": "This task has no gates to extend."}
    lines.insert(last + 1, gate)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if not any(l.strip() == "## Owner feedback" for l in lines): lines += ["", "## Owner feedback"]
    lines.append("- %s on %s: %s" % (label, stamp, note))
    _rewrite_status(lines)
    _replace_bytes(f, ("\n".join(lines) + "\n").encode("utf-8"))
    _after_edit(project, task_id, "%s: changes asked for by you in the app" % task_id)
    queue_next(project, task_id)
    return {"ok": True}


# ---------------------------------------------------------------- projects that keep their live status in their own board (relay.sh): a mailbox the dispatcher reads
OWNER_KINDS = ("next", "fix", "approved")


def _owner_load(project):
    try: items = json.loads((pathlib.Path(project) / "OWNER-REQUESTS.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError): return []
    return [i for i in items if isinstance(i, dict) and i.get("kind") in OWNER_KINDS and re.fullmatch(r"T\d+[a-z]?", str(i.get("task", "")))]


def _owner_current(project):
    try:
        cur = (pathlib.Path(project) / "CURRENT_TASK.md").read_text(encoding="utf-8", errors="replace").splitlines()
        m = re.match(r"(T\d+[a-z]?)", cur[1].strip()) if len(cur) > 1 else None
        return m.group(1) if m else ""
    except OSError:
        return ""


def _owner_save(project, items):
    p = pathlib.Path(project)
    with open(p / "OWNER-REQUESTS.json", "w", encoding="utf-8", newline="\n") as fh: json.dump(items, fh, indent=1)
    def sect(kind): return [i for i in items if i["kind"] == kind]
    def review_label(item):
        return " [Review %d, build %s]" % (item["revision"], item["build"]) if item.get("revision") else ""
    lines = ["# Owner requests", "",
             "Written by the Agent Relay app when the owner presses a button. The dispatcher reads this file at the start of every round (see the OWNER REQUESTS rule).", "",
             "## DO NEXT (the first line is the most wanted)"]
    lines += ["- %s (%s)" % (i["task"], i["at"]) for i in sect("next")] or ["(none)"]
    lines += ["", "## FIX (the owner's feedback on a task: it stays open until the owner approves it)"]
    lines += ["- %s (%s)%s: %s" % (i["task"], i["at"], review_label(i), i.get("note", "")) for i in sect("fix")] or ["(none)"]
    lines += ["", "## APPROVED (the owner reviewed these and is happy; do not rework them. The owner ticks Human signed off in the board themselves)"]
    lines += ["- %s (%s)%s" % (i["task"], i["at"], review_label(i)) for i in sect("approved")] or ["(none)"]
    with open(p / "OWNER-REQUESTS.md", "w", encoding="utf-8", newline="\n") as fh: fh.write("\n".join(lines) + "\n")


def owner_requests(project):
    """Active requests. A 'do next' is dropped as soon as the relay is building that task."""
    items = _owner_load(project)
    cur = _owner_current(project)
    kept = [i for i in items if not (i["kind"] == "next" and i["task"] == cur)]
    if len(kept) != len(items): _owner_save(project, kept)
    return kept


def decide_board_review(project, task_id, revision, kind, note=""):
    runtime = runtime_for(project)
    with runtime.lock():
        state = runtime.state()
        reviews = state.get("reviews", {}).get(task_id, [])
        if not revision and (not reviews or (kind == "fix" and reviews[-1]["status"] != "pending")):
            return owner_request(project, kind, task_id, note)
        if state.get("phase") == "notion-review-in-flight" and state.get("task") == task_id:
            return {"ok": False, "error": "Wait for the board review to finish saving."}
        try:
            review = runtime.require_review(state, task_id, revision)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        result = owner_request(project, kind, task_id, note, review=review)
        if result["ok"]:
            review.update(status="approved" if kind == "approved" else "changes requested",
                          feedback=" ".join((note or "").split())[:400], decidedAt=time.time())
            runtime.save(state)
        return result


def owner_request(project, kind, task_id, note="", *, review=None):
    if kind not in OWNER_KINDS or not re.fullmatch(r"T\d+[a-z]?", task_id or ""): return {"ok": False, "error": "bad request"}
    if not (pathlib.Path(project) / "tasks" / (task_id + ".md")).exists(): return {"ok": False, "error": "unknown task"}
    note = " ".join((note or "").split())[:400]
    if kind == "fix" and len(note) < 3: return {"ok": False, "error": "Say what should be changed."}
    items = owner_requests(project)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if kind == "approved": items = [i for i in items if i["task"] != task_id]
    elif kind == "next": items = [i for i in items if not (i["task"] == task_id and i["kind"] in ("next", "approved"))]
    else: items = [i for i in items if not (i["task"] == task_id and i["kind"] == "approved")]
    entry = {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + task_id, "kind": kind, "task": task_id, "at": stamp, "note": note}
    if review:
        entry.update(revision=review["number"], build=review["build"], reviewId=review["id"])
    items = ([entry] + items) if kind == "next" else (items + [entry])
    _owner_save(project, items)
    return {"ok": True}


def owner_cancel(project, task_id, kind):
    items = [i for i in _owner_load(project) if not (i["task"] == task_id and i["kind"] == kind)]
    _owner_save(project, items)
    return {"ok": True}
