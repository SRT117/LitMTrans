from __future__ import annotations

"""LitMTrans document-chat settings, secrets, and interface utilities."""

import os
import sys
import json
import re
import base64
import mimetypes
import time
import ctypes
import shutil
import http.client
import urllib.request
import urllib.error
import urllib.parse
import zipfile
import uuid
from datetime import datetime

from dataclasses import asdict, dataclass, field
from pathlib import Path
from workspace_paths import configured_work_dir, default_workspace_path
from typing import Callable
from urllib.parse import urlparse

from app_version import APP_NAME, APP_VERSION
from secret_session import SESSION_SECRETS, STORAGE_ERRORS

from PySide6.QtCore import Qt, QThread, QObject, QEvent, Signal, QByteArray, QBuffer, QIODevice, QTimer, QPoint, QSettings, qInstallMessageHandler, QSize, QUrl
from PySide6.QtGui import QPixmap, QAction, QImage, QImageReader, QTextOption, QCursor, QFontDatabase, QColor, QTextCursor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QTextBrowser,
    QTextEdit,
    QPlainTextEdit,
    QPushButton,
    QComboBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QFileDialog,
    QScrollArea,
    QFrame,
    QGraphicsDropShadowEffect,
    QMenu,
    QSizePolicy,
    QCheckBox,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QToolButton,
    QToolTip,
)

APP_DATA_ROOT = Path(os.environ.get("APPDATA", str(Path.home())))
APP_DIR = APP_DATA_ROOT / "LitMTrans"
LEGACY_APP_DIRS = (
    APP_DATA_ROOT / "LiteratureTranslationReadingWorkbench",
    APP_DATA_ROOT / "MinerUResearchWorkbench",
    APP_DATA_ROOT / "LiteratureWorkbench",
)
SETTINGS_PATH = APP_DIR / "settings_chat.json"
LEGACY_SETTINGS_PATH = APP_DIR / "settings_ai_chat.json"
LEGACY_SETTINGS_PATHS = (
    LEGACY_SETTINGS_PATH,
    *(directory / "settings_ai_chat.json" for directory in LEGACY_APP_DIRS),
    *(directory / "settings.json" for directory in LEGACY_APP_DIRS),
)
MINERU_PROVIDER_ID = "mineru"
API_KEY_SECRET_NAME = "api_key"
_SESSION_SECRETS = SESSION_SECRETS
_SECRET_STORAGE_ERRORS = STORAGE_ERRORS
FRESH_USER_DEBUG_ENV_VAR = "LITMTRANS_FRESH_USER_DEBUG"
_FRESH_USER_DEBUG_MESSAGE = "新用户调试模式：密钥仅在本次运行有效，不会写入本机配置。"
_FRESH_USER_DEBUG_SETTINGS = None


def fresh_user_debug_enabled() -> bool:
    """Return whether this process must behave like a newly installed app."""
    return os.environ.get(FRESH_USER_DEBUG_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}

APP_UI_FONT_FAMILY_STACK = (
    '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", '
    '"Noto Sans CJK SC", "Noto Sans", "Yu Gothic UI", "Malgun Gothic", '
    'Arial, sans-serif'
)
APP_DISPLAY_FONT_FAMILY_STACK = (
    '"Segoe UI Variable Display", "Segoe UI Variable Text", "Segoe UI", '
    '"Microsoft YaHei UI", "Noto Sans CJK SC", Arial, sans-serif'
)
APP_MONO_FONT_FAMILY_STACK = (
    '"Cascadia Mono", "Consolas", "Noto Sans Mono CJK SC", '
    '"Sarasa Mono SC", "SimSun", monospace'
)
APP_SERIF_FONT_FAMILY_STACK = (
    '"Source Han Serif CN", "Times New Roman", "Noto Serif CJK SC", "Source Han Serif SC", '
    '"SimSun", "PMingLiU", "MingLiU", "Yu Mincho", "MS Mincho", '
    '"Batang", "Times", serif'
)
APP_UI_SYMBOL_FALLBACK_STACK = '"Segoe UI Symbol", "Noto Sans Symbols 2", "Arial Unicode MS"'


# =============================================================================
# Shared application style constants.
# =============================================================================
# The interface uses a sans-serif UI font; document content keeps its serif stack.
COLOR_BG_BASE = "#F5F5F2"
COLOR_BG_SURFACE = "#FAFAF8"
COLOR_BG_SURFACE_2 = "#FFFFFF"
COLOR_BG_INSET = "#EFEFEC"

COLOR_BORDER_HAIR = "#D6D6D0"
COLOR_BORDER_STRONG = "#111111"

COLOR_TEXT_PRIMARY = "#0A0A0A"
COLOR_TEXT_SECONDARY = "#3D3D3A"
COLOR_TEXT_MUTED = "#74746E"
COLOR_TEXT_DISABLED = "#A7A7A1"

COLOR_ACCENT = "#0A0A0A"
COLOR_ACCENT_HOVER = "#242424"
COLOR_ACCENT_PRESS = "#000000"
COLOR_ACCENT_SOFT = "#E8E8E4"
COLOR_ACCENT_SOFT_WEAK = "#F0F0ED"

COLOR_DANGER = "#B42318"
COLOR_DANGER_SOFT = "#F7E7E5"
COLOR_SUCCESS = "#137333"

# Keep corners square or nearly square across the interface.
RADIUS_SM = "0px"
RADIUS_MD = "2px"
RADIUS_LG = "2px"
RADIUS_XL = "2px"
RADIUS_PILL = "2px"

# Use borders and spacing for hierarchy instead of drop shadows.
ELEVATION_PROFILES = {
    "card": (0, 0, 0.0),
    "raised": (0, 0, 0.0),
    "overlay": (0, 0, 0.0),
}


def apply_elevation(widget: QWidget, level: str = "card") -> QGraphicsDropShadowEffect | None:
    """Remove any shadow effect; the application style uses borders for hierarchy."""
    widget.setGraphicsEffect(None)
    if hasattr(widget, "_zcode_elevation"):
        widget._zcode_elevation = None  # type: ignore[attr-defined]
    return None


def remove_elevation(widget: QWidget) -> None:
    """Detach a previously attached elevation shadow, if any."""
    widget.setGraphicsEffect(None)
    if hasattr(widget, "_zcode_elevation"):
        widget._zcode_elevation = None  # type: ignore[attr-defined]


