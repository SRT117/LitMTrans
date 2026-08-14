"""LitMTrans shared configuration, settings, secrets, and export helpers."""

import base64
import copy
import html
import http.client
import json
import mimetypes
import os
import re
import hashlib
import shutil
import subprocess
import sys
import time
import ctypes
import threading
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

import machine_translate

import AI_services
from app_version import APP_ID, APP_NAME, APP_VERSION
from secret_session import SESSION_SECRETS, STORAGE_ERRORS

from PySide6.QtCore import QMarginsF, QPointF, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPageLayout, QPageSize, QPainter, QPolygonF, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QButtonGroup,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QMenu,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QGroupBox,
    QInputDialog,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
except Exception:
    QWebEngineSettings = None
    QWebEngineView = None
    WEBENGINE_AVAILABLE = False


API_V4_BASE_URL = "https://mineru.net/api/v4"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"
DEFAULT_MODEL_VERSION = "vlm"
WORKSPACE = Path(__file__).resolve().parent
RESOURCES = WORKSPACE / "resources"
EXPORT_FILTER = RESOURCES / "filters" / "export_fidelity.lua"
CHECKMARK_PATH = RESOURCES / "assets" / "checkmark.svg"
CHECKMARK_ICON = CHECKMARK_PATH.as_posix()
EXPORT_MARKDOWN_FORMAT = (
    "markdown+pipe_tables+raw_html+tex_math_dollars+link_attributes+table_captions+implicit_figures"
)
# 内置思源宋体会参与浏览器排版测量，升级版本以废弃旧字体生成的版面缓存。
LAYOUT_PREVIEW_VERSION = 60
LAYOUT_PREVIEW_DEBUG = False
ORIGINAL_PDF_PREVIEW_VERSION = 7
SUPPORTED_INPUT_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".jp2",
    ".webp",
    ".gif",
    ".bmp",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".html",
    ".htm",
    ".epub",
}

APP_DISPLAY_NAME = APP_NAME
APP_SUBTITLE = "文献解析、全文翻译、思维导图与文献对话"
APP_DATA_ROOT = Path(os.environ.get("APPDATA", str(Path.home())))
APP_DIR = APP_DATA_ROOT / "LitMTrans"
LEGACY_APP_DIRS = (
    APP_DATA_ROOT / "LiteratureTranslationReadingWorkbench",
    APP_DATA_ROOT / "LiteratureWorkbench",
    APP_DATA_ROOT / "MinerUResearchWorkbench",
)
SETTINGS_PATH = APP_DIR / "settings.json"
LEGACY_SETTINGS_PATH = APP_DIR / "settings_workbench.json"
LEGACY_SETTINGS_PATHS = (
    LEGACY_SETTINGS_PATH,
    *(directory / "settings_workbench.json" for directory in LEGACY_APP_DIRS),
    *(directory / "settings_mineru_workbench.json" for directory in LEGACY_APP_DIRS),
    *(directory / "settings.json" for directory in LEGACY_APP_DIRS),
)
MINERU_PROVIDER_ID = "mineru"
API_KEY_SECRET_NAME = "api_key"
APP_USER_MODEL_ID = APP_ID
_SESSION_SECRETS = SESSION_SECRETS
_SECRET_STORAGE_ERRORS = STORAGE_ERRORS
FRESH_USER_DEBUG_ENV_VAR = "LITMTRANS_FRESH_USER_DEBUG"
_FRESH_USER_DEBUG_MESSAGE = "新用户调试模式：密钥仅在本次运行有效，不会写入本机配置。"
_FRESH_USER_DEBUG_SETTINGS = None


