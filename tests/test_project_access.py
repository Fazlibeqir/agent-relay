import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import appapi


class ProjectAccessTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        with patch.object(appapi, "APPDATA", root / "appdata"):
            result = appapi.create_project("Team", str(root), workspace_idea="Fixture")
        self.assertTrue(result["ok"], result)
        self.project = Path(result["path"])
        self.launcher = root / "mock-ai"
        self.launcher.write_text("mock executable")
        for substitute in (
            patch.object(appapi, "access_launcher", return_value=str(self.launcher)),
            patch.object(appapi, "relay_tree", return_value=([], [])),
            patch.dict(os.environ, {"RELAY_ACCESS_CLI": str(self.launcher)}),
        ):
            substitute.start()
            self.addCleanup(substitute.stop)
        self.runtime = appapi.runtime_for(self.project)

    def test_challenge_must_be_read_and_written_and_checks_are_project_local(self):
        def copy_challenge(provider, launcher, folder, prompt, model):
            self.assertEqual(provider, "codex")
            self.assertEqual(folder.parent, self.project / ".relay")
            self.assertTrue((folder / ".git").is_dir())
            self.assertFalse((folder / "README.md").exists())
            answer = (folder / "input.txt").read_bytes()
            self.assertNotIn(answer.decode(), prompt)
            (folder / "result.txt").write_bytes(answer)
            return True

        with patch.object(appapi, "_run_access_check", side_effect=copy_challenge):
            result = appapi.check_project_access(self.project, "codex")
        self.assertTrue(result["passed"], result)
        self.assertEqual(self.runtime.access_status("codex"), "passed")
        self.assertEqual(self.runtime.access_status("claude"), "unchecked")
        self.assertFalse(list((self.project / ".relay").glob("access-*")))
        self.assertFalse((self.project / ".relay.lock").exists())
        self.assertTrue(self.runtime.eligible("builder", "codex", "auto")[0])

    def test_successful_program_exit_without_file_blocks_assignment(self):
        with patch.object(appapi, "_run_access_check", return_value=True):
            self.assertFalse(appapi.check_project_access(self.project, "claude")["passed"])
        eligible, reason = self.runtime.eligible("reviewer", "claude", "claude-fixture")
        self.assertFalse(eligible)
        self.assertIn("access check failed", reason)
        self.assertTrue(self.runtime.eligible("builder", "codex", "auto")[0])

    def test_passed_only_mode_rejects_unknown_and_replaced_launchers(self):
        self.runtime.set_access_policy(True)
        self.assertFalse(self.runtime.eligible("builder", "codex", "auto")[0])
        self.runtime.record_access("codex", self.launcher, True)
        self.assertTrue(self.runtime.eligible("builder", "codex", "auto")[0])
        with patch.dict(os.environ, {"RELAY_ACCESS_CLI": str(self.launcher) + "-other"}):
            self.assertFalse(self.runtime.eligible("builder", "codex", "auto")[0])
        self.launcher.write_text("updated mock executable")
        self.assertEqual(self.runtime.access_status("codex"), "unchecked")
        self.assertFalse(self.runtime.eligible("builder", "codex", "auto")[0])

    def test_running_and_custom_projects_are_skipped_without_invoking_ai(self):
        with patch.object(appapi, "_run_access_check") as run:
            with patch.object(appapi, "relay_tree", return_value=([17], [17])):
                self.assertIn("Stop Relay", appapi.check_project_access(self.project, "codex")["error"])
            (self.project / "relay.sh").write_text("# Custom workflow\n")
            self.assertFalse(appapi.project_access(self.project)["supported"])
            self.assertFalse(appapi.check_project_access(self.project, "codex")["ok"])
            run.assert_not_called()

    def test_backup_cli_checks_use_project_models_and_read_write_tools(self):
        config = self.runtime.config()
        config["models"].update(opencode="example/fixture-model", copilot="fixture-reviewer")
        (self.project / "relay.settings.json").write_text(json.dumps(config), encoding="utf-8")

        for provider, model in (("opencode", "example/fixture-model"), ("copilot", "fixture-reviewer")):
            with self.subTest(provider=provider), patch.dict(os.environ, {provider.upper() + "_MODEL": ""}):
                original = subprocess.Popen

                def launch(command, **kwargs):
                    if command[0] != str(self.launcher):
                        return original(command, **kwargs)
                    cwd, env = kwargs["cwd"], kwargs["env"]
                    self.assertIn(model, command)
                    self.assertNotIn("--allow-all", command)
                    if provider == "opencode":
                        self.assertIn("--pure", command)
                        self.assertEqual(json.loads(env["OPENCODE_PERMISSION"])["*"], "deny")
                    else:
                        self.assertIn("--available-tools=view,create,edit,apply_patch", command)
                        self.assertIn("--disable-builtin-mcps", command)
                    (cwd / "result.txt").write_bytes((cwd / "input.txt").read_bytes())
                    return process.return_value

                with patch.object(appapi.subprocess, "Popen", side_effect=launch) as process:
                    spawn = process.return_value
                    spawn.__enter__.return_value = spawn
                    spawn.returncode = 0
                    self.assertTrue(appapi.check_project_access(self.project, provider)["passed"])
                self.assertEqual(self.runtime.access_status(provider), "passed")


if __name__ == "__main__":
    unittest.main()
