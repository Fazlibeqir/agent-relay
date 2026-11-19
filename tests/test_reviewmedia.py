import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
import appapi
import digest
import reviewmedia
import server

# Header-only container fixture. Real encoded playback is covered by browser QA.
WEBM = b"\x1a\x45\xdf\xa3" + b"relay-unit-fixture" * 4
MP4 = b"\x00\x00\x00\x18ftypisom" + b"relay-unit-fixture"


class ReviewMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "tasks").mkdir()
        self.task = self.root / "tasks/T01.md"
        self.task.write_text("# T01 - Review\n- [x] AUTO: fixture\n- [ ] HUMAN: inspect workflow\n")
        (self.root / "relay-files.sh").write_text("#!/usr/bin/env bash\n")
        (self.root / "relay.project.json").write_text('{"testCommand":"echo fixture"}')
        (self.root / ".gitignore").write_text(".relay/\nrelay-tests.log\nCURRENT_TASK.md\nPLAYTEST_QUEUE.md\n")
        for args in (("init", "-q", "-b", "agent-run"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@localhost"), ("add", "."), ("commit", "-qm", "fixture")):
            self.git(*args)
        self.context = reviewmedia.context(self.root, "T01")

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def save(self, data=WEBM, **overrides):
        options = {"project": self.root, "task": "T01", "stream": io.BytesIO(data), "length": len(data), "content_type": "video/webm",
                   "project_id": self.context["projectId"]}
        options.update(overrides)
        return reviewmedia.save(**options)

    def test_screen_clip_metadata_and_human_gate_stay_separate(self):
        before = self.task.read_bytes()
        item = self.save(source="screen-recording", capture_head=self.context["head"], task_hash=self.context["taskHash"], duration=2.5, note="Show the save flow")
        self.assertEqual(appapi.artifact_file(self.root, "T01", item["path"]).read_bytes(), WEBM)
        self.assertEqual(item["media"]["projectCommitAtStart"], self.context["head"])
        self.assertEqual(item["media"]["durationSeconds"], 2.5)
        self.assertEqual(before, self.task.read_bytes())
        self.assertFalse(item["media"]["taskChangedDuringCapture"])

    def test_uploaded_clip_does_not_claim_its_recording_commit(self):
        item = self.save(MP4, content_type="video/mp4")
        self.assertIsNone(item["media"]["projectCommitAtStart"])
        self.assertEqual(item["media"]["source"], "upload")
        self.assertTrue(item["path"].endswith(".mp4"))

    def test_changed_commit_and_task_are_visible_in_recording_metadata(self):
        self.task.write_text(self.task.read_text() + "\nChanged requirement\n")
        self.git("add", ".")
        self.git("commit", "-qm", "changed task")
        item = self.save(source="screen-recording", capture_head=self.context["head"], task_hash=self.context["taskHash"])
        self.assertNotEqual(item["head"], item["media"]["projectCommitAtStart"])
        self.assertTrue(item["media"]["taskChangedDuringCapture"])

    def test_upload_rejects_wrong_project_or_missing_context(self):
        for project_id in ("", "wrong-project"):
            with self.subTest(project_id=project_id), self.assertRaises(ValueError): self.save(project_id=project_id)
        self.assertFalse((self.root / ".relay/recordings").exists())

    def test_upload_rejects_oversize_empty_and_wrong_containers(self):
        for options in ({"length": 0}, {"length": reviewmedia.MAX_CLIP_BYTES + 1}, {"content_type": "text/html"}, {"content_type": "video/mp4"}):
            with self.subTest(options=options), self.assertRaises(ValueError): self.save(**options)
        with self.assertRaises(ValueError): self.save(b"not a video")
        self.assertFalse((self.root / ".relay/recordings").exists())

    def test_interrupted_upload_leaves_no_partial_clip(self):
        with self.assertRaisesRegex(ValueError, "interrupted"):
            self.save(length=len(WEBM) + 100)
        self.assertFalse(list((self.root / ".relay").rglob("*.webm")))
        self.assertFalse(appapi.runtime_for(self.root).state().get("artifacts"))

    def test_filename_collision_never_removes_an_existing_clip(self):
        from types import SimpleNamespace
        with patch.object(reviewmedia.time, "strftime", return_value="fixed-"), patch.object(reviewmedia.uuid, "uuid4", return_value=SimpleNamespace(hex="1234567890abcdef")):
            first = self.save()
            with self.assertRaises(FileExistsError): self.save()
        self.assertEqual(appapi.artifact_file(self.root, "T01", first["path"]).read_bytes(), WEBM)

    def test_recording_metadata_is_validated(self):
        for options in ({"duration": "NaN"}, {"duration": -1}, {"duration": float("inf")}, {"capture_head": "not-a-commit"}, {"task_hash": "invalid"}, {"source": "hidden-capture"}):
            with self.subTest(options=options), self.assertRaises(ValueError): self.save(**options)

    def test_recording_task_cannot_escape_or_be_invented(self):
        for task in ("../outside", "T999", "T01/other"):
            with self.subTest(task=task), self.assertRaises(ValueError): self.save(task=task)

    def test_review_page_is_local_and_plays_clips_without_notion_account(self):
        item = self.save(note='<script>alert("unsafe")</script>')
        page = digest.page(self.root)
        self.assertIn("No Notion account is needed", page)
        self.assertIn("<video controls", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("Request a fresh review", page)
        self.assertNotIn('data-a="approve"', page)
        self.assertIn("HUMAN: inspect workflow", page)
        self.assertIn("/review.js", page)
        self.assertIn(quote(item["path"], safe=""), page)

    def queue_review(self, note, capture=None):
        runtime = appapi.runtime_for(self.root)
        cmd = "echo fixture"
        (self.root / "CURRENT_TASK.md").write_text("tasks/T01.md\nT01 - Review\n")
        runtime.checkpoint("building", "T01", cmd)
        builder = runtime.begin("builder", "codex", "gpt-fixture", "relay-live-codex.log")
        runtime.finish(builder, 0, "success")
        runtime.checkpoint("testing", "T01", cmd)
        (self.root / "relay-tests.log").write_text("PASS\n")
        runtime.checkpoint("reviewing", "T01", cmd, 0)
        reviewer = runtime.begin("reviewer", "claude", "claude-fixture", "relay-live-claude.log")
        if capture:
            path = self.root / ".relay/capture.png"
            path.write_bytes(capture)
            runtime.add_artifact("T01", ".relay/capture.png", "Camera footage", build=self.git("rev-parse", "HEAD"))
        self.task.write_text(self.task.read_text().replace("[ ] AUTO:", "[x] AUTO:"), encoding="utf-8")
        with (self.root / "PLAYTEST_QUEUE.md").open("a", encoding="utf-8") as queue:
            queue.write("T01 — Camera — " + note + "\n")
        runtime.finish(reviewer, 0, "success")
        runtime.guard_review(cmd)
        runtime.checkpoint("idle", "T01", cmd)
        return runtime.state()["reviews"]["T01"][-1]

    def test_rejected_review_keeps_its_build_footage_and_feedback(self):
        original = self.queue_review("Turn while running.", b"original screenshot")
        original_path = digest.items(self.root)[0]["artifacts"][0]["path"]
        self.assertEqual(original["number"], 1)
        self.assertTrue(appapi.decide_review(self.root, "T01", original["id"], "changes", "The camera swings too fast.")["ok"])

        (self.root / "camera.gd").write_text("var swing_speed = 1\n")
        self.git("add", "camera.gd")
        self.git("commit", "-qm", "T01: slow camera swing")
        updated = self.queue_review("Turn 180 degrees while sprinting.")
        self.assertEqual(updated["number"], 2)
        self.assertNotEqual(original["build"], updated["build"])
        self.assertFalse(appapi.decide_review(self.root, "T01", original["id"], "approve")["ok"])
        self.assertFalse(appapi.decide_review(self.root, "T01", "", "approve")["ok"])
        page = digest.page(self.root)
        self.assertIn("Playthrough needed", page)
        self.assertIn("Turn 180 degrees while sprinting.", page)
        self.assertIn("The camera swings too fast.", page)
        self.assertIn("Review 1", page)
        self.assertIn("Review 2", page)
        card = digest.items(self.root)[0]
        self.assertFalse(card["artifacts"])
        self.assertEqual(card["history"][0]["artifacts"][0]["path"], original_path)

        runtime = appapi.runtime_for(self.root)
        with self.assertRaisesRegex(ValueError, "fresh footage"):
            runtime.add_artifact("T01", ".relay/capture.png", build=updated["build"], revision=updated["id"])
        (self.root / ".relay/capture.png").write_bytes(b"updated screenshot")
        self.assertEqual(appapi.artifact_file(self.root, "T01", original_path).read_bytes(), b"original screenshot")
        runtime.add_artifact("T01", ".relay/capture.png", build=updated["build"], revision=updated["id"])
        self.assertNotIn("Playthrough needed", digest.page(self.root))
        self.assertTrue(appapi.decide_review(self.root, "T01", updated["id"], "approve")["ok"])
        history = runtime.state()["reviews"]["T01"]
        self.assertEqual([r["status"] for r in history], ["changes requested", "approved"])
        self.assertIn(updated["build"], self.task.read_text())

    def test_review_upload_rejects_wrong_build_and_changed_capture_workspace(self):
        review = self.queue_review("Open the camera settings.")
        current = reviewmedia.context(self.root, "T01")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.save(revision=review["id"], build="wrong")
        (self.root / "changed.txt").write_text("changed while capturing\n")
        with self.assertRaisesRegex(ValueError, "changed during recording"):
            self.save(revision=review["id"], build=review["build"], source="screen-recording",
                      capture_head=current["head"], task_hash=current["taskHash"])
        item = self.save(revision=review["id"], build=review["build"])
        self.assertEqual(item["reviewId"], review["id"])

    def test_byte_ranges_allow_seeking_and_reject_invalid_requests(self):
        self.assertEqual(reviewmedia.byte_range(None, 100), (0, 99, False))
        self.assertEqual(reviewmedia.byte_range("bytes=5-10", 100), (5, 10, True))
        self.assertEqual(reviewmedia.byte_range("bytes=95-", 100), (95, 99, True))
        self.assertEqual(reviewmedia.byte_range("bytes=-4", 100), (96, 99, True))
        self.assertEqual(reviewmedia.byte_range("bytes=98-999", 100), (98, 99, True))
        for header in ("bytes=101-", "bytes=9-2", "bytes=0-2,5-7", "bytes=-0", "wrong"):
            with self.subTest(header=header), self.assertRaises(ValueError): reviewmedia.byte_range(header, 100)

    def test_raw_upload_and_range_api_require_local_context(self):
        http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        with patch.object(server.Handler, "relay", self.root):
            thread.start()
            base = f"http://127.0.0.1:{http.server_port}"
            try:
                with urllib.request.urlopen(base + "/api/recordings/context?task=T01") as response: context = json.load(response)
                headers = {"Content-Type": "video/webm", "X-Clip-Project": context["projectId"]}
                request = urllib.request.Request(base + "/api/recordings/save?task=T01", data=WEBM, headers=headers)
                with urllib.request.urlopen(request) as response: result = json.load(response)
                self.assertTrue(result["ok"])
                from urllib.parse import urlencode
                url = base + "/api/artifact?" + urlencode({"task": "T01", "path": result["artifact"]["path"]})
                with urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=4-9"})) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.read(), WEBM[4:10])
                    self.assertEqual(response.headers["Content-Range"], f"bytes 4-9/{len(WEBM)}")
                with self.assertRaises(urllib.error.HTTPError) as invalid:
                    urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=999-"}))
                self.assertEqual(invalid.exception.code, 416)
                request.add_header("Origin", "https://unrelated.example")
                with self.assertRaises(urllib.error.HTTPError) as refused: urllib.request.urlopen(request)
                self.assertEqual(refused.exception.code, 403)
                request = urllib.request.Request(base + "/api/recordings/save?task=T01", data=b"x", headers={**headers, "Content-Length": str(reviewmedia.MAX_CLIP_BYTES + 1)})
                with self.assertRaises(urllib.error.HTTPError) as oversized: urllib.request.urlopen(request)
                self.assertEqual(oversized.exception.code, 413)
                self.assertIn("[ ] HUMAN", self.task.read_text())
            finally:
                http.shutdown(); http.server_close(); thread.join(timeout=5)


if __name__ == "__main__": unittest.main()
