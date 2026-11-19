"""Local routing decisions, task checkpoints and evidence. No provider calls or credentials."""
import argparse
import contextlib
from collections import Counter
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from functools import wraps

PROVIDERS = ("claude", "codex", "copilot", "opencode")
DEFAULT_CAPABILITIES = {p: ["repository", "shell"] for p in PROVIDERS}
DEFAULT_LIMITS = {"callsPerTask": 24, "callsPerProviderDay": 100, "failuresPerTask": 3,
                  "sameFailureLimit": 2, "timeoutSeconds": 5400, "idleWarningSeconds": 600}
GATE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.*)$")
HUMAN = re.compile(r"\**\s*(?:HUMAN|OWNER)\b", re.I)
SAFE_ID = re.compile(r"^[a-zA-Z0-9_.:/+-]{1,100}$")
ARTIFACT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".pdf", ".zip", ".txt", ".log", ".json"}


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30,
                            creationflags=0x08000000 if os.name == "nt" else 0)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "git failed")
    return result.stdout.strip()


def model_key(model):
    if not isinstance(model, str) or not SAFE_ID.fullmatch(model.strip()):
        return None
    key = model.strip().lower().split("/")[-1].removesuffix(":free").removesuffix("-latest")
    return key if key not in ("", "auto", "default", "unknown", "latest", "sonnet", "opus", "haiku") else None


def launcher_signature(executable):
    try:
        path = Path(executable).resolve(strict=True)
        info = path.stat()
        return {"path": str(path), "size": info.st_size, "mtime": info.st_mtime_ns}
    except (OSError, ValueError):
        return None


def launcher_key(executable):
    path = Path(executable).resolve()
    if os.name == "nt" and path.suffix.lower() in (".cmd", ".bat"):
        path = path.with_suffix("")
    return os.path.normcase(str(path))


def call_models(call):
    reported = call.get("reportedModels") or [call.get("reportedModel")]
    models = {model_key(model) for model in reported} - {None}
    return models or ({model_key(call.get("requestedModel"))} - {None})


def task_builders(state):
    return [
        call for call in state["calls"]
        if call["role"] == "builder" and call["task"] == state["task"]
        and not (call.get("outcome") in ("error", "quota", "timeout", "interrupted") and call.get("changedCode") is False)
    ]


def reported_models(provider, path):
    with path.open(encoding="utf-8", errors="replace") as stream:
        if provider == "claude":
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("type") == "system" and item.get("subtype") == "init":
                    model = item.get("model")
                    return [model] if model_key(model) else []
            return []
        if provider != "codex":
            return []
        ansi = re.compile(r"\x1b\[[0-9;]*m")
        header = []
        for _ in range(32):
            line = ansi.sub("", stream.readline()).strip()
            if line.startswith("OpenAI Codex v"):
                header = [line]
            elif header:
                header.append(line)
                if len(header) > 2 and line == "--------":
                    break
        if len(header) < 4 or header[1] != "--------" or header[-1] != "--------":
            return []
        fields = dict(line.split(": ", 1) for line in header if ": " in line)
        model = fields.get("model")
        if not {"workdir", "provider", "session id"} <= fields.keys() or not model_key(model):
            return []
        models = [model]
        for line in stream:
            match = re.fullmatch(r"model rerouted: (\S+) -> (\S+)", ansi.sub("", line).strip())
            if match and model_key(match[1]) in {model_key(value) for value in models} and model_key(match[2]):
                if match[2] not in models:
                    models.append(match[2])
        return models


def synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.lock():
            return method(self, *args, **kwargs)
    return locked


