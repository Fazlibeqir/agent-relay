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


class PhaseTimerTests(unittest.TestCase):
    def relay(self, lines, running):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        relay = Path(folder.name)
        (relay / "relay.log").write_text("\n".join("[%s] %s" % (stamp(ago), msg) for ago, msg in lines) + "\n", encoding="utf-8")
        if running:
            (relay / ".relay.lock").mkdir()
        return relay

    def state(self, relay):
        return server.build_state(relay, [])

    def test_a_restarted_relay_does_not_show_the_previous_runs_phase(self):
        two_days = 2 * 86400
        relay = self.relay([(two_days, "relay started on branch agent-run"), (two_days - 5, "made 1 commit(s); running the full suite"),
                            (30, "relay started on branch agent-run")], running=True)
        phase = self.state(relay)["phase"]
        self.assertEqual(phase["kind"], "START")
        self.assertLess(phase["elapsed"], 120)
        self.assertEqual(phase["estimate"], 0)

    def test_a_phase_logged_in_the_current_run_is_shown(self):
        relay = self.relay([(600, "relay started on branch agent-run"), (500, "running baseline test suite (about 8-10 minutes)")], running=True)
        phase = self.state(relay)["phase"]
        self.assertEqual(phase["kind"], "BASELINE")
        self.assertTrue(400 < phase["elapsed"] < 600)

    def test_a_relay_asleep_because_no_task_can_be_advanced_is_waiting_not_starting(self):
        relay = self.relay([(2000, "relay started on branch agent-run"), (1500, "baseline tests pass"),
                            (600, "no task the agents can advance right now — see PLAYTEST_QUEUE.md. Checking again in 1800s.")], running=True)
        phase = self.state(relay)["phase"]
        self.assertEqual(phase["kind"], "IDLE")
        self.assertEqual(phase["estimate"], 1800)
        self.assertTrue(1100 < phase["remaining"] < 1300)
        self.assertAlmostEqual(phase["retryAt"], phase["since"] + 1800)

    def test_a_stopped_relay_still_reports_when_it_stopped(self):
        relay = self.relay([(600, "relay started on branch agent-run"), (500, "baseline tests FAIL")], running=False)
        phase = self.state(relay)["phase"]
        self.assertEqual(phase["kind"], "STOPPED")


if __name__ == "__main__":
    unittest.main()