def build_dark_premium_stylesheet() -> str:
    """Build the shared application stylesheet for the workbench and readers."""
    return f"""
    * {{
        outline: 0;
    }}
    QWidget {{
        background: {COLOR_BG_BASE};
        color: {COLOR_TEXT_PRIMARY};
        font-family: {APP_UI_FONT_FAMILY_STACK}, {APP_UI_SYMBOL_FALLBACK_STACK};
        font-size: 14px;
    }}
    QLabel {{
        background: transparent;
    }}
    QDialog, QMainWindow, QWidget#cardShell, QWidget#readerShell, QWidget#chatContainer {{
        background: {COLOR_BG_BASE};
    }}
    QFrame {{
        background: transparent;
    }}

    /* 输入区：纯白平面、1px 边界、无软阴影。 */
    QLineEdit, QComboBox, QAbstractSpinBox, QTextEdit, QTextBrowser, QPlainTextEdit {{
        background: {COLOR_BG_SURFACE_2};
        color: {COLOR_TEXT_PRIMARY};
        border: 1px solid {COLOR_BORDER_HAIR};
        border-radius: {RADIUS_MD};
        selection-background-color: {COLOR_ACCENT};
        selection-color: #FFFFFF;
    }}
    QLineEdit, QComboBox, QAbstractSpinBox {{
        min-height: 34px;
        padding: 4px 10px;
        font-size: 14px;
        font-weight: 600;
    }}
    QTextEdit, QTextBrowser, QPlainTextEdit {{
        padding: 9px;
    }}
    QLineEdit:hover, QComboBox:hover, QAbstractSpinBox:hover,
    QTextEdit:hover, QTextBrowser:hover, QPlainTextEdit:hover {{
        border-color: #90908A;
    }}
    QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus,
    QTextEdit:focus, QTextBrowser:focus, QPlainTextEdit:focus {{
        border-color: {COLOR_BORDER_STRONG};
    }}
    QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled,
    QTextEdit:disabled, QTextBrowser:disabled, QPlainTextEdit:disabled {{
        color: {COLOR_TEXT_DISABLED};
        background: {COLOR_BG_INSET};
        border-color: {COLOR_BORDER_HAIR};
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 24px;
        border: none;
        border-left: 1px solid {COLOR_BORDER_HAIR};
        background: {COLOR_BG_SURFACE};
        border-top-right-radius: {RADIUS_MD};
        border-bottom-right-radius: {RADIUS_MD};
    }}
    QComboBox::down-arrow {{
        image: none;
        width: 0px;
        height: 0px;
    }}
    QComboBox QAbstractItemView {{
        background: {COLOR_BG_SURFACE_2};
        color: {COLOR_TEXT_PRIMARY};
        border: 1px solid {COLOR_BORDER_STRONG};
        border-radius: 0px;
        padding: 0px;
        outline: 0;
        selection-background-color: {COLOR_ACCENT};
        selection-color: #FFFFFF;
    }}
    QComboBox QAbstractItemView::item {{
        min-height: 28px;
        padding: 5px 10px;
        border-radius: 0px;
    }}

    /* 按钮：默认白底；悬停与选中立即反相，反馈清晰。 */
    QPushButton, QToolButton {{
        min-height: 34px;
        background: {COLOR_BG_SURFACE_2};
        color: {COLOR_TEXT_PRIMARY};
        border: 1px solid {COLOR_BORDER_HAIR};
        border-radius: {RADIUS_MD};
        padding: 5px 13px;
        font-size: 14px;
        font-weight: 700;
    }}
    QPushButton:hover, QToolButton:hover {{
        background: {COLOR_ACCENT};
        color: #FFFFFF;
        border-color: {COLOR_ACCENT};
    }}
    QPushButton:pressed, QToolButton:pressed {{
        background: {COLOR_ACCENT_PRESS};
        color: #FFFFFF;
        border-color: {COLOR_ACCENT_PRESS};
    }}
    QPushButton:checked, QToolButton:checked {{
        background: {COLOR_ACCENT};
        color: #FFFFFF;
        border-color: {COLOR_ACCENT};
    }}
    QPushButton:disabled, QToolButton:disabled {{
        color: {COLOR_TEXT_DISABLED};
        background: {COLOR_BG_INSET};
        border-color: {COLOR_BORDER_HAIR};
    }}
    QPushButton#primaryButton, QPushButton#accentButton, QPushButton#sendButton {{
        background: {COLOR_ACCENT};
        color: #FFFFFF;
        border-color: {COLOR_ACCENT};
        font-weight: 700;
    }}
    QPushButton#primaryButton:hover, QPushButton#accentButton:hover, QPushButton#sendButton:hover {{
        background: {COLOR_ACCENT_HOVER};
        border-color: {COLOR_ACCENT_HOVER};
    }}
    QPushButton#dangerButton {{
        background: transparent;
        color: {COLOR_DANGER};
        border-color: {COLOR_DANGER};
    }}
    QPushButton#dangerButton:hover {{
        background: {COLOR_DANGER};
        color: #FFFFFF;
        border-color: {COLOR_DANGER};
    }}
    QPushButton#exportButton {{
        background: {COLOR_BG_SURFACE_2};
        color: {COLOR_TEXT_PRIMARY};
        border-color: {COLOR_BORDER_STRONG};
    }}

    /* 容器只依赖网格线，不使用悬浮卡片。 */
    QGroupBox {{
        background: {COLOR_BG_SURFACE_2};
        color: {COLOR_TEXT_PRIMARY};
        border: 1px solid {COLOR_BORDER_HAIR};
        border-radius: 0px;
        margin-top: 11px;
        padding: 13px 10px 10px 10px;
        font-weight: 650;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 8px;
        padding: 0 4px;
        color: {COLOR_TEXT_SECONDARY};
        background: {COLOR_BG_BASE};
        font-family: {APP_MONO_FONT_FAMILY_STACK};
        font-size: 11px;
        font-weight: 700;
    }}
    QScrollArea {{
        background: {COLOR_BG_SURFACE_2};
        border: 1px solid {COLOR_BORDER_HAIR};
        border-radius: 0px;
    }}
    QScrollArea > QWidget > QWidget {{
        background: transparent;
    }}

    QListWidget, QListView {{
        background: {COLOR_BG_SURFACE_2};
        color: {COLOR_TEXT_PRIMARY};
        border: 1px solid {COLOR_BORDER_HAIR};
        border-radius: 0px;
        padding: 0px;
        outline: 0;
    }}
    QListWidget::item, QListView::item {{
        min-height: 30px;
        padding: 6px 9px;
        border-radius: 0px;
        border-bottom: 1px solid {COLOR_ACCENT_SOFT_WEAK};
        color: {COLOR_TEXT_PRIMARY};
    }}
    QListWidget::item:selected, QListView::item:selected {{
        background: {COLOR_ACCENT};
        color: #FFFFFF;
        border-color: {COLOR_ACCENT};
    }}
    QListWidget::item:hover, QListView::item:hover {{
        background: {COLOR_ACCENT_SOFT};
        color: {COLOR_TEXT_PRIMARY};
    }}

    /* 8px 窄滚动条，滑块保持直角。 */
    QScrollBar:vertical {{
        background: {COLOR_BG_INSET};
        width: 8px;
        margin: 0px;
        border: none;
    }}
    QScrollBar::handle:vertical {{
        background: #8A8A84;
        border-radius: 0px;
        min-height: 32px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {COLOR_ACCENT};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
        background: transparent;
        border: none;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: transparent;
    }}
    QScrollBar:horizontal {{
        background: {COLOR_BG_INSET};
        height: 8px;
        margin: 0px;
        border: none;
    }}
    QScrollBar::handle:horizontal {{
        background: #8A8A84;
        border-radius: 0px;
        min-width: 32px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {COLOR_ACCENT};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
        background: transparent;
        border: none;
    }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
        background: transparent;
    }}

    QMenu {{
        background: {COLOR_BG_SURFACE_2};
        color: {COLOR_TEXT_PRIMARY};
        border: 1px solid {COLOR_BORDER_STRONG};
        border-radius: 0px;
        padding: 2px;
    }}
    QMenu::item {{
        padding: 7px 18px;
        border-radius: 0px;
    }}
    QMenu::item:selected {{
        background: {COLOR_ACCENT};
        color: #FFFFFF;
    }}
    QMenu::separator {{
        height: 1px;
        background: {COLOR_BORDER_HAIR};
        margin: 3px 6px;
    }}
    QToolTip {{
        color: #FFFFFF;
        background: {COLOR_ACCENT};
        border: 1px solid {COLOR_ACCENT};
        padding: 5px 8px;
        font-family: {APP_UI_FONT_FAMILY_STACK};
    }}
    QDialogButtonBox QPushButton {{
        min-width: 88px;
    }}

    QProgressBar {{
        background: {COLOR_BG_INSET};
        color: {COLOR_TEXT_SECONDARY};
        border: 1px solid {COLOR_BORDER_HAIR};
        border-radius: 0px;
        min-height: 11px;
        max-height: 11px;
        text-align: center;
        font-family: {APP_MONO_FONT_FAMILY_STACK};
        font-size: 9px;
    }}
    QProgressBar::chunk {{
        background: {COLOR_ACCENT};
        border-radius: 0px;
    }}

    QCheckBox, QRadioButton {{
        color: {COLOR_TEXT_PRIMARY};
        background: transparent;
        spacing: 6px;
    }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 13px;
        height: 13px;
        border: 1px solid #85857F;
        background: {COLOR_BG_SURFACE_2};
        border-radius: 0px;
    }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
        border-color: {COLOR_ACCENT};
    }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {COLOR_ACCENT};
        border-color: {COLOR_ACCENT};
    }}

    QSplitter::handle {{
        background: {COLOR_BORDER_HAIR};
    }}
    QSplitter::handle:hover {{
        background: {COLOR_ACCENT};
    }}
    """