class Runtime:
    def __init__(self, root="."):
        self.root = Path(root).resolve()
        self.directory = self.root / ".relay"
        self.path = self.directory / "state.json"

    @contextlib.contextmanager
    def lock(self):
        self.directory.mkdir(exist_ok=True)
        with (self.directory / "state.lock").open("a+b") as stream:
            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt": msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else: fcntl.flock(stream, fcntl.LOCK_UN)

    def state(self):
        state = read_json(self.path, {"version": 1, "task": "", "phase": "idle", "calls": [], "events": [], "artifacts": {}})
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("Unsupported or damaged Relay checkpoint; keep it for recovery.")
        return state

    def save(self, state):
        atomic_json(self.path, state)

    @staticmethod
    def process_alive(pid):
        if os.name == "nt":
            import csv
            result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                    timeout=10, creationflags=0x08000000)
            if result.returncode: return True  # An inspection failure must not release another worker's lock.
            return any(len(row) > 1 and row[1] == str(pid) for row in csv.reader(result.stdout.splitlines()))
        try: os.kill(pid, 0)
        except ProcessLookupError: return False
        except PermissionError: return True
        return True

    @staticmethod
    def process_identity(pid):
        try:
            if os.name == "nt":
                script = f"$p=Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue; if ($p) {{ $p.StartTime.ToUniversalTime().Ticks }}"
                result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], capture_output=True, text=True,
                                        timeout=10, creationflags=0x08000000)
                value = result.stdout.strip()
                return value if value.isdigit() else None
            return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            return None

    def _release_stale_lock(self):
        lock = self.root / ".relay.lock"
        if not lock.exists(): return True
        if lock.is_symlink(): raise ValueError("Worker lock cannot be a symlink")
        owner = read_json(lock / "owner.json", {})
        if owner.get("root") != str(self.root) or not isinstance(owner.get("pid"), int) or owner["pid"] <= 0:
            return False
        if self.process_alive(owner["pid"]): return False
        if {p.name for p in lock.iterdir()} != {"owner.json"}: return False
        (lock / "owner.json").unlink()
        lock.rmdir()
        return True

    @synchronized
    def release_stale_lock(self):
        return self._release_stale_lock()

    @synchronized
    def claim(self, pid):
        if int(pid) <= 0: raise ValueError("Worker PID must be positive")
        if not self._release_stale_lock(): raise ValueError("Worker lock is active or unverified; stop/inspect that worker first")
        lock = self.root / ".relay.lock"
        lock.mkdir()
        atomic_json(lock / "owner.json", {"pid": int(pid), "root": str(self.root), "identity": self.process_identity(pid), "at": time.time()})

    def config(self):
        cfg = read_json(self.root / "relay.settings.json", {})
        if not isinstance(cfg, dict):
            raise ValueError("Relay settings must be an object")
        return cfg

    def project_access(self):
        if (self.root / "relay.sh").exists():
            return {"checkedOnly": False, "tools": {}}
        return read_json(self.directory / "project-access.json", {"checkedOnly": False, "tools": {}})

    @synchronized
    def set_access_policy(self, enabled):
        data = self.project_access()
        data["checkedOnly"] = enabled
        atomic_json(self.directory / "project-access.json", data)

    @synchronized
    def record_access(self, provider, executable, passed):
        signature = launcher_signature(executable)
        if signature is None:
            raise ValueError("The AI tool's launcher is no longer available.")
        data = self.project_access()
        data["tools"][provider] = {"launcher": signature, "passed": passed, "at": time.time()}
        atomic_json(self.directory / "project-access.json", data)

    def access_status(self, provider, executable=None, data=None):
        data = self.project_access() if data is None else data
        entry = data["tools"].get(provider)
        if not entry:
            return "unchecked"
        launcher = entry["launcher"]
        if launcher_signature(launcher["path"]) != launcher:
            return "unchecked"
        if executable and launcher_key(executable) != launcher_key(launcher["path"]):
            return "unchecked"
        return "passed" if entry["passed"] else "failed"

    def limits(self):
        return {**DEFAULT_LIMITS, **self.config().get("limits", {})}

    def event(self, state, kind, message):
        event = {"id": uuid.uuid4().hex, "at": time.time(), "kind": kind, "task": state.get("task", ""), "message": message}
        state["events"] = (state.get("events", []) + [event])[-100:]
        return event

    def requirements(self, role):
        project = read_json(self.root / "relay.project.json", {})
        required = set(project.get("requiredCapabilities", {}).get(role, ["repository", "shell"]))
        if os.environ.get("RELAY_NOTION_BOARD") == "1" and role in ("dispatcher", "reviewer"):
            required.add("notion")
        current = self.root / "CURRENT_TASK.md"
        if current.exists():
            for line in current.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.upper().startswith("REQUIRES:"):
                    required.update(x.strip() for x in re.split(r"[,\s]+", line.split(":", 1)[1]) if x.strip())
        if any(not isinstance(x, str) or not SAFE_ID.fullmatch(x) for x in required):
            raise ValueError("Invalid required capability")
        return required

    def eligible(self, role, provider, model, now=None):
        now = time.time() if now is None else now
        cfg = self.config()
        capabilities = cfg.get("capabilities", {}).get(provider, DEFAULT_CAPABILITIES.get(provider, []))
        missing = self.requirements(role) - set(capabilities)
        if missing:
            return False, "missing capabilities: " + ", ".join(sorted(missing))
        if role in ("dispatcher", "builder", "reviewer"):
            access = self.project_access()
            status = self.access_status(provider, os.environ.get("RELAY_ACCESS_CLI"), access)
            if status == "failed":
                return False, "project access check failed. Check the tool again under AIs & settings."
            if access["checkedOnly"] and status != "passed":
                return False, "this project requires a passing AI access check"
        state = self.state()
        if role == "reviewer" and state.get("phase") == "notion-review-in-flight":
            return False, "the previous Notion review may have updated the board"
        calls = state.get("calls", [])
        task = "" if role == "dispatcher" else state.get("task", "")
        limits = self.limits()
        all_task_calls = [c for c in calls if c.get("task") == task]
        task_calls = [c for c in all_task_calls if c["startedAt"] >= state.get("budgetStartedAt", 0)] if role != "dispatcher" else []
        today = dt.datetime.fromtimestamp(now).date()
        day_calls = [c for c in calls if c["provider"] == provider and dt.datetime.fromtimestamp(c["startedAt"]).date() == today]
        if len(task_calls) >= limits["callsPerTask"]:
            return False, "task call budget reached"
        if len(day_calls) >= limits["callsPerProviderDay"]:
            return False, "provider daily call budget reached"
        failures = [c for c in task_calls if c.get("outcome") in ("error", "timeout")]
        if len(failures) >= limits["failuresPerTask"]:
            return False, "task failure budget reached"
        failed_provider = [c for c in failures if c["provider"] == provider and c["role"] == role]
        n = limits["sameFailureLimit"]
        if len(failed_provider) >= n and len({c.get("outputHash") for c in failed_provider[-n:]}) == 1:
            return False, "same failure repeated; try another provider"
        if role == "reviewer" and state.get("phase") == "reviewing":
            builders = task_builders(state)
            if not builders:
                return False, "No builder model was recorded for this task"
            for built in builders:
                models = call_models(built)
                if not models:
                    return False, f"{built['provider']} did not identify its build model. Use a named model or a tool that reports its model."
                if model_key(model) in models:
                    return False, "model already built this task"
        elif role == "reviewer":
            for built in all_task_calls:
                if built["role"] != "builder":
                    continue
                actual = built.get("reportedModel") or built.get("requestedModel")
                if model_key(model) and model_key(model) == model_key(actual):
                    return False, "model already built this task"
                if provider == built["provider"] and (not model_key(model) or not model_key(actual)):
                    return False, "builder model was auto/unreported; use another provider"
        return True, "eligible"

    @synchronized
    def begin(self, role, provider, model, live):
        state = self.state()
        ok, reason = self.eligible(role, provider, model)
        if not ok:
            raise ValueError(reason)
        call = {"id": uuid.uuid4().hex, "task": "" if role == "dispatcher" else state.get("task", ""), "role": role, "provider": provider,
                "requestedModel": model, "reportedModel": None, "startedAt": time.time(), "log": live, "outcome": "running",
                "beforeHead": git(self.root, "rev-parse", "HEAD")}
        if role == "builder":
            call["beforeCode"] = self.code_fingerprint()
        if role == "reviewer" and state.get("phase") == "reviewing":
            state.pop("independentReview", None)
        state["calls"].append(call)
        state["activeCall"] = call["id"]
        if role == "reviewer" and state.get("phase") == "notion-review-ready":
            state.update(phase="notion-review-in-flight", boardReviewCall=call["id"])
        self.event(state, "activity", f"{role}: {provider} ({model})")
        self.save(state)
        return call["id"]

    @synchronized
    def finish(self, call_id, code, outcome):
        state = self.state()
        call = next(c for c in state["calls"] if c["id"] == call_id)
        call.update(endedAt=time.time(), exitCode=code, outcome=outcome)
        call["afterHead"] = git(self.root, "rev-parse", "HEAD")
        call["changedFiles"] = git(self.root, "diff", "--name-only", call["beforeHead"], "--").splitlines()
        if call["role"] == "builder" and "beforeCode" in call:
            call["changedCode"] = call["afterHead"] != call["beforeHead"] or self.code_fingerprint() != call["beforeCode"]
        live = self.safe_path(call["log"], extensions=False)
        if live.is_file():
            call["outputHash"] = digest(live)
            models = reported_models(call["provider"], live)
            if models:
                call.update(reportedModel=models[-1], reportedModels=models)
        if (call["role"] == "builder" and outcome == "success" and code == 0
                and state.get("phase") == "building" and state.get("buildContext")
                and self.build_context() == state["buildContext"]):
            state["finishedBuild"] = {"call": call["id"], "files": self.build_files()}
        if call["role"] == "reviewer" and state.get("phase") == "reviewing":
            if outcome != "success" or code != 0:
                self.restore_review(state)
            else:
                self.check_reviewer(state, call)
        elif call["role"] == "reviewer" and call.get("reportedModel"):
            if any(call_models(call) & call_models(built) for built in task_builders(state)):
                self.reject_reviewer(state, call, "self_review_rejected", "Reviewer used the builder's model.")
        state.pop("activeCall", None)
        self.event(state, "activity", f"{call['role']}: {call['provider']} {outcome} (exit {code})")
        self.save(state)

    def check_reviewer(self, state, call):
        reviewers = call_models(call)
        builders = [call_models(built) for built in task_builders(state)]
        if not reviewers or not builders or not all(builders):
            self.reject_reviewer(state, call, "review_identity_unknown",
                                 "Review returned without a known model. Use a named model or a tool that reports its model.")
        if any(reviewers & models for models in builders):
            self.reject_reviewer(state, call, "self_review_rejected",
                                 "Reviewer used the builder's model. Choose a different model.")
        state["independentReview"] = call["id"]
        self.event(state, "activity", "Independent review: " + ", ".join(sorted(reviewers)))

    def reject_reviewer(self, state, call, rejection, message):
        call["outcome"] = rejection
        state.pop("activeCall", None)
        self.restore_review(state)
        self.event(state, "routing", message)
        self.save(state)
        raise ValueError(message)

    def code_fingerprint(self, *, for_build=False):
        change = git(self.root, "diff", "--binary", "HEAD", "--", ".", ":(exclude)tasks", ":(exclude)CURRENT_TASK.md",
                     ":(exclude)REVIEW.md", ":(exclude)PLAYTEST_QUEUE.md")
        files = []
        for name in git(self.root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            if not name or re.match(r"(?:CURRENT_TASK\.md$|REVIEW\.md$|PLAYTEST_QUEUE\.md$|\.relay/|tasks/)", name):
                continue
            if name.startswith(("relay-", "relay.")) and not for_build:
                continue
            path = self.root / name
            if for_build and path.is_symlink():
                files.append(name + ":link:" + os.readlink(path))
            else:
                path = path.resolve()
                path.relative_to(self.root)
                if path.is_file(): files.append(name + ":" + digest(path))
        return hashlib.sha256((change + "\n" + "\n".join(files)).encode()).hexdigest()

    def build_context(self):
        current = self.root / "CURRENT_TASK.md"
        if not current.is_file():
            return None
        lines = current.read_text(encoding="utf-8-sig").splitlines()
        if not lines or not re.fullmatch(r"tasks/T\d+[a-z]?\.md", lines[0]):
            return None
        task = self.safe_path(lines[0], extensions=False)
        if not task.is_file():
            return None
        review = self.root / "REVIEW.md"
        return {
            "branch": git(self.root, "rev-parse", "--abbrev-ref", "HEAD"),
            "current": digest(current),
            "task": digest(task),
            "review": digest(review) if review.is_file() else None,
        }

    def build_files(self):
        # The index can change while the working copy still looks the same.
        files = {"code": self.code_fingerprint(for_build=True), "index": git(self.root, "ls-files", "--stage", "-z")}
        for name in ("relay-commit.txt", "relay.settings.json", "relay.providers.json", "relay.project.json"):
            path = self.root / name
            files[name] = digest(path) if path.is_file() else None
        return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()

    def build_resume_phase(self, state, command):
        if (not state.get("buildContext") or not state.get("buildFiles")
                or state.get("head") != git(self.root, "rev-parse", "HEAD")
                or state.get("commandHash") != hashlib.sha256(command.encode()).hexdigest()
                or state["buildContext"] != self.build_context()):
            return "blocked"
        files = self.build_files()
        finished = state.get("finishedBuild")
        if finished:
            if finished["files"] == files and any(
                call["id"] == finished["call"] and call["role"] == "builder" and call["task"] == state["task"]
                and call.get("outcome") == "success" and call.get("exitCode") == 0 for call in state["calls"]
            ):
                return "build-ready"
            return "blocked"
        return "building" if state["buildFiles"] == files else "blocked"

    @synchronized
    def checkpoint(self, phase, task="", command="", exit_code=None):
        state = self.state()
        head = git(self.root, "rev-parse", "HEAD")
        if task:
            state["taskTitle"] = task
            match = re.match(r"T\d+[a-z]?\b", task)
            if match: task = match[0]
        if task and task != state.get("task"):
            state.pop("evidence", None)
            state.pop("reviewBackup", None)
            state["task"] = task
            state.pop("budgetStartedAt", None)
        state.update(phase=phase, head=head, commandHash=hashlib.sha256(command.encode()).hexdigest(), updatedAt=time.time())
        if phase in ("building", "testing", "reviewing"):
            current = self.root / "CURRENT_TASK.md"
            if not current.is_file():
                raise ValueError("Current task is missing")
            state["taskHash"] = digest(current)
        if phase == "building":
            state["beforeHead"] = head
            state.pop("retryApproved", None)
            state.pop("finishedBuild", None)
            state["buildContext"] = self.build_context()
            state["buildFiles"] = self.build_files() if state["buildContext"] else None
        if phase == "testing":
            state.pop("finishedBuild", None)
            state.pop("evidence", None)
            state["testedCode"] = self.code_fingerprint()
        if phase == "reviewing":
            state.pop("independentReview", None)
            log = self.root / "relay-tests.log"
            if exit_code != 0 or not command.strip() or not log.is_file():
                raise ValueError("Review requires an executed, passing test command and its log")
            if state.get("testedCode") != self.code_fingerprint():
                raise ValueError("Code changed while tests ran; rerun tests before review")
            self.directory.mkdir(exist_ok=True)
            artifact = self.directory / "evidence" / (uuid.uuid4().hex + ".log")
            artifact.parent.mkdir(exist_ok=True)
            shutil.copyfile(log, artifact)
            state["evidence"] = {"head": head, "commandHash": state["commandHash"], "exitCode": 0,
                                 "codeHash": self.code_fingerprint(), "log": artifact.relative_to(self.root).as_posix(), "sha256": digest(artifact), "at": time.time()}
            current_lines = (self.root / "CURRENT_TASK.md").read_text(encoding="utf-8").splitlines()
            path = self.safe_path(current_lines[0], extensions=False)
            state["reviewBackup"] = {"path": path.relative_to(self.root).as_posix(), "text": path.read_text(encoding="utf-8"),
                                     "currentTask": (self.root / "CURRENT_TASK.md").read_text(encoding="utf-8")}
            state["reviewBackup"]["notes"] = {
                name: (self.root / name).read_text(encoding="utf-8") if (self.root / name).is_file() else None
                for name in ("REVIEW.md", "PLAYTEST_QUEUE.md")
            }
        self.save(state)

    def evidence_valid(self, state, command):
        evidence = state.get("evidence", {})
        try:
            log = self.safe_path(evidence["log"], extensions=False)
            return (evidence["exitCode"] == 0 and evidence["head"] == git(self.root, "rev-parse", "HEAD")
                    and evidence["commandHash"] == hashlib.sha256(command.encode()).hexdigest()
                    and evidence["codeHash"] == self.code_fingerprint()
                    and digest(log) == evidence["sha256"])
        except (KeyError, OSError, ValueError):
            return False

    @synchronized
    def board_ready(self, task, command, headless_code, graphics_code):
        """A board review has remote side effects, so local task restoration is insufficient."""
        state = self.state()
        head = git(self.root, "rev-parse", "HEAD")
        current = self.root / "CURRENT_TASK.md"
        if state["phase"] != "testing" or state["head"] != head or state["taskTitle"] != task:
            raise ValueError("Notion review needs a matching testing checkpoint")
        if (state["commandHash"] != hashlib.sha256(command.encode()).hexdigest()
                or state["taskHash"] != digest(current) or state["testedCode"] != self.code_fingerprint()):
            raise ValueError("Notion review input changed while tests ran")
        if headless_code != 0 or graphics_code not in (0, None):
            raise ValueError("Notion review needs passing tests and any enabled graphics checks")
        log = self.directory / "evidence" / (uuid.uuid4().hex + ".log")
        log.parent.mkdir(exist_ok=True)
        shutil.copyfile(self.root / "relay-tests.log", log)
        state.update(phase="notion-review-ready", graphicsRan=graphics_code is not None,
                     boardQueueBefore=self.review_queue_lines(),
                     evidence={"head": head, "commandHash": state["commandHash"], "exitCode": 0,
                               "codeHash": state["testedCode"], "log": log.relative_to(self.root).as_posix(),
                               "sha256": digest(log), "at": time.time()})
        self.save(state)

    @synchronized
    def board_resume(self, command):
        state = self.state()
        phase = state.get("phase")
        if phase == "notion-review-in-flight":
            self.event(state, "blocked", "Notion review may have written to the board. Reconcile the board and local task before retrying.")
            self.save(state)
            return "blocked"
        if phase == "notion-review-ready":
            current = self.root / "CURRENT_TASK.md"
            if current.is_file() and digest(current) == state["taskHash"] and self.evidence_valid(state, command):
                self.event(state, "recovered", "Resuming Notion review from saved test evidence")
                self.save(state)
                return phase
            self.event(state, "blocked", "Notion review evidence or task changed. Retry the task to generate fresh evidence.")
            self.save(state)
            return "blocked"
        return self._resume(command, recover_build=False)

    @synchronized
    def board_check(self, command):
        state = self.state()
        call = next((c for c in state["calls"] if c["id"] == state.get("boardReviewCall")), {})
        if (state.get("phase") != "notion-review-in-flight" or call.get("outcome") != "success"
                or not self.evidence_valid(state, command)):
            raise ValueError("Notion review did not complete with matching evidence")
        task = state["task"]
        if not re.fullmatch(r"T\d+[a-z]?", task):
            return
        path = self.safe_path(f"tasks/{task}.md", extensions=False)
        if not path.is_file():
            return
        gates = [m[2].strip() for line in path.read_text(encoding="utf-8").splitlines()
                 if (m := GATE.match(line)) and m[1] == " "]
        if ("boardQueueBefore" in state and gates and all(HUMAN.match(gate) for gate in gates)
                and state["evidence"]["codeHash"] == hashlib.sha256(b"\n").hexdigest()):
            note = self.review_queue_note(task, state.get("boardQueueBefore", []))
            if note and "NEEDS OWNER" not in note:
                self.publish_review(state, path, note, call["id"])
                self.save(state)

    @synchronized
    def board_reconcile(self, task):
        state = self.state()
        if not self._release_stale_lock():
            raise ValueError("Stop the worker before reconciling the board")
        if state.get("phase") != "notion-review-in-flight" or task != state.get("task"):
            raise ValueError("Task does not match the interrupted Notion review")
        state["phase"] = "idle"
        for key in ("activeCall", "boardReviewCall", "evidence"):
            state.pop(key, None)
        self.event(state, "recovered", "Owner reconciled the Notion review and local task")
        self.save(state)

    @synchronized
    def resume(self, command):
        return self._resume(command)

    def _resume(self, command, *, recover_build=True):
        state = self.state()
        if state.get("phase") == "idle":
            return "idle"
        if state.get("phase") not in ("building", "testing", "reviewing"):
            return "idle"
        interrupted = self.interrupt_call(state)
        if state["phase"] == "building" and recover_build:
            phase = self.build_resume_phase(state, command)
            if phase == "blocked":
                self.event(state, "blocked", "The saved build and project files differ. Review the edits before retrying.")
            else:
                if interrupted and interrupted["role"] == "builder" and phase == "building":
                    interrupted["changedCode"] = False
                message = "Continuing the finished build without calling the builder again" if phase == "build-ready" else "Resuming the saved task from matching files"
                self.event(state, "recovered", message)
            self.save(state)
            return phase
        current = self.root / "CURRENT_TASK.md"
        valid = state.get("head") == git(self.root, "rev-parse", "HEAD") and state.get("commandHash") == hashlib.sha256(command.encode()).hexdigest()
        task_context_matches = not current.exists() or (current.is_file() and digest(current) == state.get("taskHash"))
        if state["phase"] == "reviewing" and valid and task_context_matches and self.evidence_valid(state, command):
            self.restore_review(state)
            state.pop("independentReview", None)
            self.event(state, "recovered", "Resuming review from verified test evidence")
            self.save(state)
            return "reviewing"
        if valid and current.is_file() and digest(current) == state.get("taskHash"):
            if state["phase"] == "building":
                # A crash may leave partial edits. Do not silently run a fresh builder over them.
                self.event(state, "blocked", "Interrupted build: inspect unfinished work before resuming")
                self.save(state)
                return "blocked"
            if state["phase"] == "testing" and state.get("testedCode") == self.code_fingerprint():
                self.save(state)
                return "testing"
        self.event(state, "blocked", "Checkpoint no longer matches task, commit or test command")
        self.save(state)
        return "blocked"

    @staticmethod
    def interrupt_call(state):
        active = state.pop("activeCall", None)
        if active is not None:
            for call in state["calls"]:
                if call["id"] == active and call["outcome"] == "running":
                    call.update(outcome="interrupted", endedAt=time.time())
                    return call
        return None

    def restore_review(self, state=None):
        state = state or self.state()
        backup = state.get("reviewBackup")
        if backup:
            self.safe_path(backup["path"], extensions=False).write_text(backup["text"], encoding="utf-8")
            (self.root / "CURRENT_TASK.md").write_text(backup["currentTask"], encoding="utf-8")
            for name, contents in backup.get("notes", {}).items():
                path = self.safe_path(name, extensions=False)
                if contents is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(contents, encoding="utf-8")

    @synchronized
    def guard_review(self, command):
        state = self.state()
        backup = state.get("reviewBackup")
        if not backup or not self.evidence_valid(state, command):
            self.restore_review(state)
            raise ValueError("Review rejected: missing or changed test evidence")
        if state.get("phase") != "reviewing" or not state.get("independentReview"):
            self.restore_review(state)
            raise ValueError("Review rejected: no independent model completed this review")
        path = self.safe_path(backup["path"], extensions=False)
        if not path.is_file():
            self.restore_review(state)
            raise ValueError("Reviewer deleted the task; restored the saved version")
        now = path.read_text(encoding="utf-8").splitlines()
        original = {}
        for line in backup["text"].splitlines():
            match = GATE.match(line)
            if match:
                original[match[2].strip()] = line
        current = {GATE.match(line)[2].strip(): i for i, line in enumerate(now) if GATE.match(line)}
        for label, line in original.items():
            if label not in current:
                now.append(line)
            elif HUMAN.match(label):
                now[current[label]] = line
        # A reviewer cannot invent an already-completed gate.
        for index, line in enumerate(now):
            match = GATE.match(line)
            if match and match[2].strip() not in original and match[1] != " ":
                now[index] = re.sub(r"\[[xX]\]", "[ ]", line, count=1)
        gates = [GATE.match(line) for line in now if GATE.match(line)]
        auto_open = any(g[1] == " " and not HUMAN.match(g[2]) for g in gates)
        human_open = any(g[1] == " " and HUMAN.match(g[2]) for g in gates)
        if human_open and not auto_open and state["evidence"]["codeHash"] != hashlib.sha256(b"\n").hexdigest():
            self.restore_review(state)
            raise ValueError("Commit the tested project files before queuing human review.")
        path.write_text("\n".join(now) + "\n", encoding="utf-8")
        if human_open and not auto_open:
            previous = (state["reviewBackup"].get("notes", {}).get("PLAYTEST_QUEUE.md") or "").splitlines()
            note = self.review_queue_note(path.stem, previous)
            checks = "\n".join(g[2].strip() for g in gates if g[1] == " ")
            self.publish_review(state, path, note or checks, state["independentReview"])
        kind = "task_updated" if auto_open else "needs_review" if human_open else "task_done"
        self.event(state, kind, "Automatic review checked; human approval remains separate")
        self.save(state)

    def review_queue_lines(self):
        queue = self.root / "PLAYTEST_QUEUE.md"
        return queue.read_text(encoding="utf-8").splitlines() if queue.is_file() else []

    def review_queue_note(self, task, previous):
        old = Counter(previous)
        note = ""
        for line in self.review_queue_lines():
            if old[line]:
                old[line] -= 1
                continue
            match = re.fullmatch(rf"{re.escape(task)}\s+[—-]\s+.*?\s+[—-]\s+(.*)", line)
            if match:
                note = match[1].strip()
        return note

    def publish_review(self, state, path, note, review_call):
        task = path.stem
        evidence = state["evidence"]
        revisions = state.setdefault("reviews", {}).setdefault(task, [])
        task_hash = digest(path)
        if revisions and revisions[-1]["evidence"] == evidence["log"] and revisions[-1]["taskHash"] == task_hash:
            return
        for review in revisions:
            if review["status"] == "pending":
                review["status"] = "superseded"
        review = {
            "id": uuid.uuid4().hex, "number": len(revisions) + 1, "build": evidence["head"],
            "codeHash": evidence["codeHash"], "evidence": evidence["log"], "taskHash": task_hash,
            "status": "pending", "toTry": note,
        }
        revisions.append(review)
        for item in state.get("artifacts", {}).get(task, []):
            if item.get("reviewEvidence") == evidence["log"] and item.get("reviewCall") == review_call:
                item["reviewId"] = review["id"]

    def require_review(self, state, task, review_id):
        revisions = state.get("reviews", {}).get(task, [])
        review = revisions[-1] if revisions else None
        if not review or review["id"] != review_id or review["status"] != "pending":
            raise ValueError("This review is no longer pending. Open the latest review for the task.")
        path = self.safe_path(f"tasks/{task}.md", extensions=False)
        if not path.is_file() or digest(path) != review["taskHash"]:
            raise ValueError("The task changed after this review was queued. Open the latest review.")
        return review

    def review_build_matches(self, review):
        if self.code_fingerprint() != review["codeHash"]:
            return False
        return not git(self.root, "diff", "--name-only", review["build"], "HEAD", "--", ".",
                       ":(exclude)tasks", ":(exclude)CURRENT_TASK.md", ":(exclude)REVIEW.md",
                       ":(exclude)PLAYTEST_QUEUE.md")

    def safe_path(self, value, extensions=True):
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts or "\\" in str(value) or ":" in str(value):
            raise ValueError("Use a repository-relative path")
        result = (self.root / relative).resolve()
        result.relative_to(self.root)
        if any(part.startswith(".") and part != ".relay" for part in relative.parts):
            raise ValueError("Hidden files cannot be attached")
        if extensions and (result.suffix.lower() not in ARTIFACT_EXTENSIONS or re.search(r"secret|password|credential|token", result.name, re.I)):
            raise ValueError("Unsupported or sensitive artifact")
        return result

    @synchronized
    def add_artifact(self, task, value, note="", *, media=None, build="", revision=""):
        if not re.fullmatch(r"T\d+[a-z]?", task):
            raise ValueError("Invalid task ID")
        if not (self.root / "tasks" / (task + ".md")).is_file():
            raise ValueError("Task does not exist")
        path = self.safe_path(value)
        if not path.is_file():
            raise ValueError("Artifact does not exist")
        state = self.state()
        head = git(self.root, "rev-parse", "HEAD")
        item = {"path": path.relative_to(self.root).as_posix(), "sha256": digest(path), "note": note[:300], "head": head}
        if media is not None: item["media"] = media
        items = state.setdefault("artifacts", {}).setdefault(task, [])
        if build or revision:
            if revision:
                review = self.require_review(state, task, revision)
                if media and media["source"] == "screen-recording" and (
                    media.get("projectCommitAtStart") != head or media.get("taskHashAtStart") != review["taskHash"]
                    or not self.review_build_matches(review)
                ):
                    raise ValueError("The build changed during recording. Capture the exact review build again.")
            else:
                evidence = state.get("evidence", {})
                if (state.get("phase") not in ("reviewing", "notion-review-in-flight") or state.get("task") != task or evidence.get("head") != head
                        or evidence.get("codeHash") != self.code_fingerprint()):
                    raise ValueError("Attach footage during review of the matching tested build.")
                if not any(call["id"] == state.get("activeCall") and call["role"] == "reviewer"
                           and call.get("outcome") == "running" for call in state["calls"]):
                    raise ValueError("Attach footage while the reviewer is running.")
                review = {"build": head, "evidence": evidence["log"]}
                item["reviewCall"] = state["activeCall"]
            if build != review["build"]:
                raise ValueError("The attachment build does not match this review.")
            if not media:
                archive = self.safe_path(f".relay/review-artifacts/{task}/{uuid.uuid4().hex}/{path.name}")
                archive.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, archive)
                item["path"] = archive.relative_to(self.root).as_posix()
                item["sha256"] = digest(archive)
            if any(a["sha256"] == item["sha256"] and a.get("reviewId")
                   and a["reviewId"] != revision and a["reviewEvidence"] != review["evidence"] for a in items):
                if not media:
                    archive.unlink()
                    archive.parent.rmdir()
                raise ValueError("This file was already used for a different review. Attach fresh footage.")
            item.update(head=build, reviewEvidence=review["evidence"])
            if revision:
                item["reviewId"] = revision
        items[:] = [x for x in items if x["path"] != item["path"]]
        items.append(item)
        if media is not None:
            event = self.event(state, "review_clip", "Workflow clip attached; approval remains separate")
            event["task"] = task
        self.save(state)
        return item

    @synchronized
    def emit(self, kind, message):
        state = self.state()
        self.event(state, kind, message)
        self.save(state)

    @synchronized
    def approve_retry(self):
        state = self.state()
        if state.get("phase") == "notion-review-in-flight":
            raise ValueError("Reconcile the Notion board and local task before retrying this review")
        self.restore_review(state)
        current = self.root / "CURRENT_TASK.md"
        if not current.is_file(): raise ValueError("There is no saved task to retry")
        state.update(phase="idle", head=git(self.root, "rev-parse", "HEAD"), taskHash=digest(current),
                     retryApproved=True, budgetStartedAt=time.time())
        self.interrupt_call(state)
        state.pop("evidence", None)
        state.pop("reviewBackup", None)
        state.pop("finishedBuild", None)
        self.event(state, "recovered", "Person approved retrying this task with existing edits preserved")
        self.save(state)

    def summary(self):
        state = self.state()
        for call in state.get("calls", []):
            if call.get("outcome") == "running":
                live = self.safe_path(call["log"], extensions=False)
                last_output = live.stat().st_mtime if live.exists() else call["startedAt"]
                call["idleSeconds"] = max(0, time.time() - last_output)
                call["idleWarning"] = call["idleSeconds"] > self.limits()["idleWarningSeconds"]
        state["limits"] = self.limits()
        state["calls"] = state.get("calls", [])[-100:]
        return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--build", default="")
    parser.add_argument("action", choices=["eligible", "begin", "finish", "checkpoint", "resume", "guard", "restore", "summary", "event", "claim", "artifact",
                                          "board-ready", "board-resume", "board-check", "board-reconcile"])
    parser.add_argument("args", nargs="*")
    opts = parser.parse_args()
    runtime = Runtime(opts.root)
    args = opts.args
    try:
        if opts.action == "eligible":
            ok, reason = runtime.eligible(*args)
            print(reason)
            return 0 if ok else 1
        if opts.action == "begin": print(runtime.begin(*args))
        elif opts.action == "claim": runtime.claim(int(args[0]))
        elif opts.action == "artifact": print(json.dumps(runtime.add_artifact(*args, build=opts.build)))
        elif opts.action == "finish": runtime.finish(args[0], int(args[1]), args[2])
        elif opts.action == "checkpoint": runtime.checkpoint(args[0], args[1], args[2], int(args[3]) if len(args) > 3 else None)
        elif opts.action == "resume": print(runtime.resume(args[0]))
        elif opts.action == "board-ready": runtime.board_ready(args[0], args[1], int(args[2]), int(args[3]) if len(args) == 4 else None)
        elif opts.action == "board-resume": print(runtime.board_resume(args[0]))
        elif opts.action == "board-check": runtime.board_check(args[0])
        elif opts.action == "board-reconcile": runtime.board_reconcile(args[0])
        elif opts.action == "guard": runtime.guard_review(args[0])
        elif opts.action == "restore": runtime.restore_review()
        elif opts.action == "summary": print(json.dumps(runtime.summary()))
        elif opts.action == "event":
            runtime.emit(args[0], args[1])
        return 0
    except (ValueError, OSError, KeyError, StopIteration) as exc:
        print("Relay runtime: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
