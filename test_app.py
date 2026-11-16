import unittest, tempfile, json, shutil
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import app as photobooth

class PhotoboothSmokeTests(unittest.TestCase):
    def setUp(self):
        self.client = photobooth.app.test_client()
        with photobooth.lock:
            photobooth.state.update(active=False, session_id=None, started_at=None, photos={},
                                    complete=False, revision=0, final=None, status="idle", shot_count=3,
                                    access_token=None, access_token_expires_at=None, phone_upload_token=None)
        with photobooth.live_frame_lock:
            photobooth.live_camera_frame.update(data=b"", updated=0.0, sequence=0)

    def test_pages(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/guest").status_code, 200)
        self.assertEqual(self.client.get("/phone").status_code, 404)
        self.assertEqual(self.client.get("/ready-gallery").status_code, 200)
        self.assertEqual(self.client.get("/finished-photos").status_code, 200)

    def test_start_conflict_and_end(self):
        a = self.client.post("/api/session/start")
        self.assertEqual(a.status_code, 200)
        b = self.client.post("/api/session/start")
        self.assertEqual(b.status_code, 409)
        self.assertEqual(self.client.post("/api/session/end").status_code, 200)

    def test_state_shape(self):
        data = self.client.get("/api/state").get_json()
        self.assertIn("photo_count", data)
        self.assertEqual(data["photo_count"], 0)
        self.assertIn("status", data)

    def test_live_preview_stream_sends_only_latest_frame(self):
        import io
        from PIL import Image
        frames = []
        for color in ((20, 80, 140), (120, 160, 200)):
            buffer = io.BytesIO()
            Image.new("RGB", (24, 16), color).save(buffer, format="JPEG")
            frames.append(buffer.getvalue())
        for frame in frames:
            response = self.client.post("/api/live-frame", data=frame, content_type="image/jpeg")
            self.assertEqual(response.status_code, 204)
        status = self.client.get("/api/live-frame-status").get_json()
        self.assertTrue(status["fresh"])
        self.assertEqual(status["sequence"], 2)
        stream = self.client.get("/api/live-stream.mjpg", buffered=False)
        self.assertIn("multipart/x-mixed-replace", stream.headers.get("Content-Type", ""))
        first_part = next(stream.response)
        self.assertTrue(first_part.startswith(b"--frame"))
        self.assertIn(frames[-1], first_part)
        self.assertNotIn(frames[0], first_part)
        stream.close()

    def test_session_id_accepts_hex_suffix_for_qr_gallery(self):
        self.assertTrue(photobooth.valid_session_id("20261112-201530-a1b2c3"))
        self.assertTrue(photobooth.valid_session_id("20261112-201530-abcdef"))
        self.assertFalse(photobooth.valid_session_id("20261112-201530-xyz123"))
        self.assertFalse(photobooth.valid_session_id("../session"))

    def test_deleting_one_finished_photo_preserves_the_other_slots(self):
        original_store = photobooth.STORE
        with tempfile.TemporaryDirectory() as temp:
            photobooth.STORE = Path(temp)
            photobooth.ensure_dirs()
            try:
                sid = "20261113-001500-a1b2c3"
                folder = photobooth.STORE / "sessions" / sid
                folder.mkdir(parents=True)
                photos = {}
                for slot in (1, 2, 3):
                    name = f"{sid}_photo_{slot}_test.jpg"
                    Image.new("RGB", (160, 120), (60 + slot * 20, 90, 130)).save(photobooth.STORE / "original" / name)
                    shutil.copy2(photobooth.STORE / "original" / name, folder / name)
                    photos[str(slot)] = name
                (folder / "session.json").write_text(json.dumps({
                    "session_id": sid, "photos": photos, "status": "finished",
                    "finished": True, "complete": True, "revision": 1,
                    "final": {"jpg": f"/final/{sid}_strip.jpg"}
                }), encoding="utf-8")
                preview = {"gif": f"/final/{sid}_flashback.gif", "gif_filename": f"{sid}_flashback.gif"}
                with patch.object(photobooth, "_build_ready_preview", side_effect=lambda sid, photos, request_host=None: preview):
                    response = self.client.delete(f"/api/saved-session/{sid}/photo/2")
                self.assertEqual(response.status_code, 200)
                saved = json.loads((folder / "session.json").read_text(encoding="utf-8"))
                self.assertEqual(set(saved["photos"]), {"1", "3"})
                self.assertTrue((photobooth.STORE / "original" / photos["1"]).is_file())
                self.assertFalse((photobooth.STORE / "original" / photos["2"]).exists())
                self.assertTrue((photobooth.STORE / "original" / photos["3"]).is_file())
                self.assertIsNone(saved["final"])
            finally:
                photobooth.STORE = original_store

    def test_ready_gallery_requires_explicit_finish_and_save(self):
        original_store = photobooth.STORE
        with tempfile.TemporaryDirectory() as temp:
            photobooth.STORE = Path(temp)
            photobooth.ensure_dirs()
            try:
                ok, _ = photobooth.start_session(3)
                self.assertTrue(ok)
                sid = photobooth.state["session_id"]
                folder = photobooth.STORE / "sessions" / sid
                for slot in (1, 2, 3):
                    name = f"{sid}_photo_{slot}_test.jpg"
                    Image.new("RGB", (160, 120), (80 + slot * 20, 40, 70)).save(photobooth.STORE / "original" / name)
                    shutil.copy2(photobooth.STORE / "original" / name, folder / name)
                    photobooth.state["photos"][str(slot)] = name
                photobooth.state["complete"] = True
                photobooth.persist_state()
                preview = {"gif": f"/final/{sid}_flashback.gif", "gif_filename": f"{sid}_flashback.gif",
                           "session_id": sid, "ready_preview": True}
                with patch.object(photobooth, "_build_ready_preview", side_effect=lambda sid, photos, request_host=None: preview):
                    response = self.client.post("/api/session/prepare")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json().get("processing"))
                import time
                for _ in range(100):
                    saved = json.loads((folder / "session.json").read_text())
                    if saved.get("status") == "ready": break
                    time.sleep(0.01)
                self.assertEqual(saved["status"], "ready")
                import time
                for _ in range(100):
                    if photobooth.gif_generation_lock.acquire(blocking=False):
                        photobooth.gif_generation_lock.release()
                        break
                    time.sleep(0.01)
                self.assertEqual(self.client.get("/ready-gallery").status_code, 200)
                self.assertEqual(self.client.get(f"/guest.html?session={sid}").status_code, 200)
                self.assertNotIn(sid, self.client.get("/finished-photos").get_data(as_text=True))
                final = {"jpg": f"/final/{sid}_strip.jpg", "png": f"/final/{sid}_strip.png",
                         "gif": f"/final/{sid}_flashback.gif", "filename": f"{sid}_strip.png",
                         "gif_filename": f"{sid}_flashback.gif", "session_id": sid, "url": "", "qr": ""}
                with patch.object(photobooth, "_build_outputs_for_session", return_value=(final, (100, 200))):
                    response = self.client.post(f"/api/ready-session/{sid}/finish")
                self.assertEqual(response.status_code, 200)
                saved = json.loads((folder / "session.json").read_text())
                self.assertEqual(saved["status"], "finished")
                self.assertIn(sid, self.client.get("/finished-photos").get_data(as_text=True))
                operator_gallery = self.client.get(f"/session/{sid}")
                self.assertEqual(operator_gallery.status_code, 200)
                self.assertIn("FINISHED PHOTO STRIP", operator_gallery.get_data(as_text=True))
            finally:
                photobooth.STORE = original_store
                with photobooth.lock:
                    photobooth.state.update(active=False, session_id=None, started_at=None, photos={},
                                            complete=False, final=None, status="idle", revision=0, shot_count=3)

    def test_private_guest_gallery_requires_unpredictable_token(self):
        ok, _ = photobooth.start_session(3)
        self.assertTrue(ok)
        sid = photobooth.state["session_id"]
        token = photobooth.state["access_token"]
        state_response = self.client.get("/api/state")
        self.assertNotIn("access_token", state_response.get_json())
        private_page = self.client.get(f"/g/{token}")
        self.assertEqual(private_page.status_code, 200)
        self.assertNotIn(sid, private_page.get_data(as_text=True))
        self.assertEqual(self.client.get(f"/g/{'x' * 43}").status_code, 404)
        manifest = photobooth.STORE / "sessions" / sid / "session.json"
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        saved["access_token_expires_at"] = 0
        manifest.write_text(json.dumps(saved), encoding="utf-8")
        self.assertEqual(self.client.get(f"/g/{token}").status_code, 404)
        self.assertEqual(self.client.get(f"/session/{sid}", environ_base={"REMOTE_ADDR":"192.0.2.10"}).status_code, 404)
        self.assertEqual(self.client.get("/api/state", environ_base={"REMOTE_ADDR":"192.0.2.10"}).status_code, 404)

    def test_phone_upload_requires_session_scoped_token(self):
        ok, _ = photobooth.start_session(3)
        self.assertTrue(ok)
        response = self.client.post("/api/phone/upload", data={"token":"wrong"})
        self.assertEqual(response.status_code, 403)

if __name__ == "__main__":
    unittest.main()
