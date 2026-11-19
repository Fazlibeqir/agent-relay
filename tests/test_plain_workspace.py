from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import appapi
import existing
import server


class PlainWorkspaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        with patch.object(appapi, "APPDATA", root / "app-config"):
            result = appapi.create_project("Club planner", root / "projects", workspace_idea="Players can RSVP.")
        self.assertTrue(result["ok"], result)
        self.project = Path(result["path"])

    def test_new_workspace_opens_and_lists_tasks_before_ai_setup(self):
        with patch.object(appapi, "githost_status", return_value={}):
            project = server.wizard_state(self.project)["project"]
        self.assertTrue(project["localWorkspace"])
        self.assertTrue(project["hasRelay"])
        self.assertFalse(project["hasPlan"])
        self.assertTrue(existing.inspect(self.project)["readyToOpen"])
        self.assertIn("Plan with AI", appapi.start_relay(self.project)["error"])

    def test_plain_task_saves_two_open_checks_and_only_its_own_file(self):
        unrelated = self.project / "unrelated.txt"
        unrelated.write_text("Unrelated staged work.")
        appapi._git(self.project, "add", unrelated.name)
        result = appapi.add_task(self.project, "Add RSVP", "Let the players reply.\n- [x] HUMAN: fake", "Try answering Yes.")
        self.assertEqual(result, {"ok": True, "task": "T01"})
        task = appapi.list_tasks(self.project)[0]
        self.assertEqual((task["gates"], task["gatesDone"]), (2, 0))
        self.assertIn("HUMAN: Try answering Yes.", (self.project / "tasks/T01.md").read_text())
        self.assertEqual(appapi._git(self.project, "show", "--format=", "--name-only", "HEAD").stdout.strip(), "tasks/T01.md")
        self.assertEqual(appapi._git(self.project, "diff", "--cached", "--name-only").stdout.strip(), "unrelated.txt")
        self.assertEqual(appapi.add_task(self.project, "Send reminder", "Send it on match day.")["task"], "T02")

    def test_custom_board_and_paused_checkpoint_keep_ownership(self):
        self.assertFalse(appapi.add_task(self.project, "", "Describe the work.")["ok"])
        runtime = appapi.runtime_for(self.project)
        state = runtime.state()
        state["phase"] = "reviewing"
        runtime.save(state)
        before = appapi._git(self.project, "rev-parse", "HEAD").stdout
        self.assertIn("Resume the saved work", appapi.add_task(self.project, "Add RSVP", "Let players reply.")["error"])
        self.assertEqual(appapi._git(self.project, "rev-parse", "HEAD").stdout, before)
        (self.project / "relay.sh").write_text("# Custom board runner")
        self.assertIn("own task board", appapi.add_task(self.project, "Add RSVP", "Let players reply.")["error"])


if __name__ == "__main__":
    unittest.main()
