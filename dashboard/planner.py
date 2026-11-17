"""New-project planning: from an idea (and maybe a tech stack) to a proposal the person approves, then to a plan and task files.

Two AI phases, each a background job, with the person in between:
  1. propose  -> the AI writes PROPOSAL.md only (what it understood, questions, recommended tech stack with reasons when the person has none,
                 how it will be tested, milestones, risks). Nothing else, no code, no commands.
  2. plan     -> after the person approves (and adds notes) the AI writes PLAN.md, AGENTS.md, tasks/T00.md ... and relay.project.json.
The app (not the AI) creates the folder, runs git, and commits the plan files. The planner AI never gets a shell."""
import json, os, pathlib, shutil, subprocess, threading, time, uuid

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
JOBS = {}
PLAN_FILES = ("PLAN.md", "AGENTS.md", "relay.project.json")

PROPOSE = """You are a friendly senior engineer and tech lead helping someone who may not be technical start a software project.

Their idea, in their own words:
<<<
{idea}
>>>

Tech stack: {stack_line}

Your job right now is ONLY to write one file in the project folder {folder}: PROPOSAL.md (path {folder}/PROPOSAL.md). Do not write code, do not create any other file, do not run commands.
Write PROPOSAL.md in plain, non-technical language (explain any technical word the first time), with exactly these sections:

## 1. What I understood
3 to 6 sentences.

## 2. Questions I would ask
At most 5. After each question write the assumption you are making so work can continue without an answer.

## 3. Recommended tech stack
{stack_task}
Prefer free, popular, well-documented tools that AI coding agents handle well and that can be tested automatically from the command line.

## 4. How we will know it works
The automatic test command(s) for this stack, and what they will check.

## 5. Milestones
4 to 8 milestones, one sentence each. Each one must be something the person can see or try.

## 6. Risks
At most 4, in plain words.

Keep the whole file under 120 lines. Write it with the file-writing tool to PROPOSAL.md."""

STACK_SUGGEST = ("The person does not have a tech stack. Pick ONE recommended stack for this idea and explain why in plain words: what it is, why it fits, "
                 "whether it is free or paid, and how hard it is to maintain. Then give 2 alternatives with one line each on when you would choose them instead.")
STACK_GIVEN = ("The person named this stack. Use it. Confirm that it fits the idea in a sentence or two; if you see a serious problem, say so plainly and offer "
               "one alternative, but still plan around their choice.")

PLAN = """You are a tech lead turning an approved proposal into a plan that coding agents will carry out one task at a time.

Read {folder}/PROPOSAL.md. The person approved it with these notes (they override the proposal where they differ):
<<<
{notes}
>>>

Write these files in the project folder {folder} (use paths inside it), and nothing else. Do not write application code and do not run commands.

1. PLAN.md - the final decision in plain language: the idea, the tech stack chosen and why, the milestones in order (each with what the person will be able to see or try).

2. AGENTS.md - binding rules for the coding agents: the stack, the folder layout to use, the exact command that runs ALL automatic tests, code style in a few lines, and these fixed rules:
   never push, merge or delete branches; work only on the current branch; never mark a task finished unless its test command passed in that run;
   never tick a gate that needs the person's judgement (a gate whose text says HUMAN); commit only files you changed on purpose.

3. tasks/T00.md - set up the project skeleton and the test command so that one command runs all automatic tests and passes on an empty project.
   Then tasks/T01.md, T02.md, ... one per milestone, in order (4 to 8 tasks). Every task file has exactly this shape:

   # T01 - <short title>

   Status: Not started | Completion: 0% (0/N gates)

   Recommended AI: <provider> <model>, <effort> reasoning

   - [ ] AUTO: <one checkable thing, with the exact command that proves it>
   - [ ] AUTO: <another, if needed>
   - [ ] HUMAN: <the person tries something specific and says whether it is right>

   ## Objective
   <2-4 plain sentences>

   ## Notes for the builder
   <files to create, constraints, anything the agent should know>

   Every task has at least one AUTO gate and exactly one final HUMAN gate. N is the number of gates.

   RECOMMENDED AI: choose, for every task, which AI should build it and how much it should think, ONLY from this list of what is installed and signed in on this computer:
{available}
   The reviewer is {reviewer}: never recommend that exact model for building (a different model must check the work).
   Rule of thumb: a small, well-defined task (scaffolding, plain pages, simple tests, docs) gets a cheaper model or a free one with low or medium reasoning;
   anything with real logic, data, security, money, concurrency or a tricky bug gets the strongest available model with high reasoning. Vary it across the tasks
   instead of giving every task the same choice. Write it exactly as: Recommended AI: <provider> <model>, <low|medium|high|xhigh|max> reasoning
   (for example: Recommended AI: codex gpt-6.1-sol, high reasoning). Use the provider name exactly as listed (claude, codex, copilot, opencode, or a custom name).

4. relay.project.json - JSON with: name, idea (one sentence), stack (list of strings), testCommand (the one command that runs all automatic tests), branch ("agent-run"),
   and requires: the list of programs that must be installed on the computer to build and test this project, chosen ONLY from:
   node, python, java, maven, dotnet, go, rust, php, ruby, docker, godot. Include every one the stack needs (for example a Next.js site: ["node"]).

Use the file-writing tool for each file."""