def apply_monochrome_app_style(app: QApplication) -> None:
    """
    Apply the unified paper research base theme for the whole Qt app.

    Despite the legacy name, this now installs the paper research design system.
    The name is kept unchanged so existing call sites continue to work. Every
    window and inline widget should reuse build_dark_premium_stylesheet() for
    consistency.
    """
    app.setStyleSheet(build_dark_premium_stylesheet())


_silent_message_box_filter = None
_silent_message_boxes_installed = False


def create_silent_message_box(parent=None) -> QMessageBox:
    """创建不会调用 Windows 系统提示音的消息框。"""
    box = QMessageBox(parent)
    box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    return box


class _SilentMessageBoxFilter(QObject):
    """Keep manually-created message boxes silent without affecting file dialogs."""

    def eventFilter(self, obj, event):
        if isinstance(obj, QMessageBox) and event.type() == QEvent.Type.Polish:
            obj.setOption(QMessageBox.Option.DontUseNativeDialog, True)
        return super().eventFilter(obj, event)


def configure_silent_application() -> None:
    """仅让消息提示框静音，保留系统原生文件选择器。

    Windows 原生 QMessageBox 会播放“信息/警告/错误”系统声音。这里将
    QMessageBox 的便捷方法和手动创建的消息框改为 Qt 绘制；QFileDialog 等
    文件选择窗口不受影响，继续使用用户熟悉的系统原生界面。
    """
    global _silent_message_box_filter, _silent_message_boxes_installed
    if _silent_message_boxes_installed:
        return

    app = QApplication.instance()
    if app is None:
        raise RuntimeError("configure_silent_application() 必须在 QApplication 创建后调用。")

    def show_silent_message(icon, parent, title, text, buttons=QMessageBox.StandardButton.Ok,
                            default_button=QMessageBox.StandardButton.NoButton):
        # The five-argument overload is (icon, title, text, buttons, parent).
        # Passing parent as the fourth argument selects no valid overload.
        box = create_silent_message_box(parent)
        box.setIcon(icon)
        box.setWindowTitle(str(title))
        box.setText(str(text))
        box.setStandardButtons(buttons)
        if default_button != QMessageBox.StandardButton.NoButton:
            box.setDefaultButton(default_button)
        return box.exec()

    def make_static_message(icon):
        def show(parent, title, text, buttons=QMessageBox.StandardButton.Ok,
                 defaultButton=QMessageBox.StandardButton.NoButton):
            return show_silent_message(icon, parent, title, text, buttons, defaultButton)
        return staticmethod(show)

    QMessageBox.information = make_static_message(QMessageBox.Icon.Information)
    QMessageBox.warning = make_static_message(QMessageBox.Icon.Warning)
    QMessageBox.critical = make_static_message(QMessageBox.Icon.Critical)
    QMessageBox.question = make_static_message(QMessageBox.Icon.Question)

    def about(parent, title, text):
        show_silent_message(QMessageBox.Icon.Information, parent, title, text)

    QMessageBox.about = staticmethod(about)
    _silent_message_box_filter = _SilentMessageBoxFilter(app)
    app.installEventFilter(_silent_message_box_filter)
    _silent_message_boxes_installed = True



