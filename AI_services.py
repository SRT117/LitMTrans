"""LitMTrans model providers, document parsing services, and chat payloads."""

from __future__ import annotations

from AI_common import *
from AI_request_construction import *
from epub_pipeline import EpubParseWorker

SEARCH_APP_DIR = APP_DIR
SEARCH_SETTINGS_PATH = SETTINGS_PATH
SEARCH_LEGACY_SETTINGS_PATH = LEGACY_SETTINGS_PATH
SEARCH_WORKSPACE_DIR = APP_DIR / "workspace"

# Chat panel dimensions and control sizes.
AGENT_UI_METRICS = {
    "window_initial_width": 1180,
    "window_initial_height": 800,
    "left_panel_min_width": 330,
    "left_panel_max_width": 430,
    "splitter_left_width": 360,
    "splitter_right_width": 860,
    "splitter_handle_width": 8,
    "form_label_width": 58,
    "reasoning_label_width": 38,
    "model_combo_min_width": 240,
    # Shared height for the provider, parser, and file controls.
    "combo_control_height": 38,
    "provider_card_button_width": 58,
    "provider_card_button_height": 32,
    "provider_card_dialog_width": 660,
    "provider_card_dialog_height": 360,
    "provider_card_list_min_width": 190,
    "provider_card_dialog_margin": 12,
    "provider_card_dialog_spacing": 10,
    "mineru_tool_button_height": 32,
    "action_button_height": 34,
    "input_box_height": 132,
    "reference_preview_width": 180,
    "reference_preview_height": 120,
    "left_panel_spacing": 8,
    "chat_panel_spacing": 8,
    "api_group_spacing": 6,
}
PROVIDER_CARD_SECRET_PROVIDER_ID = "provider_card"


class SearchAppConfig(_EmbeddedAppConfig):
    """Application settings used by the embedded document-chat window."""

    APP_DIR = APP_DIR


def make_parse_output_dir(input_path: Path) -> Path:
    """Create a unique parser output directory in the active work folder."""
    workspace_dir = work_dir_path(load_settings()) / "parsed_documents"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    base_name = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", input_path.stem).strip("_") or "document"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = workspace_dir / f"{base_name}_{timestamp}"
    index = 2
    while candidate.exists():
        candidate = workspace_dir / f"{base_name}_{timestamp}_{index:02d}"
        index += 1
    return candidate


DIRECT_TEXT_INPUT_EXTENSIONS = {
    # Markdown 仍按文本直接读取，不走 MinerU。
    ".md",
    ".markdown",

    # 常见纯文本和代码文件：直接按原文发送给模型，不走 MinerU 解析。
    ".txt",
    ".text",
    ".log",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",

    # 常见代码文件。
    ".py",
    ".m",
    ".r",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".css",
    ".scss",
    ".less",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cs",
    ".go",
    ".rs",
    ".php",
    ".rb",
    ".swift",
    ".kt",
    ".kts",
    ".sh",
    ".bash",
    ".bat",
    ".ps1",
    ".sql",
}


def is_direct_text_input_file(path: Path) -> bool:
    """判断文件是否应直接按纯文本读取，不送 MinerU 解析。"""
    return path.suffix.lower() in DIRECT_TEXT_INPUT_EXTENSIONS


def is_supported_input_file(path: Path) -> bool:
    # 纯文本/代码文件由本程序直接读取，不需要 MinerU。
    if is_direct_text_input_file(path):
        return True

    return path.suffix.lower() in {
        ".pdf",
        ".docx",
        ".doc",
        ".pptx",
        ".ppt",
        ".xlsx",
        ".xls",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".bmp",
        ".epub",
    }


def stored_original_path(output_dir: Path, source_path: Path) -> Path:
    suffix = source_path.suffix if source_path.suffix else ".bin"
    return output_dir / f"original{suffix}"


