"""Projects the person already has: look at a folder, work out its stack and test command, put it on the relay's own work branch, and take a task list from a file.

Nothing here edits the person's code. The only things added to their repository are the relay scripts (committed on the work branch, never on main) and, later, the plan files."""
import json, pathlib, re, subprocess

import appapi

NO_WINDOW = appapi.NO_WINDOW
WORK_BRANCH = "agent-run"
SOURCE_NAME = "IMPORTED-TASKS.md"


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)


def detect_project(path):
    """Stack, the command that runs the tests, and the programs it needs, guessed from the files. Every part may be empty: it is a hint for the planner, not a verdict."""
    p = pathlib.Path(path); stack, test, req = [], "", []
    def has(*names): return any((p / n).exists() for n in names)
    if (p / "package.json").exists():
        stack.append("Node.js"); req.append("node")
        try:
            pj = json.loads((p / "package.json").read_text(encoding="utf-8-sig"))
            deps = {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}
            for k, label in (("next", "Next.js"), ("react", "React"), ("vue", "Vue"), ("svelte", "Svelte"), ("express", "Express"), ("typescript", "TypeScript"), ("vite", "Vite"), ("electron", "Electron")):
                if k in deps: stack.append(label)
            ts = (pj.get("scripts") or {}).get("test", "")
            if ts and "no test specified" not in ts: test = "npm test"
        except (OSError, ValueError): pass
    if has("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile") or list(p.glob("*.py")):
        stack.append("Python"); req.append("python")
        if not test:
            blob = ""
            for n in ("pyproject.toml", "requirements.txt", "setup.cfg", "Pipfile"):
                f = p / n
                if f.exists(): blob += f.read_text(encoding="utf-8", errors="replace").lower()
            if "pytest" in blob or (p / "pytest.ini").exists() or (p / "conftest.py").exists(): test = "python -m pytest -q"
            elif (p / "tests").is_dir() or list(p.glob("test_*.py")): test = "python -m unittest discover -s tests -q" if (p / "tests").is_dir() else "python -m unittest discover -q"
    if has("pom.xml"):
        stack += ["Java", "Maven"]; req += ["java", "maven"]; test = test or "mvn -B test"
    elif has("build.gradle", "build.gradle.kts"):
        stack += ["Java/Kotlin", "Gradle"]; req.append("java"); test = test or ("gradlew.bat test" if (p / "gradlew.bat").exists() and __import__("os").name == "nt" else "./gradlew test")
    if has("go.mod"): stack.append("Go"); req.append("go"); test = test or "go test ./..."
    if has("Cargo.toml"): stack.append("Rust"); req.append("rust"); test = test or "cargo test"
    if list(p.glob("*.sln")) or list(p.glob("*.csproj")): stack.append(".NET"); req.append("dotnet"); test = test or "dotnet test"
    if has("composer.json"): stack.append("PHP"); req.append("php"); test = test or "composer test"
    if has("Gemfile"): stack.append("Ruby"); req.append("ruby"); test = test or "bundle exec rake test"
    if has("project.godot"): stack.append("Godot"); req.append("godot")
    if has("docker-compose.yml", "compose.yaml", "Dockerfile"): req.append("docker")
    if not test and (p / "Makefile").exists() and re.search(r"^test:", (p / "Makefile").read_text(encoding="utf-8", errors="replace"), re.M): test = "make test"
    seen = set(); stack = [s for s in stack if not (s in seen or seen.add(s))]; seen = set(); req = [r for r in req if not (r in seen or seen.add(r))]
    return {"stack": stack, "testCommand": test, "requires": req}


def configure_saved_tasks(project, test_command):
    """Install a standard runner without invoking a planner or rewriting existing tasks."""
    info = inspect(project)
    if not info["ok"]:
        return info
    p = pathlib.Path(info["path"])
    if not info["isGit"] or not info.get("hasCommits") or info.get("needsBranch"):
        return {"ok": False, "error": "Open a Git work branch with a saved starting commit first."}
    configuration = p / "relay.project.json"
    if info["running"] or info["hasRelay"] or configuration.exists():
        return {"ok": False, "error": "This project's runner or configuration already exists."}
    if not info["tasksReady"] or not info["hasAgents"]:
        return {"ok": False, "error": "Local setup needs AGENTS.md and explicit AUTO: and HUMAN: gates on every task. For other task boards, use the project's custom runner."}
    if not isinstance(test_command, str) or not test_command.strip():
        return {"ok": False, "error": "Enter the project's test command."}
    result = appapi.install_relay(p)
    if not result["ok"]:
        return result
    try:
        with configuration.open("x", encoding="utf-8") as output:
            json.dump({"name": p.name, "testCommand": test_command.strip(), "branch": info["branch"]}, output, indent=2)
            output.write("\n")
    except OSError as exc:
        return {"ok": False, "error": f"Could not save runner configuration: {exc}"}
    return {"ok": True}


