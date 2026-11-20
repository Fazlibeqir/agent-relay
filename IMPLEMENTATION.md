# Relay reliability work

This branch includes the previous quota-fallback fixes. The installed app and its active worker remain on that checkpoint while this batch is built and tested.

Acceptance for this batch:

- [x] Route by declared role/task capabilities; unknown connectors never count as available.
- [x] Persist task phases and resume only when the commit, command and evidence still match.
- [x] Create separate worktrees without moving or cleaning the original checkout.
- [x] Record requested and reported models; block known model reuse across providers.
- [x] Require current test evidence before accepting automatic gate changes; preserve human gates.
- [x] Bound calls and repeated failures; surface inactive output without calling it a proven hang.
- [x] Show task/provider timing, outcomes, changes and routing reasons.
- [x] Expose call/time limits and provider preferences.
- [x] Attach safe local artifacts to the existing human review queue.
- [x] Show meaningful event notifications, with optional desktop notifications enabled by the user.

No task's human signoff will be inferred from automated checks. Linux behavior remains unverified until tested there.

Validation (Windows, 18 November 2026): 45 automated tests passed, including a complete mocked build/commit/test/review cycle, restart directly into review without a second build, changed-task rejection, and detached-worker identity checks. Bash syntax, Python compilation and JavaScript syntax checks passed. Browser QA covered persisted settings, invalid-budget rejection, artifact attachment without signoff, worktree creation, explicit retry, and a single notification across repeated polls. Desktop notification delivery was mocked; actual OS permission/delivery remains a user opt-in. Desktop and phone widths had no page overflow. The packaged Windows engine passed an isolated startup/API smoke test.

This is a first implementation of the ten items, not an automatic parallel scheduler or a claim that opaque provider model identities can be verified. Capabilities require verified declarations. The active custom Project X/Notion worker was not migrated or restarted; the new recovery path is implemented and tested for the file-based runner. See README for the migration boundary.

## Workflow review follow-up

- [x] Explain the default local-task workflow and make Notion's optional/custom status visible.
- [x] Add explicit app/window capture, bounded recording and a no-audio capture path.
- [x] Accept existing WebM/MP4 uploads when capture is unavailable or a test harness already produced a clip.
- [x] Play clips on review cards, support playback ranges, and retain commit/checksum context.
- [x] Retain failed saves for retry/download and keep HUMAN gates open.
- [ ] Native OS screen-picker acceptance by a person on each supported desktop host.

The browser test captures a synthetic canvas through the real MediaRecorder, verifies decoded playback, uploads an existing clip, retries a simulated save failure, and handles a cancelled permission request. No real desktop/microphone footage or Notion connection is used by that test.

Current follow-up validation: 57 automated tests passed on Windows. The additional media tests cover interrupted uploads, file collisions, wrong-project context, container/size validation, commit and task changes during capture, local-only upload requests, playback ranges, and keeping HUMAN gates open. Browser playback decoded a 640×360 WebM to completion, and both desktop and phone layouts fit without page overflow. The real native OS sharing picker remains a deferred human check.

Delivery: this branch contains the ten-item baseline and workflow recording/upload/playback. The Windows review ZIP is under `dist/`. This work did not replace the installed app or restart the active Project X worker. No commit or push has been made for this batch.

## Desktop and Project X acceptance

- [x] The Windows WebView2 review page opened the OS share picker and handled cancellation.
- [ ] Capture a selected window through the OS picker. Automated playback uses a synthetic capture source.
- [x] Windows emitted the native notification-shown event for both a test notification and a task event. A repeated delivery request left the shown timestamp unchanged.
- [x] Migrated the stopped local Project X Notion runner. The actual script completed dispatch, build, evidence checkpoint and review with local mock providers. Restart tests reused the evidence without rebuilding or retesting and prevented replay of an in-flight review.
- [ ] Run a real Notion review on an eligible live task. Current automated evidence covers connector routing, resume from evidence, and prevention of duplicate review after a potential board write.

The previous Project X run halted at dispatch. The new runner remains stopped. Its next dispatch requires a provider with verified access to both Notion and the local checkout. The Start preflight allows Relay's known unstaged ignore-rule additions and still flags unrelated edits or removals. All 63 tests passed on Windows.

