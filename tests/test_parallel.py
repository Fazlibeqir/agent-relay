import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "template"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parallel = load("relay_parallel", TEMPLATE / "relay-parallel.py")
settings = load("relay_settings_parallel", TEMPLATE / "relay-settings.py")

OPEN = "- [ ] AUTO: the check passes\n- [ ] HUMAN: try it\n"
DONE = "- [x] AUTO: the check passes\n- [x] HUMAN: tried it\n"


class ParallelPlanning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "tasks").mkdir()

    def task(self, ident, body=OPEN, extra=""):
        (self.repo / "tasks" / (ident + ".md")).write_text("# %s - Title %s\nStatus: Not started\n%s\n%s" % (ident, ident, extra, body), encoding="utf-8")

    def ids(self):
        return [c["id"] for c in parallel.candidates(self.repo)]

    def test_open_tasks_with_finished_dependencies_are_candidates(self):
        self.task("T01", DONE)
        self.task("T02", OPEN, "Depends on T01 setup.")
        self.task("T03")
        self.assertEqual(self.ids(), ["T02", "T03"])

    def test_a_task_waiting_on_an_open_task_is_not_a_candidate(self):
        self.task("T01")
        self.task("T02", OPEN, "Phase 2. Depends on T01 and T09 clock,\nT10 results.")
        self.task("T03")
        self.assertEqual(self.ids(), ["T01", "T03"])

    def test_a_dependency_on_the_next_line_counts_and_self_mentions_do_not(self):
        self.task("T01")
        self.task("T02", OPEN, "Depends on\nT01 first. T02 is this task.")
        self.assertEqual(self.ids(), ["T01"])

    def test_parked_tasks_are_left_out(self):
        self.task("T01")
        self.task("T02")
        (self.repo / "PLAYTEST_QUEUE.md").write_text("T01 — Title — NEEDS OWNER: needs hardware\n", encoding="utf-8")
        self.assertEqual(self.ids(), ["T02"])

    def test_count_is_the_number_of_candidates(self):
        self.task("T01")
        self.task("T02", OPEN, "Depends on T01.")
        self.assertEqual(len(parallel.candidates(self.repo)), 1)
        self.task("T03")
        self.assertEqual(len(parallel.candidates(self.repo)), 2)

    def test_the_prompt_lists_candidates_and_allowed_builders(self):
        self.task("T01")
        self.task("T02", OPEN, "Depends on T01.")
        self.task("T03")
        text = parallel.prompt(self.repo, ["codex", "copilot"])
        self.assertIn("- T01 |", text)
        self.assertIn("- T03 |", text)
        self.assertNotIn("- T02 |", text)
        self.assertIn("codex, copilot", text)
        self.assertIn('"lanes"', text)

    def plan(self, reply, workers=("codex", "copilot", "opencode")):
        return parallel.plan(self.repo, list(workers), reply)

    def test_a_valid_plan_is_accepted_inside_prose_and_code_fences(self):
        for n in range(1, 4): self.task("T0%d" % n)
        reply = 'Here you go:\n```json\n{"lanes": [{"task": "T01", "builder": "codex"}, {"task": "T03", "builder": "Copilot"}], "reason": "disjoint"}\n```'
        self.assertEqual(self.plan(reply), [("T01", "codex"), ("T03", "copilot")])

    def test_unsafe_lanes_are_dropped(self):
        for n in range(1, 6): self.task("T0%d" % n)
        self.task("T06", DONE)
        reply = json.dumps({"lanes": [
            {"task": "T01", "builder": "codex"},
            {"task": "T01", "builder": "copilot"},       # the same task twice
            {"task": "T02", "builder": "codex"},         # the same builder twice
            {"task": "T06", "builder": "opencode"},      # finished
            {"task": "T99", "builder": "opencode"},      # does not exist
            {"task": "T03", "builder": "claude"},        # the reviewer does not build in parallel
            {"task": "T04", "builder": "copilot"},
        ]})
        self.assertEqual(self.plan(reply), [("T01", "codex"), ("T04", "copilot")])

    def test_a_dependent_task_cannot_be_planned_with_the_task_it_needs(self):
        self.task("T01")
        self.task("T02", OPEN, "Depends on T01.")
        reply = json.dumps({"lanes": [{"task": "T01", "builder": "codex"}, {"task": "T02", "builder": "copilot"}]})
        self.assertEqual(self.plan(reply), [])

    def test_fewer_than_two_lanes_or_a_bad_reply_means_no_parallel_round(self):
        self.task("T01")
        self.task("T02")
        self.assertEqual(self.plan(json.dumps({"lanes": [{"task": "T01", "builder": "codex"}]})), [])
        self.assertEqual(self.plan("not json"), [])
        self.assertEqual(self.plan('{"lanes": "T01"}'), [])

    def test_a_builder_that_is_not_in_the_worker_list_is_refused(self):
        self.task("T01")
        self.task("T02")
        reply = json.dumps({"lanes": [{"task": "T01", "builder": "codex"}, {"task": "T02", "builder": "opencode"}]})
        self.assertEqual(self.plan(reply, workers=("codex", "copilot")), [])

    def test_task_body_matches_what_the_picker_writes(self):
        self.task("T01")
        body = parallel.task_body(self.repo, "T01")
        self.assertTrue(body.startswith("tasks/T01.md\nT01 — Title T01\nRECOMMENDED_AI: \n\n# T01 - Title T01"))
        self.assertIsNone(parallel.task_body(self.repo, "T77"))


class ParallelSettings(unittest.TestCase):
    def check(self, raw):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "relay.settings.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            return settings.effective(path)

    def test_serial_is_the_default_and_parallel_is_accepted(self):
        config, problems, ok = self.check({})
        self.assertTrue(ok, problems)
        self.assertEqual(config["mode"], "serial")
        config, problems, ok = self.check({"mode": "parallel", "parallel": {"workers": ["codex", "opencode"]}})
        self.assertTrue(ok, problems)
        self.assertEqual((config["mode"], config["parallel"]["workers"]), ("parallel", ["codex", "opencode"]))

    def test_bad_values_keep_the_defaults(self):
        for raw in ({"mode": "fast"}, {"parallel": {"workers": ["claude"]}}, {"parallel": {"workers": ["codex", "codex"]}}, {"parallel": "codex"}):
            config, problems, ok = self.check(raw)
            self.assertFalse(ok, raw)
            self.assertEqual(config["mode"], "serial")


if __name__ == "__main__":
    unittest.main()
