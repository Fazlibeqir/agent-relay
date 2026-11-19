import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import appapi


class RunnerUpdateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        with patch.object(appapi, "APPDATA", self.root / "appdata"):
            result = appapi.create_project("Club", str(self.root), workspace_idea="A club website")
        self.assertTrue(result["ok"], result)
        self.project = Path(result["path"])
        self.script = self.project / "relay-runtime.py"
        self.bundled = self.script.read_bytes()
        self.old = self.bundled + b"\n# An earlier bundled runner\n"
        self.script.write_bytes(self.old)
        self.git("rm", "-q", "--", "relay-limits.py")
        self.git("add", "--", self.script.name)
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "--amend", "--no-edit", "-q")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        table = patch.object(appapi.osops, "process_table", return_value={})
        table.start()
        self.addCleanup(table.stop)

    def git(self, *args):
        result = appapi._git(self.project, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_updates_owned_files_and_preserves_unrelated_staging(self):
        user_file = self.project / "README.md"
        user_file.write_text("User staged draft\n")
        self.git("add", "--", "README.md")
        user_file.write_text("User unsaved draft\n")
        settings = self.project / "relay.settings.json"
        settings.write_text('{"models":{"copilot":"my-selection"}}')
        task = self.project / "tasks/T01.md"
        task.write_text("# My task\n- [ ] HUMAN: Check it\n")
        self.assertTrue(appapi.runner_update_available(self.project))
        result = appapi.update_runner(self.project)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.script.read_bytes(), self.bundled)
        self.assertTrue((self.project / "relay-limits.py").is_file())
        self.assertEqual(self.git("show", "-1", "--format=", "--name-only").stdout.split(),
                         ["relay-limits.py", "relay-runtime.py"])
        self.assertEqual(self.git("show", ":README.md").stdout, "User staged draft\n")
        self.assertEqual(user_file.read_text(), "User unsaved draft\n")
        self.assertIn("my-selection", settings.read_text())
        self.assertIn("[ ] HUMAN", task.read_text())
        self.assertFalse(appapi.runner_update_available(self.project))
        head = self.git("rev-parse", "HEAD").stdout
        self.assertTrue(appapi.update_runner(self.project)["ok"])
        self.assertEqual(head, self.git("rev-parse", "HEAD").stdout)

    def test_protects_committed_and_unsaved_script_edits(self):
        self.script.write_bytes(self.old + b"# customized\n")
        self.assertIn("unsaved", appapi.update_runner(self.project)["error"])
        self.git("add", "--", self.script.name)
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "Customize runner")
        self.assertIn("custom changes", appapi.update_runner(self.project)["error"])
        self.assertTrue(self.script.read_bytes().endswith(b"# customized\n"))
        self.assertFalse((self.project / "relay-limits.py").exists())

    def test_blocks_custom_runner_and_pending_task(self):
        with patch.object(appapi, "relay_tree", return_value=([42], [42])):
            self.assertIn("Stop Relay", appapi.update_runner(self.project)["error"])
        runtime = appapi.runtime_for(self.project)
        state = runtime.state()
        state["phase"] = "reviewing"
        runtime.save(state)
        self.assertIn("saved Relay task", appapi.update_runner(self.project)["error"])
        (self.project / "relay.sh").write_text("# Custom runner\n")
        self.assertFalse(appapi.runner_update_available(self.project))
        self.assertIn("custom Relay setup", appapi.update_runner(self.project)["error"])
        self.assertEqual(self.base, self.git("rev-parse", "HEAD").stdout.strip())

    def test_commit_failure_restores_only_relay_files(self):
        unrelated = self.project / "README.md"
        unrelated.write_text("Staged\n")
        self.git("add", "--", "README.md")

        def rejected_commit(project, message, files, *, only):
            self.assertTrue(only)
            self.git("add", "--", *files)
            return subprocess.CompletedProcess([], 1)

        with patch.object(appapi, "_commit", side_effect=rejected_commit):
            result = appapi.update_runner(self.project)
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.script.read_bytes(), self.old)
        self.assertFalse((self.project / "relay-limits.py").exists())
        self.assertFalse((self.project / appapi.RUNNER_UPDATE).exists())
        self.assertEqual(self.git("diff", "--cached", "--name-only").stdout.strip(), "README.md")
        self.assertTrue(appapi.update_runner(self.project)["ok"])

    def test_retry_recovers_an_interrupted_update_before_applying_again(self):
        journal = self.project / appapi.RUNNER_UPDATE
        journal.parent.mkdir()
        journal.write_text(json.dumps({"head": self.base, "branch": "agent-run",
                                      "files": {self.script.name: [self.old.hex(), self.bundled.hex()]}}))
        self.script.write_bytes(self.bundled)
        self.git("add", "--", self.script.name)
        self.assertIn("Finish Update Relay", appapi.start_relay(self.project)["error"])
        result = appapi.update_runner(self.project)
        self.assertTrue(result["ok"], result)
        self.assertFalse(journal.exists())
        self.assertFalse((self.project / ".relay.lock").exists())


if __name__ == "__main__":
    unittest.main()
