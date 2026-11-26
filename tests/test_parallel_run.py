"""End-to-end tests of parallel mode: the real relay-files.sh and relay-parallel.sh in a real git repository, with mocked AI command-line tools."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
import appapi  # noqa: E402

CLAUDE = r'''#!/usr/bin/env bash
prompt=$(cat)
if grep -q "^PARALLEL ROUND" <<<"$prompt"; then
  echo dispatcher >> "$MOCK_ROOT/roles.txt"
  python -c 'import json, os; print(json.dumps({"type": "result", "result": os.environ.get("MOCK_PLAN", "no plan")}))'
  exit 0
fi
echo reviewer >> "$MOCK_ROOT/roles.txt"
printf '%s
' "$prompt" >> "$MOCK_ROOT/review-prompts.txt"
f=$(grep -o 'tasks/T[0-9a-z]*\.md' <<<"$prompt" | head -n 1)
python - "$f" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); p.write_text(p.read_text().replace("[ ]", "[x]"))
for name in ("CURRENT_TASK.md", "REVIEW.md"):
    q = pathlib.Path(name)
    if q.exists(): q.unlink()
PY
printf '%s\n' '{"type":"result","result":"reviewed"}'
'''

BUILDER = r'''#!/usr/bin/env bash
tool=$(basename "$0")
cat > "$MOCK_ROOT/builder-prompt-$tool.txt"
id=$(sed -n 1p CURRENT_TASK.md | sed 's#tasks/##; s#\.md##' | tr -d '\r')
mode_var="MOCK_$(echo "$tool" | tr a-z A-Z)"
if [ "${!mode_var:-ok}" = limit ]; then printf '%s\n' "You've hit your weekly limit · resets Nov 20, 12pm"; exit 1; fi
echo "$tool $id start $(date +%s.%N) $PWD" >> "$MOCK_ROOT/times.txt"
sleep "${MOCK_SLEEP:-2}"
if [ -n "${MOCK_SHARED:-}" ]; then echo "$tool-$id" > shared.txt; files=shared.txt; else echo "built by $tool" > "src_$id.py"; files="src_$id.py"; fi
printf '%s: built by %s\nFILES:\n%s\n' "$id" "$tool" "$files" > relay-commit.txt
echo "$tool $id end $(date +%s.%N)" >> "$MOCK_ROOT/times.txt"
printf '%s\n' 'OpenAI Codex v1.0' '--------' 'workdir: lane' 'model: gpt-test' 'provider: openai' 'session id: lane-session' '--------' "$tool built $id"
'''

PLAN = {"lanes": [{"task": "T01", "builder": "codex"}, {"task": "T02", "builder": "copilot"}], "reason": "disjoint"}


class ParallelRun(unittest.TestCase):
    def setUp(self):
        self.bash = os.environ.get("BASH_BIN") or (r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash"))
        if not self.bash or not Path(self.bash).exists():
            self.skipTest("Git Bash or bash is required")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "project"
        self.root.mkdir()
        self.mock = base / "mock"
        self.mock.mkdir()
        self.git("init", "-q", "-b", "agent-run")
        self.git("config", "user.name", "Relay Test")
        self.git("config", "user.email", "test@localhost")
        for name in appapi.RELAY_FILES:
            shutil.copyfile(ROOT / "template" / name, self.root / name)
        (self.root / "AGENTS.md").write_text("# Fixture\n")
        (self.root / "tasks").mkdir()
        for n in (1, 2, 3):
            (self.root / "tasks" / ("T0%d.md" % n)).write_text("# T0%d - Task %d\nStatus: Not started\n- [ ] AUTO: it works\n- [ ] HUMAN: try it\n" % (n, n))
        (self.root / ".gitignore").write_text("\n".join(appapi.GITIGNORE) + "\n.relay/\nrelay.settings.json\n" + "\n".join(appapi.RELAY_FILES) + "\n")
        self.set_test_command("true")
        for tool, text in (("claude", CLAUDE), ("codex", BUILDER), ("copilot", BUILDER), ("opencode", BUILDER)):
            path = self.mock / tool
            path.write_text(text, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        self.settings({})

    def git(self, *args, cwd=None):
        result = subprocess.run(["git", "-C", str(cwd or self.root), *args], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def commit_relay_scripts(self):
        """Real projects commit the relay scripts, so every lane worktree gets its own copy of relay-runtime.py and the rest."""
        ignore = self.root / ".gitignore"
        kept = [line for line in ignore.read_text().splitlines() if line not in appapi.RELAY_FILES]
        ignore.write_text(chr(10).join(kept) + chr(10))
        self.git("add", "-f", *appapi.RELAY_FILES, ".gitignore")
        self.git("commit", "-qm", "Add the Agent Relay scripts")

    def set_test_command(self, command):
        (self.root / "relay.project.json").write_text(json.dumps({"testCommand": command}))
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture " + command)

    def settings(self, extra):
        config = json.loads((ROOT / "relay.settings.example.json").read_text())
        config.update({"mode": "parallel", "parallel": {"workers": ["codex", "copilot"]}, "fallbackBuilders": []})
        config["fallbackRoles"] = {"dispatcher": [], "reviewer": []}
        config["models"] = {**config["models"], "copilot": "copilot-test"}      # a named model: the reviewer rules need to know who built the work
        config.update(extra)
        (self.root / "relay.settings.json").write_text(json.dumps(config))

    def run_relay(self, **env):
        self.git("add", "-A")
        self.git("commit", "-qm", "ready", "--allow-empty")
        full = {**os.environ, "MAX_ROUNDS": "1", "AGENT_TIMEOUT": "60", "MOCK_ROOT": self.mock.as_posix(), "MOCK_PLAN": json.dumps(PLAN),
                "CLAUDE_BIN": (self.mock / "claude").as_posix(), "CODEX_BIN": (self.mock / "codex").as_posix(),
                "COPILOT_BIN": (self.mock / "copilot").as_posix(), "OPENCODE_BIN": (self.mock / "opencode").as_posix(), **env}
        return subprocess.run([self.bash, "./relay-files.sh", "."], cwd=self.root, env=full, capture_output=True, text=True, encoding="utf-8", timeout=180)

    def subjects(self):
        return self.git("log", "--format=%s").splitlines()

    def roles(self):
        path = self.mock / "roles.txt"
        return path.read_text().split() if path.exists() else []

    def times(self):
        spans = {}
        for line in (self.mock / "times.txt").read_text().splitlines():
            tool, ident, kind, stamp = line.split()[:4]
            spans.setdefault(ident, {})[kind] = float(stamp)
        return spans

    def lane_leftovers(self):
        lanes = Path(str(self.root) + "-lanes")
        branches = [b for b in self.git("branch", "--format=%(refname:short)").splitlines() if "-lane-" in b]
        return (list(lanes.iterdir()) if lanes.exists() else []), branches

    def task_text(self, ident):
        return (self.root / "tasks" / (ident + ".md")).read_text()

    def test_two_builders_work_at_once_and_each_result_is_merged_tested_and_reviewed(self):
        result = self.run_relay()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.roles().count("dispatcher"), 1)
        self.assertEqual(self.roles().count("reviewer"), 2)
        spans = self.times()
        self.assertEqual(set(spans), {"T01", "T02"})
        self.assertLess(max(s["start"] for s in spans.values()), min(s["end"] for s in spans.values()), "the two builds did not overlap: " + str(spans))
        subjects = self.subjects()
        self.assertIn("relay: merge T01 (codex build)", subjects)
        self.assertIn("relay: merge T02 (copilot build)", subjects)
        self.assertEqual((self.root / "src_T01.py").read_text().strip(), "built by codex")
        self.assertEqual((self.root / "src_T02.py").read_text().strip(), "built by copilot")
        self.assertIn("[x] AUTO", self.task_text("T01"))
        self.assertIn("[ ] HUMAN", self.task_text("T01"))
        self.assertIn("[x] AUTO", self.task_text("T02"))
        self.assertIn("[ ] AUTO", self.task_text("T03"))
        self.assertEqual(self.lane_leftovers(), ([], []))
        self.assertFalse((self.root / "src_T03.py").exists())
        self.assertFalse((self.root / ".relay.lock").exists())
        prompts = (self.mock / "review-prompts.txt").read_text()
        self.assertIn("Built by: codex (gpt-test)", prompts)        # the reviewer is told who built each task, and records it in the review log
        self.assertIn("Built by: copilot (copilot-test)", prompts)
        self.assertIn("Start that sentence with 'Built by <who>.'", prompts)

    def test_a_project_that_commits_its_relay_scripts_works_the_same(self):
        self.commit_relay_scripts()
        result = self.run_relay()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        subjects = self.subjects()
        self.assertIn("relay: merge T01 (codex build)", subjects)
        self.assertIn("relay: merge T02 (copilot build)", subjects)
        self.assertEqual(self.roles().count("reviewer"), 2)
        self.assertIn("[x] AUTO", self.task_text("T01"))
        self.assertIn("[x] AUTO", self.task_text("T02"))
        self.assertEqual(self.lane_leftovers(), ([], []))
        # the lane's own runtime stayed out of it: the project's runtime recorded each build once, with the model the tool reported
        state = json.loads((self.root / ".relay/state.json").read_text())
        builders = sorted((c["provider"], c.get("reportedModel") or c["requestedModel"]) for c in state["calls"] if c["role"] == "builder")
        self.assertEqual(builders, [("codex", "gpt-test"), ("copilot", "copilot-test")])

    def test_copilot_with_its_automatic_model_can_build_a_lane_and_is_recorded_as_such(self):
        self.settings({"models": {"copilot": "auto", "opencode": "opencode/deepseek-v4.1-flash-free", "advisor": "claude-opus-5-5"}})
        result = self.run_relay()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("relay: merge T02 (copilot build)", self.subjects())
        self.assertEqual(self.roles().count("reviewer"), 2)
        state = json.loads((self.root / ".relay/state.json").read_text())
        models = {c["provider"]: c.get("reportedModel") or c["requestedModel"] for c in state["calls"] if c["role"] == "builder"}
        self.assertEqual(models["copilot"], "copilot-auto")
        self.assertIn("Built by: copilot (chosen automatically by Copilot)", (self.mock / "review-prompts.txt").read_text())

    def test_the_lane_prompt_names_its_own_branch_and_does_not_ask_for_the_whole_suite(self):
        self.run_relay()
        for tool, ident in (("codex", "T01"), ("copilot", "T02")):
            prompt = (self.mock / ("builder-prompt-" + tool + ".txt")).read_text()
            self.assertIn("current git branch (agent-run-lane-%s)" % ident, prompt)
            self.assertNotIn("Run that command yourself before you finish", prompt)
            self.assertIn("do NOT run the whole suite", prompt)

    def test_the_lanes_run_in_their_own_worktrees_not_the_project_folder(self):
        self.run_relay()
        starts = [line for line in (self.mock / "times.txt").read_text().splitlines() if " start " in line]
        self.assertEqual(len(starts), 2)
        for line in starts:
            self.assertIn("-lanes", line.split(" ", 4)[4])

    def test_a_lane_whose_tests_fail_after_merging_is_undone_and_the_other_lane_stays(self):
        self.set_test_command("test ! -f src_T02.py")
        result = self.run_relay()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "src_T01.py").exists())
        self.assertFalse((self.root / "src_T02.py").exists(), "the failed merge was not undone")
        self.assertIn("[ ] AUTO", self.task_text("T02"))
        self.assertTrue((self.root / ".relay/lane-feedback/T02.md").exists())
        self.assertIn("project tests failed", (self.root / ".relay/lane-feedback/T02.md").read_text())
        self.assertEqual(self.roles().count("reviewer"), 1)
        self.assertEqual(self.lane_leftovers(), ([], []))
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")

    def test_two_lanes_that_edit_the_same_file_do_not_corrupt_the_branch(self):
        result = self.run_relay(MOCK_SHARED="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        merged = [s for s in self.subjects() if s.startswith("relay: merge")]
        self.assertEqual(len(merged), 1, merged)
        self.assertNotIn("<<<<<<<", (self.root / "shared.txt").read_text())
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(self.roles().count("reviewer"), 1)
        self.assertEqual(self.lane_leftovers(), ([], []))
        left_open = [i for i in ("T01", "T02") if "[ ] AUTO" in self.task_text(i)]
        self.assertEqual(len(left_open), 1)

    def test_a_builder_out_of_usage_ends_its_lane_and_the_round_goes_on(self):
        result = self.run_relay(MOCK_COPILOT="limit")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[x] AUTO", self.task_text("T01"))
        self.assertIn("[ ] AUTO", self.task_text("T02"))
        self.assertTrue((self.root / "relay-provider-copilot.limit").exists())
        self.assertEqual(self.roles().count("reviewer"), 1)
        self.assertEqual(self.lane_leftovers(), ([], []))

    def test_an_invalid_plan_falls_back_to_one_task_at_a_time(self):
        result = self.run_relay(MOCK_PLAN="I cannot decide")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.roles().count("dispatcher"), 1)
        self.assertFalse([s for s in self.subjects() if s.startswith("relay: merge")])
        self.assertIn("[x] AUTO", self.task_text("T01"))      # built by the normal builder (codex) and reviewed
        self.assertIn("[ ] AUTO", self.task_text("T02"))
        self.assertEqual(self.lane_leftovers(), ([], []))

    def test_serial_mode_never_plans_or_makes_lanes(self):
        self.settings({"mode": "serial"})
        result = self.run_relay()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.roles().count("dispatcher"), 0)
        self.assertEqual(self.lane_leftovers(), ([], []))

    def test_with_fewer_than_two_open_tasks_the_dispatcher_is_not_asked(self):
        for name in ("T02", "T03"):
            lines = ["# %s - Done" % name, "Status: Done", "- [x] AUTO: ok", "- [x] HUMAN: ok", ""]
            (self.root / "tasks" / (name + ".md")).write_text(chr(10).join(lines))
        result = self.run_relay()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.roles().count("dispatcher"), 0)
        self.assertIn("fewer than two tasks can start now", result.stdout)

    def test_one_available_builder_means_serial(self):
        self.settings({"parallel": {"workers": ["codex"]}})
        result = self.run_relay()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.roles().count("dispatcher"), 0)
        self.assertFalse([s for s in self.subjects() if s.startswith("relay: merge")])


if __name__ == "__main__":
    unittest.main()