def fresh_user_debug_enabled() -> bool:
    """Return whether this process must behave like a newly installed app."""
    return os.environ.get(FRESH_USER_DEBUG_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


def get_base_path():
    """获取资源文件的基路径，兼容脚本运行与 PyInstaller 打包。"""
    try:
        # PyInstaller 运行时会把资源解包到临时目录。
        base_path = sys._MEIPASS
    except Exception:
        # 普通脚本运行时，资源相对当前脚本目录查找。
        base_path = os.path.dirname(os.path.abspath(__file__))
    return base_path


# 阅读界面统一使用随程序打包的思源宋体，不依赖操作系统是否安装字体。
BUNDLED_READER_FONT_FILE_NAME = "SourceHanSerifCN-Regular.ttf"
BUNDLED_READER_FONT_CSS_FAMILY = "LitMTrans Source Han Serif"
BUNDLED_READER_FONT_STACK = (
    f'"{BUNDLED_READER_FONT_CSS_FAMILY}", "Source Han Serif CN", '
    '"Times New Roman", "SimSun", "Noto Serif CJK SC", '
    '"PMingLiU", "MingLiU", "Yu Mincho", "MS Mincho", "Batang", serif'
)

_BUNDLED_READER_FONT_ASSET_PATH: Path | None = None
_BUNDLED_READER_FONT_QT_FAMILY = ""
_BUNDLED_READER_FONT_LOCK = threading.Lock()


def _file_sha256(path: Path) -> str:
    """计算字体文件摘要，用于判断私有运行时副本是否需要更新。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundled_reader_font_source_path() -> Path:
    """返回源码运行或 PyInstaller 解包目录中的内置字体路径。"""
    return (
        Path(get_base_path())
        / "resources"
        / "fonts"
        / BUNDLED_READER_FONT_FILE_NAME
    )


def ensure_bundled_reader_font_asset() -> Path:
    """把内置字体同步到稳定的程序私有目录，供缓存 HTML 长期引用。

    PyInstaller 单文件模式的 sys._MEIPASS 路径可能在每次启动后变化。
    若生成的 HTML 直接引用该临时路径，程序重启后旧预览中的字体地址会失效。
    因此这里把打包字体复制到 APP_DIR 下的稳定私有目录；这不是系统字体安装。
    """
    global _BUNDLED_READER_FONT_ASSET_PATH

    with _BUNDLED_READER_FONT_LOCK:
        if (
            _BUNDLED_READER_FONT_ASSET_PATH is not None
            and _BUNDLED_READER_FONT_ASSET_PATH.is_file()
        ):
            return _BUNDLED_READER_FONT_ASSET_PATH

        source_path = bundled_reader_font_source_path()
        if not source_path.is_file():
            raise FileNotFoundError(
                "未找到内置阅读字体："
                f"{source_path}。请确认打包配置包含 resources/fonts。"
            )

        target_dir = APP_DIR / "runtime_assets" / "fonts"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / BUNDLED_READER_FONT_FILE_NAME

        needs_copy = not target_path.is_file()
        if not needs_copy:
            try:
                needs_copy = (
                    source_path.stat().st_size != target_path.stat().st_size
                    or _file_sha256(source_path) != _file_sha256(target_path)
                )
            except OSError:
                needs_copy = True

        if needs_copy:
            temp_path = target_path.with_name(
                f".{target_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                shutil.copy2(source_path, temp_path)
                os.replace(temp_path, target_path)
            finally:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        _BUNDLED_READER_FONT_ASSET_PATH = target_path
        return target_path


def register_bundled_reader_font() -> str:
    """把内置思源宋体注册为当前 Qt 进程的应用字体。"""
    global _BUNDLED_READER_FONT_QT_FAMILY

    if _BUNDLED_READER_FONT_QT_FAMILY:
        return _BUNDLED_READER_FONT_QT_FAMILY

    if QApplication.instance() is None:
        raise RuntimeError("必须在创建 QApplication 后注册内置字体。")

    font_path = ensure_bundled_reader_font_asset()
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    if font_id < 0:
        raise RuntimeError(f"Qt 无法加载内置字体：{font_path}")

    families = QFontDatabase.applicationFontFamilies(font_id)
    if not families:
        raise RuntimeError(f"内置字体没有返回有效的字体族：{font_path}")

    _BUNDLED_READER_FONT_QT_FAMILY = str(families[0])
    return _BUNDLED_READER_FONT_QT_FAMILY


def bundled_reader_qt_font_family() -> str:
    """返回 Qt 控件应使用的内置思源宋体实际字体族名称。"""
    return register_bundled_reader_font()


def bundled_reader_font_face_css() -> str:
    """生成 QWebEngine 页面使用的内置字体声明。"""
    font_path = ensure_bundled_reader_font_asset()
    font_url = json.dumps(
        font_path.resolve().as_uri(),
        ensure_ascii=False,
    )
    return f"""
@font-face {{
  font-family: "{BUNDLED_READER_FONT_CSS_FAMILY}";
  src: url({font_url}) format("truetype");
  font-style: normal;
  font-weight: 400;
  font-display: block;
}}
"""


@dataclass
class ProviderSettings:
    provider_id: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = ""
    # OneAPI 可选择 Codex 或 Claude 协议构造；旧配置默认 Codex。
    request_body_mode: str = "codex"


@dataclass
class ExportStyleSettings:
    preset_id: str = "tsinghua_default"
    body_font_cjk: str = "宋体"
    body_font_latin: str = "Times New Roman"
    heading_font_cjk: str = "黑体"
    heading_font_latin: str = "Arial"
    body_font_pt: int = 12
    heading1_pt: int = 15
    heading2_pt: int = 14
    heading3_pt: int = 13
    caption_font_pt: int = 11
    line_spacing_pt: int = 20
    first_line_indent_cm: float = 0.8
    image_width_percent: int = 45


@dataclass
class HtmlTableCell:
    text: str
    colspan: int = 1
    rowspan: int = 1
    is_header: bool = False


@dataclass
class HtmlTablePlaceholder:
    marker: str
    rows: list[list[HtmlTableCell]]


@dataclass
class WordTableFormulaTarget:
    text: str


@dataclass
class WordTableRefineChunk:
    kind: str
    text: str


@dataclass
class AppSettings:
    work_dir: str = ""
    mineru_model: str = "vlm"
    ai_provider: str = "deepseek"
    providers: dict[str, ProviderSettings] = field(default_factory=dict)
    recent_files: list[str] = field(default_factory=list)
    key_points_prompt: str = ""
    # Document-list order and the last opened item belong to the reader state.
    document_order: list[str] = field(default_factory=list)
    last_open_document: str = ""
    batch_concurrency: int = 1
    translation_source_language: str = "英文"
    translation_target_language: str = "简体中文"
    translation_mode: str = "full_context"
    translation_reference_paths: list[str] = field(default_factory=list)
    # Keep the field name shared with document chat so custom translation
    # instructions survive settings synchronization.
    translation_custom_instruction: str = ""
    # 关闭高速排版后，恢复为服务商默认强度的 DeepSeek 思考请求。
    translation_deepseek_thinking_enabled: bool = True
    translation_deepseek_reasoning_effort: str = "default"
    # Used only by the layout-preserving translation worker.
    translation_deepseek_fast_layout_enabled: bool = True
    # Gemini 翻译思考设置独立保存，切换服务商后仍可恢复。
    translation_gemini_thinking_enabled: bool = False
    translation_gemini_reasoning_effort: str = "medium"
    local_machine_parallelism: int = 4
    show_parsed_source: bool = False
    stream_show_parsed_source: bool = False
    layout_show_parsed_source: bool = False
    show_layout_restoration: bool = True
    layout_reading_mode: bool = True
    layout_development_mode: bool = False
    sync_scroll: bool = True
    # 流式阅读的同步滚动偏好单独保存，避免进入排版模式后被“强制同步”覆盖。
    stream_sync_scroll: bool = True
    reader_font_pt: int = 12
    # 排版阅读的正文字号按文献保存。键是 full.cleaned.md 的规范化绝对路径，
    # 值为 pt；仅在用户主动调整后写入，不改变新文献的自动排版结果。
    layout_body_font_by_document: dict[str, float] = field(default_factory=dict)
    export_style: ExportStyleSettings = field(default_factory=ExportStyleSettings)
    auto_check_updates: bool = True
    update_mirror_acceleration: bool = True


def _blob_from_bytes(data: bytes):
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer, DATA_BLOB


def _dpapi_protect(text: str) -> str:
    if os.name != "nt":
        raise RuntimeError("DPAPI only works on Windows")
    data_blob, data_buffer, DATA_BLOB = _blob_from_bytes(text.encode("utf-8"))
    out_blob = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(data_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        data = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return "dpapi:" + base64.b64encode(data).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        _ = data_buffer


def _dpapi_unprotect(value: str) -> str:
    if os.name != "nt" or not value.startswith("dpapi:"):
        raise RuntimeError("not a DPAPI value")
    raw = base64.b64decode(value.split(":", 1)[1])
    data_blob, data_buffer, DATA_BLOB = _blob_from_bytes(raw)
    out_blob = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(data_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8", errors="replace")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        _ = data_buffer


def protect_secret(text: str) -> str:
    if not text:
        return ""
    return _dpapi_protect(text)


def unprotect_secret(value: str) -> str:
    if not value:
        return ""
    try:
        if value.startswith("dpapi:"):
            return _dpapi_unprotect(value)
        if value.startswith("plain64:"):
            return base64.b64decode(value.split(":", 1)[1]).decode("utf-8", errors="replace")
    except Exception:
        return ""
    return value


def clamp_int(value, minimum: int, maximum: int, default: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except Exception:
        return default


def clamp_float(value, minimum: float, maximum: float, default: float) -> float:
    try:
        return max(minimum, min(maximum, float(value)))
    except Exception:
        return default


def _style_from_dict(data: dict | None) -> ExportStyleSettings:
    data = data or {}
    return ExportStyleSettings(
        preset_id=str(data.get("preset_id") or "tsinghua_default"),
        body_font_cjk=str(data.get("body_font_cjk") or "宋体"),
        body_font_latin=str(data.get("body_font_latin") or "Times New Roman"),
        heading_font_cjk=str(data.get("heading_font_cjk") or "黑体"),
        heading_font_latin=str(data.get("heading_font_latin") or "Arial"),
        body_font_pt=clamp_int(data.get("body_font_pt"), 10, 16, 12),
        heading1_pt=clamp_int(data.get("heading1_pt"), 12, 22, 15),
        heading2_pt=clamp_int(data.get("heading2_pt"), 12, 20, 14),
        heading3_pt=clamp_int(data.get("heading3_pt"), 11, 18, 13),
        caption_font_pt=clamp_int(data.get("caption_font_pt"), 9, 14, 11),
        line_spacing_pt=clamp_int(data.get("line_spacing_pt"), 15, 30, 20),
        first_line_indent_cm=clamp_float(data.get("first_line_indent_cm"), 0.0, 2.0, 0.8),
        image_width_percent=clamp_int(data.get("image_width_percent"), 25, 100, 45),
    )


def resolve_export_style(style: ExportStyleSettings | None = None) -> ExportStyleSettings:
    if isinstance(style, ExportStyleSettings):
        return style
    try:
        settings = load_settings()
        if isinstance(getattr(settings, "export_style", None), ExportStyleSettings):
            return settings.export_style
    except Exception:
        pass
    return ExportStyleSettings()


def export_style_markdown_image_width(style: ExportStyleSettings | None = None) -> str:
    style = resolve_export_style(style)
    return f"{clamp_int(style.image_width_percent, 25, 100, 45)}%"


def export_style_text_width_emu() -> int:
    # A4 with roughly 3.18 cm / 2.54 cm margins leaves about 14.28 cm text width.
    return 5_140_800


def export_style_image_width_emu(style: ExportStyleSettings | None = None) -> int:
    style = resolve_export_style(style)
    return int(export_style_text_width_emu() * (clamp_int(style.image_width_percent, 25, 100, 45) / 100.0))


def style_half_points(pt: int) -> str:
    return str(max(16, int(round(pt * 2))))


def style_line_twips(style: ExportStyleSettings | None = None) -> str:
    style = resolve_export_style(style)
    return str(max(240, int(round(style.line_spacing_pt * 20))))


def style_first_line_twips(style: ExportStyleSettings | None = None) -> str:
    style = resolve_export_style(style)
    return str(max(0, int(round(style.first_line_indent_cm * 1440 / 2.54))))


def _settings_from_dict(data: dict) -> AppSettings:
    def parse_providers(raw_providers) -> dict[str, ProviderSettings]:
        parsed = {}
        for provider_id, raw in (raw_providers or {}).items():
            if isinstance(raw, dict):
                parsed[provider_id] = ProviderSettings(
                    provider_id=provider_id,
                    base_url=str(raw.get("base_url") or ""),
                    model=str(raw.get("model") or ""),
                    # 兼容读取旧字段，但 OneAPI 已固定 OpenAI Chat Completions。
                    request_body_mode="codex",
                )
        return parsed

    providers = parse_providers(data.get("providers"))
    layout_body_fonts = {}
    raw_layout_body_fonts = data.get("layout_body_font_by_document")
    for raw_path, raw_size in (raw_layout_body_fonts if isinstance(raw_layout_body_fonts, dict) else {}).items():
        try:
            size = float(raw_size)
        except (TypeError, ValueError):
            continue
        if str(raw_path).strip() and 5.0 <= size <= 30.0:
            layout_body_fonts[str(raw_path)] = round(size * 2.0) / 2.0

    return AppSettings(
        mineru_model=str(data.get("mineru_model") or "vlm"),
        work_dir=str(data.get("work_dir") or ""),
        ai_provider=str(data.get("ai_provider") or "deepseek"),
        providers=providers,
        recent_files=[str(item) for item in data.get("recent_files") or []],
        key_points_prompt=str(data.get("key_points_prompt") or ""),
        document_order=[str(item) for item in data.get("document_order") or []],
        last_open_document=str(data.get("last_open_document") or ""),
        batch_concurrency=max(1, int(data.get("batch_concurrency") or 1)),
        translation_source_language=str(data.get("translation_source_language") or "英文"),
        translation_target_language=str(data.get("translation_target_language") or "简体中文"),
        translation_mode=str(data.get("translation_mode") or "full_context"),
        translation_reference_paths=[str(item) for item in data.get("translation_reference_paths") or []],
        translation_custom_instruction=str(data.get("translation_custom_instruction") or ""),
        translation_deepseek_thinking_enabled=bool(data.get("translation_deepseek_thinking_enabled", True)),
        translation_deepseek_reasoning_effort=(
            str(data.get("translation_deepseek_reasoning_effort") or "default").strip().lower()
            if str(data.get("translation_deepseek_reasoning_effort") or "default").strip().lower() in {"default", "high", "max"}
            else "default"
        ),
        translation_deepseek_fast_layout_enabled=bool(
            data.get("translation_deepseek_fast_layout_enabled", True)
        ),
        translation_gemini_thinking_enabled=bool(data.get("translation_gemini_thinking_enabled", False)),
        translation_gemini_reasoning_effort=(
            str(data.get("translation_gemini_reasoning_effort") or "medium").strip().lower()
            if str(data.get("translation_gemini_reasoning_effort") or "medium").strip().lower()
            in {"low", "medium", "high"}
            else "medium"
        ),
        local_machine_parallelism=clamp_int(data.get("local_machine_parallelism"), 1, 28, 4),
        show_parsed_source=bool(data.get("show_parsed_source", False)),
        stream_show_parsed_source=bool(data.get("stream_show_parsed_source", data.get("show_parsed_source", False))),
        layout_show_parsed_source=bool(data.get("layout_show_parsed_source", False)),
        show_layout_restoration=bool(data.get("show_layout_restoration", True)),
        layout_reading_mode=bool(data.get("layout_reading_mode", True)),
        layout_development_mode=bool(data.get("layout_development_mode", False)),
        sync_scroll=bool(data.get("sync_scroll", True)),
        # 新安装默认开启流式同步滚动；已有配置仍保留用户此前的偏好。
        stream_sync_scroll=bool(data.get("stream_sync_scroll", data.get("sync_scroll", True))),
        reader_font_pt=max(9, min(18, int(data.get("reader_font_pt") or 12))),
        layout_body_font_by_document=layout_body_fonts,
        export_style=_style_from_dict(data.get("export_style") if isinstance(data.get("export_style"), dict) else None),
        auto_check_updates=bool(data.get("auto_check_updates", True)),
        update_mirror_acceleration=bool(data.get("update_mirror_acceleration", True)),
    )


def load_settings() -> AppSettings:
    global _FRESH_USER_DEBUG_SETTINGS
    if fresh_user_debug_enabled():
        if _FRESH_USER_DEBUG_SETTINGS is None:
            _FRESH_USER_DEBUG_SETTINGS = AppSettings()
        return _FRESH_USER_DEBUG_SETTINGS
    for candidate in (SETTINGS_PATH, *LEGACY_SETTINGS_PATHS):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                return _settings_from_dict(data)
        except Exception:
            pass
    return AppSettings()


def save_settings(settings: AppSettings) -> None:
    global _FRESH_USER_DEBUG_SETTINGS
    if fresh_user_debug_enabled():
        _FRESH_USER_DEBUG_SETTINGS = settings
        return
    APP_DIR.mkdir(parents=True, exist_ok=True)
    data = asdict(settings)
    data["providers"] = {key: asdict(value) for key, value in settings.providers.items()}
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def secret_path(provider_id: str, name: str) -> Path:
    return APP_DIR / "secrets" / f"{provider_id}.{name}.json"


def _secret_key(provider_id: str, name: str) -> tuple[str, str]:
    return str(provider_id), str(name)


def save_secret(provider_id: str, name: str, value: str) -> bool:
    key = _secret_key(provider_id, name)
    normalized = str(value or "")
    if fresh_user_debug_enabled():
        _SESSION_SECRETS[key] = normalized
        _SECRET_STORAGE_ERRORS[key] = _FRESH_USER_DEBUG_MESSAGE
        return False
    path = secret_path(provider_id, name)
    _SESSION_SECRETS[key] = normalized
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = protect_secret(normalized)
        temporary.write_text(json.dumps({"value": encoded}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except Exception as exc:
        _SECRET_STORAGE_ERRORS[key] = str(exc) or exc.__class__.__name__
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    _SECRET_STORAGE_ERRORS.pop(key, None)
    return True


def load_secret(provider_id: str, name: str) -> str:
    key = _secret_key(provider_id, name)
    if key in _SESSION_SECRETS:
        return _SESSION_SECRETS[key]
    if fresh_user_debug_enabled():
        return ""
    paths = [secret_path(provider_id, name)]
    paths.extend(directory / "secrets" / f"{provider_id}.{name}.json" for directory in LEGACY_APP_DIRS)
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            value = unprotect_secret(str(data.get("value") or ""))
            if value:
                _SESSION_SECRETS[key] = value
                return value
        except Exception:
            continue
    return ""


def secret_is_session_only(provider_id: str, name: str) -> bool:
    return _secret_key(provider_id, name) in _SECRET_STORAGE_ERRORS


def secret_storage_error(provider_id: str, name: str) -> str:
    return _SECRET_STORAGE_ERRORS.get(_secret_key(provider_id, name), "")


def save_mineru_token(value: str) -> str:
    saved = save_secret(MINERU_PROVIDER_ID, API_KEY_SECRET_NAME, value.strip())
    return str(secret_path(MINERU_PROVIDER_ID, API_KEY_SECRET_NAME)) if saved else ""


def load_mineru_token_from_settings() -> str:
    return load_secret(MINERU_PROVIDER_ID, API_KEY_SECRET_NAME).strip()


def remember_recent_file(settings: AppSettings, file_path: str, limit: int = 12) -> None:
    normalized = str(Path(file_path))
    settings.recent_files = [item for item in settings.recent_files if item != normalized]
    settings.recent_files.insert(0, normalized)
    settings.recent_files = settings.recent_files[:limit]


def work_dir_path(settings: AppSettings | None = None) -> Path:
    settings = settings or load_settings()
    if settings.work_dir:
        return Path(settings.work_dir)
    return APP_DIR / "workspace"


def chat_history_path(settings: AppSettings | None = None) -> Path:
    return work_dir_path(settings) / "chat_conversations.json"


def re_split_model_name(model: str) -> list[str]:
    return re.split(r"[^a-z0-9]+", model)


def choose_preferred_model(model_ids: list[str], current: str = "") -> str:
    """
    在未命中用户已保存模型时，从候选模型中挑选默认模型。

    选择策略：
    1. 如果用户已明确保存且当前仍可用，优先保持用户选择不变；
    2. 如果用户没有选过，或原模型已不可用，则优先选择 pro 系列；
    3. 若没有 pro，再依次回退到 plus、高性能类，再到常规模型；
    4. mini、flash、lite 等轻量模型放到更后面，避免默认偏向轻量模型。
    """
    if current and current in model_ids:
        return current

    # 先把模型名分词缓存下来，避免重复拆分。
    token_map: dict[str, list[str]] = {}
    for model in model_ids:
        token_map[model] = [item for item in re_split_model_name(model.lower()) if item]

    # 按优先级从高到低选择默认模型，整体偏向 pro，而不是 flash。
    preferred_keyword_groups = (
        ("pro",),
        ("plus",),
        ("max", "ultra", "flagship", "advanced"),
        ("turbo",),
        ("chat",),
        ("mini", "flash", "lite", "small"),
    )

    for keyword_group in preferred_keyword_groups:
        for model in model_ids:
            tokens = token_map.get(model, [])
            if any(keyword in tokens for keyword in keyword_group):
                return model

    return model_ids[0] if model_ids else current


def is_lightweight_ai_model(model: str) -> bool:
    """判断是否是 mini/flash/lite/turbo 等轻量模型，用于参考语料翻译场景的提醒。"""
    tokens = [item for item in re_split_model_name((model or "").lower()) if item]
    lightweight_tokens = {"mini", "flash", "lite", "turbo", "small"}
    return any(token in lightweight_tokens for token in tokens)


def is_free_machine_translation_config(config) -> bool:
    return bool(config and machine_translate.is_machine_translation_provider(config.provider_id))


def translation_provider_label(provider_id: str) -> str:
    provider_id = (provider_id or "").strip().lower()
    if machine_translate.is_machine_translation_provider(provider_id):
        return machine_translate.provider_label(provider_id)
    return AI_services.get_provider_spec(provider_id).display_name


class _EmbeddedAppConfig:
    APP_DIR = APP_DIR
    ProviderSettings = ProviderSettings
    AppSettings = AppSettings
    load_settings = staticmethod(load_settings)
    save_settings = staticmethod(save_settings)
    secret_path = staticmethod(secret_path)
    save_secret = staticmethod(save_secret)
    load_secret = staticmethod(load_secret)
    secret_is_session_only = staticmethod(secret_is_session_only)
    secret_storage_error = staticmethod(secret_storage_error)
    save_mineru_token = staticmethod(save_mineru_token)
    load_mineru_token = staticmethod(load_mineru_token_from_settings)
    remember_recent_file = staticmethod(remember_recent_file)
    work_dir_path = staticmethod(work_dir_path)
    chat_history_path = staticmethod(chat_history_path)
    choose_preferred_model = staticmethod(choose_preferred_model)


app_config = _EmbeddedAppConfig()


class PreviewMode(Enum):
    ORIGINAL = "original"
    PARSED = "parsed"


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp"}
PANDOC_OFFICE_SUFFIXES = {".docx", ".doc", ".html", ".htm", ".epub"}
DEFAULT_EXPORT_IMAGE_WIDTH = "45%"
EXPORT_FONT_CJK_OPTIONS = ["宋体", "黑体", "仿宋", "楷体", "微软雅黑"]
EXPORT_FONT_LATIN_OPTIONS = ["Times New Roman", "Arial", "Calibri", "Cambria", "Georgia"]
EXPORT_FONT_SIZE_OPTIONS = [
    ("五号", 10),
    ("小四", 12),
    ("四号", 14),
    ("小三", 15),
    ("三号", 16),
    ("小二", 18),
    ("二号", 22),
]
EXPORT_FONT_SIZE_LABEL_TO_PT = {label: pt for label, pt in EXPORT_FONT_SIZE_OPTIONS}


def export_font_size_label_from_pt(value: int, fallback: str = "小四") -> str:
    """把内部 pt 数值映射为中文字号名称，便于界面展示。"""
    for label, pt in EXPORT_FONT_SIZE_OPTIONS:
        if pt == int(value):
            return label
    return fallback


def export_font_size_pt_from_label(label: str, default: int) -> int:
    """把中文字号名称映射回内部 pt 数值，便于继续复用现有导出逻辑。"""
    return EXPORT_FONT_SIZE_LABEL_TO_PT.get((label or "").strip(), int(default))


def find_pandoc_for_workspace(workspace: Path) -> Path | None:
    for candidate in (workspace / "resources" / "pandoc.exe",):
        if candidate.exists():
            return candidate
    system_pandoc = shutil.which("pandoc")
    if system_pandoc:
        return Path(system_pandoc)
    return None


def hidden_subprocess_kwargs() -> dict:
    kwargs: dict = {}
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


__all__ = [name for name in globals() if not name.startswith("__")]
