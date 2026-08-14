"""LitMTrans document-chat window and session entry points."""

from __future__ import annotations

import hashlib
import uuid

from AI_widgets import *
from AI_history import (
    atomic_write_chat_sessions,
    chat_asset_dir,
    externalize_chat_images,
    hydrate_chat_images,
    prune_unreferenced_chat_assets,
)
import AI_common as chat_settings_module

# Document chat keeps its own settings so embedded conversations do not alter translation settings.
app_config = chat_settings_module.app_config

DIAGRAM_CHINESE_INSTRUCTION = (
    "语言要求：除 evidence 中逐字引用的 quote 必须保持论文原文外，title、所有节点的 label/detail、"
    "以及边的可见 label 必须使用简体中文。专业名词可在中文后保留必要的英文名称或缩写，"
    "但不得因为论文原文是英文而输出整句英文。协议字段 id、kind、type、role、relation 仍按协议使用英文值。"
)

_SECRET_SESSION_WARNINGS: set[tuple[str, str]] = set()


def warn_chat_secret_session_only(parent, provider_id: str, name: str) -> None:
    key = (str(provider_id), str(name))
    if app_config.secret_is_session_only(provider_id, name) and key not in _SECRET_SESSION_WARNINGS:
        _SECRET_SESSION_WARNINGS.add(key)
        detail = app_config.secret_storage_error(provider_id, name)
        QMessageBox.warning(
            parent,
            "密钥仅在本次运行中有效",
            "Windows 安全存储暂时不可用。密钥没有写入磁盘，但本次运行仍可正常使用；"
            "关闭后本次输入不会保留。下次将使用此前已保存的密钥（如果有），否则需要重新填写。"
            + (f"\n\n错误信息：{detail}" if detail else ""),
        )


def save_chat_secret_with_session_fallback(parent, provider_id: str, name: str, value: str) -> bool:
    persisted = bool(app_config.save_secret(provider_id, name, value))
    if not persisted:
        warn_chat_secret_session_only(parent, provider_id, name)
    return persisted


DOCUMENT_AI_TASKS = {
    "key_points": {
        "label": "要点提炼",
        "question": "请提炼当前论文的核心要点。",
        "instruction": "这是要点提炼任务：用最少信息帮助读者判断论文做了什么、如何获得证据、核心发现及适用边界。以问题、方法、结果、边界为主要认知面（文档类型不适合时可自适应），通常 8–14 个节点；保留影响判断的指标、比较、条件和不确定性，不补充文献未支持的批评。为最重要的结果或结论补充原文逐字 evidence quote（{type:\"quote\",quote:\"…\"}），不能可靠逐字引用时留空。用户的偏好只能改变关注重点，不能改变图形协议。" + DIAGRAM_CHINESE_INSTRUCTION,
        "format": "只输出图形协议：第一行必须是 <!-- litmtrans-mindmap-v2 -->，随后只输出一个 JSON 对象。对象必须包含 version:2、mode、title、nodes。nodes 只有一个 parentId 为 null 的 root；每个节点必须有唯一 ASCII id、parentId、包含完整表达语义的 label、可选 detail、kind、importance(1-3)、可选 evidence（必须是对象数组）。注意：图表引擎不支持渲染 LaTeX，请绝对不要在 label 和 detail 中使用任何 LaTeX 公式或反斜杠转义符号，必须全部使用纯文本或 Unicode 字符替代（例如用 H₂O 代替公式写法，用 cm⁻¹ 代替复杂的物理单位公式）。不得输出 Markdown、代码围栏、坐标、颜色、SVG 或任何额外文字。",
    },
    "paper_mindmap": {
        "label": "思维导图",
        "question": "请建立当前论文的完整知识结构图。",
        "instruction": "为当前论文建立完整科研认知地图。先判断论文类型，再围绕核心问题或贡献组织树：研究背景/缺口、问题或假设、设计与关键方法、数据或证据、主要结果、机制或推理、验证与比较、贡献、适用边界。不要把目录或 Introduction/Methods/Results/Discussion 当作分支；用 25–55 个有价值节点（短文可更少），3–4 层为主，label 应包含完整的知识认知要点，无需刻意简短或拆分；原始证据放入 evidence。为最重要的结果、方法、结论或边界节点补充 evidence：使用 {type:\"quote\",quote:\"…\"}，quote 必须逐字复制当前文献原文语言的短句，不能翻译、改写或编造；没有可靠短句时留空。只使用文献直接支持的信息。" + DIAGRAM_CHINESE_INSTRUCTION,
        "format": "只输出图形协议：第一行必须是 <!-- litmtrans-mindmap-v2 -->，随后只输出一个 JSON 对象。对象必须包含 version:2、mode、title、nodes。nodes 只有一个 parentId 为 null 的 root；每个节点必须有唯一 ASCII id、parentId、包含完整表达语义的 label、可选 detail、kind、importance(1-3)、可选 evidence（必须是对象数组）。注意：图表引擎不支持渲染 LaTeX，请绝对不要在 label 和 detail 中使用任何 LaTeX 公式或反斜杠转义符号，必须全部使用纯文本或 Unicode 字符替代（例如用 H₂O 代替公式写法，用 cm⁻¹ 代替复杂的物理单位公式）。不得输出 Markdown、代码围栏、坐标、颜色、SVG 或任何额外文字。",
    },
    "paper_logic_flow": {
        "label": "思路流程",
        "question": "请重建当前论文的研究逻辑与证据链。",
        "instruction": "重建当前论文的研究逻辑与证据链，而不是章节目录或单纯实验步骤。根据论文类型组织从背景/痛点、缺口、研究问题或假设、核心设计、关键证据、结果、推理/机制、结论到边界的有向关系；边优先表达 motivates/tests/produces/supports/explains/validates/limits。通常保留 8–22 个主节点，必要时并行证据汇合。只有真实条件判断才使用 decision，绝不为了装饰滥用菱形或数据库。为关键证据和结论节点补充原文逐字 evidence quote，格式为 {type:\"quote\",quote:\"…\"}；不能可靠逐字引用时留空。只使用文献直接支持的信息。" + DIAGRAM_CHINESE_INSTRUCTION,
        "format": "只输出图形协议：第一行必须是 <!-- litmtrans-flowchart-v2 -->，随后只输出一个 JSON 对象。对象必须包含 version:2、mode、title、layout、nodes、edges。节点有唯一 ASCII id、type、role、包含完整表达语义的 label（字段名必须且只能是 label）、可选 detail、importance(1-3)、可选 evidence（必须是对象数组）。注意：图表引擎不支持渲染 LaTeX，请绝对不要在节点或边中使用任何 LaTeX 公式或反斜杠转义符号，必须全部使用纯文本或 Unicode 字符替代（例如用 H₂O 代替公式写法，用 cm⁻¹ 代替复杂的物理单位公式）。type 只可为 terminator/process/decision/io/subprocess/database/document，role 只表达科研角色。边必须有 from/to/relation 和可选短 label。不得输出 Markdown、代码围栏、坐标、颜色、SVG 或任何额外文字。",
    },
}


def is_machine_translation_provider_id(provider_id: str) -> bool:
    return str(provider_id or "").strip().lower() in {"free_machine", "machine_translate", "mtranserver_local"}