def get_base_path() -> str:
    """
    获取程序资源根目录。

    说明：
    1. 普通源码运行时返回当前 py 文件所在目录。
    2. PyInstaller 打包运行时优先返回临时解包目录。
    3. Built-in prompt actions use it to locate bundled resources.
    """
    if hasattr(sys, "_MEIPASS"):
        return str(getattr(sys, "_MEIPASS"))
    return str(Path(__file__).resolve().parent)


GOOGLE_SANS_CODE_FAMILY = "Google Sans Code"
APP_FONT_FAMILY_STACK = APP_MONO_FONT_FAMILY_STACK


def apply_google_sans_code_font(app: QApplication, point_size: int = 10) -> str:
    """
    兼容旧函数名：加载可选内嵌字体，但应用主字体始终优先选择覆盖面更广的 UI 无衬线字体。

    可选字体文件放在 resources/fonts 或 resources 根目录即可；缺失时直接使用系统字体，
    不会因为某个拉丁字体缺少中日韩字符而导致字号和字面高度跳变。
    """
    if app is None:
        return ""

    resources_dir = Path(get_base_path()) / "resources"
    font_dirs = [resources_dir / "fonts", resources_dir]
    optional_font_names = (
        "SourceHanSerifCN-Regular.ttf",
        "NotoSansCJKsc-Regular.otf",
        "NotoSansCJKsc-Medium.otf",
        "NotoSerifCJKsc-Regular.otf",
        "GoogleSansCode-VariableFont_wght.ttf",
        "GoogleSansCode-Italic-VariableFont_wght.ttf",
    )

    for font_dir in font_dirs:
        for name in optional_font_names:
            font_path = font_dir / name
            if font_path.exists():
                QFontDatabase.addApplicationFont(str(font_path))

    available = set(QFontDatabase.families())
    preferred_families = (
        "Segoe UI Variable Text",
        "Segoe UI",
        "Microsoft YaHei UI",
        "Noto Sans CJK SC",
        "Noto Sans",
        "Arial",
    )
    family = next((name for name in preferred_families if name in available), app.font().family())

    font = app.font()
    font.setFamily(family)
    font.setPointSize(max(1, int(point_size)))
    app.setFont(font)
    ensure_valid_application_font(point_size)
    return family


def install_qt_warning_filter() -> None:
    """
    过滤少量 Qt 内部无害警告，避免控制台被误导性信息干扰。

    说明：
    1. QFont::setPointSize(-1) 常来自系统默认字体或外部宿主程序，不影响界面实际显示。
    2. QTextCursor::setPosition out of range 常见于富文本控件内容刷新期间的 Qt 内部光标同步。
    3. 这里只过滤已知无害警告，其他 Qt 消息仍正常输出，便于排查真正问题。
    """
    def message_handler(mode, context, message):
        text = str(message)

        ignored_messages = (
            "QFont::setPointSize: Point size <= 0",
            "QTextCursor::setPosition: Position",
        )

        if any(item in text for item in ignored_messages):
            return

        print(text, flush=True)

    qInstallMessageHandler(message_handler)


def ensure_valid_application_font(point_size: int = 10) -> None:
    """
    修复部分运行环境中 QApplication 默认字体 pointSize 为 -1 的问题。

    Qt 要求 QFont.setPointSize() 的参数必须大于 0。
    某些主题、系统缩放或外部宿主程序可能传入无效字号。
    """
    app = QApplication.instance()
    if app is None:
        return

    font = app.font()

    # pointSize 为 -1 时不读取它作为新字号，直接使用明确合法的字号。
    if font.pointSize() <= 0:
        font.setPointSize(max(1, int(point_size)))
        app.setFont(font)


class _ComboPopupFilter(QObject):
    """让下拉框点击任意位置时都展开，并避免按下阶段触发后立刻收起。"""

    def __init__(self, combo: QComboBox):
        super().__init__(combo)

        # The Python wrapper can outlive the underlying Qt object.
        self.combo = combo
        combo.destroyed.connect(self._on_combo_destroyed)

    def _on_combo_destroyed(self):
        """下拉框销毁后清空引用，避免事件过滤器访问已删除的 C++ 对象。"""
        self.combo = None

    def _safe_combo(self) -> QComboBox | None:
        """安全取得下拉框；如果底层 C++ 对象已删除则返回 None。"""
        combo = self.combo
        if combo is None:
            return None

        try:
            # 触碰一个轻量属性，用于检测包装对象是否仍有效。
            combo.isEnabled()
        except RuntimeError:
            self.combo = None
            return None

        return combo

    def _show_popup_safely(self):
        """
        延迟打开下拉框。

        QTimer.singleShot 触发时，下拉框可能已经被销毁；
        因此这里必须重新做有效性检查。
        """
        combo = self._safe_combo()
        if combo is None:
            return

        try:
            if combo.isEnabled():
                combo.showPopup()
        except RuntimeError:
            self.combo = None

    def eventFilter(self, obj, event):
        combo = self._safe_combo()

        # Let Qt handle events after the control has been destroyed.
        if combo is None:
            return super().eventFilter(obj, event)

        # 只处理左键鼠标事件，其他事件全部交给 Qt 默认处理。
        try:
            if not combo.isEnabled():
                return super().eventFilter(obj, event)
        except RuntimeError:
            self.combo = None
            return super().eventFilter(obj, event)

        # Open on release, after the current mouse event has finished.
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            QTimer.singleShot(0, self._show_popup_safely)
            return True

        # Prevent an editable combo from entering text-edit mode on press.
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            return True

        return super().eventFilter(obj, event)


