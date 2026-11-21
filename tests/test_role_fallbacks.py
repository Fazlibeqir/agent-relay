import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "template"
sys.path.insert(0, str(ROOT / "dashboard"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


settings = load_module("relay_settings", TEMPLATE / "relay-settings.py")
limits = load_module("relay_limits", TEMPLATE / "relay-limits.py")


class SettingsAndLimits(unittest.TestCase):
    def test_existing_settings_receive_role_backups(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "relay.settings.json"
            path.write_text('{"fallbackBuilders": ["opencode"]}')
            config, problems, ok = settings.effective(path)
            self.assertTrue(ok, problems)
            self.assertEqual(config["fallbackRoles"]["reviewer"], ["copilot", "opencode", "codex"])
            self.assertEqual(config["fallbackBuilders"], ["opencode"])

    def test_builtin_reviewer_and_backup_validation(self):
        config = json.loads(json.dumps(settings.DEFAULTS))
        config["roles"]["reviewer"] = {"provider": "opencode", "model": "test-model"}
        problems = {"errors": [], "warnings": []}
        settings.validate(config, problems)
        self.assertFalse(problems["errors"])
        config["fallbackRoles"]["reviewer"] = ["bogus"]
        settings.validate(config, problems)
        self.assertTrue(problems["errors"])

    def test_absolute_weekly_reset(self):
        now = dt.datetime(2026, 11, 18, 10, 0, tzinfo=dt.timezone.utc)
        result = limits.wait_seconds("You've hit your weekly limit · resets Nov 20, 12pm", now=now)
        self.assertEqual(result, 50 * 3600 + 60)

    def test_relative_and_clock_resets(self):
        now = dt.datetime(2026, 11, 18, 10, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(limits.wait_seconds("Try again in 2 hours 5 minutes", now=now), 7560)
        self.assertEqual(limits.wait_seconds("Try again at 11:39 AM", now=now), 6000)
        self.assertEqual(limits.wait_seconds("unknown format", 1234, now), 1234)
        self.assertEqual(limits.wait_seconds("resets Foo 33, 25pm", 1234, now), 1234)
        self.assertEqual(limits.wait_seconds("resets Nov 20, 25pm", 1234, now), 1234)

    def test_timezone_reset(self):
        now = dt.datetime(2026, 11, 18, 9, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(limits.wait_seconds("resets Nov 20, 12pm (UTC/Unknown)", 1234, now), 1234)
        self.assertEqual(limits.wait_seconds("resets Nov 20, 12pm (Europe/Skopje)", now=now), 50 * 3600 + 60)


class RoleRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        for name in ("relay-lib.sh", "relay-settings.py", "relay-limits.py", "relay-claude-result.py"):
            shutil.copyfile(TEMPLATE / name, self.folder / name)
        self.bin = self.folder / "bin"
        self.bin.mkdir()
        mock = r"""#!/usr/bin/env bash
tool=$(basename "$0")
printf '%s\n' "$tool" >> calls.txt
cat > "prompt-$tool.txt"
case "$tool" in
  claude) mode=${MOCK_CLAUDE:-limit};;
  codex) mode=${MOCK_CODEX:-ok};;
  copilot) mode=${MOCK_COPILOT:-ok};;
  opencode) mode=${MOCK_OPENCODE:-ok};;
esac
case "$mode" in
  limit) printf '%s\n' "You've hit your weekly limit · resets Nov 20, 12pm"; exit 1;;
  error) echo "provider failed"; exit 7;;
esac
if [ "$tool" = claude ]; then printf '%s\n' '{"type":"result","result":"review complete"}'
else printf '%s\n' "$tool complete"; fi
"""
        for tool in ("claude", "codex", "copilot", "opencode"):
            executable = self.bin / tool
            executable.write_text(mock, encoding="utf-8", newline="\n")
            executable.chmod(0o755)
        bash = os.environ.get("BASH_BIN") or (r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash"))
        if not bash or not Path(bash).exists():
            self.skipTest("Git Bash or bash is required for runner integration")
        self.bash = bash
        self.config = json.loads(json.dumps(settings.DEFAULTS))

    def run_shell(self, script, **environment):
        (self.folder / "relay.settings.json").write_text(json.dumps(self.config), encoding="utf-8")
        env = dict(os.environ)
        env.update(environment)
        env["MOCK_BIN"] = self.bin.as_posix()
        harness = r"""set -uo pipefail
if command -v cygpath >/dev/null; then MOCK_BIN=$(cygpath -u "$MOCK_BIN"); fi
export PATH="$MOCK_BIN:$PATH"
CLAUDE_BIN="$MOCK_BIN/claude"; CODEX_BIN="$MOCK_BIN/codex"; COPILOT_BIN="$MOCK_BIN/copilot"
CODEX_ARGS=(); CODEX_PICK=(); CODEX_PICK_MODEL=test-model
AGENT_TIMEOUT=5; SLEEP_ON_LIMIT=1800; LIMIT_RE="weekly limit|usage limit"
LOG=relay.log; TASK_FILE=CURRENT_TASK.md
log() { printf '%s\n' "$*" >> harness.log; }
source ./relay-lib.sh
limit_sleep() { echo unexpected-sleep; exit 89; }
""" + script
        return subprocess.run([self.bash, "-c", harness], cwd=self.folder, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=30)

    def calls(self):
        return (self.folder / "calls.txt").read_text().splitlines()

    def test_reviewer_falls_back_with_original_prompt(self):
        prompt = "Review this task with quoted 'text' and\na second line."
        result = self.run_shell("claude_run reviewer \"$REVIEW_PROMPT\"; printf '%s' \"$LAST_OUT\"", REVIEW_PROMPT=prompt)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.calls(), ["claude", "copilot"])
        self.assertEqual((self.folder / "prompt-copilot.txt").read_text().rstrip(), prompt)
        self.assertIn("copilot complete", result.stdout)

    def test_dispatcher_skips_cooled_down_claude(self):
        result = self.run_shell('claude_run dispatcher "first"; claude_run dispatcher "second"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["claude", "codex", "codex"])

    def test_actual_backup_builder_cannot_review(self):
        result = self.run_shell('builder_run "build"; claude_run reviewer "review"', MOCK_CODEX="limit")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.calls(), ["codex", "copilot", "claude", "opencode"])
        self.assertIn("copilot|auto", (self.folder / "relay-builder.identity").read_text())
        self.assertIn("it built this round", (self.folder / "harness.log").read_text())

    def test_failed_backup_does_not_count_as_success(self):
        result = self.run_shell('claude_run reviewer "review"', MOCK_COPILOT="error")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["claude", "copilot", "opencode"])

    def test_all_limited_waits_after_trying_every_eligible_backup(self):
        result = self.run_shell('claude_run reviewer "review"', MOCK_COPILOT="limit", MOCK_OPENCODE="limit")
        self.assertEqual(result.returncode, 89, result.stderr)
        self.assertEqual(self.calls(), ["claude", "copilot", "opencode"])

    def test_successful_primary_never_calls_backup(self):
        result = self.run_shell('claude_run reviewer "review"; printf "%s" "$LAST_OUT"', MOCK_CLAUDE="ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["claude"])
        self.assertEqual(result.stdout, "review complete")

    def test_explicit_different_model_can_review(self):
        self.config["roles"]["builder"]["model"] = "test-model"
        self.config["roles"]["reviewer"] = {"provider": "codex", "model": "different-model", "effort": "high"}
        (self.folder / "relay-builder.identity").write_text("codex|test-model\n")
        result = self.run_shell('claude_run reviewer "review"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["codex"])

    def test_no_independent_provider_fails_the_review(self):
        self.config["roles"]["reviewer"] = {"provider": "codex", "model": "auto"}
        self.config["roles"]["builder"] = {"provider": "copilot", "model": "auto"}
        self.config["fallbackRoles"]["reviewer"] = []
        (self.folder / "relay-builder.identity").write_text("codex|test-model\n")
        result = self.run_shell('claude_run reviewer "review"')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse((self.folder / "calls.txt").exists())

    def test_daily_cap_skips_backup(self):
        self.config["fallbackMaxPerDay"] = 0
        result = self.run_shell('claude_run reviewer "review"')
        self.assertEqual(result.returncode, 89, result.stderr)
        self.assertEqual(self.calls(), ["claude"])

    def test_duplicate_primary_is_not_retried(self):
        self.config["fallbackRoles"]["dispatcher"] = ["claude", "codex"]
        result = self.run_shell('claude_run dispatcher "dispatch"', MOCK_CLAUDE="error")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["claude", "codex"])

    def test_runtime_capability_policy_is_used_by_real_role_loop(self):
        shutil.copyfile(TEMPLATE / "relay-runtime.py", self.folder / "relay-runtime.py")
        subprocess.run(["git", "init", "-q", str(self.folder)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.folder), "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "--allow-empty", "-qm", "fixture"], check=True, capture_output=True)
        (self.folder / "CURRENT_TASK.md").write_text("tasks/T01.md\nT01\nREQUIRES: notion\n")
        self.config["capabilities"]["claude"].append("notion")
        self.config["capabilities"]["opencode"].append("notion")
        result = self.run_shell('claude_run reviewer "review"')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), ["claude", "opencode"])
        self.assertIn("missing capabilities: notion", (self.folder / "harness.log").read_text())

    def test_real_runner_falls_back_when_review_model_is_unreported(self):
        shutil.copyfile(TEMPLATE / "relay-runtime.py", self.folder / "relay-runtime.py")
        subprocess.run(["git", "init", "-q", str(self.folder)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.folder), "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                        "commit", "--allow-empty", "-qm", "fixture"], check=True, capture_output=True)
        (self.folder / "tasks").mkdir()
        (self.folder / "tasks/T01.md").write_text("- [ ] AUTO: pass tests\n- [ ] HUMAN: check it\n")
        (self.folder / "CURRENT_TASK.md").write_text("tasks/T01.md\nT01\n")
        (self.folder / ".gitignore").write_text(".relay/\ncalls.txt\nprompt-*.txt\nharness.log\n")
        self.config["roles"]["reviewer"] = {"provider": "copilot", "model": "auto"}
        self.config["fallbackRoles"]["reviewer"] = ["claude"]
        result = self.run_shell(
            """
relay_runtime checkpoint building T01 "echo PASS" || exit
builder_run "build" || exit
relay_runtime checkpoint testing T01 "echo PASS" || exit
echo PASS > relay-tests.log
relay_runtime checkpoint reviewing T01 "echo PASS" 0 || exit
claude_run reviewer "review" || exit
relay_runtime guard "echo PASS"
""", MOCK_CLAUDE="ok")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), ["codex", "copilot", "claude"])
        state = json.loads((self.folder / ".relay/state.json").read_text(encoding="utf-8"))
        self.assertEqual([call["outcome"] for call in state["calls"]], ["success", "review_identity_unknown", "success"])
        self.assertEqual(state["independentReview"], state["calls"][-1]["id"])

    def test_checked_launcher_is_selected_after_failed_tool_is_skipped(self):
        from appapi import runtime_for

        shutil.copyfile(TEMPLATE / "relay-runtime.py", self.folder / "relay-runtime.py")
        subprocess.run(["git", "init", "-q", str(self.folder)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.folder), "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                        "commit", "--allow-empty", "-qm", "fixture"], check=True, capture_output=True)
        runtime = runtime_for(self.folder)
        runtime.record_access("claude", self.bin / "claude", False)
        runtime.record_access("codex", self.bin / "codex", True)
        runtime.set_access_policy(True)
        result = self.run_shell('claude_run dispatcher "fixture"', MOCK_CLAUDE="ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["codex"])
        self.assertIn("project access check failed", (self.folder / "harness.log").read_text())


class DashboardState(unittest.TestCase):
    def test_claude_reset_is_visible_without_a_reported_percentage(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            relay = Path(folder)
            log = relay / "relay-live-claude.log"
            event = {"type": "rate_limit_event", "rate_limit_info": {
                "status": "allowed", "rateLimitType": "five_hour", "resetsAt": 2200005000,
            }}
            with patch.object(server.appapi, "APPDATA", relay / "config"), patch.object(server.time, "time", return_value=2200000000):
                log.write_text(json.dumps(event), encoding="utf-8")
                usage = server.claude_usage(relay)
                self.assertEqual(usage["reset"], {"label": "5-hour", "resetsAt": 2200005000})
                self.assertEqual(usage["windows"], [])

                event["rate_limit_info"]["utilization"] = 0.82
                log.write_text(json.dumps(event), encoding="utf-8")
                self.assertEqual(server.claude_usage(relay)["windows"][0]["usedPercent"], 82)
                del event["rate_limit_info"]["utilization"]
                log.write_text(json.dumps(event), encoding="utf-8")
                self.assertEqual(server.claude_usage(relay)["windows"][0]["usedPercent"], 82)
                event["rate_limit_info"]["resetsAt"] = 2200006000
                log.write_text(json.dumps(event), encoding="utf-8")
                self.assertEqual(server.claude_usage(relay)["windows"], [])

    def test_claude_reads_every_window_from_the_new_event_format(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            relay = Path(folder)
            event = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": 2200005000,
                     "unifiedWindows": {"five_hour": {"utilization": 0.54, "resetsAt": 2200005000, "status": "allowed"},
                                        "seven_day": {"utilization": 0.04, "resetsAt": 2200400000, "status": "allowed"}}}}
            with patch.object(server.appapi, "APPDATA", relay / "config"), patch.object(server.time, "time", return_value=2200000000):
                (relay / "relay-live-claude.log").write_text(json.dumps(event), encoding="utf-8")
                usage = server.claude_usage(relay)
                self.assertEqual(sorted((w["label"], w["usedPercent"]) for w in usage["windows"]), [("5-hour", 54), ("weekly", 4)])
                self.assertEqual(usage["reset"], {"label": "5-hour", "resetsAt": 2200005000})

    def test_copilot_card_prefers_the_account_quota_and_falls_back_to_the_estimate(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            relay = Path(folder)
            with patch.object(server.copilot_quota, "cached", return_value={"windows": [{"label": "Premium", "usedPercent": 21}], "installed": True}):
                usage = server.copilot_usage(relay, 2)
                self.assertEqual([(w["label"], w["usedPercent"]) for w in usage["windows"]], [("Premium", 21), ("today", 20)])
                self.assertNotIn("estimate", usage)
            with patch.object(server.copilot_quota, "cached", return_value=None), patch.dict(os.environ, {"COPILOT_HOME": str(relay / "none")}):
                usage = server.copilot_usage(relay, 0)
                self.assertEqual([w["label"] for w in usage["windows"]], ["today"])

    def test_copilot_usage_uses_only_reported_finite_allowances(self):
        from copilot_usage import quota_windows
        windows = quota_windows({
            "premium_interactions": {"entitlementRequests": 200, "remainingPercentage": 79,
                                     "resetDate": "2026-11-21T10:28:53.034Z"},
            "chat": {"entitlementRequests": -1, "remainingPercentage": 100},
            "completions": {"entitlementRequests": 0, "remainingPercentage": 100},
            "weekly": {"entitlementRequests": 10, "remainingPercentage": float("nan")},
            "ai_credits": {"entitlementRequests": 50, "remainingPercentage": 100,
                           "resetDate": "2026-12-01T00:00:00Z"},
        })
        self.assertEqual(windows, [
            {"label": "Premium", "usedPercent": 21},
            {"label": "AI credits", "usedPercent": 0},
        ])

    def test_settings_round_trip_and_invalid_backup_preserves_file(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            relay = Path(folder)
            shutil.copyfile(TEMPLATE / "relay-settings.py", relay / "relay-settings.py")
            config = json.loads(json.dumps(settings.DEFAULTS))
            saved, problems = server.save_settings(relay, config)
            self.assertTrue(saved, problems)
            self.assertEqual(server.settings_state(relay)["effective"]["fallbackRoles"], config["fallbackRoles"])
            before = (relay / "relay.settings.json").read_bytes()
            config["fallbackRoles"]["reviewer"] = ["unavailable-provider"]
            saved, problems = server.save_settings(relay, config)
            self.assertFalse(saved)
            self.assertTrue(problems["errors"])
            self.assertEqual(before, (relay / "relay.settings.json").read_bytes())

    def test_wait_is_a_retry_and_active_role_is_named(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            relay = Path(folder)
            (relay / ".relay.lock").mkdir()
            (relay / "relay.log").write_text(
                "[2026-11-18 10:00:00] relay started on branch agent-run\n"
                "[2026-11-18 10:01:00] claude hit a usage limit — sleeping 1800s (until about 10:31)\n",
                encoding="utf-8")
            with patch.object(server, "codex_usage", return_value=None), patch.object(server, "claude_usage", return_value=None):
                state = server.build_state(relay, [{"id": "claude"}, {"id": "copilot"}])
                self.assertIn("retry", state["phase"]["label"])
                self.assertIn("retryAt", state["phase"])
                self.assertNotIn("until", state["phase"])
                retry_at = state["phase"]["retryAt"]
                with patch.object(server.time, "time", return_value=retry_at - 1):
                    active = server.build_state(relay, [{"id": "claude"}])
                    self.assertEqual(active["providers"][0]["lastLimit"]["retryAt"], retry_at)
                with patch.object(server.time, "time", return_value=retry_at):
                    expired = server.build_state(relay, [{"id": "claude"}])
                    self.assertIsNone(expired["providers"][0]["lastLimit"])
                with (relay / "relay.log").open("a", encoding="utf-8") as log:
                    log.write("[2026-11-18 10:02:00] role reviewer running copilot auto\n")
                state = server.build_state(relay, [{"id": "claude"}, {"id": "copilot"}])
                self.assertEqual(state["phase"]["kind"], "REVIEW")
                self.assertEqual(state["providers"][1]["role"], "reviewer")


if __name__ == "__main__":
    unittest.main()
