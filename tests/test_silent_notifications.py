"""Regression tests for the application's no-sound notification policy."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from AI_common import configure_silent_application, create_silent_message_box


class SilentNotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_silent_application()

    def test_message_box_factory_disables_native_dialogs_only_for_messages(self):
        box = create_silent_message_box()
        self.assertTrue(box.testOption(QMessageBox.Option.DontUseNativeDialog))
        self.assertFalse(
            QApplication.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
        )

    def test_static_message_helpers_create_non_native_message_boxes(self):
        seen = []

        def fake_exec(box):
            seen.append(box.testOption(QMessageBox.Option.DontUseNativeDialog))
            return QMessageBox.StandardButton.No

        with patch.object(QMessageBox, "exec", fake_exec):
            QMessageBox.information(None, "测试", "信息")
            QMessageBox.warning(None, "测试", "警告")
            QMessageBox.critical(None, "测试", "错误")
            QMessageBox.question(
                None,
                "测试",
                "确认",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )

        self.assertEqual(seen, [True, True, True, True])


if __name__ == "__main__":
    unittest.main()
