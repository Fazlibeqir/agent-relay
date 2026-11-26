import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
import server  # noqa: E402


class SettingsSave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.relay = Path(self.tmp.name)
        (self.relay / "AGENTS.md").write_text("# x\n")
        (self.relay / "relay.project.json").write_text(json.dumps({"testCommand": "true"}))
        (self.relay / "relay-files.sh").write_text("#!/usr/bin/env bash\n")
        shutil.copyfile(ROOT / "template" / "relay-settings.py", self.relay / "relay-settings.py")
        self.file = self.relay / "relay.settings.json"

    def saved(self):
        return json.loads(self.file.read_text(encoding="utf-8"))

    def page_save(self, body):
        """What the settings route does with the body the page posts."""
        return server.save_settings(self.relay, server.keep_unmanaged_settings(self.relay, body))

    def test_saving_from_the_page_keeps_parallel_mode_and_the_other_keys_it_does_not_show(self):
        self.file.write_text(json.dumps({"mode": "parallel", "parallel": {"workers": ["codex", "copilot"]}, "copilotMonthlyPremium": 1500,
                                         "fallbackMaxPerDay": 10}))
        ok, problems = self.page_save({"fallbackMaxPerDay": 7})
        self.assertTrue(ok, problems)
        saved = self.saved()
        self.assertEqual(saved["fallbackMaxPerDay"], 7)
        self.assertEqual(saved["mode"], "parallel")
        self.assertEqual(saved["parallel"], {"workers": ["codex", "copilot"]})
        self.assertEqual(saved["copilotMonthlyPremium"], 1500)

    def test_a_key_the_page_manages_can_still_be_removed_and_a_key_the_body_names_wins(self):
        self.file.write_text(json.dumps({"mode": "parallel", "idleChores": ["backend"], "fallbackBuilders": ["copilot"]}))
        ok, problems = self.page_save({"mode": "serial", "fallbackBuilders": []})
        self.assertTrue(ok, problems)
        saved = self.saved()
        self.assertEqual(saved["mode"], "serial")            # the body names it: it wins
        self.assertNotIn("idleChores", saved)                # managed by the page and left out of the body: it is removed as before
        self.assertEqual(saved["fallbackBuilders"], [])

    def test_no_settings_file_yet_or_an_unreadable_one_saves_the_body_as_it_is(self):
        ok, problems = self.page_save({"fallbackMaxPerDay": 5})
        self.assertTrue(ok, problems)
        self.assertEqual(self.saved(), {"fallbackMaxPerDay": 5})
        self.file.write_text("{not json")
        ok, problems = self.page_save({"fallbackMaxPerDay": 6})
        self.assertTrue(ok, problems)
        self.assertEqual(self.saved(), {"fallbackMaxPerDay": 6})

    def test_a_body_that_is_not_an_object_is_left_for_the_validator(self):
        self.assertEqual(server.keep_unmanaged_settings(self.relay, [1]), [1])


if __name__ == "__main__":
    unittest.main()
