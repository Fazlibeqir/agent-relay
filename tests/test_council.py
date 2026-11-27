import pathlib
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
import appapi  # noqa: E402
import council  # noqa: E402

AIS = [("claude", "x"), ("codex", "x"), ("copilot", "x"), ("opencode", "x")]


class FakeAIs:
    """Stands in for run_ai: reads the output path out of the prompt and writes what the script says. script[(provider, kind, round)] -> text or None (write nothing)."""
    def __init__(self, script):
        self.script, self.calls = script, []

    def __call__(self, provider, found, model, effort, prompt, cwd, timeout=0):
        self.calls.append((provider, prompt))
        m = re.search(r"Write ONLY the file (\S+review-(\w+)\.md)", prompt)
        if m:
            n = int(re.search(r"rounds/(\d+)/review", m.group(1)).group(1))
            text = self.script.get((provider, "review", n), "## Missing\nnone\nVERDICT: APPROVE")
            if text is not None:
                pathlib.Path(m.group(1)).write_text(text, encoding="utf-8")
            return 0, ""
        m = re.search(r"1\. (\S+rounds/(\d+)/draft\.md) - the improved", prompt)
        if m:
            pathlib.Path(m.group(1)).write_text("revised plan", encoding="utf-8")
            pathlib.Path(m.group(1)).with_name("decisions.md").write_text("| a | b |", encoding="utf-8")
            return 0, ""
        m = re.search(r"Write (\S+rounds/(\d+)/draft\.md) with these sections", prompt)
        if m:
            text = self.script.get((provider, "draft", 1), "first plan")
            if text is not None:
                pathlib.Path(m.group(1)).write_text(text, encoding="utf-8")
            return 0, ""
        return 1, "unexpected prompt"


class CouncilTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.project = self.tmp / "game"
        (self.project / "docs").mkdir(parents=True); (self.project / "tasks").mkdir()
        (self.project / "README.md").write_text("# Game\nA football game.\n", encoding="utf-8")
        (self.project / "docs" / "PLAN.md").write_text("# Plan\nBuild it.\n", encoding="utf-8")
        (self.project / "tasks" / "T01.md").write_text("# T01 - First\nStatus: Done\nDependencies: none\n", encoding="utf-8")
        for patcher in (mock.patch.object(appapi, "APPDATA", self.tmp / "appdata"), mock.patch.object(council, "usable_ais", lambda relay: list(AIS))):
            patcher.start(); self.addCleanup(patcher.stop)

    def go(self, script, reviewers=("codex", "copilot"), rounds=3, drafter="claude", **kw):
        fake = FakeAIs(script)
        with mock.patch.object(council, "run_ai", fake):
            r = council.start(self.project, "plan it", kw.pop("source", "project"), drafter, list(reviewers), rounds, **kw)
            self.assertTrue(r["ok"], r)
            for _ in range(400):
                run = council.RUNS[r["run"]]
                if run["state"] != "running":
                    break
                time.sleep(0.05)
        return council.RUNS[r["run"]], fake

    # ---- the sources
    def test_remote_addresses(self):
        self.assertEqual(council.safe_repo_url("owner/repo"), "https://github.com/owner/repo.git")
        self.assertEqual(council.safe_repo_url("https://github.com/o/r.git"), "https://github.com/o/r.git")
        for bad in ("-x/y", "file:///etc", "git@github.com:o/r.git", "http://x.com/a", "https://x.com/../a", "https://x.com/a b", "", "a/b/c", "--upload-pack=x/y"):
            with self.assertRaises(council.PackError, msg=bad):
                council.safe_repo_url(bad)

    def test_pack_orders_caps_and_skips_generated_folders(self):
        (self.project / "node_modules").mkdir(); (self.project / "node_modules" / "x.md").write_text("junk")
        (self.project / "docs" / "PROGRESS.md").write_text("log\n" * 50000, encoding="utf-8")
        for i in range(120):
            (self.project / "tasks" / ("T%03d.md" % (i + 2))).write_text("# T%d - t\nStatus: New\nDependencies: T01\n- [x] Human signoff required\n" % (i + 2), encoding="utf-8")
        out = self.tmp / "pack"
        summary = council.build_pack([("project", self.project)], out)
        index = (out / "context" / "INDEX.md").read_text(encoding="utf-8")
        self.assertLessEqual(summary["files"], council.MAX_FILES)
        self.assertLessEqual(summary["kilobytes"] * 1024, council.MAX_TOTAL + 1024)
        self.assertNotIn("node_modules", index)
        self.assertLess(index.index("README.md"), index.index("PROGRESS.md"), "the explaining files come before the logs")
        self.assertIn("Task index (project)", index)
        self.assertIn("T01 - First | Done", index)
        self.assertIn("sign-off needed", index)
        progress = (out / "context/files/project/docs/PROGRESS.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(progress.encode()), council.MAX_FILE + 200)
        self.assertIn("left out", progress)


    def test_pack_includes_build_and_ci_files_but_not_source_code(self):
        (self.project / ".github" / "workflows").mkdir(parents=True)
        (self.project / ".github" / "workflows" / "release.yml").write_text("name: Release", encoding="utf-8")
        (self.project / "build-app.sh").write_text("echo build", encoding="utf-8")
        (self.project / "package.json").write_text("{}", encoding="utf-8")
        (self.project / "main.py").write_text("print('source')", encoding="utf-8")
        (self.project / "sub").mkdir(); (self.project / "sub" / "package.json").write_text("{}", encoding="utf-8")
        out = self.tmp / "pack2"
        council.build_pack([("project", self.project)], out)
        index = (out / "context" / "INDEX.md").read_text(encoding="utf-8")
        for wanted in ("release.yml", "build-app.sh", "project/package.json"):
            self.assertIn(wanted, index)
        self.assertNotIn("files/project/main.py", index)
        self.assertNotIn("files/project/sub/package.json", index)

    # ---- the verdict line
    def test_verdict_parsing(self):
        self.assertEqual(council.parse_verdict("x\nVERDICT: APPROVE"), ("APPROVE", 0))
        self.assertEqual(council.parse_verdict("x\nVERDICT: CHANGES 4\n"), ("CHANGES", 4))
        self.assertEqual(council.parse_verdict("verdict: changes"), ("CHANGES", 0))
        self.assertEqual(council.parse_verdict("I approve it"), (None, 0))
        self.assertEqual(council.parse_verdict("VERDICT: APPROVE\nbut also more text\nand more\nand more"), (None, 0))

    def test_call_estimate(self):
        self.assertEqual(council.plan_calls(3, 1), (4, 4))
        self.assertEqual(council.plan_calls(3, 3), (4, 12))

    # ---- the loop
    def test_everyone_approves_in_round_one(self):
        run, fake = self.go({})
        self.assertEqual(run["state"], "done")
        self.assertEqual(len(run["rounds"]), 1)
        self.assertEqual(run["final"], "rounds/1/draft.md")
        self.assertIn("approved", run["outcome"])
        self.assertEqual(len(fake.calls), 3, "one draft and two reviews, no revision")

    def test_the_drafter_never_reviews_its_own_plan(self):
        run, fake = self.go({}, reviewers=("claude", "codex"))
        self.assertEqual(run["reviewers"], ["codex"])
        self.assertNotIn("claude", [p for p, prompt in fake.calls if "reviewing a plan" in prompt])

    def test_changes_then_approval_runs_a_revision(self):
        run, fake = self.go({("codex", "review", 1): "## Missing\n- tests\nVERDICT: CHANGES 2"})
        self.assertEqual(run["state"], "done")
        self.assertEqual([r["n"] for r in run["rounds"]], [1, 2])
        self.assertEqual(run["final"], "rounds/2/draft.md")
        self.assertTrue(run["rounds"][0]["revise"])
        self.assertTrue((pathlib.Path(run["dir"]) / "rounds/2/decisions.md").is_file())

    def test_round_limit_stops_the_run(self):
        always = {("codex", "review", n): "VERDICT: CHANGES 1" for n in range(1, 5)}
        run, _ = self.go(always, rounds=2)
        self.assertEqual(run["state"], "done")
        self.assertEqual(len(run["rounds"]), 2)
        self.assertIn("Round limit", run["outcome"])
        self.assertEqual(run["final"], "rounds/2/draft.md")

    def test_a_reviewer_that_writes_nothing_is_skipped_not_fatal(self):
        run, _ = self.go({("codex", "review", 1): None})
        self.assertEqual(run["state"], "done")
        states = {r["provider"]: r["state"] for r in run["rounds"][0]["reviews"]}
        self.assertEqual(states, {"codex": "skipped", "copilot": "done"})
        self.assertIn("approved", run["outcome"])

    def test_no_reviewer_answering_is_reported_honestly(self):
        run, _ = self.go({("codex", "review", 1): None, ("copilot", "review", 1): None})
        self.assertEqual(run["state"], "done")
        self.assertIn("no second opinion", run["outcome"])

    def test_a_review_without_a_verdict_line_counts_as_wanting_changes(self):
        run, _ = self.go({("codex", "review", 1): "## Missing\n- a thing\n"}, rounds=1)
        review = [r for r in run["rounds"][0]["reviews"] if r["provider"] == "codex"][0]
        self.assertEqual((review["verdict"], review["state"]), ("CHANGES", "done"))

    def test_a_drafter_that_writes_no_plan_fails_the_run(self):
        run, _ = self.go({("claude", "draft", 1): None})
        self.assertEqual(run["state"], "failed")
        self.assertIn("wrote no plan", run["error"])

    def test_nothing_is_written_into_the_project(self):
        before = sorted(p.relative_to(self.project).as_posix() for p in self.project.rglob("*"))
        self.go({("codex", "review", 1): "VERDICT: CHANGES 1"})
        self.assertEqual(sorted(p.relative_to(self.project).as_posix() for p in self.project.rglob("*")), before)

    def test_start_refuses_unavailable_or_missing_reviewers(self):
        self.assertFalse(council.start(self.project, "g", "project", "nope", ["codex"], 2)["ok"])
        self.assertFalse(council.start(self.project, "g", "project", "claude", ["claude"], 2)["ok"])
        self.assertFalse(council.start(self.project, "g", "notes", "claude", ["codex"], 2)["ok"], "notes source needs notes")
        self.assertFalse(council.start(self.project, "g", "remote", "claude", ["codex"], 2, remote="file:///x")["ok"])

    # ---- reading results
    def test_documents_are_read_by_name_only(self):
        run, _ = self.go({})
        ok = council.read_doc(self.project, run["id"], "rounds/1/draft.md")
        self.assertTrue(ok["ok"]); self.assertEqual(ok["text"], "first plan")
        for bad in ("../run.json", "rounds/1/../../run.json", "context/INDEX.md", "rounds/x/draft.md", "/etc/passwd"):
            self.assertFalse(council.read_doc(self.project, run["id"], bad)["ok"], bad)
        self.assertFalse(council.read_doc(self.project, "../../x", "rounds/1/draft.md")["ok"])

    def test_status_hides_the_folder_and_lists_documents(self):
        run, _ = self.go({})
        st = council.status(self.project, run["id"])
        self.assertNotIn("dir", st); self.assertNotIn("stop", st)
        self.assertIn("rounds/1/draft.md", st["docs"]); self.assertIn("rounds/1/review-codex.md", st["docs"])
        self.assertEqual(st["kind"], "plan")

    def test_a_finished_run_can_be_read_back_after_a_restart(self):
        run, _ = self.go({})
        council.RUNS.clear()
        again = council.status(self.project, run["id"])
        self.assertTrue(again["ok"]); self.assertEqual(again["state"], "done")
        self.assertEqual([r["id"] for r in council.list_runs(self.project)["runs"]], [run["id"]])

    def test_save_to_project_only_when_asked_and_never_overwrites(self):
        run, _ = self.go({})
        self.assertFalse((self.project / "PLAN-REVIEW.md").exists())
        self.assertTrue(council.save_to_project(self.project, run["id"])["ok"])
        self.assertEqual((self.project / "PLAN-REVIEW.md").read_text(encoding="utf-8"), "first plan")
        self.assertFalse(council.save_to_project(self.project, run["id"])["ok"])

    def test_one_more_round_after_a_limit_revises_then_reviews_again(self):
        always = {("codex", "review", 1): "VERDICT: CHANGES 1", ("codex", "review", 2): "VERDICT: CHANGES 1"}
        run, fake = self.go(always, rounds=2)
        self.assertEqual(len(run["rounds"]), 2)
        with mock.patch.object(council, "run_ai", FakeAIs({})):
            self.assertTrue(council.extend(self.project, run["id"])["ok"])
            for _ in range(400):
                if council.RUNS[run["id"]]["state"] != "running":
                    break
                time.sleep(0.05)
        run = council.RUNS[run["id"]]
        self.assertEqual(run["state"], "done")
        self.assertEqual(run["rounds"][-1]["n"], 3)
        self.assertEqual(run["final"], "rounds/3/draft.md")

    # ---- the design review
    def test_design_review_packs_the_page_source_and_the_snapshot_and_uses_the_design_prompts(self):
        app = self.tmp / "dashboard"; app.mkdir()
        (app / "index.html").write_text("<html>page</html>", encoding="utf-8"); (app / "phone.html").write_text("<html>phone</html>", encoding="utf-8")
        fake = FakeAIs({})
        with mock.patch.object(council, "run_ai", fake):
            r = council.start(self.project, "find problems", "app", "claude", ["codex"], 1, kind="ux", app_dir=app, snapshot="# measured\nControls with no accessible name: 3")
            self.assertTrue(r["ok"], r)
            for _ in range(400):
                if council.RUNS[r["run"]]["state"] != "running":
                    break
                time.sleep(0.05)
        run = council.RUNS[r["run"]]
        rd = pathlib.Path(run["dir"])
        self.assertEqual((run["kind"], run["state"]), ("ux", "done"))
        self.assertEqual((rd / "context/files/ui/index.html").read_text(encoding="utf-8"), "<html>page</html>")
        self.assertIn("no accessible name", (rd / "context/files/ui/ui-snapshot.md").read_text(encoding="utf-8"))
        self.assertTrue(any("lead designer" in prompt for _, prompt in fake.calls))
        self.assertFalse(any("lead planner" in prompt for _, prompt in fake.calls))

    def test_design_review_without_a_project_is_possible_and_without_app_files_is_refused(self):
        self.assertFalse(council.start(None, "g", "app", "claude", ["codex"], 1, kind="ux", app_dir=None)["ok"])


if __name__ == "__main__":
    unittest.main()
