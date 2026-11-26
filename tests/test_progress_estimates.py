import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
import phases  # noqa: E402
import server  # noqa: E402

HOUR = 3600


def stamp(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


class Log:
    """A relay.log written with absolute times, so a test can describe several rounds without waiting."""

    def __init__(self, test):
        self.dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.dir.cleanup)
        self.relay = Path(self.dir.name)
        self.lines = []
        self.t = time.time() - 40 * HOUR

    def at(self, after, msg):
        self.t += after
        self.lines.append("[%s] %s" % (stamp(self.t), msg))
        return self

    def noise(self, megabytes):
        """Output of the test runner between two timestamped lines (the game's log is 70 MB of it)."""
        self.lines.append(("Godot output line that is not a timestamped relay message\n" * 60000)[: int(megabytes * 1_000_000)])
        return self

    def write(self):
        (self.relay / "relay.log").write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        return self.relay


def serial_round(log, build, suite, review, commits=1, dispatch=60, provider="Codex"):
    log.at(1, "round 1 — choosing next task from Notion")
    log.at(1, "role dispatcher running claude claude-opus-5-5")
    log.at(dispatch, "round 1 — T50 — Something — %s building" % provider)
    log.at(build, "round 1 — %s made %d commit(s); running the full suite for the review (about 9–10 minutes)" % (provider, commits))
    log.at(suite, "round 1 — headless suite PASS, graphics PASS; Claude reviewing")
    log.at(1, "role reviewer running claude claude-sonnet-5-5")
    log.at(review, "round 1 — review done: relay: loop state")


def obs_of(log):
    return phases.observations(phases.history(log.write()), server.kind_of)


class HistoryWindow(unittest.TestCase):
    def test_estimates_use_rounds_far_older_than_the_last_megabytes_of_the_log(self):
        log = Log(self)
        for build in (600, 700, 800):
            serial_round(log, build=build, suite=500, review=50)
            log.noise(2.0)       # 2 MB of test output after every round: the old 1.5 MB tail never saw more than the last line or two
        relay = log.write()
        self.assertGreater((relay / "relay.log").stat().st_size, 6_000_000)
        old_tail = server.parse_events(relay)
        self.assertLess(len(old_tail), 6)
        est, _ = phases.estimates(phases.observations(phases.history(relay), server.kind_of), server.DEFAULTS)
        self.assertEqual(est["BUILD"], 700)
        self.assertEqual(est["SUITE"], 500)

    def test_a_second_read_only_reads_what_was_added_and_a_rewritten_log_starts_over(self):
        log = Log(self)
        serial_round(log, build=600, suite=500, review=50)
        relay = log.write()
        first = phases.history(relay)
        self.assertEqual(len(phases.history(relay)), len(first))
        with open(relay / "relay.log", "a", encoding="utf-8") as f:
            f.write("[%s] round 2 — choosing next task from Notion\n" % stamp(log.t + 10))
            f.write("[%s] partial line without a newline yet" % stamp(log.t + 20))
        again = phases.history(relay)
        self.assertEqual(len(again), len(first) + 1)
        (relay / "relay.log").write_text("[%s] relay started on branch x\n" % stamp(time.time()), encoding="utf-8")
        self.assertEqual(len(phases.history(relay)), 1)


