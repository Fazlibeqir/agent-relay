# Agent Relay

Notion is optional. Standard projects keep their tasks in `tasks/*.md` and use Relay's built-in review queue. No Notion account or connector is needed for that workflow. Project X's Notion board is a custom integration, rather than a dependency of Agent Relay.

Choose **Create project**, give it a name, then select **Add a task** and describe the change in your own words. You can save tasks before connecting an AI. When you’re ready to build, select **Plan with AI**, read the proposal, and approve the plan. **Start building** becomes available after the plan is ready. Building uses two AI tools so a separate tool can review the work.

Open **Review work** to see what to try, watch any attached clips, and approve the result or ask for changes. Use **Open a project folder** to return to a project Relay already knows. Model choices and other setup options stay under **Advanced setup** or **AIs & settings**.

When the app includes newer Relay scripts, an existing standard project's dashboard shows **Update Relay**. Stop Relay for that project, then click the button. The app checks which scripts it originally installed and saves the update as a local commit. Customized scripts and tasks stay in place. Projects with a custom runner, such as the Project X Notion integration, are updated from their own project.

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

## The dashboard tabs

- **Overview**: what is happening now with countdowns, each AI's activity and usage, the **next five tasks** in line (with the AI chosen for each and its progress), what is
  waiting for you, what is done and what broke.
- **Tasks**: every task with its status (not started, in progress, building now, needs your review, done), percent and gate count, filters and search, and the AI, model and
  reasoning level for each. For projects run from task files, a task that needs your review has **Looks good** (ticks your own check and finishes it) and **Needs changes**
  (your note becomes a new automatic check, the task is reopened and built next). **Do next** moves any open task to the front. A project that keeps its live status in its own
  board (like the game relay) shows state and percent from its task files without these buttons.
- **AIs & settings**: roles, backups, permission handling (you or an AI judge), idle chores, tools and sign-ins, and adding other AIs.

## Projects you already have

Step 2 also takes an existing project. Pick its folder and the app shows what it found (stack, test command, branch, unsaved changes). Then:
- **The tasks are already there** (`tasks/T*.md`): open them in the dashboard. On a Git work branch, **Configure local runner** can reuse tasks with explicit `AUTO:` and `HUMAN:` gates and a supplied test command. Projects with a custom task board use their own runner.
- **I have a task list** (a file, or pasted text, any format): the AI turns it into proper tasks, faithfully and in your order.
- **I don't have tasks yet**: say what you want done; the AI reads the project, proposes milestones for you to approve, and writes the tasks.

The relay works on its own `agent-run` branch (created for you; your `main` is never touched) and adds its scripts there. Anything a planning AI changes outside the plan files
is put back (and anything it cannot safely put back is reported). A folder that is not a git project can be made into one on request.

For proposals and plans, choose any ready Claude Code, Codex, OpenCode or GitHub Copilot tool and its available model. A task's recommendation is optional and can be edited under **Tasks** → **Show AI assignments**.

## When an AI asks for permission

An AI that needs something outside its rules (pushing, uploading, installing programs system-wide, using a paid or outside service, handling secrets) is told to file a request
instead of doing it. The relay also holds back any file that looks like a secret (`.env`, keys, tokens, `secrets.json`, `.aws/`...) when a builder lists it for a commit.
The page shows a card for each request with plain-language risk and **Accept / Deny** buttons (and an optional note for the AI). The relay waits for the answer (up to 15 minutes by
default, then carries on without it), and then acts: an accepted secret file is committed, a denied one is added to `.gitignore`, an accepted push pushes the work branch (never `main`, never forced),
and the builder is told every decision in its next prompt.

**Auto mode** (Settings): an AI judge answers instead of you, choosing approve, deny, or "ask" when only you can know. Secret-looking files and uploads still wait for you unless you also
tick the box that hands those over. Everything the judge decides is listed under "Recent permission decisions".

