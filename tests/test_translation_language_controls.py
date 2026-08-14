import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import OT_ui
import machine_translate


def settings(provider_id: str):
    return SimpleNamespace(
        ai_provider=provider_id,
        providers={},
        translation_source_language="英文",
        translation_target_language="简体中文",
        translation_reference_paths=[],
        local_machine_parallelism=4,
        translation_mode="full_context",
        translation_custom_instruction="",
        layout_reading_mode=False,
    )


class TranslationLanguageControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_provider_settings_heading_links_to_selected_official_key_page(self):
        deepseek_heading = OT_ui.translation_provider_settings_title("deepseek")
        gemini_heading = OT_ui.translation_provider_settings_title("gemini")
        self.assertIn("platform.deepseek.com/api_keys", deepseek_heading)
        self.assertIn("DeepSeek API 官网创建 Key", deepseek_heading)
        self.assertIn("aistudio.google.com/app/apikey", gemini_heading)
        self.assertNotIn("href=", OT_ui.translation_provider_settings_title("oneapi"))
        self.assertIn("无需 API 密钥", OT_ui.translation_provider_settings_title("free_machine"))

    def test_new_user_startup_opens_the_combined_settings_dialog(self):
        window = SimpleNamespace(
            _startup_configuration_required=True,
            _startup_key_prompt_shown=False,
            show_mineru_options_dialog=Mock(),
            prompt_for_missing_startup_keys=Mock(),
        )
        OT_ui.MainWindow.run_startup_configuration(window)
        self.assertTrue(window._startup_key_prompt_shown)
        window.show_mineru_options_dialog.assert_called_once_with(startup=True)
        window.prompt_for_missing_startup_keys.assert_not_called()

    def test_edge_uses_visible_editable_source_and_target_suggestions(self):
        with (
            patch.object(OT_ui.app_config, "load_settings", return_value=settings(machine_translate.EDGE_LOCAL_PROVIDER)),
            patch.object(OT_ui.app_config, "load_secret", return_value=""),
        ):
            dialog = OT_ui.TranslationOptionsDialog(provider_id=machine_translate.EDGE_LOCAL_PROVIDER)
        try:
            self.assertFalse(dialog.source_combo.isHidden())
            self.assertTrue(dialog.source_combo.isEditable())
            self.assertTrue(dialog.target_combo.isEditable())
            self.assertGreaterEqual(dialog.source_combo.findData("德文"), 0)
            self.assertGreaterEqual(dialog.target_combo.findData("繁体中文"), 0)
        finally:
            dialog.deleteLater()

    def test_mtran_choices_follow_installed_pair_directions(self):
        available = {
            ("en", "zh-Hans"),
            ("de", "en"),
            ("de", "zh-Hans"),
        }
        with (
            patch.object(OT_ui.app_config, "load_settings", return_value=settings(machine_translate.MTRAN_SERVER_PROVIDER)),
            patch.object(OT_ui.app_config, "load_secret", return_value=""),
            patch.object(machine_translate, "mtran_available_language_pairs", return_value=available),
        ):
            dialog = OT_ui.TranslationOptionsDialog(provider_id=machine_translate.MTRAN_SERVER_PROVIDER)
            try:
                self.assertFalse(dialog.source_combo.isEditable())
                self.assertFalse(dialog.target_combo.isEditable())
                dialog.source_combo.setCurrentIndex(dialog.source_combo.findData("英文"))
                self.assertEqual([dialog.target_combo.itemData(i) for i in range(dialog.target_combo.count())], ["简体中文"])
                dialog.source_combo.setCurrentIndex(dialog.source_combo.findData("德文"))
                self.assertEqual(
                    {dialog.target_combo.itemData(i) for i in range(dialog.target_combo.count())},
                    {"英文", "简体中文"},
                )
            finally:
                dialog.deleteLater()

    def test_mtran_does_not_offer_languages_when_no_pack_is_installed(self):
        with (
            patch.object(OT_ui.app_config, "load_settings", return_value=settings(machine_translate.MTRAN_SERVER_PROVIDER)),
            patch.object(OT_ui.app_config, "load_secret", return_value=""),
            patch.object(machine_translate, "mtran_available_language_pairs", return_value=set()),
        ):
            dialog = OT_ui.TranslationOptionsDialog(provider_id=machine_translate.MTRAN_SERVER_PROVIDER)
        try:
            self.assertEqual(dialog.source_combo.count(), 0)
            self.assertEqual(dialog.target_combo.count(), 0)
        finally:
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