def inspect(path):
    """What is in this folder? Read-only."""
    p = pathlib.Path(str(path or "").strip().strip('"')).expanduser()
    if not str(path or "").strip() or not p.is_dir(): return {"ok": False, "error": "That folder does not exist."}
    p = p.resolve()
    is_git = (p / ".git").exists()
    info = {"ok": True, "path": str(p), "name": p.name, "isGit": is_git, "detect": detect_project(p), "hasAgents": (p / "AGENTS.md").exists(),
            "hasRelay": bool(appapi.relay_script(p)), "running": appapi.relay_running(p)}
    ready, n = appapi.formatted_tasks(p)
    info["tasksReady"], info["taskFiles"] = ready, n
    metadata = appapi.project_metadata(p)
    info["readyToOpen"] = bool(n or info["hasRelay"] or metadata.get("localWorkspace") is True)
    info["tasksFolderOther"] = (p / "tasks").is_dir() and not ready
    if is_git:
        b = _git(p, "rev-parse", "--abbrev-ref", "HEAD"); info["branch"] = b.stdout.strip() if b.returncode == 0 else ""
        info["hasCommits"] = _git(p, "rev-parse", "HEAD").returncode == 0
        st = _git(p, "status", "--porcelain")
        info["dirtyCount"] = len([l for l in st.stdout.splitlines() if l.strip()])
        info["needsBranch"] = info["branch"] in ("main", "master", "HEAD", "")
    return info


def attach(path, init_git=False):
    """Make the folder ready for the relay: a git repository on its own work branch, with the relay scripts committed there. Never touches the person's files."""
    info = inspect(path)
    if not info["ok"]: return info
    p = pathlib.Path(info["path"]); did = []
    if not info["isGit"]:
        if not init_git: return {"ok": False, "needsGit": True, "error": "This folder is not a git project yet."}
        r = _git(p, "init", "-q", "-b", WORK_BRANCH)
        if r.returncode != 0: return {"ok": False, "error": "git init failed: " + (r.stderr or r.stdout)[-200:]}
        _git(p, "add", "-A")
        ident = [] if _git(p, "config", "user.email").stdout.strip() else ["-c", "user.name=Agent Relay", "-c", "user.email=agent-relay@localhost"]
        c = subprocess.run(["git", "-C", str(p), *ident, "commit", "-q", "-m", "Starting point before Agent Relay", "--allow-empty"], capture_output=True, text=True, creationflags=NO_WINDOW)
        if c.returncode != 0: return {"ok": False, "error": "could not make the first commit: " + (c.stderr or c.stdout)[-200:]}
        did.append("made it a git project and saved a starting point")
        info = inspect(p)
    elif not info.get("hasCommits"):
        return {"ok": False, "error": "This git project has no commits yet. Make a first commit, then try again."}
    if info.get("needsBranch"):
        has_branch = _git(p, "rev-parse", "--verify", "--quiet", WORK_BRANCH).returncode == 0
        r = _git(p, "checkout", WORK_BRANCH) if has_branch else _git(p, "checkout", "-b", WORK_BRANCH)
        if r.returncode != 0: return {"ok": False, "error": "could not switch to the work branch: " + (r.stderr or r.stdout)[-250:]}
        did.append("switched to its own work branch '%s' (your main branch is untouched)" % WORK_BRANCH)
    rel = appapi.install_relay(str(p), commit=True)
    did.append("added the relay scripts on that branch")
    cfg = appapi.load_config(); cfg["project"] = str(p); appapi.save_config(cfg)
    return {"ok": True, "path": str(p), "did": did, "info": inspect(p), "relay": rel}


def import_source(project, path="", text=""):
    """Take the person's task list from a file or from pasted text and save it in the project as IMPORTED-TASKS.md for the planner to read."""
    p = pathlib.Path(project)
    body = ""
    if path:
        f = pathlib.Path(str(path).strip().strip('"')).expanduser()
        if not f.is_file(): return {"ok": False, "error": "That file does not exist."}
        if f.stat().st_size > 400_000: return {"ok": False, "error": "That file is too big for a task list (over 400 KB)."}
        raw = f.read_bytes()
        if b"\x00" in raw[:2000]: return {"ok": False, "error": "That looks like a binary file, not a text task list."}
        body = raw.decode("utf-8", errors="replace")
        origin = f.name
    else:
        body = text or ""; origin = "pasted text"
    if len(body.strip()) < 5: return {"ok": False, "error": "The task list is empty."}
    (p / SOURCE_NAME).write_text("<!-- Task list given by the person (source: %s). The planner converts it into tasks/T*.md. -->\n\n%s\n" % (origin, body.strip()), encoding="utf-8")
    return {"ok": True, "source": SOURCE_NAME, "chars": len(body), "origin": origin}
