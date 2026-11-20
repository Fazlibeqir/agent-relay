from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import appapi
import existing
import planner
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

    def saved_checkout(self, task_text):
        project = self.project.parent / "Existing project"
        project.mkdir()
        self.assertEqual(appapi._git(project, "init", "-q", "-b", "work/test").returncode, 0)
        (project / "AGENTS.md").write_text("# Project rules\n", encoding="utf-8")
        (project / "tasks").mkdir()
        task = project / "tasks/T01.md"
        task.write_text(task_text, encoding="utf-8")
        self.assertEqual(appapi._commit(project, "Starting checkout", ["AGENTS.md", "tasks/T01.md"]).returncode, 0)
        return project, task

    def test_saved_board_tasks_open_without_a_false_plan_or_launch(self):
        project, task = self.saved_checkout("# T01\nStatus: In progress (80%)\n- [x] Human signoff required\n- [ ] Human signed off\n")
        original = task.read_bytes()
        self.assertTrue(existing.inspect(project)["readyToOpen"])
        with patch.object(appapi, "githost_status", return_value={}):
            state = server.wizard_state(project)["project"]
        self.assertEqual(state["taskCount"], 1)
        self.assertFalse(state["hasPlan"])
        self.assertFalse(state["canStart"])
        self.assertFalse(state["canConfigureSavedTasks"])
        with patch.object(appapi.osops, "start_background") as worker:
            self.assertEqual(appapi.start_relay(project)["error"], state["startError"])
            self.assertFalse(existing.configure_saved_tasks(project, "echo PASS")["ok"])
            worker.assert_not_called()
        self.assertEqual(task.read_bytes(), original)
        self.assertFalse((project / "relay.project.json").exists())

    def test_existing_relay_tasks_configure_without_regenerating_the_plan(self):
        project, task = self.saved_checkout("# T01\n- [ ] AUTO: run tests\n- [ ] HUMAN: try the result\n")
        original = task.read_bytes()
        self.assertFalse(existing.configure_saved_tasks(project, " ")["ok"])
        self.assertTrue(existing.configure_saved_tasks(project, "echo PASS")["ok"])
        self.assertFalse(existing.configure_saved_tasks(project, "exit 1")["ok"])
        with patch.object(appapi, "githost_status", return_value={}):
            state = server.wizard_state(project)["project"]
        self.assertTrue(state["hasPlan"])
        self.assertTrue(state["canStart"])
        self.assertEqual(state["testCommand"], "echo PASS")
        with patch.object(appapi.osops, "start_background") as worker:
            self.assertTrue(appapi.start_relay(project)["ok"])
            worker.assert_called_once_with(project, "relay-files.sh")
        (project / "relay.sh").write_text("# Existing custom runner\n", encoding="utf-8")
        self.assertEqual(appapi.relay_script(project), "relay.sh")
        self.assertEqual(task.read_bytes(), original)

    def test_copilot_can_plan_with_selected_model_and_restricted_tools(self):
        self.assertIn("copilot", planner.PLANNER_PROVIDERS)

        def fake_copilot(cmd, **options):
            self.assertIn("--deny-tool=shell", cmd)
            self.assertIn("--available-tools=view,glob,grep,create,edit,apply_patch", cmd)
            self.assertIn("--allow-tool=write", cmd)
            self.assertEqual(cmd[-6:-1], ["--model", "test-model", "--reasoning-effort", "high", "-p"])
            self.assertIn("Let club members RSVP.", cmd[-1])
            self.assertEqual(options["env"]["COPILOT_ALLOW_ALL"], "false")
            (self.project / "PROPOSAL.md").write_text("# Proposal\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(planner.threading, "Thread") as background, patch.object(planner.subprocess, "run", side_effect=fake_copilot):
            result = planner.start_job("propose", self.project, "copilot", "copilot", "test-model",
                                       idea="Let club members RSVP.", effort="high")
            self.assertTrue(result["ok"], result)
            self.addCleanup(planner.JOBS.pop, result["job"], None)
            background.call_args.kwargs["target"]()
        self.assertEqual(planner.job_status(result["job"])["state"], "done")

    def test_copilot_auto_and_implicit_model_ignore_stale_reasoning(self):
        for model in ("auto", ""):
            with self.subTest(model=model):
                cmd, _ = planner._provider_cmd("copilot", "copilot", model, self.project, "medium")
                self.assertNotIn("--reasoning-effort", cmd)
                if model:
                    self.assertEqual(cmd[-2:], ["--model", "auto"])
                else:
                    self.assertNotIn("--model", cmd)

if __name__ == "__main__":
    unittest.main()