class ChatWindow(QWidget):
    def __init__(self, embedded: bool = False):
        super().__init__()
        self.embedded = bool(embedded)

        self.setWindowTitle("LitMTrans - 文献对话")
        self.resize(950, 720)

        # 纯净对话：不内置 system，不追加额外指引。
        self.messages = []

        self.chat_worker = None
        self.model_worker = None
        self.thinking_probe_worker = None
        self._pending_thinking_probe = None
        self._silent_model_refresh = False
        self._model_refresh_provider_id = ""
        self._pending_model_refresh_provider_id = ""
        self.document_parse_worker = None
        self.session_model = None
        self._syncing_shared_settings = False
        self.local_reference_image_path = ""
        self.settings = app_config.load_settings()
        self.ensure_chat_settings_fields(self.settings)
        self.shared_app_settings = None
        self.shared_settings_save_callback = None
        self.shared_secret_save_callback = None
        cleanup_non_multimodal_model_marks()
        cleanup_thinking_capabilities()

        # 系统居中提示气泡列表。用于窗口尺寸变化时自动保持 70% 宽度。
        self.system_bubbles = []

        # 输入框中待发送的多模态图片附件。
        # 每项包含：data_url / pixmap / name。
        self.pending_input_images = []
        self.selected_reference_images = []
        self.document_contexts = []
        self.document_context_sent = False
        self.pending_document_parse_output_dir = None
        self.pending_document_parse_source_path = None
        self.pending_document_parse_cancel_requested = False
        self.current_embedded_document_path: Path | None = None
        self.current_embedded_document_fingerprint = ""
        # 切换文献时不能中断正在进行的请求。宿主会在回复结束后继续这次切换，
        # 避免界面标题已经指向 B、实际消息却仍属于 A 的错配。
        self.pending_embedded_document_load: tuple[str, str, Path] | None = None
        self.embedded_document_loaded_callback = None
        self.document_parse_status_text = ""
        self.document_parse_progress_percent = -1

        # 用户从主阅读器右键询问时，本轮引用内容只作为小气泡显示；
        # 真正发送给模型时会合入用户问题，避免用户还要在输入框里重复看到大段引用。
        self.pending_reference_quotes = []
        # Aggregated compatibility view used by persisted messages and older
        # call sites. The input UI itself keeps every quote separately.
        self.pending_reference_quote = None

        # 点击“已发送文档”气泡后打开的 MinerU 阅读窗口。
        # 必须保存引用，避免窗口被 Python 垃圾回收后自动关闭。
        self.document_reader_windows = []
        # 宿主阅读器可注入此回调。引用气泡应优先回到正在阅读的同一篇文献，
        # 而不是每次都额外打开一个阅读窗口。
        self.reference_quote_reveal_callback = None

        # 对话记录持久化：
        # 1. current_session_id 为空时，首次发送前会要求用户命名对话。
        # 2. conversation_sessions 保存到本地 JSON，方便继续对话或删除记录。
        self.conversation_sessions = []
        self.current_session_id = ""
        self.current_conversation_name = ""
        # 仅供 OneAPI 缓存关联使用的随机公开 ID；绝不从本地路径推导。
        self.api_cache_session_id = ""

        # Text bubbles created for model replies; refreshed when Markdown display changes.
        self.assistant_bubbles = []
        self.chat_bubbles = []

        # 当前正在流式输出的模型文本气泡
        self.current_assistant_label = None
        self.pending_reply_insert_index = None
        self._startup_key_prompt_shown = False

        # 当前正在流式输出的思考过程控件。
        # 注意：思考过程只用于展示，不进入普通多轮 messages。
        self.current_reasoning_widget = None

        # 流式 UI 节流：
        # 模型 chunk 可能非常碎，不能每个 chunk 都立即 setText + 重新布局。
        # 这里先把正文和思考过程分别缓存起来，再用 QTimer 每 80ms 批量刷新一次界面。
        self.pending_assistant_text = ""
        self.pending_reasoning_text = ""
        self.system_message_history = []
        self.stream_flush_timer = QTimer(self)
        self.stream_flush_timer.setInterval(80)
        self.stream_flush_timer.setSingleShot(True)
        self.stream_flush_timer.timeout.connect(self.flush_stream_buffers)
        self.bubble_width_update_timer = QTimer(self)
        self.bubble_width_update_timer.setInterval(0)
        self.bubble_width_update_timer.setSingleShot(True)
        self.bubble_width_update_timer.timeout.connect(self.update_system_bubble_widths)
        self.system_toast_timer = QTimer(self)
        self.system_toast_timer.setInterval(2000)
        self.system_toast_timer.setSingleShot(True)
        self.system_toast_timer.timeout.connect(self.hide_system_message_toast)

        # 流式输出时的自动滚动策略：
        # 只有用户本来就在底部附近时才自动滚到底部；
        # 如果用户手动向上滚动查看历史，流式刷新不得强行把滚动条拉回底部。
        self._stream_should_auto_scroll = True
        # A pending geometry/layout pass may change the scroll range after a
        # streamed chunk is painted.  This timer keeps the viewport attached
        # to the newest output while the user is still following the reply.
        self._stream_scroll_timer = QTimer(self)
        self._stream_scroll_timer.setSingleShot(True)
        self._stream_scroll_timer.timeout.connect(self.scroll_stream_to_bottom_after_layout)
        # 新消息插入后，嵌入式侧栏有时会在多个事件周期内才完成高度重算。
        # 标记用于合并同一轮发送产生的多次“滚到底部”请求。
        self._bottom_scroll_after_layout_pending = False

        # 用户是否点击了“停止生成”。
        self.cancel_requested = False

        # “停止生成”按钮边框动画：
        # 使用定时器循环点亮上下左右边框，模拟 #ED5126 沿按钮边框转动。
        self.send_button_border_phase = 0
        self.send_button_border_timer = QTimer(self)
        self.send_button_border_timer.setInterval(140)
        self.send_button_border_timer.timeout.connect(self.update_send_button_stop_animation)
        self._updating_bubble_widths = False

        self._last_normal_bubble_width = 0
        self._last_system_bubble_width = 0
        self._bulk_rendering_messages = False
        # 用户消息索引到其聊天行的映射，供右上角的对话定位器直接跳转。
        self.message_row_widgets: dict[int, QWidget] = {}

        self.init_ui()
        if self.embedded:
            self.configure_embedded_mode()
        else:
            QTimer.singleShot(500, self.prompt_for_missing_startup_keys)
        # 启动后做一次安全清理：只清理未完成解析残留和无用 zip，不删除仍被历史记录引用的解析目录。
        QTimer.singleShot(1500, self.cleanup_stale_parse_outputs)

    def closeEvent(self, event):
        self.shutdown_for_application_exit()
        super().closeEvent(event)

    def shutdown_for_application_exit(self):
        """Release chat renderers and request worker cancellation during exit."""
        if getattr(self, "_shutdown_started", False):
            return
        self._shutdown_started = True
        for timer_name in (
            "stream_flush_timer",
            "_stream_scroll_timer",
            "bubble_width_update_timer",
            "system_toast_timer",
            "send_button_border_timer",
        ):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                try:
                    timer.stop()
                except RuntimeError:
                    pass
        for worker in (getattr(self, "chat_worker", None), getattr(self, "model_worker", None), getattr(self, "document_parse_worker", None)):
            try:
                if worker is not None and worker.isRunning():
                    request_stop = getattr(worker, "request_stop", None)
                    if callable(request_stop):
                        request_stop()
                    else:
                        worker.requestInterruption()
            except (AttributeError, RuntimeError):
                pass
        for reader in list(getattr(self, "document_reader_windows", [])):
            try:
                shutdown = getattr(reader, "shutdown_webengines", None)
                if callable(shutdown):
                    shutdown()
                reader.close()
                reader.deleteLater()
            except RuntimeError:
                pass
        self.document_reader_windows = []

        # Chat bubbles lazily own QWebEngineViews for Markdown/MathJax.  Make
        # their renderer shutdown explicit instead of waiting for widget GC.
        if CHAT_WEBENGINE_AVAILABLE and QWebEngineView is not None:
            for web_view in self.findChildren(QWebEngineView):
                try:
                    web_view.stop()
                    web_view.hide()
                    web_view.setParent(None)
                    web_view.deleteLater()
                except RuntimeError:
                    pass

    def configure_embedded_mode(self):
        self.setWindowTitle("文献对话")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        if hasattr(self, "left_panel"):
            self.left_panel.setVisible(False)
        if hasattr(self, "main_splitter"):
            self.main_splitter.setSizes([0, 1])
        if hasattr(self, "model_options_button"):
            self.model_options_button.setText("对话模型设置")
            try:
                self.model_options_button.toggled.disconnect(self.set_model_options_visible)
            except Exception:
                pass
            self.model_options_button.setCheckable(False)
            self.model_options_button.clicked.connect(self.open_model_settings_dialog)
        if hasattr(self, "refresh_models_button"):
            self.refresh_models_button.setText("刷新模型列表")
            self.refresh_models_button.setVisible(True)
            self.refresh_models_button.setToolTip("按当前服务商、API 密钥和服务地址刷新模型列表")
        if hasattr(self, "model_combo"):
            self.model_combo.setMinimumWidth(90)
            self.model_combo.setToolTip("当前对话模型；切换后会保存到当前服务商设置")
        self.create_embedded_top_bar()
        if hasattr(self, "history_group"):
            self.history_group.setVisible(False)
        if hasattr(self, "image_group"):
            self.image_group.setVisible(False)
        if hasattr(self, "input_box"):
            self.input_box.setFixedHeight(118)
        if hasattr(self, "chat_main_layout"):
            self.chat_main_layout.setSpacing(8)
            self.chat_main_layout.setContentsMargins(0, 0, 0, 0)
        if hasattr(self, "document_status_label"):
            self.document_status_label.setMaximumHeight(38)
        self.apply_embedded_compact_style()

    def create_embedded_top_bar(self):
        if hasattr(self, "embedded_top_bar"):
            return

        self.embedded_top_bar = QWidget()
        self.embedded_top_bar.setObjectName("embeddedTopBar")
        self.model_row_widget.setVisible(False)
        top_layout = QHBoxLayout(self.embedded_top_bar)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)

        top_button_height = 34
        # 这些控件分别受到 QToolButton、QComboBox 和 QPushButton 的全局样式影响。
        # 仅调用 setFixedHeight 会被其中的 min-height / padding 再次撑开，
        # 所以在控件级别同步压平三者的纵向尺寸。
        embedded_tool_button_style = f"""
            QToolButton {{
                /* QToolButton 的内容盒比普通按钮少 2px，补偿后外框同为 38px。 */
                min-height: {top_button_height + 2}px;
                max-height: {top_button_height + 2}px;
                padding: 0px 9px;
            }}
        """
        embedded_combo_style = f"""
            QComboBox {{
                min-height: {top_button_height}px;
                max-height: {top_button_height}px;
                padding: 0px 28px 0px 8px;
            }}
        """
        embedded_button_style = f"""
            QPushButton {{
                min-height: {top_button_height}px;
                max-height: {top_button_height}px;
                padding: 0px 10px;
            }}
        """
        self.model_options_button.setFixedHeight(top_button_height)
        self.model_combo.setFixedHeight(top_button_height)
        self.refresh_models_button.setFixedHeight(top_button_height)
        self.model_options_button.setStyleSheet(embedded_tool_button_style)
        self.model_combo.setStyleSheet(embedded_combo_style)
        self.refresh_models_button.setStyleSheet(embedded_button_style)
        self.model_options_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.model_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.refresh_models_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        top_layout.addWidget(self.model_options_button, 0, Qt.AlignmentFlag.AlignVCenter)
        top_layout.addWidget(self.model_combo, 1, Qt.AlignmentFlag.AlignVCenter)
        top_layout.addWidget(self.refresh_models_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.message_history_button = QPushButton("系统消息")
        self.message_history_button.setToolTip("查看本次会话的状态和错误信息")
        self.message_history_button.clicked.connect(self.show_system_messages_dialog)
        self.message_history_button.setFixedHeight(top_button_height)
        self.message_history_button.setStyleSheet(embedded_button_style)
        self.message_history_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        top_layout.addWidget(self.message_history_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.system_toast_label = QLabel("")
        self.system_toast_label.setObjectName("systemToastLabel")
        self.system_toast_label.setWordWrap(False)
        self.system_toast_label.setVisible(False)
        self.system_toast_label.setFixedHeight(AGENT_UI_METRICS["action_button_height"])
        self.system_toast_label.setMinimumWidth(0)
        self.system_toast_label.setMaximumWidth(260)
        self.system_toast_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        top_layout.addWidget(self.system_toast_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self.chat_main_layout.insertWidget(0, self.embedded_top_bar)

    def apply_embedded_compact_style(self):
        self.setObjectName("embeddedChatWindow")
        self.setStyleSheet(self.styleSheet() + f"""
            QWidget#embeddedChatWindow {{
                background: transparent;
            }}
            QToolButton {{
                background: {COLOR_BG_SURFACE_2};
                color: {COLOR_TEXT_PRIMARY};
                border: 1px solid {COLOR_BORDER_HAIR};
                border-radius: 2px;
                padding: 4px 9px;
                font-weight: 650;
            }}
            QToolButton:hover {{
                background: {COLOR_ACCENT};
                color: #FFFFFF;
                border-color: {COLOR_ACCENT};
            }}
            QScrollArea {{
                background-color: {COLOR_BG_SURFACE_2};
                border: 1px solid {COLOR_BORDER_HAIR};
                border-radius: 0px;
            }}
            QPlainTextEdit {{
                background-color: {COLOR_BG_SURFACE_2};
                color: {COLOR_TEXT_PRIMARY};
                border: 1px solid {COLOR_BORDER_HAIR};
                border-radius: 2px;
            }}
        """)

    def open_model_settings_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("对话模型设置")
        # 这个窗口展示的是完整表单，不沿用嵌入式顶部栏的紧凑尺寸。
        # 520px 宽度会使模型行和思考设置在 Qt 计算最小尺寸时相互挤压，
        # 从而出现下拉框或按钮文字被裁切的情况。
        dialog.resize(640, 620)
        dialog.setMinimumSize(600, 590)
        dialog.setStyleSheet(self.styleSheet())
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        model_group = QGroupBox("模型")
        model_group_layout = QHBoxLayout(model_group)
        # 明确预留标题、控件及上下留白所需的高度，防止被底部操作区压缩。
        model_group.setMinimumHeight(76)
        model_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        model_group_layout.setContentsMargins(12, 12, 12, 10)
        model_group_layout.setSpacing(10)
        model_label = QLabel("模型:")
        model_label.setFixedWidth(AGENT_UI_METRICS["form_label_width"])
        # 顶部栏中“刷新”可使用紧凑宽度；在表单中保留更舒适的点击热区，
        # 并在关闭窗口后还原，避免影响嵌入式阅读器的顶部布局。
        refresh_button_min_width = self.refresh_models_button.minimumWidth()
        self.refresh_models_button.setMinimumWidth(72)
        model_group_layout.addWidget(model_label)
        model_group_layout.addWidget(self.model_combo, 1)
        model_group_layout.addWidget(self.refresh_models_button)

        moved_widgets = [
            getattr(self, "api_group", None),
            model_group,
            getattr(self, "reasoning_group", None),
            getattr(self, "render_markdown_checkbox", None),
        ]
        for widget in moved_widgets:
            if widget is not None:
                widget.setParent(content)
                content_layout.addWidget(widget)
                widget.setVisible(True)
        # 窗口重开时按当前服务商和模型恢复可见性，不能保留临时强制显示。
        self.update_reasoning_visibility()
        layout.addWidget(content, 1)

        request_body_button = QPushButton()
        request_body_button.setMinimumHeight(38)
        request_body_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        def refresh_request_body_button():
            # OneAPI uses the OpenAI Chat Completions format; no protocol selector is needed.
            request_body_button.setVisible(False)

        def edit_request_body_construction():
            return

        request_body_button.clicked.connect(edit_request_body_construction)
        self.provider_combo.currentIndexChanged.connect(refresh_request_body_button)
        refresh_request_body_button()
        layout.addWidget(request_body_button)

        prompt_button = QPushButton("编辑要点提炼指引")
        prompt_button.setMinimumHeight(38)
        prompt_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        prompt_button.clicked.connect(self.open_key_points_prompt_dialog)
        layout.addWidget(prompt_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.setMinimumHeight(38)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        try:
            dialog.exec()
        finally:
            try:
                self.provider_combo.currentIndexChanged.disconnect(refresh_request_body_button)
            except (TypeError, RuntimeError):
                pass
            self.refresh_models_button.setMinimumWidth(refresh_button_min_width)
            model_group_layout.removeWidget(self.model_combo)
            model_group_layout.removeWidget(self.refresh_models_button)
            if self.embedded and hasattr(self, "embedded_top_bar"):
                top_layout = self.embedded_top_bar.layout()
                self.model_combo.setParent(self.embedded_top_bar)
                self.refresh_models_button.setParent(self.embedded_top_bar)
                top_layout.insertWidget(1, self.model_combo, 1, Qt.AlignmentFlag.AlignVCenter)
                top_layout.insertWidget(2, self.refresh_models_button, 0, Qt.AlignmentFlag.AlignVCenter)
                self.model_combo.setVisible(True)
                self.refresh_models_button.setVisible(True)
                self.model_row_widget.setVisible(False)
                for widget in (getattr(self, "api_group", None), getattr(self, "reasoning_group", None), getattr(self, "render_markdown_checkbox", None)):
                    if widget is not None:
                        widget.setParent(self.model_options_panel)
                        self.model_options_layout.addWidget(widget)
                        widget.setVisible(False)
            else:
                row_layout = self.model_row_widget.layout().itemAt(0).layout()
                self.model_combo.setParent(self.model_row_widget)
                self.refresh_models_button.setParent(self.model_row_widget)
                row_layout.insertWidget(1, self.model_combo, 1)
                row_layout.insertWidget(2, self.refresh_models_button)
                self.model_row_widget.setVisible(True)
                for widget in (getattr(self, "api_group", None), getattr(self, "reasoning_group", None), getattr(self, "render_markdown_checkbox", None)):
                    if widget is not None:
                        widget.setParent(self.model_options_panel)
                        self.model_options_layout.addWidget(widget)
                        widget.setVisible(False)

    def key_points_default_prompt(self) -> str:
        try:
            from LS_pipeline import DEFAULT_KEY_POINTS_PROMPT
            return str(DEFAULT_KEY_POINTS_PROMPT or "")
        except Exception:
            return "【要点提炼任务】\n请阅读当前文档，并提炼其核心内容。"

    def key_points_prompt(self) -> str:
        source_settings = self.shared_app_settings or self.settings
        prompt = str(getattr(source_settings, "key_points_prompt", "") or "").strip()
        return prompt or self.key_points_default_prompt()

    def save_key_points_prompt_setting(self, prompt: str):
        if self.shared_app_settings is not None:
            self.shared_app_settings.key_points_prompt = prompt
            if callable(self.shared_settings_save_callback):
                self.shared_settings_save_callback(self.shared_app_settings)
            return
        self.settings.key_points_prompt = prompt
        app_config.save_settings(self.settings)

    def open_key_points_prompt_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑要点提炼指引")
        dialog.resize(680, 520)
        dialog.setStyleSheet(self.styleSheet())

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        editor = QPlainTextEdit()
        editor.setPlainText(self.key_points_prompt())
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(editor, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        restore_button = QPushButton("恢复默认")
        buttons.addButton(restore_button, QDialogButtonBox.ButtonRole.ResetRole)
        layout.addWidget(buttons)

        def restore_default():
            editor.setPlainText(self.key_points_default_prompt())

        def save_prompt():
            prompt = editor.toPlainText().strip()
            default_prompt = self.key_points_default_prompt().strip()
            self.save_key_points_prompt_setting("" if prompt == default_prompt else prompt)
            QMessageBox.information(dialog, "已保存", "要点提炼指引已保存。")
            dialog.accept()

        restore_button.clicked.connect(restore_default)
        buttons.accepted.connect(save_prompt)
        buttons.rejected.connect(dialog.reject)
        dialog.exec()

    def _document_task_has_context(self) -> bool:
        """Return whether a document-wide task can be grounded in a paper."""
        path = getattr(self, "current_embedded_document_path", None)
        if isinstance(path, Path) and path.exists():
            return True
        if self.document_contexts or self.document_context_sent:
            return True
        return any(
            isinstance(message, dict) and self.is_document_context_history_message(message.get("content"))
            for message in self.messages
        )

    def prepare_document_ai_task(self, task_type: str, document_context: dict | None = None) -> bool:
        """Place a validated Zotero-parity task in the composer without sending it."""
        task = DOCUMENT_AI_TASKS.get(str(task_type or ""))
        if task is None:
            return False
        if not self._document_task_has_context():
            QMessageBox.information(self, "暂无文档", "请先打开或添加一个已解析的文档，再使用文献导图功能。")
            return False

        context = dict(document_context or {})
        current_path = getattr(self, "current_embedded_document_path", None)
        if isinstance(current_path, Path) and current_path.exists():
            context.setdefault("markdown_path", str(current_path))
            context.setdefault("document_path", str(current_path))
            context.setdefault("source_markdown_path", str(current_path))
            context.setdefault("title", current_path.parent.name)
        context.setdefault("pane", "source")
        context.setdefault("render_mode", "stream")

        instruction = task["instruction"]
        if task_type == "key_points":
            custom = str(getattr(self.shared_app_settings or self.settings, "key_points_prompt", "") or "").strip()
            if custom:
                instruction += f"\n\n用户的要点关注偏好（不能改变图形协议）：\n{custom}"
        context["text"] = f"{instruction}\n\n{task['format']}"
        self.set_pending_reference_quote(context)
        self.input_box.setPlainText(task["question"])
        self.input_box.setFocus()
        return True

    def submit_document_ai_task(self, task_type: str) -> bool:
        """Prepare and immediately send one document-wide visual analysis task."""
        if self.chat_worker and self.chat_worker.isRunning():
            QMessageBox.information(self, "正在回复", "请等待当前回复完成后，再生成新的导图。")
            return False
        if not self.prepare_document_ai_task(task_type):
            return False
        self.send_message()
        return True

    def init_ui(self):
        # ================= 主界面布局 =================
        # 左侧：供应商、模型、思考、文档、对话记录
        # 右侧：聊天区、输入框、发送按钮
        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setObjectName("mainInvisibleSplitter")

        # 保留左右栏宽度拖拽功能，但隐藏分隔条的灰线、黄线或阴影视觉效果。
        # handleWidth 只决定可拖拽热区宽度；样式表把该热区绘制为完全透明。
        self.main_splitter.setHandleWidth(AGENT_UI_METRICS["splitter_handle_width"])
        self.main_splitter.setStyleSheet("""
            QSplitter#mainInvisibleSplitter::handle {
                background-color: transparent;
                border: none;
            }
            QSplitter#mainInvisibleSplitter::handle:hover {
                background-color: transparent;
                border: none;
            }
            QSplitter#mainInvisibleSplitter::handle:pressed {
                background-color: transparent;
                border: none;
            }
            QSplitter#mainInvisibleSplitter::handle:horizontal {
                width: 8px;
                background-color: transparent;
                border: none;
            }
        """)

        root_layout.addWidget(self.main_splitter)

        self.left_panel = QWidget()
        self.left_panel.setMinimumWidth(AGENT_UI_METRICS["left_panel_min_width"])
        self.left_panel.setMaximumWidth(AGENT_UI_METRICS["left_panel_max_width"])

        main_layout = QVBoxLayout(self.left_panel)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(AGENT_UI_METRICS["left_panel_spacing"])
        self.model_options_button = QToolButton()
        self.model_options_button.setText("模型选项")
        self.model_options_button.setCheckable(True)
        self.model_options_button.setChecked(False)
        self.model_options_button.setArrowType(Qt.ArrowType.RightArrow)
        self.model_options_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.model_options_panel = QWidget()
        self.model_options_layout = QVBoxLayout(self.model_options_panel)
        self.model_options_layout.setContentsMargins(0, 0, 0, 0)
        self.model_options_layout.setSpacing(8)
        self.model_options_panel.setVisible(False)
        self.model_options_button.toggled.connect(self.set_model_options_visible)
        main_layout.addWidget(self.model_options_button)
        main_layout.addWidget(self.model_options_panel)

        self.right_panel = QWidget()
        self.chat_main_layout = QVBoxLayout(self.right_panel)
        self.chat_main_layout.setContentsMargins(0, 0, 0, 0)
        self.chat_main_layout.setSpacing(AGENT_UI_METRICS["chat_panel_spacing"])

        self.main_splitter.addWidget(self.left_panel)
        self.main_splitter.addWidget(self.right_panel)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([
            AGENT_UI_METRICS["splitter_left_width"],
            AGENT_UI_METRICS["splitter_right_width"],
        ])

        # Keep connection details collapsible while leaving model controls visible.
        self.api_group = QGroupBox("服务和连接设置")
        self.api_group.setCheckable(False)
        self.api_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        api_group_layout = QVBoxLayout(self.api_group)
        api_group_layout.setContentsMargins(8, 8, 8, 8)
        api_group_layout.setSpacing(AGENT_UI_METRICS["api_group_spacing"])

        # ================= 供应商选择 =================
        provider_layout = QHBoxLayout()
        provider_layout.setContentsMargins(0, 0, 0, 0)
        provider_layout.setSpacing(6)
        provider_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        provider_label = QLabel("服务商:")
        # 固定左侧标签宽度，让同一侧输入控件的左、右边缘更整齐。
        provider_label.setFixedWidth(AGENT_UI_METRICS["form_label_width"])
        self.provider_combo = QComboBox()
        make_combo_popup_on_click(self.provider_combo)
        self.provider_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # Keep the service selector and configuration button aligned.
        self.provider_combo.setFixedHeight(AGENT_UI_METRICS["combo_control_height"])

        self.provider_card_button = QPushButton("配置")
        self.provider_card_button.setToolTip("管理并快速切换服务商、API 密钥和服务地址")
        self.provider_card_button.setFixedSize(
            AGENT_UI_METRICS["provider_card_button_width"],
            AGENT_UI_METRICS["combo_control_height"],
        )
        self.provider_card_button.clicked.connect(self.open_provider_cards_dialog)

        # 对象级样式覆盖全局 QPushButton / QComboBox 的 min-height 和 padding。
        # 否则全局样式中的 min-height + padding 会让按钮看起来比下拉框高，或基线不一致。
        self.provider_combo.setStyleSheet(f"""
            QComboBox {{
                min-height: 0px;
                max-height: {AGENT_UI_METRICS["combo_control_height"]}px;
                padding: 0px 30px 0px 8px;
            }}
        """)
        self.provider_card_button.setStyleSheet(f"""
            QPushButton {{
                min-height: 0px;
                max-height: {AGENT_UI_METRICS["combo_control_height"]}px;
                padding: 0px 12px;
            }}
        """)

        for provider_id, spec in PROVIDERS.items():
            self.provider_combo.addItem(spec.display_name, provider_id)

        stored_provider_index = self.provider_combo.findData(self.settings.chat_provider or "oneapi")
        if stored_provider_index >= 0:
            self.provider_combo.setCurrentIndex(stored_provider_index)

        self.provider_combo.currentIndexChanged.connect(self.on_provider_changed)

        provider_layout.addWidget(provider_label, 0, Qt.AlignmentFlag.AlignVCenter)
        # 输入控件使用 stretch=1，占满当前行剩余宽度，使右边缘与下方控件对齐。
        provider_layout.addWidget(self.provider_combo, 1, Qt.AlignmentFlag.AlignVCenter)
        provider_layout.addWidget(self.provider_card_button, 0, Qt.AlignmentFlag.AlignVCenter)

        api_group_layout.addLayout(provider_layout)

        initial_spec = get_provider_spec(self.get_current_provider())

        # ================= 连接凭据 =================
        key_layout = QHBoxLayout()

        key_label = QLabel("API 密钥：")
        # 与“供应商 / API 地址 / 模型”等标签统一宽度，保证控件边缘对齐。
        key_label.setFixedWidth(AGENT_UI_METRICS["form_label_width"])
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("请输入 API 密钥")
        self.key_input.setText(load_provider_key(initial_spec))

        key_layout.addWidget(key_label)
        key_layout.addWidget(self.key_input, 1)

        api_group_layout.addLayout(key_layout)

        # ================= API URL =================
        url_layout = QHBoxLayout()

        url_label = QLabel("服务地址：")
        url_label.setFixedWidth(AGENT_UI_METRICS["form_label_width"])
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("例如：https://api.example.com/v1")
        self.url_input.setText(load_provider_base_url(initial_spec))

        # Save a changed service address when editing ends.
        self.url_input.editingFinished.connect(self.save_current_api_settings)

        url_layout.addWidget(url_label)
        url_layout.addWidget(self.url_input, 1)

        api_group_layout.addLayout(url_layout)
        self.model_options_layout.addWidget(self.api_group)

        # ================= 模型选择 =================
        self.model_row_widget = QWidget()
        model_row_widget_layout = QVBoxLayout(self.model_row_widget)
        model_row_widget_layout.setContentsMargins(0, 0, 0, 0)
        model_row_widget_layout.setSpacing(0)
        model_layout = QHBoxLayout()
        model_layout.setContentsMargins(0, 0, 0, 0)

        model_label = QLabel("模型:")
        model_label.setFixedWidth(AGENT_UI_METRICS["form_label_width"])

        self.model_combo = QComboBox()
        make_combo_popup_on_click(self.model_combo)
        self.model_combo.setMinimumWidth(AGENT_UI_METRICS["model_combo_min_width"])
        self.model_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # 不内置任何模型；优先展示用户上次保存的选择，其次展示环境变量配置。
        initial_provider_settings = self.settings.chat_providers.get(initial_spec.provider_id)
        initial_model = (
            initial_provider_settings.model
            if initial_provider_settings and initial_provider_settings.model
            else load_provider_model(initial_spec)
        )
        if initial_model:
            self.model_combo.addItem(initial_model)
            self.model_combo.setCurrentText(initial_model)

        self.refresh_models_button = QPushButton("刷新模型列表")
        self.refresh_models_button.clicked.connect(self.fetch_models)
        self.refresh_models_button.setVisible(True)

        self.model_combo.currentTextChanged.connect(self.update_image_mode_visibility)
        self.model_combo.currentTextChanged.connect(self.restore_reasoning_preferences)
        self.model_combo.currentTextChanged.connect(self.update_reasoning_visibility)
        self.model_combo.currentTextChanged.connect(self.on_model_selection_changed)

        model_layout.addWidget(model_label)
        model_layout.addWidget(self.model_combo, 1)
        model_layout.addWidget(self.refresh_models_button)

        model_row_widget_layout.addLayout(model_layout)
        self.model_options_layout.addWidget(self.model_row_widget)

        self.mineru_key_button = QPushButton("设置 MinerU 访问令牌")
        self.mineru_key_button.setObjectName("mineruKeyButton")
        self.mineru_key_button.clicked.connect(self.configure_mineru_api_key)

        # Keep the parser and current-message file controls the same height.
        common_button_height = AGENT_UI_METRICS["combo_control_height"]

        self.mineru_key_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Use one baseline and equal stretch for the two controls.
        self.mineru_tools_layout = QHBoxLayout()
        self.mineru_tools_layout.setContentsMargins(0, 0, 0, 0)
        self.mineru_tools_layout.setSpacing(8)
        self.mineru_tools_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self.mineru_tools_layout.addWidget(self.mineru_key_button, 1, Qt.AlignmentFlag.AlignVCenter)
        self.model_options_layout.addLayout(self.mineru_tools_layout)

        # ================= 回复显示设置 =================
        self.render_markdown_checkbox = QCheckBox("使用 Markdown 显示回复")
        self.render_markdown_checkbox.setChecked(True)
        self.render_markdown_checkbox.setToolTip(
            "关闭后以原始 Markdown 文本显示回复。双击消息气泡可复制内容。"
        )
        self.render_markdown_checkbox.toggled.connect(self.on_markdown_render_toggled)
        self.model_options_layout.addWidget(self.render_markdown_checkbox)

        # ================= 思考模式设置 =================
        self.reasoning_group = QGroupBox("模型思考设置")
        reasoning_group_layout = QVBoxLayout(self.reasoning_group)

        # Keep the two reasoning controls on one row.
        reasoning_options_layout = QHBoxLayout()
        reasoning_options_layout.setContentsMargins(0, 0, 0, 0)
        reasoning_options_layout.setSpacing(10)

        mode_field_layout = QHBoxLayout()
        mode_field_layout.setContentsMargins(0, 0, 0, 0)
        mode_field_layout.setSpacing(6)

        mode_label = QLabel("模式:")
        mode_label.setFixedWidth(AGENT_UI_METRICS["reasoning_label_width"])

        self.thinking_mode_combo = QComboBox()
        make_combo_popup_on_click(self.thinking_mode_combo)
        self.thinking_mode_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.thinking_mode_combo.addItem("默认", "default")
        self.thinking_mode_combo.addItem("开启", "enabled")
        self.thinking_mode_combo.addItem("关闭", "disabled")

        mode_field_layout.addWidget(mode_label)
        mode_field_layout.addWidget(self.thinking_mode_combo, 1)

        effort_field_layout = QHBoxLayout()
        effort_field_layout.setContentsMargins(0, 0, 0, 0)
        effort_field_layout.setSpacing(6)

        effort_label = QLabel("程度:")
        effort_label.setFixedWidth(AGENT_UI_METRICS["reasoning_label_width"])

        self.reasoning_effort_combo = QComboBox()
        make_combo_popup_on_click(self.reasoning_effort_combo, editable=True)
        self.reasoning_effort_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # 这里不写死模型，只提供常见协议值作为便捷选项。
        # 用户仍可手动输入未来新增的强度值。
        self.reasoning_effort_combo.addItems([
            "默认",
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ])
        self.reasoning_effort_combo.setCurrentText("默认")

        effort_field_layout.addWidget(effort_label)
        effort_field_layout.addWidget(self.reasoning_effort_combo, 1)

        reasoning_options_layout.addLayout(mode_field_layout, 1)
        reasoning_options_layout.addLayout(effort_field_layout, 1)
        reasoning_group_layout.addLayout(reasoning_options_layout)

        self.show_reasoning_checkbox = QCheckBox("显示模型思考过程")
        self.show_reasoning_checkbox.setChecked(True)
        # 避免该复选框在部分样式表下出现灰色整块背景。
        self.show_reasoning_checkbox.setStyleSheet("""
            QCheckBox {
                background-color: transparent;
                color: #212121;
                padding: 0px;
                border: none;
            }
        """)
        reasoning_group_layout.addWidget(self.show_reasoning_checkbox)

        # 思考设置以“服务商 + 模型”保存；每次改动立即落盘，切换模型时即可恢复。
        self.thinking_mode_combo.currentIndexChanged.connect(self.save_reasoning_preferences)
        self.reasoning_effort_combo.currentTextChanged.connect(self.save_reasoning_preferences)
        self.show_reasoning_checkbox.toggled.connect(self.save_reasoning_preferences)
        self._restoring_reasoning_preferences = False
        self.restore_reasoning_preferences()

        self.reasoning_group.setVisible(initial_spec.supports_reasoning)
        self.model_options_layout.addWidget(self.reasoning_group)

        # ================= 图片模型设置 =================
        # 图片参数通常不需要每次都调整；默认收起以给输入区留出更多空间。
        # 保持 image_group 作为外层容器，供模型切换逻辑统一控制可见性。
        self.image_group = QWidget()
        image_group_layout = QVBoxLayout(self.image_group)
        image_group_layout.setContentsMargins(0, 0, 0, 0)
        image_group_layout.setSpacing(4)

        self.image_settings_toggle_button = QToolButton()
        self.image_settings_toggle_button.setText("图片生成设置")
        self.image_settings_toggle_button.setCheckable(True)
        self.image_settings_toggle_button.setChecked(False)
        self.image_settings_toggle_button.setArrowType(Qt.ArrowType.RightArrow)
        self.image_settings_toggle_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.image_settings_toggle_button.toggled.connect(self.set_image_settings_visible)
        image_group_layout.addWidget(self.image_settings_toggle_button)

        self.image_settings_panel = QGroupBox()
        self.image_settings_panel.setTitle("")
        self.image_settings_panel.setVisible(False)
        image_settings_panel_layout = QHBoxLayout(self.image_settings_panel)
        image_settings_panel_layout.setContentsMargins(8, 8, 8, 8)
        image_settings_panel_layout.setSpacing(12)

        image_settings_column = QVBoxLayout()
        image_settings_column.setSpacing(8)

        image_size_layout = QHBoxLayout()
        image_size_label = QLabel("尺寸:")
        self.image_size_combo = QComboBox()
        make_combo_popup_on_click(self.image_size_combo, editable=True)
        self.image_size_combo.addItems([
            "auto",
            "1024x1024",
            "1536x1024",
            "1024x1536",
            "2048x2048",
            "2048x1152",
            "1152x2048",
            "3840x2160",
            "2160x3840",
        ])
        self.image_size_combo.setCurrentText("auto")
        image_size_layout.addWidget(image_size_label)
        image_size_layout.addWidget(self.image_size_combo)
        image_settings_column.addLayout(image_size_layout)

        image_quality_layout = QHBoxLayout()
        image_quality_label = QLabel("画质:")
        self.image_quality_combo = QComboBox()
        make_combo_popup_on_click(self.image_quality_combo)
        self.image_quality_combo.addItems([
            "auto",
            "low",
            "medium",
            "high",
        ])
        self.image_quality_combo.setCurrentText("auto")
        image_quality_layout.addWidget(image_quality_label)
        image_quality_layout.addWidget(self.image_quality_combo)
        image_settings_column.addLayout(image_quality_layout)

        image_format_layout = QHBoxLayout()
        image_format_label = QLabel("输出格式:")
        self.image_format_combo = QComboBox()
        make_combo_popup_on_click(self.image_format_combo)
        self.image_format_combo.addItems([
            "png",
            "jpeg",
            "webp",
        ])
        self.image_format_combo.setCurrentText("png")
        image_format_layout.addWidget(image_format_label)
        image_format_layout.addWidget(self.image_format_combo)
        image_settings_column.addLayout(image_format_layout)

        image_settings_panel_layout.addLayout(image_settings_column, 1)

        reference_panel = QVBoxLayout()
        reference_panel.setSpacing(8)
        reference_title = QLabel("参考图")
        reference_title.setStyleSheet("font-weight: 600; color: #212121;")
        reference_panel.addWidget(reference_title)

        self.select_reference_image_button = QPushButton("选择参考图")
        self.clear_reference_image_button = QPushButton("清除参考图")

        self.reference_image_preview = QLabel("未选择参考图")
        self.reference_image_preview.setFixedSize(
            AGENT_UI_METRICS["reference_preview_width"],
            AGENT_UI_METRICS["reference_preview_height"],
        )
        self.reference_image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reference_image_preview.setStyleSheet("""
            QLabel {
                background-color: #ffffff;
                border: 1px solid #cccccc;
                border-radius: 8px;
                color: #666666;
            }
        """)

        self.reference_image_label = QLabel("未选择参考图")
        self.reference_image_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.select_reference_image_button.clicked.connect(self.select_reference_image)
        self.clear_reference_image_button.clicked.connect(self.clear_reference_image)
        self.clear_reference_image_button.setEnabled(False)

        reference_buttons = QHBoxLayout()
        reference_buttons.addWidget(self.select_reference_image_button)
        reference_buttons.addWidget(self.clear_reference_image_button)
        reference_panel.addLayout(reference_buttons)
        reference_panel.addWidget(self.reference_image_preview)
        reference_panel.addWidget(self.reference_image_label)
        reference_hint = QLabel(
            "如需使用参考图，请直接粘贴到输入框后发送。"
        )
        reference_hint.setWordWrap(True)
        reference_hint.setStyleSheet("color: #666666; font-size: 12px;")
        reference_panel.addWidget(reference_hint)
        reference_panel.addStretch(1)

        reference_container = QWidget()
        reference_container.setLayout(reference_panel)
        reference_container.setVisible(False)
        image_settings_panel_layout.addWidget(reference_container, 1)
        image_group_layout.addWidget(self.image_settings_panel)

        self.image_group.setVisible(False)

        # ================= 文档对话设置 =================
        # Keep document options separate from the main input area.
        self.document_advanced_group = QWidget()
        self.document_advanced_group.setObjectName("documentAdvancedGroup")
        self.document_advanced_group_layout = QVBoxLayout(self.document_advanced_group)
        self.document_advanced_group_layout.setContentsMargins(0, 0, 0, 0)
        self.document_advanced_group_layout.setSpacing(6)

        # 顶部按钮：点击后展开/收起下方高级选项。
        # Use a text button so the control keeps the same height as its neighbor.
        # The text arrow keeps the button compact and aligned with the parser control.
        self.document_advanced_toggle_button = QPushButton("▸ 本次发送的文件选项")
        self.document_advanced_toggle_button.setObjectName("documentAdvancedToggleButton")
        self.document_advanced_toggle_button.setCheckable(True)
        self.document_advanced_toggle_button.setChecked(False)
        self.document_advanced_toggle_button.toggled.connect(self.set_document_advanced_visible)

        # Match the parser control height and preserve the compact button layout.
        self.document_advanced_toggle_button.setFixedHeight(common_button_height)
        self.document_advanced_toggle_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.mineru_key_button.setFixedHeight(common_button_height)
        self.mineru_key_button.setStyleSheet(f"""
            QPushButton#mineruKeyButton {{
                min-height: 0px;
                max-height: {common_button_height}px;
                background-color: #FFFFFF;
                color: #212121;
                border: 2px solid #D4AF37;
                border-radius: 4px;
                /* Keep enough vertical padding for readable text. */
                padding: 4px 14px;
                font-weight: bold;
            }}
            QPushButton#mineruKeyButton:hover {{
                background-color: #FFF8E1;
            }}
            QPushButton#mineruKeyButton:pressed {{
                background-color: #FBC02D;
            }}
        """)
        self.document_advanced_toggle_button.setStyleSheet(f"""
            QPushButton#documentAdvancedToggleButton {{
                min-height: 0px;
                max-height: {common_button_height}px;
                background-color: #FFFFFF;
                color: #212121;
                border: 2px solid #D4AF37;
                border-radius: 4px;
                /* Keep enough vertical padding for readable text. */
                padding: 4px 14px;
                font-weight: bold;
            }}
            QPushButton#documentAdvancedToggleButton:hover {{
                background-color: #FFF8E1;
            }}
            QPushButton#documentAdvancedToggleButton:pressed {{
                background-color: #FBC02D;
            }}
        """)

        # Place file options beside the parser control.
        if hasattr(self, "mineru_tools_layout"):
            # Use the shared control height.
            self.mineru_tools_layout.addWidget(
                self.document_advanced_toggle_button,
                1,
                Qt.AlignmentFlag.AlignVCenter,
            )
            self.mineru_key_button.setFixedHeight(common_button_height)
            self.document_advanced_toggle_button.setFixedHeight(common_button_height)

            # Use the default full-document, compressed, ordered-image settings.
            self.document_advanced_toggle_button.setVisible(False)
        else:
            self.document_advanced_group_layout.addWidget(self.document_advanced_toggle_button)

        # Keep the advanced options panel hidden; it is shown in a popup.
        self.document_advanced_panel = QFrame()
        self.document_advanced_panel.setFrameShape(QFrame.Shape.NoFrame)
        self.document_advanced_panel_layout = QHBoxLayout(self.document_advanced_panel)
        self.document_advanced_panel_layout.setContentsMargins(8, 0, 0, 0)
        self.document_advanced_panel_layout.setSpacing(8)

        self.document_advanced_panel_layout.addWidget(QLabel("发送方式:"))
        self.document_send_mode_combo = QComboBox()
        make_combo_popup_on_click(self.document_send_mode_combo)
        self.document_send_mode_combo.addItem("全文带图", "full_with_images")
        self.document_send_mode_combo.addItem("全文无图", "full_no_images")
        self.document_send_mode_combo.setCurrentIndex(0)
        self.document_advanced_panel_layout.addWidget(self.document_send_mode_combo)

        self.document_compress_images_checkbox = QCheckBox("压缩图片")
        self.document_compress_images_checkbox.setChecked(True)
        self.document_advanced_panel_layout.addWidget(self.document_compress_images_checkbox)

        self.document_sequential_images_checkbox = QCheckBox("顺序读图")
        self.document_sequential_images_checkbox.setChecked(True)
        self.document_advanced_panel_layout.addWidget(self.document_sequential_images_checkbox)

        # Keep file-option values on the window so closing the popup does not change them.
        self._document_send_mode_value = "full_with_images"
        self._document_compress_images_value = True
        self._document_sequential_images_value = True

        # Keep the advanced-options area compact.
        self.document_advanced_panel_layout.addStretch(1)

        self.document_advanced_group_layout.addWidget(self.document_advanced_panel)

        # The panel is opened by set_document_advanced_visible().
        self.document_advanced_group.setVisible(False)

        self.document_send_mode_combo.currentIndexChanged.connect(self.on_document_send_mode_changed)
        self.refresh_document_tool_controls()
        self.set_document_advanced_visible(False)
        self.on_document_send_mode_changed()

        # ================= 对话记录 =================
        self.history_group = QGroupBox("对话历史")
        history_layout = QVBoxLayout(self.history_group)
        history_layout.setContentsMargins(8, 8, 8, 8)
        history_layout.setSpacing(6)

        self.current_conversation_label = QLabel("当前对话：未命名")
        self.current_conversation_label.setWordWrap(True)
        self.current_conversation_label.setStyleSheet("color: #555555;")

        history_button_layout = QHBoxLayout()
        self.new_conversation_button = QPushButton("新对话")
        self.delete_conversation_button = QPushButton("清空历史")
        self.new_conversation_button.clicked.connect(self.create_new_conversation)
        # The button clears every stored conversation, not only the selected item.
        self.delete_conversation_button.clicked.connect(self.clear_all_conversation_records)
        history_button_layout.addWidget(self.new_conversation_button)
        history_button_layout.addWidget(self.delete_conversation_button)

        self.conversation_history_list = QListWidget()
        self.conversation_history_list.setToolTip("单击任意记录可继续该对话。")
        self.conversation_history_list.itemClicked.connect(self.load_conversation_from_item)
        self.conversation_history_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.conversation_history_list.customContextMenuRequested.connect(self.show_conversation_history_context_menu)

        history_layout.addWidget(self.current_conversation_label)
        history_layout.addLayout(history_button_layout)
        history_layout.addWidget(self.conversation_history_list, 1)

        main_layout.addWidget(self.history_group, 2)

        # ================= 聊天显示 =================
        # 使用滚动区域 + 气泡控件实现聊天窗口：
        # 1. 用户消息靠右
        # 2. 模型消息靠左
        # 3. 图片用 QLabel/QPixmap 内存显示，不依赖本地临时文件
        self.chat_scroll_area = QScrollArea()
        self.chat_scroll_area.setWidgetResizable(True)
        self.chat_scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.chat_scroll_area.setStyleSheet(f"""
            QScrollArea {{
                background-color: {COLOR_BG_SURFACE_2};
                border: 1px solid {COLOR_BORDER_HAIR};
                border-radius: 0px;
            }}
            QScrollArea > QWidget > QWidget {{
                background-color: {COLOR_BG_SURFACE_2};
            }}
            QScrollBar:vertical {{
                background-color: {COLOR_BG_INSET};
                width: 8px;
                margin: 0px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background-color: #8A8A84;
                min-height: 32px;
                border-radius: 0px;
            }}
            QScrollBar::handle:vertical:hover {{
                background-color: {COLOR_ACCENT};
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0px;
                background: transparent;
                border: none;
            }}
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
        """)

        self.chat_container = QWidget()
        self.chat_container.setObjectName("chatContainer")

        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(14, 14, 14, 14)
        self.chat_layout.setSpacing(8)
        # 横向强制填满滚动区视口；否则全屏时行容器可能按内容宽度布局，系统消息视觉上会偏移。
        self.chat_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.chat_layout.addStretch()

        self.chat_scroll_area.setWidget(self.chat_container)
        self.chat_main_layout.addWidget(self.chat_scroll_area, 1)
        self.create_message_navigator()

        # ================= 输入框 =================
        self.input_box = MultimodalInputEdit()
        self.input_box.setPlaceholderText("输入问题；可直接粘贴图片，Ctrl + Enter 发送")
        # 用户输入框高度扩展到原来的 1.5 倍：100px -> 150px。
        self.input_box.setFixedHeight(AGENT_UI_METRICS["input_box_height"])
        self.input_box.setStyleSheet(f"""
            QPlainTextEdit {{
                font-size: 14px;
                padding: 10px 12px;
                background-color: {COLOR_BG_SURFACE_2};
                color: {COLOR_TEXT_PRIMARY};
                border: 1px solid {COLOR_BORDER_HAIR};
                border-radius: 2px;
                font-family: {APP_UI_FONT_FAMILY_STACK};
            }}
            QPlainTextEdit:focus {{
                border-color: {COLOR_ACCENT};
            }}
        """)
        self.input_box.image_pasted.connect(self.add_pasted_input_image)
        self.input_box.installEventFilter(self)

        self.input_reference_quote_row = QWidget()
        self.input_reference_quote_layout = QHBoxLayout(self.input_reference_quote_row)
        self.input_reference_quote_layout.setContentsMargins(4, 0, 4, 0)
        self.input_reference_quote_layout.setSpacing(6)
        self.input_reference_quote_layout.addStretch(1)
        self.input_reference_quote_row.setVisible(False)
        self.chat_main_layout.addWidget(self.input_reference_quote_row)

        self.chat_main_layout.addWidget(self.input_box)

        # Document-wide analysis tasks (要点提炼/思维导图/思路流程) have been moved
        # to the right-click context menu on the reading pane to save space.
        # The row and buttons are kept as hidden stubs so call sites that reference
        # self.document_task_buttons or self.document_task_row still work.
        self.document_task_row = QWidget()
        self.document_task_row.setVisible(False)
        document_task_layout = QHBoxLayout(self.document_task_row)
        document_task_layout.setContentsMargins(0, 0, 0, 0)
        document_task_layout.setSpacing(6)
        self.document_task_buttons = {}
        for task_type in ("key_points", "paper_mindmap", "paper_logic_flow"):
            button = QPushButton(DOCUMENT_AI_TASKS[task_type]["label"])
            button.clicked.connect(lambda _checked=False, name=task_type: self.submit_document_ai_task(name))
            self.document_task_buttons[task_type] = button
            document_task_layout.addWidget(button, 1)
        # Row is intentionally not added to chat_main_layout — buttons live in right-click menu.

        # 粘贴图片后的缩略图预览区。
        # Images are sent with the next user message in OpenAI-compatible
        # multimodal format.
        self.input_image_preview_area = QWidget()
        self.input_image_preview_layout = QHBoxLayout(self.input_image_preview_area)
        self.input_image_preview_layout.setContentsMargins(4, 4, 4, 4)
        self.input_image_preview_layout.setSpacing(8)
        self.input_image_preview_area.setVisible(False)
        self.input_image_preview_area.setStyleSheet("""
            QWidget {
                background-color: #FFFFFF;
                border: 1px solid #D7D7D2;
                border-radius: 10px;
            }
        """)

        self.chat_main_layout.addWidget(self.input_image_preview_area)

        # 图片模型设置放在输入区下方，避免左侧模型栏被参考图和图片参数挤得过长。
        self.chat_main_layout.addWidget(self.image_group)

        # File options are opened beside the parser control so the input area stays clear.

        # ================= 按钮区 =================
        button_area = QWidget()
        button_layout = QVBoxLayout(button_area)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(6)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(0)
        file_button_row = QHBoxLayout()
        file_button_row.setContentsMargins(0, 0, 0, 0)
        file_button_row.setSpacing(6)
        action_button_row = QHBoxLayout()
        action_button_row.setContentsMargins(0, 0, 0, 0)
        action_button_row.setSpacing(6)

        # Keep the hidden compatibility actions out of the document-chat toolbar.
        self.prompt_insert_button = QPushButton("指引")
        self.prompt_insert_button.setToolTip("")
        self.prompt_insert_button.clicked.connect(self.insert_builtin_prompt_to_input)
        self.prompt_insert_button.setVisible(False)

        self.current_code_button = QPushButton("当前内容")
        self.current_code_button.setToolTip("")
        self.current_code_button.setEnabled(False)
        self.current_code_button.clicked.connect(self.insert_current_code_to_input)
        self.current_code_button.setVisible(False)

        self.add_document_button = QPushButton("添加文件")
        self.add_document_button.setToolTip("添加到下一条消息，发送后自动移除。")
        self.add_document_button.clicked.connect(self.add_document_file)

        self.clear_documents_button = QPushButton("移除待发送文件")
        self.clear_documents_button.setEnabled(False)
        self.clear_documents_button.clicked.connect(self.clear_documents)

        self.document_status_label = QLabel("未添加待发送文件")
        self.document_status_label.setWordWrap(True)
        self.document_status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.document_status_label.setStyleSheet("color: #555555;")

        self.clear_button = QPushButton("清空对话")
        self.clear_button.setText("清空对话")
        self.clear_button.setToolTip("清空当前对话、待发送文件和待发送图片。")
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("sendButton")

        self.clear_button.clicked.connect(self.clear_chat)
        self.send_button.clicked.connect(self.on_send_button_clicked)

        # Keep toolbar controls equal in height and let them share the available width.
        for action_button in (
            self.prompt_insert_button,
            self.current_code_button,
            self.add_document_button,
            self.clear_documents_button,
            self.clear_button,
            self.send_button,
        ):
            action_button.setFixedHeight(AGENT_UI_METRICS["action_button_height"])
            action_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Legacy editing controls stay hidden from document chat.
        status_row.addWidget(self.document_status_label, 1)
        action_button_row.addWidget(self.add_document_button, 1, Qt.AlignmentFlag.AlignVCenter)
        action_button_row.addWidget(self.clear_documents_button, 1, Qt.AlignmentFlag.AlignVCenter)
        action_button_row.addWidget(self.clear_button, 1, Qt.AlignmentFlag.AlignVCenter)
        action_button_row.addWidget(self.send_button, 1, Qt.AlignmentFlag.AlignVCenter)
        button_layout.addLayout(status_row)
        button_layout.addLayout(action_button_row)

        self.chat_main_layout.addWidget(button_area)
        self.refresh_document_status()

        # 所有控件和对象级样式都创建完成后，最后统一收口一次左侧控件高度。
        # 这样可以覆盖全局样式表 min-height / padding 对视觉高度的干扰。
        self.apply_left_control_height_policy()

        self.load_conversation_sessions()
        self.refresh_conversation_history_list()
        self.update_current_conversation_label()

    def apply_left_control_height_policy(self):
        """Apply one height policy to the left-side controls."""
        height = AGENT_UI_METRICS["combo_control_height"]

        controls = [
            getattr(self, "provider_combo", None),
            getattr(self, "provider_card_button", None),
            getattr(self, "model_combo", None),
            getattr(self, "refresh_models_button", None),
            getattr(self, "mineru_key_button", None),
            getattr(self, "document_advanced_toggle_button", None),
        ]

        for control in controls:
            if control is None:
                continue

            try:
                control.setMinimumHeight(height)
                control.setMaximumHeight(height)
                control.setFixedHeight(height)
                control.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            except RuntimeError:
                pass

        try:
            self.provider_card_button.setFixedWidth(AGENT_UI_METRICS["provider_card_button_width"])
        except Exception:
            pass

        # 对象级样式再次压平纵向 padding，避免按钮和下拉框视觉高度不一致。
        try:
            combo_style = f"""
                QComboBox {{
                    min-height: 0px;
                    max-height: {height}px;
                    padding: 3px 28px 3px 8px;
                    background: {COLOR_BG_SURFACE_2};
                    color: {COLOR_TEXT_PRIMARY};
                    border: 1px solid {COLOR_BORDER_HAIR};
                    border-radius: 2px;
                }}
                QComboBox:focus {{
                    border-color: {COLOR_ACCENT};
                }}
                QComboBox::drop-down {{
                    subcontrol-origin: padding;
                    subcontrol-position: top right;
                    width: 22px;
                    border: none;
                    border-left: 1px solid {COLOR_BORDER_HAIR};
                    background: {COLOR_BG_SURFACE};
                    border-top-right-radius: 2px;
                    border-bottom-right-radius: 2px;
                }}
            """
            self.provider_combo.setStyleSheet(combo_style)
            self.model_combo.setStyleSheet(combo_style)

            compact_button_style = f"""
                QPushButton {{
                    min-height: 0px;
                    max-height: {height}px;
                    padding: 3px 10px;
                    background: {COLOR_BG_SURFACE_2};
                    color: {COLOR_TEXT_PRIMARY};
                    border: 1px solid {COLOR_BORDER_HAIR};
                    border-radius: 2px;
                    font-weight: 650;
                }}
                QPushButton:hover {{
                    background: {COLOR_ACCENT};
                    color: #FFFFFF;
                    border-color: {COLOR_ACCENT};
                }}
            """
            self.provider_card_button.setStyleSheet(compact_button_style)
            self.refresh_models_button.setStyleSheet(compact_button_style)
            self.mineru_key_button.setStyleSheet(compact_button_style)
            self.document_advanced_toggle_button.setStyleSheet(compact_button_style)
        except Exception:
            pass

    def get_current_provider(self) -> str:
        return self.provider_combo.currentData() or "oneapi"

    @staticmethod
    def ensure_chat_settings_fields(settings) -> None:
        """兼容 OT_common 等旧设置对象，避免运行时缺少新增的 chat_* 字段。"""
        if settings is None:
            return
        if not hasattr(settings, "chat_provider"):
            settings.chat_provider = str(getattr(settings, "ai_provider", "") or "oneapi")
        if not hasattr(settings, "chat_providers"):
            settings.chat_providers = {
                provider_id: app_config.ProviderSettings(
                    item.provider_id,
                    item.base_url,
                    item.model,
                    getattr(item, "request_body_mode", "codex"),
                )
                for provider_id, item in (getattr(settings, "providers", {}) or {}).items()
            }
        if not hasattr(settings, "chat_reasoning_preferences"):
            settings.chat_reasoning_preferences = {}

    @staticmethod
    def reasoning_preference_key(provider_id: str, model: str) -> str:
        """Stable key for reasoning preferences scoped to one provider model."""
        return " | ".join((str(provider_id or "").strip().lower(), str(model or "").strip().lower()))

    def save_reasoning_preferences(self, *_args) -> None:
        """Persist the visible reasoning controls for the selected provider/model."""
        if getattr(self, "_restoring_reasoning_preferences", False):
            return
        model = self.model_combo.currentText().strip()
        if not model:
            return
        ChatWindow.ensure_chat_settings_fields(self.settings)
        provider_id = self.get_current_provider()
        key = ChatWindow.reasoning_preference_key(provider_id, model)
        self.settings.chat_reasoning_preferences[key] = {
            "thinking_mode": self.thinking_mode_combo.currentData() or "default",
            "reasoning_effort": self.reasoning_effort_combo.currentText().strip() or "默认",
            "show_reasoning": self.show_reasoning_checkbox.isChecked(),
        }
        app_config.save_settings(self.settings)

    def restore_reasoning_preferences(self, *_args) -> None:
        """Restore reasoning controls after changing the provider or model."""
        model = self.model_combo.currentText().strip()
        if not model or not hasattr(self, "thinking_mode_combo"):
            return
        ChatWindow.ensure_chat_settings_fields(self.settings)
        key = ChatWindow.reasoning_preference_key(self.get_current_provider(), model)
        preference = self.settings.chat_reasoning_preferences.get(key, {})
        if not isinstance(preference, dict):
            return

        self._restoring_reasoning_preferences = True
        try:
            thinking_mode = str(preference.get("thinking_mode") or "default")
            mode_index = self.thinking_mode_combo.findData(thinking_mode)
            self.thinking_mode_combo.setCurrentIndex(mode_index if mode_index >= 0 else 0)
            self.reasoning_effort_combo.setCurrentText(str(preference.get("reasoning_effort") or "默认"))
            self.show_reasoning_checkbox.setChecked(bool(preference.get("show_reasoning", True)))
        finally:
            self._restoring_reasoning_preferences = False

    def request_body_mode_for_provider(self, provider_id: str) -> str:
        provider = self.settings.chat_providers.get(provider_id)
        return normalize_oneapi_request_body_mode(getattr(provider, "request_body_mode", "codex"))

    def set_request_body_mode_for_current_provider(self, mode: str) -> None:
        provider_id = self.get_current_provider()
        if provider_id not in {"oneapi", "openai_compatible"}:
            return
        old = self.settings.chat_providers.get(provider_id)
        self.settings.chat_providers[provider_id] = app_config.ProviderSettings(
            provider_id=provider_id,
            base_url=self.url_input.text().strip() or getattr(old, "base_url", ""),
            model=self.model_combo.currentText().strip() or getattr(old, "model", ""),
            request_body_mode=normalize_oneapi_request_body_mode(mode),
        )
        app_config.save_settings(self.settings)

    def sync_from_app_settings(self, settings=None, provider_keys: dict[str, str] | None = None):
        """接收宿主可共享的数据，但绝不替换对话自己的设置对象。

        翻译和对话的模型选择是独立的；仅将宿主已保存的同服务商 API 密钥
        复制到对话密钥仓库，方便首次使用时复用，不会反向覆盖翻译配置。
        """
        shared_settings = settings
        provider_keys = provider_keys or {}
        self.shared_app_settings = shared_settings
        self.ensure_chat_settings_fields(self.settings)

        # 全新对话配置以当前翻译服务商的“连接方式”作为一次性起点：
        # The address and credential may match, but translation credentials are never copied into chat.
        # 意外成为对话默认模型。此后对话配置独立保存，不会继续跟随翻译修改。
        if not self.settings.chat_providers and shared_settings is not None:
            host_provider_id = str(getattr(shared_settings, "ai_provider", "") or "").strip()
            host_providers = getattr(shared_settings, "providers", {}) or {}
            host_provider = host_providers.get(host_provider_id)
            host_url = str(getattr(host_provider, "base_url", "") or "").strip()
            if host_provider_id and host_url and not is_machine_translation_provider_id(host_provider_id):
                self.settings.chat_provider = host_provider_id
                self.settings.chat_providers[host_provider_id] = app_config.ProviderSettings(
                    provider_id=host_provider_id,
                    base_url=host_url,
                    model="",
                    request_body_mode=getattr(host_provider, "request_body_mode", "codex"),
                )

        for provider_id, value in provider_keys.items():
            if not provider_id or is_machine_translation_provider_id(provider_id):
                continue
            shared_key = str(value or "").strip()
            if shared_key:
                save_chat_secret_with_session_fallback(self, provider_id, "api_key", shared_key)

        selected_provider = self.settings.chat_provider or ""
        if is_machine_translation_provider_id(selected_provider) or not selected_provider:
            selected_provider = "oneapi"
            self.settings.chat_provider = selected_provider
        provider_id = selected_provider

        provider_index = self.provider_combo.findData(provider_id)
        old_block = self.provider_combo.blockSignals(True)
        try:
            if provider_index >= 0:
                self.provider_combo.setCurrentIndex(provider_index)
        finally:
            self.provider_combo.blockSignals(old_block)

        spec = get_provider_spec(provider_id)
        stored_provider = self.settings.chat_providers.get(provider_id)
        self.key_input.setText(app_config.load_secret(provider_id, "api_key") or load_provider_key(spec))
        self.url_input.setText(
            stored_provider.base_url.strip()
            if stored_provider and stored_provider.base_url.strip()
            else load_provider_base_url(spec)
        )

        model = stored_provider.model.strip() if stored_provider and stored_provider.model else ""
        self._syncing_shared_settings = True
        self.model_combo.blockSignals(True)
        try:
            self.model_combo.clear()
            if model:
                self.model_combo.addItem(model)
                self.model_combo.setCurrentText(model)
        finally:
            self.model_combo.blockSignals(False)
            self._syncing_shared_settings = False
        self.update_reasoning_visibility()
        self.update_image_mode_visibility()
        app_config.save_settings(self.settings)

    def open_provider_cards_dialog(self):
        """Open the reusable provider card manager."""
        self.save_current_api_settings()
        dialog = ProviderCardsDialog(self)
        dialog.exec()

    def current_ai_key_available(self) -> bool:
        provider_id = self.get_current_provider()
        spec = get_provider_spec(provider_id)
        return bool(
            self.key_input.text().strip()
            or app_config.load_secret(provider_id, "api_key")
            or load_key_setting(spec.env_key_name)
            or load_labelled_secret(provider_id)
        )

    def current_document_tool_adapter(self) -> DocumentToolAdapter | None:
        return get_document_tool_adapter()

    def document_tool_name(self) -> str:
        adapter = self.current_document_tool_adapter()
        return adapter.display_name if adapter else "文档工具"

    def document_tool_key_available(self) -> bool:
        adapter = self.current_document_tool_adapter()
        if not adapter or not adapter.is_configured:
            return True
        try:
            return bool(adapter.is_configured())
        except Exception:
            return False

    def refresh_document_tool_controls(self):
        adapter = self.current_document_tool_adapter()
        if not adapter:
            self.mineru_key_button.setVisible(False)
            return

        # The main workspace manages the MinerU access token; chat does not duplicate this control.
        self.mineru_key_button.setVisible(False)
        button_text = adapter.settings_button_text or f"设置 {adapter.display_name} 访问令牌"
        self.mineru_key_button.setText(button_text)

    def mineru_key_available(self) -> bool:
        return self.document_tool_key_available()

    def prompt_for_missing_startup_keys(self, *, include_document_tool: bool = True):
        if self._startup_key_prompt_shown:
            return
        self._startup_key_prompt_shown = True

        missing_ai = not self.current_ai_key_available()
        adapter = self.current_document_tool_adapter()
        missing_document_tool = bool(
            include_document_tool and adapter and adapter.save_key and not self.document_tool_key_available()
        )
        if not missing_ai and not missing_document_tool:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("初始设置")
        dialog.resize(520, 190)
        layout = QVBoxLayout(dialog)
        hint = QLabel(
            "尚未配置对话服务。DeepSeek 只是当前默认选项，并非必填；"
            "你可以在服务设置中选择并配置要使用的服务，也可以稍后再配置。"
            if missing_ai
            else "尚未配置文档解析服务；需要添加需解析的文件时再配置即可。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        if missing_document_tool:
            document_tool_name = adapter.display_name if adapter else self.document_tool_name()
            if missing_ai:
                document_hint = QLabel(f"{document_tool_name} 用于解析文档；需要添加需解析的文件时再配置即可。")
                document_hint.setWordWrap(True)
                layout.addWidget(document_hint)

        buttons = QDialogButtonBox()
        open_settings_button = buttons.addButton("打开服务设置", QDialogButtonBox.ButtonRole.AcceptRole)
        later_button = buttons.addButton("稍后配置", QDialogButtonBox.ButtonRole.RejectRole)
        later_button.setDefault(True)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def open_settings():
            dialog.accept()
            # 等初始对话框关闭后再打开，避免两个模态窗口叠在一起。
            QTimer.singleShot(0, self.open_provider_cards_dialog)

        open_settings_button.clicked.connect(open_settings)
        dialog.exec()

    def set_model_options_visible(self, visible: bool):
        self.model_options_panel.setVisible(visible)
        self.model_options_button.setArrowType(
            Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow
        )
        if visible:
            QTimer.singleShot(50, self.fetch_models)

    def set_image_settings_visible(self, visible: bool):
        """展开或收起图片模型的可选生成参数。"""
        self.image_settings_panel.setVisible(visible)
        self.image_settings_toggle_button.setArrowType(
            Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow
        )

    def set_layout_children_visible(self, layout, visible: bool):
        """递归显示或隐藏布局中的控件，用于实现 QGroupBox 内容折叠。"""
        if layout is None:
            return

        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget()
            child_layout = item.layout()

            if widget is not None:
                widget.setVisible(visible)

            if child_layout is not None:
                self.set_layout_children_visible(child_layout, visible)

    def set_group_children_visible(self, group: QGroupBox, visible: bool):
        """
        只折叠分组内容，不隐藏分组标题本身。

        这样左上角仍保留“服务和连接设置”的标题，用户需要时可随时展开。
        """
        self.set_layout_children_visible(group.layout(), visible)

    def conversation_history_path(self) -> Path:
        """
        对话记录保存路径。

        这里改为跟随当前工作文件夹，便于用户重新选择工作目录后，
        对话记录也能自动迁移到新的工作区中。
        """
        return app_config.chat_history_path(self.settings)

    def current_timestamp_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def make_conversation_id(self) -> str:
        return f"chat-{int(time.time() * 1000)}"

    def ensure_api_cache_session_id(self) -> str:
        """返回当前对话持久化的随机远端缓存键。"""
        if not self.api_cache_session_id:
            self.api_cache_session_id = str(uuid.uuid4())
        return self.api_cache_session_id

    def ask_conversation_name(self) -> str:
        """创建新对话或首次发送前，必须先让用户命名对话。"""
        name, ok = QInputDialog.getText(
            self,
            "命名新对话",
            "请输入对话名称：",
        )

        if not ok:
            return ""

        name = name.strip()

        if not name:
            QMessageBox.information(self, "需要对话名称", "新对话必须先命名。")
            return ""

        return name

    def load_conversation_sessions(self):
        """从本地 JSON 读取历史对话。"""
        path = self.conversation_history_path()

        if not path.exists():
            self.conversation_sessions = []
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            self.conversation_sessions = []
            return

        if isinstance(data, list):
            self.conversation_sessions = [
                item for item in data
                if isinstance(item, dict) and item.get("id") and item.get("name")
            ]
        else:
            self.conversation_sessions = []

    def write_conversation_sessions(self):
        """将历史对话写入本地 JSON。"""
        path = self.conversation_history_path()
        assets = chat_asset_dir(path)
        compact_sessions = externalize_chat_images(self.conversation_sessions, assets)
        atomic_write_chat_sessions(path, compact_sessions)
        # Publish JSON first. Only then remove assets that the successfully
        # saved history no longer references.
        self.conversation_sessions = compact_sessions
        prune_unreferenced_chat_assets(assets, compact_sessions)

    def find_conversation_record(self, session_id: str) -> dict | None:
        for record in self.conversation_sessions:
            if record.get("id") == session_id:
                return record
        return None

    def ensure_current_conversation_named(self) -> bool:
        """
        保证当前对话已经命名。

        用户点击“发送”时，如果当前还没有会话名称，会先弹窗要求命名；
        用户取消命名则不发送消息。
        """
        if self.current_session_id:
            return True

        if self.embedded:
            QMessageBox.information(self, "请选择文献", "请先在文献列表中选择一篇文献，再使用文献对话。")
            return False

        name = self.ask_conversation_name()

        if not name:
            return False

        self.current_session_id = self.make_conversation_id()
        self.current_conversation_name = name
        self.save_current_conversation_to_history()
        self.update_current_conversation_label()
        return True

    def save_current_conversation_to_history(self):
        """保存当前对话，供左下角对话记录继续加载。"""
        if not self.current_session_id:
            return

        now = self.current_timestamp_text()
        record = self.find_conversation_record(self.current_session_id)

        if record is None:
            record = {
                "id": self.current_session_id,
                "name": self.current_conversation_name or "未命名对话",
                "created_at": now,
                "updated_at": now,
                "session_model": self.session_model,
                "messages": externalize_chat_images(
                    self.messages,
                    chat_asset_dir(self.conversation_history_path()),
                ),
                "api_cache_session_id": self.ensure_api_cache_session_id(),
            }
            self.conversation_sessions.append(record)
        else:
            record["name"] = self.current_conversation_name or record.get("name", "未命名对话")
            record["updated_at"] = now
            record["session_model"] = self.session_model
            record["messages"] = externalize_chat_images(
                self.messages,
                chat_asset_dir(self.conversation_history_path()),
            )
            record["api_cache_session_id"] = self.ensure_api_cache_session_id()

        # 文献会话必须记录正文版本。仅用目录作为会话 ID 时，重新解析并覆盖同一
        # full.cleaned.md 会让旧上下文被误认为仍然有效。
        if self.embedded and self.current_embedded_document_fingerprint:
            record["document_fingerprint"] = self.current_embedded_document_fingerprint
            if self.current_embedded_document_path is not None:
                record["document_source_path"] = str(self.current_embedded_document_path)

        self.write_conversation_sessions()
        self.refresh_conversation_history_list()
        self.update_current_conversation_label()

    def refresh_conversation_history_list(self):
        """刷新左下角对话记录列表。"""
        if not hasattr(self, "conversation_history_list"):
            return

        self.conversation_history_list.blockSignals(True)
        self.conversation_history_list.clear()

        sorted_sessions = sorted(
            self.conversation_sessions,
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )

        for record in sorted_sessions:
            name = str(record.get("name", "未命名对话")).strip() or "未命名对话"
            message_count = len(record.get("messages") or [])

            # The custom label owns the display text; an empty item prevents duplicate rendering.
            item = QListWidgetItem("")
            item.setData(Qt.ItemDataRole.UserRole, record.get("id"))
            item.setData(Qt.ItemDataRole.DisplayRole, "")
            item.setData(Qt.ItemDataRole.ToolTipRole, f"{name}—{message_count} 条消息")

            label = QLabel()
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setWordWrap(False)
            label.setMinimumHeight(34)
            label.setText(
                "<div style='white-space: nowrap;'>"
                "<span style='font-size: 14px; font-weight: 700; color: #212121;'>"
                f"{html_escape(name)}"
                "</span>"
                "<span style='font-size: 13px; font-weight: 400; color: #212121;'>"
                f"—{message_count} 条消息"
                "</span>"
                "</div>"
            )
            label.setStyleSheet(f"""
                QLabel {{
                    /* 使用不透明背景，避免列表刷新、滚动或选中状态变化时出现残影。 */
                    background-color: {COLOR_BG_SURFACE_2};
                    color: {COLOR_TEXT_PRIMARY};
                    border: none;
                    padding: 5px 8px;
                    font-family: {APP_UI_FONT_FAMILY_STACK};
                }}
            """)

            self.conversation_history_list.addItem(item)

            # 宽度不要设为 1，否则部分 Qt 样式下 setItemWidget 会出现文本挤压、重叠或显示错乱。
            list_width = self.conversation_history_list.viewport().width()
            if list_width <= 0:
                list_width = 260
            item.setSizeHint(QSize(list_width, 38))

            self.conversation_history_list.setItemWidget(item, label)

            if record.get("id") == self.current_session_id:
                item.setSelected(True)

        self.conversation_history_list.blockSignals(False)

        # 强制刷新视口，清除 setItemWidget 重建后可能残留的旧绘制内容。
        self.conversation_history_list.doItemsLayout()
        self.conversation_history_list.updateGeometry()
        self.conversation_history_list.viewport().update()

    def update_current_conversation_label(self):
        if not hasattr(self, "current_conversation_label"):
            return

        self.current_conversation_label.setTextFormat(Qt.TextFormat.RichText)

        if self.current_conversation_name:
            # 对话标题加粗；只加粗标题本身，不把“当前对话：”也变粗。
            self.current_conversation_label.setText(
                f"当前对话：<b>{html_escape(self.current_conversation_name)}</b>"
            )
        else:
            self.current_conversation_label.setText("当前对话：<b>未命名</b>（发送前需要命名）")

    def create_new_conversation(self):
        """新建对话：先命名，再进入空白对话。"""
        if self.embedded:
            QMessageBox.information(self, "文献对话", "每篇文献都有独立对话记录，不需要新建对话。")
            return
        if self.chat_worker and self.chat_worker.isRunning():
            QMessageBox.information(self, "正在回复", "请先停止或等待当前回复完成，再新建对话。")
            return

        name = self.ask_conversation_name()

        if not name:
            return

        # 切换前保存旧对话。
        self.save_current_conversation_to_history()

        self.current_session_id = self.make_conversation_id()
        self.current_conversation_name = name
        self.messages = []
        self.session_model = None
        self.api_cache_session_id = ""

        # 新对话必须从空白待发送文件开始，避免旧对话选择的文件串到新对话。
        self.clear_documents(show_message=False)
        self.clear_chat_widgets_only()
        self.clear_pending_input_images()

        self.save_current_conversation_to_history()
        self.append_system_message(f"已创建新对话：{name}")

    def show_conversation_history_context_menu(self, pos):
        """对话记录右键菜单：只提供删除当前条目，不再重复提供“载入对话”。"""
        item = self.conversation_history_list.itemAt(pos)
        if not item:
            return

        self.conversation_history_list.setCurrentItem(item)

        menu = QMenu(self)
        delete_action = menu.addAction("删除该条记录")

        action = menu.exec(self.conversation_history_list.mapToGlobal(pos))

        if action == delete_action:
            self.delete_selected_conversation()

    def clear_all_conversation_records(self):
        """清空所有对话记录，并同步清空当前聊天上下文和界面。"""
        if self.embedded:
            if not self.current_session_id:
                return
            confirm = QMessageBox.question(
                self,
                "清空当前文献对话",
                "确定清空当前文献的对话历史吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm == QMessageBox.StandardButton.Yes:
                self.clear_current_embedded_document_history()
            return
        if self.chat_worker and self.chat_worker.isRunning():
            QMessageBox.information(self, "正在回复", "请先停止或等待当前回复完成，再清空记录。")
            return

        if not self.conversation_sessions and not self.messages:
            QMessageBox.information(self, "没有记录", "当前没有可清空的对话记录。")
            return

        confirm = QMessageBox.question(
            self,
            "清空全部对话记录",
            "确定清空所有对话记录吗？\n\n"
            "此操作只会清空文献对话历史和当前聊天内容，不会进入回收站。\n"
            "不会删除 MinerU 解析结果、译文、图片或原始文件副本。",
        )

        if confirm != QMessageBox.StandardButton.Yes:
            return

        # Conversation cleanup must not remove parsed documents, translations,
        # images, or source-file copies managed by the main workspace.

        # 清空内存中的所有历史会话和当前会话状态。
        self.conversation_sessions = []
        self.current_session_id = ""
        self.current_conversation_name = ""
        self.api_cache_session_id = ""
        self.messages = []
        self.session_model = None

        # 同步清空本轮待发送文件、待发送图片和聊天区控件，避免残留上下文。
        self.clear_documents(show_message=False)
        self.clear_pending_input_images()
        self.clear_chat_widgets_only()

        # 写回空列表，确保本地 JSON 文件中的历史记录也被清空。
        self.write_conversation_sessions()
        self.refresh_conversation_history_list()
        self.update_current_conversation_label()
        self.append_system_message("已清空全部对话记录。")

    def delete_selected_conversation(self):
        """删除左下角选中的单条对话记录。"""
        selected_items = self.conversation_history_list.selectedItems()

        if not selected_items:
            QMessageBox.information(self, "未选择记录", "请先在左下角选择一条对话记录。")
            return

        item = selected_items[0]
        session_id = item.data(Qt.ItemDataRole.UserRole)

        if not session_id:
            return

        record = self.find_conversation_record(session_id)
        name = record.get("name", "未命名对话") if record else "未命名对话"

        confirm = QMessageBox.question(
            self,
            "删除该条记录",
            f"确定删除对话“{name}”吗？\n\n"
            "此操作只删除这条对话记录，不会删除其引用过的文档解析结果、译文或图片。\n"
            "聊天记录删除后不会进入回收站。",
        )

        if confirm != QMessageBox.StandardButton.Yes:
            return

        # Deleting a conversation must leave its parsed documents and exported
        # files available to the main workspace.
        self.conversation_sessions = [
            record for record in self.conversation_sessions
            if record.get("id") != session_id
        ]
        self.write_conversation_sessions()

        if session_id == self.current_session_id:
            self.current_session_id = ""
            self.current_conversation_name = ""
            self.api_cache_session_id = ""
            self.messages = []
            self.session_model = None

            # 删除当前对话时，同步清掉本轮待发送文件和图片附件。
            self.clear_documents(show_message=False)
            self.clear_pending_input_images()
            self.clear_chat_widgets_only()

            self.update_current_conversation_label()

        self.refresh_conversation_history_list()

    def load_conversation_from_item(self, item: QListWidgetItem):
        """单击历史记录后，载入该对话并可继续发送消息。"""
        if self.chat_worker and self.chat_worker.isRunning():
            QMessageBox.information(self, "正在回复", "请先停止或等待当前回复完成，再切换对话。")
            return

        session_id = item.data(Qt.ItemDataRole.UserRole)
        record = self.find_conversation_record(session_id)

        if not record:
            QMessageBox.warning(self, "记录不存在", "未找到这条对话记录，可能已经被删除。")
            self.refresh_conversation_history_list()
            return

        self.save_current_conversation_to_history()

        self.current_session_id = record.get("id", "")
        self.current_conversation_name = record.get("name", "未命名对话")
        self.api_cache_session_id = str(record.get("api_cache_session_id") or "")
        self.messages = hydrate_chat_images(
            record.get("messages") or [],
            chat_asset_dir(self.conversation_history_path()),
        )
        self.session_model = record.get("session_model")

        # 历史对话里的文档已经保存在 messages 中。
        # 切回历史对话时不能把右下角待发送文件再次加入，否则会重复塞文档。
        self.clear_documents(show_message=False, delete_parse_outputs=False)
        self.clear_pending_input_images()

        self.clear_chat_widgets_only()
        self.render_messages_from_history()
        self.update_current_conversation_label()
        self.refresh_conversation_history_list()
        self.append_system_message(f"已载入对话：{self.current_conversation_name}")

    @staticmethod
    def document_fingerprint(markdown_path: Path | None) -> str:
        """Return a cheap, stable content fingerprint for a parsed Markdown document."""
        if not isinstance(markdown_path, Path) or not markdown_path.exists():
            return ""
        digest = hashlib.sha256()
        try:
            with markdown_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return ""
        return digest.hexdigest()

    def archive_document_revision(self, session_id: str, record: dict, title: str):
        """Keep an old re-parsed-document conversation instead of mixing it with new text."""
        previous_fingerprint = str(record.get("document_fingerprint") or "legacy")[:12]
        archive_id = f"{session_id}-revision-{previous_fingerprint}"
        suffix = 2
        while self.find_conversation_record(archive_id) is not None:
            archive_id = f"{session_id}-revision-{previous_fingerprint}-{suffix}"
            suffix += 1
        archived = dict(record)
        archived["id"] = archive_id
        archived["name"] = f"{title}（旧解析版本）"
        archived["archived_document_revision"] = True
        self.conversation_sessions.remove(record)
        self.conversation_sessions.append(archived)

    def load_document_conversation(self, session_id: str, title: str, markdown_path: Path):
        """Embedded mode: one stable conversation per parsed document."""
        if self.chat_worker and self.chat_worker.isRunning():
            self.pending_embedded_document_load = (session_id, title, markdown_path)
            return False
        self.save_current_conversation_to_history()
        record = self.find_conversation_record(session_id)
        self.current_session_id = session_id
        self.current_conversation_name = title
        self.current_embedded_document_path = markdown_path.resolve() if markdown_path and markdown_path.exists() else None
        self.current_embedded_document_fingerprint = self.document_fingerprint(self.current_embedded_document_path)

        # 有明确版本标识的旧记录与当前正文不一致时，不能继续把旧全文当作当前论文。
        # 旧版本另存，当前论文从空会话重新开始并自动附上新版 Markdown。
        if (
            record
            and record.get("document_fingerprint")
            and self.current_embedded_document_fingerprint
            and record.get("document_fingerprint") != self.current_embedded_document_fingerprint
        ):
            self.archive_document_revision(session_id, record, title)
            record = None

        self.api_cache_session_id = str(record.get("api_cache_session_id") or "") if record else ""

        # 切换论文时，草稿和引用都属于上一论文，绝不能带入新论文的请求。
        self.input_box.clear()
        self.clear_pending_reference_quote()
        if record:
            self.messages = hydrate_chat_images(
                record.get("messages") or [],
                chat_asset_dir(self.conversation_history_path()),
            )
            self.session_model = record.get("session_model")
            self.clear_documents(show_message=False, delete_parse_outputs=False)
            self.clear_pending_input_images()
            self.clear_chat_widgets_only()
            self.render_messages_from_history()
        else:
            self.messages = []
            self.session_model = None
            self.clear_documents(show_message=False, delete_parse_outputs=False)
            self.clear_pending_input_images()
            self.clear_chat_widgets_only()
            self.save_current_conversation_to_history()

        # 空会话记录可能已在上次启动时提前落盘，但“待发送文档”不会写入 messages。
        # 因此无论记录是否存在，都重新核对一次：只要历史中尚未发送原文，就把当前论文放回待发送区。
        self.ensure_embedded_document_attached()
        self.update_current_conversation_label()
        callback = self.embedded_document_loaded_callback
        if callable(callback):
            callback(session_id)
        return True

    def delete_document_conversation(self, session_id: str):
        if self.pending_embedded_document_load and self.pending_embedded_document_load[0] == session_id:
            self.pending_embedded_document_load = None
        self.conversation_sessions = [
            record for record in self.conversation_sessions
            if record.get("id") != session_id
        ]
        if self.current_session_id == session_id:
            self.current_session_id = ""
            self.current_conversation_name = ""
            self.api_cache_session_id = ""
            self.messages = []
            self.session_model = None
            self.clear_documents(show_message=False, delete_parse_outputs=False)
            self.clear_pending_input_images()
            self.clear_chat_widgets_only()
        self.write_conversation_sessions()
        self.refresh_conversation_history_list()
        self.update_current_conversation_label()

    def clear_current_embedded_document_history(self):
        """
        内嵌文献对话的“清空历史”只清当前文献对话内容。

        保留当前文献会话 ID，并把原文解析 Markdown 重新放回待发送区；
        这样下一次发送仍从这篇文献的空白对话开始。
        """
        if self.chat_worker and self.chat_worker.isRunning():
            QMessageBox.information(self, "正在回复", "请先停止或等待当前回复完成，再清空对话。")
            return

        session_id = self.current_session_id
        title = self.current_conversation_name
        markdown_path = getattr(self, "current_embedded_document_path", None)

        self.conversation_sessions = [
            record for record in self.conversation_sessions
            if record.get("id") != session_id
        ]
        self.current_session_id = session_id
        self.current_conversation_name = title
        self.messages = []
        self.session_model = None
        self.api_cache_session_id = ""
        self.clear_documents(show_message=False, delete_parse_outputs=False)
        self.clear_pending_input_images()
        self.clear_chat_widgets_only()
        if isinstance(markdown_path, Path) and markdown_path.exists():
            self.attach_markdown_document(markdown_path)
        self.save_current_conversation_to_history()
        self.refresh_conversation_history_list()
        self.update_current_conversation_label()

    def delete_document_conversations_except(self, valid_session_ids: set[str]):
        valid_session_ids = {str(item) for item in valid_session_ids if item}
        if (
            self.pending_embedded_document_load
            and self.pending_embedded_document_load[0] not in valid_session_ids
        ):
            self.pending_embedded_document_load = None
        def is_valid_document_record(record_id: str) -> bool:
            return record_id in valid_session_ids or any(
                record_id.startswith(f"{session_id}-revision-")
                for session_id in valid_session_ids
            )
        before = len(self.conversation_sessions)
        self.conversation_sessions = [
            record for record in self.conversation_sessions
            if not str(record.get("id") or "").startswith("doc-chat-")
            or is_valid_document_record(str(record.get("id") or ""))
        ]
        if self.current_session_id.startswith("doc-chat-") and self.current_session_id not in valid_session_ids:
            self.current_session_id = ""
            self.api_cache_session_id = ""
            self.current_conversation_name = ""
            self.messages = []
            self.session_model = None
            self.clear_documents(show_message=False, delete_parse_outputs=False)
            self.clear_pending_input_images()
            self.clear_chat_widgets_only()
        if len(self.conversation_sessions) != before:
            self.write_conversation_sessions()
            self.refresh_conversation_history_list()
            self.update_current_conversation_label()

    def message_content_to_display_text(self, content) -> str:
        """
        将 OpenAI 兼容消息 content 转成历史区可显示文本。

        注意：
        1. 原始 self.messages 不会被修改，继续对话时仍保留完整上下文。
        2. 如果历史消息过长，只折叠界面显示，不裁剪实际上下文。
        3. 只有被严格识别为“文档上下文”的消息，才允许按“===== 用户问题 =====”改写显示。
           普通用户代码里可能也包含这些字符串，不能误判为文档。
        """
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts = []

            for part in content:
                if isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                    text_parts.append(str(part.get("text") or ""))

            text = "\n".join(part for part in text_parts if part)
        else:
            text = str(content or "")

        # Rewrite only recognized document-context messages; user text may contain the delimiter.
        if self.looks_like_real_document_context_text(text) and "===== 用户问题 =====" in text:
            question = text.rsplit("===== 用户问题 =====", 1)[-1].strip()
            text = "📄 历史文档上下文已保留，可继续追问。\n\n用户问题：\n" + question

        # Let ChatTextBubble fold long history without altering stored content.
        return text

    def create_message_navigator(self):
        """Create the floating user-message navigator over the chat viewport."""
        # 放在右侧聊天面板上，而不是滚动 viewport 内。这样聊天内容滚动/重绘时
        # 不会盖住圆球，也不会把它一起卷走。
        self.message_navigator_button = QToolButton(self.right_panel)
        self.message_navigator_button.setObjectName("messageNavigatorButton")
        self.message_navigator_button.setText("☰")
        self.message_navigator_button.setToolTip("对话定位：查看并跳转到已发送的问题")
        self.message_navigator_button.setFixedSize(38, 38)
        self.message_navigator_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.message_navigator_button.setStyleSheet(f"""
            QToolButton#messageNavigatorButton {{
                background: {COLOR_ACCENT}; color: #FFFFFF; border: 2px solid #FFFFFF;
                border-radius: 19px; font-size: 17px; font-weight: 700; padding: 0px;
            }}
            QToolButton#messageNavigatorButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}
            QToolButton#messageNavigatorButton:pressed {{ background: {COLOR_ACCENT_PRESS}; }}
            QToolButton#messageNavigatorButton:disabled {{ background: #B8B8B3; color: #F4F4F1; }}
        """)
        self.message_navigator_button.clicked.connect(self.show_message_navigator)
        self.message_navigator_popup = None
        self.chat_scroll_area.installEventFilter(self)
        self.chat_scroll_area.viewport().installEventFilter(self)
        self.chat_scroll_area.verticalScrollBar().sliderPressed.connect(
            self.mark_stream_scroll_interrupted
        )
        self.position_message_navigator()
        QTimer.singleShot(0, self.position_message_navigator)
        self.refresh_message_navigator()

    def position_message_navigator(self):
        """Keep the navigator at the chat area's upper-left corner."""
        if not hasattr(self, "message_navigator_button"):
            return
        chat_area_pos = self.chat_scroll_area.pos()
        # Place the navigator just outside the chat surface so it does not cover text.
        self.message_navigator_button.move(max(2, chat_area_pos.x() - 12), chat_area_pos.y() + 12)
        self.message_navigator_button.raise_()

    def message_navigator_summary(self, content) -> str:
        """Return the leading part of a user's first sentence for the jump list."""
        text = " ".join(self.message_content_to_display_text(content).split()).strip()
        if not text:
            return "（图片消息）"
        sentence_end = re.search(r"[。！？!?\n]", text)
        if sentence_end:
            text = text[:sentence_end.start() + 1]
        max_length = 38
        return text if len(text) <= max_length else text[:max_length - 1].rstrip() + "…"

    def refresh_message_navigator(self):
        """Refresh availability; list contents are rebuilt when the ball is opened."""
        if not hasattr(self, "message_navigator_button"):
            return
        has_user_messages = any(message.get("role") == "user" for message in self.messages)
        self.message_navigator_button.setEnabled(has_user_messages)
        self.message_navigator_button.setToolTip(
            "对话定位：查看并跳转到已发送的问题" if has_user_messages else "发送第一条消息后可定位对话"
        )

    def show_message_navigator(self):
        entries = [
            (index, self.message_navigator_summary(message.get("content")))
            for index, message in enumerate(self.messages)
            if message.get("role") == "user"
        ]
        if not entries:
            return

        if self.message_navigator_popup is not None:
            # 导航球是开关：列表已展开时再次点击应收起，而不是新建一个列表。
            if self.message_navigator_popup.isVisible():
                self.message_navigator_popup.close()
                self.message_navigator_popup = None
                return
            self.message_navigator_popup.deleteLater()
            self.message_navigator_popup = None

        # 作为聊天面板的子控件浮在内容上方。相比原生 Popup，这样既不会出现
        # Windows 黑色边角，也不会在第二次点击圆球前自动关闭而导致又重新展开。
        popup = QFrame(self.right_panel)
        popup.setObjectName("messageNavigatorPopup")
        popup.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        popup.setFixedWidth(318)
        # 这个弹窗使用直角外框而非带阴影的半透明圆角：在 Windows 的 Popup
        # 原生窗口上，阴影与圆角叠加会在四角留下黑色像素。
        popup.setStyleSheet(f"""
            QFrame#messageNavigatorPopup {{
                background: {COLOR_BG_SURFACE_2}; border: 1px solid {COLOR_BORDER_HAIR}; border-radius: 0px;
            }}
            QListWidget {{ background: transparent; border: none; outline: none; padding: 6px; color: {COLOR_TEXT_PRIMARY}; }}
            QListWidget::item {{ border-radius: 8px; padding: 8px 10px; margin: 1px 0px; }}
            QListWidget::item:hover, QListWidget::item:selected {{ background: {COLOR_ACCENT_SOFT}; color: {COLOR_TEXT_PRIMARY}; }}
            QScrollBar:vertical {{ width: 6px; background: transparent; }}
            QScrollBar::handle:vertical {{ background: #A5A5A0; border-radius: 3px; min-height: 24px; }}
        """)
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(5, 5, 5, 5)
        message_list = QListWidget()
        message_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        message_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for message_index, summary in entries:
            item = QListWidgetItem(summary)
            item.setData(Qt.ItemDataRole.UserRole, message_index)
            item.setToolTip(summary)
            message_list.addItem(item)
        message_list.itemClicked.connect(lambda item: self.jump_to_message_from_navigator(item, popup))
        layout.addWidget(message_list)
        # 即使当前消息较少，也预留至少五条摘要的可视容量；更多消息时滚动查看。
        popup.setFixedHeight(min(340, max(231, len(entries) * 43 + 16)))
        self.message_navigator_popup = popup

        # 左边缘与圆球对齐，因此列表从左上角圆球的右下方展开。
        popup.move(
            self.message_navigator_button.x(),
            self.message_navigator_button.y() + self.message_navigator_button.height() + 6,
        )
        popup.show()
        popup.raise_()
        message_list.setFocus(Qt.FocusReason.MouseFocusReason)

    def jump_to_message_from_navigator(self, item: QListWidgetItem, popup: QFrame):
        message_index = item.data(Qt.ItemDataRole.UserRole)
        row_widget = self.message_row_widgets.get(int(message_index)) if message_index is not None else None
        if row_widget is not None:
            y = row_widget.mapTo(self.chat_container, QPoint(0, 0)).y()
            self.chat_scroll_area.verticalScrollBar().setValue(
                max(0, min(y - 10, self.chat_scroll_area.verticalScrollBar().maximum()))
            )
        popup.close()
        self.message_navigator_popup = None

    @staticmethod
    def message_content_text_parts(content) -> list[str]:
        if isinstance(content, str):
            return [content]

        if not isinstance(content, list):
            return [str(content or "")]

        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                parts.append(str(part.get("text") or ""))
        return parts

    def message_content_to_full_text(self, content) -> str:
        return "\n".join(part for part in self.message_content_text_parts(content) if part)

    def document_records_from_history_text(self, text: str) -> list[dict]:
        """
        从历史 messages 的文档上下文文本中重建可点击文档气泡所需记录。

        仅用于界面恢复；不修改原始 messages，继续对话仍使用完整上下文。
        """
        records: list[dict] = []
        pattern = re.compile(
            r"^===== 文档\s+\d+:\s*(?P<title>.*?)\s*=====\s*\n来源:\s*(?P<path>[^\n]+)",
            re.M | re.S,
        )

        for match in pattern.finditer(text or ""):
            title = match.group("title").strip() or "未命名文档"
            path_text = match.group("path").strip()
            if not path_text:
                continue
            records.append({
                "title": title,
                "path": path_text,
            })

        return records

    def looks_like_real_document_context_text(self, text: str) -> bool:
        """
        严格判断一段文本是否真的是程序构造的文档上下文。

        A document message must start with the generated context header and
        contain a document block followed by its source line. This prevents
        ordinary user text containing the same delimiters from being reclassified.
        """
        text = str(text or "")
        stripped = text.lstrip()

        if not stripped.startswith("以下是用户添加的文档全文。请基于这些文档回答后续问题"):
            return False

        return bool(
            re.search(
                r"^===== 文档\s+\d+:\s*.+?\s*=====\s*\n来源:\s*.+$",
                stripped,
                flags=re.M,
            )
        )

    def is_document_context_history_message(self, content) -> bool:
        text = self.message_content_to_full_text(content)
        return self.looks_like_real_document_context_text(text)

    def document_history_display_text(self, content, images_filtered: bool = False) -> str:
        text = self.message_content_to_full_text(content)
        records = self.document_records_from_history_text(text)
        titles = "；".join(record.get("title", "未命名文档") for record in records) or "未命名文档"

        send_mode = ""
        mode_match = re.search(r"文档发送方式:\s*(?P<mode>[^\n]+)", text)
        if mode_match:
            send_mode = mode_match.group("mode").strip()

        question = "请先阅读并概括这些文档。"
        if "===== 用户问题 =====" in text:
            # 文档正文中理论上也可能出现类似分隔符，因此取最后一个更符合程序构造格式。
            question = text.rsplit("===== 用户问题 =====", 1)[-1].strip() or question

        image_count = 0 if images_filtered else len(self.message_content_to_display_images(content))

        lines = [
            "📄 已发送文档",
            f"文档数量: {len(records) or 1}",
            f"文档: {titles}",
        ]
        if send_mode:
            if images_filtered and "全文带图" in send_mode:
                send_mode += "（此次发送未包含图片）"
            lines.append(f"发送方式: {send_mode}")
        lines.extend([
            f"发送的文档图片: {image_count} 张",
            f"用户问题: {question}",
            "",
            "点击使用系统默认程序打开原始文献。",
        ])
        return "\n".join(lines)

    def data_url_to_pixmap(self, data_url: str) -> QPixmap:
        pixmap = QPixmap()
        if not isinstance(data_url, str) or not data_url.startswith("data:image/") or "," not in data_url:
            return pixmap
        try:
            raw = base64.b64decode(data_url.split(",", 1)[1], validate=False)
            pixmap.loadFromData(raw)
        except Exception:
            pass
        return pixmap

    @staticmethod
    def detect_image_mime_type_from_bytes(image_bytes: bytes) -> str:
        if not image_bytes:
            return "image/png"

        byte_array = QByteArray(image_bytes)
        buffer = QBuffer(byte_array)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        image_format = bytes(reader.format()).decode("ascii", errors="ignore").lower().strip()
        buffer.close()

        if image_format in ("jpg", "jpeg"):
            return "image/jpeg"
        if image_format == "webp":
            return "image/webp"
        if image_format == "bmp":
            return "image/bmp"
        if image_format == "gif":
            return "image/gif"
        return "image/png"

    def message_content_to_display_images(self, content) -> list[dict]:
        images: list[dict] = []
        if not isinstance(content, list):
            return images
        for index, part in enumerate(content, 1):
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image_url = part.get("image_url")
            data_url = image_url.get("url") if isinstance(image_url, dict) else image_url
            pixmap = self.data_url_to_pixmap(data_url)
            if not pixmap.isNull():
                images.append({"pixmap": pixmap, "data_url": data_url, "name": f"历史图片{index}.png"})
        return images

    def remove_new_manual_image_parts_from_content(self, content):
        if not isinstance(content, list):
            return content
        stripped_parts = []
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "image_url"
                and part.get("local_image_origin") == "manual_new"
            ):
                continue
            stripped_parts.append(part)
        return stripped_parts

    def convert_new_manual_images_to_history_placeholders(self, content):
        if not isinstance(content, list):
            return content
        converted_parts = []
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "image_url"
                and part.get("local_image_origin") == "manual_new"
            ):
                image_url = part.get("image_url")
                converted = {
                    "type": "image_url",
                    "image_url": image_url,
                    "local_image_origin": "manual_history",
                }
                converted_parts.append(converted)
            else:
                converted_parts.append(part)
        return converted_parts

    def commit_new_manual_images_in_history(self):
        changed = False
        for message in self.messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            new_content = self.convert_new_manual_images_to_history_placeholders(content)
            if new_content != content:
                message["content"] = new_content
                if not self.is_document_context_history_message(new_content):
                    message["has_manual_images"] = True
                changed = True
        return changed

    def discard_new_manual_images_from_latest_user_message(self) -> bool:
        for message in reversed(self.messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            stripped_content = self.remove_new_manual_image_parts_from_content(content)
            if stripped_content != content:
                message["content"] = stripped_content
                message["manual_images_filtered"] = True
                return True
            return False
        return False

    def has_previous_image_context(self, before_index: int) -> bool:
        for index in range(before_index - 1, -1, -1):
            if self.message_content_to_display_images(self.messages[index].get("content")):
                return True
        return False

    def infer_image_mode_for_message(self, user_message_index: int, content) -> str:
        if self.local_reference_image_path or self.selected_reference_images:
            return "edit"

        if self.message_content_to_display_images(content):
            return "edit"

        if self.has_previous_image_context(user_message_index):
            return "edit"

        return "generation"

    def render_messages_from_history(self):
        """根据 self.messages 重建聊天区显示。"""
        self.assistant_bubbles = []
        self.system_bubbles = []
        self.message_row_widgets = {}
        model_marked_no_images = self.current_model_marked_non_multimodal()
        self._bulk_rendering_messages = True

        try:
            for message_index, message in enumerate(self.messages):
                role = message.get("role")
                content = message.get("content")

                if role == "user" and self.is_document_context_history_message(content):
                    records = self.document_records_from_history_text(self.message_content_to_full_text(content))
                    self.append_document_message(
                        self.document_history_display_text(content, images_filtered=model_marked_no_images),
                        records,
                        reference_quote=message.get("reference_quote") if isinstance(message.get("reference_quote"), dict) else None,
                        message_index=message_index,
                    )
                    continue

                text = self.message_content_to_display_text(content)
                images = self.message_content_to_display_images(content)

                if role == "user":
                    self.append_user_message(
                        text,
                        images,
                        message_index=message_index,
                        reference_quote=message.get("reference_quote") if isinstance(message.get("reference_quote"), dict) else None,
                        hide_images=model_marked_no_images and bool(images),
                    )
                elif role == "assistant":
                    reasoning_text = str(message.get("reasoning_content") or "")

                    if reasoning_text.strip() and text.strip():
                        self.append_assistant_message_with_reasoning(
                            text,
                            reasoning_text,
                            message_index=message_index,
                        )
                    elif text.strip():
                        self.append_assistant_message(text, message_index=message_index)

                    for image_item in images:
                        pixmap = image_item.get("pixmap", QPixmap())
                        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
                            self.add_bubble_row(
                                ChatImageLabel(
                                    pixmap,
                                    image_data_url=image_item.get("data_url", ""),
                                    on_set_as_reference=self.set_reference_image_from_chat,
                                ),
                                "assistant",
                            )
        finally:
            self._bulk_rendering_messages = False

        # Only the newest reply owns a Chromium/MathJax surface. Older replies
        # remain readable through the lightweight renderer and promote on click.
        if self.assistant_bubbles:
            self.activate_assistant_web_bubble(self.assistant_bubbles[-1])

        self.current_assistant_label = None
        self.current_reasoning_widget = None
        self.refresh_message_navigator()
        self.schedule_bubble_width_update()
        self.scroll_chat_to_bottom(force=True)

    def clear_chat_widgets_only(self):
        """只清空聊天区控件，不修改 self.messages。"""
        self.flush_stream_buffers()

        self.current_assistant_label = None
        self.current_reasoning_widget = None
        self.pending_assistant_text = ""
        self.pending_reasoning_text = ""
        self.cancel_requested = False
        self.system_bubbles = []
        self.assistant_bubbles = []
        self.chat_bubbles = []
        self.message_row_widgets = {}
        self.refresh_message_navigator()

        for reader in list(self.document_reader_windows):
            try:
                reader.close()
            except Exception:
                pass

        self.document_reader_windows = []

        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def record_system_message(self, text: str):
        text = str(text or "").strip()
        if not text:
            return
        self.system_message_history.append({
            "time": self.current_timestamp_text(),
            "text": text,
        })

    def hide_system_message_toast(self):
        if hasattr(self, "system_toast_label"):
            self.system_toast_label.setVisible(False)

    def show_system_message_toast(self, text: str):
        if not self.embedded or not hasattr(self, "system_toast_label"):
            return
        text = " ".join(str(text or "").split())
        if not text:
            return
        full_text = text
        if len(text) > 42:
            text = text[:39] + "..."
        self.system_toast_label.setText(text)
        self.system_toast_label.setToolTip(full_text)
        self.system_toast_label.updateGeometry()
        self.system_toast_label.setVisible(True)
        self.system_toast_timer.start()

    def show_system_messages_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("系统消息")
        dialog.resize(520, 420)
        dialog.setStyleSheet(self.styleSheet())
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        output = QTextBrowser()
        output.setOpenExternalLinks(False)
        output.setStyleSheet("""
            QTextBrowser {
                background-color: #FFFFFF;
                border: 1px solid #D7D7D2;
                border-radius: 8px;
                padding: 8px;
                font-size: 13px;
            }
        """)
        if self.system_message_history:
            parts = []
            for item in self.system_message_history:
                timestamp = html_escape(str(item.get("time") or ""))
                message = html_escape(str(item.get("text") or ""))
                parts.append(
                    f"<p style='margin:0 0 10px 0;'>"
                    f"<span style='color:#777;'>{timestamp}</span><br>"
                    f"<span>{message}</span>"
                    f"</p>"
                )
            output.setHtml("".join(parts))
        else:
            output.setPlainText("暂无消息。")
        layout.addWidget(output, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def on_markdown_render_toggled(self, checked: bool):
        """切换模型回答的 Markdown 渲染状态，并刷新现有气泡。"""
        for bubble in list(self.assistant_bubbles):
            if bubble is None:
                continue
            if hasattr(bubble, "set_render_markdown"):
                bubble.set_render_markdown(checked)

    def on_provider_changed(self):
        provider_id = self.get_current_provider()
        spec = get_provider_spec(provider_id)
        stored_provider = self.settings.chat_providers.get(provider_id)

        self.key_input.setText(load_provider_key(spec))
        self.url_input.setText(
            stored_provider.base_url.strip()
            if stored_provider and stored_provider.base_url.strip()
            else load_provider_base_url(spec)
        )

        self.model_combo.clear()

        stored_model = stored_provider.model if stored_provider and stored_provider.model else ""
        initial_model = stored_model or load_provider_model(spec)
        if initial_model:
            self.model_combo.addItem(initial_model)
            self.model_combo.setCurrentText(initial_model)

        self.update_reasoning_visibility()
        self.update_image_mode_visibility()
        self.rerender_history_after_image_filter_state_change()

        self.append_system_message(f"已切换服务商：{spec.display_name}")
        if self.isVisible() and spec.supports_model_list:
            # 切换服务商后的列表属于新服务商；后台刷新失败不能打断正在阅读/对话的用户。
            QTimer.singleShot(0, lambda: self.fetch_models(silent=True))

    def choose_model_after_refresh(self, model_ids: list[str], requested_model: str) -> tuple[str, bool]:
        if requested_model and requested_model in model_ids:
            return requested_model, False
        if not requested_model and model_ids:
            return model_ids[0], False
        return "", bool(requested_model)

    def on_model_selection_changed(self, model: str):
        if getattr(self, "_syncing_shared_settings", False):
            return
        model = (model or "").strip()
        if not model or not hasattr(self, "url_input"):
            return
        provider_id = self.get_current_provider()
        self.settings.chat_provider = provider_id
        self.settings.chat_providers[provider_id] = app_config.ProviderSettings(
            provider_id=provider_id,
            base_url=self.url_input.text().strip(),
            model=model,
            request_body_mode=self.request_body_mode_for_provider(provider_id),
        )
        app_config.save_settings(self.settings)
        self.rerender_history_after_image_filter_state_change()

    def update_reasoning_visibility(self):
        provider_id = self.get_current_provider()
        spec = get_provider_spec(provider_id)
        model = self.model_combo.currentText().strip()

        # 硅基流动按模型能力显示；其他供应商按其协议能力显示。
        supports_reasoning = provider_supports_reasoning_for_model(
            provider_id,
            self.url_input.text(),
            model,
        )
        self.reasoning_group.setVisible(supports_reasoning)

        if is_siliconflow_provider(provider_id, self.url_input.text()) and model:
            self.maybe_probe_siliconflow_thinking_capability(provider_id, model)

        if is_deepseek_reasoning_protocol(provider_id, self.url_input.text(), model):
            self.reasoning_effort_combo.setToolTip(
                "DeepSeek Thinking 协议：官方强度为 high / max；low、medium 会映射到 high，xhigh 会映射到 max。"
            )
        else:
            self.reasoning_effort_combo.setToolTip(
                "OpenAI / OneAPI 兼容协议：常见值为 none、minimal、low、medium、high、xhigh；是否支持取决于具体模型。"
            )

    def maybe_probe_siliconflow_thinking_capability(self, provider_id: str, model: str):
        """Probe uncached SiliconFlow models once, without persisting API keys."""
        base_url = self.url_input.text().strip()
        api_key = self.key_input.text().strip()
        if not api_key or not base_url or cached_thinking_capability(provider_id, base_url, model) is not None:
            return
        request = (provider_id, normalize_base_url(base_url, provider_id), model)
        if self.thinking_probe_worker and self.thinking_probe_worker.isRunning():
            if request != self._pending_thinking_probe:
                self._pending_thinking_probe = request
            return
        self._pending_thinking_probe = None
        self.thinking_probe_worker = ThinkingCapabilityProbeWorker(api_key, base_url, provider_id, model)
        self.thinking_probe_worker.capability_checked.connect(self.on_siliconflow_thinking_capability_checked)
        self.thinking_probe_worker.finished.connect(self.schedule_pending_thinking_probe)
        self.thinking_probe_worker.start()

    def on_siliconflow_thinking_capability_checked(self, provider_id: str, base_url: str, model: str, supports, error: str):
        if supports is None:
            return
        mark_thinking_capability(provider_id, base_url, model, bool(supports))
        if provider_id == self.get_current_provider() and model == self.model_combo.currentText().strip():
            self.update_reasoning_visibility()
        if supports:
            self.append_system_message(f"已校核 {model}：支持思考参数。")
        elif error:
            self.append_system_message(f"已校核 {model}：不支持思考参数，已隐藏相关设置。")

    def schedule_pending_thinking_probe(self):
        request = self._pending_thinking_probe
        self._pending_thinking_probe = None
        if request and request[0] == self.get_current_provider() and request[2] == self.model_combo.currentText().strip():
            QTimer.singleShot(0, lambda: self.maybe_probe_siliconflow_thinking_capability(request[0], request[2]))

    def eventFilter(self, obj, event):
        if (
            obj in (getattr(self, "chat_scroll_area", None), getattr(getattr(self, "chat_scroll_area", None), "viewport", lambda: None)())
            and event.type() in (QEvent.Type.Wheel, QEvent.Type.MouseButtonPress)
            and getattr(self, "chat_worker", None) is not None
            and self.chat_worker.isRunning()
        ):
            # Any direct gesture is an explicit opt-out from following the
            # stream.  The auto-scroll timer checks this flag before moving.
            self._stream_should_auto_scroll = False

        if obj == getattr(self, "chat_scroll_area", None) and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Move,
            QEvent.Type.Show,
        ):
            # 此时 QVBoxLayout 可能尚未给聊天区写入最终 geometry；延后一轮事件循环
            # 再定位，保证圆球紧贴实际聊天区左上角。
            QTimer.singleShot(0, self.position_message_navigator)

        if obj == getattr(self, "input_box", None) and event.type() == event.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                    self.send_message()
                    return True

        return super().eventFilter(obj, event)

    def get_api_key_and_url(self):
        api_key = self.key_input.text().strip()
        base_url = self.url_input.text().strip()

        if not api_key:
            QMessageBox.warning(self, "缺少 API 密钥", "请填写 API 密钥。")
            return None, None

        if not base_url:
            QMessageBox.warning(self, "缺少 API 地址", "请填写 API 地址，例如：https://api.example.com/v1")
            return None, None

        return api_key, base_url

    def save_current_api_settings(self):
        provider_id = self.get_current_provider()
        if is_machine_translation_provider_id(provider_id):
            return
        if not hasattr(self.settings, "provider_cards"):
            self.settings = app_config.load_settings()
        self.settings.chat_provider = provider_id
        self.settings.chat_providers[provider_id] = app_config.ProviderSettings(
            provider_id=provider_id,
            base_url=self.url_input.text().strip(),
            model=self.model_combo.currentText().strip(),
            request_body_mode=self.request_body_mode_for_provider(provider_id),
        )
        api_key = self.key_input.text().strip()
        save_chat_secret_with_session_fallback(self, provider_id, "api_key", api_key)
        if callable(self.shared_secret_save_callback) and api_key:
            self.shared_secret_save_callback(provider_id, "api_key", api_key)
        app_config.save_settings(self.settings)

    def get_config(self) -> AIConfig | None:
        provider_id = self.get_current_provider()
        spec = get_provider_spec(provider_id)

        api_key, base_url = self.get_api_key_and_url()
        if not api_key or not base_url:
            return None
        self.save_current_api_settings()

        model = self.model_combo.currentText().strip()

        if not model:
            QMessageBox.warning(self, "缺少模型名称", "请先展开模型选项并等待模型列表自动刷新完成。")
            return None

        thinking_mode = "default"
        reasoning_effort = "default"
        show_reasoning = False

        if provider_supports_reasoning_for_model(provider_id, base_url, model):
            thinking_mode = self.thinking_mode_combo.currentData() or "default"
            reasoning_effort = self.reasoning_effort_combo.currentText().strip() or "默认"
            show_reasoning = self.show_reasoning_checkbox.isChecked()

        image_mode = "generation"
        image_size = "auto"
        image_quality = "auto"
        image_output_format = "png"
        local_reference_image_path = ""
        selected_reference_images = []

        if spec.supports_images and is_probably_image_model(model):
            image_size = self.image_size_combo.currentText().strip() or "auto"
            image_quality = self.image_quality_combo.currentText().strip() or "auto"
            image_output_format = self.image_format_combo.currentText().strip() or "png"
            local_reference_image_path = self.local_reference_image_path
            selected_reference_images = [dict(item) for item in self.selected_reference_images]
            if local_reference_image_path or selected_reference_images:
                image_mode = "edit"

        return AIConfig(
            provider_id=provider_id,
            api_key=api_key,
            base_url=base_url,
            model=model,
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
            show_reasoning=show_reasoning,
            image_mode=image_mode,
            image_size=image_size,
            image_quality=image_quality,
            image_output_format=image_output_format,
            local_reference_image_path=local_reference_image_path,
            selected_reference_images=selected_reference_images,
            prompt_cache_key=self.ensure_api_cache_session_id(),
            request_body_mode=normalize_oneapi_request_body_mode(
                getattr(self.settings.chat_providers.get(provider_id), "request_body_mode", "codex")
            ),
        )

    def update_image_mode_visibility(self):
        provider_id = self.get_current_provider()
        spec = get_provider_spec(provider_id)

        model = self.model_combo.currentText().strip()
        is_image = spec.supports_images and is_probably_image_model(model)
        was_visible = self.image_group.isVisible()

        self.image_group.setVisible(is_image)

        if is_image and not was_visible:
            self.append_system_message("已检测到图片模型，图片模式设置已显示。")

        # 待发送文档会为图片模型保留、但不会进入图片请求。模型切换后立即
        # 刷新状态文案，避免继续显示“此次发送将包含文件”的误导性提示。
        self.refresh_document_status()

    def current_model_marked_non_multimodal(self) -> bool:
        config = self.get_config()
        if config is None:
            return False
        return is_marked_non_multimodal_model(config.provider_id, config.base_url, config.model)

    def rerender_history_after_image_filter_state_change(self):
        if not hasattr(self, "chat_layout"):
            return
        self.clear_chat_widgets_only()
        self.render_messages_from_history()

    def update_reference_image_preview(self, file_path: str):
        """显示用户选择的参考图预览。"""
        pixmap = QPixmap(file_path)

        if pixmap.isNull():
            self.reference_image_preview.setPixmap(QPixmap())
            self.reference_image_preview.setText("图片加载失败")
            return

        preview_pixmap = pixmap.scaled(
            self.reference_image_preview.width(),
            self.reference_image_preview.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        self.reference_image_preview.setText("")
        self.reference_image_preview.setPixmap(preview_pixmap)

    def update_reference_image_preview_from_pixmap(self, pixmap: QPixmap):
        if pixmap.isNull():
            self.reference_image_preview.setPixmap(QPixmap())
            self.reference_image_preview.setText("图片加载失败")
            return

        preview_pixmap = pixmap.scaled(
            self.reference_image_preview.width(),
            self.reference_image_preview.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        self.reference_image_preview.setText("")
        self.reference_image_preview.setPixmap(preview_pixmap)

    @staticmethod
    def reference_image_key(item: dict) -> str:
        kind = str(item.get("kind") or "")
        if kind == "local":
            return f"local:{item.get('path', '')}"
        return f"chat:{item.get('data_url', '')}"

    def rebuild_reference_image_summary(self):
        if not self.local_reference_image_path and not self.selected_reference_images:
            self.reference_image_label.setText("未选择参考图")
            self.reference_image_preview.setPixmap(QPixmap())
            self.reference_image_preview.setText("未选择参考图")
            self.clear_reference_image_button.setEnabled(False)
            return

        summary_lines = []

        if self.local_reference_image_path:
            summary_lines.append(f"本地参考图：{self.local_reference_image_path}")

        chat_refs = [
            item for item in self.selected_reference_images
            if str(item.get("kind") or "") == "chat"
        ]
        if chat_refs:
            summary_lines.append(f"已手动选中历史参考图：{len(chat_refs)} 张")

        total_count = (1 if self.local_reference_image_path else 0) + len(chat_refs)
        if total_count > 1:
            summary_lines.insert(0, f"当前共 {total_count} 张参考图")

        self.reference_image_label.setText("\n".join(summary_lines))
        self.clear_reference_image_button.setEnabled(True)

    def set_reference_preview_from_item(self, item: dict, pixmap: QPixmap | None = None):
        kind = str(item.get("kind") or "")
        if kind == "local":
            file_path = str(item.get("path") or "")
            if file_path:
                self.update_reference_image_preview(file_path)
                return

        if pixmap is None or pixmap.isNull():
            data_url = str(item.get("data_url") or "")
            pixmap = self.data_url_to_pixmap(data_url)

        self.update_reference_image_preview_from_pixmap(pixmap if pixmap is not None else QPixmap())

    def set_local_reference_image(self, file_path: str):
        self.local_reference_image_path = file_path
        self.set_reference_preview_from_item({
            "kind": "local",
            "path": file_path,
        })
        self.rebuild_reference_image_summary()

    def add_chat_reference_image(self, data_url: str, pixmap: QPixmap, label: str = "聊天历史图片"):
        if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
            QMessageBox.information(self, "无法设为参考图", "这张聊天图片缺少可重发的原始数据。")
            return

        item = {
            "kind": "chat",
            "data_url": data_url,
            "label": label,
        }
        item_key = self.reference_image_key(item)

        for existing in self.selected_reference_images:
            if self.reference_image_key(existing) == item_key:
                self.set_reference_preview_from_item(existing, pixmap)
                self.rebuild_reference_image_summary()
                self.append_system_message("这张聊天图片已经在参考图列表中，已切换到它的预览。")
                return

        self.selected_reference_images.append(item)
        self.set_reference_preview_from_item(item, pixmap)
        self.rebuild_reference_image_summary()
        self.append_system_message("已将这张历史图片加入参考图列表；后续图片编辑请求会重新发送它。")

    def select_reference_image(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择参考图片",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*)"
        )

        if not file_path:
            return

        self.set_local_reference_image(file_path)

        # 参考图是用户主动选择的本地文件，不复制、不缓存、不另存，避免额外残留。
        self.append_system_message(f"已选择参考图：{os.path.basename(file_path)}")

    def set_reference_image_from_chat(self, data_url: str, pixmap: QPixmap):
        self.add_chat_reference_image(data_url, pixmap)

    def clear_reference_image(self):
        self.local_reference_image_path = ""
        self.selected_reference_images = []
        self.rebuild_reference_image_summary()

    def set_document_advanced_visible(self, visible: bool):
        """
        以弹出窗口形式显示当前消息的文件选项。

        The popup creates its controls on demand. Values are stored on the chat
        window so closing the popup does not discard the selected options.
        """
        if not hasattr(self, "document_advanced_toggle_button"):
            return

        button = self.document_advanced_toggle_button

        def sync_button_state(opened: bool):
            """同步按钮勾选状态和箭头方向，避免信号递归。"""
            try:
                button.blockSignals(True)
                button.setChecked(opened)
                button.blockSignals(False)
                # The text arrow keeps this QPushButton aligned without a separate
                # QToolButton arrow area.
                button.setText("▾ 本次发送的文件选项" if opened else "▸ 本次发送的文件选项")
            except RuntimeError:
                # 主窗口或按钮正在销毁时，Qt 底层对象可能已经失效。
                # 此时无需再同步按钮状态，直接忽略即可。
                pass

        if not visible:
            popup = getattr(self, "document_advanced_popup_dialog", None)
            if popup is not None:
                try:
                    popup.close()
                except Exception:
                    pass
            sync_button_state(False)
            return

        existing_popup = getattr(self, "document_advanced_popup_dialog", None)
        if existing_popup is not None and existing_popup.isVisible():
            existing_popup.raise_()
            existing_popup.activateWindow()
            sync_button_state(True)
            return

        popup = QDialog(self)
        popup.setWindowTitle("本次发送的文件选项")
        popup.setModal(False)
        popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        popup.setStyleSheet(self.styleSheet())

        popup_layout = QVBoxLayout(popup)
        popup_layout.setContentsMargins(12, 12, 12, 12)
        popup_layout.setSpacing(10)

        title_label = QLabel("本次发送的文件选项")
        title_label.setStyleSheet("font-size: 15px; font-weight: bold; color: #212121;")
        popup_layout.addWidget(title_label)

        option_row = QHBoxLayout()
        option_row.setContentsMargins(0, 0, 0, 0)
        option_row.setSpacing(8)

        option_row.addWidget(QLabel("发送方式:"))

        send_mode_combo = QComboBox()
        make_combo_popup_on_click(send_mode_combo)
        send_mode_combo.addItem("全文带图", "full_with_images")
        send_mode_combo.addItem("全文无图", "full_no_images")

        current_mode = getattr(self, "_document_send_mode_value", "full_with_images")
        current_index = send_mode_combo.findData(current_mode)
        send_mode_combo.setCurrentIndex(current_index if current_index >= 0 else 0)
        option_row.addWidget(send_mode_combo)

        compress_checkbox = QCheckBox("压缩图片")
        compress_checkbox.setChecked(bool(getattr(self, "_document_compress_images_value", True)))
        option_row.addWidget(compress_checkbox)

        sequential_checkbox = QCheckBox("顺序读图")
        sequential_checkbox.setChecked(bool(getattr(self, "_document_sequential_images_value", True)))
        option_row.addWidget(sequential_checkbox)

        option_row.addStretch(1)
        popup_layout.addLayout(option_row)

        def apply_popup_values():
            """Store popup selections on the chat window."""
            self._document_send_mode_value = send_mode_combo.currentData() or "full_with_images"
            self._document_compress_images_value = compress_checkbox.isChecked()
            self._document_sequential_images_value = sequential_checkbox.isChecked()

            send_images = self._document_send_mode_value == "full_with_images"
            compress_checkbox.setEnabled(send_images)
            sequential_checkbox.setEnabled(send_images)

        send_mode_combo.currentIndexChanged.connect(lambda _=None: apply_popup_values())
        compress_checkbox.toggled.connect(lambda _=None: apply_popup_values())
        sequential_checkbox.toggled.connect(lambda _=None: apply_popup_values())
        apply_popup_values()

        close_button_layout = QHBoxLayout()
        close_button_layout.addStretch(1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(popup.close)
        close_button_layout.addWidget(close_button)
        popup_layout.addLayout(close_button_layout)

        def on_popup_destroyed():
            """弹窗销毁后恢复按钮状态，并清空引用。"""
            try:
                self.document_advanced_popup_dialog = None
            except RuntimeError:
                pass
            sync_button_state(False)

        popup.destroyed.connect(lambda _=None: on_popup_destroyed())

        self.document_advanced_popup_dialog = popup
        sync_button_state(True)

        global_pos = button.mapToGlobal(QPoint(0, button.height() + 6))
        popup.move(global_pos)
        popup.resize(430, popup.sizeHint().height())
        popup.show()
        popup.raise_()
        popup.activateWindow()

    def current_document_image_mode(self) -> str:
        """文档图片发送总开关：全文带图 / 全文无图。"""
        # Read the stored values because popup controls are transient.
        value = getattr(self, "_document_send_mode_value", "")
        if value:
            return value

        try:
            return self.document_send_mode_combo.currentData() or "full_with_images"
        except RuntimeError:
            return "full_with_images"

    def current_document_should_compress_images(self) -> bool:
        """是否压缩文档图片。全文无图时此选项不会生效。"""
        if hasattr(self, "_document_compress_images_value"):
            return bool(self._document_compress_images_value)

        try:
            return self.document_compress_images_checkbox.isChecked()
        except RuntimeError:
            return True

    def current_document_should_read_images_in_order(self) -> bool:
        """是否按 Markdown 图片引用位置交错构造“文本 + 图片 + 文本 + 图片”。"""
        if self.current_document_image_mode() != "full_with_images":
            return False

        if hasattr(self, "_document_sequential_images_value"):
            return bool(self._document_sequential_images_value)

        try:
            return self.document_sequential_images_checkbox.isChecked()
        except RuntimeError:
            return True

    def document_send_summary(self) -> str:
        """生成文档发送方式的用户可读摘要。"""
        if self.current_document_image_mode() == "full_no_images":
            return "全文无图"

        parts = ["全文带图"]
        parts.append("压缩图片" if self.current_document_should_compress_images() else "原图")
        parts.append("顺序读图" if self.current_document_should_read_images_in_order() else "全文后附图")
        return " · ".join(parts)

    def on_document_send_mode_changed(self):
        """
        根据“全文带图 / 全文无图”切换图片相关选项可用状态。

        The image options are disabled for text-only sending while their
        selections remain available when images are enabled again.
        """
        send_images = self.current_document_image_mode() == "full_with_images"

        try:
            self.document_compress_images_checkbox.setEnabled(send_images)
            self.document_sequential_images_checkbox.setEnabled(send_images)
        except RuntimeError:
            # The popup may be closed; stored values remain valid.
            pass

    def append_image_filter_notice(
        self,
        *,
        manual_image_count: int = 0,
        manual_images_filtered: bool = False,
        document_images_filtered: bool = False,
    ):
        if not manual_images_filtered and not document_images_filtered:
            return
        if manual_image_count <= 0 and not document_images_filtered:
            return

        parts = ["当前模型已被标记为非多模态模型，本次发送已自动过滤图片。"]
        if document_images_filtered:
            parts.append("文献/附件仍发送完整原文解析文本，图片数据暂不发送；该模型标记有效期为 7 天，过期后会重新尝试带图发送。")
        if manual_images_filtered and manual_image_count > 0:
            parts.append(f"你此次粘贴的 {manual_image_count} 张图片没有发送，也不会显示在聊天记录里。")
        self.append_system_message(" ".join(parts))

    def ensure_embedded_document_attached(self) -> bool:
        """
        内嵌文献对话清空历史后，下一条消息仍应像首次对话一样携带当前文献原文解析版。
        """
        if not self.embedded:
            return True
        path = getattr(self, "current_embedded_document_path", None)
        if not isinstance(path, Path) or not path.exists():
            return False
        if self.document_contexts or self.is_document_already_sent_in_current_conversation(path):
            return True
        self.attach_markdown_document(path)
        return self.is_document_pending(path)

    def verify_embedded_document_before_send(self) -> bool:
        """Last line of defence: an embedded request may not leave without its paper."""
        if not self.embedded:
            return True
        # 图片生成/编辑接口只接受一条 prompt（及可选参考图），不应把当前论文
        # 拼进 prompt。待发送论文保留在界面中，用户切回文本模型后仍可发送。
        config = self.get_config()
        if config is not None and is_probably_image_model(config.model):
            return True
        path = getattr(self, "current_embedded_document_path", None)
        if not isinstance(path, Path) or not path.exists():
            QMessageBox.warning(self, "文献上下文不可用", "当前没有可用的论文原文，已阻止发送。请重新选择文献后再试。")
            return False
        if self.is_document_already_sent_in_current_conversation(path):
            return True
        if not self.ensure_embedded_document_attached() or not self.is_document_pending(path):
            QMessageBox.warning(self, "文献上下文不可用", "未能将当前论文加入消息上下文，已阻止发送。")
            return False
        return True

    def configure_mineru_api_key(self):
        adapter = self.current_document_tool_adapter()
        if not adapter or not adapter.save_key:
            QMessageBox.information(self, "未配置文档工具", "当前未接入可配置的文档解析工具。")
            return
        existing_token = load_mineru_token()
        token, ok = QInputDialog.getText(
            self,
            adapter.settings_button_text or f"设置 {adapter.display_name} 访问令牌",
            adapter.token_placeholder or f"请输入 {adapter.display_name} 访问令牌：",
            QLineEdit.EchoMode.Password,
            existing_token,
        )
        if not ok:
            return
        token = token.strip().removeprefix("Bearer ").strip()
        if not token:
            QMessageBox.information(self, "未保存", f"{adapter.display_name} 访问令牌不能为空。")
            return
        if not token.isascii() or any(ch.isspace() for ch in token):
            QMessageBox.warning(self, "格式不正确", f"{adapter.display_name} 访问令牌只能包含无空格的 ASCII 字符。")
            return
        try:
            saved_path = adapter.save_key(token)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        warn_chat_secret_session_only(self, "mineru", "api_key")
        if not app_config.secret_is_session_only("mineru", "api_key"):
            QMessageBox.information(self, "已保存", f"{adapter.display_name} 访问令牌已保存到用户配置目录：\n{saved_path or app_config.APP_DIR}")

    def normalize_document_path_text(self, value: str) -> str:
        """
        将文档路径规范化为可比较字符串。

        用途：
        1. 判断本轮待发送文件是否重复。
        2. 判断当前对话 messages 中是否已经包含过同一份文档。
        """
        text = str(value or "").strip()

        if not text:
            return ""

        try:
            return str(Path(text).expanduser().resolve())
        except Exception:
            return text

    def extract_document_source_paths_from_message_text(self, text: str) -> list[Path]:
        """
        从历史消息文本中提取文档来源路径。

        文档上下文会写入固定格式：
        来源: <markdown_path>
        """
        paths: list[Path] = []
        seen: set[str] = set()

        for match in re.finditer(r"^来源:\s*(.+?)\s*$", str(text or ""), flags=re.MULTILINE):
            normalized = self.normalize_document_path_text(match.group(1))
            if not normalized or normalized in seen:
                continue

            seen.add(normalized)
            try:
                paths.append(Path(normalized))
            except Exception:
                continue

        return paths

    def delete_document_parse_output(self, markdown_path: Path):
        """
        删除某个已解析文档对应的输出目录。

        只处理解析产物目录，不碰仓库源码。
        """
        try:
            folder = markdown_path.parent
            if not folder.exists():
                return

            markers = [
                folder / "full.cleaned.md",
                folder / "full.md",
                folder / "image_map.json",
                folder / "mineru_task.json",
            ]

            if not any(path.exists() for path in markers):
                return

            shutil.rmtree(folder)
        except Exception:
            pass

    def referenced_document_parse_folders(self) -> set[str]:
        """
        收集仍被当前历史记录引用的文档解析目录。

        用于安全清理：只有未被任何历史消息引用的“不完整解析目录”才允许自动删除。
        """
        folders: set[str] = set()

        def add_from_text(text: str):
            for markdown_path in self.extract_document_source_paths_from_message_text(text):
                try:
                    folders.add(str(markdown_path.parent.resolve()))
                except Exception:
                    pass

        for record in getattr(self, "conversation_sessions", []) or []:
            for message in record.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                add_from_text(self.message_content_to_full_text(message.get("content", "")))

        for message in getattr(self, "messages", []) or []:
            if isinstance(message, dict):
                add_from_text(self.message_content_to_full_text(message.get("content", "")))

        for item in getattr(self, "document_contexts", []) or []:
            path = item.get("path") if isinstance(item, dict) else None
            if isinstance(path, Path):
                try:
                    folders.add(str(path.parent.resolve()))
                except Exception:
                    pass

        return folders

    def cleanup_stale_parse_outputs(self):
        """
        清理解析残留垃圾。

        安全策略：
        1. 已成功解析的目录只删除 mineru_result.zip，因为 zip 解压后不再需要。
        2. 不完整目录必须满足“未被历史记录引用 + 超过 24 小时”才删除，避免误删正在解析或刚失败的目录。
        3. settings、secrets、chat_conversations.json 属于用户配置/历史，不视为垃圾，不自动删除。
        """
        # Limit cleanup to parser outputs in the active work folder.
        workspace = work_dir_path(self.settings) / "parsed_documents"

        if not workspace.exists():
            return

        referenced_folders = self.referenced_document_parse_folders()
        now = time.time()

        for folder in workspace.iterdir():
            if not folder.is_dir():
                continue

            try:
                resolved_folder = str(folder.resolve())
            except Exception:
                resolved_folder = str(folder)

            # 解析成功后遗留的 zip 可以无条件删除；解析结果已在 mineru_result/full.md 等文件中。
            zip_path = folder / "mineru_result.zip"
            try:
                if zip_path.exists():
                    zip_path.unlink()
            except Exception:
                pass

            has_marker = any(
                (folder / name).exists()
                for name in ("full.cleaned.md", "full.md", "image_map.json", "mineru_task.json")
            )
            is_complete = (folder / "full.cleaned.md").exists()

            if is_complete or not has_marker or resolved_folder in referenced_folders:
                continue

            try:
                folder_mtime = folder.stat().st_mtime
            except Exception:
                folder_mtime = now

            # 仅清理超过 24 小时仍没有 full.cleaned.md 的半成品目录。
            if now - folder_mtime > 24 * 3600:
                try:
                    shutil.rmtree(folder)
                except Exception:
                    pass

    def current_conversation_document_source_paths(self) -> set[str]:
        """
        从当前对话 messages 中提取已经发送过的文档来源路径。

        build_document_context_for_message() 写入文档时会包含：
        来源: <markdown_path>
        因此这里扫描 messages，可以阻止同一对话中重复添加同一文档。
        """
        source_paths: set[str] = set()

        for message in self.messages:
            text = ChatWorker.message_content_to_text(message.get("content", ""))

            for match in re.finditer(r"^来源:\s*(.+?)\s*$", text, flags=re.MULTILINE):
                normalized = self.normalize_document_path_text(match.group(1))
                if normalized:
                    source_paths.add(normalized)

        return source_paths

    def is_document_pending(self, path: Path) -> bool:
        """判断文档是否已经在本轮待发送列表中。"""
        normalized = self.normalize_document_path_text(str(path))

        for item in self.document_contexts:
            item_path = self.normalize_document_path_text(str(item.get("path", "")))
            if item_path and item_path == normalized:
                return True

        return False

    def is_document_already_sent_in_current_conversation(self, path: Path) -> bool:
        """判断文档是否已经作为上下文发送进当前对话。"""
        normalized = self.normalize_document_path_text(str(path))
        return normalized in self.current_conversation_document_source_paths()

    def is_document_parse_in_progress(self) -> bool:
        worker = self.document_parse_worker
        return bool(worker and worker.isRunning())

    def set_document_parse_progress(self, text: str, percent: int = -1):
        self.document_parse_status_text = str(text or "").strip()
        if isinstance(percent, (int, float)) and percent >= 0:
            self.document_parse_progress_percent = max(0, min(100, int(percent)))
        else:
            self.document_parse_progress_percent = -1
        if hasattr(self, "send_button"):
            self.send_button.setEnabled(not self.is_document_parse_in_progress())
        self.refresh_document_status()

    def reset_document_parse_progress(self):
        self.document_parse_status_text = ""
        self.document_parse_progress_percent = -1
        if hasattr(self, "send_button") and not (self.chat_worker and self.chat_worker.isRunning()):
            self.send_button.setEnabled(True)
        self.refresh_document_status()

    def handle_document_parse_log(self, text: str):
        log_text = str(text or "").strip()
        if not log_text:
            return
        percent = -1
        status_text = log_text
        page_match = re.search(r"解析中:\s*(\d+)\s*/\s*(\d+)\s*页", log_text)
        if page_match:
            current_page = int(page_match.group(1))
            total_page = max(1, int(page_match.group(2)))
            percent = min(95, 25 + int(current_page * 65 / total_page))
            status_text = f"正在解析文档：{current_page}/{total_page} 页"
        elif "准备解析" in log_text:
            percent = 5
            status_text = "正在准备解析文档"
        elif "batch_id" in log_text:
            percent = 15
            status_text = "已创建解析任务"
        elif "上传文件" in log_text:
            percent = 25
            status_text = "正在上传文件到 MinerU"
        elif "等待 MinerU 返回结果" in log_text:
            percent = 55
            status_text = "正在等待 MinerU 返回解析结果"
        elif "精准解析状态" in log_text:
            percent = 60
            status_text = "MinerU 正在解析文档内容"
        elif "精准解析完成" in log_text:
            percent = 95
            status_text = "解析完成，正在整理结果"
        elif "下载结果压缩包" in log_text:
            percent = 98
            status_text = "正在下载解析结果"
        self.set_document_parse_progress(status_text, percent)
    def add_document_file(self):
        """
        添加本轮待发送文件。

        Files are queued for the next message and cleared after sending.
        """
        if self.chat_worker and self.chat_worker.isRunning():
            QMessageBox.information(self, "正在回复", "请先停止或等待当前回复完成，再添加文件。")
            return

        if not self.ensure_current_conversation_named():
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "添加文献文件",
            "",
            "文献与解析结果 (*.md *.markdown *.epub *.pdf *.png *.jpg *.jpeg *.jp2 *.webp *.gif *.bmp *.doc *.docx *.ppt *.pptx *.xls *.xlsx *.html *.htm);;"
            "All Files (*)",
        )
        if not file_path:
            return
        path = Path(file_path)

        # Parsed Markdown must use the document attachment path so its image map is preserved.
        if path.suffix.lower() in {".md", ".markdown"}:
            self.attach_markdown_document(path)
            return

        adapter = self.current_document_tool_adapter()
        if not adapter or not adapter.is_supported_input_file or not adapter.create_output_dir or not adapter.create_parse_worker:
            QMessageBox.information(self, "未配置文档解析", "当前独立对话程序未接入文档解析器，只能直接添加 Markdown 文件。")
            return

        if not adapter.is_supported_input_file(path):
            unsupported_message = adapter.unsupported_file_message or f"{self.document_tool_name()} 暂不支持此文件类型"
            QMessageBox.information(self, "不支持的文件", f"{unsupported_message}: {path.suffix}")
            return
        if self.is_document_parse_in_progress():
            QMessageBox.information(self, "正在解析", "请等待当前文档解析完成。")
            return

        if path.suffix.lower() != ".epub" and not self.document_tool_key_available():
            self.configure_mineru_api_key()
            if not self.document_tool_key_available():
                QMessageBox.information(self, "需要文档工具访问令牌", f"请先设置 {self.document_tool_name()} 访问令牌后再添加需要解析的文件。")
                return

        output_dir = adapter.create_output_dir(path)
        self.document_parse_worker = adapter.create_parse_worker(path, output_dir)
        self.pending_document_parse_output_dir = output_dir
        self.pending_document_parse_source_path = path
        self.pending_document_parse_cancel_requested = False

        # Show only the final parser result in the conversation.
        # 解析细节仅输出到控制台，聊天区只保留“正在解析 / 添加成功 / 添加失败”三类用户关心的状态。
        self.document_parse_worker.log_signal.connect(self.handle_document_parse_log)

        self.document_parse_worker.finished_signal.connect(self.finish_document_parse)
        self.add_document_button.setEnabled(False)
        self.set_document_parse_progress(f"正在解析文档：{path.name}", 0)

        # 用户侧只显示一个简洁状态。
        self.append_system_message(f"{self.document_tool_name()} 正在解析文档: {path.name}")
        self.document_parse_worker.start()

    def finish_document_parse(self, success: bool, message: str, markdown_path: str):
        self.add_document_button.setEnabled(True)
        self.reset_document_parse_progress()

        if self.pending_document_parse_cancel_requested or message == "已取消解析":
            if markdown_path:
                self.delete_document_parse_output(Path(markdown_path))
            elif self.pending_document_parse_output_dir:
                try:
                    shutil.rmtree(self.pending_document_parse_output_dir)
                except Exception:
                    pass
            self.document_parse_worker = None
            self.pending_document_parse_output_dir = None
            self.pending_document_parse_source_path = None
            self.pending_document_parse_cancel_requested = False
            self.append_system_message("已取消本轮文件解析，并清理解析产物。")
            return

        if not success:
            # Keep parser internals out of the conversation and show the final failure reason.
            QMessageBox.critical(self, "文档添加失败", message)
            self.append_system_message(f"文档添加失败: {message}")
            self.document_parse_worker = None
            self.pending_document_parse_output_dir = None
            self.pending_document_parse_source_path = None
            self.pending_document_parse_cancel_requested = False
            return

        path = Path(markdown_path)
        self.document_parse_worker = None
        self.pending_document_parse_output_dir = None
        self.pending_document_parse_source_path = None
        self.pending_document_parse_cancel_requested = False
        self.attach_markdown_document(path)

    def read_text_document_file(self, path: Path) -> str:
        """
        读取纯文本/代码文件。

        优先 UTF-8；失败时回退到常见中文 Windows 编码。
        只做文本读取，不做 MinerU 解析。
        """
        encodings = ("utf-8", "utf-8-sig", "gbk", "gb18030")

        last_error = None
        for encoding in encodings:
            try:
                return path.read_text(encoding=encoding)
            except UnicodeDecodeError as exc:
                last_error = exc
                continue
            except Exception as exc:
                last_error = exc
                break

        # 最后使用替换策略兜底，避免因为少量非法字符导致整个文件无法发送。
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            raise RuntimeError(f"读取文本文件失败：{last_error or exc}") from exc

    def attach_text_document(self, path: Path):
        """
        添加纯文本/代码文件为本轮待发送文档。

        与 MinerU 解析结果一样写入 document_contexts，
        但 image_map 为空，发送时只包含原文。
        """
        if not path.exists():
            return

        path = path.resolve()

        if self.is_document_pending(path):
            QMessageBox.information(
                self,
                "文件已添加",
                f"文件“{path.name}”已经在待发送列表中，不会重复添加。",
            )
            return

        if self.is_document_already_sent_in_current_conversation(path):
            QMessageBox.information(
                self,
                "文件已在当前对话中",
                f"文件“{path.name}”已经发送进当前对话上下文，不会再次重复加入。\n\n"
                "你可以直接继续追问；如果确实要重新发送，请新建一个对话。",
            )
            return

        try:
            text = self.read_text_document_file(path)
        except Exception as exc:
            QMessageBox.warning(self, "读取失败", f"无法读取文件“{path.name}”：\n{exc}")
            return

        # 给代码/文本文件加一个轻量文件头，便于模型识别文件名和类型。
        markdown = (
            f"```text\n"
            f"文件名: {path.name}\n"
            f"文件路径: {path}\n"
            f"文件类型: {path.suffix or '无扩展名'}\n"
            f"```\n\n"
            f"{text}"
        )

        self.document_contexts.append(
            {
                "path": path,
                "title": path.name,
                "markdown": markdown,
                "image_map": {},
                "direct_text": True,
            }
        )

        self.document_context_sent = False
        self.refresh_document_status()
        self.append_system_message(f"已添加文本文件：{path.name}。发送方式：直接发送原文。")

    def attach_markdown_document(self, path: Path):
        if not path.exists():
            return

        path = path.resolve()

        if self.is_document_pending(path):
            QMessageBox.information(
                self,
                "文件已添加",
                f"文件“{path.name}”已经在待发送列表中，不会重复添加。",
            )
            return

        if self.is_document_already_sent_in_current_conversation(path):
            QMessageBox.information(
                self,
                "文件已在当前对话中",
                f"文件“{path.name}”已经发送进当前对话上下文，不会再次重复加入。\n\n"
                "你可以直接继续追问；如果确实要重新发送，请新建一个对话。",
            )
            return

        markdown = path.read_text(encoding="utf-8", errors="replace")
        self.document_contexts.append(
            {
                "path": path,
                "title": path.parent.name if path.name in {"full.cleaned.md", "full.zh.md"} else path.stem,
                "markdown": markdown,
                "image_map": self.load_document_image_map(path.parent),
            }
        )

        # document_contexts contains only files queued for the current message.
        self.document_context_sent = False
        self.refresh_document_status()

        # 用户侧只提示文档最终添加成功；不展示解析过程中的中间状态。
        self.append_system_message(f"已添加文件：{path.name}。发送方式：{self.document_send_summary()}。")

    def clear_documents(self, show_message: bool = True, delete_parse_outputs: bool = False):
        """
        清空本轮待发送文件。

        注意：
        1. 这里只清空“本轮待发送附件列表”，不删除解析结果目录。
        2. Parsed documents are managed by the main workspace; chat only removes
           the current attachment reference.
        3. 只有正在解析且用户主动取消解析时，才由 finish_document_parse() 清理半成品目录。
        """
        cleanup_paths: list[Path] = []
        if delete_parse_outputs:
            cleanup_paths.extend(
                item["path"]
                for item in self.document_contexts
                if isinstance(item, dict) and isinstance(item.get("path"), Path)
            )

            if self.document_parse_worker and self.document_parse_worker.isRunning():
                self.pending_document_parse_cancel_requested = True
                try:
                    self.document_parse_worker.request_stop()
                except Exception:
                    pass

            if self.pending_document_parse_output_dir is not None:
                cleanup_paths.append(Path(self.pending_document_parse_output_dir))

        self.document_contexts = []
        self.document_context_sent = False
        self.refresh_document_status()

        if delete_parse_outputs:
            for path in cleanup_paths:
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        self.delete_document_parse_output(path)
                except Exception:
                    pass

        if show_message:
            self.append_system_message("已移除待发送文件。")

    def refresh_document_status(self):
        """刷新输入区旁边的本轮文件状态。"""
        if not hasattr(self, "document_status_label"):
            return

        if self.is_document_parse_in_progress():
            progress_text = self.document_parse_status_text or "正在解析文档"
            if self.document_parse_progress_percent >= 0:
                progress_text = f"{progress_text}（{self.document_parse_progress_percent}%）"
            self.document_status_label.setText(progress_text)
            if hasattr(self, "clear_documents_button"):
                self.clear_documents_button.setEnabled(True)
            return

        if not self.document_contexts:
            self.document_status_label.setText("未添加待发送文件")

            if hasattr(self, "clear_documents_button"):
                self.clear_documents_button.setEnabled(False)

            return

        titles = "；".join(str(item["title"]) for item in self.document_contexts)
        model = self.model_combo.currentText().strip() if hasattr(self, "model_combo") else ""
        if is_probably_image_model(model):
            self.document_status_label.setText(
                f"已暂存 {len(self.document_contexts)} 个文件（图片模型本轮不会发送，切回文本模型后可用）：{titles}"
            )
        else:
            self.document_status_label.setText(f"此次发送将包含 {len(self.document_contexts)} 个文件：{titles}")

        if hasattr(self, "clear_documents_button"):
            self.clear_documents_button.setEnabled(True)

    def set_pending_reference_quote(self, quote: dict | None):
        """Replace the pending reference list (legacy single-quote API)."""
        self.pending_reference_quotes = [dict(quote)] if isinstance(quote, dict) else []
        self.refresh_pending_reference_quotes()

    @staticmethod
    def reference_quote_identity(quote: dict) -> tuple:
        return (
            str(quote.get("type") or "text"),
            str(quote.get("markdown_path") or quote.get("path") or ""),
            str(quote.get("page") or ""),
            str(quote.get("formula_tex") or quote.get("text") or "").strip(),
        )

    def append_pending_reference_quote(self, quote: dict | None):
        """Append a formula/text quote while preserving earlier references."""
        if not isinstance(quote, dict):
            return
        next_quote = dict(quote)
        identity = self.reference_quote_identity(next_quote)
        if any(self.reference_quote_identity(item) == identity for item in self.pending_reference_quotes):
            return
        self.pending_reference_quotes.append(next_quote)
        self.refresh_pending_reference_quotes()

    @staticmethod
    def individual_reference_quotes(quote: dict | None) -> list[dict]:
        """Keep every sent reference independently clickable and focusable."""
        if not isinstance(quote, dict):
            return []
        nested = quote.get("quotes")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        return [quote]

    def combined_pending_reference_quote(self) -> dict | None:
        quotes = [dict(item) for item in self.pending_reference_quotes if isinstance(item, dict)]
        if not quotes:
            return None
        if len(quotes) == 1:
            return quotes[0]
        first = quotes[0]
        sections = []
        for index, quote in enumerate(quotes, start=1):
            kind = "公式" if quote.get("type") == "formula" else "引用"
            sections.append(f"[{kind} {index}]\n{str(quote.get('text') or '').strip()}")
        return {
            "type": "reference_collection",
            "text": "\n\n".join(sections),
            "quotes": quotes,
            "markdown_path": first.get("markdown_path") or first.get("path") or "",
            "title": first.get("title") or "",
        }

    def refresh_pending_reference_quotes(self):
        self.pending_reference_quote = self.combined_pending_reference_quote()

        if not hasattr(self, "input_reference_quote_layout"):
            return

        while self.input_reference_quote_layout.count() > 1:
            item = self.input_reference_quote_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if not self.pending_reference_quotes:
            self.input_reference_quote_row.setVisible(False)
            return

        for quote in self.pending_reference_quotes:
            label = ReferenceQuoteLabel(
                quote,
                self.open_reference_quote_reader,
                clear_callback=lambda target=quote: self.remove_pending_reference_quote(target),
            )
            label.setMaximumWidth(340)
            self.input_reference_quote_layout.insertWidget(
                self.input_reference_quote_layout.count() - 1,
                label,
                0,
                Qt.AlignmentFlag.AlignRight,
            )
        self.input_reference_quote_row.setVisible(True)

    def remove_pending_reference_quote(self, quote: dict):
        identity = self.reference_quote_identity(quote)
        self.pending_reference_quotes = [
            item for item in self.pending_reference_quotes
            if self.reference_quote_identity(item) != identity
        ]
        self.refresh_pending_reference_quotes()

    def clear_pending_reference_quote(self):
        """清空输入框中的全部引用气泡。"""
        self.pending_reference_quotes = []
        self.refresh_pending_reference_quotes()

    def open_reference_quote_reader(self, quote: dict):
        """优先在宿主阅读器定位引用；无宿主时再打开独立阅读窗口。"""
        nested_quotes = quote.get("quotes") if isinstance(quote, dict) else None
        if isinstance(nested_quotes, list) and nested_quotes and isinstance(nested_quotes[0], dict):
            quote = nested_quotes[0]

        reveal_callback = getattr(self, "reference_quote_reveal_callback", None)
        if callable(reveal_callback):
            try:
                if reveal_callback(quote):
                    return
            except Exception:
                # 定位只是增强交互；宿主阅读器状态异常时继续使用原有安全兜底。
                pass

        # New references retain a stable parsed-source path.  Older histories
        # may only have a translation path, which remains the compatibility fallback.
        path_text = str(
            quote.get("document_path")
            or quote.get("source_markdown_path")
            or quote.get("markdown_path")
            or quote.get("path")
            or ""
        ).strip()
        quote_text = str(quote.get("text") or "").strip()

        if not path_text:
            return

        path = Path(path_text)
        if not path.exists():
            QMessageBox.information(self, "无法打开引用", "引用对应的文档文件不存在。")
            return

        adapter = self.current_document_tool_adapter()
        translation_path = None
        original_path = None

        try:
            if adapter and adapter.latest_translation_path:
                translation_path = adapter.latest_translation_path(path.parent)
        except Exception:
            translation_path = None

        try:
            if adapter and adapter.find_stored_original:
                original_path = adapter.find_stored_original(path.parent)
        except Exception:
            original_path = None

        reader = self.create_document_reader_window(
            source_path=path,
            translation_path=translation_path,
            original_path=original_path,
            adapter=adapter,
        )
        if reader is None:
            return

        reader.destroyed.connect(lambda _=None, window=reader: self.remove_document_reader_window(window))
        self.document_reader_windows.append(reader)
        reader.show()
        reader.raise_()
        reader.activateWindow()

        # ReaderWindow / StandaloneDocumentReaderWindow 若实现 reveal_text，则定位引用；
        # 没有该方法时只打开文档，不影响基本功能。
        if quote_text and hasattr(reader, "reveal_text"):
            QTimer.singleShot(300, lambda: reader.reveal_text(quote_text))

    def insert_text_at_input_cursor(self, text: str):
        """将文本直接插入到当前聊天输入框的光标位置。"""
        if not text:
            return

        cursor = self.input_box.textCursor()
        cursor.insertText(text)
        self.input_box.setTextCursor(cursor)
        self.input_box.setFocus()

    def insert_text_on_new_line_at_input_cursor(self, text: str):
        """
        将文本另起一行插入到当前聊天输入框。

        规则：
        1. 输入框为空时，直接插入文本，不额外补前导换行。
        2. 如果当前光标前面不是换行，则先补一个换行，再插入文本。
        3. 如果光标已经位于新行开头，则直接插入，避免产生多余空行。
        """
        if not text:
            return

        cursor = self.input_box.textCursor()
        full_text = self.input_box.toPlainText()

        if not full_text:
            cursor.insertText(text)
            self.input_box.setTextCursor(cursor)
            self.input_box.setFocus()
            return

        insert_prefix = ""

        # 光标不在开头，且前一个字符不是换行时，先补一个换行。
        if cursor.position() > 0:
            previous_char = full_text[cursor.position() - 1]
            if previous_char != "\n":
                insert_prefix = "\n"
        # 光标在开头时不补换行，直接插入。
        cursor.insertText(insert_prefix + text)
        self.input_box.setTextCursor(cursor)
        self.input_box.setFocus()

    def insert_builtin_prompt_to_input(self):
        """把内置指引插入到当前输入框光标位置。"""
        prompt_path = Path(get_base_path()) / "resources" / "Prompt.txt"

        try:
            if prompt_path.exists():
                prompt_text = prompt_path.read_text(encoding="utf-8", errors="replace")
            else:
                # 兜底文本：避免资源文件缺失时按钮失效。
                prompt_text = "请基于我提供的内容，结合上下文进行分析，并给出可执行的结论。"

            if not prompt_text.strip():
                QMessageBox.information(self, "指引为空", "内置指引文件为空，未插入任何内容。")
                return

            # Insert the built-in prompt on a new line at the current cursor.
            self.insert_text_on_new_line_at_input_cursor(prompt_text)

        except Exception as e:
            QMessageBox.warning(self, "读取失败", f"读取内置指引时发生错误：\n{e}")

    def _load_current_code_file_paths(self) -> list[str]:
        """从代码编辑器的历史设置中读取最近一次选择的代码文件列表。"""
        settings = QSettings("MyCompany", "CodeSearchReplaceTool")
        saved_files_json = settings.value("last_selected_py_files", "")

        if not saved_files_json:
            return []

        try:
            saved_files = json.loads(saved_files_json)
        except Exception:
            return []

        if not isinstance(saved_files, list):
            return []

        # 只保留当前仍然存在的文件，避免插入已删除路径。
        valid_files = []
        for file_path in saved_files:
            if isinstance(file_path, str) and os.path.exists(file_path):
                valid_files.append(file_path)

        return valid_files

    def insert_current_code_to_input(self):
        """将当前载入的代码文件最新内容插入到输入框光标位置。"""
        file_paths = self._load_current_code_file_paths()

        if not file_paths:
            QMessageBox.information(
                self,
                "当前功能不可用",
                "该功能不适用于文献对话。"
            )
            return

        inserted_blocks = []
        read_errors = []

        for file_path in file_paths:
            try:
                # 必须按点击时从磁盘重新读取，确保拿到最新内容，而不是旧缓存。
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except UnicodeDecodeError:
                try:
                    with open(file_path, "r", encoding="gbk", errors="replace") as f:
                        content = f.read()
                except Exception as e:
                    read_errors.append(f"{os.path.basename(file_path)}: {e}")
                    continue
            except Exception as e:
                read_errors.append(f"{os.path.basename(file_path)}: {e}")
                continue

            inserted_blocks.append(
                f"文件{os.path.basename(file_path)}的当前内容：\n{content}"
            )

        if not inserted_blocks:
            QMessageBox.warning(self, "读取失败", "未能读取任何代码文件的最新内容。")
            return

        # Insert current content on a new line.
        self.insert_text_on_new_line_at_input_cursor("\n\n".join(inserted_blocks))

        if read_errors:
            QMessageBox.warning(
                self,
                "部分文件读取失败",
                "以下文件读取失败，但其余文件已插入：\n" + "\n".join(read_errors)
            )

    @staticmethod
    def load_document_image_map(folder: Path) -> dict[str, Path]:
        image_map_path = folder / "image_map.json"
        image_map: dict[str, Path] = {}
        if not image_map_path.exists():
            return image_map
        try:
            records = json.loads(image_map_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return image_map
        if not isinstance(records, list):
            return image_map
        for record in records:
            if not isinstance(record, dict):
                continue
            image_id = str(record.get("id") or "").strip()
            clean_target = str(record.get("clean_target") or "").strip()
            saved_file = str(record.get("saved_file") or "").strip()
            image_path = Path(saved_file) if saved_file else folder / clean_target
            if image_id and image_path.exists():
                image_map[image_id] = image_path
        return image_map

    @staticmethod
    def markdown_image_references(markdown: str) -> list[tuple[str, str]]:
        pattern = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)")
        return [(match.group("alt").strip(), match.group("target").strip()) for match in pattern.finditer(markdown)]

    @staticmethod
    def markdown_image_reference_pattern() -> re.Pattern:
        """统一维护 Markdown 图片引用正则，方便顺序读图时保留图片在正文中的位置。"""
        return re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)")

    @staticmethod
    def append_text_content_part(parts: list[dict], text: str):
        """
        向 OpenAI 兼容多模态 content parts 追加文本。

        相邻文本块会合并，避免顺序读图时因为切分产生过多碎片。
        """
        if text is None or text == "":
            return

        if parts and isinstance(parts[-1], dict) and parts[-1].get("type") == "text":
            parts[-1]["text"] += text
        else:
            parts.append({
                "type": "text",
                "text": text,
            })

    def resolve_document_image_path(self, doc: dict, alt: str, target: str) -> Path | None:
        """
        根据 Markdown 图片引用定位本地图片文件。

        优先使用 MinerU 解析阶段生成的 image_map.json；
        找不到时再按 Markdown target 相对当前 Markdown 目录查找。
        """
        folder = Path(doc["path"]).parent
        image_map = doc.get("image_map") or {}

        image_path = image_map.get(alt)
        if image_path:
            image_path = Path(image_path)
            if image_path.exists():
                return image_path.resolve()

        clean_target = str(target or "").strip().strip("<>").strip()
        if not clean_target:
            return None

        candidate = Path(clean_target)
        if not candidate.is_absolute():
            candidate = folder / clean_target

        if candidate.exists():
            return candidate.resolve()

        return None

    def document_image_label_text(
        self,
        doc: dict,
        alt: str,
        target: str,
        image_path: Path,
        image_index: int,
    ) -> str:
        """
        给每张发送给模型的文档图片添加文本说明。

        这是关键：API 只会收到图片像素数据，不会天然知道文件名或 Markdown 占位符；
        因此必须把 IMAGE_001、Markdown 引用、文件名等信息显式写进文本块。
        """
        image_id = alt or f"IMAGE_{image_index:03d}"
        markdown_ref = f"![{alt}]({target})" if alt or target else image_id

        return (
            f"下面是文档《{doc.get('title', '未命名文档')}》中的图片 {image_id}。\n"
            f"Markdown 引用: {markdown_ref}\n"
            f"图片文件: {image_path.name}\n"
            "请把这张图片与正文中的对应占位符、图注和上下文一起阅读。"
        )

    def build_document_image_item(
        self,
        doc: dict,
        alt: str,
        target: str,
        compress: bool,
        image_index: int,
    ) -> dict | None:
        """构造一张文档图片的发送项。"""
        image_path = self.resolve_document_image_path(doc, alt, target)

        if not image_path:
            return None

        data_url = self.image_file_to_data_url(image_path, compress=compress)

        if not data_url:
            return None

        return {
            "data_url": data_url,
            "name": image_path.name,
            "path": image_path,
            "alt": alt,
            "target": target,
            "label": self.document_image_label_text(doc, alt, target, image_path, image_index),
        }

    def append_image_content_part(self, parts: list[dict], image_item: dict):
        """向多模态 content parts 追加“图片说明文本 + 图片数据”。"""
        data_url = image_item.get("data_url")

        if not data_url:
            return

        label = image_item.get("label") or f"下面是一张图片：{image_item.get('name', '未命名图片')}。"
        self.append_text_content_part(parts, "\n\n" + label + "\n")
        parts.append({
            "type": "image_url",
            "local_image_origin": "document",
            "image_url": {
                "url": data_url,
            },
        })

    def append_extra_user_images_to_content_parts(self, parts: list[dict], images: list[dict] | None):
        """
        追加用户在当前消息中粘贴或选择的图片。

        文档图片不在聊天气泡中逐张显示；但用户额外粘贴的图片仍按普通聊天图片处理。
        """
        images = images or []

        for index, image_item in enumerate(images, 1):
            data_url = image_item.get("data_url")

            if not data_url:
                continue

            name = image_item.get("name") or f"用户附加图片{index}"
            self.append_text_content_part(
                parts,
                f"\n\n下面是用户随本轮问题额外附加的图片 {index}，名称：{name}。\n"
            )
            parts.append({
                "type": "image_url",
                "local_image_origin": image_item.get("local_image_origin") or "manual_new",
                "image_url": {
                    "url": data_url,
                },
            })

    def document_images_for_message(self, doc: dict, compress: bool) -> list[dict]:
        """
        按 Markdown 中图片出现顺序收集文档图片。

        用于“全文带图但不顺序读图”的模式：先发全文，再按顺序附图。
        """
        items: list[dict] = []
        seen: set[Path] = set()

        for alt, target in self.markdown_image_references(doc["markdown"]):
            image_item = self.build_document_image_item(
                doc,
                alt,
                target,
                compress=compress,
                image_index=len(items) + 1,
            )

            if not image_item:
                continue

            image_path = Path(image_item["path"]).resolve()

            if image_path in seen:
                continue

            seen.add(image_path)
            items.append(image_item)

        return items

    def build_document_context_for_message(
        self,
        user_question: str,
        extra_user_images: list[dict] | None = None,
        include_images_override: bool | None = None,
    ) -> tuple[object, int]:
        """
        构造当前消息发送给模型的文档内容。

        返回：
        1. OpenAI 兼容 user.content：可以是纯文本字符串，也可以是多模态 content parts。
        2. 实际发送的文档图片数量，不包含用户额外粘贴的图片。

        设计原则：
        1. 文档正文始终完整发送，不通过裁切正文节省 token。
        2. 全文无图：只发完整 Markdown 正文。
        3. 全文带图 + 不顺序读图：先发完整 Markdown，再附“图片说明 + 图片”列表。
        4. 全文带图 + 顺序读图：按 Markdown 图片引用位置构造“文本 + 图片 + 文本 + 图片”。
        """
        if not self.document_contexts:
            return self.build_user_message_content(user_question, extra_user_images or []), 0

        include_images = self.current_document_image_mode() == "full_with_images"
        if include_images_override is not None:
            include_images = bool(include_images_override)
        compress = self.current_document_should_compress_images()
        sequential = self.current_document_should_read_images_in_order()
        user_question = user_question.strip() or "请先阅读并概括这些文档。"

        intro_lines = [
            "以下是用户添加的文档全文。请基于这些文档回答后续问题；不要裁切、摘取或忽略正文内容。",
            f"文档发送方式: {self.document_send_summary()}",
            "注意：Markdown 中的 IMAGE_001 等图片占位符会与随后的图片说明和图片数据对应。",
        ]

        # 模式一：全文无图，或全文带图但不顺序读图。
        # 这种模式先保证全文 Markdown 作为一个整体进入上下文，再按顺序附图。
        if not include_images or not sequential:
            text_parts = intro_lines.copy()
            document_image_items: list[dict] = []

            for index, doc in enumerate(self.document_contexts, 1):
                path = doc["path"]
                markdown = doc["markdown"]
                text_parts.append(f"\n\n===== 文档 {index}: {doc['title']} =====\n来源: {path}\n\n{markdown}")

                if include_images:
                    document_image_items.extend(
                        self.document_images_for_message(doc, compress=compress)
                    )

            text_parts.append(f"\n\n===== 用户问题 =====\n{user_question}")
            base_text = "\n".join(text_parts)

            if not document_image_items and not extra_user_images:
                return base_text, 0

            content_parts: list[dict] = [{
                "type": "text",
                "text": base_text,
            }]

            for image_item in document_image_items:
                self.append_image_content_part(content_parts, image_item)

            self.append_extra_user_images_to_content_parts(content_parts, extra_user_images)
            return content_parts, len(document_image_items)

        # 模式二：全文带图 + 顺序读图。
        # 只按图片引用位置切分，不按段落/句子切碎；保证正文完整，同时增强图文绑定。
        content_parts: list[dict] = []
        self.append_text_content_part(content_parts, "\n".join(intro_lines) + "\n\n")

        image_pattern = self.markdown_image_reference_pattern()
        sent_image_paths: set[Path] = set()
        sent_image_count = 0

        for doc_index, doc in enumerate(self.document_contexts, 1):
            markdown = doc["markdown"]
            path = doc["path"]
            self.append_text_content_part(
                content_parts,
                f"\n\n===== 文档 {doc_index}: {doc['title']} =====\n来源: {path}\n\n"
            )

            last_end = 0

            for match in image_pattern.finditer(markdown):
                # Add the text before each image reference first to preserve reading order.
                self.append_text_content_part(content_parts, markdown[last_end:match.start()])

                alt = match.group("alt").strip()
                target = match.group("target").strip()
                original_ref = match.group(0)

                # 保留原始 Markdown 图片引用，保证文档文本结构完整。
                self.append_text_content_part(content_parts, f"\n\n{original_ref}\n")

                image_item = self.build_document_image_item(
                    doc,
                    alt,
                    target,
                    compress=compress,
                    image_index=sent_image_count + 1,
                )

                if image_item:
                    image_path = Path(image_item["path"]).resolve()

                    if image_path in sent_image_paths:
                        self.append_text_content_part(
                            content_parts,
                            f"\n该图片文件此前已经发送过，当前为重复引用，不重复发送图片数据：{image_path.name}\n"
                        )
                    else:
                        self.append_image_content_part(content_parts, image_item)
                        sent_image_paths.add(image_path)
                        sent_image_count += 1
                else:
                    self.append_text_content_part(
                        content_parts,
                        "\n[提示：未能在本地解析输出目录中找到这张图片文件，因此本处只保留 Markdown 图片引用。]\n"
                    )

                last_end = match.end()

            # 追加最后一张图片之后的正文。
            self.append_text_content_part(content_parts, markdown[last_end:])

        self.append_text_content_part(content_parts, f"\n\n===== 用户问题 =====\n{user_question}")
        self.append_extra_user_images_to_content_parts(content_parts, extra_user_images)
        return content_parts, sent_image_count

    @staticmethod
    def image_file_to_data_url(path: Path, compress: bool = False) -> str:
        if compress:
            image = QImage(str(path))
            if image.isNull():
                return ""
            max_side = 1024
            if image.width() > max_side or image.height() > max_side:
                image = image.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            byte_array = QByteArray()
            buffer = QBuffer(byte_array)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, "JPEG", 78)
            buffer.close()
            encoded = bytes(byte_array.toBase64()).decode("ascii")
            return f"data:image/jpeg;base64,{encoded}"
        mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def qimage_to_data_url(image: QImage, mime_type: str = "image/png") -> str:
        """将 QImage 转为 OpenAI 兼容 image_url 可用的 data URL。"""
        byte_array = QByteArray()
        buffer = QBuffer(byte_array)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)

        # 统一转 PNG，避免剪贴板图片没有稳定文件格式。
        image.save(buffer, "PNG")
        buffer.close()

        encoded = bytes(byte_array.toBase64()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def add_pasted_input_image(self, image: QImage, name: str = ""):
        """接收输入框粘贴的图片，并加入待发送附件列表。"""
        if image.isNull():
            return

        data_url = self.qimage_to_data_url(image)
        pixmap = QPixmap.fromImage(image)
        resolved_name = str(name or "").strip()
        if not resolved_name or resolved_name == "粘贴图片.png":
            resolved_name = pasted_image_name(
                sequence=len(self.pending_input_images) + 1,
            )

        self.pending_input_images.append({
            "data_url": data_url,
            "pixmap": pixmap,
            "name": resolved_name,
        })

        self.refresh_input_image_previews()

    def remove_input_image(self, index: int):
        """移除一张待发送图片。"""
        if 0 <= index < len(self.pending_input_images):
            self.pending_input_images.pop(index)

        self.refresh_input_image_previews()

    def clear_pending_input_images(self):
        """清空待发送图片附件。"""
        self.pending_input_images = []
        self.refresh_input_image_previews()

    def refresh_input_image_previews(self):
        """刷新输入框下方的图片缩略图预览。"""
        while self.input_image_preview_layout.count():
            item = self.input_image_preview_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if not self.pending_input_images:
            self.input_image_preview_area.setVisible(False)
            return

        self.input_image_preview_area.setVisible(True)

        for index, image_item in enumerate(self.pending_input_images):
            card = QFrame()
            card.setFrameShape(QFrame.Shape.NoFrame)
            card.setStyleSheet("""
                QFrame {
                    background-color: #ffffff;
                    border: 1px solid #dddddd;
                    border-radius: 8px;
                }
            """)

            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(6, 6, 6, 6)
            card_layout.setSpacing(4)

            preview_label = QLabel()
            preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview_label.setFixedSize(96, 72)

            pixmap = image_item.get("pixmap", QPixmap())
            if isinstance(pixmap, QPixmap) and not pixmap.isNull():
                preview_label.setPixmap(
                    pixmap.scaled(
                        preview_label.width(),
                        preview_label.height(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )

            name_label = QLabel(image_item.get("name", "图片"))
            name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name_label.setWordWrap(True)
            name_label.setMaximumWidth(120)
            name_label.setStyleSheet("color: #555555; font-size: 12px; border: none;")

            remove_button = QToolButton()
            remove_button.setText("移除")
            remove_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            remove_button.clicked.connect(lambda checked=False, i=index: self.remove_input_image(i))

            card_layout.addWidget(preview_label)
            card_layout.addWidget(name_label)
            card_layout.addWidget(remove_button)

            self.input_image_preview_layout.addWidget(card)

        self.input_image_preview_layout.addStretch()

    def build_user_message_content(self, text: str, images: list[dict]):
        """
        构造 OpenAI 兼容多模态 user.content。

        无图片时保持原有字符串格式；
        有图片时改为 content parts：
        [
            {"type": "text", "text": "..."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        ]
        """
        if not images:
            return text

        content_parts = []

        if text:
            content_parts.append({
                "type": "text",
                "text": text,
            })

        for image_item in images:
            data_url = image_item.get("data_url")
            if not data_url:
                continue

            content_parts.append({
                "type": "image_url",
                "local_image_origin": image_item.get("local_image_origin") or "manual_new",
                "image_url": {
                    "url": data_url,
                },
            })

        return content_parts

    def fetch_models(self, checked: bool = False, *, silent: bool = False) -> bool:
        """异步获取当前对话服务商的模型列表。

        ``silent`` 用于首次打开文献对话时的后台刷新：失败只写入对话提示，
        不用弹窗打断阅读。手动点击仍保留原有提示行为。
        """
        if silent:
            api_key = self.key_input.text().strip()
            base_url = self.url_input.text().strip()
            if not api_key or not base_url:
                self.append_system_message("当前服务尚未配置 API 密钥或服务地址，暂不自动刷新模型列表。")
                return False
        else:
            api_key, base_url = self.get_api_key_and_url()
            if not api_key or not base_url:
                return False
        self.save_current_api_settings()

        provider_id = self.get_current_provider()
        spec = get_provider_spec(provider_id)

        if not spec.supports_model_list:
            message = f"{spec.display_name} 当前未配置模型列表接口，请手动输入模型名称。"
            if silent:
                self.append_system_message(message)
            else:
                QMessageBox.information(self, "不支持刷新", message)
            return False

        if self.model_worker and self.model_worker.isRunning():
            if silent:
                # 同一时间只保留一个网络线程；切换服务商时记住最新选择，
                # 等当前请求收尾后再自动请求新服务商。重复进入文献对话
                # 不排队第二次请求，避免频繁切换页签后产生无意义的连续刷新。
                if provider_id != self._model_refresh_provider_id:
                    self._pending_model_refresh_provider_id = provider_id
            if not silent:
                QMessageBox.information(self, "正在获取", "正在刷新模型列表，请稍候。")
            return False

        self.refresh_models_button.setEnabled(False)
        if hasattr(self, "model_combo"):
            self.model_combo.setEnabled(False)

        self.append_system_message(f"正在从 {spec.display_name} 获取模型列表...")
        self._silent_model_refresh = bool(silent)
        self._model_refresh_provider_id = provider_id

        self.model_worker = ModelFetchWorker(api_key, base_url, provider_id)
        self.model_worker.models_received.connect(self.on_models_received)
        self.model_worker.error_occurred.connect(self.on_model_fetch_error)
        # 信号可能在 QThread 真正结束前抵达；待旧线程结束后再派发下一次，
        # 避免因 isRunning() 竞态把切换后的刷新请求丢失或重复排队。
        self.model_worker.finished.connect(self.schedule_pending_model_refresh)
        self.model_worker.start()
        return True

    def schedule_pending_model_refresh(self):
        pending_provider = self._pending_model_refresh_provider_id
        self._pending_model_refresh_provider_id = ""
        if pending_provider and pending_provider == self.get_current_provider():
            QTimer.singleShot(0, lambda: self.fetch_models(silent=True))

    def on_models_received(self, model_ids: list):
        provider_id = self.get_current_provider()
        if self._model_refresh_provider_id and provider_id != self._model_refresh_provider_id:
            self.refresh_models_button.setEnabled(True)
            self.model_combo.setEnabled(True)
            self.append_system_message("模型列表已返回，但对话服务商已切换；已忽略旧服务商的结果。")
            self._silent_model_refresh = False
            self._model_refresh_provider_id = ""
            return
        model_ids = [str(item) for item in model_ids if str(item).strip() and not is_machine_translation_provider_id(str(item))]
        stored_provider = self.settings.chat_providers.get(provider_id)
        stored_model = stored_provider.model if stored_provider and stored_provider.model else ""
        current_model = self.model_combo.currentText().strip()
        requested_model = stored_model or current_model

        self._syncing_shared_settings = True
        self.model_combo.blockSignals(True)
        try:
            self.model_combo.clear()
            self.model_combo.addItems(model_ids)
        finally:
            self.model_combo.blockSignals(False)
            self._syncing_shared_settings = False

        selected_model, requested_missing = self.choose_model_after_refresh(model_ids, requested_model)
        if selected_model:
            self.model_combo.setCurrentText(selected_model)
            self.save_current_api_settings()

        self.refresh_models_button.setEnabled(True)
        self.model_combo.setEnabled(True)
        self.update_image_mode_visibility()

        self.update_reasoning_visibility()

        self.append_system_message(f"模型列表获取成功，共 {len(model_ids)} 个模型。")
        if requested_missing and selected_model:
            self.append_system_message(
                f"上次对话模型“{requested_model}”已不在当前模型列表中，已临时显示“{selected_model}”。"
            )
            if not self._silent_model_refresh:
                QMessageBox.information(
                    self,
                    "模型需要重新选择",
                    f"上次对话模型“{requested_model}”已不在当前模型列表中。\n请在上方模型下拉框中选择当前对话模型。",
                )
        self._silent_model_refresh = False
        self._model_refresh_provider_id = ""

    def on_model_fetch_error(self, error_message: str):
        self.refresh_models_button.setEnabled(True)
        self.model_combo.setEnabled(True)

        if self._model_refresh_provider_id and self.get_current_provider() != self._model_refresh_provider_id:
            self.append_system_message("旧服务商的模型列表刷新失败；当前已切换服务商，未影响当前选择。")
            self._silent_model_refresh = False
            self._model_refresh_provider_id = ""
            return

        # 不做本地模型列表兜底，避免模型信息过期。
        # 用户仍可在可编辑模型框中手动输入模型名称。
        self.append_system_message(f"获取模型列表失败：{error_message}")

        if not self._silent_model_refresh:
            QMessageBox.warning(
                self,
                "获取模型失败",
                error_message,
            )
        self._silent_model_refresh = False
        self._model_refresh_provider_id = ""

    def on_send_button_clicked(self):
        """发送按钮的统一入口：空闲时发送，生成中时停止。"""
        if self.chat_worker and self.chat_worker.isRunning():
            self.stop_generation()
            return

        self.send_message()

    def api_messages_for_config(self, config: AIConfig, messages: list[dict]) -> list[dict]:
        is_image_model = is_probably_image_model(config.model)
        api_messages: list[dict] = []
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("system", "user", "assistant", "tool"):
                continue
            # 图片模型走 Images API；该接口会把历史转成一个生成 prompt。文献
            # 全文既无助于图片生成，又会显著膨胀请求，因此无论是待发送还是历史
            # 中的文献上下文都不能进入该 prompt。
            if is_image_model and self.is_document_context_history_message(message.get("content")):
                continue
            api_messages.append({
                "role": role,
                "content": sanitize_content_parts_for_api(message.get("content")),
            })
        if is_marked_non_multimodal_model(config.provider_id, config.base_url, config.model):
            return strip_image_url_parts_from_messages(api_messages)
        return api_messages

    def start_chat_worker(self, config: AIConfig, messages: list[dict]):
        worker_messages = self.api_messages_for_config(config, messages)
        self.chat_worker = ChatWorker(config, worker_messages)
        self.chat_worker.chunk_received.connect(self.on_chunk_received)
        self.chat_worker.reasoning_chunk_received.connect(self.on_reasoning_chunk_received)
        self.chat_worker.system_info_received.connect(self.on_system_info_received)
        self.chat_worker.image_received.connect(self.on_image_received)
        self.chat_worker.finished_reply.connect(self.on_finished_reply)
        self.chat_worker.error_occurred.connect(self.on_chat_error)
        self.chat_worker.start()

    def stop_generation(self):
        """安全请求停止当前生成，不使用 QThread.terminate()。"""
        if not self.chat_worker or not self.chat_worker.isRunning():
            return

        if self.cancel_requested:
            return

        self.cancel_requested = True
        self.flush_stream_buffers()
        # 保持按钮可见和可绘制，边框动画继续显示；重复点击由 cancel_requested 拦截。
        self.send_button.setEnabled(True)
        self.send_button.setText("停止中...")
        self.start_send_button_stop_animation()
        self.append_system_message("正在停止生成，请稍候...")

        self.chat_worker.request_stop()

    def send_message(self):
        if self.is_document_parse_in_progress():
            progress_text = self.document_parse_status_text or "当前文件仍在解析中"
            if self.document_parse_progress_percent >= 0:
                progress_text = f"{progress_text}（{self.document_parse_progress_percent}%）"
            QMessageBox.information(
                self,
                "文档仍在解析",
                f"{progress_text}\n\n请等待解析完成后再发送消息，确保待发送文件能正确加入上下文。",
            )
            return

        config = self.get_config()
        if config is None:
            return

        is_image_model = is_probably_image_model(config.model)

        if not self.ensure_current_conversation_named():
            return
        # 发送前的保底检查。即使某条清空/切换路径遗漏了初始化，也不能让
        # “针对论文”的内嵌对话在没有论文正文时发往模型。
        if not self.verify_embedded_document_before_send():
            return

        self.cancel_requested = False
        self.pending_assistant_text = ""
        self.pending_reasoning_text = ""

        user_text = self.input_box.toPlainText()
        original_input_images = self.pending_input_images.copy()
        model_marked_no_images = is_marked_non_multimodal_model(config.provider_id, config.base_url, config.model)
        input_images = [] if model_marked_no_images else original_input_images
        manual_image_count = len(original_input_images)
        display_user_text = user_text
        reference_quote = self.combined_pending_reference_quote()

        # 引用气泡用于界面展示；发送给模型时仍把引用内容合入本轮问题。
        api_user_text = user_text
        if reference_quote and str(reference_quote.get("text") or "").strip():
            quote_text = str(reference_quote.get("text") or "").strip()
            api_user_text = (
                "用户引用了文档中的以下内容，请结合全文回答：\n\n"
                f"“{quote_text}”\n\n"
                "用户问题：\n"
                f"{user_text.strip() or '请解释这段内容的含义，并结合全文说明它在论文中的作用。'}"
            )

        # 只阻止“没有文字也没有实际可发送图片”的空消息。
        # 当前模型被 7 天标记为非多模态时，用户只粘贴图片会被拦截，不进入聊天记录。
        if not user_text.strip() and not input_images and (is_image_model or not self.document_contexts):
            if manual_image_count > 0:
                self.clear_pending_input_images()
                self.append_image_filter_notice(
                    manual_image_count=manual_image_count,
                    manual_images_filtered=model_marked_no_images,
                )
            return

        if self.chat_worker and self.chat_worker.isRunning():
            QMessageBox.information(self, "正在回复", "请等待当前回复完成。")
            return

        if self.session_model is None:
            self.session_model = config.model
        elif config.model != self.session_model:
            QMessageBox.information(
                self,
                "模型已切换",
                "当前会话已经使用过其他模型。切换模型后，服务端上下文缓存通常无法继续复用。"
            )
            self.session_model = config.model

        # 新一轮回复开始时，重置思考过程控件。
        self.current_reasoning_widget = None

        self.input_box.clear()
        self.clear_pending_input_images()
        self.clear_pending_reference_quote()

        document_display_records = None
        document_display_text = ""
        document_images_requested = bool(
            self.document_contexts
            and not self.document_context_sent
            and not is_image_model
            and self.current_document_image_mode() == "full_with_images"
        )

        if self.document_contexts and not self.document_context_sent and not is_image_model:
            user_question = api_user_text.strip() or "请先阅读并概括这些文档。"
            user_content, document_image_count = self.build_document_context_for_message(
                user_question,
                input_images,
                include_images_override=True,
            )
            document_display_records = self.document_contexts.copy()
            document_display_text = self.build_sent_document_display_text(
                user_question=user_question,
                document_image_count=0 if model_marked_no_images else document_image_count,
            )
            self.document_context_sent = True
            self.append_system_message(
                f"已将 {len(self.document_contexts)} 个文档全文加入当前消息；发送方式：{self.document_send_summary()}。"
            )
        else:
            # 纯净保存：文本不改写；图片按 OpenAI 兼容多模态格式传入 user.content。
            user_content = self.build_user_message_content(api_user_text, input_images)

            if is_image_model and self.document_contexts and not self.document_context_sent:
                self.append_system_message("图片模型不会发送待发送的论文或附件；已保留它们，切回文本模型后可继续使用。")

        self.append_image_filter_notice(
            manual_image_count=manual_image_count,
            manual_images_filtered=manual_image_count > 0 and model_marked_no_images,
            document_images_filtered=document_images_requested and model_marked_no_images,
        )

        user_message = {
            "role": "user",
            "content": user_content,
        }
        if reference_quote:
            user_message["reference_quote"] = reference_quote
        if manual_image_count > 0 and not model_marked_no_images:
            user_message["has_manual_images"] = True

        self.messages.append(user_message)
        user_message_index = len(self.messages) - 1
        self.pending_reply_insert_index = None

        if document_display_records is not None:
            # Keep document images in the request payload instead of adding each
            # image as a separate chat bubble.
            # 这里显示的是“文档摘要气泡”，不是用户原始文本气泡，因此不绑定右键编辑。
            self.append_document_message(
                document_display_text,
                document_display_records,
                reference_quote=reference_quote,
                message_index=user_message_index,
            )

            # 用户本轮额外粘贴的图片仍然显示为普通用户图片，便于确认。
            if input_images:
                self.append_user_message("", input_images)

            # 文档已经写入 self.messages，本轮待发送区必须立即清空。
            # 这样新建对话、切换对话、继续追问时都不会再次把同一文档塞进上下文。
            self.clear_documents(show_message=False, delete_parse_outputs=False)
        else:
            self.append_user_message(
                display_user_text,
                input_images,
                message_index=user_message_index,
                reference_quote=reference_quote,
            )

        self.save_current_conversation_to_history()

        if is_image_model:
            config.image_mode = self.infer_image_mode_for_message(
                user_message_index,
                user_content,
            )

            if config.image_mode == "edit":
                image_label = "图片模型 - 图片编辑"
            else:
                image_label = "图片模型 - 图片生成"

            # 图片接口通常不支持真正的逐步流式返回，这里给用户明确等待提示即可。
            self.append_assistant_message(
                f"{image_label} 正在生成，请稍候……图片生成可能需要 30-90 秒。"
            )
        else:
            # 文本模型先创建等待提示气泡，避免接口响应前聊天区出现空白气泡。
            self.begin_assistant_stream_message()

        self.set_chat_buttons_enabled(False)

        # 新一轮流式输出刚开始时，用户通常希望看到最新回复，因此允许自动滚到底部。
        self._stream_should_auto_scroll = True

        # 不截断、不压缩、不摘要，完整发送 self.messages。
        # 如果当前模型已被标记为非多模态，只在 API 副本中移除图片，历史中的文档位置不变。
        self.start_chat_worker(config, self.messages.copy())

    def on_chunk_received(self, text: str):
        """接收正文流式片段，先进入缓冲区，避免每个 chunk 都刷新 UI。"""
        if not text:
            return

        self.pending_assistant_text += text
        self.schedule_stream_flush()

    def on_reasoning_chunk_received(self, text: str):
        """接收思考过程流式片段，先进入缓冲区，避免频繁重排 QLabel。"""
        if not text:
            return

        self.pending_reasoning_text += text
        self.schedule_stream_flush()

    def schedule_stream_flush(self):
        """安排一次批量刷新；计时器已经在等待时不重复启动。"""
        if not self.stream_flush_timer.isActive():
            self.stream_flush_timer.start()

    def flush_stream_buffers(self):
        """批量刷新当前积累的流式文本，降低 setText / 布局 / 滚动频率。"""
        if self.stream_flush_timer.isActive():
            self.stream_flush_timer.stop()

        assistant_text = self.pending_assistant_text
        reasoning_text = self.pending_reasoning_text

        self.pending_assistant_text = ""
        self.pending_reasoning_text = ""

        # The flag is set when the reply starts and is cleared only by an
        # explicit user gesture.  Rechecking ``maximum - value`` here is
        # unreliable: a previous chunk may already have increased the range
        # while the relayout timer is still pending, falsely looking like the
        # user scrolled away from the bottom.
        should_follow = bool(self._stream_should_auto_scroll)

        if reasoning_text:
            self.append_reasoning_stream_text(reasoning_text)

        if assistant_text:
            self.append_assistant_stream_text(assistant_text)

        # Do not reset this to True: doing so steals the viewport after a user
        # scrolls upward.  When following, re-run after Qt has applied the
        # newly increased bubble height (and therefore the new scrollbar max).
        if should_follow:
            self.schedule_stream_scroll_to_bottom()

    def on_system_info_received(self, text: str):
        """
        接收模型统计、停止提示等系统信息。

        如果用户在流式传输中已经手动向上滚动，则系统信息不应强行把窗口拉到底部。
        """
        was_near_bottom = self.is_chat_near_bottom(margin=120)
        self.flush_stream_buffers()
        self.append_system_message(text)

        if not was_near_bottom:
            # append_system_message 会新增一整行，默认会 force 到底部；
            # 这里把滚动位置尽量恢复到非底部状态，避免打断用户阅读历史。
            scroll_bar = self.chat_scroll_area.verticalScrollBar()
            scroll_bar.setValue(max(0, scroll_bar.value() - 160))

    def on_image_received(self, image_bytes: bytes):
        """
        在主线程把图片字节转为 QPixmap。

        后台线程不创建 QPixmap，避免跨线程使用 GUI 资源导致随机崩溃或显示异常。
        """
        self.flush_stream_buffers()

        if not image_bytes:
            return

        pixmap = QPixmap()
        if not pixmap.loadFromData(image_bytes):
            self.append_system_message("图片解析失败：无法从返回的图片字节创建 QPixmap。")
            return

        encoded = base64.b64encode(image_bytes).decode("ascii")
        mime_type = self.detect_image_mime_type_from_bytes(image_bytes)
        image_data_url = f"data:{mime_type};base64,{encoded}"
        self.append_assistant_image(pixmap, image_data_url=image_data_url)
        assistant_image_message = {
            "role": "assistant",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": image_data_url},
                }
            ],
        }

        insert_index = self.pending_reply_insert_index

        if isinstance(insert_index, int) and 0 <= insert_index <= len(self.messages):
            self.messages.insert(insert_index, assistant_image_message)
            self.pending_reply_insert_index = insert_index + 1
        else:
            self.messages.append(assistant_image_message)

        self.save_current_conversation_to_history()

    def on_finished_reply(self, reply: str):
        self.flush_stream_buffers()
        # 最后一段流式文本已显示且用户仍跟随在底部时，最终 Markdown / MathJax
        # 渲染也应继续保持在末尾。此状态必须在切换渲染模式前记录，因为切换会
        # 立即改变气泡高度，使之后的“是否在底部”判断失真。
        follow_reply_after_render = self.is_chat_near_bottom(margin=120)

        inserted_in_middle = (
            isinstance(self.pending_reply_insert_index, int)
            and self.pending_reply_insert_index < len(self.messages)
        )

        # 文本模型正常保存回复；
        # 图片模型通常没有文本回复，此时不追加空 assistant 消息。
        # 如果用户中途停止，reply 会保存已经生成出的可见部分。
        if reply != "":
            assistant_message = {
                "role": "assistant",
                "content": reply,
            }

            # 保存本轮可展示的思考过程到本地历史。
            # 注意：ChatWorker 发送给 API 前会清理这个字段，不会把思考链回传给模型。
            reasoning_text = ""
            try:
                if self.chat_worker is not None:
                    reasoning_text = getattr(self.chat_worker, "reasoning_reply", "") or ""
            except RuntimeError:
                reasoning_text = ""

            if reasoning_text.strip():
                assistant_message["reasoning_content"] = reasoning_text

            insert_index = self.pending_reply_insert_index

            if isinstance(insert_index, int) and 0 <= insert_index <= len(self.messages):
                inserted_in_middle = insert_index < len(self.messages)
                self.messages.insert(insert_index, assistant_message)
                assistant_message_index = insert_index
                self.pending_reply_insert_index = insert_index + 1
            else:
                self.messages.append(assistant_message)
                assistant_message_index = len(self.messages) - 1

            # 流式气泡在开始生成时还没有 self.messages 下标。
            # 回复完成写入 messages 后，补上绑定下标，使右键菜单可修改/删除该轮。
            if self.current_assistant_label is not None and hasattr(self.current_assistant_label, "message_index"):
                self.current_assistant_label.message_index = assistant_message_index

        self.commit_new_manual_images_in_history()
        self.save_current_conversation_to_history()

        # 思考过程在流式生成期间保持展开，方便用户查看进度；本轮回复完成后
        # 自动收起，避免长推理文本持续占用聊天区。
        if (
            self.current_reasoning_widget is not None
            and self.current_reasoning_widget.reasoning_text
        ):
            self.current_reasoning_widget.set_expanded(False)

        # 流式阶段使用纯文本预览以保持稳定；本轮结束后只重渲染一次，
        # 恢复 Markdown、公式和长回复折叠效果。
        if self.current_assistant_label is not None and hasattr(self.current_assistant_label, "set_streaming"):
            self.activate_assistant_web_bubble(self.current_assistant_label, refresh=False)
            self.current_assistant_label.set_streaming(False)

        if follow_reply_after_render:
            self.schedule_reply_render_scroll_to_bottom()

        self.pending_reply_insert_index = None
        self.current_assistant_label = None
        self.current_reasoning_widget = None
        self.cancel_requested = False
        self.set_chat_buttons_enabled(True)

        if inserted_in_middle:
            self.clear_chat_widgets_only()
            self.render_messages_from_history()
        self.resume_pending_embedded_document_load()

    def on_chat_error(self, error_message: str):
        self.flush_stream_buffers()

        # 停止或异常时也要退出轻量流式预览，避免留下未完成的纯文本气泡。
        if self.current_assistant_label is not None and hasattr(self.current_assistant_label, "set_streaming"):
            self.current_assistant_label.set_streaming(False)

        # 如果错误来自用户主动停止，不弹出失败对话框，避免把正常停止误报为异常。
        if self.cancel_requested:
            self.append_system_message("已停止生成。")
            self.cancel_requested = False
            self.set_chat_buttons_enabled(True)
            self.resume_pending_embedded_document_load()
            return

        if str(error_message or "").startswith("NON_MULTIMODAL_IMAGE_INPUT_UNSUPPORTED"):
            config = self.get_config()
            if config is not None:
                mark_non_multimodal_model(
                    config.provider_id,
                    config.base_url,
                    config.model,
                    reason=error_message,
                )
                self.append_system_message(
                    "服务端确认当前模型不支持图片输入，已标记该模型 7 天。"
                    "此次发送将自动移除图片后重试；文献解析文本仍会完整发送。"
                )
                self.discard_new_manual_images_from_latest_user_message()
                self.save_current_conversation_to_history()
                self.clear_chat_widgets_only()
                self.render_messages_from_history()
                self.begin_assistant_stream_message()
                self.cancel_requested = False
                self.pending_assistant_text = ""
                self.pending_reasoning_text = ""
                self.current_reasoning_widget = None
                self.set_chat_buttons_enabled(False)
                self.start_chat_worker(config, self.messages.copy())
                return

        if looks_like_non_multimodal_image_error(error_message):
            friendly_message = (
                "服务端确认当前模型不支持图片输入，图片没有发送成功。\n\n"
                "请切换到实际支持图片理解的多模态模型后重试，或移除图片只发送文字。"
            )
            self.append_system_message(friendly_message)
            self.pending_reply_insert_index = None
            self.cancel_requested = False
            self.set_chat_buttons_enabled(True)
            QMessageBox.information(self, "当前模型不能理解图片", friendly_message)
            self.resume_pending_embedded_document_load()
            return

        self.append_system_message(f"请求失败：{error_message}")

        self.pending_reply_insert_index = None
        self.cancel_requested = False
        self.set_chat_buttons_enabled(True)

        QMessageBox.warning(
            self,
            "请求失败",
            error_message,
        )
        self.resume_pending_embedded_document_load()

    def resume_pending_embedded_document_load(self):
        """Complete a document switch deferred while a reply was streaming."""
        pending = self.pending_embedded_document_load
        self.pending_embedded_document_load = None
        if not pending:
            return
        if self.chat_worker and self.chat_worker.isRunning():
            # finished_reply/error 信号可能比 QThread 的 isRunning 状态早一个事件循环。
            self.pending_embedded_document_load = pending
            QTimer.singleShot(50, self.resume_pending_embedded_document_load)
            return
        session_id, title, markdown_path = pending
        self.load_document_conversation(session_id, title, markdown_path)

    def clear_chat(self):
        """清空当前对话内容，但保留当前对话名称和记录项。"""
        if self.chat_worker and self.chat_worker.isRunning():
            QMessageBox.information(self, "正在回复", "请先停止或等待当前回复完成，再清空对话。")
            return

        if self.embedded:
            # 内嵌模式的“清空历史”必须等价于当前文献的首次对话：清空后立即
            # 把论文重新放入待发送区，不能留下一个脱离文献的空会话。
            self.clear_current_embedded_document_history()
            return

        self.messages = []
        self.session_model = None

        # 清空当前对话时，也清空还没有发送出去的本轮文件。
        self.clear_documents(show_message=False, delete_parse_outputs=False)
        self.clear_pending_input_images()
        self.clear_chat_widgets_only()

        self.save_current_conversation_to_history()

    def start_send_button_stop_animation(self):
        """启动“停止生成”按钮的 #ED5126 边框循环动画。"""
        self.send_button_border_phase = 0
        self.update_send_button_stop_animation()

        if not self.send_button_border_timer.isActive():
            self.send_button_border_timer.start()

    def stop_send_button_stop_animation(self):
        """停止“停止生成”边框动画，并恢复按钮默认样式。"""
        if self.send_button_border_timer.isActive():
            self.send_button_border_timer.stop()

        if hasattr(self, "send_button"):
            self.send_button.setStyleSheet("")

    def update_send_button_stop_animation(self):
        """
        更新停止按钮边框动画。

        Qt 样式表不能直接让边框颜色“旋转”，这里通过定时切换四条边颜色，
        形成黑白间隔的呼吸感，提示当前处于停止模式。
        """
        if not hasattr(self, "send_button"):
            return

        active = "#111111"
        normal = "#BDBDB8"
        hover_bg = "#F0F0EE"
        pressed_bg = "#E3E3E0"
        phases = [
            ("top", active),
            ("right", active),
            ("bottom", active),
            ("left", active),
        ]
        active_side = phases[self.send_button_border_phase % len(phases)][0]
        self.send_button_border_phase = (self.send_button_border_phase + 1) % len(phases)

        border_top = active if active_side == "top" else normal
        border_right = active if active_side == "right" else normal
        border_bottom = active if active_side == "bottom" else normal
        border_left = active if active_side == "left" else normal

        self.send_button.setStyleSheet(f"""
            QPushButton#sendButton {{
                min-height: 30px;
                background-color: #FFFFFF;
                color: #111111;
                border-top: 2px solid {border_top};
                border-right: 2px solid {border_right};
                border-bottom: 2px solid {border_bottom};
                border-left: 2px solid {border_left};
                border-radius: 10px;
                padding: 5px 14px;
                font-weight: 600;
            }}
            QPushButton#sendButton:hover {{
                background-color: {hover_bg};
            }}
            QPushButton#sendButton:pressed {{
                background-color: {pressed_bg};
            }}
        """)

    def set_chat_buttons_enabled(self, enabled: bool):
        """
        生成中仍保持发送按钮可用，但按钮语义改为“停止生成”。

        enabled=True  ：空闲状态，可发送、可清空。
        enabled=False ：生成状态，可停止、不可清空。
        """
        if enabled:
            self.stop_send_button_stop_animation()
            self.send_button.setEnabled(True)
            self.send_button.setText("发送")
            self.clear_button.setEnabled(True)

            if hasattr(self, "add_document_button"):
                self.add_document_button.setEnabled(True)

            if hasattr(self, "clear_documents_button"):
                self.clear_documents_button.setEnabled(bool(self.document_contexts))

            for button in getattr(self, "document_task_buttons", {}).values():
                button.setEnabled(True)
        else:
            self.send_button.setEnabled(True)
            self.send_button.setText("停止生成")
            self.start_send_button_stop_animation()
            self.clear_button.setEnabled(False)

            if hasattr(self, "add_document_button"):
                self.add_document_button.setEnabled(False)

            if hasattr(self, "clear_documents_button"):
                self.clear_documents_button.setEnabled(False)

            for button in getattr(self, "document_task_buttons", {}).values():
                button.setEnabled(False)

    def is_chat_near_bottom(self, margin: int = 80) -> bool:
        """判断聊天滚动条是否位于底部附近。"""
        if not hasattr(self, "chat_scroll_area"):
            return True

        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        return scroll_bar.maximum() - scroll_bar.value() <= margin

    def mark_stream_scroll_interrupted(self):
        """Stop stream following when the user grabs the scrollbar handle."""
        if getattr(self, "chat_worker", None) is not None and self.chat_worker.isRunning():
            self._stream_should_auto_scroll = False

    def scroll_chat_to_bottom(self, force: bool = False):
        """
        滚动到聊天底部。

        force=True 用于新增用户消息、系统消息等明确需要跳到底部的场景。
        流式输出时默认不强制滚动，避免用户查看上方内容时被持续拉回底部。

        新气泡插入布局的同一轮事件循环中，滚动条 maximum 可能还是旧值。
        因此强制滚动时再排队一次，待 Qt 完成布局并更新气泡高度后再定位到底部。
        """
        if not force and not getattr(self, "_stream_should_auto_scroll", True):
            return

        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        maximum = scroll_bar.maximum()
        if scroll_bar.value() != maximum:
            scroll_bar.setValue(maximum)

        if force and not self._bottom_scroll_after_layout_pending:
            self._bottom_scroll_after_layout_pending = True
            # 0ms 覆盖普通布局更新；80ms 覆盖嵌入式面板中由尺寸协商引起的
            # 第二次高度更新。两次之间保持 pending，避免用户消息和等待气泡
            # 各自排一组重复定时器。
            QTimer.singleShot(0, lambda: self.scroll_chat_to_bottom_after_layout(complete=False))
            QTimer.singleShot(80, self.scroll_chat_to_bottom_after_layout)

    def schedule_stream_scroll_to_bottom(self):
        """Coalesce stream updates and follow the new maximum after relayout."""
        timer = getattr(self, "_stream_scroll_timer", None)
        if timer is not None:
            timer.start(35)
        # Reasoning and WebEngine bubbles can perform a second geometry pass.
        # Re-apply after those passes, while each callback still respects an
        # explicit user opt-out.
        for delay in (0, 90, 240):
            QTimer.singleShot(delay, self.scroll_stream_to_bottom_after_layout)

    def scroll_stream_to_bottom_after_layout(self):
        if not getattr(self, "_stream_should_auto_scroll", False):
            return
        if not getattr(self, "chat_scroll_area", None):
            return
        self.chat_layout.activate()
        self.chat_container.updateGeometry()
        self.chat_scroll_area.verticalScrollBar().setValue(
            self.chat_scroll_area.verticalScrollBar().maximum()
        )

    def scroll_chat_to_bottom_after_layout(self, complete: bool = True):
        """在新增消息的布局完成后，使用更新后的滚动范围再次定位到底部。"""
        if not hasattr(self, "chat_scroll_area"):
            self._bottom_scroll_after_layout_pending = False
            return

        # 强制激活布局，确保此时读取到的是刚插入气泡后的真实滚动范围。
        self.chat_layout.activate()
        self.chat_container.updateGeometry()
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())
        if complete:
            self._bottom_scroll_after_layout_pending = False

    def schedule_reply_render_scroll_to_bottom(self):
        """跟随一条回复结束后的 Markdown / MathJax 异步高度更新。"""
        # WebEngine 首次载入、MathJax 排版和高度回传分别可能发生在不同事件周期。
        # 只在结束流式前本来就跟随底部时调用，因而不会打断正在查看历史的用户。
        for delay in (0, 420, 1150):
            QTimer.singleShot(delay, lambda: self.scroll_chat_to_bottom(force=True))

    def calculate_system_bubble_width(self) -> int:
        """系统居中消息固定为聊天视口宽度的 70%。"""
        if not hasattr(self, "chat_scroll_area"):
            return 680

        viewport_width = self.chat_scroll_area.viewport().width()
        if viewport_width <= 0:
            viewport_width = self.width()

        return max(1, int(viewport_width * 0.70))

    def calculate_normal_bubble_width(self) -> int:
        """Keep user and model-reply bubbles within the available chat width."""
        viewport_width = 680
        if hasattr(self, "chat_scroll_area"):
            viewport_width = self.chat_scroll_area.viewport().width() or self.chat_scroll_area.width() or viewport_width

        available_width = max(180, int(viewport_width) - 36)
        preferred_width = int(viewport_width * (0.82 if getattr(self, "embedded", False) else 0.72))
        minimum_width = 220 if getattr(self, "embedded", False) else 320
        return max(180, min(680, available_width, max(minimum_width, preferred_width)))

    def apply_bubble_width_policy(self, bubble: QLabel, role: str):
        """按消息角色应用气泡宽度策略。"""
        if role == "system":
            bubble.setProperty("chat_role", "system")
            self.system_bubbles.append(bubble)

            target_width = self.calculate_system_bubble_width()
            # 系统消息气泡固定宽度，并让控件本身在行内容器中水平居中。
            bubble.setMinimumWidth(target_width)
            bubble.setMaximumWidth(target_width)
            bubble.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

            if hasattr(bubble, "setAlignment"):
                bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)

            if hasattr(bubble, "adjust_to_content"):
                bubble.adjust_to_content()

            return

        target_width = self.calculate_normal_bubble_width()
        if bubble not in self.chat_bubbles:
            self.chat_bubbles.append(bubble)

        bubble.setMinimumWidth(target_width)
        bubble.setMaximumWidth(target_width)

        if hasattr(bubble, "adjust_to_content"):
            bubble.adjust_to_content()

    def update_system_bubble_widths(self):
        """Update system and model-reply bubble widths after a resize."""
        if not hasattr(self, "system_bubbles"):
            return
        if getattr(self, "_updating_bubble_widths", False):
            return

        self._updating_bubble_widths = True
        try:
            self._update_system_bubble_widths_now()
        finally:
            self._updating_bubble_widths = False

    def _update_system_bubble_widths_now(self):
        """执行实际气泡宽度刷新；由 update_system_bubble_widths 做防重入包装。"""

        target_width = self.calculate_system_bubble_width()
        normal_width = self.calculate_normal_bubble_width()
        system_width_changed = target_width != getattr(self, "_last_system_bubble_width", 0)
        normal_width_changed = normal_width != getattr(self, "_last_normal_bubble_width", 0)
        self._last_system_bubble_width = target_width
        self._last_normal_bubble_width = normal_width

        for bubble in self.system_bubbles:
            if bubble is None:
                continue

            try:
                if system_width_changed or bubble.minimumWidth() != target_width or bubble.maximumWidth() != target_width:
                    bubble.setMinimumWidth(target_width)
                    bubble.setMaximumWidth(target_width)
            except RuntimeError:
                continue

            if hasattr(bubble, "setAlignment"):
                bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)

            if hasattr(bubble, "adjust_to_content"):
                bubble.adjust_to_content()
            else:
                # QLabel 系统气泡没有 adjust_to_content，按 sizeHint 手动刷新高度。
                bubble.adjustSize()
                bubble.setFixedHeight(max(42, bubble.sizeHint().height() + 8))

            bubble.updateGeometry()

        # Update user, model-reply, and document bubbles after the chat area changes.
        alive_bubbles = []
        for bubble in getattr(self, "chat_bubbles", []):
            if bubble is None:
                continue

            try:
                if normal_width_changed or bubble.minimumWidth() != normal_width or bubble.maximumWidth() != normal_width:
                    bubble.setMinimumWidth(normal_width)
                    bubble.setMaximumWidth(normal_width)
                else:
                    alive_bubbles.append(bubble)
                    continue
            except RuntimeError:
                continue

            if hasattr(bubble, "adjust_to_content"):
                bubble.adjust_to_content()

            bubble.updateGeometry()
            alive_bubbles.append(bubble)

        self.chat_bubbles = alive_bubbles

    def schedule_bubble_width_update(self):
        if hasattr(self, "bubble_width_update_timer") and not self.bubble_width_update_timer.isActive():
            self.bubble_width_update_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.schedule_bubble_width_update()
        self.position_message_navigator()

    def make_chat_container_transparent(self, widget: QWidget, object_name: str):
        """聊天行里的普通容器不绘制背景，避免消息之间出现整块底色。"""
        widget.setObjectName(object_name)
        widget.setAutoFillBackground(False)
        widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        widget.setStyleSheet(f"QWidget#{object_name} {{ background: transparent; border: none; }}")

    def create_text_bubble(self, text: str, role: str, message_index: int | None = None) -> ChatTextBubble:
        """Create a text bubble with Markdown, copy, edit, and delete actions."""
        render_markdown = (
            role == "assistant"
            and hasattr(self, "render_markdown_checkbox")
            and self.render_markdown_checkbox.isChecked()
        )

        bubble = ChatTextBubble(text, role, render_markdown)
        bubble.message_index = message_index
        bubble.diagram_evidence_callback = self.open_reference_quote_reader
        bubble.diagram_ask_callback = self.prepare_diagram_node_question
        bubble.diagram_document_path = str(getattr(self, "current_embedded_document_path", "") or "")

        # 把气泡右键菜单请求转交给 ChatWindow 统一处理。
        bubble.edit_requested.connect(self.edit_message_from_bubble)
        bubble.apply_requested.connect(self.apply_assistant_bubble_text)
        bubble.delete_turn_requested.connect(self.delete_turn_from_bubble)
        bubble.resend_requested.connect(self.resend_message_from_bubble)
        bubble.rich_render_requested.connect(self.activate_assistant_web_bubble)

        self.apply_bubble_width_policy(bubble, role)

        base_style = f"""
            QTextBrowser {{
                font-size: 14px;
                line-height: 1.55;
                padding: 10px 13px;
                border-radius: 2px;
                background-color: {COLOR_BG_SURFACE_2};
                border: 1px solid {COLOR_BORDER_HAIR};
                font-family: {APP_SERIF_FONT_FAMILY_STACK};
            }}
            QTextBrowser QWidget,
            QTextBrowser QAbstractScrollArea,
            QTextBrowser > QWidget,
            QTextBrowser::viewport {{
                background-color: transparent;
            }}
        """

        if role == "user":
            bubble.setStyleSheet(base_style + f"""
                QTextBrowser {{
                    color: #FFFFFF;
                    background-color: {COLOR_ACCENT};
                    border-color: {COLOR_ACCENT};
                }}
            """)
        elif role == "assistant":
            bubble.setStyleSheet(base_style + f"""
                QTextBrowser {{
                    color: {COLOR_TEXT_PRIMARY};
                    background-color: {COLOR_BG_SURFACE_2};
                    border-color: {COLOR_BORDER_HAIR};
                }}
            """)
            self.assistant_bubbles.append(bubble)
        else:
            bubble.setStyleSheet(base_style + f"""
                QTextBrowser {{
                    color: {COLOR_TEXT_MUTED};
                    background-color: {COLOR_ACCENT_SOFT_WEAK};
                    border-color: {COLOR_BORDER_HAIR};
                    font-family: {APP_UI_FONT_FAMILY_STACK};
                }}
            """)

        bubble.viewport().setStyleSheet("background-color: transparent;")

        bubble.adjust_to_content()
        return bubble

    def prepare_diagram_node_question(self, node: dict):
        """Put a diagram-node question in the composer without sending it."""
        label = str((node or {}).get("label") or "").strip()
        detail = str((node or {}).get("detail") or "").strip()
        if not label:
            return
        self.input_box.setPlainText(f"请解释“{label}”{f'：{detail}' if detail else ''}在当前文献中的含义和证据。")
        self.input_box.setFocus()

    def activate_assistant_web_bubble(self, bubble: ChatTextBubble, refresh: bool = True):
        """Keep at most one heavyweight chat WebEngine surface alive."""
        if bubble is None:
            return
        for other in list(getattr(self, "assistant_bubbles", []) or []):
            if other is bubble or not hasattr(other, "release_web_render"):
                continue
            other.release_web_render()
        if hasattr(bubble, "activate_web_render"):
            bubble.activate_web_render(refresh=refresh)

    def add_bubble_row(self, widget: QWidget, role: str):
        """添加一行聊天气泡：用户右侧，模型左侧，系统居中。"""
        row_widget = QWidget()
        self.make_chat_container_transparent(row_widget, "chatBubbleRow")
        # Expand the row so the two stretches can center system messages.
        row_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)

        if (
            role != "system"
            and widget.objectName() in {"chatTurnContainer", "chatImagesRow"}
            and widget not in getattr(self, "chat_bubbles", [])
        ):
            target_width = self.calculate_normal_bubble_width()
            try:
                widget.setMinimumWidth(target_width)
                widget.setMaximumWidth(target_width)
                self.chat_bubbles.append(widget)
            except RuntimeError:
                pass

        if role == "user":
            row_layout.addStretch(1)
            row_layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignRight)
        elif role == "system":
            # 进一步强制行布局居中：
            # 1. 左右 stretch 权重相同；
            # 2. widget 使用 AlignCenter；
            # 3. row_layout 自身也设置 AlignCenter，避免全屏时布局只按内容宽度计算。
            row_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row_layout.addStretch(1)
            row_layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignCenter)
            row_layout.addStretch(1)
        else:
            row_layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignLeft)
            row_layout.addStretch(1)

        self.chat_layout.insertWidget(self.chat_layout.count() - 1, row_widget)
        message_index = widget.property("chat_message_index")
        if role == "user" and isinstance(message_index, int):
            self.message_row_widgets[message_index] = row_widget
            self.refresh_message_navigator()
        # 新增一整行消息时强制到底部；流式追加文本则由 append_*_stream_text 单独按需处理。
        if not getattr(self, "_bulk_rendering_messages", False):
            self.scroll_chat_to_bottom(force=True)

    def build_sent_document_display_text(self, user_question: str, document_image_count: int) -> str:
        """构造聊天窗口中的“已发送文档”摘要气泡文本。"""
        titles = "；".join(str(item.get("title", "未命名文档")) for item in self.document_contexts)
        question = user_question.strip() or "请先阅读并概括这些文档。"
        send_summary = self.document_send_summary()
        if self.current_document_image_mode() == "full_with_images" and document_image_count == 0:
            send_summary += "（此次发送未包含图片）"

        return (
            "📄 已发送文档\n"
            f"文档数量: {len(self.document_contexts)}\n"
            f"文档: {titles}\n"
            f"发送方式: {send_summary}\n"
            f"发送的文档图片: {document_image_count} 张\n"
            f"用户问题: {question}\n\n"
            "点击使用系统默认程序打开原始文献。"
        )

    def append_document_message(
        self,
        text: str,
        document_records: list[dict],
        reference_quote: dict | None = None,
        message_index: int | None = None,
    ):
        """添加一个可点击的文档气泡；可在气泡上方悬浮显示引用内容。"""
        bubble = DocumentBubbleLabel(
            text,
            document_records,
            self.open_sent_document_original,
        )
        bubble.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.apply_bubble_width_policy(bubble, "user")
        bubble.setStyleSheet(f"""
            QLabel {{
                font-size: 13px;
                line-height: 1.5;
                padding: 10px 13px;
                border-radius: 2px;
                background-color: {COLOR_BG_SURFACE_2};
                color: {COLOR_TEXT_PRIMARY};
                border: 1px solid {COLOR_BORDER_STRONG};
                font-family: {APP_MONO_FONT_FAMILY_STACK};
            }}
            QLabel:hover {{
                background-color: {COLOR_ACCENT};
                color: #FFFFFF;
            }}
        """)

        if reference_quote:
            turn_widget = QWidget()
            turn_widget.setProperty("chat_message_index", message_index)
            self.make_chat_container_transparent(turn_widget, "chatTurnContainer")
            turn_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            turn_layout = QVBoxLayout(turn_widget)
            turn_layout.setContentsMargins(0, 0, 0, 0)
            turn_layout.setSpacing(3)

            for item in self.individual_reference_quotes(reference_quote):
                quote_label = ReferenceQuoteLabel(item, self.open_reference_quote_reader)
                quote_label.setMaximumWidth(520)
                turn_layout.addWidget(quote_label, 0, Qt.AlignmentFlag.AlignRight)
            turn_layout.addWidget(bubble, 0, Qt.AlignmentFlag.AlignRight)
            self.add_bubble_row(turn_widget, "user")
            return

        bubble.setProperty("chat_message_index", message_index)
        self.add_bubble_row(bubble, "user")

    def open_sent_document_original(self, document_records: list[dict]):
        """用系统默认程序打开已发送文档对应的原始文件。"""
        adapter = self.current_document_tool_adapter()
        opened_count = 0
        missing_original_count = 0

        for record in document_records:
            source_path = Path(record.get("path", ""))

            if not source_path.exists():
                missing_original_count += 1
                continue

            original_path = None
            try:
                if adapter and adapter.find_stored_original:
                    original_path = adapter.find_stored_original(source_path.parent)
            except Exception:
                original_path = None

            # 直接添加的文本文件本身就是用户选择的原始文件；经 MinerU 解析的
            # Prefer the source copy saved with the parsed document; do not open
            # the generated Markdown in the reader as a substitute.
            target_path = original_path if original_path and original_path.exists() else source_path
            if source_path.suffix.lower() in {".md", ".markdown"} and target_path == source_path:
                missing_original_count += 1
                continue

            if QDesktopServices.openUrl(QUrl.fromLocalFile(str(target_path.resolve()))):
                opened_count += 1
            else:
                QMessageBox.warning(self, "无法打开文件", f"系统默认程序无法打开：\n{target_path}")

        if opened_count == 0 and missing_original_count:
            QMessageBox.information(
                self,
                "未找到原始文献",
                "该消息对应的是 Markdown 解析文件，但同目录没有找到保存的原始文献。\n\n"
                "通过“添加文件”解析的 PDF、Word 等文件会自动保存原始副本；"
                "直接添加 Markdown 时，请保留原始文献在解析结果目录中。",
            )

    def create_document_reader_window(
        self,
        source_path: Path,
        translation_path: Path | None,
        original_path: Path | None,
        adapter: DocumentToolAdapter | None,
    ) -> QWidget | None:
        """创建阅读窗口；宿主阅读器失败时降级为纯文本窗口，绝不让点击气泡带崩对话界面。"""
        reader_kwargs = {
            "source_path": source_path,
            "translation_path": translation_path,
            "live_translation_markdown": "",
            "original_path": original_path,
            "parent": self,
        }

        if adapter and adapter.create_reader_window:
            try:
                return adapter.create_reader_window(**reader_kwargs)
            except Exception as exc:
                # ReaderWindow 可能依赖宿主的预览组件；嵌入式对话侧栏中该组件
                # 初始化失败时，继续用独立阅读器展示 Markdown，而不是让 Qt 事件回调冒泡。
                QMessageBox.warning(
                    self,
                    "已切换为安全阅读模式",
                    f"高级阅读界面无法打开，已改用安全阅读模式。\n\n原因: {exc}",
                )

        try:
            return StandaloneDocumentReaderWindow(**reader_kwargs)
        except Exception as exc:
            QMessageBox.critical(self, "无法打开文档", f"阅读窗口初始化失败：{exc}")
            return None

    def remove_document_reader_window(self, reader):
        """阅读窗口关闭后移除引用，避免列表长期积累无效窗口。"""
        if reader in self.document_reader_windows:
            self.document_reader_windows.remove(reader)

    def append_user_message(
        self,
        text: str,
        images: list[dict] | None = None,
        message_index: int | None = None,
        reference_quote: dict | None = None,
        hide_images: bool = False,
    ):
        """
        添加用户消息，右侧显示。

        关键调整：
        1. 同一轮用户消息携带的图片以小型附件缩略图显示在文字气泡上方。
        2. 图片和文字放在同一个 turn_widget 中，视觉上属于同一轮对话。
        3. User images use smaller thumbnails than generated images.
        """
        images = [] if hide_images else (images or [])
        valid_pixmaps: list[QPixmap] = []

        for image_item in images:
            pixmap = image_item.get("pixmap", QPixmap())
            if isinstance(pixmap, QPixmap) and not pixmap.isNull():
                valid_pixmaps.append(pixmap)

        # 没有文本也没有图片时不创建空行。
        if not text.strip() and not valid_pixmaps:
            return

        turn_widget = QWidget()
        turn_widget.setProperty("chat_message_index", message_index)
        self.make_chat_container_transparent(turn_widget, "chatTurnContainer")
        turn_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        turn_layout = QVBoxLayout(turn_widget)
        turn_layout.setContentsMargins(0, 0, 0, 0)
        turn_layout.setSpacing(3)

        if reference_quote:
            for item in self.individual_reference_quotes(reference_quote):
                quote_label = ReferenceQuoteLabel(item, self.open_reference_quote_reader)
                quote_label.setMaximumWidth(520)
                turn_layout.addWidget(quote_label, 0, Qt.AlignmentFlag.AlignRight)

        if valid_pixmaps:
            images_row = QWidget()
            self.make_chat_container_transparent(images_row, "chatImagesRow")
            images_row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

            images_layout = QHBoxLayout(images_row)
            images_layout.setContentsMargins(0, 0, 8, 0)
            images_layout.setSpacing(5)

            # 用户消息整体靠右，因此图片行内部也靠右排列。
            images_layout.addStretch(1)

            for image_item in images:
                pixmap = image_item.get("pixmap", QPixmap())
                if not isinstance(pixmap, QPixmap) or pixmap.isNull():
                    continue

                data_url = image_item.get("data_url", "")
                # 用户随消息发送的图片只作为小型附件预览显示。
                image_label = ChatImageLabel(
                    pixmap,
                    thumbnail_size=86,
                    image_data_url=data_url,
                    on_set_as_reference=self.set_reference_image_from_chat,
                )
                images_layout.addWidget(
                    image_label,
                    0,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                )

            turn_layout.addWidget(images_row, 0, Qt.AlignmentFlag.AlignRight)

        if text.strip():
            bubble = self.create_text_bubble(text, "user", message_index=message_index)
            turn_layout.addWidget(bubble, 0, Qt.AlignmentFlag.AlignRight)

        self.add_bubble_row(turn_widget, "user")

    def append_system_message(self, text: str):
        """
        添加系统提示，居中显示。

        系统提示不需要 QTextBrowser 的富文本、右键菜单和内部文档模型；
        直接使用 QLabel 更稳定，避免 QTextDocument 对齐、宽度重算导致视觉不居中。
        """
        self.record_system_message(text)
        if self.embedded:
            self.show_system_message_toast(text)
            return

        bubble = QLabel(text or "")
        bubble.setWordWrap(True)
        bubble.setTextFormat(Qt.TextFormat.PlainText)
        bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bubble.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.apply_bubble_width_policy(bubble, "system")

        bubble.setStyleSheet(f"""
            QLabel {{
                font-size: 11px;
                line-height: 1.4;
                padding: 7px 10px;
                border-radius: 0px;
                background-color: {COLOR_ACCENT_SOFT_WEAK};
                color: {COLOR_TEXT_SECONDARY};
                border: 1px solid {COLOR_BORDER_HAIR};
                font-family: {APP_MONO_FONT_FAMILY_STACK};
            }}
        """)

        # QLabel 没有 QTextBrowser 的 adjust_to_content，因此手动按文本内容给一个稳定高度。
        bubble.adjustSize()
        bubble.setFixedHeight(max(42, bubble.sizeHint().height() + 8))

        self.add_bubble_row(bubble, "system")

    def append_assistant_message(self, text: str, message_index: int | None = None):
        """添加模型消息，左侧显示。"""
        bubble = self.create_text_bubble(text, "assistant", message_index=message_index)
        self.add_bubble_row(bubble, "assistant")
        self.current_assistant_label = bubble

    def append_assistant_message_with_reasoning(
        self,
        text: str,
        reasoning_text: str,
        message_index: int | None = None,
    ):
        """
        载入历史对话时恢复“思考过程 + 模型回复”的组合气泡。

        reasoning_content 只用于本地界面展示；
        ChatWorker 发送 API 前会过滤该字段，不会污染模型上下文。
        """
        bubble = self.create_text_bubble(text, "assistant", message_index=message_index)
        reasoning_widget = CollapsibleReasoningWidget()
        reasoning_widget.append_text(reasoning_text)

        # 历史对话载入时，思考链默认折叠，减少旧对话占用的垂直空间。
        # 注意：这只影响历史恢复；正常流式对话会在生成期间展开，并在
        # on_finished_reply() 中自动折叠。
        reasoning_widget.set_expanded(False)

        turn_widget = QWidget()
        self.make_chat_container_transparent(turn_widget, "chatTurnContainer")
        turn_width = self.calculate_normal_bubble_width()
        turn_widget.setMinimumWidth(turn_width)
        turn_widget.setMaximumWidth(turn_width)
        turn_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        turn_layout = QVBoxLayout(turn_widget)
        turn_layout.setContentsMargins(0, 0, 0, 0)
        turn_layout.setSpacing(4)
        # 思考区与回答气泡共用整轮消息宽度；带 AlignLeft 会让它退回内容宽度，
        # 在嵌入式窄栏中看起来像一条异常窄的竖栏。
        turn_layout.addWidget(reasoning_widget)
        turn_layout.addWidget(bubble, 0, Qt.AlignmentFlag.AlignLeft)

        self.add_bubble_row(turn_widget, "assistant")
        self.current_assistant_label = bubble

    def begin_assistant_stream_message(self):
        """开始一条模型流式文本消息，并预留本轮可折叠思考区。"""
        # 初始显示等待提示；收到第一个正文片段后会替换为真实回复内容。
        bubble = self.create_text_bubble("响应中......", "assistant")
        if hasattr(bubble, "set_streaming"):
            bubble.set_streaming(True)
        reasoning_widget = CollapsibleReasoningWidget()

        turn_widget = QWidget()
        self.make_chat_container_transparent(turn_widget, "chatTurnContainer")
        # 统一整轮 assistant 消息的最大宽度，保证思考框和回答框视觉一致。
        turn_width = self.calculate_normal_bubble_width()
        turn_widget.setMinimumWidth(turn_width)
        turn_widget.setMaximumWidth(turn_width)
        turn_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        turn_layout = QVBoxLayout(turn_widget)
        turn_layout.setContentsMargins(0, 0, 0, 0)
        turn_layout.setSpacing(4)
        # 流式思考区同样应占满本轮消息宽度，避免按内容宽度收缩。
        turn_layout.addWidget(reasoning_widget)
        turn_layout.addWidget(bubble, 0, Qt.AlignmentFlag.AlignLeft)

        self.add_bubble_row(turn_widget, "assistant")
        self.current_assistant_label = bubble
        self.current_reasoning_widget = reasoning_widget

    def begin_reasoning_stream_message(self):
        """
        确保本轮 assistant 气泡上方存在思考过程控件。

        注意：
        1. 这里仅用于界面展示。
        2. 普通聊天中不把 reasoning_content 写入 self.messages。
        3. 普通聊天中不把 reasoning_content 回传给模型。
        """
        if self.current_reasoning_widget is None:
            if self.current_assistant_label is None:
                self.begin_assistant_stream_message()
            else:
                # 流式重试、会话重绘等少数时序下，回答气泡可能还在，但原先的
                # reasoning 控件引用已被清空。新控件必须先重新挂到这条回复的布局；
                # 否则 append_text() 令其可见时，Qt 会把无父级 QWidget 当作独立
                # 顶层窗口显示。
                reasoning_widget = CollapsibleReasoningWidget()
                if self.attach_reasoning_widget_to_assistant_turn(
                    self.current_assistant_label,
                    reasoning_widget,
                ):
                    self.current_reasoning_widget = reasoning_widget
                else:
                    # 若旧气泡已在重绘过程中被销毁，创建一条完整的新回复容器，
                    # 也不要留下一个无父级的思考控件。
                    reasoning_widget.deleteLater()
                    self.current_assistant_label = None
                    self.begin_assistant_stream_message()

    @staticmethod
    def attach_reasoning_widget_to_assistant_turn(
        assistant_bubble: QWidget | None,
        reasoning_widget: CollapsibleReasoningWidget,
    ) -> bool:
        """Insert a recovered reasoning widget into its assistant turn safely."""
        if assistant_bubble is None:
            return False

        turn_widget = assistant_bubble.parentWidget()
        turn_layout = turn_widget.layout() if turn_widget is not None else None
        if not isinstance(turn_layout, QVBoxLayout):
            return False

        # The reasoning panel belongs directly above the answer bubble.
        turn_layout.insertWidget(0, reasoning_widget)
        return reasoning_widget.parentWidget() is turn_widget

    def append_reasoning_stream_text(self, text: str):
        """向当前回复上方的思考过程控件追加文本。"""
        if self.current_reasoning_widget is None:
            self.begin_reasoning_stream_message()

        if self.current_reasoning_widget is not None:
            self.current_reasoning_widget.append_text(text)
            self.scroll_chat_to_bottom(force=False)

    def append_assistant_stream_text(self, text: str):
        """向当前模型气泡追加文本。"""
        if self.current_assistant_label is None:
            self.begin_assistant_stream_message()

        if hasattr(self.current_assistant_label, "append_raw_text"):
            # 如果当前仍是等待提示，第一段真实响应到达时直接替换，避免保留“响应中......”。
            if (
                hasattr(self.current_assistant_label, "has_exact_raw_text")
                and self.current_assistant_label.has_exact_raw_text("响应中......")
            ):
                self.current_assistant_label.set_raw_text(text)
            else:
                self.current_assistant_label.append_raw_text(text)
        else:
            old_text = self.current_assistant_label.text()
            if old_text == "响应中......":
                self.current_assistant_label.setText(text)
            else:
                self.current_assistant_label.setText(old_text + text)

        self.scroll_chat_to_bottom(force=False)

    def message_content_editable_text(self, content) -> str:
        """
        取得可编辑文本。

        对纯文本消息直接返回字符串；
        对多模态 content，仅编辑其中的文本部分，图片 data_url 等附件保持不变。
        """
        if isinstance(content, str):
            return content

        return self.message_content_to_full_text(content)

    def replace_message_text_content(self, message: dict, new_text: str):
        """
        将编辑后的文本写回消息内容。

        兼容：
        1. content 为字符串。
        2. content 为 OpenAI 兼容多模态 parts 列表。
           此时只替换第一个文本 part，保留图片 part。
        """
        content = message.get("content")

        if isinstance(content, str):
            message["content"] = new_text
            return

        if isinstance(content, list):
            replaced = False

            for part in content:
                if isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                    part["text"] = new_text
                    replaced = True
                    break

            if not replaced:
                content.insert(0, {
                    "type": "text",
                    "text": new_text,
                })

            message["content"] = content
            return

        message["content"] = new_text

    def start_generation_for_existing_user_message(self, user_message_index: int, notice_text: str = ""):
        """
        复用已有 user 消息重新请求模型，不重复追加用户消息。

        规则：
        1. 删除该 user 后、下一条 user 前的旧 assistant 回复。
        2. 请求上下文只使用截至该 user 的消息，避免把后续轮次混入本次重发。
        3. 新回复完成后插回该 user 后面；如果后面已有其他轮次，会自动重建聊天区保证顺序正确。
        """
        if self.chat_worker and self.chat_worker.isRunning():
            QMessageBox.information(self, "正在回复", "请先停止或等待当前回复完成，再重新发送。")
            return

        if not (0 <= int(user_message_index) < len(self.messages)):
            QMessageBox.information(self, "无法重新发送", "该消息不在有效对话记录中。")
            return

        user_message_index = int(user_message_index)
        message = self.messages[user_message_index]

        if message.get("role") != "user":
            QMessageBox.information(self, "无法重新发送", "只能重新发送用户消息。")
            return

        if self.is_document_context_history_message(message.get("content")):
            QMessageBox.information(
                self,
                "不建议重新发送文档上下文",
                "这条消息是程序构造的文档上下文，包含文档全文和图片引用。\n"
                "如需重新发送文档，请新建对话或清空当前对话后重新添加文件。"
            )
            return

        config = self.get_config()
        if config is None:
            return

        if not self.ensure_current_conversation_named():
            return

        if self.session_model is None:
            self.session_model = config.model
        elif config.model != self.session_model:
            QMessageBox.information(
                self,
                "模型已切换",
                "当前会话已经使用过其他模型。切换模型后，服务端上下文缓存通常无法继续复用。"
            )
            self.session_model = config.model

        start, end = self.find_turn_range_by_message_index(user_message_index)

        if start != user_message_index:
            start = user_message_index
            end = start + 1
            while end < len(self.messages) and self.messages[end].get("role") != "user":
                end += 1

        del self.messages[start + 1:end]

        insert_index = start + 1
        worker_messages = self.messages[:insert_index]

        self.pending_reply_insert_index = insert_index
        self.cancel_requested = False
        self.pending_assistant_text = ""
        self.pending_reasoning_text = ""
        self.current_reasoning_widget = None

        self.save_current_conversation_to_history()

        self.clear_chat_widgets_only()
        self.render_messages_from_history()

        if notice_text:
            self.append_system_message(notice_text)

        if is_probably_image_model(config.model):
            config.image_mode = self.infer_image_mode_for_message(
                start,
                self.messages[start].get("content"),
            )

            if config.image_mode == "edit":
                image_label = "图片模型 - 图片编辑"
            else:
                image_label = "图片模型 - 图片生成"

            self.append_assistant_message(
                f"{image_label} 正在重新生成，请稍候……图片生成可能需要 30-90 秒。"
            )
        else:
            self.begin_assistant_stream_message()

        self.set_chat_buttons_enabled(False)

        self.start_chat_worker(config, worker_messages)

    def resend_message_from_bubble(self, bubble):
        """右键用户消息：重新发送该消息，不重复追加用户消息。"""
        message_index = getattr(bubble, "message_index", None)

        if message_index is None:
            QMessageBox.information(self, "无法重新发送", "该气泡尚未绑定到有效对话记录。")
            return

        self.start_generation_for_existing_user_message(
            int(message_index),
            "正在重新发送该用户消息。",
        )

    def find_clipboard_button_in_widget(self, root_widget: QWidget | None) -> QPushButton | None:
        """在指定窗口中查找文字为“剪贴板”的按钮，用于自动触发主界面应用逻辑。"""
        if root_widget is None:
            return None

        try:
            for button in root_widget.findChildren(QPushButton):
                button_text = button.text().replace("&", "").strip()
                if button_text == "剪贴板":
                    return button
        except RuntimeError:
            return None

        return None

    def raise_widget_to_front(self, widget: QWidget | None):
        """将目标窗口恢复显示并置于用户可见的最上层。"""
        if widget is None:
            return

        try:
            if widget.isMinimized():
                widget.showNormal()
            else:
                widget.show()
            widget.raise_()
            widget.activateWindow()
        except RuntimeError:
            pass

    def trigger_external_clipboard_button(self) -> bool:
        """
        触发外部主界面的“剪贴板”按钮。

        查找顺序：
        1. open_ai_agent_dialog(parent=...) 传入的宿主主界面。
        2. 当前对话窗口的父级窗口。
        3. QApplication 中所有可见顶层窗口。
        """
        candidate_roots = []

        host_parent = getattr(self, "host_parent_window", None)
        if host_parent is not None:
            candidate_roots.append(host_parent)

        parent_window = self.parentWidget()
        if parent_window is not None:
            candidate_roots.append(parent_window)

        candidate_roots.extend(QApplication.topLevelWidgets())

        seen_ids = set()
        for root_widget in candidate_roots:
            if root_widget is None:
                continue

            root_id = id(root_widget)
            if root_id in seen_ids:
                continue
            seen_ids.add(root_id)

            clipboard_button = self.find_clipboard_button_in_widget(root_widget)
            if clipboard_button is None:
                continue

            # 先把外部主界面提到最上层，再延迟点击按钮，确保用户能看到触发结果。
            self.raise_widget_to_front(root_widget)
            QTimer.singleShot(0, clipboard_button.click)
            return True

        return False

    def apply_assistant_bubble_text(self, bubble):
        """Copy the unformatted reply text to the system clipboard."""
        raw_text = getattr(bubble, "raw_text", "") or ""

        if not raw_text.strip():
            QMessageBox.information(self, "无法复制", "该回复没有可复制的原文内容。")
            return

        QApplication.clipboard().setText(raw_text)
        self.append_system_message("已复制回复原文到剪贴板。")

    def edit_message_from_bubble(self, bubble):
        """
        右键“修改该对话”。

        修改当前气泡对应的 self.messages 内容；
        修改后立即保存历史，并重建聊天区，保证界面与上下文一致。
        """
        if self.chat_worker and self.chat_worker.isRunning():
            QMessageBox.information(self, "正在回复", "请先停止或等待当前回复完成，再修改消息。")
            return

        message_index = getattr(bubble, "message_index", None)

        if message_index is None or not (0 <= int(message_index) < len(self.messages)):
            QMessageBox.information(self, "无法修改", "该气泡尚未绑定到有效对话记录。")
            return

        message_index = int(message_index)
        message = self.messages[message_index]

        # 程序构造的文档上下文通常很长，且包含图片 data_url、文档全文等结构化内容。
        # 这类消息不应该当作普通用户原生消息直接编辑，否则容易破坏后续上下文。
        if message.get("role") == "user" and self.is_document_context_history_message(message.get("content")):
            QMessageBox.information(
                self,
                "不建议直接修改文档上下文",
                "这条消息是程序构造的文档上下文，包含文档全文和图片引用。\n"
                "如需重新发送文档，请新建对话或清空当前对话后重新添加文件。"
            )
            return

        old_text = self.message_content_editable_text(message.get("content"))

        # 使用 QInputDialog 实例替代静态方法，便于精确控制“修改该对话”弹窗尺寸。
        edit_dialog = QInputDialog(self)
        edit_dialog.setWindowTitle("修改该对话")
        edit_dialog.setLabelText("请修改消息内容：")
        edit_dialog.setInputMode(QInputDialog.InputMode.TextInput)
        edit_dialog.setOption(QInputDialog.InputDialogOption.UsePlainTextEditForTextInput, True)
        edit_dialog.setTextValue(old_text)

        # 将弹窗宽度调整为 Qt 当前默认推荐宽度的 2 倍，高度保持默认推荐高度。
        default_size = edit_dialog.sizeHint()
        edit_dialog.resize(default_size.width() * 2, default_size.height())

        ok = edit_dialog.exec() == QDialog.DialogCode.Accepted
        if not ok:
            return

        new_text = edit_dialog.textValue()

        self.replace_message_text_content(message, new_text)
        self.save_current_conversation_to_history()

        if message.get("role") == "user":
            self.start_generation_for_existing_user_message(
                message_index,
                "已修改该用户消息，正在重新发送。",
            )
            return

        # 重新渲染聊天区，避免只改一个气泡导致多模态图片、文档摘要等显示不同步。
        self.clear_chat_widgets_only()
        self.render_messages_from_history()
        self.append_system_message("已修改该对话。")

    def find_turn_range_by_message_index(self, message_index: int) -> tuple[int, int]:
        """
        根据消息下标查找所在“轮次”的范围。

        规则：
        1. 一轮从最近的 user 消息开始。
        2. 到下一条 user 消息之前结束。
        3. 如果右键的是 assistant，则向前找到对应 user。
        """
        if not self.messages:
            return 0, 0

        start = max(0, min(message_index, len(self.messages) - 1))

        while start > 0 and self.messages[start].get("role") != "user":
            start -= 1

        end = start + 1

        while end < len(self.messages) and self.messages[end].get("role") != "user":
            end += 1

        return start, end

    def delete_turn_from_bubble(self, bubble):
        """
        右键“删除该轮对话”。

        删除当前气泡所在的一轮 user + assistant 回复；
        删除后保存历史并重绘界面。
        """
        if self.chat_worker and self.chat_worker.isRunning():
            QMessageBox.information(self, "正在回复", "请先停止或等待当前回复完成，再删除消息。")
            return

        message_index = getattr(bubble, "message_index", None)

        if message_index is None or not (0 <= int(message_index) < len(self.messages)):
            QMessageBox.information(self, "无法删除", "该气泡尚未绑定到有效对话记录。")
            return

        message_index = int(message_index)
        start, end = self.find_turn_range_by_message_index(message_index)

        confirm = QMessageBox.question(
            self,
            "删除该轮对话",
            f"确定删除第 {start + 1} 条消息开始的这一轮对话吗？\n\n"
            "该操作会同时删除该用户消息及其后续回复，并写入本地对话记录。",
        )

        if confirm != QMessageBox.StandardButton.Yes:
            return

        del self.messages[start:end]
        self.save_current_conversation_to_history()

        self.clear_chat_widgets_only()
        self.render_messages_from_history()
        self.append_system_message("已删除该轮对话。")

    def append_assistant_image(self, pixmap: QPixmap, image_data_url: str = ""):
        """
        添加模型图片，左侧显示。

        注意：
        1. pixmap 来自内存
        2. 不使用本地路径
        3. 不创建临时文件
        4. 用户可右键复制或另存为
        """
        image_label = ChatImageLabel(
            pixmap,
            image_data_url=image_data_url,
            on_set_as_reference=self.set_reference_image_from_chat,
        )
        self.add_bubble_row(image_label, "assistant")
        self.append_system_message("图片已生成。可直接输入下一步修改要求，程序会自动复用最近一张生成图；也可右键复制、另存或设为参考图。")

    def append_html(self, html: str):
        """
        兼容旧调用：新的聊天区不再使用 QTextBrowser HTML 拼接。
        旧 HTML 提示会作为系统文本显示。
        """
        plain_text = re.sub(r"<[^>]+>", "", html).strip()
        if plain_text:
            self.append_system_message(plain_text)

    def append_plain(self, text: str):
        """兼容旧调用：追加到当前模型文本气泡。"""
        self.append_assistant_stream_text(text)


