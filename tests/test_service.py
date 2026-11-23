import io
import tempfile
import unittest
import base64
from pathlib import Path

from PIL import Image

from frame_art_mode.config import Config
from frame_art_mode.service import FrameService, ServiceError
from frame_art_mode.web import create_app


def jpeg_bytes(color):
    output = io.BytesIO()
    Image.new("RGB", (640, 640), color).save(output, "JPEG")
    return output.getvalue()


class FakeArt:
    def __init__(self):
        self.items = {}
        self.upload_count = 0
        self.fail_upload = False
        self.fail_on_upload_number = None
        self.fail_delete = set()
        self.mode = "on"
        self.current = None
        self.calls = []
        self.return_none = False
        self.on_upload = None

    def get_artmode(self):
        return self.mode

    def available(self):
        return [{"content_id": content_id} for content_id in self.items]

    def upload(self, data, file_type):
        self.calls.append(("upload", file_type))
        self.upload_count += 1
        if self.fail_upload or self.upload_count == self.fail_on_upload_number:
            self.fail_upload = False
            raise RuntimeError("upload rejected")
        content_id = f"APP-{self.upload_count}"
        self.items[content_id] = data
        if self.on_upload:
            callback, self.on_upload = self.on_upload, None
            callback()
        return None if self.return_none else content_id

    def delete(self, content_id):
        self.calls.append(("delete", content_id))
        if content_id in self.fail_delete:
            return False
        self.items.pop(content_id, None)
        return True

    def select_image(self, content_id):
        self.current = content_id
        self.calls.append(("select", content_id))

    def get_current(self):
        return {"content_id": self.current}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.images = base / "images"
        self.images.mkdir()
        self.state = base / "state"
        self.config = Config(base, self.images, self.state, "192.168.1.50", "192.168.1.20")
        self.art = FakeArt()
        self.sleeps = []
        self.service = FrameService(
            self.config, tv_factory=lambda config, token: self.art, sleeper=self.sleeps.append
        )
        (self.images / "photo.jpg").write_bytes(jpeg_bytes("red"))

    def tearDown(self):
        self.service.stop()
        self.temp.cleanup()

    def test_sync_reuses_unchanged_uploads_and_preserves_untracked_art(self):
        self.art.items["SAMSUNG-ART"] = b"existing"
        first = self.service.sync("root")
        content_id = first["entries"][0]["content_id"]
        second = self.service.sync("root")
        self.assertEqual(second["entries"][0]["content_id"], content_id)
        self.assertEqual(self.art.upload_count, 1)
        self.assertIn("SAMSUNG-ART", self.art.items)
        self.assertEqual(self.art.current, content_id)

    def test_missing_tv_item_is_reuploaded_and_restart_does_not_sync(self):
        content_id = self.service.sync("root")["entries"][0]["content_id"]
        self.art.items.pop(content_id)
        result = self.service.sync("root")
        self.assertEqual(self.art.upload_count, 2)
        self.assertNotEqual(result["entries"][0]["content_id"], content_id)
        active_id = result["entries"][0]["content_id"]
        self.service.stop()
        reopened = FrameService(
            self.config, tv_factory=lambda config, token: self.art, sleeper=self.sleeps.append
        )
        try:
            self.assertEqual(reopened.status()["active_set"], "root")
            self.assertEqual(reopened.status()["entries"][0]["content_id"], active_id)
            self.assertEqual(self.art.upload_count, 2)
        finally:
            reopened.stop()

    def test_second_process_for_same_tv_is_rejected(self):
        with self.assertRaisesRegex(ServiceError, "already managing TV"):
            FrameService(self.config, tv_factory=lambda config, token: self.art)

    def test_none_upload_confirmation_is_verified_through_available_list(self):
        self.art.return_none = True
        result = self.service.sync("root")
        self.assertEqual(result["entries"][0]["content_id"], "APP-1")

    def test_failed_upload_rolls_back_new_items_and_keeps_old_set_active(self):
        initial = self.service.sync("root")
        old_id = initial["entries"][0]["content_id"]
        (self.images / "photo.jpg").write_bytes(jpeg_bytes("blue"))
        self.art.fail_upload = True
        with self.assertRaisesRegex(ServiceError, "previously active"):
            self.service.sync("root")
        self.assertEqual(self.service.status()["entries"][0]["content_id"], old_id)
        self.assertIn(old_id, self.art.items)
        self.assertNotIn(old_id, [item[1] for item in self.art.calls if item[0] == "delete"])

    def test_later_upload_failure_rolls_back_only_this_attempts_success(self):
        old_id = self.service.sync("root")["entries"][0]["content_id"]
        (self.images / "photo.jpg").write_bytes(jpeg_bytes("blue"))
        (self.images / "photo2.jpg").write_bytes(jpeg_bytes("green"))
        self.art.fail_on_upload_number = self.art.upload_count + 2
        with self.assertRaisesRegex(ServiceError, "previously active"):
            self.service.sync("root")
        self.assertIn(old_id, self.art.items)
        self.assertNotIn("APP-2", self.art.items)
        self.assertEqual(self.service.status()["entries"][0]["content_id"], old_id)

    def test_empty_valid_set_does_not_change_tv_or_manifest(self):
        active = self.service.sync("root")["entries"][0]["content_id"]
        (self.images / "photo.jpg").write_bytes(b"not an image")
        with self.assertRaisesRegex(ServiceError, "no valid images"):
            self.service.sync("root")
        self.assertEqual(self.service.status()["entries"][0]["content_id"], active)
        self.assertIn(active, self.art.items)

    def test_partial_cleanup_keeps_new_set_active_and_retries_known_ids(self):
        old_id = self.service.sync("root")["entries"][0]["content_id"]
        party = self.images / "Party"
        party.mkdir()
        (party / "photo.jpg").write_bytes(jpeg_bytes("blue"))
        self.art.fail_delete.add(old_id)
        result = self.service.sync("occasion:Party")
        new_id = result["entries"][0]["content_id"]
        self.assertEqual(result["active_set"], "occasion:Party")
        self.assertEqual(result["cleanup_pending"], [old_id])
        self.assertIn(old_id, self.art.items)
        self.art.fail_delete.clear()
        retried = self.service.sync("occasion:Party")
        self.assertEqual(retried["cleanup_pending"], [])
        self.assertNotIn(old_id, self.art.items)
        self.assertIn(new_id, self.art.items)

    def test_sync_checks_art_mode_and_records_batch_delays(self):
        self.art.mode = "off"
        with self.assertRaisesRegex(ServiceError, "not in Art Mode"):
            self.service.sync("root")
        self.assertFalse(any(call[0] == "upload" for call in self.art.calls))
        self.art.mode = "on"
        self.service.sync("root")
        self.assertIn(12, self.sleeps)

    def test_upload_and_delete_batches_use_request_spacing(self):
        (self.images / "photo2.jpg").write_bytes(jpeg_bytes("green"))
        self.service.sync("root")
        self.assertIn(1, self.sleeps)
        self.assertIn(12, self.sleeps)

    def test_delete_batch_is_spaced_and_has_settle_delay(self):
        (self.images / "photo2.jpg").write_bytes(jpeg_bytes("green"))
        self.service.sync("root")
        party = self.images / "Party"
        party.mkdir()
        (party / "photo.jpg").write_bytes(jpeg_bytes("blue"))
        self.sleeps.clear()
        self.service.sync("occasion:Party")
        self.assertEqual(len([call for call in self.art.calls if call[0] == "delete"]), 2)
        self.assertGreaterEqual(self.sleeps.count(1), 1)
        self.assertGreaterEqual(self.sleeps.count(12), 2)

    def test_preview_is_limited_to_active_set_and_restores_current_image(self):
        (self.images / "photo2.jpg").write_bytes(jpeg_bytes("green"))
        result = self.service.sync("root")
        first, second = [entry["content_id"] for entry in result["entries"]]
        restore_id = self.art.current
        preview_id = second if restore_id == first else first
        self.service.preview(preview_id)
        self.assertEqual(self.art.current, preview_id)
        with self.assertRaisesRegex(ServiceError, "currently synced set"):
            self.service.preview("SAMSUNG-ART")
        self.assertEqual(self.service._preview_restore_id, restore_id)

    def test_unreadable_manifest_fails_closed(self):
        active = self.service.sync("root")["entries"][0]["content_id"]
        manifest_path = self.service.manifest_path
        self.service.stop()
        manifest_path.write_text("{", encoding="utf-8")
        reopened = FrameService(
            self.config, tv_factory=lambda config, token: self.art, sleeper=self.sleeps.append
        )
        try:
            with self.assertRaisesRegex(ServiceError, "ownership manifest is unreadable"):
                reopened.sync("root")
            self.assertIn(active, self.art.items)
            self.assertFalse(any(call == ("delete", active) for call in self.art.calls))
        finally:
            reopened.stop()

    def test_manifest_removed_while_running_prevents_sync_deletion(self):
        active = self.service.sync("root")["entries"][0]["content_id"]
        self.service.manifest_path.unlink()
        with self.assertRaisesRegex(ServiceError, "manifest disappeared"):
            self.service.sync("root")
        self.assertIn(active, self.art.items)
        self.assertFalse(any(call == ("delete", active) for call in self.art.calls))

    def test_manifest_loss_during_upload_rolls_back_new_content(self):
        active = self.service.sync("root")["entries"][0]["content_id"]
        (self.images / "photo.jpg").write_bytes(jpeg_bytes("blue"))
        self.art.on_upload = self.service.manifest_path.unlink
        with self.assertRaisesRegex(ServiceError, "could not commit the ownership manifest"):
            self.service.sync("root")
        self.assertIn(active, self.art.items)
        self.assertNotIn("APP-2", self.art.items)

    def test_web_settings_require_csrf_and_thumbnails_are_available(self):
        app = create_app(self.service)
        app.testing = True
        client = app.test_client()
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Sync to TV", page.data)
        self.assertEqual(client.post("/settings", data={"set_id": "root"}).status_code, 400)
        with client.session_transaction() as browser_session:
            csrf = browser_session["csrf_token"]
        response = client.post(
            "/settings",
            data={"set_id": "root", "interval_minutes": "7", "random": "1", "csrf_token": csrf},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.service.settings_for("root")["interval_minutes"], 7)
        encoded = base64.urlsafe_b64encode(b"photo.jpg").decode().rstrip("=")
        thumbnail = client.get(f"/thumbnail/{encoded}")
        self.assertEqual(thumbnail.mimetype, "image/jpeg")


if __name__ == "__main__":
    unittest.main()
