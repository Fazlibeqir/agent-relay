import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
import server  # noqa: E402


def stamp(seconds_ago):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - seconds_ago))


class ParallelDashboardState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.relay = Path(self.tmp.name)

    def log(self, lines, running=True):
        text = "\n".join("[%s] %s" % (stamp(ago), msg) for ago, msg in lines) + "\n"
        (self.relay / "relay.log").write_text(text, encoding="utf-8")
        if running:
            (self.relay / ".relay.lock").mkdir(exist_ok=True)

    def test_lanes_and_the_building_phase_come_from_the_parallel_round(self):
        self.log([(300, "relay started on branch agent-run"),
                  (120, "round 1 — parallel mode: the dispatcher plans a round for codex copilot"),
                  (100, "round 1 — T01: codex builds in its own worktree"),
                  (100, "round 1 — T02: copilot builds in its own worktree")])
        (self.relay / "relay.settings.json").write_text(json.dumps({"mode": "parallel"}))
        state = server.build_state(self.relay, [])
        self.assertEqual(state["phase"]["kind"], "BUILD")
        self.assertEqual(state["lanes"], [{"task": "T01", "provider": "codex"}, {"task": "T02", "provider": "copilot"}])
        self.assertEqual(state["mode"], "parallel")

    def test_the_dispatcher_planning_is_its_own_phase_and_merging_is_the_suite_phase(self):
        self.log([(300, "relay started on branch agent-run"), (60, "round 1 — parallel mode: the dispatcher plans a round for codex copilot")])
        self.assertEqual(server.build_state(self.relay, [])["phase"]["kind"], "DISPATCH")
        self.log([(300, "relay started on branch agent-run"), (60, "round 1 — T01 — Task 1 — codex builder: merged 2 commit(s); running the full suite for the review")])
        self.assertEqual(server.build_state(self.relay, [])["phase"]["kind"], "SUITE")

    def test_a_serial_relay_has_no_lanes_and_mode_serial(self):
        self.log([(300, "relay started on branch agent-run"), (100, "round 1 — T01 — Task 1 — Codex building")])
        state = server.build_state(self.relay, [])
        self.assertEqual(state["lanes"], [])
        self.assertEqual(state["mode"], "serial")

    def test_lanes_of_an_earlier_round_are_replaced_by_the_latest_plan(self):
        self.log([(500, "relay started on branch agent-run"),
                  (400, "round 1 — parallel mode: the dispatcher plans a round for codex copilot"),
                  (390, "round 1 — T01: codex builds in its own worktree"),
                  (200, "round 2 — parallel mode: the dispatcher plans a round for codex copilot"),
                  (190, "round 2 — T05: copilot builds in its own worktree")])
        self.assertEqual(server.build_state(self.relay, [])["lanes"], [{"task": "T05", "provider": "copilot"}])


class ModeSwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.relay = Path(self.tmp.name)
        (self.relay / "AGENTS.md").write_text("# x\n")
        (self.relay / "tasks").mkdir()
        (self.relay / "relay.project.json").write_text(json.dumps({"testCommand": "true"}))
        (self.relay / "relay-files.sh").write_text("#!/usr/bin/env bash\n")
        shutil.copyfile(ROOT / "template" / "relay-settings.py", self.relay / "relay-settings.py")

    def saved_mode(self):
        return json.loads((self.relay / "relay.settings.json").read_text()).get("mode")

    def test_parallel_needs_the_parallel_scripts_in_the_project(self):
        ok, problems = server.set_mode(self.relay, "parallel")
        self.assertFalse(ok)
        self.assertIn("Update Relay", problems["errors"][0])
        self.assertFalse((self.relay / "relay.settings.json").exists())

    def test_parallel_can_be_switched_on_and_off_and_keeps_the_other_settings(self):
        (self.relay / "relay-parallel.sh").write_text("# parallel\n")
        (self.relay / "relay.settings.json").write_text(json.dumps({"fallbackMaxPerDay": 7}))
        ok, problems = server.set_mode(self.relay, "parallel")
        self.assertTrue(ok, problems)
        self.assertEqual(self.saved_mode(), "parallel")
        self.assertEqual(json.loads((self.relay / "relay.settings.json").read_text())["fallbackMaxPerDay"], 7)
        ok, problems = server.set_mode(self.relay, "serial")
        self.assertTrue(ok, problems)
        self.assertEqual(self.saved_mode(), "serial")

    def test_serial_is_always_allowed_and_nonsense_is_refused(self):
        self.assertTrue(server.set_mode(self.relay, "serial")[0])
        self.assertFalse(server.set_mode(self.relay, "fast")[0])
        self.assertFalse(server.set_mode(self.relay, "")[0])

    def test_a_project_with_its_own_runner_cannot_use_parallel_mode(self):
        (self.relay / "relay-parallel.sh").write_text("# parallel\n")
        (self.relay / "relay.sh").write_text("#!/usr/bin/env bash\n")
        ok, problems = server.set_mode(self.relay, "parallel")
        self.assertFalse(ok)
        self.assertIn("own runner", problems["errors"][0])
        self.assertFalse(server.parallel_support(self.relay)["available"])

    def test_an_unreadable_settings_file_is_never_overwritten(self):
        (self.relay / "relay-parallel.sh").write_text("# parallel\n")
        (self.relay / "relay.settings.json").write_text("{not json")
        ok, problems = server.set_mode(self.relay, "parallel")
        self.assertFalse(ok)
        self.assertEqual((self.relay / "relay.settings.json").read_text(), "{not json")


if __name__ == "__main__":
    unittest.main()