class SearchDocumentParseWorker(QThread):
    finished_signal = Signal(bool, str, str)
    log_signal = Signal(str)

    def __init__(self, source_path: Path, output_dir: Path):
        super().__init__()
        self.source_path = source_path
        self.output_dir = output_dir
        self.mineru_token = load_mineru_token().strip()
        self.cancel_requested = False

    def log(self, text: str):
        self.log_signal.emit(text)

    def request_stop(self):
        """请求停止当前文档解析。"""
        self.cancel_requested = True

    def is_cancelled(self) -> bool:
        return bool(self.cancel_requested)

    def run(self):
        if not self.mineru_token:
            self.finished_signal.emit(False, "缺少 MinerU 访问令牌。", "")
            return
        try:
            if self.is_cancelled():
                raise RuntimeError("已取消解析")
            self.output_dir.mkdir(parents=True, exist_ok=True)
            original_copy = stored_original_path(self.output_dir, self.source_path)
            if self.source_path.resolve() != original_copy.resolve():
                shutil.copy2(self.source_path, original_copy)

            if self.is_cancelled():
                raise RuntimeError("已取消解析")
            self.log(f"正在准备解析：{self.source_path.name}")
            options = mineru.ParseOptions()
            quota = mineru.query_quota(self.mineru_token)
            if quota:
                remaining_pages = quota.get('user_left_quota') or quota.get('total_left_quota') or '?'
                self.log(f"MinerU 账户剩余解析额度：{remaining_pages} 页")

            if self.is_cancelled():
                raise RuntimeError("已取消解析")
            batch_id, upload_url = mineru.submit_precise_file(self.source_path, options, self.mineru_token)
            self.log(f"已创建解析任务（ID: {batch_id}）")
            mineru.http_put_file(upload_url, self.source_path, log=self.log)
            if self.is_cancelled():
                raise RuntimeError("已取消解析")
            result_item = mineru.poll_precise_result(batch_id, options, self.mineru_token, self.log)
            if self.is_cancelled():
                raise RuntimeError("已取消解析")
            markdown, zip_url, extract_dir = mineru.extract_markdown_from_zip(result_item, self.output_dir, self.log)

            raw_path = self.output_dir / "full.md"
            clean_path = self.output_dir / "full.cleaned.md"
            map_path = self.output_dir / "image_map.json"
            meta_path = self.output_dir / "mineru_task.json"

            if self.is_cancelled():
                raise RuntimeError("已取消解析")
            raw_path.write_text(markdown, encoding="utf-8")
            cleaned, image_records = mineru.simplify_markdown_images(markdown, self.output_dir, [extract_dir])
            if self.is_cancelled():
                raise RuntimeError("已取消解析")
            clean_path.write_text(cleaned, encoding="utf-8")
            map_path.write_text(json.dumps(image_records, ensure_ascii=False, indent=2), encoding="utf-8")
            meta_path.write_text(
                json.dumps(
                    {
                        "batch_id": batch_id,
                        "model_version": options.model_version,
                        "zip_url": zip_url,
                        "source_file": str(original_copy),
                        "extract_dir": str(extract_dir),
                        "result_item": result_item,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            self.finished_signal.emit(True, f"处理完成: {clean_path}", str(clean_path))
        except Exception as exc:
            if self.is_cancelled() or str(exc) == "已取消解析":
                if self.output_dir.exists():
                    try:
                        shutil.rmtree(self.output_dir)
                    except Exception:
                        pass
                self.finished_signal.emit(False, "已取消解析", "")
                return
            if self.output_dir.exists() and not (self.output_dir / "full.cleaned.md").exists():
                try:
                    shutil.rmtree(self.output_dir)
                except Exception:
                    pass
            self.finished_signal.emit(False, str(exc), "")


def latest_translation_path(folder: Path) -> Path | None:
    candidates = []
    for pattern in ("full.*.md", "*.translated.md", "*.translation.md"):
        candidates.extend([path for path in folder.glob(pattern) if path.is_file()])
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.name))


def find_stored_original(folder: Path) -> Path | None:
    for candidate in folder.glob("original.*"):
        if candidate.is_file():
            return candidate
    return None


def build_search_agent_stylesheet() -> str:
    """Build the shared application stylesheet for document chat."""
    return build_dark_premium_stylesheet() + f"""
        QWidget#SearchAgentTitleBar {{
            background: {COLOR_BG_SURFACE_2};
            border-bottom: 1px solid {COLOR_BORDER_HAIR};
        }}
        QWidget[role="secondary"] {{
            color: {COLOR_TEXT_MUTED};
        }}
        QToolButton {{
            border-radius: 2px;
        }}
        QSplitter::handle {{
            background: transparent;
            border: none;
        }}
        QSplitter::handle:hover {{
            background: {COLOR_ACCENT_SOFT};
        }}
        QLabel#systemToastLabel {{
            background: {COLOR_ACCENT};
            color: #FFFFFF;
            border: 1px solid {COLOR_ACCENT};
            border-radius: 0px;
            padding: 5px 9px;
            font-family: {APP_MONO_FONT_FAMILY_STACK};
            font-size: 11px;
        }}
    """


class _SearchAgentDialogStyleFilter(QObject):
    """Apply the document-chat stylesheet to dialogs without local styling."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Show and isinstance(obj, QDialog):
            try:
                # Preserve local dialog styles and style unconfigured dialogs consistently.
                if not obj.styleSheet():
                    obj.setStyleSheet(build_search_agent_stylesheet())
            except Exception:
                pass

        return super().eventFilter(obj, event)


_search_agent_dialog_style_filter = None


def install_search_agent_dialog_style_filter(app: QApplication | None = None) -> None:
    """安装弹窗样式过滤器；重复调用不会重复安装。"""
    global _search_agent_dialog_style_filter

    app = app or QApplication.instance()
    if app is None or _search_agent_dialog_style_filter is not None:
        return

    _search_agent_dialog_style_filter = _SearchAgentDialogStyleFilter(app)
    app.installEventFilter(_search_agent_dialog_style_filter)


def build_document_tool_adapter() -> DocumentToolAdapter:
    return DocumentToolAdapter(
        display_name="本地 EPUB / MinerU",
        settings_button_text="设置 MinerU 访问令牌",
        key_label="MinerU 访问令牌：",
        token_placeholder="请输入 MinerU 访问令牌",
        unsupported_file_message="文档解析器暂不支持此文件类型",
        is_configured=lambda: bool(load_mineru_token()),
        save_key=save_mineru_token,
        is_supported_input_file=is_supported_input_file,
        create_output_dir=make_parse_output_dir,
        create_parse_worker=lambda source_path, output_dir: (
            EpubParseWorker(str(source_path), str(output_dir))
            if source_path.suffix.lower() == ".epub"
            else SearchDocumentParseWorker(source_path, output_dir)
        ),
        latest_translation_path=latest_translation_path,
        find_stored_original=find_stored_original,
        create_reader_window=lambda *args, **kwargs: StandaloneDocumentReaderWindow(*args, **kwargs),
    )


def configure_research_ai_base() -> None:
    """Bind document chat to the LitMTrans settings and parser services."""
    global app_config, APP_DIR, SETTINGS_PATH, LEGACY_SETTINGS_PATH
    app_config = SearchAppConfig()
    APP_DIR = SEARCH_APP_DIR
    SETTINGS_PATH = SEARCH_SETTINGS_PATH
    LEGACY_SETTINGS_PATH = SEARCH_LEGACY_SETTINGS_PATH
    set_document_tool_adapter(build_document_tool_adapter())


def configure_search_ai_base() -> None:
    """Keep the historical function name available for integrations."""
    configure_research_ai_base()


def open_ai_agent_dialog(parent=None):
    configure_research_ai_base()

    # Keep Qt warnings and host-provided font settings from leaking into the dialog.
    install_qt_warning_filter()
    apply_google_sans_code_font(QApplication.instance(), 10)

    window = ChatWindow()
    # The embedded window has no external host clipboard action.
    window.host_parent_window = None
    window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
    window.setWindowTitle("LitMTrans - 文献对话")
    window.resize(
        AGENT_UI_METRICS["window_initial_width"],
        AGENT_UI_METRICS["window_initial_height"],
    )

    stylesheet = build_search_agent_stylesheet()
    # Apply the same style to message boxes and other dialogs.
    install_search_agent_dialog_style_filter(QApplication.instance())

    if parent is not None:
        # Copy only the host palette and icon; the chat window keeps its own stylesheet.
        try:
            window.setPalette(parent.palette())
            window.setWindowIcon(parent.windowIcon())
        except Exception:
            pass

    window.setStyleSheet(stylesheet)

    # Keep the chat surface light so message borders remain visible.
    window.chat_scroll_area.setStyleSheet(f"""
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
    window.chat_container.setStyleSheet(f"background-color: {COLOR_BG_SURFACE_2};")
    window.left_panel.setMinimumWidth(AGENT_UI_METRICS["left_panel_min_width"])
    window.left_panel.setMaximumWidth(AGENT_UI_METRICS["left_panel_max_width"])
    window.main_splitter.setSizes([
        AGENT_UI_METRICS["splitter_left_width"],
        AGENT_UI_METRICS["splitter_right_width"],
    ])

    # open_ai_agent_dialog() 会在 ChatWindow 创建后重新套用整窗样式表；
    # 因此这里再次给主分隔器设置透明样式，避免全局 QSplitter 样式把竖线/阴影带回来。
    window.main_splitter.setHandleWidth(AGENT_UI_METRICS["splitter_handle_width"])
    window.main_splitter.setStyleSheet("""
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

    window.input_box.setPlaceholderText("输入问题；可粘贴图片，文档会随下一条消息发送。Ctrl + Enter 发送")
    window.show()
    window.raise_()
    window.activateWindow()
    return window


@dataclass
class AIConfig:
    provider_id: str
    api_key: str
    base_url: str
    model: str

    # Reasoning mode: omit the parameter, enable it, or disable it explicitly.
    thinking_mode: str = "default"

    # Reasoning effort; custom values remain valid for newly released models.
    reasoning_effort: str = "default"

    # 是否在界面中显示模型返回的 reasoning_content。
    # 注意：普通多轮对话中不把 reasoning_content 写入 messages，也不回传给模型。
    show_reasoning: bool = False

    image_mode: str = "generation"
    image_size: str = "auto"
    image_quality: str = "auto"
    image_output_format: str = "png"
    local_reference_image_path: str = ""
    selected_reference_images: list[dict] = field(default_factory=list)
    # Stable cache-routing key for one conversation; it contains no secret or user content.
    prompt_cache_key: str = ""
    # OneAPI 固定使用 OpenAI Chat Completions 兼容构造；其他服务商走标准路径。
    request_body_mode: str = "codex"


@dataclass
class ProviderSpec:
    provider_id: str
    display_name: str
    default_base_url: str

    # 是否自动补 /v1。
    # OneAPI / NewAPI 一般需要 /v1；DeepSeek 官方 OpenAI 兼容入口不强制补 /v1。
    append_v1: bool = True

    # 是否允许调用 /models 刷新模型列表。
    supports_model_list: bool = True

    # 是否允许图片模型设置显示。
    supports_images: bool = False

    # 是否显示思考模式设置。
    # 这里按服务商协议控制，不写死任何具体模型 ID。
    supports_reasoning: bool = False

    env_key_name: str = ""
    env_base_url_name: str = ""
    env_model_name: str = ""


@dataclass
class DocumentToolAdapter:
    display_name: str = "文档工具"
    settings_button_text: str = ""
    key_label: str = ""
    token_placeholder: str = ""
    unsupported_file_message: str = ""
    is_configured: Callable[[], bool] | None = None
    save_key: Callable[[str], str] | None = None
    is_supported_input_file: Callable[[Path], bool] | None = None
    create_output_dir: Callable[[Path], Path] | None = None
    create_parse_worker: Callable[[Path, Path], QThread] | None = None
    create_reader_window: Callable[..., QWidget] | None = None
    latest_translation_path: Callable[[Path], Path | None] | None = None
    find_stored_original: Callable[[Path], Path | None] | None = None


@dataclass
class DocumentChatSession:
    title: str
    markdown_path: Path
    selected_text: str = ""
    question: str = ""


_document_tool_adapter: DocumentToolAdapter | None = None


def set_document_tool_adapter(adapter: DocumentToolAdapter | None):
    global _document_tool_adapter
    _document_tool_adapter = adapter


def get_document_tool_adapter() -> DocumentToolAdapter | None:
    return _document_tool_adapter


# 只写“供应商协议与默认入口”，不在代码中写死具体模型名称。
# 模型列表统一由用户点击“刷新模型列表”后从服务商 /models 实时获取。
PROVIDERS: dict[str, ProviderSpec] = {
    "zai": ProviderSpec(
        provider_id="zai",
        display_name="Z.ai",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        append_v1=False,
        supports_model_list=True,
        supports_images=False,
        supports_reasoning=True,
        env_key_name="ZAI_API_KEY",
        env_base_url_name="ZAI_BASE_URL",
        env_model_name="ZAI_MODEL",
    ),
    "openrouter": ProviderSpec(
        provider_id="openrouter",
        display_name="OpenRouter",
        default_base_url="https://openrouter.ai/api/v1",
        append_v1=True,
        supports_model_list=True,
        supports_images=True,
        supports_reasoning=True,
        env_key_name="OPENROUTER_API_KEY",
        env_base_url_name="OPENROUTER_BASE_URL",
        env_model_name="OPENROUTER_MODEL",
    ),
    "oneapi": ProviderSpec(
        provider_id="oneapi",
        display_name="OneAPI / NewAPI",
        default_base_url="",
        append_v1=True,
        supports_model_list=True,
        supports_images=True,
        supports_reasoning=True,
        env_key_name="ONEAPI_KEY",
        env_base_url_name="ONEAPI_BASE_URL",
        env_model_name="ONEAPI_MODEL",
    ),
    "openai_compatible": ProviderSpec(
        provider_id="openai_compatible",
        display_name="OpenAI 兼容接口",
        default_base_url="",
        append_v1=True,
        supports_model_list=True,
        supports_images=True,
        supports_reasoning=True,
        env_key_name="OPENAI_COMPATIBLE_API_KEY",
        env_base_url_name="OPENAI_COMPATIBLE_BASE_URL",
        env_model_name="OPENAI_COMPATIBLE_MODEL",
    ),
    "deepseek": ProviderSpec(
        provider_id="deepseek",
        display_name="DeepSeek",
        default_base_url="https://api.deepseek.com",
        append_v1=False,
        supports_model_list=True,
        supports_images=False,
        supports_reasoning=True,
        env_key_name="DEEPSEEK_API_KEY",
        env_base_url_name="DEEPSEEK_BASE_URL",
        env_model_name="DEEPSEEK_MODEL",
    ),
    "gemini": ProviderSpec(
        provider_id="gemini",
        display_name="Google Gemini",
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        append_v1=False,
        supports_model_list=True,
        supports_images=True,
        supports_reasoning=True,
        env_key_name="GEMINI_API_KEY",
        env_base_url_name="GEMINI_BASE_URL",
        env_model_name="GEMINI_MODEL",
    ),
    "siliconflow": ProviderSpec(
        provider_id="siliconflow",
        display_name="硅基流动 (SiliconFlow)",
        default_base_url="https://api.siliconflow.cn/v1",
        append_v1=True,
        supports_model_list=True,
        supports_images=False,
        # This provider does not advertise reasoning support, so omit the parameter.
        supports_reasoning=False,
    ),
}


def get_provider_spec(provider_id: str) -> ProviderSpec:
    return PROVIDERS.get(provider_id, PROVIDERS["oneapi"])


NON_MULTIMODAL_MODEL_MARKS_PATH = APP_DIR / "non_multimodal_models.json"
NON_MULTIMODAL_MODEL_MARK_TTL_SECONDS = 7 * 24 * 3600
THINKING_CAPABILITY_PATH = APP_DIR / "thinking_capabilities.json"
THINKING_CAPABILITY_TTL_SECONDS = 7 * 24 * 3600


def non_multimodal_model_key(provider_id: str, base_url: str, model: str) -> str:
    """多模态能力标记仅按模型名区分，不绑定服务商或网关地址。"""
    return str(model or "").strip().lower()


def load_non_multimodal_model_marks() -> dict:
    now = time.time()
    try:
        data = json.loads(NON_MULTIMODAL_MODEL_MARKS_PATH.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    marks = data.get("models") if isinstance(data.get("models"), dict) else data
    cleaned: dict[str, dict] = {}
    changed = False
    for key, value in marks.items():
        if not isinstance(value, dict):
            changed = True
            continue
        marked_at = float(value.get("marked_at") or 0)
        if marked_at <= 0 or now - marked_at > NON_MULTIMODAL_MODEL_MARK_TTL_SECONDS:
            changed = True
            continue
        model = str(value.get("model") or "").strip()
        # 旧版本使用“服务商 | URL | 模型”作为键。读取时统一迁移为模型名，
        # 让仍在有效期内的历史判断继续生效。
        normalized_key = non_multimodal_model_key("", "", model) or str(key)
        existing = cleaned.get(normalized_key)
        if existing is None or float(existing.get("marked_at") or 0) < marked_at:
            cleaned[normalized_key] = value
        if normalized_key != str(key):
            changed = True
    if changed:
        save_non_multimodal_model_marks(cleaned)
    return cleaned


def save_non_multimodal_model_marks(marks: dict):
    NON_MULTIMODAL_MODEL_MARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"models": marks, "ttl_seconds": NON_MULTIMODAL_MODEL_MARK_TTL_SECONDS}
    NON_MULTIMODAL_MODEL_MARKS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cleanup_non_multimodal_model_marks():
    load_non_multimodal_model_marks()


def thinking_capability_key(provider_id: str, base_url: str, model: str) -> str:
    """Keep capability records scoped to the provider endpoint and model."""
    return " | ".join(
        (
            str(provider_id or "").strip().lower(),
            normalize_base_url(base_url or "", provider_id or "oneapi").lower(),
            str(model or "").strip().lower(),
        )
    )


def load_thinking_capabilities() -> dict:
    now = time.time()
    try:
        data = json.loads(THINKING_CAPABILITY_PATH.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    records = data.get("models") if isinstance(data, dict) else None
    if not isinstance(records, dict):
        return {}
    cleaned = {}
    for key, value in records.items():
        if not isinstance(value, dict) or float(value.get("checked_at") or 0) <= 0:
            continue
        if now - float(value["checked_at"]) > THINKING_CAPABILITY_TTL_SECONDS:
            continue
        cleaned[str(key)] = value
    if cleaned != records:
        save_thinking_capabilities(cleaned)
    return cleaned


def save_thinking_capabilities(records: dict):
    THINKING_CAPABILITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"models": records, "ttl_seconds": THINKING_CAPABILITY_TTL_SECONDS}
    THINKING_CAPABILITY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cached_thinking_capability(provider_id: str, base_url: str, model: str) -> bool | None:
    value = load_thinking_capabilities().get(thinking_capability_key(provider_id, base_url, model))
    return bool(value.get("supports_thinking")) if isinstance(value, dict) else None


def mark_thinking_capability(provider_id: str, base_url: str, model: str, supports_thinking: bool):
    records = load_thinking_capabilities()
    records[thinking_capability_key(provider_id, base_url, model)] = {
        "provider_id": str(provider_id or "").strip(),
        "base_url": normalize_base_url(base_url or "", provider_id or "oneapi"),
        "model": str(model or "").strip(),
        "supports_thinking": bool(supports_thinking),
        "checked_at": time.time(),
    }
    save_thinking_capabilities(records)


def cleanup_thinking_capabilities():
    load_thinking_capabilities()


def is_marked_non_multimodal_model(provider_id: str, base_url: str, model: str) -> bool:
    key = non_multimodal_model_key(provider_id, base_url, model)
    return key in load_non_multimodal_model_marks()


def mark_non_multimodal_model(provider_id: str, base_url: str, model: str, reason: str = ""):
    key = non_multimodal_model_key(provider_id, base_url, model)
    marks = load_non_multimodal_model_marks()
    marks[key] = {
        "provider_id": str(provider_id or "").strip(),
        "base_url": normalize_base_url(base_url or "", provider_id or "oneapi"),
        "model": str(model or "").strip(),
        "marked_at": time.time(),
        "reason": str(reason or "")[:1000],
    }
    save_non_multimodal_model_marks(marks)


def read_key_file_lines() -> list[str]:
    # Public builds never load plaintext credentials from the working directory.
    # The helper remains for compatibility with callers; DPAPI storage and
    # environment variables are the supported sources.
    return []


def load_key_setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    target = name.lower()
    for line in read_key_file_lines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if key.strip().lower() == target:
            return raw_value.strip().strip('"').strip("'")
    return ""


def load_labelled_secret(label: str) -> str:
    lines = read_key_file_lines()
    expect_value = False
    label_lower = label.lower()
    candidates: list[str] = []

    def accept(value: str) -> str:
        token = value.strip().strip('"').strip("'").removeprefix("Bearer ").strip()
        token = token.split()[0] if token else ""
        if token and token.isascii() and len(token) >= 20:
            return token
        return ""

    for line in lines:
        if not line or line.startswith("#"):
            continue
        if expect_value:
            token = accept(line)
            if token:
                return token
            expect_value = False
        lower = line.lower()
        if label_lower in lower:
            parts = re.split(r"[:=：]", line, maxsplit=1)
            if len(parts) == 2:
                token = accept(parts[1])
                if token:
                    return token
            expect_value = True
            continue
        token = accept(line)
        if token:
            candidates.append(token)
    return candidates[0] if candidates else ""


def load_provider_key(spec: ProviderSpec) -> str:
    aliases = {
        "zai": ("zai", "zhipu", "bigmodel"),
        "openrouter": ("openrouter",),
        "deepseek": ("deepseek",),
        "oneapi": ("oneapi",),
        "openai_compatible": ("openai_compatible",),
        "gemini": ("gemini", "google_gemini"),
        "siliconflow": ("siliconflow",),
    }
    value = app_config.load_secret(spec.provider_id, "api_key") or load_key_setting(spec.env_key_name)
    if value:
        return value
    for label in aliases.get(spec.provider_id, (spec.provider_id,)):
        value = load_labelled_secret(label)
        if value:
            return value
    return ""


def load_provider_base_url(spec: ProviderSpec) -> str:
    """
    读取 API 地址，并优先使用用户已保存的配置。

    说明：
    1. 环境变量优先，便于无人值守运行时覆盖设置。
    2. 未提供环境变量时，读取 LitMTrans 设置中保存的服务地址。
    3. 两者都没有时才使用服务商默认地址。
    """
    env_base_url = load_key_setting(spec.env_base_url_name)
    if env_base_url:
        return env_base_url

    try:
        settings = load_settings()
        provider_settings = settings.providers.get(spec.provider_id)
        if provider_settings and provider_settings.base_url.strip():
            return provider_settings.base_url.strip()
    except Exception:
        pass

    return spec.default_base_url


def load_provider_model(spec: ProviderSpec) -> str:
    aliases = {
        "zai": ("ZAI_MODEL", "ZHIPU_MODEL", "BIGMODEL_MODEL"),
        "openrouter": ("OPENROUTER_MODEL",),
        "openai_compatible": ("OPENAI_COMPATIBLE_MODEL",),
    }
    for name in aliases.get(spec.provider_id, (spec.env_model_name,)):
        value = load_key_setting(name)
        if value:
            return value
    return ""


def normalize_base_url(url: str, provider_id: str = "oneapi") -> str:
    """
    按供应商协议规范化 API 地址。

    设计原则：
    1. OneAPI / NewAPI 这类 OpenAI 兼容网关通常使用 /v1。
    2. DeepSeek 官方 OpenAI 兼容入口不强制补 /v1。
    3. 用户可能直接粘贴完整接口地址，例如 /chat/completions 或 /models，这里统一裁剪到 base_url。
    """
    url = url.strip().rstrip("/")
    spec = get_provider_spec(provider_id)

    endpoint_suffixes = [
        "/chat/completions",
        "/messages",
        "/responses",
        "/images/generations",
        "/images/edits",
        "/models",
    ]

    for suffix in endpoint_suffixes:
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")

    if spec.append_v1:
        if not url.endswith("/v1"):
            url += "/v1"
    else:
        if url.endswith("/v1"):
            url = url[: -len("/v1")].rstrip("/")

    return url


def normalize_thinking_mode(text: str) -> str:
    """
    将界面显示文本归一化为 API 内部值。
    """
    value = text.strip().lower()

    mapping = {
        "服务商默认": "default",
        "默认": "default",
        "default": "default",
        "开启": "enabled",
        "启用": "enabled",
        "enabled": "enabled",
        "on": "enabled",
        "关闭": "disabled",
        "禁用": "disabled",
        "disabled": "disabled",
        "off": "disabled",
    }

    return mapping.get(value, value or "default")


def is_deepseek_reasoning_protocol(provider_id: str, base_url: str, model: str) -> bool:
    """
    判断当前请求是否更像 DeepSeek Thinking 协议。

    OneAPI / NewAPI 只是网关，后面可能转发到 DeepSeek、OpenAI、Qwen 等模型。
    如果模型名或 base_url 明确带 deepseek，就按 DeepSeek 的 thinking + reasoning_effort 规则处理。
    """
    name = (model or "").lower()
    url = (base_url or "").lower()

    return provider_id == "deepseek" or "deepseek" in name or "deepseek" in url


def is_probably_reasoning_model(model: str) -> bool:
    """
    宽松判断是否是推理模型。
    用于避免给推理模型强塞 temperature，并决定 OneAPI 是否显示思考设置。
    """
    name = (model or "").lower().strip()

    reasoning_markers = [
        "deepseek-reasoner",
        "deepseek-r1",
        "deepseek-v4",
        "gpt-5",
        "o1",
        "o3",
        "o4",
        "qwq",
        "qwen3",
        "thinking",
        "reasoner",
        "reasoning",
    ]

    return any(marker in name for marker in reasoning_markers)


def normalize_reasoning_effort(provider_id: str, text: str, model: str = "", base_url: str = "") -> str:
    """
    思考强度按协议做轻量归一化，不按具体模型写死。

    - OpenAI / OneAPI 兼容 Chat Completions：reasoning_effort 支持值随模型变化，常见为
      none / minimal / low / medium / high / xhigh。
    - DeepSeek Thinking 协议：官方 canonical 值为 high / max；low、medium 会映射为 high，
      xhigh 会映射为 max。

    用户手动输入的其他值会保留，避免服务商新增参数后客户端无法使用。
    """
    value = text.strip().lower()

    if value in ("", "服务商默认", "默认", "default"):
        return "default"

    if is_deepseek_reasoning_protocol(provider_id, base_url, model):
        aliases = {
            "low": "high",
            "medium": "high",
            "xhigh": "max",
            "none": "high",
            "minimal": "high",
        }
        return aliases.get(value, value)

    return value


def is_gemini_provider(provider_id: str, base_url: str = "") -> bool:
    return (
        str(provider_id or "").strip().lower() == "gemini"
        or "generativelanguage.googleapis.com" in str(base_url or "").lower()
    )


def is_siliconflow_provider(provider_id: str, base_url: str = "") -> bool:
    return (
        str(provider_id or "").strip().lower() == "siliconflow"
        or "api.siliconflow.cn" in str(base_url or "").lower()
    )


def siliconflow_supports_thinking(base_url: str, model: str) -> bool:
    """Return the cached result of an actual SiliconFlow capability probe."""
    return cached_thinking_capability("siliconflow", base_url, model) is True


def provider_supports_reasoning_for_model(provider_id: str, base_url: str, model: str) -> bool:
    """Resolve reasoning controls at the provider-and-model level."""
    if is_siliconflow_provider(provider_id, base_url):
        return siliconflow_supports_thinking(base_url, model)
    return get_provider_spec(provider_id).supports_reasoning


def normalize_gemini_model_id(model: str) -> str:
    return re.sub(r"^models/", "", str(model or "").strip(), flags=re.IGNORECASE)


def gemini_supports_thinking_none(model: str) -> bool:
    model_id = normalize_gemini_model_id(model).lower()
    return bool(re.match(r"^gemini-2\.5-flash(?:-|$)", model_id))


def should_send_temperature(config: AIConfig) -> bool:
    """
    推理模型或显式思考设置时不主动发送 temperature，避免与 reasoning 参数冲突。
    """
    thinking_mode = normalize_thinking_mode(config.thinking_mode)
    reasoning_effort = normalize_reasoning_effort(
        config.provider_id,
        config.reasoning_effort,
        config.model,
        config.base_url,
    )

    if thinking_mode != "default" or reasoning_effort != "default":
        return False

    if is_probably_reasoning_model(config.model):
        return False

    return True


def apply_reasoning_payload(payload: dict, config: AIConfig):
    """
    根据供应商协议向请求体添加思考参数。

    普通聊天只发送用户配置的思考控制参数；
    不把上一轮 reasoning_content 混入 messages。
    """
    thinking_mode = normalize_thinking_mode(config.thinking_mode)
    reasoning_effort = normalize_reasoning_effort(
        config.provider_id,
        config.reasoning_effort,
        config.model,
        config.base_url,
    )

    if is_siliconflow_provider(config.provider_id, config.base_url):
        if not siliconflow_supports_thinking(config.base_url, config.model):
            return
        if thinking_mode != "default":
            payload["enable_thinking"] = thinking_mode == "enabled"
        if thinking_mode != "disabled" and reasoning_effort != "default":
            budgets = {
                "minimal": 1024,
                "low": 2048,
                "medium": 4096,
                "high": 8192,
                "xhigh": 16384,
                "max": 32768,
            }
            if reasoning_effort in budgets:
                payload["thinking_budget"] = budgets[reasoning_effort]
        return

    spec = get_provider_spec(config.provider_id)
    if not spec.supports_reasoning:
        return

    is_deepseek_protocol = is_deepseek_reasoning_protocol(
        config.provider_id,
        config.base_url,
        config.model,
    )

    if is_gemini_provider(config.provider_id, config.base_url):
        effort = str(config.reasoning_effort or "").strip().lower()
        if thinking_mode == "disabled":
            payload["reasoning_effort"] = "none" if gemini_supports_thinking_none(config.model) else "minimal"
        elif effort in {"minimal", "low", "medium", "high"}:
            payload["reasoning_effort"] = effort
        elif thinking_mode in {"enabled", "default"}:
            payload["reasoning_effort"] = "medium"
        return

    if is_deepseek_protocol:
        # DeepSeek HTTP JSON 请求体中直接放 thinking；使用 OpenAI SDK 时才需要 extra_body。
        if thinking_mode != "default":
            payload["thinking"] = {
                "type": thinking_mode,
            }

        if thinking_mode != "disabled" and reasoning_effort != "default":
            payload["reasoning_effort"] = reasoning_effort

        return

    # OneAPI / NewAPI 常作为 OpenAI Chat Completions 兼容网关。
    # OpenAI Chat Completions 使用顶层 reasoning_effort。没有单独 thinking 开关。
    if thinking_mode == "disabled":
        payload["reasoning_effort"] = "none"
        return

    if reasoning_effort != "default":
        payload["reasoning_effort"] = reasoning_effort
    elif thinking_mode == "enabled":
        # 对 gpt-5.1+ 这类默认 none 的模型，显式开启时给一个中等强度。
        payload["reasoning_effort"] = "medium"


def gemini_public_thinking_config(config: AIConfig) -> dict:
    """Build Gemini-native thinking settings when public summaries are wanted."""
    model_id = normalize_gemini_model_id(config.model).lower()
    requested = str(config.reasoning_effort or "").strip().lower()
    thinking_mode = normalize_thinking_mode(config.thinking_mode)

    if model_id.startswith("gemini-2.5-"):
        if thinking_mode == "disabled" and not "2.5-pro" in model_id:
            budget = 0
        else:
            budget = {
                "minimal": 1024,
                "low": 1024,
                "medium": 8192,
                "high": 24576,
            }.get(requested, 8192)
        return {"thinking_budget": budget, "include_thoughts": True}

    level = requested if requested in {"minimal", "low", "medium", "high"} else "medium"
    # Gemini 3.1 Pro does not accept minimal; use its smallest supported level.
    if "3.1-pro" in model_id and level == "minimal":
        level = "low"
    return {"thinking_level": level, "include_thoughts": True}


def request_gemini_thought_summaries(payload: dict, config: AIConfig) -> None:
    """Ask Gemini for its public thought summary, not hidden chain-of-thought."""
    payload["extra_body"] = {
        "google": {
            "thinking_config": gemini_public_thinking_config(config)
        }
    }


def is_openrouter_provider(provider_id: str, base_url: str = "") -> bool:
    return str(provider_id or "").strip().lower() == "openrouter" or "openrouter.ai" in str(base_url or "").lower()


def openrouter_cache_mode(model: str) -> str:
    """Return OpenRouter's documented prompt-cache mode for a model family."""
    model_id = str(model or "").strip().lower()
    if model_id.startswith("anthropic/"):
        return "anthropic-auto"
    if (
        model_id.startswith("google/gemini-")
        or re.match(r"^(?:qwen/qwen(?:3(?:\.6)?-(?:coder-)?(?:plus|flash)|-plus)|deepseek/deepseek-v3\.2)", model_id)
    ):
        return "explicit-breakpoint"
    return "implicit"


def add_openrouter_cache_breakpoint(messages: list[dict]) -> bool:
    """Mark only the stable history, leaving the newest user input dynamic."""
    for message in reversed((messages or [])[:-1]):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            message["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
            return True
        if not isinstance(content, list):
            continue
        for index in range(len(content) - 1, -1, -1):
            part = content[index]
            if not isinstance(part, dict) or part.get("type") not in {"text", "input_text"} or not str(part.get("text") or "").strip():
                continue
            content[index] = {**part, "cache_control": {"type": "ephemeral"}}
            return True
    return False


def apply_openrouter_cache_strategy(payload: dict, config: AIConfig) -> str:
    if not is_openrouter_provider(config.provider_id, config.base_url):
        return ""
    mode = openrouter_cache_mode(config.model)
    if mode == "anthropic-auto":
        payload["cache_control"] = {"type": "ephemeral"}
    elif mode == "explicit-breakpoint":
        add_openrouter_cache_breakpoint(payload.get("messages") or [])
    return mode


def build_text_chat_payload(config: AIConfig, messages: list[dict]) -> dict:
    """
    构造文本聊天请求体。

    抽成独立函数后，可以在不真正发请求的情况下验证：
    1. messages 是否被原样保留。
    2. reasoning / temperature / stream_options 是否只在需要时追加。
    """
    payload = {
        "model": config.model,
        "messages": messages,
        "stream": True,
    }

    if should_send_temperature(config):
        payload["temperature"] = 0.7

    if config.show_reasoning and is_gemini_provider(config.provider_id, config.base_url):
        # Gemini rejects a request containing both reasoning_effort and a custom
        # thinking_config.  The native config also requests the public summary.
        request_gemini_thought_summaries(payload, config)
    else:
        apply_reasoning_payload(payload, config)

    if uses_codex_construction(config.provider_id, config.request_body_mode) and config.prompt_cache_key:
        # 保留原 messages 的字节级结构；重写历史前缀会直接降低缓存命中率。
        payload["prompt_cache_key"] = config.prompt_cache_key

    if is_openrouter_provider(config.provider_id, config.base_url) and config.prompt_cache_key:
        # OpenRouter uses this stable ID to keep consecutive chat turns routed
        # to a cache-compatible provider instance.
        payload["session_id"] = config.prompt_cache_key

    if is_deepseek_reasoning_protocol(
        config.provider_id,
        config.base_url,
        config.model,
    ) or (uses_codex_construction(config.provider_id, config.request_body_mode) and config.prompt_cache_key):
        # DeepSeek 会在 usage 中返回 prompt_cache_hit_tokens / prompt_cache_miss_tokens。
        # include_usage 如果被服务商支持，就可以在流式响应末尾看到缓存命中信息。
        payload["stream_options"] = {
            "include_usage": True,
        }
    elif is_gemini_provider(config.provider_id, config.base_url):
        payload["stream_options"] = {
            "include_usage": True,
        }

    apply_openrouter_cache_strategy(payload, config)

    return payload


def is_gpt56_cache_model(model: str) -> bool:
    """判断是否为 GPT-5.6（含供应商别名）缓存协议模型。"""
    return bool(re.search(r"(?:^|[^0-9])gpt[-_ ]?5[._-]?6(?:[^0-9]|$)", str(model or "").lower()))


def make_codex_cache_session_id(local_session_id: str) -> str:
    """Legacy helper retained for callers outside the chat session store.

    New chat sessions persist a random UUID instead, so remote cache IDs are
    not deterministically derived from local conversation or document IDs.
    """
    local_session_id = str(local_session_id or "").strip()
    return str(uuid.uuid4()) if local_session_id else ""


def build_codex_session_headers(prompt_cache_key: str) -> dict:
    """构造 Codex 使用的非鉴权会话关联头。

    调用方传入的 prompt_cache_key 已是 make_codex_cache_session_id 的结果；
    因此三个头与请求体中的缓存键严格相同。
    """
    session_id = str(prompt_cache_key or "").strip()
    if not session_id:
        return {}
    return {
        "session-id": session_id,
        "thread-id": session_id,
        "x-client-request-id": session_id,
    }


def build_headers(api_key: str, stream: bool = False, prompt_cache_key: str = "", use_codex_session_headers: bool = False, provider_id: str = "", base_url: str = "") -> dict:
    accept = "application/json, text/event-stream" if stream else "application/json"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": accept,
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "LitMTrans/1.0",
    }
    if use_codex_session_headers:
        headers.update(build_codex_session_headers(prompt_cache_key))
    elif is_openrouter_provider(provider_id, base_url) and prompt_cache_key:
        headers["x-session-id"] = str(prompt_cache_key)
    if is_gemini_provider(provider_id, base_url):
        headers["x-goog-api-client"] = "litmtrans/1.0"
    return headers


def build_multipart_headers(api_key: str) -> dict:
    """
    用于 /v1/images/edits。
    注意：multipart/form-data 不能手动设置 Content-Type，
    requests 会自动生成 boundary。
    """
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "LitMTrans/1.0",
    }


def is_probably_image_model(model: str) -> bool:
    """
    根据模型名粗略判断是否图片模型。
    服务商模型会变化，所以这里只做宽松判断，不写死具体模型列表。
    """
    name = model.lower()

    image_keywords = [
        "image",
        "img",
        "dall",
        "flux",
        "stable-diffusion",
        "sdxl",
        "midjourney",
        "mj",
        "ideogram",
        "recraft",
        "seedream",
        "jimeng",
        "kling-image",
    ]

    return any(keyword in name for keyword in image_keywords)


def html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
        .replace("\n", "<br>")
    )


__all__ = [name for name in globals() if not name.startswith("__")]