This is a safety net, not a sandbox: the AIs run with your permissions and could in principle ignore the rules. Do not keep secrets in a folder you point an AI at if you cannot afford a leak.

## Download

Ready-made builds for Windows and Linux are on the [Releases page](../../releases). Agent Relay is a desktop app with its own window and a loading screen.

- **Windows 10/11:** `AgentRelay-windows-x64.zip`. Extract it and double-click `AgentRelay.exe` (the window uses WebView2, which is part of Windows). Windows marks downloaded files as blocked; the app clears that mark on its own folder at first start. If it still opens in your browser, right-click the zip, choose Properties, tick Unblock, and extract again.
- **Linux (x86_64):** `AgentRelay-linux-x86_64.tar.gz`. Extract it and run `./AgentRelay/AgentRelay`; it needs libnss3, libxkbcommon-x11-0, libxcb-cursor0, libasound2 and libgl1.

They are built by a GitHub Actions workflow (`.github/workflows/release.yml`) that starts each build to check it comes up before publishing. To make a release, push a tag such as `v0.2.0`.

## Run it

| | Windows | Linux |
|---|---|---|
| From source | `Run AgentRelay.cmd` | `./run-app.sh` |
| Packaged app | `build-app.ps1` makes `dist\AgentRelay\AgentRelay.exe` and a zip | `./build-app.sh` makes `dist/AgentRelay/` (run it on a Linux machine) |

The Linux source launcher uses a GTK desktop window with an embedded WebKitGTK view and native file and folder pickers. On Fedora, install the desktop libraries if needed:

```bash
sudo dnf install python3-gobject gtk4 webkitgtk6.0
bash ./run-app.sh
```

The packaged Linux release uses the bundled Qt desktop window. Linux launches require a working desktop window and report failures in the terminal. For engine-only development, set `AGENT_RELAY_NO_WINDOW=1`. The engine listens on localhost by default. **Quit app** closes the app. A running Relay worker keeps running.

**Status of platforms.** Windows is built and tested. See [Implementation notes](IMPLEMENTATION.md) for the Fedora desktop checks. macOS is unsupported.

## What it never does