def make_combo_popup_on_click(combo: QComboBox, *, editable: bool = False) -> QComboBox:
    """统一下拉框交互：点击任意位置展开，不显示可编辑光标。"""
    combo.setEditable(editable)
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    combo.setCursor(Qt.CursorShape.PointingHandCursor)
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow)

    click_filter = _ComboPopupFilter(combo)

    # 必须把过滤器挂到 combo 属性上保存引用，否则 Python 可能提前回收过滤器。
    combo._popup_click_filter = click_filter  # type: ignore[attr-defined]
    combo.installEventFilter(click_filter)

    # Clear the filter reference when Qt destroys the combo.
    combo.destroyed.connect(lambda _=None: setattr(combo, "_popup_click_filter", None))

    line_edit = combo.lineEdit()
    if line_edit is not None:
        # Keep an editable combo visually consistent with a normal combo.
        line_edit.setReadOnly(True)
        line_edit.setCursor(Qt.CursorShape.PointingHandCursor)

        # Remove the internal line-edit frame and padding so labels align.
        line_edit.setFrame(False)
        line_edit.setContentsMargins(0, 0, 0, 0)
        line_edit.setStyleSheet("""
            QLineEdit {
                border: none;
                background: transparent;
                padding: 0px;
                margin: 0px;
            }
        """)

        line_edit.installEventFilter(click_filter)

    return combo


ONEAPI_REQUEST_BODY_MODE_CODEX = "codex"
ONEAPI_REQUEST_BODY_MODE_CLAUDE = "claude"


def normalize_oneapi_request_body_mode(value) -> str:
    """Return the request format used by the OneAPI-compatible endpoint."""
    return ONEAPI_REQUEST_BODY_MODE_CODEX


def edit_oneapi_request_body_mode(parent, current_mode: str) -> str | None:
    """Return the only supported request format."""
    return ONEAPI_REQUEST_BODY_MODE_CODEX


@dataclass
class ProviderSettings:
    provider_id: str = "oneapi"
    base_url: str = ""
    model: str = ""
    # OneAPI 可按上游协议构造请求；其他服务商固定走其既有标准路径。
    request_body_mode: str = "codex"


@dataclass
class ProviderCard:
    card_id: str = ""
    name: str = ""
    provider_id: str = "oneapi"
    base_url: str = ""


@dataclass
class AppSettings:
    work_dir: str = ""
    mineru_model: str = "vlm"
    ai_provider: str = "deepseek"
    providers: dict[str, ProviderSettings] = field(default_factory=dict)
    # 翻译和对话可使用同一服务商凭据，但它们的“当前服务商 / 模型”是独立选择。
    # ai_provider / providers 保持为翻译配置，以兼容既有工作流和旧设置文件。
    chat_provider: str = "deepseek"
    chat_providers: dict[str, ProviderSettings] = field(default_factory=dict)
    # Per provider-and-model chat preferences for public reasoning controls.
    chat_reasoning_preferences: dict[str, dict] = field(default_factory=dict)
    provider_cards: list[ProviderCard] = field(default_factory=list)
    key_points_prompt: str = ""
    recent_files: list[str] = field(default_factory=list)
    batch_concurrency: int = 1
    translation_source_language: str = "英文"
    translation_target_language: str = "简体中文"
    translation_mode: str = "full_context"
    translation_reference_paths: list[str] = field(default_factory=list)
    # 用户自定义翻译指令会随翻译请求发送给大模型；免费机翻模式仅保存、不使用。
    translation_custom_instruction: str = ""
    # 关闭高速排版后，恢复为服务商默认强度的 DeepSeek 思考请求。
    translation_deepseek_thinking_enabled: bool = True
    translation_deepseek_reasoning_effort: str = "default"
    # Available only for the official DeepSeek provider and consumed solely by
    # layout-preserving translation.
    translation_deepseek_fast_layout_enabled: bool = True
    # Gemini 翻译思考设置独立保存，切换服务商后仍可恢复。
    translation_gemini_thinking_enabled: bool = False
    translation_gemini_reasoning_effort: str = "medium"
    local_machine_parallelism: int = 4
    show_parsed_source: bool = False
    sync_scroll: bool = True
    reader_font_pt: int = 12
    # Reader mode settings are also used by the embedded document-chat panel.
    layout_reading_mode: bool = True
    layout_show_parsed_source: bool = False
    stream_show_parsed_source: bool = False
    stream_sync_scroll: bool = True
    show_layout_restoration: bool = True
    layout_development_mode: bool = False
    reader_scroll_positions: dict[str, dict[str, float]] = field(default_factory=dict)
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


def _settings_from_dict(data: dict) -> AppSettings:
    def parse_providers(raw_providers) -> dict[str, ProviderSettings]:
        parsed = {}
        for provider_id, raw in (raw_providers or {}).items():
            if isinstance(raw, dict):
                parsed[provider_id] = ProviderSettings(
                    provider_id=provider_id,
                    base_url=str(raw.get("base_url") or ""),
                    model=str(raw.get("model") or ""),
                    request_body_mode=normalize_oneapi_request_body_mode(raw.get("request_body_mode")),
                )
        return parsed

    providers = parse_providers(data.get("providers"))
    chat_providers = parse_providers(data.get("chat_providers"))
    raw_reasoning_preferences = data.get("chat_reasoning_preferences")
    chat_reasoning_preferences = {
        str(key): dict(value)
        for key, value in (raw_reasoning_preferences or {}).items()
        if isinstance(value, dict)
    }
    # Migrate a single legacy provider map into the document-chat provider map.
    if not chat_providers:
        chat_providers = {
            provider_id: ProviderSettings(item.provider_id, item.base_url, item.model, item.request_body_mode)
            for provider_id, item in providers.items()
        }

    provider_cards = []
    for raw in data.get("provider_cards") or []:
        if not isinstance(raw, dict):
            continue
        card_id = str(raw.get("card_id") or "").strip()
        if not card_id:
            card_id = uuid.uuid4().hex
        provider_cards.append(
            ProviderCard(
                card_id=card_id,
                name=str(raw.get("name") or ""),
                provider_id=str(raw.get("provider_id") or "oneapi"),
                base_url=str(raw.get("base_url") or ""),
            )
        )

    # The former individual webpage providers are now one resilient route:
    # Google first, then Bing when Google is unreachable.
    ai_provider = str(data.get("ai_provider") or "deepseek")
    if ai_provider.strip().lower() in {"google_free", "bing_free"} or ai_provider.strip().lower().endswith("_web"):
        ai_provider = "free_machine"

    return AppSettings(
        mineru_model=str(data.get("mineru_model") or "vlm"),
        work_dir=str(data.get("work_dir") or ""),
        ai_provider=ai_provider,
        providers=providers,
        chat_provider=str(data.get("chat_provider") or data.get("ai_provider") or "deepseek"),
        chat_providers=chat_providers,
        chat_reasoning_preferences=chat_reasoning_preferences,
        provider_cards=provider_cards,
        key_points_prompt=str(data.get("key_points_prompt") or ""),
        recent_files=[str(item) for item in data.get("recent_files") or []],
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
        local_machine_parallelism=max(1, min(28, int(data.get("local_machine_parallelism") or 4))),
        show_parsed_source=bool(data.get("show_parsed_source", False)),
        sync_scroll=bool(data.get("sync_scroll", True)),
        reader_font_pt=max(9, int(data.get("reader_font_pt") or 12)),
        layout_reading_mode=bool(data.get("layout_reading_mode", True)),
        layout_show_parsed_source=bool(data.get("layout_show_parsed_source", False)),
        stream_show_parsed_source=bool(data.get("stream_show_parsed_source", False)),
        stream_sync_scroll=bool(data.get("stream_sync_scroll", True)),
        show_layout_restoration=bool(data.get("show_layout_restoration", True)),
        layout_development_mode=bool(data.get("layout_development_mode", False)),
        reader_scroll_positions={
            str(key): {
                "ratio": max(0.0, min(1.0, float((value or {}).get("ratio") or 0))),
                "top": max(0.0, float((value or {}).get("top") or 0)),
            }
            for key, value in (data.get("reader_scroll_positions") or {}).items()
            if isinstance(value, dict)
        },
        auto_check_updates=bool(data.get("auto_check_updates", True)),
        update_mirror_acceleration=bool(data.get("update_mirror_acceleration", True)),
    )


