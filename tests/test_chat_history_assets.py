import base64
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from AI_history import (
    CHAT_ASSET_PREFIX,
    atomic_write_chat_sessions,
    chat_asset_dir,
    externalize_chat_images,
    hydrate_chat_images,
    prune_unreferenced_chat_assets,
)
from LS_pipeline import rewrite_zip_members


class ChatHistoryAssetTests(unittest.TestCase):
    def test_round_trip_preserves_exact_api_image_content(self):
        with tempfile.TemporaryDirectory() as folder:
            history = Path(folder) / "chat_conversations.json"
            payload = b"\x89PNG\r\n\x1a\nexample"
            data_url = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
            sessions = [{"id": "one", "messages": [{"content": [{"type": "image_url", "image_url": {"url": data_url}}]}]}]
            compact = externalize_chat_images(sessions, chat_asset_dir(history))
            stored_url = compact[0]["messages"][0]["content"][0]["image_url"]["url"]
            self.assertTrue(stored_url.startswith(CHAT_ASSET_PREFIX))
            self.assertNotIn("base64", json.dumps(compact))
            self.assertEqual(hydrate_chat_images(compact, chat_asset_dir(history)), sessions)

    def test_atomic_history_and_reference_aware_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            history = Path(folder) / "chat_conversations.json"
            first = "data:image/png;base64," + base64.b64encode(b"first").decode("ascii")
            second = "data:image/png;base64," + base64.b64encode(b"second").decode("ascii")
            compact = externalize_chat_images([{"first": first}, {"second": second}], chat_asset_dir(history))
            atomic_write_chat_sessions(history, compact[:1])
            prune_unreferenced_chat_assets(chat_asset_dir(history), compact[:1])
            self.assertEqual(json.loads(history.read_text(encoding="utf-8")), compact[:1])
            self.assertEqual(len(list(chat_asset_dir(history).glob("*"))), 1)

    def test_docx_zip_rewrite_preserves_unmodified_binary_members(self):
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / "example.docx"
            binary_payload = b"binary-image" * 10000
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", b"<old/>")
                archive.writestr("word/media/image1.png", binary_payload)
            rewrite_zip_members(archive_path, {"word/document.xml": b"<new/>"})
            with zipfile.ZipFile(archive_path, "r") as archive:
                self.assertEqual(archive.read("word/document.xml"), b"<new/>")
                self.assertEqual(archive.read("word/media/image1.png"), binary_payload)


if __name__ == "__main__":
    unittest.main()