Push, merge, delete branches or force anything; tick a gate that needs your judgement; spend money; sign in for you (it only opens each tool's own sign-in);
change a file you already have (setup never overwrites); accept commands from another computer (everything that changes something is localhost only).

## Layout

```
agent_relay_app.py   starts the local engine and shows the interface in a native window with a splash screen
dashboard/           server.py (API), index.html (the whole UI), appapi.py (actions), osops.py (Windows/Linux differences),
                     planner.py (idea -> proposal -> plan), customai.py (add other AIs), providers.json (the cards)
template/            what gets copied into every new project: relay-files.sh (the loop), relay-lib.sh, relay-pick.py (task picker),
                     relay-settings.py (roles and models), relay-idle.sh, relay-digest.py, relay-awake.ps1 (Windows launcher)
assets/              the logo, icons and splash (tools/make_icons.py draws them), and the Flux credit logo
relay.settings.example.json   the default roles for a new project
```

## Settings

`relay.settings.json` in each project (or the Settings card) sets provider, model and reasoning for dispatcher, builder, reviewer and cleaner (`none` = skipped), plus backups
and a per-provider daily cap shared by backup calls across all roles. `fallbackRoles.dispatcher` and `fallbackRoles.reviewer` set ordered backup providers independently of `fallbackBuilders`.
When a tool reports a usage limit, Relay records its cooldown and tries the next installed backup before waiting. Reviewer selection excludes models used by builders in the current round; an unknown (`auto`) model excludes that provider. If no independent reviewer can run, the task stays pending.
The dashboard shows the active provider and the next retry time separately from the provider's actual quota reset. Built-in tools can fill any role; custom providers still require `"experimental": true`.
The builder model `auto` means: follow each task's `Recommended AI:` line. A missing or invalid file falls back to the built-in defaults and says so in the log.

These defaults apply to newly created projects. Existing projects keep their local relay scripts; update their `relay-lib.sh`, `relay-settings.py`, and `relay-limits.py` from `template/` while the relay is stopped. Projects with an embedded library in `relay.sh` need that embedded section updated as well. Restart the relay to load script changes. Settings alone take effect on the next call.

Run regression tests with `python -m unittest discover -s tests -v` (requires Bash / Git Bash). Provider commands are mocked; the tests do not spend AI usage or change a project.

## Routing and recovery

The file-based runner persists task phases, provider calls and evidence under `.relay/` (ignored by git). On **Start building**, it resumes the same task when the saved branch, commit, task, test command, and project files match. If the builder had already finished, Relay saves the recorded changes and runs tests without calling the builder again. A completed test run resumes directly into review when its evidence still matches.

If a build stopped after edits that do not match a saved checkpoint, inspect the files and use **Approve task retry** under **Technical details** to continue from them. This also applies to older checkpoints that lack file fingerprints. Retrying by approval resets the task budget and keeps the daily provider limits. A stale lock with a verified dead owner can be released; an unknown lock or reused live PID stays blocked for inspection.

Declare each provider's verified capabilities under `capabilities` in settings. Only `repository` and `shell` are supplied for built-ins. For a connector-dependent task add, for example, `REQUIRES: notion` to its task file. Project-wide role requirements belong in `relay.project.json`:

```json
{"requiredCapabilities": {"dispatcher": ["repository", "notion"], "builder": ["repository", "shell"], "reviewer": ["repository", "shell", "notion"]}}
```

Capabilities are declarations, not connector installations or sign-ins. Relay never assumes that one AI's connector exists in another. Custom tools start with no declared capabilities.

For standard projects, open **AIs & settings** and choose **Check access** beside Codex, Claude Code, OpenCode or GitHub Copilot. The check makes one AI call in a temporary Git folder inside the project. The tool reads a random value and writes it back to a new file. Relay removes the folder and stores the result for that checkout and launcher. A failed check skips that tool during assignment. Use **Use only AI tools that pass** to require a passing result. Recheck after changing accounts or tool permissions. The check covers file access, and connector permissions remain separate.

Settings also contain call/failure budgets and per-call timeouts (`limits`; see the example file). Calls include primary providers and quota responses. Identical failures stop that provider from repeating the same attempt. No-output warnings report inactivity, not a proven hang; a quiet tool may still be working. The timeout bounds the call. An explicit `AGENT_TIMEOUT` environment value overrides the setting.

**Create isolated workspace** makes a new local branch/worktree from the last commit. It leaves the original branch and unsaved edits in place and starts no worker. Open the resulting folder as an existing project to run another Relay there. Builders and reviewers within one worker stay sequential on that workspace; this is not automatic parallel scheduling or automatic merging.

## Activity, models and review files

Standard task-file projects number each human review per task and display the tested commit on its card. A request for changes preserves that revision's feedback and footage and reopens the task. The next reviewed build receives its own review number. If it has no screenshot or video, the card says **Playthrough needed** and shows what to try. Approval records the revision and commit shown on the card. Older task-file reviews can use **Request a fresh review** to create their first numbered review.

Reviewers can attach footage from the tested build with `python relay-runtime.py --build <tested-commit> artifact <task-id> <file> <note>`. Captured files can go under `.relay/captures/` so they stay outside source changes. Relay saves a copy and a checksum for the review. For manually attached media, the person attaching it confirms which build is shown.

The activity panel shows requested and reported models, timing, exit status, changed files, routing skips and recovery events. Relay reads Claude's startup event and Codex's startup model and reported reroutes. When a tool leaves the reported model blank, a selected model ID can still be used for comparison. Aliases such as **Auto** need a reported model.

For local projects, a reviewer must use a different known model from every builder that may have changed the result. If a reviewer uses a matching or unidentified model, Relay restores the task and review notes and tries the next reviewer. If the builder's model is unidentified, Relay pauses review and explains why in **Technical details**. Configured model IDs and CLI output are the available evidence. Providers may conceal backend routing.

Automatic review requires a successful executed test command, its retained log and matching code/commit. The guard restores deleted or changed HUMAN/OWNER gates and rejects invented completed gates. This does not prove the test suite is sufficient; human checks remain yours.

Attach screenshots, videos, PDFs, logs or ZIPs from **Files to review** on the review page, or run `python relay-runtime.py artifact T01 captures/example.png "What to inspect"`. Paths must stay inside the project, sensitive/hidden/executable paths are refused, and the file is bound to a commit and SHA-256. Changed attachments must be attached again. Browser preview is limited to 50 MB; larger builds stay available at their local path.

### Record a workflow for human review

On a task's review card, **Record workflow** opens the browser's sharing picker. Choose the app/window to show, demonstrate the steps, then press **Stop & attach**. Capture requests no audio, stops at three minutes or near 45 MB, and releases the shared tracks when stopped. The picker is an explicit user action; see the [Screen Capture API permission requirements](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getDisplayMedia#security).

Clips are stored only in the project's `.relay/recordings/` folder and play directly on the review card. Add a note describing the steps, expected result, or issue. Each attachment has a checksum and attachment commit; a screen recording also records the project commit at capture start and flags a task definition that changed during capture. This context does not certify which executable was visible in the chosen window. Uploaded clips have an unknown recording commit and may contain audio.

**Upload an existing clip** accepts WebM/MP4 up to 45 MB, including videos produced by a project's own test workflow. If screen sharing is unavailable in the desktop window or the user cancels it, upload remains available. If saving fails, the browser retains the clip for **Retry save** or **Download unsaved clip**, and warns before leaving with unsaved footage. Playback supports byte ranges for seeking.

Saving or watching a clip never checks a HUMAN gate. Approval still requires **Looks good**, or the custom board's separate signoff. Native screen-picker behavior and desktop-host permissions need platform-specific acceptance; automated browser QA uses a synthetic canvas stream and the real video encoder, rather than recording the user's desktop.

Blocked, finished and review-ready events appear in the app. **Enable desktop notifications** is optional and requests browser permission only after that button is pressed. Unsupported/denied notifications leave the in-app events available. No push service is used.

**Custom Notion runners:** the Project X checkout uses its own `relay.sh` with the shared checkpoint helper. Dispatcher and reviewer providers need a declared `notion` capability. Dispatch reads the board and writes the local task snapshot. During review, a checkpoint binds both test commands and the saved log to the tested commit. If the worker stops before a reviewer runs, Start can continue from that evidence.

Custom runners using the updated `relay-runtime.py` can publish a numbered review through `board-check`. The reviewer must leave a fresh playthrough entry in `PLAYTEST_QUEUE.md` and a local task snapshot with only human checks open. A decision made from its review card records the revision and tested build in the owner mailbox. Human sign-off on Notion is recorded separately.

If the activity panel says the Notion review started and the worker is stopped, compare the task page and local task files first. The provider may have written to Notion before it stopped. After reconciling those records, run `python relay-runtime.py board-reconcile <task-id>` in the Project X Relay checkout. This acknowledges reconciliation locally. It does not edit the board or approve a task. Review can then proceed on fresh evidence.

With the Windows desktop app open, **Enable desktop notifications** sends a native test notification. New review, completion, and blocker events use the same Windows delivery path. The browser dashboard still uses its own notification permission.

## Known limits

- The tests that decide whether a task is done are the ones the planner and the builder write; the relay checks they pass, not that they are good. The HUMAN gate is yours.
- The planner and the builder are AIs and can be wrong. Read the proposal before approving it.
- Custom AIs cannot be fenced off from git, so they are marked experimental.
- GitHub or GitLab repository creation uses the `gh` or `glab` tool and has not been exercised end to end.
- The packaged app is not code-signed, so Windows SmartScreen may warn once.