class CleanSamples(unittest.TestCase):
    def test_a_build_that_made_no_commits_is_not_a_build_sample(self):
        log = Log(self)
        serial_round(log, build=900, suite=500, review=50)
        serial_round(log, build=85, suite=500, review=50, commits=0)
        obs = obs_of(log)
        self.assertEqual(obs["BUILD"], [900])

    def test_a_serial_build_is_judged_by_its_own_builder_and_needs_two_samples(self):
        log = Log(self)
        serial_round(log, build=1200, suite=500, review=50)
        obs = obs_of(log)
        est, _ = phases.estimates(obs, server.DEFAULTS)
        self.assertEqual(obs["BUILD:codex"], [1200])
        self.assertIsNone(phases.build_estimate(obs, est, "codex")[0])          # one sample is not enough to judge a builder
        serial_round(log, build=1000, suite=500, review=50)
        obs = obs_of(log)
        self.assertEqual(phases.build_estimate(obs, est, "codex")[0], 1100)
        self.assertIsNone(phases.build_estimate(obs, est, "opencode")[0])      # a builder never seen keeps the all-builders estimate

    def test_a_suite_that_was_rerun_after_the_known_flake_is_not_a_clean_suite_sample(self):
        log = Log(self)
        serial_round(log, build=600, suite=500, review=50)
        log.at(1, "round 2 — Codex made 1 commit(s); running the full suite for the review")
        log.at(60, "suite failed at the T72 loopback momentum cell (known timing flake) — rerunning the whole suite once")
        log.at(1500, "round 2 — headless suite PASS, graphics PASS; Claude reviewing")
        log.at(1, "role reviewer running claude claude-sonnet-5-5")
        log.at(40, "round 2 — review done")
        self.assertEqual(obs_of(log)["SUITE"], [500])

    def test_the_all_builders_done_line_is_a_merge_not_a_review(self):
        self.assertEqual(server.kind_of("round 1 — all builders done after 682s; merging, testing and reviewing one lane at a time"), "MERGE")

    def test_parallel_lanes_are_kept_per_builder_and_the_round_build_is_the_slowest_lane(self):
        log = Log(self)
        for _ in range(2):
            log.at(1, "round 1 — parallel mode: the dispatcher plans a round for codex copilot opencode")
            log.at(1, "role dispatcher running claude claude-opus-5-5")
            log.at(300, "round 1 — T62: copilot builds in its own worktree")
            log.at(0, "round 1 — T91: codex builds in its own worktree")
            log.at(390, "[T62] committed for Codex: f851344 T62: something (13 files)")
            log.at(293, "[T91] committed for Codex: 4d71a4f T91: something (13 files)")
            log.at(1, "round 1 — all builders done after 683s; merging, testing and reviewing one lane at a time")
            log.at(1, "round 1 — T62 — Title — copilot builder: merged 2 commit(s); running the full suite for the review")
            log.at(500, "round 1 — headless suite PASS, graphics PASS; Claude reviewing")
            log.at(1, "role reviewer running claude claude-sonnet-5-5")
            log.at(40, "round 1 — review done")
        obs = obs_of(log)
        self.assertEqual(obs["PLANE:copilot"], [390, 390])
        self.assertEqual(obs["PLANE:codex"], [683, 683])
        self.assertNotIn("BUILD", obs)           # a parallel build is never mixed into the serial samples
        est, _ = phases.estimates(obs, server.DEFAULTS)
        self.assertEqual(phases.build_estimate(obs, est, lane_providers=["copilot", "codex"])[0], 683)


class LiveProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.relay = Path(self.tmp.name)
        self.store = self.relay / "phase-checks.json"
        self._store, server.CHECKS_STORE = server.CHECKS_STORE, self.store
        self.addCleanup(lambda: setattr(server, "CHECKS_STORE", self._store))

    def log(self, lines, running=True):
        now = time.time()
        (self.relay / "relay.log").write_text("\n".join("[%s] %s" % (stamp(now - ago), msg) for ago, msg in lines) + "\n", encoding="utf-8")
        if running: (self.relay / ".relay.lock").mkdir(exist_ok=True)

    def checks(self, n):
        (self.relay / "relay-tests.log").write_text("".join("[checks] check %d\nsome Godot output\n" % i for i in range(1, n + 1)), encoding="utf-8")

    def test_the_checks_are_shown_against_the_size_of_the_last_runs_but_do_not_move_the_clock(self):
        phases.note_checks(self.store, self.relay, 1, 200)           # an earlier complete run: 200 checks
        phases.note_checks(self.store, self.relay, 2, 40)            # a later one that failed early
        self.log([(900, "relay started on branch x"), (300, "running baseline test suite (about 8–10 minutes)")])
        self.checks(100)
        phase = server.build_state(self.relay, [])["phase"]
        self.assertEqual(phase["kind"], "BASELINE")
        self.assertEqual((phase["checks"]["done"], phase["checks"]["total"]), (100, 200))
        self.assertEqual(phase["basis"], "history")
        self.assertNotIn("progress", phase)        # measured on the real game suite: the last third of the checks takes half of the time
        self.assertAlmostEqual(phase["remaining"], phase["estimate"] - phase["elapsed"], delta=1)

    def test_a_run_that_prints_nothing_for_minutes_is_reported_as_quiet(self):
        import os
        self.log([(2400, "relay started on branch x"), (2000, "round 1 — Codex made 1 commit(s); running the full suite for the review")])
        self.checks(60)
        stale = time.time() - 900
        os.utime(self.relay / "relay-tests.log", (stale, stale))
        phase = server.build_state(self.relay, [])["phase"]
        self.assertEqual(phase["kind"], "SUITE")
        self.assertGreater(phase["quiet"], 800)
        self.checks(61)
        self.assertNotIn("quiet", server.build_state(self.relay, [])["phase"])

    def test_a_run_with_nothing_to_compare_with_remembers_its_size_for_the_next_one(self):
        self.log([(900, "relay started on branch x"), (300, "running baseline test suite (about 8–10 minutes)")])
        self.checks(60)
        phase = server.build_state(self.relay, [])["phase"]
        self.assertIsNone(phase["checks"]["total"])
        self.log([(900, "relay started on branch x"), (100, "running baseline test suite (about 8–10 minutes)")])      # the next run
        self.checks(5)
        self.assertEqual(server.build_state(self.relay, [])["phase"]["checks"]["total"], 60)

    def test_a_rerun_after_the_flake_starts_its_own_clock(self):
        self.log([(2000, "relay started on branch x"), (1900, "round 1 — Codex made 1 commit(s); running the full suite for the review"),
                  (100, "suite failed at the T72 loopback momentum cell (known timing flake) — rerunning the whole suite once")])
        phase = server.build_state(self.relay, [])["phase"]
        self.assertEqual(phase["kind"], "SUITE")
        self.assertLess(phase["elapsed"], 130)

    def test_the_log_of_an_older_run_is_not_counted_as_this_runs_progress(self):
        phases.note_checks(self.store, self.relay, 1, 200)
        self.checks(150)
        old = time.time() - 3 * HOUR
        import os
        os.utime(self.relay / "relay-tests.log", (old, old))
        self.log([(900, "relay started on branch x"), (60, "running baseline test suite (about 8–10 minutes)")])
        phase = server.build_state(self.relay, [])["phase"]
        self.assertEqual(phase["checks"]["done"], 0)
        self.assertNotIn("progress", phase)