## Local project setup

- [x] Create a local project and save a task before connecting an AI tool.
- [x] Keep written tasks when approving an AI proposal that adds more tasks.
- [x] In the browser, start the real Relay worker with mock AI tools and see the tested task enter the human review queue.
- [x] Switch projects and reopen the same folder with its saved tasks and review queue.

All 66 automated tests passed on Windows. The packaged executable also served the local workspace and its task list. The browser test used mock AI tools and made no Notion requests.

## Independent model review on Windows

- [x] Record Codex startup model IDs and reported reroutes alongside Claude startup metadata.
- [x] Reject matching models and unidentified Auto sessions for local project review. Restore task and review notes before trying another reviewer.
- [x] Verify the fallback in Windows Git Bash and verify that the packaged executable installs the updated runner in a new workspace.

All 72 automated tests passed on Windows. The provider calls in the tests were mocked. Existing project folders keep their bundled scripts until explicitly updated. Backend model routing can only be checked against model IDs the CLI reports or the user configures.

## Updating a project's Relay scripts

- [x] Show **Update Relay** when a standard project's runner differs from the bundled scripts.
- [x] Save a scoped local commit for scripts that can be traced to a previous Relay install or update. Preserve custom script edits and unrelated staged or unsaved files.
- [x] Require a stopped worker and an idle checkpoint. Restore the original Relay files after a failed save, or recover a saved interruption on the next update attempt.
- [x] Use the packaged Windows app to update a disposable project created with the earlier runner. Verify the success state and both versions of an unrelated user file.

Project X's custom Notion runner uses its own update process.

## Resuming a saved build

- [x] Resume the same local task when its branch, commit, task, test command, and file fingerprints match.
- [x] Continue directly to saving and testing a completed builder result when the saved work and commit request still match.
- [x] Keep edits in place when recovery is blocked. Use the existing explicit retry for changed or older checkpoints.
- [x] Verify both outcomes through **Start building** in the packaged Windows app. The resumed build used mock providers, ran the test command once, and reached the human review queue without another builder call.

## AI project access

- [x] Verify a read and a write in a disposable Git folder for Codex and Claude Code. Save the result for the project and launcher.
- [x] Skip failed checks in agent routing. Let the user require a passing check for every assignment.
- [x] Verify successful and failed checks, fallback selection, and persistence in the Windows app using mock launchers.

All 86 Windows tests passed. The current checks cover local file access for Codex and Claude Code. Live provider accounts, other CLI tools, and connector permissions were outside this test.

## Numbered human reviews

- [x] Queue a numbered revision from a committed build and passing test evidence in the standard task-file workflow.
- [x] Keep each revision's feedback and archived attachments. Prevent an earlier revision or an unversioned request from approving a pending review.
- [x] Show **Playthrough needed** with a reviewer's instructions when no screenshot or clip is attached.
- [x] Verify the reject, rebuild and approve flow in the packaged Windows app using a disposable camera project with simulated test evidence.

Relay records the tested Git commit and attachment checksum. The reviewer or person recording a clip confirms which build was running in the captured window. Custom board workflows still use their own sign-off integration.

## Backup checks and custom-board review bridge

- [x] Check OpenCode and GitHub Copilot in a temporary project and allow passing launchers to qualify as checked backups.
- [x] Publish a numbered review from accepted custom-board evidence and a fresh playthrough entry.
- [x] Carry the reviewed build and revision into the custom board's owner mailbox.

All 90 Windows tests passed. The packaged Windows app verified both backup checks with mock launchers and recorded an approval for the exact build in a simulated custom-board project. The live Project X Notion runner still needs to adopt the updated helper.

## Fedora desktop launcher

The source launcher opens the existing Relay interface in a GTK window on Wayland. Fedora 44 loaded the dashboard and read 178 tasks from the Project X checkout. All 90 Relay regression tests passed on Fedora. A launch without a reachable Wayland display returned exit code 1, and explicit engine-only mode responded to its quit endpoint and exited successfully.

The Fedora checks cover source startup and the local dashboard. Packaged Linux builds, interaction with the native pickers and screen-sharing portal, signed-in providers, and a live Notion review still need verification. The Project X checkout used for this check has no Relay runner configured.
