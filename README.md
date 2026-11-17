# Agent Relay

A desktop app that turns an idea into a working project by letting several AI tools share the job, one task at a time, while you watch.
It walks you through five steps, and then runs by itself:

1. **Your AI tools.** The app finds Claude Code, Codex, GitHub Copilot and OpenCode on your computer (the command-line tool or the desktop app).
   A missing one gets an Install button that opens a terminal and runs the install; an installed one that is not signed in gets a Sign in button.
   The page shows each tool as not installed, found but not signed in, or found and signed in. Any other AI with a command-line tool (Antigravity,
   Gemini CLI, Aider, ...) can be added by typing its name and command: the app asks the tool for its models and reasoning levels.
2. **Your project.** Describe the idea, pick or type a name, and choose a tech stack (or let the AI suggest one). The app shows your disks with their
   free space and recommends the one with the most room; it creates the folder, makes it a git repository on its own working branch, and asks if you want
   it on GitHub or GitLab (a private repository, nothing is uploaded) or only on this computer.
3. **The plan.** An AI writes a plain-language proposal (what it understood, questions, a recommended tech stack with reasons, milestones, risks). You approve it
   or ask for changes. It then writes the plan, the rules for the coding agents (`AGENTS.md`) and one task file per milestone (`tasks/T00.md`, ...), each with
   checkable AUTO gates and one final HUMAN gate. The app lists the programs the project needs (Node.js, Java, ...) with Install buttons.
4. **Which AI builds what.** For every task the planner recommends an AI and a reasoning level, chosen from the AIs you actually have ready: small tasks on cheaper
   or free models, hard ones on the strongest. The app snaps any invented model name to a real one. You can change any task from a dropdown; if you do not, the plan stands.
5. **Start.** The relay runs the project's test command, then works through the tasks: a script picks the next task, the builder AI builds it, the test command runs,
   and a second, different AI reviews it and ticks the AUTO gates. When only the HUMAN gate is left the task waits for you. A review never ticks a HUMAN gate (the relay
   puts one back if it does), nothing is pushed or merged, and when an AI runs out of usage the relay waits for the reset time the tool reports (or switches to a backup AI).

After Start the same page is the dashboard: what is happening now with countdowns, each AI's activity and usage, what is done, what broke, what is next, the settings
(which AI plays which role, backups, idle chores) and the per-task AI table. Settings take effect from the next AI call, with no restart.

## Run it

| | Windows | Linux |
|---|---|---|
| From source (needs Python 3) | `Run AgentRelay.cmd` | `./run-app.sh` |
| Packaged single file | `build-app.ps1` makes `dist\AgentRelay.exe` | `./build-app.sh` makes `dist/AgentRelay` (run it on a Linux machine) |

The app opens in your browser at `http://127.0.0.1:8765/` (it picks another port if that one is busy). **Quit app** closes the app only; a running relay keeps running.

**Status of platforms.** Windows is built and tested. The Linux code paths (terminals, package installs, drives, processes, starting the relay) are written to the same shape
but have not been run on a Linux machine yet: expect small fixes on first use. macOS is not supported.

## What it never does

Push, merge, delete branches or force anything; tick a gate that needs your judgement; spend money; sign in for you (it only opens each tool's own sign-in);
change a file you already have (setup never overwrites); accept commands from another computer (everything that changes something is localhost only).

## Layout

```
agent_relay_app.py   starts the local server and opens the page
dashboard/           server.py (API), index.html (the whole UI), appapi.py (actions), osops.py (Windows/Linux differences),
                     planner.py (idea -> proposal -> plan), customai.py (add other AIs), providers.json (the cards)
template/            what gets copied into every new project: relay-files.sh (the loop), relay-lib.sh, relay-pick.py (task picker),
                     relay-settings.py (roles and models), relay-idle.sh, relay-digest.py, relay-awake.ps1 (Windows launcher)
examples/project-x/  a bigger, project-specific relay kept as an example (not installed anywhere)
relay.settings.example.json   the default roles for a new project
```

## Settings

`relay.settings.json` in each project (or the Settings card) sets provider, model and reasoning for dispatcher, builder, reviewer and cleaner (`none` = skipped), plus backups
and a per-day cap. The reviewer must be a different model from the builder; only Claude is verified as dispatcher or reviewer (anything else needs `"experimental": true`).
The builder model `auto` means: follow each task's `Recommended AI:` line. A missing or invalid file falls back to the built-in defaults and says so in the log.

## Known limits

- The tests that decide whether a task is done are the ones the planner and the builder write; the relay checks they pass, not that they are good. The HUMAN gate is yours.
- The planner and the builder are AIs and can be wrong. Read the proposal before approving it.
- Custom AIs cannot be fenced off from git, so they are marked experimental.
- GitHub or GitLab repository creation uses the `gh` or `glab` tool and has not been exercised end to end.
- The packaged app is not code-signed, so Windows SmartScreen may warn once.