class ParallelRound(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.relay = Path(self.tmp.name)

    def log(self, lines):
        now = time.time()
        (self.relay / "relay.log").write_text("\n".join("[%s] %s" % (stamp(now - ago), msg) for ago, msg in lines) + "\n", encoding="utf-8")
        (self.relay / ".relay.lock").mkdir(exist_ok=True)

    PLAN = [(900, "relay started on branch x"), (800, "round 1 — parallel mode: the dispatcher plans a round for codex copilot"),
            (700, "round 1 — T62: copilot builds in its own worktree"), (700, "round 1 — T91: codex builds in its own worktree")]

    def test_a_parallel_round_counts_every_lane_still_to_merge_test_and_review(self):
        self.log(self.PLAN)
        state = server.build_state(self.relay, [])
        self.assertEqual(state["phase"]["kind"], "BUILD")
        self.assertEqual(state["lanesLeft"], 1)
        e = state["estimates"]
        self.assertAlmostEqual(state["roundDoneIn"], max(0, state["phase"]["remaining"]) + 2 * (e["MERGE"] + e["SUITE"] + e["REVIEW"]), delta=1)

    def test_the_second_lane_is_still_to_come_while_the_first_is_in_its_suite(self):
        self.log(self.PLAN + [(300, "round 1 — all builders done after 400s; merging, testing and reviewing one lane at a time"),
                              (250, "round 1 — T62 — Title — copilot builder: merged 2 commit(s); running the full suite for the review")])
        state = server.build_state(self.relay, [])
        self.assertEqual(state["phase"]["kind"], "SUITE")
        self.assertEqual(state["lanesLeft"], 1)
        self.log(self.PLAN + [(300, "round 1 — all builders done after 400s; merging, testing and reviewing one lane at a time"),
                              (250, "round 1 — T62 — Title — copilot builder: merged 2 commit(s); running the full suite for the review"),
                              (100, "round 1 — review done: relay: loop state"),
                              (50, "round 1 — T91 — Title — codex builder: merged 2 commit(s); running the full suite for the review")])
        self.assertEqual(server.build_state(self.relay, [])["lanesLeft"], 0)

    def test_planning_a_parallel_round_is_timed_as_two_lanes_whose_build_is_the_slowest_lane(self):
        (self.relay / "relay.settings.json").write_text(json.dumps({"mode": "parallel"}), encoding="utf-8")
        self.log([(5000, "relay started on branch x"), (4000, "round 1 — T62: copilot builds in its own worktree"),
                  (3900, "round 1 — all builders done after 683s; merging, testing and reviewing one lane at a time"),
                  (3000, "round 1 — review done"), (100, "round 2 — parallel mode: the dispatcher plans a round for codex copilot")])
        state = server.build_state(self.relay, [])
        e = state["estimates"]
        self.assertEqual(state["phase"]["kind"], "DISPATCH")
        self.assertEqual(state["lanesLeft"], 1)
        expected = max(0, state["phase"]["remaining"]) + 683 + 2 * (e["MERGE"] + e["SUITE"] + e["REVIEW"])
        self.assertAlmostEqual(state["roundDoneIn"], expected, delta=1)

    def test_the_merge_step_is_its_own_phase_and_a_serial_round_has_none(self):
        self.log(self.PLAN + [(20, "round 1 — all builders done after 400s; merging, testing and reviewing one lane at a time")])
        phase = server.build_state(self.relay, [])["phase"]
        self.assertEqual((phase["kind"], phase["label"]), ("MERGE", "Merging the builders' work"))
        self.log([(900, "relay started on branch x"), (60, "round 1 — T50 — Title — Codex building")])
        state = server.build_state(self.relay, [])
        self.assertEqual([n["label"] for n in state["next"]], ["Full test suite", "Reviewing"])
        self.assertEqual(state["lanesLeft"], 0)


if __name__ == "__main__":
    unittest.main()
