"""工作目录迁移、译文预览恢复与异步模型请求测试。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import AI_common
import OT_common
import OT_ui
import PB_layout
from OT_ui import MainWindow, ModelOptionsFetchWorker
from workspace_migration import copy_workspace_data, should_copy_workspace_item
from workspace_paths import default_workspace_path


class MigrationAndAsyncModelTests(unittest.TestCase):
    def test_layout_image_src_url_generates_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            doc_folder = Path(tmp_dir) / "paper_01"
            image = doc_folder / "images" / "fig 1.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"image")
            self.assertEqual(PB_layout.layout_image_src_url(image, doc_folder), "images/fig%201.png")

    def test_should_copy_only_program_data(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            generated = root / "paper"
            generated.mkdir()
            (generated / ".mineru_generated").write_text("{}", encoding="utf-8")
            personal = root / "notes"
            personal.mkdir()
            attachments = root / "chat_attachments"
            attachments.mkdir()
            history = root / "chat_conversations.json"
            history.write_text("[]", encoding="utf-8")
            self.assertTrue(should_copy_workspace_item(generated))
            self.assertTrue(should_copy_workspace_item(attachments))
            self.assertTrue(should_copy_workspace_item(history))
            self.assertFalse(should_copy_workspace_item(personal))

    def test_copy_migration_preserves_old_data_and_remaps_structured_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            old_dir = root / "old"
            new_dir = root / "new"
            old_dir.mkdir()
            new_dir.mkdir()
            paper1 = old_dir / "paper1"
            paper10 = old_dir / "paper10"
            for folder in (paper1, paper10):
                folder.mkdir()
                (folder / ".mineru_generated").write_text("{}", encoding="utf-8")
                (folder / "full.cleaned.md").write_text("# source", encoding="utf-8")
                image = folder / "images" / "figure.png"
                image.parent.mkdir()
                image.write_bytes(b"image")
                (folder / "preview_layout_translated_current.full.cleaned.html").write_text(
                    f'<img src="{image.as_uri()}">', encoding="utf-8"
                )

            source10 = paper10 / "full.cleaned.md"
            old_hash = hashlib.sha1(str(paper10.resolve()).encode("utf-8")).hexdigest()
            sessions = [{
                "id": f"doc-chat-{old_hash}",
                "document_source_path": str(source10),
                "messages": [{
                    "role": "user",
                    "content": f"普通文字提到了 {paper1}\n来源: {source10}\n请总结",
                }],
            }]
            history = old_dir / "chat_conversations.json"
            history.write_text(json.dumps(sessions, ensure_ascii=False), encoding="utf-8")
            personal = old_dir / "personal_notes"
            personal.mkdir()
            (personal / "note.txt").write_text("private", encoding="utf-8")

            migrated_map = copy_workspace_data(old_dir, new_dir)

            self.assertTrue((paper10 / "full.cleaned.md").exists())
            self.assertTrue(history.exists())
            self.assertFalse((new_dir / "personal_notes").exists())
            self.assertTrue(
                (new_dir / "paper10" / "preview_layout_translated_current.full.cleaned.html").exists()
            )
            self.assertEqual(migrated_map[paper10.resolve()], (new_dir / "paper10").resolve())

            copied = json.loads((new_dir / "chat_conversations.json").read_text(encoding="utf-8"))
            new_source10 = new_dir / "paper10" / "full.cleaned.md"
            new_hash = hashlib.sha1(str((new_dir / "paper10").resolve()).encode("utf-8")).hexdigest()
            self.assertEqual(copied[0]["id"], f"doc-chat-{new_hash}")
            self.assertEqual(copied[0]["document_source_path"], str(new_source10))
            self.assertIn(f"来源: {new_source10}", copied[0]["messages"][0]["content"])
            self.assertIn(f"普通文字提到了 {paper1}", copied[0]["messages"][0]["content"])
            copied_html = (new_dir / "paper10" / "preview_layout_translated_current.full.cleaned.html").read_text(
                encoding="utf-8"
            )
            self.assertIn((new_dir / "paper10" / "images" / "figure.png").as_uri(), copied_html)
            self.assertNotIn(paper10.as_uri(), copied_html)

    def test_copy_migration_rejects_nonempty_target_without_touching_old_data(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            old_dir = root / "old"
            new_dir = root / "new"
            old_dir.mkdir()
            new_dir.mkdir()
            generated = old_dir / "paper"
            generated.mkdir()
            (generated / ".mineru_generated").write_text("{}", encoding="utf-8")
            (new_dir / "existing.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "空"):
                copy_workspace_data(old_dir, new_dir)
            self.assertTrue((generated / ".mineru_generated").exists())
            self.assertEqual((new_dir / "existing.txt").read_text(encoding="utf-8"), "keep")

    def test_corrupt_history_aborts_copy_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            old_dir = root / "old"
            new_dir = root / "new"
            old_dir.mkdir()
            new_dir.mkdir()
            history = old_dir / "chat_conversations.json"
            history.write_text("not-json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                copy_workspace_data(old_dir, new_dir)
            self.assertEqual(history.read_text(encoding="utf-8"), "not-json")
            self.assertEqual(list(new_dir.iterdir()), [])

    def test_current_settings_with_empty_work_dir_inherits_same_type_legacy_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            current = root / "settings.json"
            legacy = root / "settings_workbench.json"
            workspace = root / "legacy-workspace"
            workspace.mkdir()
            current.write_text(json.dumps({"work_dir": "", "ai_provider": "deepseek"}), encoding="utf-8")
            legacy.write_text(json.dumps({"work_dir": str(workspace), "ai_provider": "gemini"}), encoding="utf-8")
            with patch.object(OT_common, "APP_DIR", root), patch.object(OT_common, "SETTINGS_PATH", current), patch.object(
                OT_common, "LEGACY_SETTINGS_PATHS", (legacy,)
            ):
                loaded = OT_common.load_settings()
            self.assertEqual(loaded.work_dir, str(workspace))
            self.assertEqual(loaded.ai_provider, "deepseek")

    def test_chat_settings_inherit_legacy_work_dir_without_overwriting_chat_fields(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            current = root / "settings_chat.json"
            legacy = root / "settings_ai_chat.json"
            workspace = root / "legacy-workspace"
            workspace.mkdir()
            current.write_text(json.dumps({"work_dir": "", "ai_provider": "oneapi"}), encoding="utf-8")
            legacy.write_text(json.dumps({"work_dir": str(workspace), "ai_provider": "gemini"}), encoding="utf-8")
            with patch.object(AI_common, "APP_DIR", root), patch.object(AI_common, "SETTINGS_PATH", current), patch.object(
                AI_common, "LEGACY_SETTINGS_PATHS", (legacy,)
            ):
                loaded = AI_common.load_settings()
            self.assertEqual(loaded.work_dir, str(workspace))
            self.assertEqual(loaded.ai_provider, "oneapi")

    def test_new_install_defaults_to_install_directory(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            app_dir = root / "appdata" / "LitMTrans"
            install_dir = root / "chosen-install"
            install_dir.mkdir()
            result = default_workspace_path(app_dir, (), install_dir)
            self.assertEqual(result, install_dir / "workspace")

    def test_missing_layout_translation_preview_is_rebuilt_from_bundle(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir) / "full.cleaned.md"
            source.write_text("# source", encoding="utf-8")
            expected = PB_layout.layout_translation_preview_html_path(source)
            window = MagicMock(spec=MainWindow)
            window.current_source_path = source
            window.current_layout_translation_path = None
            window._layout_preview_refresh_workers = {}
            window.is_thread_running.return_value = False
            worker = MagicMock()
            worker.finished_signal = MagicMock()
            worker.finished = MagicMock()
            with patch.object(OT_ui, "load_layout_translation_bundle", return_value={"pages": []}), patch.object(
                OT_ui, "LayoutPreviewRefreshWorker", return_value=worker
            ):
                MainWindow.ensure_current_layout_translation_preview(window)
            self.assertEqual(window.current_layout_translation_path, expected)
            worker.start.assert_called_once_with()

    def test_apply_work_dir_change_updates_saved_path_and_label(self):
        class DummyWindow:
            def __init__(self):
                self.settings = OT_common.AppSettings()
                self.refresh_work_dir_label = MagicMock()
                self._active_work_dir_labels = {MagicMock()}

        with tempfile.TemporaryDirectory() as tmp_dir:
            window = DummyWindow()
            target = Path(tmp_dir).resolve()
            with patch.object(OT_ui.app_config, "save_settings") as save_settings:
                MainWindow.apply_work_dir_change(window, target)
            self.assertEqual(window.settings.work_dir, str(target))
            window.refresh_work_dir_label.assert_called_once_with()
            for label in window._active_work_dir_labels:
                label.setText.assert_called_once_with(str(target))
            save_settings.assert_called_once_with(window.settings)

    def test_migration_rewrites_mineru_task_and_image_map_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            old_dir = root / "old"
            new_dir = root / "new"
            old_dir.mkdir()
            new_dir.mkdir()
            paper = old_dir / "paper1"
            paper.mkdir()
            (paper / ".mineru_generated").write_text("{}", encoding="utf-8")
            (paper / "full.cleaned.md").write_text("# text", encoding="utf-8")
            (paper / "paper1.pdf").write_bytes(b"%PDF-1.4")
            img = paper / "images" / "image_001.jpg"
            img.parent.mkdir()
            img.write_bytes(b"jpg")

            task_meta = {
                "source_file": str(paper / "paper1.pdf"),
                "source_pdf": str(old_dir / "paper1.pdf"),
                "extract_dir": str(paper / "mineru_result"),
            }
            (paper / "mineru_task.json").write_text(json.dumps(task_meta, ensure_ascii=False, indent=2), encoding="utf-8")

            image_map = [{
                "id": "IMAGE_001",
                "clean_target": "images/image_001.jpg",
                "saved_file": str(img),
            }]
            (paper / "image_map.json").write_text(json.dumps(image_map, ensure_ascii=False, indent=2), encoding="utf-8")

            copy_workspace_data(old_dir, new_dir)

            new_task = json.loads((new_dir / "paper1" / "mineru_task.json").read_text(encoding="utf-8"))
            self.assertEqual(new_task["source_file"], str((new_dir / "paper1" / "paper1.pdf").resolve()))
            self.assertEqual(new_task["source_pdf"], str((new_dir / "paper1.pdf").resolve()))
            self.assertEqual(new_task["extract_dir"], str((new_dir / "paper1" / "mineru_result").resolve()))

            new_map = json.loads((new_dir / "paper1" / "image_map.json").read_text(encoding="utf-8"))
            self.assertEqual(new_map[0]["saved_file"], str((new_dir / "paper1" / "images" / "image_001.jpg").resolve()))

    def test_find_stored_original_strictly_prefers_local_document_files_over_stale_meta_paths(self):
        import LS_pipeline
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            old_paper = root / "old_workspace" / "paper"
            old_paper.mkdir(parents=True)
            old_pdf = old_paper / "paper.pdf"
            old_pdf.write_bytes(b"old")

            new_paper = root / "new_workspace" / "paper"
            new_paper.mkdir(parents=True)
            new_pdf = new_paper / "paper.pdf"
            new_pdf.write_bytes(b"new")

            stale_meta = {
                "source_file": str(old_pdf),
                "source_pdf": str(old_pdf),
            }
            # 即使旧文件存在且 meta 指向旧文件，find_stored_original 必须优先返回当前目录下的新文件
            found = LS_pipeline.find_stored_original(new_paper, stale_meta)
            self.assertEqual(found.resolve(), new_pdf.resolve())

    def test_load_document_image_map_prefers_local_folder_files(self):
        import AI_chat
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            old_folder = root / "old"
            old_folder.mkdir()
            old_img = old_folder / "img.png"
            old_img.write_bytes(b"old")

            new_folder = root / "new"
            new_folder.mkdir()
            new_img = new_folder / "img.png"
            new_img.write_bytes(b"new")

            image_map_data = [{
                "id": "IMG_1",
                "clean_target": "img.png",
                "saved_file": str(old_img),
            }]
            (new_folder / "image_map.json").write_text(json.dumps(image_map_data), encoding="utf-8")

            res = AI_chat.ChatWindow.load_document_image_map(new_folder)
            self.assertEqual(res["IMG_1"].resolve(), new_img.resolve())

    def test_apply_work_dir_change_synchronizes_ai_chat_settings(self):
        class DummyWindow:
            def __init__(self):
                self.settings = OT_common.AppSettings()
                self.refresh_work_dir_label = MagicMock()

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            window = DummyWindow()
            target = root / "new_workspace"
            target.mkdir()

            ai_settings_file = root / "settings_chat.json"
            with patch.object(AI_common, "APP_DIR", root), patch.object(AI_common, "SETTINGS_PATH", ai_settings_file):
                # 初始预置一个旧路径
                ai_init = AI_common.AppSettings()
                ai_init.work_dir = str(root / "old_workspace")
                AI_common.save_settings(ai_init)

                with patch.object(OT_ui.app_config, "save_settings"):
                    MainWindow.apply_work_dir_change(window, target)

    def test_model_options_worker_reports_network_failure(self):
        worker = ModelOptionsFetchWorker("gemini", "key", "https://example.invalid")
        received = []
        worker.finished_signal.connect(lambda options, error: received.append((options, error)))
        with patch.object(OT_ui, "http_json", side_effect=TimeoutError("timeout")):
            worker.run()
        self.assertEqual(received, [([], "timeout")])


if __name__ == "__main__":
    unittest.main()
