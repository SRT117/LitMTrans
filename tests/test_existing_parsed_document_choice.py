"""Regression tests for selecting a source file that was already parsed."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

import OT_ui


class ExistingParsedDocumentChoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_retranslate_is_the_first_and_default_action(self):
        parent = QWidget()
        seen = {}

        def fake_exec(dialog):
            seen["buttons"] = [button.text() for button in dialog.buttons()]
            seen["default"] = dialog.defaultButton().text()
            seen["default_object_name"] = dialog.defaultButton().objectName()
            seen["width"] = dialog.width()
            seen["reparse_width"] = next(
                button.minimumWidth()
                for button in dialog.buttons()
                if button.text() == "重新解析并翻译"
            )
            dialog._test_clicked_button = dialog.defaultButton()
            return 0

        with patch.object(QMessageBox, "exec", fake_exec), patch.object(
            QMessageBox,
            "clickedButton",
            lambda dialog: getattr(dialog, "_test_clicked_button", None),
        ):
            action = OT_ui.MainWindow.choose_existing_parsed_document_action(parent)

        self.assertEqual(action, "retranslate")
        self.assertIn("重新翻译", seen["buttons"])
        self.assertIn("重新解析并翻译", seen["buttons"])
        self.assertIn("取消", seen["buttons"])
        self.assertNotIn("Cancel", seen["buttons"])
        self.assertEqual(seen["default"], "重新翻译")
        self.assertEqual(seen["default_object_name"], "primaryButton")
        self.assertEqual(seen["width"], 600)
        self.assertGreaterEqual(seen["reparse_width"], 160)


if __name__ == "__main__":
    unittest.main()
