import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import AI_common
import OT_common


class SecretSessionFallbackTests(unittest.TestCase):
    MODULES = (OT_common, AI_common)

    def setUp(self):
        for module in self.MODULES:
            module._SESSION_SECRETS.clear()
            module._SECRET_STORAGE_ERRORS.clear()
            module._FRESH_USER_DEBUG_SETTINGS = None
            os.environ.pop(module.FRESH_USER_DEBUG_ENV_VAR, None)

    def tearDown(self):
        self.setUp()

    def test_dpapi_failure_uses_memory_without_writing_plaintext(self):
        for module in self.MODULES:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with (
                    mock.patch.object(module, "APP_DIR", root),
                    mock.patch.object(module, "LEGACY_APP_DIRS", ()),
                    mock.patch.object(module, "protect_secret", side_effect=OSError("DPAPI unavailable")),
                ):
                    self.assertFalse(module.save_secret("deepseek", "api_key", "session-secret"))
                    self.assertEqual(module.load_secret("deepseek", "api_key"), "session-secret")
                    self.assertTrue(module.secret_is_session_only("deepseek", "api_key"))
                    self.assertIn("DPAPI unavailable", module.secret_storage_error("deepseek", "api_key"))
                    self.assertFalse(module.secret_path("deepseek", "api_key").exists())

    def test_later_secure_save_clears_session_only_state(self):
        for module in self.MODULES:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with (
                    mock.patch.object(module, "APP_DIR", root),
                    mock.patch.object(module, "LEGACY_APP_DIRS", ()),
                    mock.patch.object(module, "protect_secret", return_value="dpapi:encrypted"),
                ):
                    self.assertTrue(module.save_secret("mineru", "api_key", "saved-secret"))
                    stored = json.loads(module.secret_path("mineru", "api_key").read_text(encoding="utf-8"))
                    self.assertEqual(stored["value"], "dpapi:encrypted")
                    self.assertEqual(module.load_secret("mineru", "api_key"), "saved-secret")
                    self.assertFalse(module.secret_is_session_only("mineru", "api_key"))

    def test_session_fallback_is_shared_between_translation_and_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(OT_common, "APP_DIR", Path(directory)),
                mock.patch.object(OT_common, "LEGACY_APP_DIRS", ()),
                mock.patch.object(OT_common, "protect_secret", side_effect=OSError("DPAPI unavailable")),
            ):
                self.assertFalse(OT_common.save_secret("deepseek", "api_key", "shared-session-secret"))
                self.assertEqual(AI_common.load_secret("deepseek", "api_key"), "shared-session-secret")
                self.assertTrue(AI_common.secret_is_session_only("deepseek", "api_key"))

    def test_delete_secret_clears_session_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(AI_common, "APP_DIR", Path(directory)),
                mock.patch.object(AI_common, "LEGACY_APP_DIRS", ()),
                mock.patch.object(AI_common, "protect_secret", side_effect=OSError("DPAPI unavailable")),
            ):
                AI_common.save_secret("deepseek", "api_key", "session-secret")
                AI_common.delete_secret("deepseek", "api_key")
                self.assertEqual(AI_common.load_secret("deepseek", "api_key"), "")
                self.assertFalse(AI_common.secret_is_session_only("deepseek", "api_key"))

    def test_fresh_user_debug_ignores_disk_settings_and_secrets(self):
        for module in self.MODULES:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                module._SESSION_SECRETS.clear()
                module._SECRET_STORAGE_ERRORS.clear()
                module._FRESH_USER_DEBUG_SETTINGS = None
                root = Path(directory)
                settings_path = root / "settings.json"
                secret_file = root / "secrets" / "deepseek.api_key.json"
                secret_file.parent.mkdir(parents=True)
                settings_path.write_text(json.dumps({"work_dir": "C:/existing-workspace"}), encoding="utf-8")
                secret_file.write_text(json.dumps({"value": "plain64:b2xkLXNlY3JldA=="}), encoding="utf-8")
                with (
                    mock.patch.object(module, "APP_DIR", root),
                    mock.patch.object(module, "SETTINGS_PATH", settings_path),
                    mock.patch.object(module, "LEGACY_APP_DIRS", ()),
                    mock.patch.dict(os.environ, {module.FRESH_USER_DEBUG_ENV_VAR: "1"}),
                ):
                    self.assertEqual(module.load_settings().work_dir, "")
                    self.assertEqual(module.load_secret("deepseek", "api_key"), "")

                    settings = module.AppSettings(work_dir="C:/session-workspace")
                    module.save_settings(settings)
                    self.assertIs(module.load_settings(), settings)
                    self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8"))["work_dir"], "C:/existing-workspace")

                    self.assertFalse(module.save_secret("deepseek", "api_key", "session-secret"))
                    self.assertEqual(module.load_secret("deepseek", "api_key"), "session-secret")
                    self.assertTrue(module.secret_is_session_only("deepseek", "api_key"))
                    self.assertEqual(
                        json.loads(secret_file.read_text(encoding="utf-8"))["value"],
                        "plain64:b2xkLXNlY3JldA==",
                    )


if __name__ == "__main__":
    unittest.main()
