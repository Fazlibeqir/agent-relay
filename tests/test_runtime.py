import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
spec = importlib.util.spec_from_file_location("relay_runtime", ROOT / "template/relay-runtime.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = module.Runtime(self.root)
        self.git("init", "-q", "-b", "agent-run")
        self.git("config", "user.name", "Relay Test")
        self.git("config", "user.email", "test@localhost")
        (self.root / "tasks").mkdir()
        self.task = self.root / "tasks/T01.md"
        self.task.write_text("# T01 - Test\n- [ ] AUTO: test suite\n- [ ] HUMAN: inspect it\n", encoding="utf-8")
        (self.root / "CURRENT_TASK.md").write_text("tasks/T01.md\nT01 — Test\n", encoding="utf-8")
        (self.root / "code.py").write_text("value=1\n")
        (self.root / ".gitignore").write_text(".relay/\nrelay-*.log\nrelay.settings.json\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.runtime.checkpoint("building", "T01", "run-tests")

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True)
        if result.returncode: raise AssertionError(result.stderr)
        return result.stdout.strip()

    def config(self, value):
        (self.root / "relay.settings.json").write_text(json.dumps(value), encoding="utf-8")

    def call(self, role="builder", provider="codex", model="gpt-test", output="done", code=0, outcome="success"):
        live = "relay-live-" + provider + ".log"
        cid = self.runtime.begin(role, provider, model, live)
        (self.root / live).write_text(output, encoding="utf-8")
        self.runtime.finish(cid, code, outcome)
        return cid

    def reviewed(self):
        self.runtime.checkpoint("testing", "T01", "run-tests")
        (self.root / "relay-tests.log").write_text("PASS\n")
        self.runtime.checkpoint("reviewing", "T01", "run-tests", 0)

    def test_unknown_connectors_do_not_transfer_between_providers(self):
        (self.root / "CURRENT_TASK.md").write_text("tasks/T01.md\nT01\nREQUIRES: notion\n")
        self.assertEqual(self.runtime.eligible("reviewer", "copilot", "auto"), (False, "missing capabilities: notion"))
        self.config({"capabilities": {"claude": ["repository", "shell", "notion"]}})
        self.assertTrue(self.runtime.eligible("reviewer", "claude", "claude-sonnet-test")[0])
        self.assertFalse(self.runtime.eligible("reviewer", "codex", "gpt-test")[0])

    def test_explicit_model_identity_is_checked_across_providers(self):
        self.call()
        self.assertFalse(self.runtime.eligible("reviewer", "copilot", "openai/gpt-test")[0])
        self.assertTrue(self.runtime.eligible("reviewer", "claude", "claude-sonnet-test")[0])

    def test_auto_builder_excludes_same_provider(self):
        self.call(model="auto")
        self.assertFalse(self.runtime.eligible("reviewer", "codex", "another-model")[0])

    def test_metadata_not_prose_reports_model(self):
        self.call(provider="claude", output='{"type":"system","subtype":"init","model":"reported-model"}\nI used invented-model')
        self.assertEqual(self.runtime.state()["calls"][0]["reportedModel"], "reported-model")

    def test_codex_auto_records_startup_model_and_reroute(self):
        self.call(model="auto", output=(
            "OpenAI Codex v1.0\n--------\nworkdir: C:\\Fixture\nmodel: openai/gpt-test\n"
            "provider: openai\nsession id: fixture\n--------\nuser\na fixture\n"
            "model rerouted: openai/gpt-test -> gpt-other\n"
            "model: invented-by-output\n"
        ))
        self.reviewed()
        self.assertEqual(self.runtime.state()["calls"][0]["reportedModels"], ["openai/gpt-test", "gpt-other"])
        for model in ("gpt-test", "openai/gpt-other-latest"):
            self.assertFalse(self.runtime.eligible("reviewer", "copilot", model)[0])
        self.assertTrue(self.runtime.eligible("reviewer", "claude", "claude-sonnet-test")[0])

    def test_unreported_auto_builder_blocks_cross_provider_review(self):
        self.call(model="OpenAI/AUTO", output='{"type":"system","subtype":"init","model":"invented-model"}')
        self.reviewed()
        self.assertFalse(self.runtime.eligible("reviewer", "claude", "claude-sonnet-test")[0])
        self.assertIsNone(self.runtime.state()["calls"][0]["reportedModel"])

    def test_opaque_quota_call_without_edits_does_not_block_fallback_review(self):
        self.call(model="auto", code=1, outcome="quota")
        self.call(provider="opencode", model="opencode/known-model")
        self.reviewed()
        self.assertTrue(self.runtime.eligible("reviewer", "claude", "claude-sonnet-test")[0])

    def test_opaque_failed_builder_edits_preserve_provenance_requirement(self):
        cid = self.runtime.begin("builder", "codex", "auto", "relay-live-codex.log")
        (self.root / "code.py").write_text("value=2\n")
        self.runtime.finish(cid, 7, "error")
        self.call(provider="opencode", model="known-model")
        self.reviewed()
        self.assertFalse(self.runtime.eligible("reviewer", "claude", "claude-sonnet-test")[0])

    def test_reported_self_review_restores_task_and_fails(self):
        self.call()
        review = self.root / "REVIEW.md"
        review.write_text("Original review notes\n")
        self.reviewed()
        cid = self.runtime.begin("reviewer", "claude", "auto", "relay-live-claude.log")
        self.task.write_text("# T01\n- [x] AUTO: test suite\n- [x] HUMAN: inspect it\n")
        review.write_text("Unverified review notes\n")
        queue = self.root / "PLAYTEST_QUEUE.md"
        queue.write_text("Unverified queue entry\n")
        (self.root / "relay-live-claude.log").write_text('{"type":"system","subtype":"init","model":"gpt-test"}')
        with self.assertRaisesRegex(ValueError, "builder's model"):
            self.runtime.finish(cid, 0, "success")
        self.assertIn("[ ] HUMAN", self.task.read_text())
        self.assertEqual(review.read_text(), "Original review notes\n")
        self.assertFalse(queue.exists())
        self.assertEqual(self.runtime.state()["calls"][-1]["outcome"], "self_review_rejected")

    def test_opaque_auto_reviewer_cannot_accept_automatic_gates(self):
        self.call()
        self.reviewed()
        cid = self.runtime.begin("reviewer", "copilot", "auto", "relay-live-copilot.log")
        self.task.write_text(self.task.read_text().replace("[ ] AUTO", "[x] AUTO"))
        with self.assertRaisesRegex(ValueError, "without a known model"):
            self.runtime.finish(cid, 0, "success")
        self.assertIn("[ ] AUTO", self.task.read_text())
        self.assertEqual(self.runtime.state()["calls"][-1]["outcome"], "review_identity_unknown")
        with self.assertRaisesRegex(ValueError, "no independent model"):
            self.runtime.guard_review("run-tests")
        self.call("reviewer", "claude", "claude-sonnet-test")
        self.runtime.guard_review("run-tests")

    def test_budget_counts_primary_calls_and_quota_calls(self):
        self.config({"limits": {"callsPerTask": 1}})
        self.call(code=1, outcome="quota")
        self.assertIn("task call budget", self.runtime.eligible("builder", "copilot", "auto")[1])

    def test_daily_cap_is_not_reset_by_task_retry(self):
        self.config({"limits": {"callsPerProviderDay": 1}})
        self.call()
        self.runtime.approve_retry()
        self.assertIn("daily call", self.runtime.eligible("builder", "codex", "gpt-test")[1])

    def test_same_failure_switches_provider(self):
        self.call(code=7, outcome="error", output="same error")
        self.call(code=7, outcome="error", output="same error")
        self.assertIn("same failure", self.runtime.eligible("builder", "codex", "gpt-test")[1])
        self.assertTrue(self.runtime.eligible("builder", "copilot", "auto")[0])

    def test_review_survives_restart_without_rebuilding(self):
        self.reviewed()
        self.task.write_text("partial review")
        (self.root / "CURRENT_TASK.md").unlink()
        restarted = module.Runtime(self.root)
        self.assertEqual(restarted.resume("run-tests"), "reviewing")
        self.assertIn("[ ] HUMAN", self.task.read_text())
        self.assertTrue((self.root / "CURRENT_TASK.md").exists())

    def test_different_command_or_commit_cannot_reuse_evidence(self):
        self.reviewed()
        self.assertEqual(self.runtime.resume("different-tests"), "blocked")
        (self.root / "code.py").write_text("value=2\n")
        self.git("add", "code.py")
        self.git("commit", "-qm", "later commit")
        self.assertFalse(self.runtime.evidence_valid(self.runtime.state(), "run-tests"))

    def test_changed_task_context_is_not_overwritten_during_recovery(self):
        self.reviewed()
        current = self.root / "CURRENT_TASK.md"
        current.write_text("tasks/T02.md\nT02 — A different task\n", encoding="utf-8")
        self.assertEqual(self.runtime.resume("run-tests"), "blocked")
        self.assertIn("T02", current.read_text())

    def board_ready(self):
        current = self.root / "CURRENT_TASK.md"
        current.write_text("https://www.notion.so/fixture\nT01 — Test\n", encoding="utf-8")
        self.runtime.checkpoint("testing", "T01 — Test", "headless\ngraphics")
        (self.root / "relay-tests.log").write_text("headless passed\ngraphics passed\n")

    def test_notion_review_recovers_before_provider_runs(self):
        self.board_ready()
        self.assertEqual(self.runtime.board_resume("headless\ngraphics"), "testing")
        with self.assertRaisesRegex(ValueError, "passing tests"):
            self.runtime.board_ready("T01 — Test", "headless\ngraphics", 0, 1)
        self.runtime.board_ready("T01 — Test", "headless\ngraphics", 0, 0)
        self.assertEqual(self.runtime.board_resume("headless\ngraphics"), "notion-review-ready")
        self.assertEqual(self.runtime.board_resume("headless\nchanged graphics"), "blocked")

    def test_interrupted_notion_review_blocks_replay_and_generic_retry(self):
        self.board_ready()
        self.runtime.board_ready("T01 — Test", "headless\ngraphics", 0, 0)
        call = self.runtime.begin("reviewer", "claude", "claude-sonnet-test", "relay-live-claude.log")
        self.runtime.finish(call, 1, "error")
        self.assertFalse(self.runtime.eligible("reviewer", "copilot", "auto")[0])
        self.assertEqual(self.runtime.board_resume("headless\ngraphics"), "blocked")
        with self.assertRaisesRegex(ValueError, "Reconcile"): self.runtime.approve_retry()
        with self.assertRaisesRegex(ValueError, "does not match"): self.runtime.board_reconcile("T02")
        before = (self.root / "CURRENT_TASK.md").read_bytes()
        self.runtime.board_reconcile("T01")
        self.assertEqual((self.root / "CURRENT_TASK.md").read_bytes(), before)
        self.assertEqual(self.runtime.board_resume("headless\ngraphics"), "idle")

    def test_notion_review_completion_validates_test_evidence(self):
        self.board_ready()
        self.runtime.board_ready("T01 — Test", "headless\ngraphics", 0, None)
        call = self.runtime.begin("reviewer", "claude", "claude-sonnet-test", "relay-live-claude.log")
        self.runtime.finish(call, 0, "success")
        self.runtime.board_check("headless\ngraphics")
        (self.root / self.runtime.state()["evidence"]["log"]).write_text("tampered")
        with self.assertRaisesRegex(ValueError, "matching evidence"):
            self.runtime.board_check("headless\ngraphics")

    def test_notion_board_queues_new_numbered_review_and_scopes_owner_mailbox(self):
        import appapi
        import digest

        (self.root / "relay.sh").write_text("# Custom board runner\n")
        self.git("add", "relay.sh")
        self.git("commit", "-qm", "fixture board runner")
        queue = self.root / "PLAYTEST_QUEUE.md"
        queue.write_text("T01 — Camera — Old steps\n", encoding="utf-8")
        cmd = "headless\ngraphics"
        title = "T01 — Test"
        self.board_ready()
        self.runtime.board_ready(title, cmd, 0, 0)
        reviewer = self.runtime.begin("reviewer", "claude", "claude-sonnet-test", "relay-live-claude.log")
        self.task.write_text(self.task.read_text().replace("[ ] AUTO:", "[x] AUTO:"), encoding="utf-8")
        queue.write_text("T01 — Camera — Try the new turn speed\n", encoding="utf-8")
        self.runtime.finish(reviewer, 0, "success")
        self.runtime.board_check(cmd)
        self.runtime.board_check(cmd)
        self.runtime.checkpoint("idle", title, cmd)
        reviews = self.runtime.state()["reviews"]["T01"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["toTry"], "Try the new turn speed")
        self.assertIn("Camera · Review 1", digest.page(self.root))
        self.assertFalse(appapi.decide_board_review(self.root, "T01", "", "approved")["ok"])
        self.assertTrue(appapi.decide_board_review(self.root, "T01", reviews[0]["id"], "approved")["ok"])
        request = appapi.owner_requests(self.root)[0]
        self.assertEqual(request["build"], reviews[0]["build"])
        self.assertIn("[ ] HUMAN", self.task.read_text())

    def test_notion_roles_require_connector_but_builder_does_not(self):
        with patch.dict(os.environ, {"RELAY_NOTION_BOARD": "1"}):
            self.assertFalse(self.runtime.eligible("dispatcher", "codex", "auto")[0])
            self.assertFalse(self.runtime.eligible("reviewer", "copilot", "auto")[0])
            self.assertTrue(self.runtime.eligible("builder", "codex", "auto")[0])
            self.config({"capabilities": {"claude": ["repository", "shell", "notion"]}})
            self.assertTrue(self.runtime.eligible("dispatcher", "claude", "claude-sonnet-test")[0])

    def test_native_notification_delivers_only_registered_events_once(self):
        import server
        import threading
        import urllib.error
        import urllib.request

        self.runtime.emit("needs_review", "The fixture needs review")
        event = self.runtime.state()["events"][-1]
        received = []
        listener = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=listener.serve_forever, daemon=True)

        def send(payload):
            url = f"http://127.0.0.1:{listener.server_port}/api/notifications/show"
            request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                self.assertTrue(json.load(response)["ok"])

        with (patch.object(server.Handler, "relay", self.root),
              patch.object(server.Handler, "on_notify", lambda title, message: received.append((title, message))),
              patch.object(server.Handler, "notification_seen", set())):
            thread.start()
            try:
                send({"id": event["id"]})
                send({"id": event["id"]})
                self.assertEqual(received, [("Agent Relay · T01", "The fixture needs review")])
                with self.assertRaises(urllib.error.HTTPError):
                    send({"id": "unknown"})
            finally:
                listener.shutdown()
                listener.server_close()
                thread.join(timeout=5)

    def test_start_preserves_user_ignore_edits_but_accepts_relay_only_setup(self):
        import appapi
        path = self.root / ".gitignore"
        original = path.read_text()
        path.write_text(original + "\nrelay-builder.identity\n")
        self.assertEqual(appapi.unfinished_work(self.root), [])
        path.write_text(original + "\nrelay-builder.identity\ncustom-project.cache\n")
        self.assertIn((" M", ".gitignore"), appapi.unfinished_work(self.root))
        path.write_text(original.replace(".relay/\n", "") + "relay-builder.identity\n")
        self.assertIn((" M", ".gitignore"), appapi.unfinished_work(self.root))

    def test_uncommitted_code_change_invalidates_evidence(self):
        self.reviewed()
        (self.root / "code.py").write_text("changed after tests\n")
        self.assertFalse(self.runtime.evidence_valid(self.runtime.state(), "run-tests"))

    def test_untracked_code_change_invalidates_evidence(self):
        self.reviewed()
        (self.root / "new_code.py").write_text("not tested")
        self.assertFalse(self.runtime.evidence_valid(self.runtime.state(), "run-tests"))

    def test_mutated_or_missing_test_log_is_rejected(self):
        self.reviewed()
        log = self.root / self.runtime.state()["evidence"]["log"]
        log.write_text("different evidence")
        with self.assertRaisesRegex(ValueError, "evidence"):
            self.runtime.guard_review("run-tests")
        log.unlink()
        self.assertEqual(self.runtime.resume("run-tests"), "blocked")

    def test_test_failure_or_missing_command_cannot_review(self):
        self.runtime.checkpoint("testing", "T01", "run-tests")
        (self.root / "relay-tests.log").write_text("FAIL")
        with self.assertRaises(ValueError): self.runtime.checkpoint("reviewing", "T01", "run-tests", 1)
        with self.assertRaises(ValueError): self.runtime.checkpoint("reviewing", "T01", "", 0)

    def test_human_gates_cannot_be_ticked_renamed_or_deleted(self):
        self.call()
        self.reviewed()
        self.task.write_text("# T01\n- [x] AUTO: test suite\n- [x] HUMAN: a renamed check\n- [x] AUTO: invented check\n")
        self.call("reviewer", "claude", "claude-sonnet-test")
        self.runtime.guard_review("run-tests")
        text = self.task.read_text()
        self.assertIn("[ ] HUMAN: inspect it", text)
        self.assertIn("[ ] HUMAN: a renamed check", text)
        self.assertIn("[ ] AUTO: invented check", text)
        self.assertIn("[x] AUTO: test suite", text)

    def test_deleted_task_is_restored(self):
        self.reviewed()
        self.task.unlink()
        with self.assertRaises(ValueError): self.runtime.guard_review("run-tests")
        self.assertIn("[ ] HUMAN", self.task.read_text())

    def test_interrupted_build_with_matching_files_resumes_same_task(self):
        call = self.runtime.begin("builder", "codex", "auto", "relay-live-codex.log")
        self.assertEqual(self.runtime.resume("run-tests"), "building")
        state = self.runtime.state()
        self.assertEqual(state["task"], "T01")
        self.assertEqual(state["calls"][-1]["id"], call)
        self.assertEqual(state["calls"][-1]["outcome"], "interrupted")
        self.assertFalse(state["calls"][-1]["changedCode"])
        self.assertFalse(module.task_builders(state))

    def test_changed_interrupted_build_requires_explicit_retry(self):
        self.runtime.begin("builder", "codex", "gpt-test", "relay-live-codex.log")
        (self.root / "code.py").write_text("unfinished work")
        self.assertEqual(self.runtime.resume("run-tests"), "blocked")
        self.runtime.approve_retry()
        self.assertEqual(self.runtime.resume("run-tests"), "idle")
        self.assertEqual((self.root / "code.py").read_text(), "unfinished work")

    def test_build_pauses_on_task_command_branch_or_index_change(self):
        state = self.runtime.state()
        self.assertEqual(self.runtime.build_resume_phase(state, "different-tests"), "blocked")
        original = self.task.read_bytes()
        self.task.write_bytes(original + b"\nNew requirement\n")
        self.assertEqual(self.runtime.build_resume_phase(state, "run-tests"), "blocked")
        self.task.write_bytes(original)
        self.git("checkout", "-qb", "other-branch")
        self.assertEqual(self.runtime.build_resume_phase(state, "run-tests"), "blocked")
        self.git("checkout", "-q", "agent-run")
        code = self.root / "code.py"
        original = code.read_bytes()
        code.write_bytes(b"value=3\n")
        self.git("add", "--", "code.py")
        code.write_bytes(original)
        self.assertEqual(self.runtime.build_resume_phase(state, "run-tests"), "blocked")

    def test_finished_build_fingerprint_covers_pending_save_and_untracked_files(self):
        self.call("judge", "claude", "claude-sonnet-test")
        self.assertNotIn("finishedBuild", self.runtime.state())
        cid = self.runtime.begin("builder", "codex", "gpt-test", "relay-live-codex.log")
        pending = self.root / "relay-commit.txt"
        pending.write_text("T01: fixture build\nFILES:\ncode.py\n")
        (self.root / "code.py").write_text("value=2\n")
        self.runtime.finish(cid, 0, "success")
        self.assertEqual(self.runtime.resume("run-tests"), "build-ready")
        untracked = self.root / "relay-user-notes.txt"
        untracked.write_text("A new file while the worker was stopped")
        self.assertEqual(self.runtime.resume("run-tests"), "blocked")
        untracked.unlink()
        pending.write_text("Different save request\nFILES:\ncode.py\n")
        self.assertEqual(self.runtime.resume("run-tests"), "blocked")

    def test_legacy_build_checkpoint_and_board_build_still_need_attention(self):
        self.assertEqual(self.runtime.board_resume("run-tests"), "blocked")
        state = self.runtime.state()
        state.pop("buildContext")
        state.pop("buildFiles")
        self.runtime.save(state)
        self.assertEqual(self.runtime.resume("run-tests"), "blocked")

    def test_idle_output_is_a_warning_not_a_claim_of_failure(self):
        self.runtime.begin("builder", "codex", "test", "relay-live-codex.log")
        state = self.runtime.state()
        state["calls"][0]["startedAt"] -= 1000
        self.runtime.save(state)
        call = self.runtime.summary()["calls"][0]
        self.assertTrue(call["idleWarning"])
        self.assertEqual(call["outcome"], "running")

    def test_review_artifacts_bind_commit_and_checksum(self):
        import appapi
        file = self.root / "capture.png"
        file.write_bytes(b"test fixture bytes")
        artifact = self.runtime.add_artifact("T01", "capture.png", "Compare the pose")
        self.assertEqual(artifact["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(appapi.artifact_file(self.root, "T01", "capture.png"), file.resolve())
        file.write_bytes(b"changed")
        with self.assertRaises(ValueError): appapi.artifact_file(self.root, "T01", "capture.png")

    def test_artifact_paths_reject_traversal_hidden_and_executables(self):
        for name in ("../outside.png", ".git/config", ".env", "password.json", "tool.exe", "C:/file.png"):
            with self.subTest(name=name), self.assertRaises(ValueError): self.runtime.add_artifact("T01", name)

    def test_local_api_artifacts_and_foreign_origin_rejection(self):
        import server
        import threading
        import urllib.request
        import urllib.error
        http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        with patch.object(server.Handler, "relay", self.root):
            thread.start()
            base = f"http://127.0.0.1:{http.server_port}"
            (self.root / "capture.png").write_bytes(b"fixture")
            data = json.dumps({"task": "T01", "path": "capture.png"}).encode()
            try:
                request = urllib.request.Request(base + "/api/artifacts/add", data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request) as response:
                    self.assertTrue(json.load(response)["ok"])
                request.add_header("Origin", "https://unrelated.example")
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    urllib.request.urlopen(request)
                self.assertEqual(refused.exception.code, 403)
                with urllib.request.urlopen(base + "/api/artifact?task=T01&path=capture.png") as response:
                    self.assertEqual(response.read(), b"fixture")
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(base + "/api/artifact?task=T01&path=../outside.png")
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=5)

    def test_review_event_only_when_auto_gates_are_finished(self):
        self.call()
        self.reviewed()
        self.call("reviewer", "claude", "claude-sonnet-test")
        self.runtime.guard_review("run-tests")
        self.assertEqual(self.runtime.state()["events"][-1]["kind"], "task_updated")
        self.task.write_text("# T01\n- [x] AUTO: test suite\n- [ ] HUMAN: inspect it\n")
        self.runtime.guard_review("run-tests")
        self.assertEqual(self.runtime.state()["events"][-1]["kind"], "needs_review")

    def test_worktree_keeps_original_branch_and_edits(self):
        import appapi
        before = self.git("branch", "--show-current")
        (self.root / "code.py").write_text("unsaved original")
        result = appapi.create_worktree(self.root)
        self.assertTrue(result["ok"], result)
        workspace = Path(result["path"])
        self.assertEqual(workspace.resolve().parent, (self.root.parent / ".agent-relay-worktrees").resolve())
        try:
            self.assertEqual(self.git("branch", "--show-current"), before)
            self.assertEqual((self.root / "code.py").read_text(), "unsaved original")
            self.assertEqual((workspace / "code.py").read_text(), "value=1\n")
            self.assertTrue((workspace / "relay-runtime.py").exists())
        finally:
            self.git("worktree", "remove", "--force", str(workspace))
            with __import__('contextlib').suppress(OSError): workspace.parent.rmdir()

    def test_dead_worker_lock_recovers_but_live_or_unknown_lock_does_not(self):
        self.runtime.claim(2147483647)
        with patch.object(module.Runtime, "process_alive", return_value=False):
            self.assertTrue(self.runtime.release_stale_lock())
        self.runtime.claim(os.getpid())
        with patch.object(module.Runtime, "process_alive", return_value=True):
            self.assertFalse(self.runtime.release_stale_lock())
        (self.root / ".relay.lock/owner.json").write_text('{}')
        self.assertFalse(self.runtime.release_stale_lock())

    def test_detached_worker_is_scoped_by_pid_and_start_identity(self):
        import appapi
        lock = self.root / ".relay.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(json.dumps({"pid": 42, "root": str(self.root.resolve()), "identity": "start-token"}))
        rows = {42: {"pid": 42, "ppid": 999, "name": "bash.exe", "cmd": "./relay-files.sh ."},
                43: {"pid": 43, "ppid": 42, "name": "mock.exe", "cmd": "mock tool"},
                44: {"pid": 44, "ppid": 999, "name": "bash.exe", "cmd": "unrelated shell"}}
        with patch.object(appapi.osops, "process_table", return_value=rows), patch.object(appapi.osops, "relay_roots", return_value=[]), patch.object(appapi, "runtime_for", return_value=self.runtime):
            with patch.object(module.Runtime, "process_identity", return_value="start-token"):
                self.assertEqual(appapi.relay_tree(self.root), ([42, 43], [42]))
            with patch.object(module.Runtime, "process_identity", return_value="different-start"):
                self.assertEqual(appapi.relay_tree(self.root), ([], []))

    def test_full_file_runner_resumes_review_without_rebuilding(self):
        import appapi
        bash = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")
        if not bash: self.skipTest("Bash required")
        for name in appapi.RELAY_FILES:
            shutil.copyfile(ROOT / "template" / name, self.root / name)
        (self.root / "AGENTS.md").write_text("# Fixture\n")
        (self.root / "relay.project.json").write_text(json.dumps({"testCommand": "echo PASS"}))
        config = json.loads((ROOT / "relay.settings.example.json").read_text())
        config["fallbackBuilders"] = []
        config["fallbackRoles"]["reviewer"] = []
        self.config(config)
        with (self.root / ".gitignore").open("a") as stream: stream.write("\n".join(appapi.GITIGNORE) + "\n")
        self.git("add", ".")
        self.git("commit", "-qm", "install fixture runner")
        mock_bin = self.root / ".relay/mock-bin"
        mock_bin.mkdir(parents=True)
        (mock_bin / "claude").write_text('''#!/usr/bin/env bash
cat >/dev/null
echo reviewer >> .relay/mock-calls.txt
python - <<'PY'
from pathlib import Path
p=Path('tasks/T01.md')
p.write_text(p.read_text().replace('[ ]','[x]'))
Path('CURRENT_TASK.md').unlink()
PY
printf '%s\\n' '{"type":"result","result":"reviewed fixture"}'
''', newline="\n")
        (mock_bin / "codex").write_text("#!/usr/bin/env bash\necho builder >> .relay/mock-calls.txt\nexit 91\n", newline="\n")
        for file in mock_bin.iterdir(): file.chmod(0o755)
        self.call(model="gpt-test")
        self.runtime.checkpoint("testing", "T01", "echo PASS")
        with (self.root / "relay-tests.log").open("w") as output:
            subprocess.run([bash, "-c", "echo PASS"], cwd=self.root, stdout=output, check=True)
        self.runtime.checkpoint("reviewing", "T01", "echo PASS", 0)
        env = {**os.environ, "MAX_ROUNDS": "1", "CLAUDE_BIN": (mock_bin / "claude").as_posix(), "CODEX_BIN": (mock_bin / "codex").as_posix()}
        result = subprocess.run([bash, "./relay-files.sh", "."], cwd=self.root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / ".relay/mock-calls.txt").read_text().splitlines(), ["reviewer"])
        self.assertIn("[x] AUTO", self.task.read_text())
        self.assertIn("[ ] HUMAN", self.task.read_text())
        self.assertEqual(self.runtime.state()["phase"], "idle")
        self.assertFalse((self.root / ".relay.lock").exists())
        # A safe retry must build, commit, execute tests and then review.
        (self.root / "CURRENT_TASK.md").write_text("tasks/T01.md\nT01 — Test\n", encoding="utf-8")
        self.task.write_text("# T01 - Test\n- [ ] AUTO: test suite\n- [ ] HUMAN: inspect it\n")
        (mock_bin / "codex").write_text('''#!/usr/bin/env bash
cat >/dev/null
echo builder >> .relay/mock-calls.txt
printf 'value=2\\n' > code.py
printf 'T01: fixture implementation\\nFILES:\\ncode.py\\n' > relay-commit.txt
printf '%s\\n' 'OpenAI Codex v1.0' '--------' 'workdir: fixture' 'model: gpt-test' 'provider: openai' 'session id: fixture' '--------'
echo fixture-built
''', newline="\n")
        self.git("add", "-f", "CURRENT_TASK.md", "tasks/T01.md")
        self.git("commit", "-qm", "next fixture cycle")
        self.runtime.checkpoint("building", "T01", "echo PASS")
        self.runtime.begin("builder", "codex", "auto", "relay-live-codex.log")

        def start_worker(root, script):
            run = subprocess.run([bash, script, "."], cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

        with patch.object(appapi.osops, "start_background", side_effect=start_worker):
            result = appapi.start_relay(self.root)
        self.assertEqual(result["message"], "Continuing the saved task.")
        self.assertEqual((self.root / ".relay/mock-calls.txt").read_text().splitlines(), ["reviewer", "builder", "reviewer"])
        self.assertEqual((self.root / "code.py").read_text(), "value=2\n")
        self.assertIn("[ ] HUMAN", self.task.read_text())
        self.assertEqual(self.runtime.state()["phase"], "idle")

        # A completed builder left code and a save request before the worker stopped.
        (self.root / "CURRENT_TASK.md").write_text("tasks/T01.md\nT01 — Test\n", encoding="utf-8")
        self.task.write_text("# T01 - Test\n- [ ] AUTO: test suite\n- [ ] HUMAN: inspect it\n")
        self.git("add", "-f", "CURRENT_TASK.md", "tasks/T01.md")
        self.git("commit", "-qm", "finished build fixture")
        self.runtime.checkpoint("building", "T01", "echo PASS")
        cid = self.runtime.begin("builder", "codex", "gpt-test", "relay-live-codex.log")
        code = self.root / "code.py"
        code.write_text("value=3\n")
        (self.root / "relay-commit.txt").write_text("T01: recovered implementation\nFILES:\ncode.py\n")
        self.runtime.finish(cid, 0, "success")
        with patch.object(appapi.osops, "start_background", side_effect=start_worker) as worker:
            code.write_text("value=4\n")
            blocked = appapi.start_relay(self.root, save_aside=True)
            self.assertIn("saved task or project files changed", blocked["error"])
            self.assertEqual(code.read_text(), "value=4\n")
            worker.assert_not_called()
            code.write_text("value=3\n")
            result = appapi.start_relay(self.root)
        self.assertEqual(result["message"], "Continuing the saved task.")
        self.assertEqual((self.root / ".relay/mock-calls.txt").read_text().splitlines(), ["reviewer", "builder", "reviewer", "reviewer"])
        self.assertIn("T01: recovered implementation", self.git("log", "-2", "--format=%s"))
        self.assertIn("[ ] HUMAN", self.task.read_text())


if __name__ == "__main__": unittest.main()