def load_settings() -> AppSettings:
    global _FRESH_USER_DEBUG_SETTINGS
    if fresh_user_debug_enabled():
        if _FRESH_USER_DEBUG_SETTINGS is None:
            _FRESH_USER_DEBUG_SETTINGS = AppSettings()
        return _FRESH_USER_DEBUG_SETTINGS

    primary_settings = None
    for candidate in (SETTINGS_PATH, *LEGACY_SETTINGS_PATHS):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                primary_settings = _settings_from_dict(data)
                break
        except Exception:
            pass

    if primary_settings is None:
        primary_settings = AppSettings()

    if not primary_settings.work_dir:
        workbench_candidates = (
            APP_DIR / "settings.json",
            APP_DIR / "settings_workbench.json",
            *(directory / "settings_workbench.json" for directory in LEGACY_APP_DIRS),
            *(directory / "settings.json" for directory in LEGACY_APP_DIRS),
        )
        primary_settings.work_dir = configured_work_dir((*LEGACY_SETTINGS_PATHS, *workbench_candidates))

    return primary_settings


def save_settings(settings: AppSettings) -> None:
    global _FRESH_USER_DEBUG_SETTINGS
    if fresh_user_debug_enabled():
        _FRESH_USER_DEBUG_SETTINGS = settings
        return
    APP_DIR.mkdir(parents=True, exist_ok=True)
    data = asdict(settings)
    data["providers"] = {key: asdict(value) for key, value in settings.providers.items()}
    data["chat_providers"] = {key: asdict(value) for key, value in settings.chat_providers.items()}
    data["provider_cards"] = [asdict(value) for value in settings.provider_cards]
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


def delete_secret(provider_id: str, name: str) -> None:
    key = _secret_key(provider_id, name)
    _SESSION_SECRETS.pop(key, None)
    _SECRET_STORAGE_ERRORS.pop(key, None)
    if fresh_user_debug_enabled():
        return
    path = secret_path(provider_id, name)
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def secret_is_session_only(provider_id: str, name: str) -> bool:
    return _secret_key(provider_id, name) in _SECRET_STORAGE_ERRORS


def secret_storage_error(provider_id: str, name: str) -> str:
    return _SECRET_STORAGE_ERRORS.get(_secret_key(provider_id, name), "")


def save_mineru_token(value: str) -> str:
    saved = save_secret(MINERU_PROVIDER_ID, API_KEY_SECRET_NAME, value.strip())
    return str(secret_path(MINERU_PROVIDER_ID, API_KEY_SECRET_NAME)) if saved else ""


def load_mineru_token() -> str:
    return load_secret(MINERU_PROVIDER_ID, API_KEY_SECRET_NAME).strip()


API_V4_BASE_URL = "https://mineru.net/api/v4"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"
DEFAULT_MODEL_VERSION = "vlm"

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


class MinerUError(RuntimeError):
    pass


@dataclass
class ParseOptions:
    model_version: str = DEFAULT_MODEL_VERSION
    enable_table: bool = True
    enable_formula: bool = True
    is_ocr: bool = False
    timeout_seconds: int = 1800
    poll_interval_seconds: int = 5


