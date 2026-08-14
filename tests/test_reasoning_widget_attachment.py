"""Regression coverage for recovering a streamed reasoning panel."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLabel, QVBoxLayout, QWidget

from AI_common import AppSettings
from AI_chat import ChatWindow
from AI_widgets import CollapsibleReasoningWidget


class ReasoningWidgetAttachmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_recovered_reasoning_widget_is_inserted_into_reply_turn(self):
        turn = QWidget()
        layout = QVBoxLayout(turn)
        bubble = QLabel("answer")
        layout.addWidget(bubble)
        reasoning = CollapsibleReasoningWidget()

        attached = ChatWindow.attach_reasoning_widget_to_assistant_turn(bubble, reasoning)

        self.assertTrue(attached)
        self.assertIs(reasoning.parentWidget(), turn)
        self.assertIs(layout.itemAt(0).widget(), reasoning)
        self.assertIs(layout.itemAt(1).widget(), bubble)

    def test_completed_reply_collapses_streamed_reasoning(self):
        reasoning = SimpleNamespace(
            reasoning_text="正在显示的思考过程",
            set_expanded=Mock(),
        )
        chat = SimpleNamespace(
            flush_stream_buffers=Mock(),
            is_chat_near_bottom=Mock(return_value=False),
            pending_reply_insert_index=None,
            messages=[],
            chat_worker=None,
            current_assistant_label=None,
            current_reasoning_widget=reasoning,
            commit_new_manual_images_in_history=Mock(),
            save_current_conversation_to_history=Mock(),
            schedule_reply_render_scroll_to_bottom=Mock(),
            set_chat_buttons_enabled=Mock(),
            clear_chat_widgets_only=Mock(),
            render_messages_from_history=Mock(),
            resume_pending_embedded_document_load=Mock(),
            cancel_requested=False,
        )

        ChatWindow.on_finished_reply(chat, "最终回答")

        reasoning.set_expanded.assert_called_once_with(False)

    def test_reasoning_preferences_are_isolated_by_provider_and_model(self):
        mode = QComboBox()
        mode.addItem("默认", "default")
        mode.addItem("开启", "enabled")
        effort = QComboBox()
        effort.setEditable(True)
        visible = QCheckBox()
        model = QComboBox()
        model.addItems(["deepseek-ai/DeepSeek-R1", "deepseek-ai/DeepSeek-V3"])
        chat = SimpleNamespace(
            settings=AppSettings(),
            model_combo=model,
            thinking_mode_combo=mode,
            reasoning_effort_combo=effort,
            show_reasoning_checkbox=visible,
            _restoring_reasoning_preferences=False,
            get_current_provider=lambda: "siliconflow",
        )
        mode.setCurrentIndex(1)
        effort.setCurrentText("high")
        visible.setChecked(False)

        with patch("AI_chat.app_config.save_settings"):
            ChatWindow.save_reasoning_preferences(chat)
            model.setCurrentText("deepseek-ai/DeepSeek-V3")
            mode.setCurrentIndex(0)
            effort.setCurrentText("minimal")
            visible.setChecked(True)
            ChatWindow.save_reasoning_preferences(chat)

            model.setCurrentText("deepseek-ai/DeepSeek-R1")
            ChatWindow.restore_reasoning_preferences(chat)

        self.assertEqual(mode.currentData(), "enabled")
        self.assertEqual(effort.currentText(), "high")
        self.assertFalse(visible.isChecked())
        self.assertEqual(len(chat.settings.chat_reasoning_preferences), 2)