def _provider_cmd(provider, found, model, folder, effort=""):
    """Command and extra environment for a planner AI that may write files but has no shell."""
    env = {}
    if provider == "claude":
        cmd = [found, "-p", "--model", model or "claude-sonnet-5-5", "--permission-mode", "acceptEdits"] + (["--effort", effort] if effort else []) + ["--allowedTools", "Write", "Edit", "Read", "Glob"]
    elif provider == "opencode":
        cmd = [found, "run", "--auto", "--dir", str(folder), "-m", model or "opencode/deepseek-v4.1-flash-free"]
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"permission": {"bash": "deny"}})
    elif provider == "codex":
        cmd = [found, "exec", "--sandbox", "workspace-write"] + (["-m", model] if model else []) + (["-c", "model_reasoning_effort=" + effort] if effort else []) + ["-"]
    else:
        return None, env
    if os.name == "nt" and found.lower().endswith((".cmd", ".bat")): cmd = ["cmd.exe", "/c"] + cmd
    return cmd, env


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, creationflags=NO_WINDOW)


def start_job(phase, path, found, provider, model, idea="", stack="", suggest=True, notes="", effort="", available="", reviewer="claude-sonnet-5-5"):
    p = pathlib.Path(path)
    if not (p / ".git").exists(): return {"ok": False, "error": "That project folder is not set up. Create the project first."}
    if not found: return {"ok": False, "error": f"{provider} is not installed."}
    cmd, env = _provider_cmd(provider, found, model, p, effort)
    if not cmd: return {"ok": False, "error": "unknown planner " + str(provider)}
    if phase == "propose":
        if not idea.strip(): return {"ok": False, "error": "Describe your idea first."}
        stack_line = "(none yet: suggest one)" if suggest or not stack.strip() else stack.strip()
        prompt = PROPOSE.format(idea=idea.strip(), stack_line=stack_line, stack_task=STACK_SUGGEST if (suggest or not stack.strip()) else STACK_GIVEN, folder=str(p).replace(chr(92), "/"))
        expect = ["PROPOSAL.md"]
    elif phase == "plan":
        if not (p / "PROPOSAL.md").exists(): return {"ok": False, "error": "There is no proposal yet."}
        prompt = PLAN.format(notes=notes.strip() or "(no changes: the proposal is approved as written)", folder=str(p).replace(chr(92), "/"), available=available or "   (only the AI doing the planning is known: recommend it)", reviewer=reviewer)
        expect = list(PLAN_FILES)
    else:
        return {"ok": False, "error": "unknown phase"}
    jid = uuid.uuid4().hex[:8]
    JOBS[jid] = {"state": "running", "phase": phase, "path": str(p), "started": time.time(), "log": "", "provider": provider, "missing": []}

    def work():
        try:
            e = dict(os.environ); e.update(env)
            r = subprocess.run(cmd, input=prompt, text=True, encoding="utf-8", capture_output=True, cwd=str(p), env=e, timeout=1500, creationflags=NO_WINDOW)
            out = ((r.stdout or "") + (r.stderr or ""))[-1500:]
            missing = [f for f in expect if not (p / f).exists()]
            if phase == "plan" and not list((p / "tasks").glob("T*.md")): missing.append("tasks/")
            JOBS[jid].update(state="done" if not missing and r.returncode == 0 else "failed", log=out, missing=missing, seconds=int(time.time() - JOBS[jid]["started"]))
        except subprocess.TimeoutExpired:
            JOBS[jid].update(state="failed", log="The planner took longer than 25 minutes and was stopped.", missing=expect)
        except Exception as ex:
            JOBS[jid].update(state="failed", log=str(ex), missing=expect)
    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "job": jid}


def job_status(jid):
    j = JOBS.get(jid)
    if not j: return {"ok": False, "error": "unknown job"}
    out = dict(j); out["ok"] = True; out["elapsed"] = int(time.time() - j["started"])
    p = pathlib.Path(j["path"])
    out["proposal"] = (p / "PROPOSAL.md").read_text(encoding="utf-8", errors="replace") if (p / "PROPOSAL.md").exists() else ""
    if j["phase"] == "plan":
        out["tasks"] = sorted(x.name for x in (p / "tasks").glob("T*.md")) if (p / "tasks").exists() else []
        out["plan"] = (p / "PLAN.md").read_text(encoding="utf-8", errors="replace") if (p / "PLAN.md").exists() else ""
    return out


def commit_plan(path):
    """Commit the plan files (and only those) on the working branch."""
    p = pathlib.Path(path)
    if not (p / ".git").exists(): return {"ok": False, "error": "not a git project"}
    files = [f for f in ("PROPOSAL.md", "PLAN.md", "AGENTS.md", "relay.project.json") if (p / f).exists()]
    if (p / "tasks").exists(): files.append("tasks")
    if not files: return {"ok": False, "error": "no plan files to commit"}
    _git(p, "add", "--", *files)
    ident = [] if _git(p, "config", "user.email").stdout.strip() else ["-c", "user.name=Agent Relay", "-c", "user.email=agent-relay@localhost"]
    c = subprocess.run(["git", "-C", str(p), *ident, "commit", "-q", "-m", "Add the project plan and tasks"], capture_output=True, text=True, creationflags=NO_WINDOW)
    return {"ok": c.returncode == 0, "files": files, "error": "" if c.returncode == 0 else (c.stderr or c.stdout)[-200:]}