def mineru_is_supported_input_file(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_INPUT_EXTENSIONS


def http_json(method: str, url: str, payload: dict | None = None, token: str | None = None, timeout: int = 60) -> dict:
    data = None
    headers = {"User-Agent": USER_AGENT}
    if token:
        if not token.isascii():
            raise MinerUError("MinerU 访问令牌包含无效字符，请检查配置。")
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise MinerUError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MinerUError(f"网络请求失败: {exc.reason}") from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MinerUError(f"接口返回不是 JSON: {text[:300]}") from exc


def query_quota(token: str) -> dict | None:
    try:
        result = http_json("GET", f"{API_V4_BASE_URL}/quota", token=token, timeout=30)
    except MinerUError:
        return None
    if result.get("code") == 0 and isinstance(result.get("data"), dict):
        return result["data"]
    return None


def http_put_file(upload_url: str, file_path: Path, timeout: int = 300, attempts: int = 4, log=None) -> None:
    parsed = urllib.parse.urlparse(upload_url)
    body = file_path.read_bytes()
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        connection = connection_cls(parsed.netloc, timeout=timeout)
        try:
            if log:
                log("上传文件..." if attempt == 1 else f"重新上传文件，第 {attempt}/{attempts} 次尝试...")

            connection.request(
                "PUT",
                path,
                body=body,
                headers={
                    "User-Agent": USER_AGENT,
                    "Content-Length": str(len(body)),
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            response_body = response.read().decode("utf-8", errors="replace")

            if response.status in (200, 201, 204):
                return

            error = MinerUError(f"文件上传失败 HTTP {response.status}: {response_body}")
            if response.status in {408, 429} or 500 <= response.status < 600:
                last_error = error
                if attempt < attempts:
                    if log:
                        log(f"上传暂时失败，将重试: {error}")
                    time.sleep(min(2**attempt, 20))
                    continue

            raise error
        except (OSError, TimeoutError) as exc:
            last_error = exc
            if attempt < attempts:
                if log:
                    log(f"上传连接中断，将重试: {exc}")
                time.sleep(min(2**attempt, 20))
                continue
            raise MinerUError(f"文件上传失败: {exc}") from exc
        finally:
            connection.close()

    raise MinerUError(f"文件上传失败，多次重试仍未成功: {last_error}")


def http_bytes(url: str, timeout: int = 300) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise MinerUError(f"下载结果失败 HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MinerUError(f"下载结果失败: {exc.reason}") from exc


def http_bytes_with_retries(url: str, log, attempts: int = 5, timeout: int = 180) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            if log:
                log("下载结果压缩包..." if attempt == 1 else f"重新下载结果压缩包，第 {attempt}/{attempts} 次尝试...")
            return http_bytes(url, timeout=timeout)
        except MinerUError as exc:
            last_error = exc
            if log:
                log(f"下载暂时失败: {exc}")
            if attempt < attempts:
                time.sleep(min(5 * attempt, 30))
    raise MinerUError(f"多次下载结果压缩包失败，最后错误: {last_error}")


def submit_precise_file(file_path: Path, options: ParseOptions, token: str) -> tuple[str, str]:
    model_version = "MinerU-HTML" if file_path.suffix.lower() in {".html", ".htm"} else options.model_version
    payload = {
        "enable_formula": options.enable_formula,
        "enable_table": options.enable_table,
        "model_version": model_version,
        "files": [
            {
                "name": file_path.name,
                "is_ocr": options.is_ocr,
                "data_id": f"{file_path.stem}-{uuid.uuid4().hex[:8]}",
            }
        ],
    }
    result = http_json("POST", f"{API_V4_BASE_URL}/file-urls/batch", payload, token=token)
    if result.get("code") != 0:
        raise MinerUError(f"创建精准解析任务失败: {result.get('msg', result)}")

    data = result.get("data") or {}
    batch_id = data.get("batch_id")
    file_urls = data.get("file_urls") or []
    if isinstance(file_urls, str):
        upload_url = file_urls
    elif isinstance(file_urls, list) and file_urls:
        first = file_urls[0]
        upload_url = first.get("url") if isinstance(first, dict) else str(first)
    else:
        upload_url = ""
    if not batch_id or not upload_url:
        raise MinerUError(f"创建精准解析任务响应缺少 batch_id/file_urls: {result}")
    return batch_id, upload_url


def poll_precise_result(batch_id: str, options: ParseOptions, token: str, log) -> dict:
    started = time.time()
    transient_errors = 0
    while time.time() - started < options.timeout_seconds:
        try:
            result = http_json("GET", f"{API_V4_BASE_URL}/extract-results/batch/{batch_id}", token=token, timeout=120)
        except MinerUError as exc:
            transient_errors += 1
            elapsed = int(time.time() - started)
            if log:
                log(f"[{elapsed}s] 查询结果暂时失败，第 {transient_errors} 次重试: {exc}")
            time.sleep(min(options.poll_interval_seconds * transient_errors, 30))
            continue

        transient_errors = 0
        if result.get("code") != 0:
            raise MinerUError(f"查询精准解析任务失败: {result.get('msg', result)}")

        data = result.get("data") or {}
        items = data.get("extract_result") or data.get("extract_results") or data.get("files") or []
        if isinstance(items, dict):
            items = [items]

        elapsed = int(time.time() - started)
        if not items:
            if log:
                log(f"[{elapsed}s] 等待 MinerU 返回结果...")
            time.sleep(options.poll_interval_seconds)
            continue

        item = items[0]
        state = str(item.get("state") or item.get("status") or "").lower()
        zip_url = item.get("full_zip_url") or item.get("zip_url") or item.get("result_url")
        progress = item.get("extract_progress")
        if zip_url and (not state or state in {"done", "finished", "success"}):
            if log:
                log(f"[{elapsed}s] 精准解析完成，下载结果压缩包")
            return item
        if state in {"failed", "fail", "error"}:
            raise MinerUError(f"精准解析失败: {item.get('err_msg') or item.get('msg') or item}")

        if log:
            if isinstance(progress, dict):
                log(f"[{elapsed}s] 解析中: {progress.get('extracted_pages', '?')}/{progress.get('total_pages', '?')} 页")
            else:
                log(f"[{elapsed}s] 精准解析状态: {state or '处理中'}")
        time.sleep(options.poll_interval_seconds)

    raise MinerUError(f"精准解析轮询超时，batch_id={batch_id}")


def extract_markdown_from_zip(result_item: dict, output_dir: Path, log) -> tuple[str, str, Path]:
    zip_url = result_item.get("full_zip_url") or result_item.get("zip_url") or result_item.get("result_url")
    if not zip_url:
        raise MinerUError(f"结果中没有 full_zip_url: {result_item}")

    zip_path = output_dir / "mineru_result.zip"
    extract_dir = output_dir / "mineru_result"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    zip_path.write_bytes(http_bytes_with_retries(zip_url, log))
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir)

    # 压缩包只用于解压，解析成功后立即删除，避免工作区长期残留 mineru_result.zip。
    try:
        if zip_path.exists():
            zip_path.unlink()
    except Exception:
        pass

    md_candidates = sorted(extract_dir.rglob("full.md")) or sorted(extract_dir.rglob("*.md"))
    if not md_candidates:
        raise MinerUError(f"结果压缩包中没有找到 Markdown 文件: {zip_path}")
    return md_candidates[0].read_text(encoding="utf-8", errors="replace"), zip_url, extract_dir


def extension_from_data_uri(header: str) -> str:
    mime = header.split(";", 1)[0].replace("data:", "").strip().lower()
    return mimetypes.guess_extension(mime) or ".bin"


def extension_from_target(target: str) -> str:
    parsed = urllib.parse.urlparse(target)
    suffix = Path(urllib.parse.unquote(parsed.path)).suffix
    return suffix if suffix and len(suffix) <= 8 else ".png"


def simplify_markdown_images(markdown: str, output_dir: Path, source_dirs: list[Path] | None = None) -> tuple[str, list[dict]]:
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    source_dirs = source_dirs or []
    records: list[dict] = []
    counter = 0

    def next_image_id() -> str:
        nonlocal counter
        counter += 1
        return f"IMAGE_{counter:03d}"

    def save_data_uri(image_id: str, target: str) -> tuple[str, str]:
        header, b64_data = target.split(",", 1)
        ext = extension_from_data_uri(header)
        save_path = images_dir / f"{image_id.lower()}{ext}"
        save_path.write_bytes(base64.b64decode(b64_data, validate=False))
        return f"images/{save_path.name}", str(save_path)

    def find_source_image(target: str) -> Path | None:
        unwrapped = target.strip()
        if unwrapped.startswith("<") and unwrapped.endswith(">"):
            unwrapped = unwrapped[1:-1]
        parsed = urllib.parse.urlparse(unwrapped)
        if parsed.scheme and parsed.scheme != "file":
            return None
        raw_path = urllib.parse.unquote(parsed.path if parsed.scheme == "file" else unwrapped)
        candidates = [Path(raw_path)]
        for source_dir in source_dirs:
            candidates.append(source_dir / raw_path)
            candidates.append(source_dir / Path(raw_path).name)
            candidates.append(source_dir / "images" / Path(raw_path).name)
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    def copy_source_image(image_id: str, source: Path) -> tuple[str, str]:
        suffix = source.suffix or ".jpg"
        save_path = images_dir / f"{image_id.lower()}{suffix}"
        shutil.copy2(source, save_path)
        return f"images/{save_path.name}", str(save_path)

    def replace_markdown_image(match: re.Match) -> str:
        alt = match.group("alt")
        target = match.group("target").strip()
        title = (match.group("title") or "").strip()
        image_id = next_image_id()
        saved_file = None
        try:
            if target.startswith("data:image/") and "," in target:
                clean_target, saved_file = save_data_uri(image_id, target)
            else:
                source_image = find_source_image(target)
                if source_image:
                    clean_target, saved_file = copy_source_image(image_id, source_image)
                else:
                    clean_target = f"images/{image_id.lower()}{extension_from_target(target)}"
        except Exception as exc:
            clean_target = f"images/{image_id.lower()}{extension_from_target(target)}"
            records.append({"id": image_id, "warning": f"图片保存失败: {exc}"})

        records.append(
            {
                "id": image_id,
                "alt": alt,
                "original_target": target,
                "title": title,
                "clean_target": clean_target,
                "saved_file": saved_file,
            }
        )
        return f"![{image_id}]({clean_target})"

    md_image_pattern = re.compile(
        r"!\[(?P<alt>[^\]]*)\]\(\s*(?P<target><[^>]+>|[^\s)]+)(?P<title>\s+['\"][^'\"]*['\"])?\s*\)",
        re.DOTALL,
    )
    cleaned = md_image_pattern.sub(replace_markdown_image, markdown)

    def replace_html_img(match: re.Match) -> str:
        attrs = match.group("attrs")
        src = re.search(r"""src\s*=\s*["'](?P<src>.*?)["']""", attrs, re.IGNORECASE | re.DOTALL)
        alt = re.search(r"""alt\s*=\s*["'](?P<alt>.*?)["']""", attrs, re.IGNORECASE | re.DOTALL)
        if not src:
            return match.group(0)
        fake = f"![{alt.group('alt') if alt else ''}]({src.group('src')})"
        return md_image_pattern.sub(replace_markdown_image, fake)

    return re.sub(r"<img\b(?P<attrs>[^>]*)>", replace_html_img, cleaned, flags=re.IGNORECASE | re.DOTALL), records


class _MinerUNamespace:
    API_V4_BASE_URL = API_V4_BASE_URL
    USER_AGENT = USER_AGENT
    DEFAULT_MODEL_VERSION = DEFAULT_MODEL_VERSION
    SUPPORTED_INPUT_EXTENSIONS = SUPPORTED_INPUT_EXTENSIONS
    MinerUError = MinerUError
    ParseOptions = ParseOptions
    is_supported_input_file = staticmethod(mineru_is_supported_input_file)
    http_json = staticmethod(http_json)
    query_quota = staticmethod(query_quota)
    http_put_file = staticmethod(http_put_file)
    http_bytes = staticmethod(http_bytes)
    http_bytes_with_retries = staticmethod(http_bytes_with_retries)
    submit_precise_file = staticmethod(submit_precise_file)
    poll_precise_result = staticmethod(poll_precise_result)
    extract_markdown_from_zip = staticmethod(extract_markdown_from_zip)
    extension_from_data_uri = staticmethod(extension_from_data_uri)
    extension_from_target = staticmethod(extension_from_target)
    simplify_markdown_images = staticmethod(simplify_markdown_images)


mineru = _MinerUNamespace()


def default_work_dir_path() -> Path:
    return default_workspace_path(APP_DIR, LEGACY_APP_DIRS, Path(__file__).parent)


def work_dir_path(settings: AppSettings | None = None) -> Path:
    settings = settings or load_settings()
    if settings.work_dir:
        return Path(settings.work_dir)
    return default_work_dir_path()


def chat_history_path(settings: AppSettings | None = None) -> Path:
    return work_dir_path(settings) / "chat_conversations.json"


def re_split_model_name(model: str) -> list[str]:
    return re.split(r"[^a-z0-9]+", model)


def choose_preferred_model(model_ids: list[str], current: str = "") -> str:
    if current and current in model_ids:
        return current
    preferred_keywords = ("mini", "flash", "lite")
    for keyword in preferred_keywords:
        for model in model_ids:
            tokens = [item for item in re_split_model_name(model.lower()) if item]
            if keyword in tokens:
                return model
    return model_ids[0] if model_ids else current


class _EmbeddedAppConfig:
    APP_DIR = APP_DIR
    ProviderSettings = ProviderSettings
    ProviderCard = ProviderCard
    AppSettings = AppSettings
    load_settings = staticmethod(load_settings)
    save_settings = staticmethod(save_settings)
    secret_path = staticmethod(secret_path)
    save_secret = staticmethod(save_secret)
    load_secret = staticmethod(load_secret)
    secret_is_session_only = staticmethod(secret_is_session_only)
    secret_storage_error = staticmethod(secret_storage_error)
    delete_secret = staticmethod(delete_secret)
    save_mineru_token = staticmethod(save_mineru_token)
    load_mineru_token = staticmethod(load_mineru_token)
    work_dir_path = staticmethod(work_dir_path)
    default_work_dir_path = staticmethod(default_work_dir_path)
    chat_history_path = staticmethod(chat_history_path)
    choose_preferred_model = staticmethod(choose_preferred_model)


app_config = _EmbeddedAppConfig()


__all__ = [name for name in globals() if not name.startswith("__")]