def open_document_chat_session(
    session: DocumentChatSession,
    parent=None,
    *,
    settings=None,
    conversation_history_path=None,
    document_tool_adapter: DocumentToolAdapter | None = None,
):
    # Use a supplied parser adapter or initialize the default one for standalone use.
    if document_tool_adapter is not None:
        set_document_tool_adapter(document_tool_adapter)
    elif get_document_tool_adapter() is None:
        configure_research_ai_base()

    window = ChatWindow()
    # Keep document-chat settings separate from the host translation settings.
    active_settings = window.settings
    if settings is not None:
        window.shared_app_settings = settings

    if conversation_history_path is not None:
        if callable(conversation_history_path):
            window.conversation_history_path = conversation_history_path
        else:
            history_path = Path(conversation_history_path)
            window.conversation_history_path = lambda: history_path

    window.load_conversation_sessions()
    window.refresh_conversation_history_list()

    if parent is not None:
        try:
            window.setStyleSheet(parent.styleSheet())
        except Exception:
            pass

    window.setWindowTitle(f"LitMTrans - 文献对话 - {session.title}")
    provider_id = active_settings.chat_provider or "oneapi"
    provider = active_settings.chat_providers.get(provider_id)

    if hasattr(window, "provider_combo"):
        index = window.provider_combo.findData(provider_id)
        if index >= 0:
            window.provider_combo.setCurrentIndex(index)
    if provider and hasattr(window, "url_input"):
        window.url_input.setText(provider.base_url)
    key = app_config.load_secret(provider_id, "api_key")
    if key and hasattr(window, "key_input"):
        window.key_input.setText(key)
    if hasattr(window, "model_combo"):
        window.model_combo.setEditable(False)
        if provider and provider.model:
            window.model_combo.clear()
            window.model_combo.addItem(provider.model)
            window.model_combo.setCurrentText(provider.model)
    if hasattr(window, "on_models_received"):
        original_on_models_received = window.on_models_received

        def on_models_received_with_preference(model_ids: list[str]):
            original_on_models_received(model_ids)
            preferred = app_config.choose_preferred_model(model_ids, window.model_combo.currentText().strip())
            if preferred:
                window.model_combo.setCurrentText(preferred)

        window.on_models_received = on_models_received_with_preference
    if hasattr(window, "document_advanced_group"):
        window.document_advanced_group.setVisible(False)

    # Store file options on the window; popup widgets are recreated as needed.
    if hasattr(window, "_document_send_mode_value"):
        window._document_send_mode_value = "full_with_images"
    if hasattr(window, "_document_compress_images_value"):
        window._document_compress_images_value = True
    if hasattr(window, "_document_sequential_images_value"):
        window._document_sequential_images_value = True

    # Synchronize the optional inline controls when they still exist.
    try:
        if hasattr(window, "document_send_mode_combo"):
            window.document_send_mode_combo.setCurrentIndex(0)
        if hasattr(window, "document_compress_images_checkbox"):
            window.document_compress_images_checkbox.setChecked(True)
        if hasattr(window, "document_sequential_images_checkbox"):
            window.document_sequential_images_checkbox.setChecked(True)
    except RuntimeError:
        pass

    # Use full-document images by default and retry without images when a model rejects them.
    try:
        if hasattr(window, "_document_send_mode_value"):
            window._document_send_mode_value = "full_with_images"
        if hasattr(window, "_document_compress_images_value"):
            window._document_compress_images_value = True
        if hasattr(window, "_document_sequential_images_value"):
            window._document_sequential_images_value = True
        if hasattr(window, "on_document_send_mode_changed"):
            window.on_document_send_mode_changed()
        QTimer.singleShot(500, window.fetch_models)
    except Exception:
        pass

    window.current_session_id = window.make_conversation_id()
    window.current_conversation_name = session.title
    window.save_current_conversation_to_history()
    if session.markdown_path.exists():
        window.attach_markdown_document(session.markdown_path)
    if session.selected_text:
        window.set_pending_reference_quote({
            "text": session.selected_text,
            "markdown_path": str(session.markdown_path),
            "title": session.title,
        })
        window.input_box.setPlainText(
            session.question.strip()
            if session.question
            else "请解释这段内容的含义，并结合全文说明它在论文中的作用。"
        )
    elif session.question:
        window.input_box.setPlainText(session.question)
    window.update_current_conversation_label()
    window.show()
    window.raise_()
    window.activateWindow()
    try:
        cursor = window.input_box.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        window.input_box.setTextCursor(cursor)
        window.input_box.setFocus()
    except Exception:
        pass
    return window


def main():
    # Install the Qt warning filter before creating the application object.
    install_qt_warning_filter()

    # Use the document-reader settings and parser adapter in standalone mode.
    configure_research_ai_base()

    app = QApplication(sys.argv)
    app.setApplicationName("LitMTrans")
    # 仅消息提示框静音；文件选择器仍保持 Windows 原生体验。
    chat_settings_module.configure_silent_application()
    app.setStyle("Fusion")

    apply_google_sans_code_font(app, 10)
    apply_monochrome_app_style(app)
    install_search_agent_dialog_style_filter(app)

    window = ChatWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
