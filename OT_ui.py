"""LitMTrans dialogs, reader windows, workers, and application entry point."""

from __future__ import annotations

# 本地请求记录默认关闭；启用时仍会排除 API 密钥。
TRANSLATION_REQUEST_AUDIT_ENABLED = False

import hashlib
import json
import os
import copy
import math
import re
import unicodedata

# Chromium otherwise renders the document preview with its rounded overlay
# scrollbar, which does not match the native PDF reader's rectangular bar.
# This must be configured before QtWebEngine creates its first profile.
_webengine_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
if "OverlayScrollbar" not in _webengine_flags:
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        f"{_webengine_flags} --disable-features=OverlayScrollbar,OverlayScrollbars"
    ).strip()

from LS_pipeline import *
from app_version import APP_VERSION, GITHUB_ISSUES_URL, GITHUB_RELEASES_URL, GITHUB_REPO_URL
from updater import ReleaseInfo, UpdateCheckWorker, UpdateDownloadWorker, format_size, is_newer_version, launch_installer

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QMargins, QObject, QPointF, QRectF, QSizeF, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPainter, QPalette, QPen, QPixmap, QTextOption
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWidgets import QAbstractSpinBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

try:
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtPdfWidgets import QPdfView

    PDF_VIEW_AVAILABLE = True
except ImportError:
    QPdfDocument = None
    QPdfView = None
    PDF_VIEW_AVAILABLE = False

# Qt's C++ QWIDGETSIZE_MAX macro is not exported by PySide.
QWIDGETSIZE_MAX = 16_777_215
_SECRET_SESSION_WARNINGS: set[tuple[str, str]] = set()


def save_secret_with_session_fallback(parent, provider_id: str, name: str, value: str) -> bool:
    persisted = bool(app_config.save_secret(provider_id, name, value))
    key = (str(provider_id), str(name))
    if not persisted and key not in _SECRET_SESSION_WARNINGS:
        _SECRET_SESSION_WARNINGS.add(key)
        detail = app_config.secret_storage_error(provider_id, name)
        QMessageBox.warning(
            parent,
            "密钥仅在本次运行中有效",
            "Windows 安全存储暂时不可用。密钥没有写入磁盘，但本次运行仍可正常使用；"
            "关闭后本次输入不会保留。下次将使用此前已保存的密钥（如果有），否则需要重新填写。"
            + (f"\n\n错误信息：{detail}" if detail else ""),
        )
    return persisted


def create_reader_font_control(font_spin: QDoubleSpinBox, tooltip: str) -> QFrame:
    """Build a compact numeric font control with unlabelled fine adjustment."""
    font_spin.setObjectName("readerFontSpin")
    font_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
    font_spin.setFixedWidth(82)

    control = QFrame()
    control.setObjectName("readerFontControl")
    control.setToolTip(tooltip)
    layout = QHBoxLayout(control)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    layout.addWidget(font_spin)

    stepper = QWidget()
    stepper.setObjectName("readerFontStepper")
    stepper_layout = QVBoxLayout(stepper)
    stepper_layout.setContentsMargins(0, 1, 1, 1)
    stepper_layout.setSpacing(1)
    increase = QToolButton()
    increase.setObjectName("readerFontStepButton")
    increase.setAccessibleName("增大字号")
    increase.setToolTip("增大字号")
    increase.clicked.connect(font_spin.stepUp)
    decrease = QToolButton()
    decrease.setObjectName("readerFontStepButton")
    decrease.setAccessibleName("减小字号")
    decrease.setToolTip("减小字号")
    decrease.clicked.connect(font_spin.stepDown)
    stepper_layout.addWidget(increase)
    stepper_layout.addWidget(decrease)
    layout.addWidget(stepper)
    return control


class DocumentListWidget(QListWidget):
    """List widget that reports a completed internal drag after its items move."""

    reordered = Signal()

    def dropEvent(self, event):
        before = [self.item(index).data(256) for index in range(self.count())]
        super().dropEvent(event)
        after = [self.item(index).data(256) for index in range(self.count())]
        if before != after:
            self.reordered.emit()


class BatchProgressPanel(QFrame):
    """Compact, user-facing state for a parse/translate pipeline.

    The raw worker log remains available for diagnosis, but this panel is the
    default view while a batch runs.  It deliberately reports document-level
    work rather than every upload, poll and retry made by the worker.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("batchProgressPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self.title = QLabel("批量任务")
        self.title.setObjectName("batchProgressTitle")
        layout.addWidget(self.title)

        self.parse_progress = QProgressBar()
        self.parse_progress.setTextVisible(True)
        self.parse_progress.setFormat("解析  %v / %m")
        layout.addWidget(self.parse_progress)

        self.translate_progress = QProgressBar()
        self.translate_progress.setTextVisible(True)
        self.translate_progress.setFormat("翻译  %v / %m")
        self.translate_progress.setVisible(False)
        layout.addWidget(self.translate_progress)

        self.summary = QLabel("")
        self.summary.setObjectName("batchProgressSummary")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.active_items = QLabel("")
        self.active_items.setObjectName("batchProgressItems")
        self.active_items.setWordWrap(True)
        layout.addWidget(self.active_items)

    def update_state(
        self,
        *,
        parse_done: int,
        parse_total: int,
        parse_failed: int,
        parse_skipped: int,
        parse_active: dict[str, str],
        translate_done: int,
        translate_total: int,
        translate_failed: int,
        translate_active: dict[str, str],
        translation_enabled: bool,
    ) -> None:
        self.parse_progress.setVisible(parse_total > 0)
        self.parse_progress.setRange(0, max(1, parse_total))
        self.parse_progress.setValue(min(max(0, parse_done), max(1, parse_total)))
        self.translate_progress.setVisible(translation_enabled)
        if translation_enabled:
            self.translate_progress.setRange(0, max(1, translate_total))
            self.translate_progress.setValue(min(max(0, translate_done), max(1, translate_total)))

        parse_summary = f"解析完成 {parse_done}/{parse_total}"
        if parse_failed:
            parse_summary += f"，失败 {parse_failed}"
        if parse_skipped:
            parse_summary += f"，跳过 {parse_skipped}"
        if translation_enabled:
            translate_summary = f"翻译完成 {translate_done}/{translate_total}"
            if translate_failed:
                translate_summary += f"，失败 {translate_failed}"
            self.summary.setText(f"{parse_summary}  ·  {translate_summary}")
        else:
            self.summary.setText(parse_summary)

        lines = []
        for name, status in list(parse_active.items())[:5]:
            lines.append(f"● 解析中 · {name}  —  {status}")
        for name, status in list(translate_active.items())[:5]:
            lines.append(f"● 翻译中 · {name}  —  {status}")
        self.active_items.setText("\n".join(lines) or "正在整理任务队列…")


def _evidence_text_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
    return "".join(character for character in normalized if not character.isspace() and unicodedata.category(character)[0] not in {"P", "S"})


def _evidence_segments(value: str) -> list[str]:
    return [part for part in (_evidence_text_key(item) for item in re.split(r"(?:\.{3,}|…+|⋯+)", str(value or ""))) if len(part) >= 2]


def _evidence_coverage(source: str, candidate: str) -> float:
    expected, actual = _evidence_text_key(source), _evidence_text_key(candidate)
    if not expected or not actual:
        return 0.0
    if expected in actual:
        return 1.0
    size = min(3, len(expected))
    expected_grams = {expected[index:index + size] for index in range(len(expected) - size + 1)}
    actual_grams = {actual[index:index + size] for index in range(len(actual) - size + 1)}
    return sum(gram in actual_grams for gram in expected_grams) / max(1, len(expected_grams))


def _sentence_ranges(text: str) -> list[tuple[int, int, str]]:
    ranges = []
    for match in re.finditer(r"[^.!?。！？]+(?:[.!?。！？]+|$)", str(text or "")):
        start, end = match.span()
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end > start:
            ranges.append((start, end, text[start:end]))
    return ranges


def closest_evidence_sentence(text: str, quote: str) -> tuple[int, int, float] | None:
    """Return the complete sentence best matching exact, omitted, or rewritten evidence."""
    sentences, segments = _sentence_ranges(text), _evidence_segments(quote)
    if not sentences or not segments:
        return None
    ranked = []
    for start, end, sentence in sentences:
        key = _evidence_text_key(sentence)
        cursor, ordered = 0, True
        for segment in segments:
            index = key.find(segment, cursor)
            if index < 0:
                ordered = False
                break
            cursor = index + len(segment)
        score = 1.0 if ordered else max(
            _evidence_coverage(quote, sentence),
            sum(_evidence_coverage(segment, sentence) for segment in segments) / len(segments),
        )
        ranked.append((score, start, end))
    score, start, end = max(ranked, key=lambda item: (item[0], -item[1]))
    return start, end, score


def resolve_pdf_evidence_sentence(view, quote: dict) -> dict | None:
    """Resolve diagram evidence to a sentence selection in the native PDF text layer."""
    if view is None or view.document() is None or not isinstance(quote, dict):
        return None
    wanted = str(quote.get("text") or quote.get("quote") or "").strip()
    if not wanted:
        return None
    document = view.document()
    cache_key = (id(document), int(document.pageCount()))
    cache = getattr(view, "_diagram_pdf_text_cache", None)
    if not cache or cache[0] != cache_key:
        page_texts = []
        for page in range(document.pageCount()):
            try:
                page_texts.append(str(document.getAllText(page).text() or ""))
            except (AttributeError, RuntimeError):
                page_texts.append("")
        cache = (cache_key, page_texts)
        view._diagram_pdf_text_cache = cache
    best = None
    for page, page_text in enumerate(cache[1]):
        match = closest_evidence_sentence(page_text, wanted)
        if match is None:
            continue
        start, end, score = match
        candidate = (score, -page, page, start, end, page_text[start:end])
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None:
        return None
    score, _negative_page, page, start, end, sentence = best
    try:
        selection = document.getSelectionAtIndex(page, start, max(1, end - start))
        page_size = document.pagePointSize(page)
        page_width, page_height = max(1.0, float(page_size.width())), max(1.0, float(page_size.height()))
        bounds = list(selection.bounds() or [])
        rectangles = [polygon.boundingRect() for polygon in bounds]
        if not rectangles:
            rectangles = [selection.boundingRectangle()]
        anchor_rects = [
            {"x": max(0.0, float(rect.x()) / page_width), "y": max(0.0, float(rect.y()) / page_height),
             "width": min(1.0, float(rect.width()) / page_width), "height": min(1.0, float(rect.height()) / page_height)}
            for rect in rectangles if rect.isValid() and rect.width() > 0 and rect.height() > 0
        ]
    except (AttributeError, RuntimeError, TypeError):
        anchor_rects = []
    result = dict(quote)
    result.update({
        "anchor_page": page + 1, "page": page + 1, "text": sentence,
        "matched_evidence_text": wanted, "approximate": score < .999,
        "match_confidence": round(float(score), 3), "anchor_rects": anchor_rects,
    })
    if anchor_rects:
        result["anchor_rect"] = anchor_rects[0]
        result["anchor_ratio"] = anchor_rects[0]["y"] + anchor_rects[0]["height"] / 2
    return result


if PDF_VIEW_AVAILABLE:
    class PdfReferenceOverlay(QWidget):
        """Transient page-relative reference highlight above a QPdfView."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.quote = None
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.hide()

        def paintEvent(self, _event):
            view = self.parent().parent() if self.parent() else None
            rects = pdf_reference_highlight_rects(view, self.quote)
            if not rects:
                return
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor("#E0A000"), 2.0))
            painter.setBrush(QColor(224, 160, 0, 28))
            for rect in rects:
                painter.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), 2, 2)


    class SyncedPdfView(QPdfView):
        """A demand-rendered PDF view that reports only real user scroll input."""

        userScrollIntent = Signal()

        def __init__(self, parent=None):
            super().__init__(parent)
            self._reference_highlight_generation = 0
            self._reference_overlay = PdfReferenceOverlay(self.viewport())

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self._reference_overlay.setGeometry(self.viewport().rect())

        def wheelEvent(self, event):
            super().wheelEvent(event)
            # The base handler updates the scroll bar. Report the intent after
            # that update so the paired pane does not receive the prior step.
            self.userScrollIntent.emit()

        def keyPressEvent(self, event):
            if event.key() in {
                Qt.Key.Key_Up,
                Qt.Key.Key_Down,
                Qt.Key.Key_Left,
                Qt.Key.Key_Right,
                Qt.Key.Key_PageUp,
                Qt.Key.Key_PageDown,
                Qt.Key.Key_Home,
                Qt.Key.Key_End,
                Qt.Key.Key_Space,
            }:
                self.userScrollIntent.emit()
            super().keyPressEvent(event)
else:
    SyncedPdfView = None


def create_synced_pdf_view(parent=None):
    if not PDF_VIEW_AVAILABLE:
        return None
    view = SyncedPdfView(parent)
    document = QPdfDocument(view)
    view.setDocument(document)
    view._sync_page_geometry_cache = None
    view.setPageMode(QPdfView.PageMode.MultiPage)
    view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
    # Match the generated layout reader: 10 px outer gutter, warm canvas,
    # and 18 px between consecutive white pages.
    view.setDocumentMargins(QMargins(10, 10, 10, 10))
    view.setPageSpacing(18)
    palette = view.palette()
    for role in (
        QPalette.ColorRole.Dark,
        QPalette.ColorRole.Mid,
        QPalette.ColorRole.Window,
    ):
        palette.setColor(role, QColor("#f6f3ee"))
    view.setPalette(palette)
    view.viewport().setAutoFillBackground(True)
    view.viewport().setPalette(palette)
    view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    view.hide()
    return view


def pdf_view_is_active(owner) -> bool:
    view = getattr(owner, "source_pdf_view", None)
    return bool(
        getattr(owner, "_source_pdf_active", False)
        and view is not None
        and view.document() is not None
        and view.document().pageCount() > 0
    )


def set_source_pdf_active(owner, active: bool) -> None:
    active = bool(active and getattr(owner, "source_pdf_view", None))
    was_active = bool(getattr(owner, "_source_pdf_active", False))
    owner._source_pdf_active = active
    pdf_view = getattr(owner, "source_pdf_view", None)
    web_view = getattr(owner, "source_web_view", None)
    fallback = getattr(owner, "source_fallback_viewer", None)
    if pdf_view is not None:
        pdf_view.setVisible(active)
        if not active and was_active and pdf_view.document() is not None:
            previous_document = pdf_view.document()
            replacement = QPdfDocument(pdf_view)
            pdf_view.setDocument(replacement)
            pdf_view._sync_page_geometry_cache = None
            previous_document.close()
            previous_document.deleteLater()
    if web_view is not None:
        web_view.setVisible(not active)
        if active and not was_active:
            try:
                web_view.stop()
                web_view.setUrl(QUrl("about:blank"))
                web_view.history().clear()
            except RuntimeError:
                pass
    elif fallback is not None:
        fallback.setVisible(not active)


def set_pdf_fit_width(view) -> None:
    try:
        if view is not None:
            view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
    except RuntimeError:
        pass


def load_source_pdf(owner, path: Path) -> bool:
    view = getattr(owner, "source_pdf_view", None)
    if view is None or not path or path.suffix.lower() != ".pdf" or not path.exists():
        return False
    previous_document = view.document()
    document = QPdfDocument(view)
    view.setDocument(document)
    view._sync_page_geometry_cache = None
    if previous_document is not None:
        previous_document.close()
        previous_document.deleteLater()
    try:
        document.load(str(path.resolve()))
    except (OSError, RuntimeError):
        document.close()
        set_source_pdf_active(owner, False)
        return False
    if document.pageCount() <= 0:
        set_source_pdf_active(owner, False)
        return False
    set_source_pdf_active(owner, True)
    view.pageNavigator().jump(0, QPointF(0.0, 0.0), 0)
    QTimer.singleShot(0, lambda: set_pdf_fit_width(view))
    QTimer.singleShot(80, owner.install_sync_scroll_bridge)
    QTimer.singleShot(140, owner.sync_translation_to_source_now)
    return True


def release_source_pdf(owner) -> None:
    view = getattr(owner, "source_pdf_view", None)
    if view is None:
        return
    # QPdfView owns an internal link model which cannot connect to a null
    # document. The normal inactive transition installs an empty replacement
    # before closing the previous document.
    set_source_pdf_active(owner, False)
    # The replacement document is detached synchronously, while deleteLater()
    # is only processed on the next Qt loop turn. Drain it now when a caller
    # is about to remove the directory containing the PDF on Windows.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()
    overlay = getattr(view, "_reference_overlay", None)
    if overlay is not None:
        overlay.quote = None
        overlay.hide()


def pdf_page_geometry(view) -> tuple[list[float], list[float]] | None:
    if view is None or view.document() is None:
        return None
    document = view.document()
    page_count = document.pageCount()
    if page_count <= 0:
        return None
    scroll_bar = view.verticalScrollBar()
    scroll_max = max(0, int(scroll_bar.maximum()))
    viewport_height = max(1, int(view.viewport().height()))
    viewport_width = max(1, int(view.viewport().width()))
    if scroll_max <= 0:
        return None
    cache_key = (
        id(document),
        page_count,
        viewport_width,
        viewport_height,
        scroll_max,
        int(view.pageSpacing()),
    )
    cached = getattr(view, "_sync_page_geometry_cache", None)
    if cached and cached[0] == cache_key:
        return cached[1], cached[2]
    page_sizes = [document.pagePointSize(index) for index in range(page_count)]
    max_page_width = max(1.0, max(float(size.width()) for size in page_sizes))
    fit_scale = viewport_width / max_page_width
    spacing = max(0.0, float(view.pageSpacing()))
    nominal_heights = [max(1.0, float(size.height()) * fit_scale) for size in page_sizes]
    nominal_total = sum(nominal_heights) + spacing * max(0, page_count - 1)
    actual_total = max(1.0, float(scroll_max + viewport_height))
    actual_scale = actual_total / max(1.0, nominal_total)
    page_tops = []
    top = 0.0
    for height in nominal_heights:
        page_tops.append(top)
        top += height + spacing
    page_tops = [top_value * actual_scale for top_value in page_tops]
    rendered_heights = [height * actual_scale for height in nominal_heights]
    view._sync_page_geometry_cache = (cache_key, page_tops, rendered_heights)
    return page_tops, rendered_heights


def pdf_sync_payload(view) -> dict | None:
    if view is None or view.document() is None:
        return None
    document = view.document()
    page_count = document.pageCount()
    if page_count <= 0:
        return None
    scroll_bar = view.verticalScrollBar()
    scroll_max = max(0, int(scroll_bar.maximum()))
    viewport_height = max(1, int(view.viewport().height()))
    geometry = pdf_page_geometry(view)
    viewport_anchor_ratio = 0.5
    if geometry:
        page_tops, rendered_heights = geometry
        anchor_y = float(scroll_bar.value()) + viewport_height * viewport_anchor_ratio
        page = page_count - 1
        for index, page_top in enumerate(page_tops):
            next_top = page_tops[index + 1] if index + 1 < page_count else float("inf")
            if anchor_y < next_top:
                page = index
                break
        page_ratio = (anchor_y - page_tops[page]) / max(1.0, rendered_heights[page])
    else:
        page = max(0, min(page_count - 1, int(view.pageNavigator().currentPage())))
        page_height = max(1.0, float(document.pagePointSize(page).height()))
        location = view.pageNavigator().currentLocation()
        page_ratio = float(location.y()) / page_height
    page_ratio = max(0.0, min(1.0, page_ratio))
    return {
        "layoutPage": page,
        "pageOffsetRatio": page_ratio,
        "viewportAnchorRatio": viewport_anchor_ratio,
        "ratio": (
            max(0.0, min(1.0, float(scroll_bar.value()) / scroll_max))
            if scroll_max > 0
            else page / max(1, page_count - 1)
        ),
    }


def scroll_pdf_view_to_payload(owner, payload: dict) -> bool:
    view = getattr(owner, "source_pdf_view", None)
    if view is None or view.document() is None or not payload:
        return False
    document = view.document()
    page_count = document.pageCount()
    if page_count <= 0:
        return False
    if payload.get("layoutPage") is not None:
        try:
            page = int(payload["layoutPage"])
        except (TypeError, ValueError):
            page = 0
    else:
        try:
            page = round(float(payload.get("ratio") or 0.0) * max(0, page_count - 1))
        except (TypeError, ValueError):
            page = 0
    page = max(0, min(page_count - 1, page))
    try:
        page_ratio = float(payload.get("pageOffsetRatio") or 0.0)
    except (TypeError, ValueError):
        page_ratio = 0.0
    page_ratio = max(0.0, min(1.0, page_ratio))
    page_height = max(1.0, float(document.pagePointSize(page).height()))
    owner._syncing_scroll = True
    view.pageNavigator().jump(page, QPointF(0.0, page_height * page_ratio), 0)
    geometry = pdf_page_geometry(view)
    if geometry:
        page_tops, rendered_heights = geometry
        try:
            anchor_ratio = float(payload.get("viewportAnchorRatio", 0.5))
        except (TypeError, ValueError):
            anchor_ratio = 0.5
        anchor_ratio = max(0.0, min(1.0, anchor_ratio))
        top = (
            page_tops[page]
            + rendered_heights[page] * page_ratio
            - view.viewport().height() * anchor_ratio
        )
        view.verticalScrollBar().setValue(round(top))
    QTimer.singleShot(80, lambda: setattr(owner, "_syncing_scroll", False))
    return True


def pdf_reference_highlight_rects(view, quote: dict | None) -> list[QRectF]:
    """Map sentence line anchors into QPdfView viewport pixels."""
    if view is None or not isinstance(quote, dict) or view.document() is None:
        return []
    try:
        page = int(quote.get("anchor_page") or quote.get("page") or 0) - 1
    except (TypeError, ValueError):
        return []
    page_count = view.document().pageCount()
    if page < 0 or page >= page_count:
        return []
    geometry = pdf_page_geometry(view)
    if not geometry:
        return []
    page_tops, rendered_heights = geometry
    page_height = rendered_heights[page]
    page_size = view.document().pagePointSize(page)
    source_height = max(1.0, float(page_size.height()))
    page_width = page_height * max(1.0, float(page_size.width())) / source_height
    page_left = (view.viewport().width() - page_width) / 2.0
    page_top = page_tops[page] - view.verticalScrollBar().value()

    rect_values = quote.get("anchor_rects") if isinstance(quote.get("anchor_rects"), list) else [quote.get("anchor_rect")]
    point_data = quote.get("anchor_point")
    mapped_rects = []
    for rect_data in rect_values:
        if not isinstance(rect_data, dict):
            continue
        try:
            x = float(rect_data.get("x") or 0.0); y = float(rect_data.get("y") or 0.0)
            width = max(0.01, float(rect_data.get("width") or 0.08)); height = max(0.01, float(rect_data.get("height") or 0.035))
            mapped_rects.append(QRectF(
                page_left + page_width * x,
                page_top + page_height * y,
                page_width * width,
                page_height * height,
            ))
        except (TypeError, ValueError):
            pass
    if mapped_rects:
        return mapped_rects
    if isinstance(point_data, dict):
        try:
            x = max(0.0, min(1.0, float(point_data.get("x") or 0.5)))
            y = max(0.0, min(1.0, float(point_data.get("y") or 0.5)))
            width = page_width * (0.20 if quote.get("type") == "formula" else 0.12)
            height = max(14.0, page_height * (0.055 if quote.get("type") == "formula" else 0.035))
            return [QRectF(page_left + page_width * x - width / 2, page_top + page_height * y - height / 2, width, height)]
        except (TypeError, ValueError):
            pass
    try:
        y = float(quote.get("anchor_ratio"))
    except (TypeError, ValueError):
        y = 0.46 if quote.get("type") == "formula" else None
    if y is None:
        return []
    y = max(0.0, min(1.0, y))
    return [QRectF(
        page_left + page_width * 0.08,
        page_top + page_height * y,
        page_width * 0.84,
        max(14.0, page_height * (0.06 if quote.get("type") == "formula" else 0.03)),
    )]


def pdf_reference_highlight_rect(view, quote: dict | None) -> QRectF | None:
    """Compatibility bounding rectangle for callers that expect one box."""
    rects = pdf_reference_highlight_rects(view, quote)
    if not rects:
        return None
    combined = QRectF(rects[0])
    for rect in rects[1:]:
        combined = combined.united(rect)
    return combined


def focus_pdf_reference_quote(owner, quote: dict) -> bool:
    """Scroll a lightweight source PDF to a document quote and flash its anchor."""
    if not pdf_view_is_active(owner):
        return False
    view = owner.source_pdf_view
    sentence_quote = resolve_pdf_evidence_sentence(view, quote)
    if sentence_quote is not None:
        quote = sentence_quote
    try:
        page = int(quote.get("anchor_page") or quote.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    if page <= 0:
        return False
    rect_data = quote.get("anchor_rect")
    point_data = quote.get("anchor_point")
    try:
        if isinstance(point_data, dict):
            page_ratio = float(point_data.get("y") or 0.0)
        elif isinstance(rect_data, dict):
            page_ratio = float(rect_data.get("y") or 0.0)
        elif quote.get("anchor_ratio") is not None:
            page_ratio = float(quote.get("anchor_ratio"))
        else:
            page_ratio = 0.46
    except (TypeError, ValueError):
        page_ratio = 0.46
    payload = {
        "layoutPage": page - 1,
        "pageOffsetRatio": max(0.0, min(1.0, page_ratio)),
        "viewportAnchorRatio": 0.5,
    }
    if not scroll_pdf_view_to_payload(owner, payload):
        return False
    overlay = getattr(view, "_reference_overlay", None)
    if overlay is not None:
        view._reference_highlight_generation += 1
        generation = view._reference_highlight_generation
        overlay.quote = dict(quote)
        overlay.setGeometry(view.viewport().rect())
        overlay.show()
        overlay.raise_()
        overlay.update()

        def clear_highlight():
            if getattr(view, "_reference_highlight_generation", 0) != generation:
                return
            overlay.quote = None
            overlay.hide()

        QTimer.singleShot(2000, clear_highlight)
    return True


def schedule_pdf_source_sync(owner) -> None:
    if (
        getattr(owner, "_syncing_scroll", False)
        or not pdf_view_is_active(owner)
        or getattr(owner, "_pdf_source_sync_pending", False)
    ):
        return
    owner._pdf_source_sync_pending = True

    def flush():
        owner._pdf_source_sync_pending = False
        if getattr(owner, "_syncing_scroll", False) or not pdf_view_is_active(owner):
            return
        payload = pdf_sync_payload(getattr(owner, "source_pdf_view", None))
        target = getattr(owner, "translation_web_view", None)
        if payload and target is not None:
            owner.apply_sync_payload_to_target(target, payload)

    QTimer.singleShot(12, flush)


def connect_pdf_source_sync(owner) -> None:
    view = getattr(owner, "source_pdf_view", None)
    if view is None:
        return
    view.userScrollIntent.connect(lambda: schedule_pdf_source_sync(owner))
    view.verticalScrollBar().valueChanged.connect(lambda _value: schedule_pdf_source_sync(owner))
    view.verticalScrollBar().sliderMoved.connect(lambda _value: schedule_pdf_source_sync(owner))
    view.horizontalScrollBar().sliderMoved.connect(lambda _value: schedule_pdf_source_sync(owner))


def poll_translation_web_to_pdf(owner) -> None:
    if getattr(owner, "_sync_poll_inflight", False):
        return
    target = getattr(owner, "translation_web_view", None)
    if target is None:
        return
    owner._sync_poll_inflight = True
    generation = owner._sync_poll_generation
    script = """
    (() => {
      if (!window.__mineruGetSyncState) return null;
      return JSON.stringify(window.__mineruGetSyncState());
    })();
    """

    def receive(result):
        if generation != owner._sync_poll_generation:
            return
        owner._sync_poll_inflight = False
        data = decode_web_javascript_payload(result) or {}
        user_scroll_at = int(data.get("userScrollAt") or 0)
        payload = data.get("payload")
        if not payload or user_scroll_at <= int(owner._last_translation_user_scroll_at or 0):
            return
        owner._last_translation_user_scroll_at = user_scroll_at
        scroll_pdf_view_to_payload(owner, payload)

    if not owner._run_sync_javascript(target, script, receive):
        owner._sync_poll_inflight = False

from AI_common import (
    apply_elevation,
    apply_monochrome_app_style,
    configure_silent_application,
    build_dark_premium_stylesheet,
    APP_UI_FONT_FAMILY_STACK,
    APP_DISPLAY_FONT_FAMILY_STACK,
    APP_MONO_FONT_FAMILY_STACK,
    APP_SERIF_FONT_FAMILY_STACK,
    COLOR_BG_BASE,
    COLOR_BG_SURFACE,
    COLOR_BG_SURFACE_2,
    COLOR_BG_INSET,
    COLOR_BORDER_HAIR,
    COLOR_BORDER_STRONG,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_DISABLED,
    COLOR_ACCENT,
    COLOR_ACCENT_HOVER,
    COLOR_ACCENT_PRESS,
    COLOR_ACCENT_SOFT,
    COLOR_DANGER,
    COLOR_DANGER_SOFT,
    RADIUS_SM,
    RADIUS_MD,
    RADIUS_LG,
    RADIUS_XL,
    RADIUS_PILL,
    QSizePolicy,
    edit_oneapi_request_body_mode,
    normalize_oneapi_request_body_mode,
)

TRANSLATION_PROVIDER_CHOICES = (
    ("mtranserver_local", "本地机翻"),
    ("free_machine", "联网免费机翻"),
    ("edge_local", "Edge 本地翻译"),
    ("zai", "Z.ai"),
    ("openrouter", "OpenRouter"),
    ("deepseek", "DeepSeek"),
    ("oneapi", "OneAPI / NewAPI"),
    ("openai_compatible", "OpenAI 兼容接口"),
    ("gemini", "Google Gemini"),
    ("siliconflow", "硅基流动 (SiliconFlow)"),
)

# API 密钥创建页只用于设置界面的帮助链接，不参与请求或鉴权。
TRANSLATION_PROVIDER_KEY_PAGES = {
    "zai": ("Z.ai", "https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/settings/keys"),
    "deepseek": ("DeepSeek", "https://platform.deepseek.com/api_keys"),
    "gemini": ("Google Gemini", "https://aistudio.google.com/app/apikey"),
    "siliconflow": ("硅基流动", "https://cloud.siliconflow.cn/account/ak"),
}

READER_FONT_MIN_PT = 9
READER_FONT_MAX_PT = 999
LAYOUT_BODY_FONT_MIN_PT = 5.0
LAYOUT_BODY_FONT_MAX_PT = 30.0
# QWebEngine 阅读页面统一使用由 @font-face 提供的内置思源宋体。
READER_SERIF_FONT_STACK = BUNDLED_READER_FONT_STACK
# Chromium can round a custom QPageLayout slightly below the requested page
# height. Keep a tiny, bottom-only allowance so a page-sized layout shell
# never spills onto an otherwise blank continuation page.
LAYOUT_PDF_PAGE_HEIGHT_ALLOWANCE_PT = 1.0
# Internal cache identity only.  It is not a user-facing file format or a
# release version; changing it makes the one generated PDF cache refresh.
LAYOUT_PDF_CACHE_VERSION = "layout-pdf-page-quantization-v3"


def dispose_web_view(web_view) -> None:
    """Release a Chromium renderer before its containing window is destroyed.

    ``QWebEngineView`` owns out-of-process Chromium renderers.  Leaving a view
    attached until Python tears down can make Windows wait for QtWebEngine's
    shutdown timeout.  This helper is deliberately tolerant of views that are
    already being deleted while the application exits.
    """
    if web_view is None:
        return
    try:
        web_view.stop()
    except RuntimeError:
        return
    except Exception:
        pass
    try:
        web_view.hide()
        web_view.setParent(None)
        web_view.deleteLater()
    except RuntimeError:
        pass


def decode_web_javascript_payload(value) -> dict | None:
    """Decode an object payload returned by a web-engine callback.

    PySide6 serializes JavaScript objects from ``runJavaScript`` differently
    from PyQt6.  Callers explicitly return JSON text so both bindings can be
    handled without relying on an implementation-specific object wrapper.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str) or not value:
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def layout_body_font_document_key(path: Path | str | None) -> str:
    """Return the stable per-literature key used by layout body-font memory."""
    if not path:
        return ""
    candidate = Path(path)
    if candidate.name != "full.cleaned.md":
        canonical = candidate.parent / "full.cleaned.md"
        if canonical.exists():
            candidate = canonical
    try:
        return os.path.normcase(str(candidate.resolve()))
    except OSError:
        return os.path.normcase(str(candidate.absolute()))


def layout_body_font_script(font_pt: float | None) -> str:
    """Apply a user override to body blocks after automatic layout fitting.

    Body blocks retain their individual unitless line-height ratios, so changing
    font size also changes the physical leading in lockstep.  Other block types
    (titles, captions, references and formulas) are deliberately untouched.
    """
    value = "null" if font_pt is None else f"{max(LAYOUT_BODY_FONT_MIN_PT, min(LAYOUT_BODY_FONT_MAX_PT, float(font_pt))):.2f}"
    return f"""
    (() => {{
      const requestedPt = {value};
      const selector = '.layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"]';
      const apply = () => {{
        const nodes = Array.from(document.querySelectorAll(selector));
        if (!nodes.length) return false;
        for (const node of nodes) {{
          if (requestedPt === null) {{
            if (node.dataset.userBodyFontPrevious) node.style.fontSize = node.dataset.userBodyFontPrevious;
            delete node.dataset.userBodyFontPrevious;
            delete node.dataset.userBodyFontPt;
          }} else {{
            if (!node.dataset.userBodyFontPrevious) {{
              node.dataset.userBodyFontPrevious = node.style.fontSize || getComputedStyle(node).fontSize || '';
            }}
            node.style.fontSize = `${{requestedPt}}pt`;
            node.dataset.userBodyFontPt = requestedPt.toFixed(2);
          }}
        }}
        document.body.dataset.userBodyFontPt = requestedPt === null ? '' : requestedPt.toFixed(2);
        if (window.__mineruDrawLayoutDebug) window.__mineruDrawLayoutDebug();
        return true;
      }};
      apply();
      window.setTimeout(apply, 80);
      window.setTimeout(apply, 300);
      window.setTimeout(apply, 900);
      return true;
    }})();
    """


def layout_body_font_probe_script() -> str:
    """Read the actual computed body font after layout fitting, in points."""
    return """
    (() => {
      const node = document.querySelector(
        '.layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"]'
      );
      if (!node) return JSON.stringify({ ready: false, fontPt: 0 });
      const px = Number.parseFloat(getComputedStyle(node).fontSize || '0');
      const ready = Boolean(
        document.body
        && document.body.dataset.layoutFitState === 'ready'
        && !document.body.classList.contains('layout-fit-pending')
      );
      return JSON.stringify({
        ready,
        fontPt: px > 0 ? px * 72 / 96 : 0
      });
    })();
    """


def reference_revision(path: Path | None = None, live_text: str = "") -> str:
    """A small, content-based identity for a particular rendered translation."""
    digest = hashlib.sha1()
    if live_text:
        digest.update(live_text.encode("utf-8", errors="replace"))
        return digest.hexdigest()
    if not path or not path.exists():
        return ""
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def canonical_reference_document_path(path_text: str | Path | None) -> Path | None:
    """Resolve old quote paths to the stable parsed-source document when possible."""
    if not path_text:
        return None
    path = Path(path_text)
    if not path.exists():
        return None
    source = path.parent / "full.cleaned.md"
    return source if source.exists() else path


def resolve_reference_quote(
    quote: dict,
    *,
    source_path: Path | None,
    translation_path: Path | None = None,
    live_translation: str = "",
) -> tuple[dict, str]:
    """Resolve a persisted quote against the currently available document revision.

    Translation geometry is intentionally treated as revision-scoped.  When it
    cannot be proven current, focus falls back to the stable original-document
    anchor instead of silently highlighting unrelated new translation text.
    """
    item = dict(quote or {})
    stable_path = canonical_reference_document_path(
        item.get("document_path") or item.get("source_markdown_path") or source_path or item.get("markdown_path")
    )
    if stable_path:
        item["document_path"] = str(stable_path)
        item["source_markdown_path"] = str(stable_path)

    if str(item.get("pane") or "source") != "translation":
        return item, "current"

    current_revision = reference_revision(translation_path, live_translation)
    recorded_revision = str(item.get("translation_revision") or "")
    if recorded_revision and current_revision and recorded_revision == current_revision:
        return item, "current"
    # Equations are preserved as TeX by the translation pipeline.  A TeX
    # fingerprint is therefore safe to re-anchor in a new translation revision.
    if item.get("type") == "formula" and item.get("formula_tex") and current_revision:
        item["translation_reanchored"] = True
        return item, "formula_semantic"
    # Historical formula quotations did not contain a revision.  Their TeX is
    # nevertheless a stable semantic key, so prefer an exact MathJax match in
    # the current translation over an unnecessarily vague source-page fallback.
    if not recorded_revision and item.get("type") == "formula" and item.get("formula_tex"):
        item["translation_revision_unknown"] = True
        return item, "legacy_formula_semantic"

    # A translated block can move after a retranslating run.  Reuse the source
    # anchor when available; the generic geometry is still a useful nearby
    # fallback for older quotations that predate source_anchor.
    source_anchor = item.get("source_anchor")
    if isinstance(source_anchor, dict):
        for key in ("anchor_page", "anchor_ratio", "anchor_rect", "anchor_point", "scroll_ratio"):
            if key in source_anchor:
                item[key] = source_anchor[key]
    item["pane"] = "source"
    item["translation_stale"] = True
    item["focus_notice"] = "译文已更新，已按原文锚点定位。"
    return item, "translation_stale"


def reference_focus_script(quote: dict) -> str:
    """Build the one-shot browser action used by every reader surface.

    The anchor is stored in page-relative coordinates, so page fitting, window
    resizing, and single-/dual-pane changes do not invalidate it.
    """
    payload = json.dumps(quote, ensure_ascii=False)
    return rf"""
    (() => {{
      const data = {payload};
      const root = document.scrollingElement || document.documentElement;
      const normalize = (value) => String(value || '').replace(/\\s+/g, '');
      const pageNumber = Number(data.anchor_page || data.page || 0);
      const page = pageNumber > 0
        ? document.querySelector(`[data-sync-page-index="${{pageNumber - 1}}"]`)
        : null;
      const rectData = data.anchor_rect && typeof data.anchor_rect === 'object' ? data.anchor_rect : null;
      const pointData = data.anchor_point && typeof data.anchor_point === 'object' ? data.anchor_point : null;
      let target = null;
      let exactRange = null;

      if (data.type === 'image' && data.image_src) {{
        target = [...document.images].find((node) =>
          node.currentSrc === data.image_src || node.src === data.image_src || node.getAttribute('src') === data.image_src) || null;
      }} else if (data.type === 'formula' && data.formula_tex) {{
        const tex = normalize(data.formula_tex);
        const annotation = [...document.querySelectorAll('annotation')].find((node) => normalize(node.textContent) === tex);
        target = annotation && (annotation.closest('mjx-container') || annotation.parentElement);
        if (!target) {{
          target = [...document.querySelectorAll('mjx-container')].find((node) => {{
            const label = normalize(node.getAttribute('aria-label'));
            const source = normalize(node.textContent);
            return label === tex || source === tex || label.includes(tex) || source.includes(tex);
          }}) || null;
        }}
      }} else if (data.text) {{
        const wanted = String(data.matched_evidence_text || data.text);
        const evidenceKey = value => String(value || '').normalize('NFKC').toLocaleLowerCase().replace(/[\s\p{{P}}\p{{S}}]+/gu, '');
        const segments = wanted.split(/(?:\.{{3,}}|…+|⋯+)/u).map(evidenceKey).filter(part => part.length >= 2);
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {{acceptNode(node) {{
          return node.parentElement && !node.parentElement.closest('script,style,#mineru-reference-focus') && node.nodeValue.trim()
            ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
        }}}});
        let raw = '', node; const rawMap = [];
        while ((node = walker.nextNode())) {{
          if (raw && !/\s$/.test(raw)) {{ raw += ' '; rawMap.push(null); }}
          for (let offset=0; offset<node.nodeValue.length; offset++) {{ raw += node.nodeValue[offset]; rawMap.push({{node,offset}}); }}
        }}
        const sentenceRanges = []; const sentencePattern = /[^.!?。！？]+(?:[.!?。！？]+|$)/gu; let sentence;
        while ((sentence = sentencePattern.exec(raw))) {{
          let start=sentence.index,end=sentence.index+sentence[0].length;
          while(start<end && /\s/.test(raw[start])) start++; while(end>start && /\s/.test(raw[end-1])) end--;
          if(end>start) sentenceRanges.push({{start,end,text:raw.slice(start,end)}});
        }}
        const coverage = (source,candidate) => {{
          const expected=evidenceKey(source),actual=evidenceKey(candidate); if(!expected||!actual)return 0; if(actual.includes(expected))return 1;
          const size=Math.min(3,expected.length), grams=new Set(); for(let i=0;i<=actual.length-size;i++)grams.add(actual.slice(i,i+size));
          let total=0,hit=0; for(let i=0;i<=expected.length-size;i++){{total++;if(grams.has(expected.slice(i,i+size)))hit++;}} return hit/Math.max(1,total);
        }};
        const ranked = sentenceRanges.map(item => {{
          const key=evidenceKey(item.text); let cursor=0,ordered=segments.length>0;
          for(const segment of segments){{const index=key.indexOf(segment,cursor);if(index<0){{ordered=false;break;}}cursor=index+segment.length;}}
          const segmentScore=segments.length?segments.reduce((sum,part)=>sum+coverage(part,item.text),0)/segments.length:0;
          return {{...item,score:ordered?1:Math.max(coverage(wanted,item.text),segmentScore)}};
        }}).sort((a,b)=>b.score-a.score||a.start-b.start);
        const best=ranked[0];
        if(best){{
          let start=best.start,end=best.end-1; while(start<=end&&!rawMap[start])start++; while(end>=start&&!rawMap[end])end--;
          const first=rawMap[start],last=rawMap[end];
          if(first&&last){{const range=document.createRange();range.setStart(first.node,first.offset);range.setEnd(last.node,last.offset+1);exactRange=range;target=first.node.parentElement;}}
        }}
      }}

      if (exactRange && target) {{
        target.scrollIntoView({{ block: 'center', behavior: 'auto' }});
      }} else if (page) {{
        page.scrollIntoView({{ block: 'start', behavior: 'auto' }});
        if (pointData) {{
          const pageRect = page.getBoundingClientRect();
          window.scrollBy(0, pageRect.height * Number(pointData.y || 0) - window.innerHeight * .22);
        }} else if (rectData) {{
          const pageRect = page.getBoundingClientRect();
          window.scrollBy(0, pageRect.height * Number(rectData.y || 0) - window.innerHeight * .22);
        }} else if (data.anchor_ratio !== null && data.anchor_ratio !== undefined) {{
          const pageRect = page.getBoundingClientRect();
          window.scrollBy(0, pageRect.height * Number(data.anchor_ratio || 0) - window.innerHeight * .22);
        }}
      }} else if (target) {{
        target.scrollIntoView({{ block: 'center', behavior: 'auto' }});
      }} else if (data.scroll_ratio !== null && data.scroll_ratio !== undefined) {{
        root.scrollTop = Math.max(0, (root.scrollHeight - root.clientHeight) * Number(data.scroll_ratio || 0));
      }}

      const draw = () => {{
        document.getElementById('mineru-reference-focus')?.remove();
        let pointTarget = null;
        if (page && pointData) {{
          const pageRect = page.getBoundingClientRect();
          const node = document.elementFromPoint(
            pageRect.left + pageRect.width * Number(pointData.x || 0),
            pageRect.top + pageRect.height * Number(pointData.y || 0)
          );
          pointTarget = node && node.closest
            ? node.closest('.layout-equation-formula, mjx-container, .layout-block, .layout-flow-stream')
            : null;
        }}
        let rangeRects = exactRange ? [...exactRange.getClientRects()].filter(item => item.width >= 2 && item.height >= 2) : [];
        let rect = pointTarget ? pointTarget.getBoundingClientRect() : (rangeRects[0] || null);
        if (!rect && target) rect = target.getBoundingClientRect();
        if ((!rect || rect.width < 2 || rect.height < 2) && page && rectData) {{
          const pageRect = page.getBoundingClientRect();
          rect = {{
            left: pageRect.left + pageRect.width * Number(rectData.x || 0),
            top: pageRect.top + pageRect.height * Number(rectData.y || 0),
            width: pageRect.width * Math.max(.01, Number(rectData.width || .08)),
            height: pageRect.height * Math.max(.01, Number(rectData.height || .035))
          }};
        }}
        if ((!rect || rect.width < 2 || rect.height < 2) && page && data.anchor_ratio !== null && data.anchor_ratio !== undefined) {{
          const pageRect = page.getBoundingClientRect();
          rect = {{ left: pageRect.left + pageRect.width * .08, top: pageRect.top + pageRect.height * Number(data.anchor_ratio || 0), width: pageRect.width * .84, height: Math.max(12, pageRect.height * .03) }};
        }}
        if ((!rect || rect.width < 2 || rect.height < 2) && page && data.type === 'formula') {{
          const pageRect = page.getBoundingClientRect();
          rect = {{ left: pageRect.left + pageRect.width * .08, top: pageRect.top + pageRect.height * .46, width: pageRect.width * .84, height: Math.max(14, pageRect.height * .06) }};
        }}
        if (!rect || rect.width < 2 || rect.height < 2) return;
        if (!rangeRects.length) rangeRects = [rect];
        const overlay = document.createElement('div');
        overlay.id = 'mineru-reference-focus';
        overlay.setAttribute('aria-hidden', 'true');
        overlay.style.cssText = 'position:fixed;inset:0;z-index:2147483646;pointer-events:none';
        for (const item of rangeRects) {{
          const mark = document.createElement('span');
          mark.style.cssText = `position:absolute;box-sizing:border-box;left:${{Math.max(0,item.left-2)}}px;top:${{Math.max(0,item.top-2)}}px;width:${{Math.max(8,item.width+4)}}px;height:${{Math.max(8,item.height+4)}}px;border:1.5px solid #E0A000;background:rgba(224,160,0,.13);box-shadow:0 0 0 2px rgba(224,160,0,.12);border-radius:2px;animation:mineruReferenceFocus .25s ease-out;`;
          overlay.appendChild(mark);
        }}
        const style = document.getElementById('mineru-reference-focus-style') || document.head.appendChild(document.createElement('style'));
        style.id = 'mineru-reference-focus-style';
        style.textContent = '@keyframes mineruReferenceFocus{{from{{opacity:.2;transform:scale(1.02)}}to{{opacity:1;transform:scale(1)}}}}@media (prefers-reduced-motion:reduce){{#mineru-reference-focus span{{animation:none!important}}}}';
        document.body.appendChild(overlay);
        window.setTimeout(() => overlay.remove(), 2000);
      }};
      requestAnimationFrame(() => requestAnimationFrame(draw));
    }})();
    """


def focus_reference_quote(web_view, fallback_viewer, quote: dict) -> None:
    """Focus a quote in the requested pane, with a QTextBrowser fallback."""
    if web_view:
        web_view.page().runJavaScript(reference_focus_script(quote))
        return
    text = str(quote.get("text") or quote.get("formula_tex") or "").strip()
    if not text or fallback_viewer is None:
        return
    cursor = fallback_viewer.document().find(text)
    if not cursor or cursor.isNull():
        return
    fallback_viewer.setTextCursor(cursor)
    fallback_viewer.ensureCursorVisible()
    def clear_selection(view=fallback_viewer):
        current = view.textCursor()
        current.clearSelection()
        view.setTextCursor(current)
    QTimer.singleShot(2000, clear_selection)


class ModeToggleButton(QPushButton):
    """Segmented reading-mode button with rich current-state emphasis."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self._layout_mode = True
        self.setMinimumWidth(178)
        self.setMinimumHeight(42)

    def set_layout_mode(self, is_layout: bool):
        self._layout_mode = bool(is_layout)
        self.setChecked(self._layout_mode)
        self.setText("")
        self.setToolTip("当前为排版阅读。点击切换为流式阅读。" if self._layout_mode else "当前为流式阅读。点击切换为排版阅读。")
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        try:
            rect = self.rect()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            normal_font = QFont(self.font())
            active_font = QFont(self.font())
            active_font.setPointSize(max(normal_font.pointSize() + 2, 15))
            active_font.setBold(True)
            inactive_color = QColor(COLOR_TEXT_SECONDARY)
            active_color = QColor("#FFFFFF" if self.isChecked() else COLOR_TEXT_PRIMARY)
            slash_color = QColor(COLOR_TEXT_MUTED)

            parts = [("流式", not self._layout_mode), (" / ", False), ("排版", self._layout_mode)]
            metrics = []
            total = 0
            for text, active in parts:
                font = active_font if active else normal_font
                painter.setFont(font)
                width = painter.fontMetrics().horizontalAdvance(text)
                metrics.append((text, active, width, font))
                total += width
            x = rect.center().x() - total / 2
            baseline = rect.center().y() + painter.fontMetrics().ascent() / 2 - 2
            for text, active, width, font in metrics:
                painter.setFont(font)
                if text.strip() == "/":
                    painter.setPen(slash_color)
                else:
                    painter.setPen(active_color if active else inactive_color)
                painter.drawText(int(x), int(baseline), text)
                x += width
        finally:
            painter.end()


class MonolithMark(QLabel):
    """以透明背景显示应用图标，替代原先的几何三角标记。"""

    def __init__(self, size: int = 34, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background: transparent; border: none;")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon_path = os.path.join(get_base_path(), "resources", "icon.ico")
        pixmap = QPixmap(icon_path)
        if not pixmap.isNull():
            self.setPixmap(
                pixmap.scaled(
                    size,
                    size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )


def populate_translation_provider_combo(combo: QComboBox) -> None:
    combo.clear()
    for provider_id, label in TRANSLATION_PROVIDER_CHOICES:
        combo.addItem(label, provider_id)


def style_reasoning_effort_combo(combo: QComboBox) -> None:
    combo.setFixedHeight(28)
    combo.setStyleSheet(f"""
        QComboBox {{
            min-height: 26px;
            max-height: 26px;
            padding: 2px 22px 2px 8px;
            font-size: 13px;
            background: {COLOR_BG_SURFACE_2};
            color: {COLOR_TEXT_PRIMARY};
            border: 1px solid {COLOR_BORDER_HAIR};
            border-radius: {RADIUS_MD};
        }}
        QComboBox:disabled {{
            color: {COLOR_TEXT_DISABLED};
            background: {COLOR_BG_INSET};
            border-color: {COLOR_BORDER_HAIR};
        }}
        QComboBox:hover {{
            border-color: #90908A;
        }}
        QComboBox:focus {{
            border-color: {COLOR_BORDER_STRONG};
        }}
        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 20px;
            border: none;
            border-left: 1px solid {COLOR_BORDER_HAIR};
            background: {COLOR_BG_SURFACE};
            border-top-right-radius: {RADIUS_MD};
            border-bottom-right-radius: {RADIUS_MD};
        }}
    """)


def translation_provider_choice_id(provider_id: str) -> str:
    """Keep saved pre-split Google configurations selectable in the UI."""
    return machine_translate.normalize_network_provider_id(provider_id)


def translation_provider_settings_title(provider_id: str) -> str:
    """Return the rich-text heading shown above one translation provider."""
    normalized = translation_provider_choice_id(provider_id)
    if machine_translate.is_machine_translation_provider(normalized):
        return "<b>翻译模型</b>（当前服务无需 API 密钥）"
    provider_page = TRANSLATION_PROVIDER_KEY_PAGES.get(normalized)
    if provider_page:
        display_name, url = provider_page
        return (
            f'<b>翻译模型</b>（<a href="{url}">点击访问 {display_name} API 官网创建 Key</a>）'
        )
    return "<b>翻译模型</b>（请从你使用的服务后台创建并填写 API 密钥）"


LOCAL_TRANSLATION_LANGUAGES = (
    ("英文", "英文"),
    ("日文", "日文"),
    ("韩文", "韩文"),
    ("德文", "德文"),
    ("法文", "法文"),
    ("西班牙文", "西班牙文"),
    ("简体中文", "简体中文"),
)


EDGE_TRANSLATION_LANGUAGES = (
    ("简体中文", "简体中文"),
    ("繁体中文", "繁体中文"),
    ("英文", "英文"),
    ("日文", "日文"),
    ("韩文", "韩文"),
    ("德文", "德文"),
    ("法文", "法文"),
    ("西班牙文", "西班牙文"),
    ("意大利文", "意大利文"),
    ("葡萄牙文", "葡萄牙文"),
    ("俄文", "俄文"),
)


LOCAL_TRANSLATION_TARGET_LANGUAGES = (
    ("简体中文", "简体中文"),
    ("英文", "英文"),
)


def populate_local_language_combo(combo: QComboBox, include_auto: bool = False) -> None:
    combo.clear()
    if include_auto:
        combo.addItem("自动检测（默认按英文处理）", "auto")
    available_pairs = machine_translate.mtran_available_language_pairs()
    source_codes = {source for source, _target in available_pairs}
    for code in sorted(source_codes, key=lambda item: machine_translate.mtran_language_label(item)):
        label = machine_translate.mtran_language_label(code)
        combo.addItem(label, label)


def populate_local_target_language_combo(combo: QComboBox, source_language: str = "") -> None:
    combo.clear()
    available_pairs = machine_translate.mtran_available_language_pairs()
    source_code = machine_translate.mtran_language_code(source_language or "英文")
    target_codes = {target for source, target in available_pairs if source == source_code}
    for code in sorted(target_codes, key=lambda item: machine_translate.mtran_language_label(item)):
        label = machine_translate.mtran_language_label(code)
        combo.addItem(label, label)


def populate_edge_language_combo(combo: QComboBox) -> None:
    combo.clear()
    for label, value in EDGE_TRANSLATION_LANGUAGES:
        combo.addItem(label, value)


def provider_runtime_default_url(provider_id: str) -> str:
    return normalize_ai_base_url(provider_default_base_url(provider_id), provider_id)


def apply_model_options_to_combo(combo: QComboBox, options: list[TranslationModelOption], preferred_model: str) -> str:
    combo.clear()
    for option in options:
        combo.addItem(option.display_text, option.model_id)
    selected_model = choose_preferred_translation_model(str(combo.property("provider_id") or ""), options, preferred_model)
    if selected_model:
        index = combo.findData(selected_model)
        if index >= 0:
            combo.setCurrentIndex(index)
    return selected_model


def web_view_url_content_signature(url: QUrl) -> tuple[str, int, int]:
    """返回足以判断本地预览内容是否变化的轻量指纹。"""
    url_text = url.toString()
    local_path = url.toLocalFile()
    if not local_path:
        return (url_text, 0, 0)
    try:
        stat = Path(local_path).stat()
        return (url_text, int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return (url_text, 0, 0)


def layout_artifact_identity(html_path: Path) -> str:
    """Identity shared by cached PDF and captured Word layout state."""
    try:
        stat = html_path.stat()
        value = f"{html_path.resolve()}::{stat.st_mtime_ns}::{stat.st_size}"
        return hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()
    except OSError:
        return ""


def layout_pdf_cache_paths(html_path: Path) -> tuple[Path, Path]:
    cache_path = html_path.with_name(f"{html_path.stem}.final-layout.pdf")
    return cache_path, cache_path.with_suffix(".pdf.meta.json")


def invalidate_layout_pdf_cache(html_path: Path) -> None:
    """Discard only regenerable PDF cache files after visible state changes."""
    for path in layout_pdf_cache_paths(html_path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def layout_state_cache_path(html_path: Path) -> Path:
    return html_path.with_name(f"{html_path.stem}.final-layout-state.json")


def read_current_layout_cache_payload(cache_path: Path, html_path: Path, payload_key: str):
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("identity") != layout_artifact_identity(html_path):
        return None
    return payload.get(payload_key)


def write_layout_cache_payload(cache_path: Path, html_path: Path, payload_key: str, value) -> None:
    payload = json.dumps(
        {"identity": layout_artifact_identity(html_path), payload_key: value},
        ensure_ascii=False,
        indent=2,
    )
    temporary = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, cache_path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_layout_fit_disk_cache(html_path: Path, state: dict) -> bool:
    """Embed the completed browser fit for reliable file:// revisits.

    Some QWebEngine builds do not retain localStorage for local files.  The
    preview already has a reserved bootstrap marker, so persist the exact
    style payload there after the first completed fit.
    """
    fit_cache = state.get("fit_cache") if isinstance(state, dict) else None
    try:
        html_text = html_path.read_text(encoding="utf-8", errors="replace")
        cache_version = layout_fit_cache_version_from_html(html_text)
        if not layout_fit_cache_payload_is_complete(fit_cache, cache_version):
            return False
        payload = json.dumps(fit_cache, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        replacement = (
            '<script data-layout-fit-disk-cache>'
            f'window.__mineruDiskFitCache={payload};'
            '(()=>{const restore=()=>{const body=document.body;const value=window.__mineruDiskFitCache;'
            # This script runs after the ordinary localStorage bootstrap. The
            # newly captured visible-reader state must replace an older cache.
            "if(!body||!value)return;const version=body.dataset.layoutCacheVersion||'';"
            "if(!version||value.version!==version||value.complete!==true||!Array.isArray(value.styles))return;"
            "const key=`${version}:${body.dataset.layoutCacheScope||''}:${body.dataset.layoutCacheKey||''}`;"
            "window.__mineruInitialFitCache={key,payload:value};body.classList.add('layout-fit-cache-hit');"
            "body.dataset.layoutFitState='restoring-cache';};if(document.body)restore();"
            "else document.addEventListener('DOMContentLoaded',restore,{once:true});})();</script>"
        )
        marker_pattern = r'<script data-layout-fit-disk-cache>.*?</script>'
        marker = re.search(marker_pattern, html_text, flags=re.S)
        if marker and marker.group(0) == replacement:
            return False
        if marker:
            updated = html_text[:marker.start()] + replacement + html_text[marker.end():]
        else:
            updated = html_text.replace('<main class="layout-doc">', f'{replacement}<main class="layout-doc">', 1)
        if updated == html_text:
            return False
        temporary = html_path.with_name(f".{html_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(updated, encoding="utf-8")
            os.replace(temporary, html_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return True
    except OSError:
        return False


def layout_fit_cache_version_from_html(html_text: str) -> str:
    match = re.search(r'data-layout-cache-version="([^"]+)"', str(html_text or ""), flags=re.I)
    return html.unescape(match.group(1)).strip() if match else ""


def layout_fit_cache_payload_is_complete(payload, expected_version: str) -> bool:
    if not isinstance(payload, dict) or not expected_version:
        return False
    styles = payload.get("styles")
    if (
        payload.get("version") != expected_version
        or payload.get("complete") is not True
        or not isinstance(styles, list)
        or payload.get("count") != len(styles)
    ):
        return False
    return all(
        isinstance(style, dict) and {"f", "l", "o"}.issubset(style)
        for style in styles
    )


PREVIEW_HISTORY_REUSE_LIMIT = 2


def set_or_reload_web_view_url(web_view, url: QUrl, *, force_reload: bool = False) -> bool:
    """仅在 URL 或本地文件内容确实变化时导航，避免反复重建整篇论文 DOM。"""
    if web_view is None:
        return False
    signature = web_view_url_content_signature(url)
    signature_by_url = getattr(web_view, "_preview_signature_by_url", {})
    # Refresh insertion order on every visit so this is a genuine two-document
    # LRU rather than a record of the first pages opened in this app session.
    known_signature = signature_by_url.pop(signature[0], None)
    should_trim_browser_history = (
        known_signature is None and len(signature_by_url) >= PREVIEW_HISTORY_REUSE_LIMIT
    )
    signature_by_url[signature[0]] = signature
    while len(signature_by_url) > PREVIEW_HISTORY_REUSE_LIMIT:
        signature_by_url.pop(next(iter(signature_by_url)))
    web_view._preview_signature_by_url = signature_by_url
    current_url = web_view.url()
    if not url.isEmpty() and current_url.toString() == url.toString():
        previous_signature = getattr(web_view, "_preview_content_signature", None)
        if force_reload or previous_signature != signature:
            web_view._preview_content_signature = signature
            web_view.reload()
            return True
        return False
    else:
        web_view._preview_content_signature = signature
        # 优先回到 WebEngine 历史页；Chromium 可直接复用最近页面的 DOM/图片/脚本状态。
        if not force_reload and known_signature == signature:
            try:
                history = web_view.history()
                candidates = (
                    list(reversed(history.backItems(PREVIEW_HISTORY_REUSE_LIMIT)))
                    + list(history.forwardItems(PREVIEW_HISTORY_REUSE_LIMIT))
                )
                history_item = next((item for item in candidates if item.url().toString() == signature[0]), None)
                if history_item is not None:
                    history.goToItem(history_item)
                    return True
            except (AttributeError, RuntimeError):
                pass
        if should_trim_browser_history:
            # QWebEngine cannot remove one arbitrary history item.  Clearing
            # before the next navigation retains the current document; after
            # setUrl() finishes, history therefore contains only current + new.
            try:
                web_view.history().clear()
            except (AttributeError, RuntimeError):
                pass
        web_view.setUrl(url)
        return True


class ModelOptionsFetchWorker(QThread):
    """在后台刷新模型列表，启动阶段绝不阻塞 Qt 事件循环。"""

    finished_signal = Signal(list, str)

    def __init__(self, provider_id: str, api_key: str, base_url: str):
        super().__init__()
        self.provider_id = provider_id
        self.api_key = api_key
        self.base_url = base_url

    def run(self):
        try:
            result = http_json(
                "GET",
                provider_model_list_url(self.provider_id, self.base_url),
                token=self.api_key,
                timeout=10,
            )
            models = result.get("data") or []
            options = build_translation_model_options(
                self.provider_id,
                list(models) if isinstance(models, list) else [],
            )
            self.finished_signal.emit(options, "")
        except Exception as exc:
            self.finished_signal.emit([], str(exc))


def layout_preview_source_stamp(path: Path) -> str:
    try:
        import re

        marker = re.search(
            r"<!--\s*layout-preview version=([^>]+?)\s*-->",
            path.read_text(encoding="utf-8", errors="ignore")[:4096],
            flags=re.IGNORECASE,
        )
        if not marker:
            return ""
        return hashlib.sha1(marker.group(0).encode("utf-8", errors="replace")).hexdigest()[:16]
    except OSError:
        return ""


def layout_translation_preview_is_current(path: Path, source_layout_path: Path | None = None) -> bool:
    try:
        html_head = path.read_text(encoding="utf-8", errors="ignore")[:4096]
        # 旧运行时没有新版译文正文行距，需要利用已保存的译文数据重新生成预览。
        if "layout-translation-runtime-v29-gallop-tail" not in html_head:
            return False
        if source_layout_path is None:
            return True
        source_stamp = layout_preview_source_stamp(source_layout_path)
        return bool(source_stamp) and f"layout-translation-source-stamp={source_stamp}" in html_head
    except OSError:
        return False


def layout_html_has_complete_disk_fit_cache(path: Path | str | None) -> bool:
    """Return whether a generated layout can be shown without a Qt cover."""
    if not path:
        return False
    try:
        # The injected cache marker precedes <main>; one bounded read avoids
        # loading a multi-megabyte manual merely to decide transition chrome.
        with Path(path).open("r", encoding="utf-8", errors="ignore") as stream:
            html_head = stream.read(1_048_576)
    except (OSError, TypeError, ValueError):
        return False
    if "data-layout-fit-disk-cache" not in html_head:
        return False
    match = re.search(r"window\.__mineruDiskFitCache=(\{.*?\});", html_head, flags=re.S)
    if not match:
        return False
    try:
        payload = json.loads(match.group(1))
    except (TypeError, ValueError):
        return False
    return layout_fit_cache_payload_is_complete(
        payload,
        layout_fit_cache_version_from_html(html_head),
    )


def is_layout_preview_html_path(path: Path | str | None) -> bool:
    """Return whether a local HTML artifact belongs to the fixed-layout reader.

    This deliberately relies on the generated artifact name, rather than the
    document body.  The check happens before a new page starts loading, so a
    reader-only transition can retain the last rendered frame without parsing
    the new (potentially large) HTML on the Qt event loop.
    """
    if not path:
        return False
    try:
        name = Path(path).name.lower()
    except (OSError, TypeError):
        return False
    return name.startswith("preview_layout_") and name.endswith(".html")


LAYOUT_LOADING_NOTICE_COMPAT_MARKER = "mineru-layout-loading-notice-v2"


def upgrade_layout_loading_notice_html(html_path: Path | str | None) -> None:
    """Upgrade retained layout pages before WebEngine parses their loading UI."""
    if not is_layout_preview_html_path(html_path):
        return
    try:
        path = Path(html_path)
        source = path.read_text(encoding="utf-8")
        if LAYOUT_LOADING_NOTICE_COMPAT_MARKER in source or "</head>" not in source:
            return
        style = f"""
<!-- {LAYOUT_LOADING_NOTICE_COMPAT_MARKER} -->
<style>
body.layout-fit-pending::before {{
  top: 14px !important;
  right: 14px !important;
  left: auto !important;
  transform: none !important;
  max-width: min(360px, calc(100vw - 48px));
  padding: 0 !important;
  color: #334e68 !important;
  background: transparent !important;
  border: 0 !important;
  border-radius: 0 !important;
  box-shadow: none !important;
  font: 600 11px/1.35 "Cascadia Mono", "Microsoft YaHei UI", monospace !important;
  letter-spacing: .02em !important;
}}
</style>
"""
        updated = source.replace("</head>", f"{style}</head>", 1)
        temp_path = path.with_name(f".{path.name}.notice.tmp")
        temp_path.write_text(updated, encoding="utf-8")
        os.replace(temp_path, path)
    except OSError:
        # A preview can be replaced by the renderer concurrently.  The freshly
        # generated file already contains the current style, so no retry is needed.
        return


def layout_fit_ready_probe_script() -> str:
    """Small, read-only readiness probe used only by the visible reader."""
    return """
    (() => Boolean(
      document.body
      && document.body.dataset.layoutFitState === 'ready'
      && !document.body.classList.contains('layout-fit-pending')
      && document.querySelectorAll('.layout-page-wrap').length > 0
    ))();
    """


class LayoutPreviewRefreshWorker(QThread):
    """使用已保存的译文 bundle 后台升级预览，不重新请求翻译模型。"""

    finished_signal = Signal(str, str, str)

    def __init__(self, markdown_path: Path, output_path: Path):
        super().__init__()
        self.markdown_path = Path(markdown_path)
        self.output_path = Path(output_path)

    def run(self):
        tmp_path = self.output_path.with_name(f".{self.output_path.name}.refresh.tmp")
        try:
            import layout_translate_preview as module

            translated_bundle = load_layout_translation_bundle(self.markdown_path)
            if not translated_bundle:
                raise MinerUError("缺少已保存的排版译文数据")
            source_layout_path = render_layout_preview_html(
                self.markdown_path,
                strict_fit=False,
                debug_overlay=False,
            )
            if not source_layout_path:
                raise MinerUError("无法生成新版原文版面预览")
            module.render_translated_layout(
                self.markdown_path,
                translated_bundle,
                source_layout_path,
                tmp_path,
                debug_overlay=False,
                reset_fit_cache=False,
            )
            # Cache identity includes the generated source-runtime stamp. A
            # runtime or DOM change therefore performs one fresh document-wide
            # fit; page-local styles are never migrated into a new artifact.
            tmp_path.replace(self.output_path)
            self.finished_signal.emit(str(self.markdown_path), str(self.output_path), "")
        except Exception as exc:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            self.finished_signal.emit(str(self.markdown_path), "", str(exc))


def install_layout_image_lightbox(web_view) -> None:
    """Install the image viewer in already-generated layout translation pages.

    Layout translation HTML is retained on disk, so it can predate new viewer
    code.  Injecting this small compatibility layer after every web load keeps
    existing translated documents clickable without requiring retranslation.
    """
    if web_view is None:
        return
    script = r"""
(() => {
  if (!document.body || !document.body.classList.contains('layout-translated')) return;
  let box = document.getElementById('layout-image-lightbox');
  const generatedViewer = Boolean(box && box.dataset.runtimeBound);
  // Legacy rendered pages have their own event listeners.  Replace that node
  // so this compatibility viewer is the sole owner of clicking, dragging, and
  // mouse-centered zooming.
  if (box && !generatedViewer) {
    const replacement = box.cloneNode(true);
    box.replaceWith(replacement);
    box = replacement;
  }
  if (!document.getElementById('layout-image-lightbox-style')) {
    const style = document.createElement('style');
    style.id = 'layout-image-lightbox-style';
    style.textContent = `#layout-image-lightbox{position:fixed;inset:0;z-index:9999;display:none;align-items:center;justify-content:center;background:rgba(16,24,32,.76);cursor:grab;touch-action:none;user-select:none}#layout-image-lightbox.open{display:flex}#layout-image-lightbox img{max-width:none;max-height:none;margin:0;cursor:grab;transform-origin:0 0;-webkit-user-drag:none;user-select:none}#layout-image-lightbox .hint{position:fixed;left:18px;bottom:14px;color:#eef5f7;font-size:12px;background:rgba(10,20,30,.54);padding:6px 9px;border-radius:5px}img.layout-media{cursor:zoom-in}`;
    document.head.appendChild(style);
  }
  if (!box) {
    box = document.createElement('div');
    box.id = 'layout-image-lightbox';
    box.innerHTML = '<img alt=""><div class="hint">滚轮缩放 · 左键拖动 · 单击退出</div>';
    document.body.appendChild(box);
  }
  if (generatedViewer) return;
  const image = box.querySelector('img');
  image.draggable = false;
  image.addEventListener('dragstart', (event) => event.preventDefault());
  box.dataset.runtimeBound = '1';
  let scale = 1, tx = 0, ty = 0, dragging = false, sx = 0, sy = 0, moved = false;
  const apply = () => { image.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`; };
  const close = () => { dragging = false; box.classList.remove('open'); image.removeAttribute('src'); };
  for (const node of document.querySelectorAll('img.layout-media')) {
    node.addEventListener('click', (event) => {
      event.preventDefault(); event.stopPropagation();
      image.src = node.currentSrc || node.src;
      scale = 1; tx = 0; ty = 0; moved = false; apply();
      box.classList.add('open');
    });
  }
  // Reset the drag marker for all new presses.  A prior image drag must not
  // make later clicks on the backdrop unable to close the viewer.
  box.addEventListener('click', () => { if (!moved) close(); });
  box.addEventListener('wheel', (event) => {
    event.preventDefault();
    const previous = scale;
    scale = Math.max(.2, Math.min(8, scale * (event.deltaY < 0 ? 1.12 : 1 / 1.12)));
    const rect = image.getBoundingClientRect();
    tx -= (event.clientX - rect.left) * (scale / previous - 1);
    ty -= (event.clientY - rect.top) * (scale / previous - 1);
    apply();
  }, { passive: false });
  box.addEventListener('pointerdown', (event) => {
    moved = false;
    if (event.target !== image) return;
    dragging = true; sx = event.clientX - tx; sy = event.clientY - ty;
  });
  box.addEventListener('pointermove', (event) => {
    if (!dragging) return;
    const nextX = event.clientX - sx, nextY = event.clientY - sy;
    if (Math.abs(nextX - tx) + Math.abs(nextY - ty) > 3) moved = true;
    tx = nextX; ty = nextY; apply();
  });
  box.addEventListener('pointerup', () => { dragging = false; });
  box.addEventListener('pointercancel', () => { dragging = false; });
})();
"""
    try:
        web_view.page().runJavaScript(script)
    except RuntimeError:
        pass


def install_layout_image_memory_manager(web_view) -> None:
    """Keep layout text searchable while bounding decoded off-screen images."""
    if web_view is None:
        return
    script = r"""
(() => {
  const pages = [...document.querySelectorAll('.layout-page-wrap')];
  if (!pages.length || window.__mineruImageMemoryManagerInstalled) return;
  window.__mineruImageMemoryManagerInstalled = true;
  const pageImages = (page) => [...page.querySelectorAll('img.layout-media')];
  for (const page of pages) {
    const renderedHeight = Math.max(1, page.getBoundingClientRect().height);
    page.style.containIntrinsicSize = `auto ${renderedHeight}px`;
    page.style.contentVisibility = 'auto';
    for (const image of pageImages(page)) {
      const source = image.getAttribute('src') || image.currentSrc || '';
      if (source) image.dataset.mineruDeferredSrc = source;
      image.loading = 'lazy';
      image.decoding = 'async';
    }
  }
  const loadPage = (page) => {
    page.dataset.mineruImageNear = '1';
    for (const image of pageImages(page)) {
      const source = image.dataset.mineruDeferredSrc || '';
      if (source && !image.getAttribute('src')) image.setAttribute('src', source);
      image.style.visibility = '';
    }
  };
  const unloadPage = (page) => {
    page.dataset.mineruImageNear = '0';
    window.setTimeout(() => {
      if (page.dataset.mineruImageNear === '1') return;
      for (const image of pageImages(page)) {
        const source = image.getAttribute('src') || image.currentSrc || image.dataset.mineruDeferredSrc || '';
        if (source) image.dataset.mineruDeferredSrc = source;
        image.style.visibility = 'hidden';
        image.removeAttribute('src');
      }
    }, 500);
  };
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) loadPage(entry.target);
      else unloadPage(entry.target);
    }
  }, { root: null, rootMargin: '2200px 0px', threshold: 0 });
  for (const page of pages) observer.observe(page);
  window.__mineruImageMemoryObserver = observer;
})();
"""
    try:
        web_view.page().runJavaScript(script)
    except RuntimeError:
        pass


def install_layout_loading_notice_style(web_view) -> None:
    """Apply the compact loading status style to existing layout pages."""
    if web_view is None:
        return
    script = r"""
    (() => {
      if (!document.body || !document.body.dataset.layoutProgress) return;
      const id = 'mineru-layout-loading-notice-style';
      if (document.getElementById(id)) return;
      const style = document.createElement('style');
      style.id = id;
      style.textContent = `body.layout-fit-pending::before{
        top:14px!important;right:14px!important;left:auto!important;transform:none!important;
        max-width:min(360px,calc(100vw - 48px));padding:0!important;
        color:#334e68!important;background:transparent!important;border:0!important;
        border-radius:0!important;box-shadow:none!important;
        font:600 11px/1.35 "Cascadia Mono","Microsoft YaHei UI",monospace!important;
        letter-spacing:.02em!important;
      }`;
      document.head.appendChild(style);
    })();
    """
    try:
        web_view.page().runJavaScript(script)
    except RuntimeError:
        pass


def install_reader_scrollbar_style(web_view) -> None:
    """Render a native-PDF-like scrollbar above WebEngine document previews."""
    if web_view is None:
        return
    script = r"""
    (() => {
      if (!document.documentElement || !document.body) return;
      const id = 'litmtrans-reader-scrollbar-style';
      if (document.getElementById(id)) return;
      const style = document.createElement('style');
      style.id = id;
      style.textContent = `
        html { scrollbar-width: none !important; }
        ::-webkit-scrollbar { width: 0 !important; height: 0 !important; }
        #litmtrans-reader-scrollbar {
          position: fixed; z-index: 2147483647; top: 0; right: 0; bottom: 0;
          width: 8px; background: #EFEFEC; cursor: default; user-select: none;
          touch-action: none;
        }
        #litmtrans-reader-scrollbar-thumb {
          position: absolute; left: 0; width: 8px; min-height: 32px;
          background: #8A8A84; border: 0; border-radius: 0;
        }
        #litmtrans-reader-scrollbar:hover #litmtrans-reader-scrollbar-thumb {
          background: #0A0A0A;
        }
      `;
      document.head.appendChild(style);
      const track = document.createElement('div');
      track.id = 'litmtrans-reader-scrollbar';
      const thumb = document.createElement('div');
      thumb.id = 'litmtrans-reader-scrollbar-thumb';
      track.appendChild(thumb);
      document.body.appendChild(track);
      const root = () => document.scrollingElement || document.documentElement;
      const metrics = () => {
        const scrollRoot = root();
        const height = Math.max(1, window.innerHeight || document.documentElement.clientHeight || 1);
        const contentHeight = Math.max(height, scrollRoot.scrollHeight || 0);
        const maximum = Math.max(0, contentHeight - height);
        const thumbHeight = Math.min(height, Math.max(32, Math.round(height * height / contentHeight)));
        return { scrollRoot, height, maximum, thumbHeight };
      };
      const render = () => {
        const { scrollRoot, height, maximum, thumbHeight } = metrics();
        if (maximum <= 0) {
          track.style.display = 'none';
          return;
        }
        track.style.display = 'block';
        thumb.style.height = `${thumbHeight}px`;
        const progress = Math.max(0, Math.min(1, Number(scrollRoot.scrollTop || 0) / maximum));
        thumb.style.transform = `translateY(${Math.round((height - thumbHeight) * progress)}px)`;
      };
      const setScrollFromPointer = (clientY, centreThumb) => {
        const { scrollRoot, height, maximum, thumbHeight } = metrics();
        if (maximum <= 0) return;
        const offset = centreThumb ? thumbHeight / 2 : 0;
        const trackY = Math.max(0, Math.min(height - thumbHeight, clientY - offset));
        scrollRoot.scrollTop = maximum * trackY / Math.max(1, height - thumbHeight);
      };
      let dragging = false;
      let dragOffset = 0;
      track.addEventListener('pointerdown', (event) => {
        const { thumbHeight } = metrics();
        const thumbTop = thumb.getBoundingClientRect().top;
        dragging = true;
        dragOffset = event.target === thumb ? event.clientY - thumbTop : thumbHeight / 2;
        track.setPointerCapture(event.pointerId);
        setScrollFromPointer(event.clientY, event.target !== thumb);
        event.preventDefault();
      });
      track.addEventListener('pointermove', (event) => {
        if (!dragging) return;
        const { scrollRoot, height, maximum, thumbHeight } = metrics();
        if (maximum > 0) {
          const trackY = Math.max(0, Math.min(height - thumbHeight, event.clientY - dragOffset));
          scrollRoot.scrollTop = maximum * trackY / Math.max(1, height - thumbHeight);
        }
        event.preventDefault();
      });
      const stopDragging = () => { dragging = false; };
      track.addEventListener('pointerup', stopDragging);
      track.addEventListener('pointercancel', stopDragging);
      window.addEventListener('scroll', render, { passive: true });
      window.addEventListener('resize', render, { passive: true });
      new ResizeObserver(render).observe(document.body);
      render();
    })();
    """
    try:
        web_view.page().runJavaScript(script)
    except RuntimeError:
        pass


def install_layout_formula_lightbox_compat(web_view) -> None:
    """Upgrade the formula viewer embedded in already-rendered layout HTML.

    Generated previews are deliberately retained on disk.  This small runtime
    shim makes pre-existing previews receive the current close and first-fit
    behavior as soon as they are loaded in a newer application build.
    """
    if web_view is None:
        return
    script = r"""
(() => {
  if (!document.body || !document.body.classList.contains('layout-translated')) return;
  const box = document.getElementById('layout-formula-lightbox');
  if (!box || box.dataset.formulaViewerVersion === '2' || box.dataset.formulaCompatBound) return;
  box.dataset.formulaCompatBound = '1';
  const stage = box.querySelector('.formula-stage');
  const hint = box.querySelector('.hint');
  if (hint) hint.textContent = '滚轮缩放 · 左键拖动 · 单击退出';
  let dragging = false, moved = false, startX = 0, startY = 0;
  let scale = 1, tx = 0, ty = 0, sx = 0, sy = 0;
  const apply = () => { if (stage) stage.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`; };
  const close = () => {
    if (!box.classList.contains('open')) return;
    const formula = stage && stage.querySelector('mjx-container');
    const placeholder = document.querySelector('.layout-formula-placeholder');
    if (formula && placeholder) {
      formula.removeAttribute('style');
      placeholder.replaceWith(formula);
    }
    if (stage) { stage.style.transform = ''; stage.textContent = ''; }
    box.classList.remove('open');
    dragging = false;
  };
  // The old listener only closes a backdrop click and retains its previous
  // drag marker.  Track this gesture independently so every later plain click
  // can still close the open reader.
  box.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    moved = false; startX = event.clientX; startY = event.clientY;
    dragging = Boolean(stage && stage.contains(event.target));
    if (!dragging) return;
    sx = event.clientX - tx; sy = event.clientY - ty;
    box.setPointerCapture(event.pointerId);
    event.stopImmediatePropagation();
  }, true);
  box.addEventListener('pointermove', (event) => {
    if (!dragging) return;
    if (Math.abs(event.clientX - startX) + Math.abs(event.clientY - startY) > 3) moved = true;
    tx = event.clientX - sx; ty = event.clientY - sy; apply();
    event.stopImmediatePropagation();
  }, true);
  box.addEventListener('pointerup', (event) => {
    if (!dragging) return;
    dragging = false; event.stopImmediatePropagation();
  }, true);
  box.addEventListener('pointercancel', (event) => {
    if (!dragging) return;
    dragging = false; event.stopImmediatePropagation();
  }, true);
  box.addEventListener('click', (event) => {
    event.stopImmediatePropagation();
    if (!moved) close();
  }, true);
  box.addEventListener('wheel', (event) => {
    event.preventDefault(); event.stopImmediatePropagation();
    const previous = scale;
    scale = Math.max(.2, Math.min(10, scale * (event.deltaY < 0 ? 1.12 : 1 / 1.12)));
    tx -= (event.clientX - window.innerWidth / 2 - tx) * (scale / previous - 1);
    ty -= (event.clientY - window.innerHeight / 2 - ty) * (scale / previous - 1);
    apply();
  }, { capture: true, passive: false });
  const fitFirstView = () => {
    if (!box.classList.contains('open') || !stage) return;
    const formula = stage.querySelector('mjx-container');
    if (!formula) return;
    stage.style.transform = '';
    const rect = stage.getBoundingClientRect();
    scale = Math.max(.2, Math.min(4, Math.min(
      (window.innerWidth * .86) / Math.max(1, rect.width),
      (window.innerHeight * .76) / Math.max(1, rect.height)
    )));
    tx = 0; ty = 0; apply();
  };
  new MutationObserver(() => { if (box.classList.contains('open')) requestAnimationFrame(fitFirstView); })
    .observe(box, { attributes: true, attributeFilter: ['class'] });
})();
"""
    try:
        web_view.page().runJavaScript(script)
    except RuntimeError:
        pass


class LayoutFormulaBridge(QObject):
    """Deliver formula-menu actions without intercepting MathJax's own menu."""

    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self.callback = callback

    @Slot(str)
    def askFormula(self, payload_text: str):
        try:
            payload = json.loads(str(payload_text or "{}"))
        except (TypeError, ValueError):
            return
        if isinstance(payload, dict) and callable(self.callback):
            self.callback(payload)


def install_layout_formula_bridge(web_view, callback) -> None:
    if web_view is None or QWebChannel is None:
        return
    existing = getattr(web_view, "_layout_formula_bridge", None)
    if existing is not None:
        existing.callback = callback
        return
    page = web_view.page()
    bridge = LayoutFormulaBridge(callback, page)
    channel = QWebChannel(page)
    channel.registerObject("layoutFormulaBridge", bridge)
    page.setWebChannel(channel)
    # Both objects need Python owners; otherwise Qt may collect the channel
    # while a long paper is still open.
    web_view._layout_formula_bridge = bridge
    web_view._layout_formula_channel = channel


class UpdateAvailableDialog(QDialog):
    def __init__(self, release: ReleaseInfo, use_mirror: bool = True, parent=None):
        super().__init__(parent)
        self.release = release
        self.setWindowTitle(f"发现新版本 - {APP_NAME}")
        self.resize(540, 420)
        if parent is not None and hasattr(parent, "styleSheet"):
            self.setStyleSheet(parent.styleSheet())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        header = QLabel(f"<h3>🎉 发现 LitMTrans 新版本 v{release.version}</h3>")
        header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header)

        meta = QLabel(
            f"当前版本：v{APP_VERSION}  ➔  <b>最新版本：v{release.version}</b><br>"
            f"安装包大小：{release.formatted_size}"
        )
        meta.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(meta)

        notes_label = QLabel("<b>更新说明：</b>")
        notes_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(notes_label)

        notes_box = QTextEdit()
        notes_box.setReadOnly(True)
        notes_box.setPlainText(release.notes or "此版本暂无附加更新日志。详情请查看 GitHub Release 页面。")
        layout.addWidget(notes_box, 1)

        self.mirror_check = QCheckBox("使用国内镜像加速下载 (推荐)")
        self.mirror_check.setChecked(use_mirror)
        layout.addWidget(self.mirror_check)

        btn_layout = QHBoxLayout()
        open_web_btn = QPushButton("在网页中查看")
        open_web_btn.clicked.connect(self._open_web)
        btn_layout.addWidget(open_web_btn)
        btn_layout.addStretch(1)

        cancel_btn = QPushButton("稍后再说")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        update_btn = QPushButton("立即下载并安装")
        update_btn.setDefault(True)
        update_btn.clicked.connect(self.accept)
        btn_layout.addWidget(update_btn)

        layout.addLayout(btn_layout)

    def _open_web(self):
        url = self.release.release_page_url or GITHUB_RELEASES_URL
        QDesktopServices.openUrl(QUrl(url))

    @property
    def use_mirror_selected(self) -> bool:
        return self.mirror_check.isChecked()


class UpdateProgressDialog(QDialog):
    def __init__(self, release: ReleaseInfo, use_mirror: bool = True, parent=None):
        super().__init__(parent)
        self.release = release
        self.setWindowTitle(f"下载更新 - {APP_NAME} v{release.version}")
        self.resize(480, 170)
        self.setModal(True)
        if parent is not None and hasattr(parent, "styleSheet"):
            self.setStyleSheet(parent.styleSheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        self.title_label = QLabel(f"<b>正在下载 LitMTrans v{release.version} 更新安装包…</b>")
        self.title_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.title_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel(f"准备下载… (安装包大小: {release.formatted_size})")
        self.status_label.setObjectName("pathHint")
        layout.addWidget(self.status_label)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch(1)
        self.cancel_btn = QPushButton("取消下载")
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

        self.worker = UpdateDownloadWorker(release, use_mirror=use_mirror, parent=self)
        self.worker.progress.connect(self._on_progress)
        self.worker.succeeded.connect(self._on_succeeded)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(self._on_worker_cancelled)

    def start_download(self):
        self.worker.start()
        self.exec()

    def _on_progress(self, written: int, total: int):
        if total > 0:
            percent = min(100, max(0, int(written * 100 / total)))
            self.progress_bar.setValue(percent)
            self.status_label.setText(
                f"已下载 {format_size(written)} / {format_size(total)} ({percent}%)"
            )

    def _on_cancel_clicked(self):
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("正在取消…")
        self.worker.cancel()

    def _on_worker_cancelled(self):
        self.reject()

    def _on_succeeded(self, installer_path: str):
        self.accept()
        parent_widget = self.parentWidget() if isinstance(self.parentWidget(), QWidget) else None
        answer = QMessageBox.question(
            parent_widget,
            "安装更新",
            f"LitMTrans v{self.release.version} 安装包已下载并通过 SHA-256 安全校验。\n\n"
            "是否立即关闭 LitMTrans 并启动安装程序？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            try:
                launch_installer(installer_path)
            except Exception as exc:
                QMessageBox.critical(
                    parent_widget,
                    "无法启动安装程序",
                    str(exc),
                )
                return
            QApplication.quit()

    def _on_failed(self, message: str):
        self.reject()
        parent_widget = self.parentWidget() if isinstance(self.parentWidget(), QWidget) else None
        QMessageBox.warning(
            parent_widget,
            "更新下载失败",
            f"无法完整下载更新安装包：\n\n{message}\n\n建议您前往 GitHub Releases 页面手动下载最新安装包。",
        )

    def closeEvent(self, event):
        if self.worker.isRunning():
            self.worker.cancel()
        super().closeEvent(event)


class ModelSelectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择翻译模型")
        self.resize(560, 300)
        self.setMinimumHeight(300)
        self.selected_config: AITranslateConfig | None = None
        self.api_key = ""
        self.base_url = ""
        self.settings = app_config.load_settings()
        self.request_body_mode = "codex"
        self._request_body_mode_dirty = False

        layout = QVBoxLayout(self)
        self.status_label = QLabel("读取翻译服务配置...")
        layout.addWidget(self.status_label)

        row = QHBoxLayout()
        row.addWidget(QLabel("服务:"))
        self.provider_combo = QComboBox()
        populate_translation_provider_combo(self.provider_combo)
        saved_index = self.provider_combo.findData(translation_provider_choice_id(self.settings.ai_provider))
        if saved_index >= 0:
            self.provider_combo.setCurrentIndex(saved_index)
        self.provider_combo.currentIndexChanged.connect(self.load_initial_config)
        row.addWidget(self.provider_combo)
        layout.addLayout(row)

        self.request_body_button = QPushButton()
        self.request_body_button.setMinimumHeight(32)
        self.request_body_button.clicked.connect(self.edit_request_body_construction)
        layout.addWidget(self.request_body_button)

        api_row = QHBoxLayout()
        api_row.addWidget(QLabel("API 密钥："))
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        api_row.addWidget(self.key_input, 1)
        api_row.addWidget(QLabel("服务地址："))
        self.base_url_input = QLineEdit()
        api_row.addWidget(self.base_url_input, 1)
        layout.addLayout(api_row)

        row = QHBoxLayout()
        row.addWidget(QLabel("模型:"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(False)
        row.addWidget(self.model_combo, 1)
        self.refresh_models_button = QPushButton("刷新模型列表")
        self.refresh_models_button.clicked.connect(self.refresh_models)
        row.addWidget(self.refresh_models_button)
        layout.addLayout(row)

        self.deepseek_reasoning_row = QWidget()
        self.deepseek_reasoning_row.setFixedHeight(34)
        deepseek_reasoning_layout = QHBoxLayout(self.deepseek_reasoning_row)
        deepseek_reasoning_layout.setContentsMargins(0, 2, 0, 2)
        deepseek_reasoning_layout.setSpacing(8)
        deepseek_reasoning_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        deepseek_reasoning_layout.addWidget(QLabel("DeepSeek 思考:"))
        self.deepseek_thinking_check = QCheckBox("启用思考")
        self.deepseek_thinking_check.setToolTip("关闭高速排版后默认开启；使用服务商默认思考强度。")
        self.deepseek_effort_combo = QComboBox()
        self.deepseek_effort_combo.addItem("服务商默认", "default")
        self.deepseek_effort_combo.addItem("高", "high")
        self.deepseek_effort_combo.addItem("最高", "max")
        style_reasoning_effort_combo(self.deepseek_effort_combo)
        self.deepseek_thinking_check.toggled.connect(self.deepseek_effort_combo.setEnabled)
        deepseek_reasoning_layout.addWidget(self.deepseek_thinking_check)
        deepseek_reasoning_layout.addWidget(QLabel("等级:"))
        deepseek_reasoning_layout.addWidget(self.deepseek_effort_combo)
        deepseek_reasoning_layout.addStretch(1)
        layout.addWidget(self.deepseek_reasoning_row)

        self.gemini_reasoning_row = QWidget()
        self.gemini_reasoning_row.setFixedHeight(34)
        gemini_reasoning_layout = QHBoxLayout(self.gemini_reasoning_row)
        gemini_reasoning_layout.setContentsMargins(0, 2, 0, 2)
        gemini_reasoning_layout.setSpacing(8)
        gemini_reasoning_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        gemini_reasoning_layout.addWidget(QLabel("Google 思考:"))
        self.gemini_thinking_check = QCheckBox("启用思考")
        self.gemini_thinking_check.setToolTip("启用 Gemini 的思考，并在翻译记录中显示公开摘要。")
        self.gemini_effort_combo = QComboBox()
        self.gemini_effort_combo.addItem("低", "low")
        self.gemini_effort_combo.addItem("中", "medium")
        self.gemini_effort_combo.addItem("高", "high")
        style_reasoning_effort_combo(self.gemini_effort_combo)
        self.gemini_thinking_check.toggled.connect(self.gemini_effort_combo.setEnabled)
        gemini_reasoning_layout.addWidget(self.gemini_thinking_check)
        gemini_reasoning_layout.addWidget(QLabel("强度:"))
        gemini_reasoning_layout.addWidget(self.gemini_effort_combo)
        gemini_reasoning_layout.addStretch(1)
        layout.addWidget(self.gemini_reasoning_row)

        self.target_combo = QComboBox()
        # 允许用户自由输入目标语言，同时保留下拉建议项。
        self.target_combo.setEditable(True)
        self.target_combo.addItems(["简体中文", "繁体中文", "英文", "日文", "韩文"])
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("目标语言:"))
        target_row.addWidget(self.target_combo, 1)
        layout.addLayout(target_row)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("全文上下文连续翻译（推荐，适合百页以内的文档）", "full_context")
        self.mode_combo.addItem("结构分块断点翻译（超长文档备用）", "chunks")
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("翻译模式:"))
        mode_row.addWidget(self.mode_combo, 1)
        layout.addLayout(mode_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.load_initial_config()
        QTimer.singleShot(300, self.refresh_models)

    def load_initial_config(self):
        try:
            provider_id = self.provider_combo.currentData() or "zai"
            if provider_id == "oneapi" and not self._request_body_mode_dirty:
                stored_for_mode = self.settings.providers.get(provider_id)
                self.request_body_mode = normalize_oneapi_request_body_mode(
                    getattr(stored_for_mode, "request_body_mode", "codex")
                )
            self.update_request_body_button()
            self.update_deepseek_thinking_controls(provider_id)
            if machine_translate.is_machine_translation_provider(provider_id):
                stored_provider = self.settings.providers.get(provider_id)
                is_local = provider_id == machine_translate.MTRAN_SERVER_PROVIDER
                self.api_key = app_config.load_secret(provider_id, "api_key") if is_local else ""
                self.base_url = (
                    stored_provider.base_url
                    if stored_provider and stored_provider.base_url
                    else machine_translate.MTRAN_SERVER_DEFAULT_BASE_URL
                    if is_local
                    else ""
                )
                self.key_input.setText("")
                self.base_url_input.setText(self.base_url)
                self.key_input.setEnabled(is_local)
                self.base_url_input.setEnabled(is_local)
                self.model_combo.clear()
                self.model_combo.addItem(machine_translate.provider_label(provider_id), provider_id)
                self.status_label.setText(
                    "本地免费机翻会优先启动内置 MTranServer；未内置时连接服务地址。API 密钥可留空。"
                    if is_local
                    else "Edge 本地翻译不需要 API 密钥；首次使用会询问是否下载语言模型。"
                    if provider_id == machine_translate.EDGE_LOCAL_PROVIDER
                    else "联网免费机翻不需要 API 密钥；Google 不可达时自动切换到 Bing。"
                )
                return
            self.key_input.setEnabled(True)
            self.base_url_input.setEnabled(True)
            stored_provider = self.settings.providers.get(provider_id)
            stored_key = app_config.load_secret(provider_id, "api_key")
            try:
                config = build_ai_endpoint_config(provider_id)
            except Exception:
                config = AITranslateConfig(provider_id, "", provider_runtime_default_url(provider_id), "")
            self.api_key = stored_key or config.api_key
            self.base_url = (stored_provider.base_url if stored_provider and stored_provider.base_url else config.base_url)
            self.key_input.setText(self.api_key)
            self.base_url_input.setText(self.base_url)
            self.model_combo.clear()
            if stored_provider and stored_provider.model:
                self.model_combo.addItem(stored_provider.model, stored_provider.model)
                self.model_combo.setCurrentIndex(0)
            self.status_label.setText(f"接口: {self.base_url}；正在自动刷新模型列表...")
            QTimer.singleShot(50, self.refresh_models)
        except Exception as exc:
            self.status_label.setText(f"读取配置失败: {exc}")

    def update_request_body_button(self):
        self.request_body_button.setVisible(False)

    def update_deepseek_thinking_controls(self, provider_id: str | None = None):
        provider_id = provider_id or self.provider_combo.currentData() or ""
        is_deepseek = provider_id == "deepseek"
        is_gemini = provider_id == "gemini"
        self.deepseek_reasoning_row.setVisible(is_deepseek)
        self.gemini_reasoning_row.setVisible(is_gemini)
        if is_deepseek:
            self.deepseek_thinking_check.setChecked(
                bool(getattr(self.settings, "translation_deepseek_thinking_enabled", True))
            )
            effort = str(getattr(self.settings, "translation_deepseek_reasoning_effort", "default") or "default")
            index = self.deepseek_effort_combo.findData(effort)
            self.deepseek_effort_combo.setCurrentIndex(index if index >= 0 else 0)
            self.deepseek_effort_combo.setEnabled(self.deepseek_thinking_check.isChecked())
        if is_gemini:
            self.gemini_thinking_check.setChecked(
                bool(getattr(self.settings, "translation_gemini_thinking_enabled", False))
            )
            effort = str(getattr(self.settings, "translation_gemini_reasoning_effort", "medium") or "medium")
            index = self.gemini_effort_combo.findData(effort)
            self.gemini_effort_combo.setCurrentIndex(index if index >= 0 else 1)
            self.gemini_effort_combo.setEnabled(self.gemini_thinking_check.isChecked())

    def deepseek_translation_thinking_values(self) -> tuple[str, str]:
        enabled = self.provider_combo.currentData() == "deepseek" and self.deepseek_thinking_check.isChecked()
        effort = str(self.deepseek_effort_combo.currentData() or "high")
        return ("enabled" if enabled else "disabled", effort)

    def translation_thinking_values(self) -> tuple[str, str]:
        if self.provider_combo.currentData() == "gemini":
            return (
                "enabled" if self.gemini_thinking_check.isChecked() else "disabled",
                str(self.gemini_effort_combo.currentData() or "medium"),
            )
        return self.deepseek_translation_thinking_values()

    def edit_request_body_construction(self):
        return

    def set_status(self, message: str):
        self.status_label.setText(message)

    def refresh_models(self):
        if machine_translate.is_machine_translation_provider(self.provider_combo.currentData() or ""):
            provider_id = self.provider_combo.currentData() or ""
            self.status_label.setText("本地免费机翻使用内置语言包，不需要刷新模型。" if provider_id == machine_translate.MTRAN_SERVER_PROVIDER else "免费机翻不需要刷新模型。")
            self.save_current_api_settings()
            return
        self.api_key = self.key_input.text().strip()
        provider_id = self.provider_combo.currentData() or "zai"
        self.base_url = normalize_ai_base_url(self.base_url_input.text().strip(), provider_id)
        if not self.api_key or not self.base_url:
            try:
                config = build_ai_endpoint_config(provider_id)
                self.api_key = config.api_key
                self.base_url = config.base_url
                self.key_input.setText(self.api_key)
                self.base_url_input.setText(self.base_url)
            except Exception as exc:
                QMessageBox.critical(self, "刷新模型失败", str(exc))
                return
        try:
            self.refresh_models_button.setEnabled(False)
            self.status_label.setText("正在自动刷新模型列表...")
            current_model = self.model_combo.currentData() or self.model_combo.currentText().strip()
            model_options = fetch_translation_model_options(provider_id, self.api_key, self.base_url)
            self.model_combo.setProperty("provider_id", provider_id)
            preferred = apply_model_options_to_combo(self.model_combo, model_options, str(current_model))
            self.status_label.setText(f"已加载 {len(model_options)} 个模型")
            if current_model and current_model not in [option.model_id for option in model_options] and preferred:
                self.status_label.setText(
                    f"上次默认模型“{current_model}”已无法访问，已按优先原则改用“{preferred}”。"
                )
                QMessageBox.information(
                    self,
                    "默认模型已切换",
                    f"上次默认模型“{current_model}”已不在当前模型列表中。\n已自动改用“{preferred}”。",
                )
            self.save_current_api_settings()
        except Exception as exc:
            QMessageBox.critical(self, "刷新模型失败", str(exc))
        finally:
            self.refresh_models_button.setEnabled(True)

    def accept(self):
        model = self.model_combo.currentText().strip()
        provider_id = self.provider_combo.currentData() or "zai"
        if machine_translate.is_machine_translation_provider(provider_id):
            is_local = provider_id == machine_translate.MTRAN_SERVER_PROVIDER
            base_url = self.base_url_input.text().strip() or (machine_translate.MTRAN_SERVER_DEFAULT_BASE_URL if is_local else "")
            api_key = self.key_input.text().strip().removeprefix("Bearer ").strip() if is_local else ""
            self.selected_config = AITranslateConfig(provider_id, api_key, base_url, machine_translate.provider_label(provider_id))
            self.save_current_api_settings()
            super().accept()
            return
        if not model:
            QMessageBox.critical(self, "错误", "请等待模型列表刷新完成后选择模型。")
            return
        self.api_key = self.key_input.text().strip()
        self.base_url = normalize_ai_base_url(self.base_url_input.text().strip(), provider_id)
        if not self.api_key or not self.base_url:
            QMessageBox.critical(self, "错误", f"没有可用的 {translation_provider_name(provider_id)} 配置。")
            return
        selected_model = self.model_combo.currentData() or model
        stored_provider = self.settings.providers.get(provider_id)
        self.selected_config = AITranslateConfig(
            provider_id,
            self.api_key,
            self.base_url,
            str(selected_model),
            request_body_mode=self.request_body_mode if provider_id == "oneapi" else "codex",
            thinking_mode=self.translation_thinking_values()[0],
            reasoning_effort=self.translation_thinking_values()[1],
        )
        self.save_current_api_settings()
        super().accept()

    def save_current_api_settings(self):
        provider_id = self.provider_combo.currentData() or "zai"
        self.settings.ai_provider = provider_id
        if provider_id == "deepseek":
            thinking_mode, reasoning_effort = self.deepseek_translation_thinking_values()
            self.settings.translation_deepseek_thinking_enabled = thinking_mode == "enabled"
            self.settings.translation_deepseek_reasoning_effort = reasoning_effort
        elif provider_id == "gemini":
            thinking_mode, reasoning_effort = self.translation_thinking_values()
            self.settings.translation_gemini_thinking_enabled = thinking_mode == "enabled"
            self.settings.translation_gemini_reasoning_effort = reasoning_effort
        if machine_translate.is_machine_translation_provider(provider_id):
            is_local = provider_id == machine_translate.MTRAN_SERVER_PROVIDER
            base_url = self.base_url_input.text().strip() or (machine_translate.MTRAN_SERVER_DEFAULT_BASE_URL if is_local else "")
            self.settings.providers[provider_id] = app_config.ProviderSettings(
                provider_id=provider_id,
                base_url=base_url,
                model=machine_translate.provider_label(provider_id),
            )
            if is_local:
                save_secret_with_session_fallback(self, provider_id, "api_key", self.key_input.text().strip().removeprefix("Bearer ").strip())
            app_config.save_settings(self.settings)
            return
        self.settings.providers[provider_id] = app_config.ProviderSettings(
            provider_id=provider_id,
            base_url=self.base_url,
            model=str(self.model_combo.currentData() or self.model_combo.currentText().strip()),
            request_body_mode=self.request_body_mode if provider_id == "oneapi" else "codex",
        )
        save_secret_with_session_fallback(self, provider_id, "api_key", self.api_key)
        app_config.save_settings(self.settings)


class MultilinePlaceholderTextEdit(QTextEdit):
    """支持显式换行和中文自动折行的多行占位提示文本框。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._multiline_placeholder_text = ""
        self.textChanged.connect(self.viewport().update)

    def set_multiline_placeholder_text(self, text: str) -> None:
        """设置由本控件自行绘制的多行占位提示。"""
        self._multiline_placeholder_text = str(text or "")
        # 禁用 QTextEdit 原生占位提示；部分 Qt 版本只会绘制第一段内容。
        super().setPlaceholderText("")
        self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)

        if not self.document().isEmpty() or not self._multiline_placeholder_text:
            return

        painter = QPainter(self.viewport())
        try:
            painter.setFont(self.font())
            painter.setPen(
                self.palette().color(QPalette.ColorRole.PlaceholderText)
            )

            text_option = QTextOption()
            text_option.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )
            # WrapAnywhere 可确保连续中文也能按照文本框实际宽度自动折行。
            text_option.setWrapMode(QTextOption.WrapMode.WrapAnywhere)

            text_rect = QRectF(
                self.viewport().rect().adjusted(10, 9, -10, -9)
            )
            painter.drawText(
                text_rect,
                self._multiline_placeholder_text,
                text_option,
            )
        finally:
            painter.end()


class TranslationOptionsDialog(QDialog):
    def __init__(self, parent=None, provider_id: str = "", allow_parse_only: bool = False, retranslate: bool = False):
        super().__init__(parent)
        self.setWindowTitle("确认重新翻译" if retranslate else "翻译选项")
        self.resize(720, 680)
        self.setMinimumHeight(680)
        self.reference_paths: list[str] = []
        self.parse_only_check = None
        self.selected_ai_config: AITranslateConfig | None = None
        settings = getattr(parent, "settings", None) if parent else None
        self.settings = settings or app_config.load_settings()
        current_provider_id = provider_id or str(getattr(self.settings, "ai_provider", "") or "")
        self.free_machine_mode = machine_translate.is_machine_translation_provider(current_provider_id)
        self.local_machine_mode = (current_provider_id or "").strip().lower() == machine_translate.MTRAN_SERVER_PROVIDER
        self.edge_local_mode = (current_provider_id or "").strip().lower() == machine_translate.EDGE_LOCAL_PROVIDER
        self.explicit_source_mode = self.local_machine_mode or self.edge_local_mode
        self.request_body_mode = normalize_oneapi_request_body_mode(
            getattr(self.settings.providers.get("oneapi"), "request_body_mode", "codex")
        )
        layout = QVBoxLayout(self)

        if retranslate:
            retranslate_hint = QLabel("确认后会清除当前目标语言的旧译文和续写缓存，并按以下设置重新翻译。")
            retranslate_hint.setWordWrap(True)
            layout.addWidget(retranslate_hint)

        model_group = QGroupBox("翻译模型")
        model_layout = QVBoxLayout(model_group)
        self.model_status = QLabel("")
        model_layout.addWidget(self.model_status)
        provider_row = QHBoxLayout()
        self.provider_label = QLabel("服务:")
        provider_row.addWidget(self.provider_label)
        self.provider_combo = QComboBox()
        populate_translation_provider_combo(self.provider_combo)
        saved_provider_index = self.provider_combo.findData(translation_provider_choice_id(current_provider_id or "zai"))
        if saved_provider_index >= 0:
            self.provider_combo.setCurrentIndex(saved_provider_index)
        provider_row.addWidget(self.provider_combo)
        self.api_key_label = QLabel("API 密钥：")
        provider_row.addWidget(self.api_key_label)
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        provider_row.addWidget(self.api_key_input, 1)
        self.provider_row = provider_row
        model_layout.addLayout(provider_row)
        endpoint_row = QHBoxLayout()
        self.base_url_label = QLabel("服务地址：")
        endpoint_row.addWidget(self.base_url_label)
        self.base_url_input = QLineEdit()
        endpoint_row.addWidget(self.base_url_input, 1)
        self.translation_model_label = QLabel("模型:")
        endpoint_row.addWidget(self.translation_model_label)
        self.translation_model_combo = QComboBox()
        endpoint_row.addWidget(self.translation_model_combo, 1)
        self.refresh_translation_models_button = QPushButton("刷新模型列表")
        endpoint_row.addWidget(self.refresh_translation_models_button)
        self.endpoint_row = endpoint_row
        model_layout.addLayout(endpoint_row)

        self.deepseek_reasoning_row = QWidget()
        self.deepseek_reasoning_row.setFixedHeight(34)
        deepseek_reasoning_layout = QHBoxLayout(self.deepseek_reasoning_row)
        deepseek_reasoning_layout.setContentsMargins(0, 2, 0, 2)
        deepseek_reasoning_layout.setSpacing(8)
        deepseek_reasoning_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        deepseek_reasoning_layout.addWidget(QLabel("DeepSeek 思考:"))
        self.deepseek_thinking_check = QCheckBox("启用思考")
        self.deepseek_thinking_check.setToolTip("关闭高速排版后默认开启；使用服务商默认思考强度。")
        self.deepseek_effort_combo = QComboBox()
        self.deepseek_effort_combo.addItem("服务商默认", "default")
        self.deepseek_effort_combo.addItem("高", "high")
        self.deepseek_effort_combo.addItem("最高", "max")
        style_reasoning_effort_combo(self.deepseek_effort_combo)
        self.deepseek_thinking_check.toggled.connect(self.deepseek_effort_combo.setEnabled)
        deepseek_reasoning_layout.addWidget(self.deepseek_thinking_check)
        deepseek_reasoning_layout.addWidget(QLabel("等级:"))
        deepseek_reasoning_layout.addWidget(self.deepseek_effort_combo)
        deepseek_reasoning_layout.addStretch(1)
        model_layout.addWidget(self.deepseek_reasoning_row)

        self.deepseek_fast_layout_check = QCheckBox("DeepSeek 快速排版翻译（仅支持排版模式翻译）")
        self.deepseek_fast_layout_check.setToolTip(
            "利用官方服务的高缓存命中进行并发请求，费用消耗会加剧。"
            "启用后会关闭 DeepSeek 思考，仅在排版阅读模式可用。"
        )
        self.deepseek_fast_layout_check.toggled.connect(self.apply_deepseek_fast_layout_state)
        model_layout.addWidget(self.deepseek_fast_layout_check)

        self.gemini_reasoning_row = QWidget()
        self.gemini_reasoning_row.setFixedHeight(34)
        gemini_reasoning_layout = QHBoxLayout(self.gemini_reasoning_row)
        gemini_reasoning_layout.setContentsMargins(0, 2, 0, 2)
        gemini_reasoning_layout.setSpacing(8)
        gemini_reasoning_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        gemini_reasoning_layout.addWidget(QLabel("Google 思考:"))
        self.gemini_thinking_check = QCheckBox("启用思考")
        self.gemini_thinking_check.setToolTip("启用 Gemini 的思考，并在翻译记录中显示公开摘要。")
        self.gemini_effort_combo = QComboBox()
        self.gemini_effort_combo.addItem("低", "low")
        self.gemini_effort_combo.addItem("中", "medium")
        self.gemini_effort_combo.addItem("高", "high")
        style_reasoning_effort_combo(self.gemini_effort_combo)
        self.gemini_thinking_check.toggled.connect(self.gemini_effort_combo.setEnabled)
        gemini_reasoning_layout.addWidget(self.gemini_thinking_check)
        gemini_reasoning_layout.addWidget(QLabel("强度:"))
        gemini_reasoning_layout.addWidget(self.gemini_effort_combo)
        gemini_reasoning_layout.addStretch(1)
        model_layout.addWidget(self.gemini_reasoning_row)

        self.request_body_button = QPushButton()
        self.request_body_button.setMinimumHeight(32)
        self.request_body_button.clicked.connect(self.edit_request_body_construction)
        model_layout.addWidget(self.request_body_button)
        layout.addWidget(model_group)

        self.source_combo = QComboBox()
        self.source_combo.setEditable(self.edge_local_mode)
        if self.edge_local_mode:
            populate_edge_language_combo(self.source_combo)
        else:
            populate_local_language_combo(self.source_combo, include_auto=False)
        if self.settings and getattr(self.settings, "translation_source_language", "").strip():
            saved_source = machine_translate.normalize_language_name(self.settings.translation_source_language, "英文")
            source_index = self.source_combo.findData(saved_source)
            if source_index >= 0:
                self.source_combo.setCurrentIndex(source_index)
            else:
                self.source_combo.setCurrentText(saved_source)
        source_row = QHBoxLayout()
        self.source_label = QLabel("源语言:")
        source_row.addWidget(self.source_label)
        source_row.addWidget(self.source_combo, 1)
        layout.addLayout(source_row)
        self.source_label.setVisible(self.explicit_source_mode)
        self.source_combo.setVisible(self.explicit_source_mode)

        self.target_combo = QComboBox()
        # 允许用户自由输入目标语言，同时保留下拉建议项。
        self.target_combo.setEditable(not self.local_machine_mode)
        if self.local_machine_mode:
            populate_local_target_language_combo(self.target_combo, self.source_combo.currentText())
        elif self.edge_local_mode:
            populate_edge_language_combo(self.target_combo)
        else:
            populate_edge_language_combo(self.target_combo)
        if self.settings and getattr(self.settings, "translation_target_language", "").strip():
            saved_target = machine_translate.normalize_language_name(self.settings.translation_target_language, "简体中文")
            if self.local_machine_mode and self.target_combo.findData(saved_target) < 0:
                saved_target = "简体中文"
            self.target_combo.setCurrentText(saved_target)
        self.source_combo.currentTextChanged.connect(self.update_local_target_language_options)
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("目标语言:"))
        target_row.addWidget(self.target_combo, 1)
        layout.addLayout(target_row)

        self.local_parallel_spin = QSpinBox()
        self.local_parallel_spin.setRange(0, machine_translate.MTRAN_SERVER_MAX_PARALLELISM)
        self.local_parallel_spin.setValue(machine_translate.normalize_parallelism(getattr(self.settings, "local_machine_parallelism", machine_translate.MTRAN_SERVER_DEFAULT_PARALLELISM)))
        parallel_row = QHBoxLayout()
        self.parallel_label = QLabel("并行数:")
        parallel_row.addWidget(self.parallel_label)
        parallel_row.addWidget(self.local_parallel_spin)
        parallel_row.addStretch(1)
        layout.addLayout(parallel_row)
        self.parallel_label.setVisible(self.local_machine_mode)
        self.local_parallel_spin.setVisible(self.local_machine_mode)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("全文上下文连续翻译（推荐，适合百页以内的文档）", "full_context")
        self.mode_combo.addItem("结构分块断点翻译（超长文档备用）", "chunks")
        if self.settings:
            saved_mode = str(getattr(self.settings, "translation_mode", "full_context") or "full_context")
            index = self.mode_combo.findData(saved_mode)
            if index >= 0:
                self.mode_combo.setCurrentIndex(index)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("翻译模式:"))
        mode_row.addWidget(self.mode_combo, 1)
        layout.addLayout(mode_row)

        reference_group = QGroupBox("参考文件与自定义翻译指令（可选）")
        reference_layout = QVBoxLayout(reference_group)
        if self.free_machine_mode:
            reference_hint_text = (
                "当前使用本地免费机翻。该模式没有上下文记忆，参考文件和自定义翻译指令功能不可用。"
                if self.local_machine_mode
                else "当前使用免费机翻。免费机翻没有上下文理解能力，参考文件和自定义翻译指令功能不可用。"
            )
        else:
            reference_hint_text = (
                "可选：选择同领域“目标语言”的期刊论文作为参考语料，建议6篇以内，但不强制限制数量。"
                "程序会把完整参考语料加入翻译上下文，"
                "让模型同时阅读完整参考语料与待翻译正文，以保留术语、语体、句式节奏、篇章推进和表达习惯。"
                "参考风格只作为软约束，"
                "翻译仍以准确还原原文科研含义为优先。"
                "使用该功能时建议选择支持长上下文的模型；mini、flash、lite 等轻量模型可能不适合此任务。"
            )
        self.reference_hint = QLabel(reference_hint_text)
        self.reference_hint.setWordWrap(True)
        reference_layout.addWidget(self.reference_hint)

        # 参考文件列表缩为左半区，右半区用于输入会随请求发送给翻译模型的附加指令。
        reference_inputs = QHBoxLayout()
        reference_inputs.setSpacing(12)

        self.reference_list = QListWidget()
        self.reference_list.setMinimumHeight(105)
        self.reference_list.setMaximumHeight(105)
        reference_inputs.addWidget(self.reference_list, 1)

        self.custom_instruction_edit = MultilinePlaceholderTextEdit()
        self.custom_instruction_edit.setAcceptRichText(False)
        self.custom_instruction_edit.setMinimumHeight(105)
        self.custom_instruction_edit.setMaximumHeight(105)
        # 不使用 QTextEdit 原生占位文本，确保换行和连续中文折行可靠生效。
        self.custom_instruction_edit.set_multiline_placeholder_text(
            "可以在此输入发送给翻译模型的自定义提示，\n"
            "比如翻译规则、特殊语法要求、术语对应关系等。"
        )
        self.custom_instruction_edit.setPlainText(
            str(getattr(self.settings, "translation_custom_instruction", "") or "")
        )
        reference_inputs.addWidget(self.custom_instruction_edit, 1)
        reference_layout.addLayout(reference_inputs)

        reference_buttons = QHBoxLayout()
        self.choose_reference_button = QPushButton("选择参考文件")
        self.remove_reference_button = QPushButton("移除选中")
        self.clear_reference_button = QPushButton("清空")
        self.choose_reference_button.clicked.connect(self.choose_reference_files)
        self.remove_reference_button.clicked.connect(self.remove_selected_reference_files)
        self.clear_reference_button.clicked.connect(self.clear_reference_files)
        reference_buttons.addWidget(self.choose_reference_button)
        reference_buttons.addWidget(self.remove_reference_button)
        reference_buttons.addWidget(self.clear_reference_button)
        reference_buttons.addStretch(1)
        reference_layout.addLayout(reference_buttons)
        if self.free_machine_mode:
            self.reference_list.setEnabled(False)
            self.custom_instruction_edit.setEnabled(False)
            self.choose_reference_button.setEnabled(False)
            self.remove_reference_button.setEnabled(False)
            self.clear_reference_button.setEnabled(False)
        layout.addWidget(reference_group)

        if allow_parse_only:
            self.parse_only_check = QCheckBox("只解析不翻译")
            self.parse_only_check.setChecked(False)
            layout.addWidget(self.parse_only_check)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if self.settings and not self.free_machine_mode:
            normalized_paths: list[str] = []
            seen = set()
            for raw_path in getattr(self.settings, "translation_reference_paths", []) or []:
                path = Path(str(raw_path)).expanduser().resolve()
                if not path.exists() or not is_supported_reference_file(path):
                    continue
                normalized = str(path)
                if normalized in seen:
                    continue
                seen.add(normalized)
                normalized_paths.append(normalized)
            self.reference_paths = normalized_paths
            self.refresh_reference_list()

        self.provider_combo.currentIndexChanged.connect(self.load_translation_provider)
        self.refresh_translation_models_button.clicked.connect(self.refresh_translation_models)
        self.load_translation_provider()

    def load_translation_provider(self):
        provider_id = self.provider_combo.currentData() or "zai"
        self.update_request_body_button()
        self.update_deepseek_thinking_controls(provider_id)
        self.free_machine_mode = machine_translate.is_machine_translation_provider(provider_id)
        self.local_machine_mode = provider_id == machine_translate.MTRAN_SERVER_PROVIDER
        self.edge_local_mode = provider_id == machine_translate.EDGE_LOCAL_PROVIDER
        self.explicit_source_mode = self.local_machine_mode or self.edge_local_mode
        current_source = machine_translate.normalize_language_name(self.source_combo.currentText(), "英文")
        self.source_combo.setEditable(self.edge_local_mode)
        if self.edge_local_mode:
            populate_edge_language_combo(self.source_combo)
        else:
            populate_local_language_combo(self.source_combo, include_auto=False)
        self.source_combo.setCurrentText(current_source)
        self.source_combo.setVisible(self.explicit_source_mode)
        self.source_label.setVisible(self.explicit_source_mode)
        self.local_parallel_spin.setVisible(self.local_machine_mode)
        self.parallel_label.setVisible(self.local_machine_mode)
        current_target = self.target_combo.currentText().strip()
        self.target_combo.setEditable(not self.local_machine_mode)
        self.target_combo.clear()
        if self.local_machine_mode:
            populate_local_target_language_combo(self.target_combo, self.source_combo.currentText())
            self.target_combo.setCurrentText(current_target if self.target_combo.findData(current_target) >= 0 else "简体中文")
        else:
            populate_edge_language_combo(self.target_combo)
            self.target_combo.setCurrentText(current_target or "简体中文")
        if self.free_machine_mode:
            is_local = self.local_machine_mode
            stored_provider = self.settings.providers.get(provider_id)
            self.api_key_input.setText(app_config.load_secret(provider_id, "api_key") if is_local else "")
            self.base_url_input.setText(stored_provider.base_url if stored_provider and stored_provider.base_url else (machine_translate.MTRAN_SERVER_DEFAULT_BASE_URL if is_local else ""))
            # 与密钥设置界面保持一致：免费机翻无需 LLM 配置，隐藏而非仅禁用。
            # 本地机翻的服务地址和 API 密钥仍从已保存的配置中读取。
            for widget in (
                self.api_key_label,
                self.api_key_input,
                self.base_url_label,
                self.base_url_input,
                self.translation_model_label,
                self.translation_model_combo,
                self.refresh_translation_models_button,
            ):
                widget.setVisible(False)
            self.provider_row.setStretch(1, 1)
            self.provider_row.setStretch(3, 0)
            self.endpoint_row.setStretch(1, 0)
            self.endpoint_row.setStretch(3, 0)
            self.translation_model_combo.clear()
            self.translation_model_combo.addItem(machine_translate.provider_label(provider_id))
            self.model_status.setText("本地免费机翻不需要选择模型。" if is_local else "免费机翻不需要 API 密钥或模型。")
        else:
            stored_provider = self.settings.providers.get(provider_id)
            for widget in (
                self.api_key_label,
                self.api_key_input,
                self.base_url_label,
                self.base_url_input,
                self.translation_model_label,
                self.translation_model_combo,
                self.refresh_translation_models_button,
            ):
                widget.setVisible(True)
            self.provider_row.setStretch(1, 0)
            self.provider_row.setStretch(3, 1)
            self.endpoint_row.setStretch(1, 1)
            self.endpoint_row.setStretch(3, 1)
            self.api_key_input.setText(app_config.load_secret(provider_id, "api_key"))
            self.base_url_input.setText(stored_provider.base_url if stored_provider and stored_provider.base_url else provider_runtime_default_url(provider_id))
            self.translation_model_combo.clear()
            if stored_provider and stored_provider.model:
                self.translation_model_combo.addItem(stored_provider.model, stored_provider.model)
            self.model_status.setText("正在自动刷新模型列表...")
            QTimer.singleShot(50, self.refresh_translation_models)
        self.update_reference_availability()

    def update_request_body_button(self):
        self.request_body_button.setVisible(False)

    def update_deepseek_thinking_controls(self, provider_id: str | None = None):
        provider_id = provider_id or self.provider_combo.currentData() or ""
        is_deepseek = provider_id == "deepseek"
        is_gemini = provider_id == "gemini"
        fast_available = is_deepseek and bool(getattr(self.settings, "layout_reading_mode", False))
        self.deepseek_reasoning_row.setVisible(is_deepseek)
        self.deepseek_fast_layout_check.setVisible(fast_available)
        self.gemini_reasoning_row.setVisible(is_gemini)
        if is_deepseek:
            previous = self.deepseek_fast_layout_check.blockSignals(True)
            self.deepseek_fast_layout_check.setChecked(
                fast_available and bool(getattr(self.settings, "translation_deepseek_fast_layout_enabled", True))
            )
            self.deepseek_fast_layout_check.blockSignals(previous)
            fast_enabled = fast_available and self.deepseek_fast_layout_check.isChecked()
            previous = self.deepseek_thinking_check.blockSignals(True)
            self.deepseek_thinking_check.setChecked(
                False if fast_enabled else bool(getattr(self.settings, "translation_deepseek_thinking_enabled", True))
            )
            self.deepseek_thinking_check.blockSignals(previous)
            effort = str(getattr(self.settings, "translation_deepseek_reasoning_effort", "default") or "default")
            index = self.deepseek_effort_combo.findData(effort)
            self.deepseek_effort_combo.setCurrentIndex(index if index >= 0 else 0)
            self.apply_deepseek_fast_layout_state()
        if is_gemini:
            self.gemini_thinking_check.setChecked(
                bool(getattr(self.settings, "translation_gemini_thinking_enabled", False))
            )
            effort = str(getattr(self.settings, "translation_gemini_reasoning_effort", "medium") or "medium")
            index = self.gemini_effort_combo.findData(effort)
            self.gemini_effort_combo.setCurrentIndex(index if index >= 0 else 1)
            self.gemini_effort_combo.setEnabled(self.gemini_thinking_check.isChecked())

    def apply_deepseek_fast_layout_state(self) -> None:
        if self.provider_combo.currentData() != "deepseek":
            return
        fast_enabled = self.deepseek_fast_layout_enabled()
        if fast_enabled:
            previous = self.deepseek_thinking_check.blockSignals(True)
            self.deepseek_thinking_check.setChecked(False)
            self.deepseek_thinking_check.blockSignals(previous)
        else:
            previous = self.deepseek_thinking_check.blockSignals(True)
            self.deepseek_thinking_check.setChecked(True)
            self.deepseek_thinking_check.blockSignals(previous)
            index = self.deepseek_effort_combo.findData("default")
            self.deepseek_effort_combo.setCurrentIndex(index if index >= 0 else 0)
        self.deepseek_thinking_check.setEnabled(not fast_enabled)
        self.deepseek_effort_combo.setEnabled(not fast_enabled and self.deepseek_thinking_check.isChecked())
        self.deepseek_reasoning_row.setToolTip(
            "高速并发翻译使用无思考请求，已自动关闭思考设置。"
            if fast_enabled else ""
        )

    def deepseek_translation_thinking_values(self) -> tuple[str, str]:
        enabled = (
            self.provider_combo.currentData() == "deepseek"
            and not self.deepseek_fast_layout_enabled()
            and self.deepseek_thinking_check.isChecked()
        )
        effort = str(self.deepseek_effort_combo.currentData() or "default")
        return ("enabled" if enabled else "disabled", effort)

    def translation_thinking_values(self) -> tuple[str, str]:
        if self.provider_combo.currentData() == "gemini":
            return (
                "enabled" if self.gemini_thinking_check.isChecked() else "disabled",
                str(self.gemini_effort_combo.currentData() or "medium"),
            )
        return self.deepseek_translation_thinking_values()

    def deepseek_fast_layout_enabled(self) -> bool:
        return bool(
            self.provider_combo.currentData() == "deepseek"
            and bool(getattr(self.settings, "layout_reading_mode", False))
            and self.deepseek_fast_layout_check.isChecked()
        )

    def edit_request_body_construction(self):
        return

    def update_reference_availability(self):
        if self.free_machine_mode:
            self.reference_hint.setText(
                "当前使用本地免费机翻。该模式没有上下文记忆，参考文件和自定义翻译指令功能不可用。"
                if self.local_machine_mode
                else "当前使用免费机翻。免费机翻没有上下文理解能力，参考文件和自定义翻译指令功能不可用。"
            )
        else:
            self.reference_hint.setText("可选：选择同领域“目标语言”的期刊论文作为参考语料，建议6篇以内，但不强制限制数量。程序会把完整参考语料加入翻译上下文，以保留术语、语体和表达习惯。（该功能可用于将自身中文原稿翻译为目标期刊的语言和风格）")
        for widget in (
            self.reference_list,
            self.custom_instruction_edit,
            self.choose_reference_button,
            self.remove_reference_button,
            self.clear_reference_button,
        ):
            widget.setEnabled(not self.free_machine_mode)

    def update_local_target_language_options(self, _source_text: str = ""):
        if not self.local_machine_mode or not hasattr(self, "target_combo"):
            return
        current_target = machine_translate.normalize_language_name(self.target_combo.currentText(), "简体中文")
        populate_local_target_language_combo(self.target_combo, self.source_combo.currentText())
        index = self.target_combo.findData(current_target)
        if index >= 0:
            self.target_combo.setCurrentIndex(index)
        elif self.target_combo.count():
            self.target_combo.setCurrentIndex(0)

    def refresh_translation_models(self):
        provider_id = self.provider_combo.currentData() or "zai"
        if machine_translate.is_machine_translation_provider(provider_id):
            return
        api_key = self.api_key_input.text().strip()
        base_url = normalize_ai_base_url(self.base_url_input.text().strip(), provider_id)
        if not api_key or not base_url:
            self.model_status.setText("请填写 API 密钥和服务地址后刷新模型。")
            return
        try:
            self.refresh_translation_models_button.setEnabled(False)
            current_model = str(self.translation_model_combo.currentData() or self.translation_model_combo.currentText().strip())
            options = fetch_translation_model_options(provider_id, api_key, base_url)
            self.translation_model_combo.setProperty("provider_id", provider_id)
            apply_model_options_to_combo(self.translation_model_combo, options, current_model)
            self.model_status.setText(f"已加载 {len(options)} 个模型")
        except Exception as exc:
            self.model_status.setText(f"刷新模型失败: {exc}")
        finally:
            self.refresh_translation_models_button.setEnabled(True)

    def choose_reference_files(self):
        if self.free_machine_mode:
            QMessageBox.information(self, "参考文件不可用", "当前使用免费机翻。免费机翻没有上下文理解能力，不能使用参考文件。")
            return
        suffixes = sorted(SUPPORTED_INPUT_EXTENSIONS | {".md", ".markdown", ".txt"})
        pattern = " ".join(f"*{suffix}" for suffix in suffixes)
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择参考文件",
            "",
            f"参考文件 ({pattern});;All Files (*)",
        )
        if not files:
            return

        merged: list[str] = []
        seen = set()
        for raw_path in self.reference_paths + files:
            path = Path(raw_path)
            if not path.exists() or not is_supported_reference_file(path):
                continue
            normalized = str(path.expanduser().resolve())
            if normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)

        if len(merged) > REFERENCE_CONTEXT_RECOMMENDED_FILES:
            QMessageBox.information(
                self,
                "参考文件数量较多",
                f"通常建议参考文件控制在 {REFERENCE_CONTEXT_RECOMMENDED_FILES} 个以内，"
                "以减少上下文压力并保持翻译稳定性。程序不会限制数量，会继续使用你选择的全部参考文件。",
            )

        self.reference_paths = merged
        self.refresh_reference_list()

    def refresh_reference_list(self):
        self.reference_list.clear()
        for path in self.reference_paths:
            item = QListWidgetItem(Path(path).name)
            item.setToolTip(path)
            item.setData(256, path)
            self.reference_list.addItem(item)

    def remove_selected_reference_files(self):
        selected_paths = {str(item.data(256)) for item in self.reference_list.selectedItems()}
        if not selected_paths:
            return
        self.reference_paths = [path for path in self.reference_paths if path not in selected_paths]
        self.refresh_reference_list()

    def clear_reference_files(self):
        self.reference_paths = []
        self.refresh_reference_list()

    def accept(self):
        provider_id = self.provider_combo.currentData() or "zai"
        needs_translation_model = not (self.parse_only_check and self.parse_only_check.isChecked())
        if machine_translate.is_machine_translation_provider(provider_id):
            is_local = provider_id == machine_translate.MTRAN_SERVER_PROVIDER
            api_key = self.api_key_input.text().strip().removeprefix("Bearer ").strip() if is_local else ""
            base_url = self.base_url_input.text().strip() or (machine_translate.MTRAN_SERVER_DEFAULT_BASE_URL if is_local else "")
            model = machine_translate.provider_label(provider_id)
        else:
            api_key = self.api_key_input.text().strip().removeprefix("Bearer ").strip()
            base_url = normalize_ai_base_url(self.base_url_input.text().strip(), provider_id)
            model = str(self.translation_model_combo.currentData() or self.translation_model_combo.currentText().strip())
            if needs_translation_model and (not api_key or not base_url or not model):
                QMessageBox.warning(self, "缺少翻译模型配置", "请填写 API 密钥、服务地址，并选择一个翻译模型。")
                return
        if needs_translation_model:
            self.settings.ai_provider = provider_id
            self.settings.providers[provider_id] = app_config.ProviderSettings(
                provider_id=provider_id,
                base_url=base_url,
                model=model,
                request_body_mode=self.request_body_mode if provider_id == "oneapi" else "codex",
            )
            if provider_id == machine_translate.MTRAN_SERVER_PROVIDER or not machine_translate.is_machine_translation_provider(provider_id):
                save_secret_with_session_fallback(self, provider_id, "api_key", api_key)
            self.selected_ai_config = AITranslateConfig(
                provider_id,
                api_key,
                base_url,
                model,
                request_body_mode=self.request_body_mode if provider_id == "oneapi" else "codex",
                thinking_mode=self.translation_thinking_values()[0],
                reasoning_effort=self.translation_thinking_values()[1],
                deepseek_fast_layout_translation=self.deepseek_fast_layout_enabled(),
                custom_translation_instruction=self.custom_instruction_edit.toPlainText().strip(),
            )
            if provider_id == "deepseek":
                thinking_mode, reasoning_effort = self.deepseek_translation_thinking_values()
                self.settings.translation_deepseek_thinking_enabled = thinking_mode == "enabled"
                self.settings.translation_deepseek_reasoning_effort = reasoning_effort
                self.settings.translation_deepseek_fast_layout_enabled = self.deepseek_fast_layout_enabled()
            elif provider_id == "gemini":
                thinking_mode, reasoning_effort = self.translation_thinking_values()
                self.settings.translation_gemini_thinking_enabled = thinking_mode == "enabled"
                self.settings.translation_gemini_reasoning_effort = reasoning_effort
        source_language = machine_translate.normalize_language_name(self.source_combo.currentText(), "英文")
        target_language = machine_translate.normalize_language_name(self.target_combo.currentText(), "简体中文")
        if self.explicit_source_mode:
            language_code = (
                machine_translate.mtran_language_code
                if self.local_machine_mode
                else lambda value: machine_translate.language_code(value, machine_translate.BING_PROVIDER)
            )
            if language_code(source_language) == language_code(target_language):
                QMessageBox.warning(self, "语言相同", "源语言和目标语言不能相同。")
                return
        if self.explicit_source_mode:
            self.settings.translation_source_language = source_language
            self.source_combo.setCurrentText(source_language)
        if self.local_machine_mode:
            self.settings.local_machine_parallelism = machine_translate.normalize_parallelism(self.local_parallel_spin.value())
        self.settings.translation_target_language = target_language
        self.target_combo.setCurrentText(target_language)
        self.settings.translation_mode = self.mode_combo.currentData() or "full_context"
        self.settings.translation_reference_paths = [] if self.free_machine_mode else list(self.reference_paths)
        # 即使当前切换到免费机翻，也保留用户已输入的规则，切回大模型后可继续使用。
        self.settings.translation_custom_instruction = self.custom_instruction_edit.toPlainText().strip()
        app_config.save_settings(self.settings)
        super().accept()


class ReaderWindow(QWidget):
    closed = Signal()

    def __init__(
        self,
        source_path: Path | None,
        translation_path: Path | None,
        live_translation_markdown: str = "",
        original_path: Path | None = None,
        initial_show_parsed: bool | None = None,
        parent=None,
    ):
        super().__init__(parent, Qt.WindowType.Window)
        self.source_path = source_path
        self.original_path = original_path
        self.translation_path = translation_path
        self.preview_provider = preview_tools.SourcePreviewProvider(WORKSPACE)
        self.live_translation_markdown = live_translation_markdown
        self._translation_live_page_ready = False
        self._translation_live_pending_markdown = ""
        self._syncing_scroll = False
        self._sync_poll_timer = None
        self._sync_poll_inflight = False
        self._sync_poll_generation = 0
        self._last_source_user_scroll_at = 0
        self._last_translation_user_scroll_at = 0
        self._layout_paired_canvas_active = False
        self._source_pdf_active = False
        self._pdf_source_sync_pending = False
        self._close_notified = False
        self.source_pdf_view = None
        self.source_web_view = None
        self.translation_web_view = None
        parent_settings = getattr(parent, "settings", None)
        # Settings from older versions may omit the layout-reading fields.
        # 阅读器在这种情况下使用普通阅读模式，不能因缺少可选设置而无法打开。
        self.layout_reading_mode = bool(getattr(parent_settings, "layout_reading_mode", False))
        self.reader_font_pt = int(getattr(parent_settings, "reader_font_pt", 12) or 12)
        self.layout_body_font_pt = self.saved_layout_body_font_pt()
        self._detected_layout_body_font_pt = None
        self.initial_show_parsed = bool(
            getattr(parent_settings, "show_parsed_source", False)
            if initial_show_parsed is None
            else initial_show_parsed
        )
        self.setWindowTitle("专注阅读模式")
        self.resize(1400, 900)
        if parent:
            self.setStyleSheet(parent.styleSheet())
        self.setup_ui()
        self.refresh_content()

    def setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)
        toolbar.addWidget(MonolithMark(24))
        title = QLabel("阅读模式")
        title.setObjectName("documentTitle")
        toolbar.addWidget(title, 1)
        self.reader_ai_button = QPushButton("AI")
        self.reader_ai_button.setObjectName("secondaryButton")
        self.reader_ai_button.setCheckable(True)
        self.both_button = QPushButton("双栏")
        self.source_button = QPushButton("只看原文")
        self.translation_button = QPushButton("只看译文")
        self.reader_show_parsed_check = QCheckBox("显示解析文件")
        self.reader_show_parsed_check.setChecked(self.initial_show_parsed)
        self.reader_show_parsed_check.toggled.connect(self.on_reader_source_mode_changed)
        parent_settings = getattr(self.parent(), "settings", None)
        self.reader_layout_restore_check = QCheckBox("版面还原")
        self.reader_layout_restore_check.setChecked(bool(getattr(parent_settings, "show_layout_restoration", False)))
        self.reader_layout_restore_check.toggled.connect(self.on_reader_layout_restore_toggled)
        self.reader_layout_restore_check.setVisible(False)
        self.reader_sync_scroll_check = QCheckBox("同步滚动")
        stream_sync_scroll = bool(
            getattr(
                parent_settings,
                "stream_sync_scroll",
                getattr(parent_settings, "sync_scroll", False),
            )
        )
        self.reader_sync_scroll_check.setChecked(True if self.layout_reading_mode else stream_sync_scroll)
        self.reader_sync_scroll_check.toggled.connect(self.on_sync_scroll_toggled)
        self.reader_font_spin = QDoubleSpinBox()
        self.reader_font_spin.setDecimals(1)
        self.reader_font_spin.setSingleStep(0.5)
        self.reader_font_spin.setRange(
            LAYOUT_BODY_FONT_MIN_PT if self.layout_reading_mode else READER_FONT_MIN_PT,
            LAYOUT_BODY_FONT_MAX_PT if self.layout_reading_mode else READER_FONT_MAX_PT,
        )
        self.reader_font_spin.setValue(self.layout_body_font_pt or self.reader_font_pt)
        self.reader_font_spin.setSuffix(" pt")
        self.reader_font_spin.valueChanged.connect(self.on_reader_font_changed)
        for button in [self.both_button, self.source_button, self.translation_button]:
            button.setObjectName("secondaryButton")
            button.setCheckable(True)
            toolbar.addWidget(button)
        # Keep the document-chat toggle beside the reader control and load the
        # chat component only when requested.
        toolbar.insertWidget(toolbar.indexOf(self.both_button), self.reader_ai_button)
        toolbar.addWidget(self.reader_show_parsed_check)
        toolbar.addWidget(self.reader_layout_restore_check)
        toolbar.addWidget(self.reader_sync_scroll_check)
        self.reader_font_control = create_reader_font_control(
            self.reader_font_spin,
            "调整阅读字号",
        )
        toolbar.addWidget(self.reader_font_control)
        # Focused reading retains these settings internally without showing the switches.
        self.reader_show_parsed_check.setVisible(False)
        self.reader_sync_scroll_check.setVisible(False)
        self.both_button.setChecked(True)
        self.both_button.clicked.connect(lambda: self.set_mode("both"))
        self.source_button.clicked.connect(lambda: self.set_mode("source"))
        self.translation_button.clicked.connect(lambda: self.set_mode("translation"))
        self.reader_ai_button.toggled.connect(self.toggle_reader_ai_sidebar)
        root.addLayout(toolbar)

        self.splitter = QSplitter()
        self.splitter.setObjectName("previewSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setOpaqueResize(False)
        self.splitter.setHandleWidth(10)
        self.reader_ai_panel = QWidget()
        self.reader_ai_panel.setObjectName("sideRail")
        # Width is controlled by the splitter; the document-chat panel has no artificial bounds.
        self.reader_ai_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.reader_ai_layout = QVBoxLayout(self.reader_ai_panel)
        self.reader_ai_layout.setContentsMargins(0, 0, 8, 0)
        self.reader_ai_layout.setSpacing(8)
        self.reader_ai_placeholder_label = QLabel("点击上方“AI”，开始针对当前文献提问。")
        self.reader_ai_placeholder_label.setObjectName("pathHint")
        self.reader_ai_placeholder_label.setWordWrap(True)
        self.reader_ai_layout.addWidget(self.reader_ai_placeholder_label)
        self.reader_ai_layout.addStretch(1)
        self.reader_ai_chat_window = None
        self.source_panel = QWidget()
        self.source_panel.setMinimumWidth(360)
        self.source_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        source_layout = QVBoxLayout(self.source_panel)
        source_layout.setContentsMargins(0, 0, 6, 0)
        source_layout.setSpacing(6)
        source_label = QLabel("原文")
        source_label.setObjectName("paneTitle")
        source_layout.addWidget(source_label)
        self.source_fallback_viewer = QTextBrowser()
        self.source_fallback_viewer.setObjectName("readerPane")
        self.source_fallback_viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.source_pdf_view = create_synced_pdf_view(self.source_panel)
        if self.source_pdf_view:
            source_layout.addWidget(self.source_pdf_view, 1)
            connect_pdf_source_sync(self)
            if self.parent() and hasattr(self.parent(), "install_export_context_menu"):
                self.parent().install_export_context_menu(self.source_pdf_view, "source", self)
        self.source_web_view = QWebEngineView() if WEBENGINE_AVAILABLE else None
        if self.source_web_view:
            configure_web_view(self.source_web_view)
            install_layout_formula_bridge(
                self.source_web_view,
                lambda payload: self.handle_formula_ai_quote("source", payload),
            )
            self.source_web_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.source_web_view.loadFinished.connect(lambda ok: self.apply_reader_font_size() if ok else None)
            self.source_web_view.loadFinished.connect(lambda ok: install_reader_scrollbar_style(self.source_web_view) if ok else None)
            self.source_web_view.loadFinished.connect(lambda ok: install_layout_loading_notice_style(self.source_web_view) if ok else None)
            self.source_web_view.loadFinished.connect(lambda ok: install_layout_image_memory_manager(self.source_web_view) if ok else None)
            self.source_web_view.loadFinished.connect(self.on_sync_view_load_finished)
            self.source_web_view.destroyed.connect(lambda _=None: self._on_sync_web_view_destroyed("source"))
            if self.parent() and hasattr(self.parent(), "install_export_context_menu"):
                self.parent().install_export_context_menu(self.source_web_view, "source", self)
            source_layout.addWidget(self.source_web_view, 1)
        else:
            if self.parent() and hasattr(self.parent(), "install_export_context_menu"):
                self.parent().install_export_context_menu(self.source_fallback_viewer, "source", self)
            self.source_fallback_viewer.setOpenExternalLinks(True)
            source_layout.addWidget(self.source_fallback_viewer, 1)
        set_source_pdf_active(self, False)
        apply_reader_font_to_text_browser(self.source_fallback_viewer, self.reader_font_pt)

        self.translation_panel = QWidget()
        self.translation_panel.setMinimumWidth(360)
        self.translation_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        translation_layout = QVBoxLayout(self.translation_panel)
        translation_layout.setContentsMargins(6, 0, 0, 0)
        translation_layout.setSpacing(6)
        translation_label = QLabel("译文")
        translation_label.setObjectName("paneTitle")
        translation_layout.addWidget(translation_label)
        self.translation_fallback_viewer = QTextBrowser()
        self.translation_fallback_viewer.setObjectName("readerPane")
        self.translation_fallback_viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.translation_web_view = QWebEngineView() if WEBENGINE_AVAILABLE else None
        if self.translation_web_view:
            configure_web_view(self.translation_web_view)
            install_layout_formula_bridge(
                self.translation_web_view,
                lambda payload: self.handle_formula_ai_quote("translation", payload),
            )
            self.translation_web_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.translation_web_view.loadFinished.connect(lambda ok: self.apply_reader_font_size() if ok else None)
            self.translation_web_view.loadFinished.connect(lambda ok: install_reader_scrollbar_style(self.translation_web_view) if ok else None)
            self.translation_web_view.loadFinished.connect(lambda ok: install_layout_loading_notice_style(self.translation_web_view) if ok else None)
            self.translation_web_view.loadFinished.connect(lambda ok: install_layout_image_lightbox(self.translation_web_view) if ok else None)
            self.translation_web_view.loadFinished.connect(lambda ok: install_layout_image_memory_manager(self.translation_web_view) if ok else None)
            self.translation_web_view.loadFinished.connect(lambda ok: install_layout_formula_lightbox_compat(self.translation_web_view) if ok else None)
            self.translation_web_view.loadFinished.connect(self.on_sync_view_load_finished)
            self.translation_web_view.destroyed.connect(lambda _=None: self._on_sync_web_view_destroyed("translation"))
            if self.parent() and hasattr(self.parent(), "install_export_context_menu"):
                self.parent().install_export_context_menu(self.translation_web_view, "translation", self)
            translation_layout.addWidget(self.translation_web_view, 1)
        else:
            if self.parent() and hasattr(self.parent(), "install_export_context_menu"):
                self.parent().install_export_context_menu(self.translation_fallback_viewer, "translation", self)
            self.translation_fallback_viewer.setOpenExternalLinks(True)
            translation_layout.addWidget(self.translation_fallback_viewer, 1)
        apply_reader_font_to_text_browser(self.translation_fallback_viewer, self.reader_font_pt)

        self.splitter.addWidget(self.reader_ai_panel)
        self.splitter.addWidget(self.source_panel)
        self.splitter.addWidget(self.translation_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 1)
        self.reader_ai_panel.setVisible(False)
        self.set_reader_splitter_mode_sizes("both")
        self._preview_refit_timer = QTimer(self)
        self._preview_refit_timer.setSingleShot(True)
        self._preview_refit_timer.timeout.connect(self.refit_preview_pages)
        self._preview_sync_timer = QTimer(self)
        self._preview_sync_timer.setSingleShot(True)
        self._preview_sync_timer.timeout.connect(self.sync_visible_previews_after_refit)
        self._reader_ai_rebalance_timer = QTimer(self)
        self._reader_ai_rebalance_timer.setSingleShot(True)
        self._reader_ai_rebalance_timer.timeout.connect(self.rebalance_reader_panes_after_ai_resize)
        self.splitter.splitterMoved.connect(self.on_reader_splitter_moved)
        root.addWidget(self.splitter, 1)
        self.update_layout_mode_controls()

    def schedule_preview_refit(self, *args):
        if hasattr(self, "_preview_refit_timer"):
            self._preview_refit_timer.start(80)
        else:
            QTimer.singleShot(80, self.refit_preview_pages)
        if hasattr(self, "_preview_sync_timer"):
            self._preview_sync_timer.start(180)
        else:
            QTimer.singleShot(180, self.sync_visible_previews_after_refit)

    def reader_splitter_available_width(self) -> int:
        # Use the actual available width. A 720px fallback makes the chat panel grow after it is widened,
        # 向 QSplitter 请求超过容器的阅读区尺寸，导致原文/译文比例失真。
        return max(1, int(self.splitter.width() or self.width() or 1400))

    def on_reader_splitter_moved(self, _position: int, index: int):
        """Keep the visible reading panes balanced after the chat panel changes."""
        self.schedule_preview_refit()
        # In the three-pane splitter, index 1 separates document chat from the
        # source and index 2 separates the source from the translation.
        if index == 1 and self.reader_ai_panel.isVisible():
            self._reader_ai_rebalance_timer.start(100)

    def rebalance_reader_panes_after_ai_resize(self):
        sizes = self.splitter.sizes()
        if len(sizes) < 3 or not self.reader_ai_panel.isVisible():
            return
        ai_width = max(0, int(sizes[0]))
        reading_width = max(1, int(sizes[1]) + int(sizes[2]))
        if self.source_panel.isVisible() and self.translation_panel.isVisible():
            source_width = reading_width // 2
            self.splitter.setSizes([ai_width, source_width, reading_width - source_width])
        elif self.source_panel.isVisible():
            self.splitter.setSizes([ai_width, reading_width, 0])
        elif self.translation_panel.isVisible():
            self.splitter.setSizes([ai_width, 0, reading_width])
        self.schedule_preview_refit()

    def set_reader_splitter_mode_sizes(self, mode: str):
        total = self.reader_splitter_available_width()
        ai_width = total // 3 if self.reader_ai_panel.isVisible() else 0
        reading_width = max(720, total - ai_width)
        if mode == "source":
            self.splitter.setSizes([ai_width, reading_width, 0])
        elif mode == "translation":
            self.splitter.setSizes([ai_width, 0, reading_width])
        else:
            left = reading_width // 2
            self.splitter.setSizes([ai_width, left, reading_width - left])

    def toggle_reader_ai_sidebar(self, visible: bool):
        """Move the shared document-chat widget between the reader and main window."""
        if visible and not self.ensure_reader_ai_chat():
            self.reader_ai_button.blockSignals(True)
            self.reader_ai_button.setChecked(False)
            self.reader_ai_button.blockSignals(False)
            return
        self.reader_ai_panel.setVisible(visible)
        if not visible:
            self.release_reader_ai_chat()
        self.set_reader_splitter_mode_sizes(
            "source" if self.source_panel.isVisible() and not self.translation_panel.isVisible()
            else "translation" if self.translation_panel.isVisible() and not self.source_panel.isVisible()
            else "both"
        )
        self.schedule_preview_refit()

    def handle_formula_ai_quote(self, pane: str, payload: dict):
        parent_window = self.parent()
        if parent_window and hasattr(parent_window, "handle_formula_ai_quote"):
            parent_window.handle_formula_ai_quote(pane, payload, owner_window=self)

    def reveal_reference_quote(self, quote: dict) -> bool:
        """Locate a referenced passage, image, or formula in the focused reader."""
        if not isinstance(quote, dict):
            return False
        quote, _resolution = resolve_reference_quote(
            quote,
            source_path=self.source_path,
            translation_path=self.translation_path,
            live_translation=self.live_translation_markdown,
        )
        quote_path = canonical_reference_document_path(
            quote.get("document_path") or quote.get("source_markdown_path") or quote.get("markdown_path") or quote.get("path")
        )
        if quote_path and self.source_path:
            try:
                if quote_path.resolve() != self.source_path.resolve():
                    return False
            except OSError:
                return False
        target = str(quote.get("formula_tex") or quote.get("text") or "").strip()
        if not target:
            return False
        pane = str(quote.get("pane") or "source")
        # 排版模式下两栏共享页码与页面坐标。用户主动选择单栏时，不应为了
        # 引用强制拉起隐藏栏；直接在当前可见栏按同一锚点定位即可。
        target_hidden = (
            (pane == "translation" and not self.translation_panel.isVisible())
            or (pane != "translation" and not self.source_panel.isVisible())
        )
        if self.layout_reading_mode and target_hidden:
            pane = "source" if self.source_panel.isVisible() else "translation"
            quote = dict(quote)
            quote["pane"] = pane
            quote["focus_notice"] = "已在当前可见栏按页面锚点定位。"
        elif pane == "translation" and not self.translation_panel.isVisible():
            self.set_mode("translation")
        elif pane != "translation" and not self.source_panel.isVisible():
            self.set_mode("source")
        self.raise_()
        self.activateWindow()
        if pane != "translation" and pdf_view_is_active(self):
            QTimer.singleShot(0, lambda item=dict(quote): focus_pdf_reference_quote(self, item))
            return True
        web_view = self.translation_web_view if pane == "translation" else self.source_web_view
        fallback_viewer = self.translation_fallback_viewer if pane == "translation" else self.source_fallback_viewer
        QTimer.singleShot(0, lambda item=dict(quote), view=web_view, fallback=fallback_viewer: focus_reference_quote(view, fallback, item))
        return True

    def reader_document_chat_session_id(self) -> str:
        parent_window = self.parent()
        if parent_window and hasattr(parent_window, "document_chat_session_id"):
            return parent_window.document_chat_session_id(self.source_path)
        if not self.source_path:
            return ""
        try:
            folder = str(self.source_path.parent.resolve())
        except Exception:
            folder = str(self.source_path.parent)
        return "doc-chat-" + hashlib.sha1(folder.encode("utf-8", errors="replace")).hexdigest()

    def ensure_reader_ai_chat(self) -> bool:
        """Use the shared document-chat widget instead of creating another session."""
        if self.reader_ai_chat_window is not None:
            return True
        if not self.source_path:
            self.reader_ai_placeholder_label.setText("当前没有可用于文献对话的解析文献。")
            return False
        parent_window = self.parent()
        if not parent_window or not hasattr(parent_window, "ensure_embedded_chat"):
            self.reader_ai_placeholder_label.setText("未找到文献对话组件。")
            return False

        previous_host = getattr(parent_window, "_reader_ai_host", None)
        if previous_host is not None and previous_host is not self:
            previous_host.release_reader_ai_chat()
            previous_host.reader_ai_button.blockSignals(True)
            previous_host.reader_ai_button.setChecked(False)
            previous_host.reader_ai_button.blockSignals(False)
            previous_host.reader_ai_panel.setVisible(False)

        chat = parent_window.ensure_embedded_chat()
        if not chat:
            self.reader_ai_placeholder_label.setText("文献对话加载失败。")
            return False
        # Rebind quote navigation while the shared chat widget is hosted here.
        chat.reference_quote_reveal_callback = self.reveal_reference_quote
        if getattr(chat, "chat_worker", None) and chat.chat_worker.isRunning():
            self.reader_ai_placeholder_label.setText("文献对话正在生成回复，请完成后再切换阅读模式。")
            return False

        # The main window owns model refresh for the shared chat widget.
        parent_window.refresh_models_when_ai_first_opened()
        session_id = self.reader_document_chat_session_id()
        if session_id:
            chat.load_document_conversation(session_id, self.source_path.parent.name, self.source_path)

        self.reader_ai_chat_window = chat
        parent_window._reader_ai_host = self
        self.reader_ai_placeholder_label.setVisible(False)
        # setup 时的弹性占位项只能用于空状态；保留它会与聊天控件平分高度，
        # leaving unused space in the lower part of the panel.
        for index in range(self.reader_ai_layout.count() - 1, -1, -1):
            item = self.reader_ai_layout.itemAt(index)
            if item and item.spacerItem():
                self.reader_ai_layout.takeAt(index)
        self.reader_ai_layout.insertWidget(0, chat, 1)
        return True

    def release_reader_ai_chat(self):
        """Return the shared chat widget to the main window."""
        chat = self.reader_ai_chat_window
        if chat is None:
            return
        self.reader_ai_chat_window = None
        parent_window = self.parent()
        if parent_window and hasattr(parent_window, "ai_sidebar_layout"):
            if hasattr(parent_window, "reveal_reference_quote"):
                chat.reference_quote_reveal_callback = parent_window.reveal_reference_quote
            parent_window.ai_placeholder_label.setVisible(False)
            parent_window.ai_sidebar_layout.insertWidget(0, chat, 1)
            if getattr(parent_window, "_reader_ai_host", None) is self:
                parent_window._reader_ai_host = None

    def refit_preview_pages(self):
        script = """
        (() => {
          window.__mineruForcedPageMetrics = null;
          if (window.__mineruFitLayoutPages) window.__mineruFitLayoutPages();
        })();
        """
        for web_view in (getattr(self, "source_web_view", None), getattr(self, "translation_web_view", None)):
            if web_view and web_view.isVisible():
                self._run_sync_javascript(web_view, script)

    def sync_visible_previews_after_refit(self):
        if not getattr(self, "source_panel", None) or not getattr(self, "translation_panel", None):
            return
        if not self.source_panel.isVisible() or not self.translation_panel.isVisible():
            return
        self.sync_translation_to_source_now()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.schedule_preview_refit()

    def closeEvent(self, event):
        self.release_reader_ai_chat()
        self.shutdown_webengines()
        if not self._close_notified:
            self._close_notified = True
            self.closed.emit()
        super().closeEvent(event)

    def shutdown_webengines(self):
        """Stop this reader's timers and release both Chromium-backed panes."""
        self.reset_sync_scroll_runtime()
        release_source_pdf(self)
        for attribute in ("source_web_view", "translation_web_view"):
            web_view = getattr(self, attribute, None)
            setattr(self, attribute, None)
            dispose_web_view(web_view)

    def reset_sync_scroll_runtime(self):
        """页面切换或重翻前重置同步桥运行态，避免旧页面的异步回调卡住新页面。"""
        self._sync_poll_generation += 1
        self._syncing_scroll = False
        self._sync_poll_inflight = False
        self._last_source_user_scroll_at = 0
        self._last_translation_user_scroll_at = 0
        if self._sync_poll_timer is not None:
            try:
                self._sync_poll_timer.stop()
            except RuntimeError:
                pass

    def _on_sync_web_view_destroyed(self, pane: str):
        self.reset_sync_scroll_runtime()
        setattr(self, f"{pane}_web_view", None)

    def on_sync_view_load_finished(self, ok: bool):
        """以真实 loadFinished 为准重新安装桥，替代只依赖固定延时的脆弱时序。"""
        if not ok:
            return
        self._sync_poll_inflight = False
        QTimer.singleShot(0, self.install_sync_scroll_bridge)
        QTimer.singleShot(80, self.sync_translation_to_source_now)

    def update_layout_mode_controls(self):
        if hasattr(self, "reader_font_spin"):
            self.reader_font_spin.blockSignals(True)
            self.reader_font_spin.setRange(
                LAYOUT_BODY_FONT_MIN_PT if self.layout_reading_mode else READER_FONT_MIN_PT,
                LAYOUT_BODY_FONT_MAX_PT if self.layout_reading_mode else READER_FONT_MAX_PT,
            )
            self.reader_font_spin.setValue(
                (
                    self.saved_layout_body_font_pt()
                    or self._detected_layout_body_font_pt
                    or self.layout_body_font_pt
                    or self.reader_font_spin.value()
                )
                if self.layout_reading_mode
                else self.reader_font_pt
            )
            self.reader_font_spin.blockSignals(False)
        if hasattr(self, "reader_show_parsed_check"):
            self.reader_show_parsed_check.setText("显示解析文件")
            self.reader_show_parsed_check.setVisible(False)
        if hasattr(self, "reader_sync_scroll_check"):
            self.reader_sync_scroll_check.setVisible(False)

    def layout_development_mode_enabled(self) -> bool:
        parent_settings = getattr(self.parent(), "settings", None)
        return bool(self.layout_reading_mode and parent_settings and getattr(parent_settings, "layout_development_mode", False))

    def schedule_layout_debug_overlay_update(self, delay_ms: int = 550):
        if not self.layout_reading_mode:
            return
        enabled = self.layout_development_mode_enabled()
        QTimer.singleShot(delay_ms, lambda: apply_layout_debug_mode_to_web_view(self.source_web_view, enabled))
        QTimer.singleShot(delay_ms, lambda: apply_layout_debug_mode_to_web_view(self.translation_web_view, enabled))

    def on_reader_source_mode_changed(self, checked: bool):
        parent_window = self.parent()
        parent_settings = getattr(parent_window, "settings", None)
        if parent_settings is not None:
            if self.layout_reading_mode:
                parent_settings.layout_show_parsed_source = bool(checked)
            else:
                parent_settings.stream_show_parsed_source = bool(checked)
            parent_settings.show_parsed_source = bool(checked)
            app_config.save_settings(parent_settings)
            if (
                hasattr(parent_window, "show_parsed_source_check")
                and bool(getattr(parent_settings, "layout_reading_mode", False)) == self.layout_reading_mode
            ):
                parent_window.show_parsed_source_check.blockSignals(True)
                parent_window.show_parsed_source_check.setChecked(bool(checked))
                parent_window.show_parsed_source_check.blockSignals(False)
        self.leave_layout_paired_canvas()
        self.refresh_content()

    def saved_layout_body_font_pt(self) -> float | None:
        parent_settings = getattr(self.parent(), "settings", None)
        memory = getattr(parent_settings, "layout_body_font_by_document", {}) if parent_settings else {}
        value = memory.get(layout_body_font_document_key(self.source_path)) if isinstance(memory, dict) else None
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def on_reader_font_changed(self, value: float):
        parent_settings = getattr(self.parent(), "settings", None)
        if self.layout_reading_mode:
            self.layout_body_font_pt = float(value)
            if parent_settings is not None:
                memory = getattr(parent_settings, "layout_body_font_by_document", None)
                if not isinstance(memory, dict):
                    memory = {}
                    parent_settings.layout_body_font_by_document = memory
                key = layout_body_font_document_key(self.source_path)
                if key:
                    memory[key] = self.layout_body_font_pt
                app_config.save_settings(parent_settings)
            if self.parent() and hasattr(self.parent(), "set_layout_body_font_for_current_document"):
                self.parent().set_layout_body_font_for_current_document(self.layout_body_font_pt, source=self)
            self.apply_reader_font_size()
            return
        self.reader_font_pt = int(value)
        if parent_settings is not None:
            parent_settings.reader_font_pt = self.reader_font_pt
            app_config.save_settings(parent_settings)
            if hasattr(self.parent(), "reader_font_spin"):
                self.parent().reader_font_spin.blockSignals(True)
                self.parent().reader_font_spin.setValue(self.reader_font_pt)
                self.parent().reader_font_spin.blockSignals(False)
                self.parent().apply_reader_font_size()
        self.apply_reader_font_size()

    def apply_reader_font_size(self):
        if self.layout_reading_mode:
            saved = self.saved_layout_body_font_pt()
            script = layout_body_font_script(saved or self.layout_body_font_pt)
            for web_view in (self.source_web_view, self.translation_web_view):
                if web_view:
                    web_view.page().runJavaScript(script)
            if saved is None:
                self.refresh_layout_body_font_display()
            return
        apply_reader_font_to_web_view(self.source_web_view, self.reader_font_pt)
        apply_reader_font_to_web_view(self.translation_web_view, self.reader_font_pt)
        apply_reader_font_to_text_browser(self.source_fallback_viewer, self.reader_font_pt)
        apply_reader_font_to_text_browser(self.translation_fallback_viewer, self.reader_font_pt)

    def refresh_layout_body_font_display(self, attempt: int = 0, view_index: int = 0):
        """Show the fitted body size without turning it into a user override."""
        if not self.layout_reading_mode or self.saved_layout_body_font_pt() is not None:
            return
        views = [view for view in (self.source_web_view, self.translation_web_view) if view]
        if not views:
            return
        if view_index >= len(views):
            if attempt < 600:
                QTimer.singleShot(100, lambda: self.refresh_layout_body_font_display(attempt + 1, 0))
            return

        def receive(payload):
            data = decode_web_javascript_payload(payload) or {}
            try:
                value = float(data.get("fontPt") or 0)
            except (TypeError, ValueError):
                value = 0.0
            if data.get("ready") is True and value > 0:
                self._detected_layout_body_font_pt = value
                self.reader_font_spin.blockSignals(True)
                self.reader_font_spin.setValue(value)
                self.reader_font_spin.blockSignals(False)
                return
            self.refresh_layout_body_font_display(attempt, view_index + 1)

        views[view_index].page().runJavaScript(layout_body_font_probe_script(), receive)

    def refresh_content(self):
        self.reset_sync_scroll_runtime()
        if self.layout_reading_mode and self.show_layout_paired_canvas():
            self.update_reader_sync_scroll_availability()
            return
        self.leave_layout_paired_canvas()
        if self.source_path and self.source_path.exists():
            if self.reader_show_parsed_check.isChecked():
                self.show_markdown_path(
                    self.source_path,
                    self.source_web_view,
                    self.source_fallback_viewer,
                    prefer_layout=self.layout_reading_mode and self.reader_layout_restore_check.isChecked(),
                )
            else:
                self.show_original_or_parsed()
        else:
            self.show_html(self.source_web_view, self.source_fallback_viewer, "暂无原文。")
            self.source_button.setEnabled(False)

        if self.translation_path and self.translation_path.exists():
            if self.translation_path.suffix.lower() in {".html", ".htm"}:
                self.show_html_path(self.translation_path, self.translation_web_view, self.translation_fallback_viewer)
            else:
                self.show_markdown_path(self.translation_path, self.translation_web_view, self.translation_fallback_viewer)
            self.translation_button.setEnabled(True)
        elif self.live_translation_markdown.strip():
            self.show_live_translation(self.live_translation_markdown)
            self.translation_button.setEnabled(True)
        else:
            self.show_html(self.translation_web_view, self.translation_fallback_viewer, "暂无译文。")
            self.translation_button.setEnabled(False)
        self.update_reader_sync_scroll_availability()

    def reader_layout_paired_source_page_path(self) -> Path | None:
        if not self.source_path:
            return None
        if self.reader_show_parsed_check.isChecked():
            layout_path = render_layout_preview_html(
                self.source_path,
                strict_fit=True,
                debug_overlay=False,
            )
            return layout_path if layout_path and layout_path.exists() else None
        if self.original_path and self.original_path.exists() and self.original_path.suffix.lower() == ".pdf":
            if self.layout_development_mode_enabled():
                path = render_original_pdf_debug_preview_html(self.original_path, self.source_path)
            else:
                path = render_original_pdf_preview_html(self.original_path)
            return path if path and path.exists() else None
        return None

    def show_layout_paired_canvas(self) -> bool:
        # Keep the translation pane on its standalone layout HTML.  The paired
        # canvas changes the translation viewport and page-scaling math when the
        # source-side "显示解析文件" checkbox changes, which makes the delivered
        # translation appear to reflow even though the translation file is the same.
        return False

    def leave_layout_paired_canvas(self):
        if not self._layout_paired_canvas_active:
            return
        self._layout_paired_canvas_active = False
        self.translation_panel.setVisible(True)

    def show_markdown_path(self, markdown_path: Path, web_view, fallback_viewer: QTextBrowser, prefer_layout: bool = False):
        if web_view is self.source_web_view:
            set_source_pdf_active(self, False)
        if web_view is self.translation_web_view:
            self._translation_live_page_ready = False
            self._translation_live_pending_markdown = ""
        html_path = (
            render_layout_preview_html(
                markdown_path,
                strict_fit=True,
                debug_overlay=False,
            )
            if prefer_layout
            else render_preview_html(markdown_path)
        )
        if prefer_layout:
            upgrade_layout_loading_notice_html(html_path)
        if web_view and html_path and html_path.exists():
            set_or_reload_web_view_url(web_view, QUrl.fromLocalFile(str(html_path)))
            QTimer.singleShot(250, self.apply_reader_font_size)
            QTimer.singleShot(250, lambda: ensure_web_view_mathjax_typeset(web_view))
            QTimer.singleShot(500, self.install_sync_scroll_bridge)
            self.schedule_layout_debug_overlay_update()
        elif html_path and html_path.exists():
            fallback_viewer.setSource(QUrl.fromLocalFile(str(html_path)))
            self.apply_reader_font_size()
        else:
            fallback_viewer.setSearchPaths([str(markdown_path.parent)])
            fallback_viewer.setMarkdown(markdown_path.read_text(encoding="utf-8", errors="replace"))
            self.apply_reader_font_size()

    def show_html_path(self, html_path: Path, web_view, fallback_viewer: QTextBrowser):
        if web_view is self.source_web_view:
            set_source_pdf_active(self, False)
        upgrade_layout_loading_notice_html(html_path)
        if web_view is self.translation_web_view:
            self._translation_live_page_ready = False
            self._translation_live_pending_markdown = ""
        if web_view:
            set_or_reload_web_view_url(web_view, QUrl.fromLocalFile(str(html_path.resolve())))
            QTimer.singleShot(250, self.apply_reader_font_size)
            QTimer.singleShot(500, self.install_sync_scroll_bridge)
            self.schedule_layout_debug_overlay_update()
        else:
            fallback_viewer.setSource(QUrl.fromLocalFile(str(html_path.resolve())))
            self.apply_reader_font_size()

    def show_original_or_parsed(self):
        if self.layout_reading_mode and not self.reader_show_parsed_check.isChecked():
            if not self.original_path or not self.original_path.exists():
                self.show_html(self.source_web_view, self.source_fallback_viewer, "原始文件缺失，无法显示原始文件。")
                return
            source_path = self.original_path
        else:
            source_path = self.original_path or self.source_path
        if not source_path or not self.source_path:
            self.show_html(self.source_web_view, self.source_fallback_viewer, "暂无原文。")
            return
        if (
            source_path.suffix.lower() == ".pdf"
            and not self.layout_development_mode_enabled()
            and load_source_pdf(self, source_path)
        ):
            return
        set_source_pdf_active(self, False)
        kind, payload = self.preview_provider.source_url_or_html(
            source_path,
            self.source_path,
            preview_tools.PreviewMode.ORIGINAL,
            prefer_layout=False,
        )
        if kind == "url":
            if self.source_web_view:
                set_or_reload_web_view_url(self.source_web_view, qurl_from_payload(str(payload)))
                QTimer.singleShot(250, self.apply_reader_font_size)
                QTimer.singleShot(500, self.install_sync_scroll_bridge)
            else:
                self.source_fallback_viewer.setSource(qurl_from_payload(str(payload)))
                self.apply_reader_font_size()
        elif kind == "markdown":
            self.show_markdown_path(Path(payload), self.source_web_view, self.source_fallback_viewer)
        else:
            if self.source_web_view:
                self.source_web_view.setHtml(str(payload), QUrl.fromLocalFile(str(WORKSPACE)))
                QTimer.singleShot(250, self.apply_reader_font_size)
                QTimer.singleShot(500, self.install_sync_scroll_bridge)
            else:
                self.source_fallback_viewer.setHtml(str(payload))
                self.apply_reader_font_size()

    def on_reader_layout_restore_toggled(self, checked: bool):
        if self.parent() and hasattr(self.parent(), "settings"):
            self.parent().settings.show_layout_restoration = checked
            app_config.save_settings(self.parent().settings)
            if hasattr(self.parent(), "show_layout_restoration_check"):
                self.parent().show_layout_restoration_check.blockSignals(True)
                self.parent().show_layout_restoration_check.setChecked(checked)
                self.parent().show_layout_restoration_check.blockSignals(False)
                self.parent().update_sync_scroll_availability()
        self.refresh_content()

    def update_reader_sync_scroll_availability(self):
        translation_available = self.translation_path is not None or bool(self.live_translation_markdown.strip())
        if self.layout_reading_mode:
            self.reader_sync_scroll_check.blockSignals(True)
            self.reader_sync_scroll_check.setChecked(True)
            self.reader_sync_scroll_check.setEnabled(True)
            self.reader_sync_scroll_check.setVisible(False)
            self.reader_sync_scroll_check.blockSignals(False)
            return
        enabled = (
            self.reader_show_parsed_check.isChecked()
            and self.source_web_view is not None
            and self.translation_web_view is not None
            and translation_available
        )
        self.reader_sync_scroll_check.setVisible(False)
        self.reader_sync_scroll_check.setEnabled(enabled)
        if not enabled:
            # 重翻或页面切换只会让同步暂时不可用，不能因此清除用户原来的流式同步偏好。
            parent_settings = getattr(self.parent(), "settings", None) if self.parent() else None
            preferred = bool(
                getattr(
                    parent_settings,
                    "stream_sync_scroll",
                    getattr(parent_settings, "sync_scroll", False),
                )
            )
            self.reader_sync_scroll_check.blockSignals(True)
            self.reader_sync_scroll_check.setChecked(preferred)
            self.reader_sync_scroll_check.blockSignals(False)

    def on_sync_scroll_toggled(self, checked: bool):
        if self.layout_reading_mode:
            self.reader_sync_scroll_check.blockSignals(True)
            self.reader_sync_scroll_check.setChecked(True)
            self.reader_sync_scroll_check.blockSignals(False)
            self.show_layout_paired_canvas()
            return
        if self.parent() and hasattr(self.parent(), "settings"):
            self.parent().settings.sync_scroll = checked
            self.parent().settings.stream_sync_scroll = checked
            app_config.save_settings(self.parent().settings)
        if checked:
            self.install_sync_scroll_bridge()
            QTimer.singleShot(120, self.sync_translation_to_source_now)
        else:
            was_paired = self._layout_paired_canvas_active
            self.leave_layout_paired_canvas()
            self.reset_sync_page_scaling()
            if was_paired:
                self.refresh_content()

    def reset_sync_page_scaling(self):
        script = """
        (() => {
          window.__mineruForcedPageMetrics = null;
          if (window.__mineruFitLayoutPages) window.__mineruFitLayoutPages();
        })();
        """
        self._run_sync_javascript(self.source_web_view, script)
        self._run_sync_javascript(self.translation_web_view, script)

    def sync_translation_to_source_now(self):
        if (
            not self.reader_sync_scroll_check.isChecked()
            or (not self.layout_reading_mode and not self.reader_show_parsed_check.isChecked())
            or not self.source_web_view
            or not self.translation_web_view
        ):
            return
        if pdf_view_is_active(self):
            payload = pdf_sync_payload(self.source_pdf_view)
            if payload:
                self.apply_sync_payload_to_target(self.translation_web_view, payload)
            return
        script = """
        (() => {
          const api = window.syncScrollApi;
          if (!api) return null;
          const payload = api.syncPayload ? api.syncPayload() : {
            ratio: api.scrollRatio(),
            heading: api.currentHeadingPosition ? api.currentHeadingPosition() : null
          };
          return JSON.stringify(payload);
        })();
        """
        generation = self._sync_poll_generation

        def apply_payload(payload):
            payload = decode_web_javascript_payload(payload)
            if generation != self._sync_poll_generation or not payload:
                return
            self.apply_sync_payload_to_target(self.translation_web_view, payload)

        self._run_sync_javascript(self.source_web_view, script, apply_payload)

    @staticmethod
    def _sync_web_view_page(web_view):
        if web_view is None:
            return None
        try:
            page = web_view.page()
            if page is None:
                return None
            page.url()
            return page
        except RuntimeError:
            return None

    def _run_sync_javascript(self, web_view, script: str, callback=None) -> bool:
        page = self._sync_web_view_page(web_view)
        if page is None:
            return False
        try:
            if callback is None:
                page.runJavaScript(script)
            else:
                page.runJavaScript(script, callback)
            return True
        except RuntimeError:
            return False

    def apply_sync_payload_to_target(self, target, payload):
        if target is None or not payload:
            return
        self._syncing_scroll = True
        safe_payload = json.dumps(payload, ensure_ascii=False)
        if not self._run_sync_javascript(
            target,
            f"""
            (() => {{
              const payload = {safe_payload};
              const api = window.syncScrollApi;
              if (!api) return;
              if (api.scrollToSyncPayload && api.scrollToSyncPayload(payload, false)) return;
              if (!api.scrollToHeadingPosition || !api.scrollToHeadingPosition(payload.heading, false)) {{
                api.scrollToRatio(Number(payload.ratio || 0), false);
              }}
            }})();
            """,
        ):
            self._syncing_scroll = False
            return
        QTimer.singleShot(16, lambda: setattr(self, "_syncing_scroll", False))

    def ensure_sync_poll_timer(self):
        if self._sync_poll_timer is not None:
            return
        self._sync_poll_timer = QTimer(self)
        self._sync_poll_timer.setInterval(16)
        self._sync_poll_timer.timeout.connect(self.poll_sync_scroll_bridge)

    def poll_sync_scroll_bridge(self):
        if (
            (not self.layout_reading_mode and self._syncing_scroll)
            or self._sync_poll_inflight
            or not self.reader_sync_scroll_check.isChecked()
            or (not self.layout_reading_mode and not self.reader_show_parsed_check.isChecked())
            or not self.source_web_view
            or not self.translation_web_view
        ):
            return
        if pdf_view_is_active(self):
            poll_translation_web_to_pdf(self)
            return

        self._sync_poll_inflight = True
        generation = self._sync_poll_generation
        state = {"pane": "", "userScrollAt": 0, "payload": None}
        pending = {"count": 2}

        def finish_one():
            if generation != self._sync_poll_generation:
                return
            pending["count"] -= 1
            if pending["count"] > 0:
                return
            self._sync_poll_inflight = False
            pane = state["pane"]
            user_scroll_at = int(state["userScrollAt"] or 0)
            payload = state["payload"]
            if not pane or not payload or user_scroll_at <= 0:
                return
            if pane == "source":
                if user_scroll_at <= self._last_source_user_scroll_at:
                    return
                self._last_source_user_scroll_at = user_scroll_at
                self.apply_sync_payload_to_target(self.translation_web_view, payload)
            else:
                if user_scroll_at <= self._last_translation_user_scroll_at:
                    return
                self._last_translation_user_scroll_at = user_scroll_at
                self.apply_sync_payload_to_target(self.source_web_view, payload)

        def handle_result(pane):
            def _inner(result):
                try:
                    result = decode_web_javascript_payload(result) or {}
                    user_scroll_at = int((result or {}).get("userScrollAt") or 0)
                    payload = (result or {}).get("payload")
                    if user_scroll_at > int(state["userScrollAt"] or 0) and payload:
                        state["pane"] = pane
                        state["userScrollAt"] = user_scroll_at
                        state["payload"] = payload
                finally:
                    finish_one()
            return _inner

        script = """
        (() => {
          if (!window.__mineruGetSyncState) return null;
          return JSON.stringify(window.__mineruGetSyncState());
        })();
        """
        def request_state(web_view, callback):
            if not self._run_sync_javascript(web_view, script, callback):
                callback(None)

        request_state(self.source_web_view, handle_result("source"))
        request_state(self.translation_web_view, handle_result("translation"))

    def install_sync_scroll_bridge(self):
        if (
            not self.reader_sync_scroll_check.isChecked()
            or (not self.layout_reading_mode and not self.reader_show_parsed_check.isChecked())
            or not self.source_web_view
            or not self.translation_web_view
        ):
            if self._sync_poll_timer is not None:
                self._sync_poll_timer.stop()
            return
        # This template is inserted as raw JavaScript and must keep single braces.
        script_template = """
        (() => {
          const bridgeVersion = 2;
          if (window.__mineruSyncInstalled === bridgeVersion) return;
          window.__mineruSyncInstalled = bridgeVersion;
          window.__mineruLastUserScrollAt = 0;
          window.__mineruLastObservedScrollTop = -1;
          window.__mineruGetSyncState = () => {
            const api = window.syncScrollApi;
            if (!api) return null;
            const root = document.scrollingElement || document.documentElement;
            const scrollTop = Number(root ? root.scrollTop : 0);
            const now = Date.now();
            if (Math.abs(scrollTop - Number(window.__mineruLastObservedScrollTop || 0)) > 0.5) {
              if (now >= Number(window.__mineruProgrammaticScrollUntil || 0)) {
                window.__mineruLastUserScrollAt = now;
              }
              window.__mineruLastObservedScrollTop = scrollTop;
            }
            const payload = api.syncPayload ? api.syncPayload() : {
              ratio: api.scrollRatio(),
              heading: api.currentHeadingPosition ? api.currentHeadingPosition() : null
            };
            return {
              userScrollAt: Number(window.__mineruLastUserScrollAt || 0),
              scrollTop,
              payload
            };
          };
          window.addEventListener('scroll', () => {
            if (Date.now() < Number(window.__mineruProgrammaticScrollUntil || 0)) return;
            window.__mineruLastUserScrollAt = Date.now();
          }, { passive: true });
        })();
        """
        if not self.layout_reading_mode:
            script_template = """
            (() => {
              if (window.__mineruSyncInstalled) return;
              window.__mineruSyncInstalled = true;
              window.__mineruLastUserScrollAt = 0;
              window.__mineruGetSyncState = () => {
                const api = window.syncScrollApi;
                if (!api) return null;
                const payload = api.syncPayload ? api.syncPayload() : {
                  ratio: api.scrollRatio(),
                  heading: api.currentHeadingPosition ? api.currentHeadingPosition() : null
                };
                return {
                  userScrollAt: Number(window.__mineruLastUserScrollAt || 0),
                  payload
                };
              };
              window.addEventListener('scroll', () => {
                if (Date.now() < Number(window.__mineruProgrammaticScrollUntil || 0)) return;
                window.__mineruLastUserScrollAt = Date.now();
              }, { passive: true });
            })();
            """
        if not pdf_view_is_active(self):
            self._run_sync_javascript(self.source_web_view, script_template)
        self._run_sync_javascript(self.translation_web_view, script_template)
        self.ensure_sync_poll_timer()
        self._sync_poll_timer.start()
        QTimer.singleShot(40, self.sync_translation_to_source_now)

    def show_html(self, web_view, fallback_viewer: QTextBrowser, text: str):
        if web_view is self.source_web_view:
            set_source_pdf_active(self, False)
        body = html.escape(text)
        font_face_css = bundled_reader_font_face_css()
        content = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<style>{font_face_css}</style></head>"
            f"<body style=\"font-family:{READER_SERIF_FONT_STACK};"
            "padding:32px;color:#1f2933;background:#fbfcfd;\">"
            f"{body}</body></html>"
        )
        if web_view:
            web_view.setHtml(content)
        else:
            fallback_viewer.setHtml(content)

    def show_live_translation(self, markdown: str):
        self.live_translation_markdown = markdown
        self.translation_button.setEnabled(bool(markdown.strip()))

        if self.translation_web_view:
            self._translation_live_pending_markdown = markdown

            def update_live_page():
                safe_text = json.dumps(self._translation_live_pending_markdown, ensure_ascii=False)
                js = f"""
                (() => {{
                    const el = document.getElementById('live-content');
                    if (el) {{
                        el.textContent = {safe_text};
                        window.scrollTo(0, document.body.scrollHeight);
                    }}
                }})();
                """
                self.translation_web_view.page().runJavaScript(js)

            if not self._translation_live_page_ready:
                escaped = html.escape(markdown)
                live_html = f"""
                <!doctype html>
                <html>
                <head>
                <meta charset="utf-8">
                <style>
                    {bundled_reader_font_face_css()}
                    html, body {{
                        margin: 0;
                        min-height: 100%;
                        background: #fbfcfd;
                        color: #1f2933;
                    }}
                    body {{
                        padding: 32px;
                        box-sizing: border-box;
                        font-family: {READER_SERIF_FONT_STACK};
                        line-height: 1.75;
                    }}
                    pre {{
                        white-space: pre-wrap;
                        word-wrap: break-word;
                        font-family: {READER_SERIF_FONT_STACK};
                        font-size: 16px;
                        margin: 0;
                    }}
                </style>
                </head>
                <body>
                    <pre id="live-content">{escaped}</pre>
                    <script>window.scrollTo(0, document.body.scrollHeight);</script>
                </body>
                </html>
                """
                def on_live_page_loaded(ok: bool):
                    try:
                        self.translation_web_view.loadFinished.disconnect(on_live_page_loaded)
                    except Exception:
                        pass
                    if ok:
                        update_live_page()
                        self.apply_reader_font_size()

                self.translation_web_view.loadFinished.connect(on_live_page_loaded)
                self.translation_web_view.setHtml(live_html)
                self._translation_live_page_ready = True
            else:
                update_live_page()
                self.apply_reader_font_size()
        else:
            self.translation_fallback_viewer.setMarkdown(markdown)
            self.apply_reader_font_size()
            self.translation_fallback_viewer.verticalScrollBar().setValue(
                self.translation_fallback_viewer.verticalScrollBar().maximum()
            )

    def show_live_translation_delta(self, delta: str, reset: bool = False):
        """Append a stream fragment without replacing the complete WebEngine page text."""
        delta = str(delta or "")
        if reset:
            self.live_translation_markdown = delta
        else:
            self.live_translation_markdown += delta
        self.translation_button.setEnabled(bool(self.live_translation_markdown.strip()))
        if not self.translation_web_view or not self._translation_live_page_ready:
            self.show_live_translation(self.live_translation_markdown)
            return
        safe_delta = json.dumps(delta, ensure_ascii=False)
        reset_js = "true" if reset else "false"
        self.translation_web_view.page().runJavaScript(
            f"""
            (() => {{
                const el = document.getElementById('live-content');
                if (!el) return;
                if ({reset_js}) el.textContent = {safe_delta};
                else {{
                    let tail = el.lastChild;
                    if (!tail || tail.nodeType !== Node.TEXT_NODE || tail.data.length > 16384) {{
                        tail = document.createTextNode('');
                        el.appendChild(tail);
                    }}
                    tail.appendData({safe_delta});
                }}
                window.scrollTo(0, document.body.scrollHeight);
            }})();
            """
        )

    def reveal_text(self, text: str, pane: str = "", image_src: str = "", page: int | None = None, anchor_ratio: float | None = None, scroll_ratio: float | None = None):
        """Return from a chat reference to the cited content in the reader."""
        text = str(text or "").strip()
        if not text:
            return

        web_views = (self.translation_web_view, self.source_web_view) if pane == "translation" else (self.source_web_view, self.translation_web_view)
        fallback_views = (self.translation_fallback_viewer, self.source_fallback_viewer) if pane == "translation" else (self.source_fallback_viewer, self.translation_fallback_viewer)
        try:
            page_number = int(page) if page is not None else 0
        except (TypeError, ValueError):
            page_number = 0
        try:
            page_ratio = max(0.0, min(1.0, float(anchor_ratio))) if anchor_ratio is not None else None
        except (TypeError, ValueError):
            page_ratio = None
        if page_number > 0:
            page_index = page_number - 1
            ratio_json = json.dumps(page_ratio)
            script = f"""(() => {{ const node = document.querySelector('[data-sync-page-index=\"{page_index}\"]'); if (!node) return; node.scrollIntoView({{block: 'start', behavior: 'auto'}}); const ratio = {ratio_json}; if (ratio !== null) window.scrollBy(0, node.getBoundingClientRect().height * ratio - window.innerHeight * .22); }})();"""
            for web_view in web_views:
                if web_view:
                    web_view.page().runJavaScript(script)
            # 版面还原中的原文是 PDF 页面图像，公式没有可查找的文字节点；
            # 此时“回到第 N 页”就是稳定且可见的定位结果。
            return
        try:
            document_ratio = max(0.0, min(1.0, float(scroll_ratio))) if scroll_ratio is not None else None
        except (TypeError, ValueError):
            document_ratio = None
        if document_ratio is not None:
            ratio_json = json.dumps(document_ratio)
            script = f"""(() => {{ const root = document.scrollingElement || document.documentElement; window.scrollTo(0, (root.scrollHeight - root.clientHeight) * {ratio_json}); }})();"""
            for web_view in web_views:
                if web_view:
                    web_view.page().runJavaScript(script)
            return
        if image_src:
            source_json = json.dumps(image_src, ensure_ascii=False)
            script = f"""(() => {{ const source = {source_json}; const image = [...document.images].find((node) => node.currentSrc === source || node.src === source || node.getAttribute('src') === source); if (image) image.scrollIntoView({{block: 'center', behavior: 'smooth'}}); }})();"""
            for web_view in web_views:
                if web_view:
                    web_view.page().runJavaScript(script)
            return
        if text.startswith("\\"):
            tex_json = json.dumps(text, ensure_ascii=False)
            script = f"""(() => {{ const tex = {tex_json}.replace(/\\s+/g, ''); const node = [...document.querySelectorAll('annotation')].find((item) => item.textContent.replace(/\\s+/g, '') === tex); const host = node && (node.closest('mjx-container') || node.parentElement); if (host) host.scrollIntoView({{block: 'center', behavior: 'smooth'}}); }})();"""
            for web_view in web_views:
                if web_view:
                    web_view.page().runJavaScript(script)
            return
        # WebEngine 优先使用浏览器查找；失败时 fallback QTextBrowser 也尝试查找。
        for web_view in web_views:
            if web_view:
                try:
                    web_view.page().findText("")
                    web_view.page().findText(text)
                    return
                except Exception:
                    pass

        for viewer in fallback_views:
            try:
                cursor = viewer.document().find(text)
                if cursor and not cursor.isNull():
                    viewer.setTextCursor(cursor)
                    viewer.ensureCursorVisible()
                    viewer.setFocus()
                    return
            except Exception:
                pass

    def set_mode(self, mode: str):
        self.both_button.setChecked(mode == "both")
        self.source_button.setChecked(mode == "source")
        self.translation_button.setChecked(mode == "translation")
        self.source_panel.setVisible(mode in {"both", "source"})
        self.translation_panel.setVisible(mode in {"both", "translation"})
        self.set_reader_splitter_mode_sizes(mode)
        self.schedule_preview_refit()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = app_config.load_settings()
        configured_work_dir = str(getattr(self.settings, "work_dir", "") or "").strip()
        self._startup_configuration_required = bool(
            not configured_work_dir or not Path(configured_work_dir).expanduser().exists()
        )
        self.preview_provider = preview_tools.SourcePreviewProvider(WORKSPACE)
        self.worker: MinerUWorker | None = None
        self.translate_worker: TranslateWorker | None = None
        self.source_preview_worker: PreviewRenderWorker | None = None
        self._source_preview_generation = 0
        self.batch_parse_queue: list[Path] = []
        self.running_parse_workers: dict[int, MinerUWorker] = {}
        self.running_parse_sources: dict[int, Path] = {}
        self._batch_parse_total = 0
        self._batch_parse_done = 0
        self._batch_parse_failed = 0
        self._batch_parse_skipped = 0
        self._batch_parse_options: ParseOptions | None = None
        self._batch_parse_wave_index = 0
        # MinerU's public v4 API exposes batch submission but does not publish
        # a per-token concurrent-task quota.  Keep a bounded client pool and
        # let the existing retry/backoff handle a server-side throttle.
        self._batch_parse_wave_size = 10
        self._batch_parse_wave_interval_seconds = 0.0
        self._batch_parse_next_wave_earliest = 0.0
        self._batch_parse_waiting_for_wave = False
        self._batch_parse_timer = QTimer(self)
        self._batch_parse_timer.setSingleShot(True)
        self._batch_parse_timer.timeout.connect(self.dispatch_next_parse_wave)
        self.batch_translate_queue: list[Path] = []
        self.running_translate_workers: dict[int, QThread] = {}
        self.running_translate_sources: dict[int, Path] = {}
        self._batch_translate_total = 0
        self._batch_translate_done = 0
        self._batch_translate_failed = 0
        self._batch_translate_concurrency = 1
        self._batch_layout_translate_concurrency = 1
        self._batch_request_concurrency = 1
        self._batch_translation_layout_mode = False
        self._batch_layout_translate_queue: list[Path] = []
        self._batch_layout_translate_total = 0
        self._batch_layout_translate_done = 0
        self._batch_layout_translate_failed = 0
        self.docs: list[ParsedDoc] = []
        self.current_markdown_path: Path | None = None
        self.current_source_path: Path | None = None
        self._current_document_is_epub = False
        self._detected_layout_body_font_pt: float | None = None
        self.current_original_path: Path | None = None
        self.current_translation_path: Path | None = None
        self.current_layout_translation_path: Path | None = None
        self.live_translation_markdown = ""
        self.live_layout_translation_markdown = ""
        self.live_translation_by_source: dict[str, str] = {}
        self.live_layout_translation_by_source: dict[str, str] = {}
        self.active_translation_source_path: Path | None = None
        self.active_translation_preview_mode = "stream"
        self._translation_live_page_ready = False
        self._translation_live_pending_markdown = ""
        self._syncing_scroll = False
        self._sync_poll_timer = None
        self._sync_poll_inflight = False
        self._sync_poll_generation = 0
        self._last_source_user_scroll_at = 0
        self._last_translation_user_scroll_at = 0
        self._layout_paired_canvas_active = False
        self._source_pdf_active = False
        self._pdf_source_sync_pending = False
        self.source_pdf_view = None
        self._stream_sync_scroll = bool(
            getattr(self.settings, "stream_sync_scroll", self.settings.sync_scroll)
        )
        self._mode_scroll_positions: dict[str, dict[str, float]] = {
            str(key): dict(value)
            for key, value in (getattr(self.settings, "reader_scroll_positions", {}) or {}).items()
            if isinstance(value, dict)
        }
        self._scroll_memory_timer = QTimer(self)
        self._scroll_memory_timer.setInterval(300)
        self._scroll_memory_timer.timeout.connect(self.capture_current_scroll_state)
        # Signal-based scroll tracking: scrollPositionChanged fires synchronously
        # with the exact position — no JS round-trip needed.
        self._scroll_signal_connected_view = None
        self._pending_parse_translation_config: TranslationJobConfig | None = None
        self._pending_parse_layout_mode = False
        self._batch_parse_then_translate = False
        self._batch_parse_translate_layout_mode = False
        self._batch_parse_success_markdowns: list[Path] = []
        self._batch_parse_translation_accepting_sources = False
        self._batch_parse_active_status: dict[str, str] = {}
        self._batch_translate_active_status: dict[str, str] = {}
        self.embedded_chat_window = None
        self._embedded_chat_doc_key = ""
        self.log_messages: list[str] = []
        self._reasoning_log_parts: list[str] = []
        self._reasoning_pending_parts: list[str] = []
        self._reasoning_flush_timer = QTimer(self)
        self._reasoning_flush_timer.setSingleShot(True)
        self._reasoning_flush_timer.timeout.connect(self.flush_reasoning_log_output)
        self._preview_refit_timer = QTimer(self)
        self._preview_refit_timer.setSingleShot(True)
        self._preview_refit_timer.timeout.connect(self.refit_preview_pages)
        self._preview_sync_timer = QTimer(self)
        self._preview_sync_timer.setSingleShot(True)
        self._preview_sync_timer.timeout.connect(self.sync_visible_previews_after_refit)
        self.log_dialog: QDialog | None = None
        self.log_dialog_output: QTextEdit | None = None
        self.batch_progress_panel: BatchProgressPanel | None = None
        self.log_details_toggle: QToolButton | None = None
        self.reasoning_log_output: QTextEdit | None = None
        self.reasoning_toggle_button: QToolButton | None = None
        self.reader_windows: list[ReaderWindow] = []
        self._main_preview_suspended = False
        self.chat_windows: list[QWidget] = []
        self._startup_key_prompt_shown = False
        self._update_check_worker = None
        self._update_download_worker = None
        self._manual_update_check = False
        self._layout_preview_refresh_workers: dict[str, LayoutPreviewRefreshWorker] = {}
        # These overlays exist only above the *visible* reader widgets.  They
        # never modify the generated layout HTML and are therefore excluded
        # from the PDF / Word export inputs by construction.
        self._layout_transition_states: dict[int, dict] = {}
        self._layout_transition_generation = 0
        self._layout_retranslation_notices: dict[int, QWidget] = {}
        self._task_stop_requested = False
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.resize(1280, 820)
        self.apply_window_icon()
        self.setup_ui()
        self._scroll_memory_timer.start()
        QTimer.singleShot(500, self.run_startup_configuration)
        if getattr(sys, "frozen", False) and os.name == "nt":
            QTimer.singleShot(3500, self.check_for_updates)
        self.refresh_docs()

    def run_startup_configuration(self):
        """Show one coherent setup page for a new or unavailable workspace."""
        if self._startup_configuration_required:
            self._startup_key_prompt_shown = True
            self.show_mineru_options_dialog(startup=True)
            return
        self.prompt_for_missing_startup_keys()

    def check_for_updates(self, manual: bool = False) -> None:
        if not manual and not getattr(self.settings, "auto_check_updates", True):
            return
        if self._update_check_worker is not None and self._update_check_worker.isRunning():
            if manual:
                QMessageBox.information(self, "检查更新", "正在检查更新，请稍候。")
            return
        self._manual_update_check = bool(manual)
        self.update_button.setEnabled(False)
        self.update_button.setText("正在检查…")
        use_mirror = bool(getattr(self.settings, "update_mirror_acceleration", True))
        worker = UpdateCheckWorker(use_mirror=use_mirror, parent=self)
        self._update_check_worker = worker
        worker.succeeded.connect(self._on_update_check_succeeded)
        worker.failed.connect(self._on_update_check_failed)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _finish_update_check(self) -> None:
        self.update_button.setEnabled(True)
        self.update_button.setText(f"版本 {APP_VERSION}")
        self._update_check_worker = None

    def _on_update_check_failed(self, message: str) -> None:
        manual = self._manual_update_check
        self._finish_update_check()
        if manual:
            QMessageBox.warning(
                self,
                "检查更新失败",
                f"暂时无法获取更新信息：\n\n{message}\n\n建议您访问 GitHub Releases 页面手动查看最新版本。",
            )

    def _on_update_check_succeeded(self, release: ReleaseInfo) -> None:
        manual = self._manual_update_check
        self._finish_update_check()
        if not is_newer_version(release.version, APP_VERSION):
            if manual:
                QMessageBox.information(self, "检查更新", f"当前已是最新版本：v{APP_VERSION}")
            return
        self.show_update_available_dialog(release, parent=self)

    def show_update_available_dialog(self, release: ReleaseInfo, parent: QWidget | None = None) -> None:
        parent_widget = parent or self
        use_mirror = bool(getattr(self.settings, "update_mirror_acceleration", True))
        dlg = UpdateAvailableDialog(release, use_mirror=use_mirror, parent=parent_widget)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.use_mirror_selected != use_mirror:
                self.settings.update_mirror_acceleration = dlg.use_mirror_selected
                app_config.save_settings(self.settings)
            progress_dlg = UpdateProgressDialog(
                release,
                use_mirror=dlg.use_mirror_selected,
                parent=parent_widget,
            )
            progress_dlg.start_download()

    def schedule_preview_refit(self, *args):
        self.position_layout_transition_overlays()
        if hasattr(self, "_preview_refit_timer"):
            self._preview_refit_timer.start(80)
        else:
            QTimer.singleShot(80, self.refit_preview_pages)
        if hasattr(self, "_preview_sync_timer"):
            self._preview_sync_timer.start(180)
        else:
            QTimer.singleShot(180, self.sync_visible_previews_after_refit)

    def is_suspended_main_preview_target(self, web_view=None) -> bool:
        if not getattr(self, "_main_preview_suspended", False):
            return False
        if web_view is None:
            return True
        return (
            web_view is getattr(self, "source_web_view", None)
            or web_view is getattr(self, "translation_web_view", None)
        )

    def suspend_main_preview_for_reader(self) -> None:
        """Release the main document surfaces while a pure reader owns them.

        The widgets and their signal wiring stay intact. Navigating to a blank
        page releases the large DOM/image trees; generated HTML and fitted
        layout snapshots remain on disk for the normal cached restore path.
        """
        if self.is_suspended_main_preview_target():
            return
        self.capture_current_scroll_state()
        self._main_preview_suspended = True
        self.reset_sync_scroll_runtime()
        release_source_pdf(self)
        self.clear_all_layout_transition_overlays()
        self.clear_all_layout_retranslation_notices()
        self._translation_live_page_ready = False
        self._translation_live_pending_markdown = ""
        for web_view in (
            getattr(self, "source_web_view", None),
            getattr(self, "translation_web_view", None),
        ):
            if not web_view:
                continue
            try:
                web_view.stop()
                web_view.setUrl(QUrl("about:blank"))
                web_view.history().clear()
                web_view._preview_signature_by_url = {}
                web_view._preview_content_signature = None
                QTimer.singleShot(250, lambda view=web_view: self.clear_suspended_preview_history(view))
            except (AttributeError, RuntimeError):
                pass
        for fallback in (
            getattr(self, "source_fallback_viewer", None),
            getattr(self, "translation_fallback_viewer", None),
        ):
            if fallback:
                fallback.clear()
        splitter = getattr(self, "preview_splitter", None)
        notice = getattr(self, "preview_suspended_notice", None)
        if splitter:
            splitter.hide()
        if notice:
            notice.show()

    def clear_suspended_preview_history(self, web_view) -> None:
        """Drop any pre-blank back/forward page retained until navigation ended."""
        if not self.is_suspended_main_preview_target(web_view) or not web_view:
            return
        try:
            if web_view.url().toString() == "about:blank":
                web_view.history().clear()
        except (AttributeError, RuntimeError):
            pass

    def resume_main_preview_after_readers(self) -> None:
        """Restore the current document through the ordinary cached paths."""
        if not self.is_suspended_main_preview_target() or getattr(self, "reader_windows", []):
            return
        self._main_preview_suspended = False
        notice = getattr(self, "preview_suspended_notice", None)
        splitter = getattr(self, "preview_splitter", None)
        if notice:
            notice.hide()
        if splitter:
            splitter.show()
        if getattr(self, "_shutdown_started", False):
            return
        if self.current_source_path:
            self.show_source_preview()
        else:
            self.show_placeholder(self.source_web_view, self.source_fallback_viewer, "请选择左侧解析结果。")
        self.show_current_translation_for_mode()
        self.ensure_current_layout_translation_preview()
        self.update_sync_scroll_availability()
        QTimer.singleShot(650, self.restore_current_scroll_state)
        QTimer.singleShot(1250, self.restore_current_scroll_state)

    def preview_splitter_available_width(self) -> int:
        splitter = getattr(self, "preview_splitter", None)
        if not splitter:
            return 1000
        # Use the actual width available to the preview splitter.
        return max(1, int(splitter.width() or 1000))

    def on_main_splitter_moved(self, _position: int, index: int):
        """主侧栏变化后，延迟均分右侧原文/译文，避免一栏停在最小宽度。"""
        if index == 1 and hasattr(self, "_sidebar_preview_rebalance_timer"):
            self._sidebar_preview_rebalance_timer.start(100)

    def rebalance_preview_after_sidebar_resize(self):
        # 只响应主侧栏手柄；用户直接拖动原文/译文之间的手柄时不会被覆盖。
        self.set_preview_splitter_equal_sizes()
        self.schedule_preview_refit()

    def set_preview_splitter_equal_sizes(self):
        splitter = getattr(self, "preview_splitter", None)
        if not splitter:
            return
        total = self.preview_splitter_available_width()
        left = total // 2
        splitter.setSizes([left, total - left])

    def ensure_preview_splitter_uses_width(self):
        splitter = getattr(self, "preview_splitter", None)
        if not splitter:
            return
        sizes = splitter.sizes()
        if len(sizes) < 2:
            return
        used = sum(max(0, int(size)) for size in sizes)
        total = self.preview_splitter_available_width()
        if used < total * 0.82:
            self.set_preview_splitter_equal_sizes()

    def refit_preview_pages(self):
        script = """
        (() => {
          window.__mineruForcedPageMetrics = null;
          if (window.__mineruFitLayoutPages) window.__mineruFitLayoutPages();
        })();
        """
        for web_view in (getattr(self, "source_web_view", None), getattr(self, "translation_web_view", None)):
            if web_view and web_view.isVisible():
                self._run_sync_javascript(web_view, script)

    def sync_visible_previews_after_refit(self):
        if not getattr(self, "source_panel", None) or not getattr(self, "translation_panel", None):
            return
        if not self.source_panel.isVisible() or not self.translation_panel.isVisible():
            return
        self.sync_translation_to_source_now()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.ensure_preview_splitter_uses_width()
        self.schedule_preview_refit()

    def closeEvent(self, event):
        # _mode_scroll_positions is kept current by scrollPositionChanged signal;
        # just flush it synchronously — no async JS call needed.
        self.settings.reader_scroll_positions = dict(self._mode_scroll_positions)
        app_config.save_settings(self.settings)
        self.remember_open_document()
        self.shutdown_for_application_exit()
        event.accept()

    def shutdown_for_application_exit(self):
        """Release child windows and WebEngine renderers before Qt exits.

        This runs while the Qt event loop is still alive, rather than relying
        on Python's process teardown (which otherwise waits for Chromium's
        renderer timeout on Windows).
        """
        if getattr(self, "_shutdown_started", False):
            return
        self._shutdown_started = True
        self.reset_sync_scroll_runtime()
        self.clear_all_layout_transition_overlays()
        self.clear_all_layout_retranslation_notices()
        # Disconnect scroll signal before shutdown.
        old_wv = getattr(self, '_scroll_signal_connected_view', None)
        if old_wv is not None:
            try:
                old_wv.page().scrollPositionChanged.disconnect(self._on_scroll_position_changed)
            except Exception:
                pass
        for timer_name in ("_batch_parse_timer", "_reasoning_flush_timer", "_preview_refit_timer", "_preview_sync_timer", "_scroll_memory_timer"):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                try:
                    timer.stop()
                except RuntimeError:
                    pass

        # Request cancellation without waiting for network-bound workers here.
        for worker in [
            getattr(self, "worker", None),
            getattr(self, "translate_worker", None),
            getattr(self, "source_preview_worker", None),
            *list(getattr(self, "running_parse_workers", {}).values()),
            *list(getattr(self, "running_translate_workers", {}).values()),
        ]:
            try:
                if worker is not None and worker.isRunning():
                    worker.requestInterruption()
            except (AttributeError, RuntimeError):
                pass

        for reader in list(getattr(self, "reader_windows", [])):
            try:
                reader.close()
                reader.deleteLater()
            except RuntimeError:
                pass
        self.reader_windows.clear()

        chats = [getattr(self, "embedded_chat_window", None), *list(getattr(self, "chat_windows", []))]
        seen_chats = set()
        for chat in chats:
            if chat is None or id(chat) in seen_chats:
                continue
            seen_chats.add(id(chat))
            try:
                shutdown = getattr(chat, "shutdown_for_application_exit", None)
                if callable(shutdown):
                    shutdown()
                chat.close()
                chat.deleteLater()
            except RuntimeError:
                pass
        self.chat_windows.clear()
        self.embedded_chat_window = None

        release_source_pdf(self)
        for attribute in ("source_web_view", "translation_web_view"):
            web_view = getattr(self, attribute, None)
            setattr(self, attribute, None)
            dispose_web_view(web_view)
        for web_view in list(getattr(self, "_pdf_export_views", [])):
            dispose_web_view(web_view)
        self._pdf_export_views = []

        # Do not force-process deleteLater() here.  A chat with many formula
        # bubbles may have many Chromium renderers, and synchronous deletion
        # makes the close button appear frozen for several seconds.  Qt drains
        # these deferred deletes naturally as it leaves the event loop.

    def reset_sync_scroll_runtime(self):
        """清除旧页面同步状态；重翻、切换文档和切换模式时都从干净状态重新安装桥。"""
        self._sync_poll_generation += 1
        self._syncing_scroll = False
        self._sync_poll_inflight = False
        self._last_source_user_scroll_at = 0
        self._last_translation_user_scroll_at = 0
        if self._sync_poll_timer is not None:
            try:
                self._sync_poll_timer.stop()
            except RuntimeError:
                pass

    def _on_sync_web_view_destroyed(self, pane: str):
        self.reset_sync_scroll_runtime()
        setattr(self, f"{pane}_web_view", None)

    def on_sync_view_load_finished(self, ok: bool):
        """页面实际加载完成后重装同步桥，避免重翻后的固定延时早于页面就绪。"""
        if not ok:
            return
        self._sync_poll_inflight = False
        QTimer.singleShot(0, self.install_sync_scroll_bridge)
        QTimer.singleShot(80, self.sync_translation_to_source_now)

    def apply_window_icon(self):
        """设置主窗口图标；失败时只打印告警，不影响程序启动。"""
        try:
            icon_path = os.path.join(get_base_path(), "resources", "icon.ico")
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
            else:
                print(f"警告: 窗口图标文件未找到: {icon_path}")
        except Exception as exc:
            print(f"警告: 设置窗口图标时出错: {exc}")

    def work_dir_path(self) -> Path:
        """获取当前工作文件夹路径，并统一展开为绝对路径。"""
        return app_config.work_dir_path(self.settings).expanduser().resolve()

    def refresh_work_dir_label(self):
        """刷新界面上的工作文件夹提示文本。"""
        if not hasattr(self, "work_dir_path_label"):
            return
        path = self.work_dir_path()
        self.work_dir_path_label.setText(str(path))
        self.work_dir_path_label.setToolTip(str(path))

    def ensure_work_dir_selected(self):
        """
        启动时确保工作文件夹可用。

        如果用户从未选择过工作文件夹，或配置中的路径已经失效，
        这里会引导用户重新选择一次。
        """
        current = self.settings.work_dir.strip()
        if current and Path(current).exists():
            return
        self.choose_work_dir(initial=True)

    def choose_work_dir(self, initial: bool = False):
        """
        重新选择工作文件夹。

        initial=True 表示首次启动时使用，取消选择后回退到默认工作目录；
        initial=False 表示用户手动点击按钮时使用，取消后不修改现有设置。
        """
        old_dir = self.work_dir_path()
        start_dir = old_dir if old_dir.exists() else Path.home()
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择工作文件夹",
            str(start_dir),
            QFileDialog.Option.ShowDirsOnly,
        )

        if not selected:
            if initial:
                selected = str(old_dir)
            else:
                return

        new_dir = Path(selected).expanduser().resolve()
        if new_dir == old_dir:
            self.settings.work_dir = str(new_dir)
            new_dir.mkdir(parents=True, exist_ok=True)
            app_config.save_settings(self.settings)
            self.refresh_work_dir_label()
            return

        # 如果新旧工作目录存在包含关系，自动迁移容易把数据搬进自己内部，
        # 这里仅切换目录，不执行迁移。
        contains_relationship = False
        try:
            contains_relationship = new_dir.is_relative_to(old_dir) or old_dir.is_relative_to(new_dir)
        except Exception:
            old_text = str(old_dir.resolve())
            new_text = str(new_dir.resolve())
            contains_relationship = old_text.startswith(new_text) or new_text.startswith(old_text)

        migrate_old_data = False
        if (
            not initial
            and old_dir.exists()
            and old_dir.is_dir()
            and any(old_dir.iterdir())
        ):
            if contains_relationship:
                QMessageBox.information(
                    self,
                    "无法自动迁移",
                    "新旧工作文件夹存在包含关系，已仅切换目录，不执行迁移。",
                )
            else:
                choice = QMessageBox.question(
                    self,
                    "迁移原数据",
                    "当前工作文件夹中检测到已有解析结果、对话记录或缓存数据。\n\n"
                    f"旧文件夹：{old_dir}\n"
                    f"新文件夹：{new_dir}\n\n"
                    "是否将原数据迁移到新工作文件夹？\n"
                    "选择“是”只会迁移程序生成的数据（解析结果、译文、聊天记录）；"
                    "选择“否”仅切换目录；选择“取消”则放弃此次更改。",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes,
                )
                if choice == QMessageBox.StandardButton.Cancel:
                    return
                migrate_old_data = choice == QMessageBox.StandardButton.Yes

        new_dir.mkdir(parents=True, exist_ok=True)

        current_markdown = getattr(self, "current_markdown_path", None)
        reopened_markdown = None
        if migrate_old_data:
            self.migrate_work_dir_data(old_dir, new_dir)
            reopened_markdown = self.remap_work_dir_path(current_markdown, old_dir, new_dir)

        self.settings.work_dir = str(new_dir)
        app_config.save_settings(self.settings)
        self.refresh_work_dir_label()

        # 如果输入框里已经有当前文件，顺手刷新一下输出目录提示。
        if hasattr(self, "pdf_input") and hasattr(self, "output_label"):
            try:
                selected_file = Path(self.pdf_input.text().strip())
                if selected_file.exists():
                    self.output_label.setText(f"输出目录: {output_dir_for_pdf(selected_file)}")
            except Exception:
                pass

        # 如果当前正在查看的文档已经迁移到新工作文件夹，自动重新打开它。
        if hasattr(self, "doc_list"):
            if reopened_markdown and reopened_markdown.exists():
                self.load_markdown(reopened_markdown)
            self.refresh_docs()

    def remap_work_dir_path(self, path: Path | None, old_dir: Path, new_dir: Path) -> Path | None:
        """把旧工作目录中的文件路径映射到新工作目录。"""
        if not path:
            return None
        try:
            relative = Path(path).resolve().relative_to(old_dir.resolve())
        except Exception:
            return None
        candidate = new_dir.resolve() / relative
        return candidate if candidate.exists() else None

    def migrate_work_dir_data(self, old_dir: Path, new_dir: Path):
        """只迁移程序生成的数据到新目录，避免误搬用户自带文件。"""
        if not old_dir.exists() or old_dir.resolve() == new_dir.resolve():
            return
        for item in old_dir.iterdir():
            if not self.should_migrate_work_dir_item(item):
                continue
            target = new_dir / item.name
            self.move_work_dir_item(item, target)

        # 尽量清理已经空掉的旧目录，避免用户误以为数据还留在旧位置。
        try:
            old_dir.rmdir()
        except OSError:
            pass

    def should_migrate_work_dir_item(self, item: Path) -> bool:
        """只迁移被程序写入过标记的输出目录，以及聊天记录文件。"""
        if item.is_dir():
            return is_generated_output_dir(item)
        return item.name == WORK_DIR_CHAT_HISTORY_NAME

    def move_work_dir_item(self, source: Path, target: Path):
        """移动单个文件或文件夹，必要时自动改名避免覆盖。"""
        if source.resolve() == target.resolve():
            return
        if target.exists():
            target = self.unique_migration_path(target)

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))

    @staticmethod
    def unique_migration_path(target: Path) -> Path:
        """为迁移目标生成不冲突的新名字。"""
        if not target.exists():
            return target

        parent = target.parent
        stem = target.stem
        suffix = target.suffix
        index = 1
        while True:
            candidate = parent / f"{stem}_migrated_{index}{suffix}"
            if not candidate.exists():
                return candidate
            index += 1

    def setup_ui_legacy(self):
        root = QVBoxLayout(self)

        file_row = QHBoxLayout()
        self.pdf_input = QLineEdit()
        self.pdf_input.setPlaceholderText("选择所需翻译的文献")
        file_button = QPushButton("选择文件")
        file_button.clicked.connect(self.select_input_file)
        self.run_button = QPushButton("解析")
        self.run_button.clicked.connect(self.start)
        self.translate_button = QPushButton("翻译当前文档")
        self.translate_button.clicked.connect(self.translate_current_doc)
        file_row.addWidget(QLabel("文件:"))
        file_row.addWidget(self.pdf_input, 1)
        file_row.addWidget(file_button)
        file_row.addWidget(self.run_button)
        file_row.addWidget(self.translate_button)
        root.addLayout(file_row)

        self.output_label = QLabel("输出目录: 未选择")
        root.addWidget(self.output_label)

        self.advanced_group = QGroupBox("解析选项")
        self.advanced_group.setCheckable(True)
        self.advanced_group.setChecked(False)
        advanced_layout = QHBoxLayout(self.advanced_group)
        self.mineru_model_combo = QComboBox()
        self.mineru_model_combo.addItems(["vlm", "pipeline", "MinerU-HTML"])
        self.mineru_model_combo.setEditable(False)
        if self.settings.mineru_model in ["vlm", "pipeline", "MinerU-HTML"]:
            self.mineru_model_combo.setCurrentText(self.settings.mineru_model)
        self.table_check = QCheckBox("识别表格")
        self.table_check.setChecked(True)
        self.formula_check = QCheckBox("识别公式")
        self.formula_check.setChecked(True)
        self.ocr_check = QCheckBox("强制 OCR")
        advanced_layout.addWidget(QLabel("模型:"))
        advanced_layout.addWidget(self.mineru_model_combo)
        advanced_layout.addWidget(self.table_check)
        advanced_layout.addWidget(self.formula_check)
        advanced_layout.addWidget(self.ocr_check)
        advanced_layout.addStretch(1)
        self.advanced_group.toggled.connect(self.set_advanced_visible)
        root.addWidget(self.advanced_group)
        self.set_advanced_visible(False)

        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("已有解析结果"))
        self.doc_list = DocumentListWidget()
        self.doc_list.itemClicked.connect(self.open_doc_item)
        left_layout.addWidget(self.doc_list, 1)
        refresh_button = QPushButton("刷新列表")
        refresh_button.clicked.connect(self.refresh_docs)
        left_layout.addWidget(refresh_button)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.title_label = QLabel("未打开文档")
        right_layout.addWidget(self.title_label)
        preview_splitter = QSplitter()
        preview_splitter.setChildrenCollapsible(False)
        preview_splitter.setOpaqueResize(False)
        preview_splitter.setHandleWidth(10)
        source_panel = QWidget()
        source_panel.setMinimumWidth(360)
        source_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        source_layout = QVBoxLayout(source_panel)
        source_layout.addWidget(QLabel("原文"))
        self.source_fallback_viewer = QTextBrowser()
        self.source_fallback_viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.source_web_view = QWebEngineView() if WEBENGINE_AVAILABLE else None
        if self.source_web_view:
            configure_web_view(self.source_web_view)
            self.source_web_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.install_export_context_menu(self.source_web_view, "source")
            source_layout.addWidget(self.source_web_view, 1)
        else:
            self.install_export_context_menu(self.source_fallback_viewer, "source")
            self.source_fallback_viewer.setOpenExternalLinks(True)
            source_layout.addWidget(self.source_fallback_viewer, 1)

        translation_panel = QWidget()
        translation_panel.setMinimumWidth(360)
        translation_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        translation_layout = QVBoxLayout(translation_panel)
        translation_layout.addWidget(QLabel("译文"))
        self.translation_fallback_viewer = QTextBrowser()
        self.translation_fallback_viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.translation_web_view = QWebEngineView() if WEBENGINE_AVAILABLE else None
        if self.translation_web_view:
            configure_web_view(self.translation_web_view)
            self.translation_web_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.install_export_context_menu(self.translation_web_view, "translation")
            translation_layout.addWidget(self.translation_web_view, 1)
        else:
            self.install_export_context_menu(self.translation_fallback_viewer, "translation")
            self.translation_fallback_viewer.setOpenExternalLinks(True)
            translation_layout.addWidget(self.translation_fallback_viewer, 1)

        preview_splitter.addWidget(source_panel)
        preview_splitter.addWidget(translation_panel)
        preview_splitter.setStretchFactor(0, 1)
        preview_splitter.setStretchFactor(1, 1)
        preview_splitter.setSizes([450, 450])
        preview_splitter.splitterMoved.connect(self.schedule_preview_refit)
        right_layout.addWidget(preview_splitter, 5)
        self.progress = QProgressBar()
        right_layout.addWidget(self.progress)
        right_layout.addWidget(QLabel("运行记录"))
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumHeight(150)
        right_layout.addWidget(self.log_output)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([260, 900])
        root.addWidget(splitter, 1)

        self.mode_label = QLabel("默认使用 MinerU v4 精准解析: vlm 模型，识别表格和公式，普通 PDF 不强制 OCR。")
        root.addWidget(self.mode_label)

    def setup_ui(self):
        self.setWindowTitle(APP_DISPLAY_NAME)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        self.apply_van_gogh_style()

        header = QFrame()
        header.setObjectName("heroPanel")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 10, 14, 10)
        header_layout.setSpacing(10)

        header_layout.addWidget(MonolithMark(36))
        title_block = QVBoxLayout()
        title_block.setSpacing(1)
        app_title = QLabel(APP_DISPLAY_NAME)
        app_title.setObjectName("appTitle")
        app_subtitle = QLabel(APP_SUBTITLE)
        app_subtitle.setObjectName("appSubtitle")
        title_block.addWidget(app_title)
        title_block.addWidget(app_subtitle)
        header_layout.addLayout(title_block, 1)

        self.update_button = QPushButton(f"版本 {APP_VERSION}")
        self.update_button.setObjectName("secondaryButton")
        self.update_button.setToolTip("检查 LitMTrans 更新")
        self.update_button.clicked.connect(lambda: self.check_for_updates(manual=True))
        # 更新仍会在已打包的应用启动后自动检查，但无需在主界面占用空间展示版本号。
        self.update_button.setVisible(False)


        self.layout_reading_mode_button = ModeToggleButton()
        self.layout_reading_mode_button.setObjectName("layoutModeButton")
        self.update_layout_mode_button_text()
        self.layout_reading_mode_button.toggled.connect(self.on_layout_reading_mode_toggled)
        header_layout.addWidget(self.layout_reading_mode_button)

        self.mineru_options_button = QPushButton("模型、服务与工作文件夹")
        self.mineru_options_button.setObjectName("settingsButton")
        self.mineru_options_button.setMinimumWidth(236)
        self.mineru_options_button.setMinimumHeight(42)
        self.mineru_options_button.clicked.connect(self.show_mineru_options_dialog)
        header_layout.addWidget(self.mineru_options_button)

        root.addWidget(header)

        control_panel = QFrame()
        control_panel.setObjectName("controlPanel")
        control_layout = QVBoxLayout(control_panel)
        control_layout.setContentsMargins(14, 12, 14, 12)
        control_layout.setSpacing(10)

        file_row = QHBoxLayout()
        file_row.setSpacing(10)
        self.pdf_input = QLineEdit()
        self.pdf_input.setPlaceholderText("选择所需翻译的文献")
        self.file_button = QPushButton("选择文件并翻译")
        self.file_button.setObjectName("primaryButton")
        self.file_button.clicked.connect(self.select_input_file)
        batch_button = QPushButton("批量处理")
        batch_button.setObjectName("secondaryButton")
        batch_button.clicked.connect(self.show_batch_dialog)
        self.run_button = QPushButton("解析并翻译")
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self.start)
        self.run_button.setVisible(False)
        self.batch_run_button = QPushButton("批量解析")
        self.batch_run_button.setObjectName("secondaryButton")
        self.batch_run_button.clicked.connect(self.start_batch_parse)
        self.translate_button = QPushButton("翻译当前文档")
        self.translate_button.setObjectName("accentButton")
        self.translate_button.clicked.connect(self.translate_current_doc)
        self.translate_button.setVisible(False)
        self.batch_translate_button = QPushButton("批量翻译")
        self.batch_translate_button.setObjectName("secondaryButton")
        self.batch_translate_button.clicked.connect(self.start_batch_translate)
        self.stop_button = QPushButton("停止任务")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.clicked.connect(self.stop_current_task)
        self.stop_button.setVisible(False)
        self.reader_button = QPushButton("专注阅读")
        self.reader_button.setObjectName("primaryButton")
        self.reader_button.clicked.connect(self.open_reader_mode)
        self.export_button = QPushButton("导出")
        self.export_button.setObjectName("secondaryButton")
        self.export_button.clicked.connect(self.show_export_dialog)
        self.view_log_button = QPushButton("运行记录")
        self.view_log_button.setObjectName("secondaryButton")
        self.view_log_button.clicked.connect(lambda: self.show_log_dialog(running=False))
        self.reader_font_spin = QDoubleSpinBox()
        self.reader_font_spin.setDecimals(1)
        self.reader_font_spin.setSingleStep(0.5)
        self.reader_font_spin.setRange(READER_FONT_MIN_PT, READER_FONT_MAX_PT)
        self.reader_font_spin.setValue(self.settings.reader_font_pt)
        self.reader_font_spin.setSuffix(" pt")
        self.reader_font_spin.valueChanged.connect(self.on_reader_font_changed)
        self.reader_font_control = create_reader_font_control(
            self.reader_font_spin,
            "调整双栏阅读字号",
        )
        file_row.addWidget(self.pdf_input, 1)
        file_row.addWidget(self.file_button)
        file_row.addWidget(self.run_button)
        file_row.addWidget(self.translate_button)
        file_row.addWidget(self.reader_button)
        file_row.addWidget(batch_button)
        file_row.addWidget(self.export_button)
        file_row.addWidget(self.view_log_button)
        file_row.addWidget(self.reader_font_control)
        control_layout.addLayout(file_row)

        self.output_label = QLabel("输出目录: 未选择")
        self.output_label.setObjectName("pathHint")

        self.work_dir_path_label = QLabel("")
        self.work_dir_path_label.setObjectName("pathHint")
        self.work_dir_path_label.setWordWrap(True)
        self.work_dir_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.choose_work_dir_button = QPushButton("选择工作文件夹")
        self.choose_work_dir_button.setObjectName("secondaryButton")
        self.choose_work_dir_button.clicked.connect(self.choose_work_dir)
        self.refresh_work_dir_label()

        self.advanced_group = QGroupBox("解析选项")
        self.advanced_group.setVisible(False)
        advanced_layout = QHBoxLayout(self.advanced_group)
        model_label = QLabel("模型")
        model_label.setObjectName("fieldLabel")
        self.mineru_model_combo = QComboBox()
        self.mineru_model_combo.addItems(["vlm", "pipeline", "MinerU-HTML"])
        self.table_check = QCheckBox("识别表格")
        self.table_check.setChecked(True)
        self.formula_check = QCheckBox("识别公式")
        self.formula_check.setChecked(True)
        self.ocr_check = QCheckBox("强制 OCR")
        advanced_layout.addWidget(model_label)
        advanced_layout.addWidget(self.mineru_model_combo)
        advanced_layout.addWidget(self.table_check)
        advanced_layout.addWidget(self.formula_check)
        advanced_layout.addWidget(self.ocr_check)
        advanced_layout.addStretch(1)
        root.addWidget(control_panel)

        splitter = QSplitter()
        splitter.setObjectName("mainSplitter")
        splitter.setChildrenCollapsible(False)
        self.main_splitter = splitter

        left = QWidget()
        left.setObjectName("sideRail")
        # Allow the document-chat sidebar to resize without forcing a legacy width.
        left.setMinimumWidth(450)
        left.setMaximumWidth(660)
        left.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(8)
        self.left_nav_group = QButtonGroup(self)
        self.left_nav_group.setExclusive(True)
        left_nav = QHBoxLayout()
        left_nav.setSpacing(8)
        self.docs_page_button = QPushButton("文献列表")
        self.docs_page_button.setObjectName("secondaryButton")
        self.docs_page_button.setCheckable(True)
        self.docs_page_button.setChecked(True)
        self.ai_page_button = QPushButton("AI")
        self.ai_page_button.setObjectName("secondaryButton")
        self.ai_page_button.setCheckable(True)
        self.left_nav_group.addButton(self.docs_page_button, 0)
        self.left_nav_group.addButton(self.ai_page_button, 1)
        left_nav.addWidget(self.docs_page_button, 1)
        left_nav.addWidget(self.ai_page_button, 1)
        left_layout.addLayout(left_nav)

        self.left_stack = QStackedWidget()

        docs_page = QWidget()
        docs_layout = QVBoxLayout(docs_page)
        docs_layout.setContentsMargins(0, 0, 0, 0)
        docs_layout.setSpacing(8)
        self.doc_list = DocumentListWidget()
        self.doc_list.setObjectName("docList")
        self.doc_list.itemClicked.connect(self.open_doc_item)
        # 仅允许在文献列表内拖动排序，避免拖入外部文件或链接。
        self.doc_list.setDragEnabled(True)
        self.doc_list.viewport().setAcceptDrops(True)
        self.doc_list.setDropIndicatorShown(True)
        self.doc_list.setDragDropOverwriteMode(False)
        self.doc_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.doc_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.doc_list.reordered.connect(self.save_document_list_order)
        self.doc_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.doc_list.customContextMenuRequested.connect(self.show_doc_list_context_menu)
        docs_layout.addWidget(self.doc_list, 1)
        self.ai_sidebar_page = QWidget()
        self.ai_sidebar_layout = QVBoxLayout(self.ai_sidebar_page)
        self.ai_sidebar_layout.setContentsMargins(0, 0, 0, 0)
        self.ai_sidebar_layout.setSpacing(8)
        self.ai_placeholder_label = QLabel("请选择一篇文献后开始对话。")
        self.ai_placeholder_label.setObjectName("pathHint")
        self.ai_placeholder_label.setWordWrap(True)
        self.ai_sidebar_layout.addWidget(self.ai_placeholder_label)
        self.ai_sidebar_layout.addStretch(1)

        self.left_stack.addWidget(docs_page)
        self.left_stack.addWidget(self.ai_sidebar_page)
        left_layout.addWidget(self.left_stack, 1)
        self.left_nav_group.idClicked.connect(self.left_stack.setCurrentIndex)
        self.left_nav_group.idClicked.connect(self.on_left_nav_changed)

        right = QWidget()
        right.setObjectName("workspacePanel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(8)
        self.document_header_widget = QWidget()
        self.document_header_layout = QHBoxLayout(self.document_header_widget)
        self.document_header_layout.setContentsMargins(0, 0, 0, 0)
        self.document_header_layout.setSpacing(10)
        # 当前文档标题可能很长，必须使用自动省略标签，避免撑大主界面。
        self.title_label = ElidedLabel("未打开文档")
        self.title_label.setObjectName("documentTitle")
        self.document_header_layout.addWidget(self.title_label, 1)
        right_layout.addWidget(self.document_header_widget)

        preview_splitter = QSplitter()
        preview_splitter.setObjectName("previewSplitter")
        preview_splitter.setChildrenCollapsible(False)
        preview_splitter.setOpaqueResize(False)
        preview_splitter.setHandleWidth(10)
        self.preview_splitter = preview_splitter
        source_panel = QWidget()
        self.source_panel = source_panel
        source_panel.setMinimumWidth(360)
        source_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        source_layout = QVBoxLayout(source_panel)
        source_layout.setContentsMargins(0, 0, 6, 0)
        source_layout.setSpacing(6)
        self.source_toolbar_widget = QWidget()
        source_toolbar = QHBoxLayout()
        source_toolbar.setContentsMargins(0, 0, 0, 0)
        source_toolbar.setSpacing(8)
        source_label = QLabel("原文")
        source_label.setObjectName("paneTitle")
        self.show_parsed_source_check = QCheckBox("显示解析文件")
        self.show_parsed_source_check.setChecked(self.current_mode_show_parsed_source())
        self.show_parsed_source_check.toggled.connect(self.on_source_preview_mode_changed)
        # Keep the persisted layout options available without exposing them in the toolbar.
        self.show_parsed_source_check.setVisible(not self.settings.layout_reading_mode)
        self.show_layout_restoration_check = QCheckBox("版面还原")
        self.show_layout_restoration_check.setChecked(self.settings.show_layout_restoration)
        self.show_layout_restoration_check.toggled.connect(self.on_layout_restoration_toggled)
        self.show_layout_restoration_check.setVisible(False)
        self.layout_development_check = QCheckBox("版面信息")
        self.layout_development_check.setChecked(self.layout_development_mode_enabled())
        self.layout_development_check.toggled.connect(self.on_layout_development_mode_toggled)
        self.layout_development_check.setVisible(False)
        self.sync_scroll_check = QCheckBox("同步滚动")
        self.sync_scroll_check.setChecked(True if self.settings.layout_reading_mode else self._stream_sync_scroll)
        self.sync_scroll_check.toggled.connect(self.on_sync_scroll_toggled)
        self.sync_scroll_check.setVisible(False)
        source_toolbar.addWidget(source_label)
        source_toolbar.addStretch(1)
        source_toolbar.addWidget(self.show_parsed_source_check)
        source_toolbar.addWidget(self.show_layout_restoration_check)
        source_toolbar.addWidget(self.layout_development_check)
        source_toolbar.addWidget(self.sync_scroll_check)
        self.update_layout_mode_controls()
        self.source_toolbar_widget.setLayout(source_toolbar)
        source_layout.addWidget(self.source_toolbar_widget)
        self.source_fallback_viewer = QTextBrowser()
        self.source_fallback_viewer.setObjectName("readerPane")
        self.source_fallback_viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.source_pdf_view = create_synced_pdf_view(source_panel)
        if self.source_pdf_view:
            source_layout.addWidget(self.source_pdf_view, 1)
            connect_pdf_source_sync(self)
            self.install_export_context_menu(self.source_pdf_view, "source")
        self.source_web_view = QWebEngineView() if WEBENGINE_AVAILABLE else None
        if self.source_web_view:
            configure_web_view(self.source_web_view)
            install_layout_formula_bridge(
                self.source_web_view,
                lambda payload: self.handle_formula_ai_quote("source", payload),
            )
            self.source_web_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.source_web_view.loadFinished.connect(lambda ok: self.apply_reader_font_size() if ok else None)
            self.source_web_view.loadFinished.connect(lambda ok: install_reader_scrollbar_style(self.source_web_view) if ok else None)
            self.source_web_view.loadFinished.connect(lambda ok: install_layout_loading_notice_style(self.source_web_view) if ok else None)
            self.source_web_view.loadFinished.connect(lambda ok: install_layout_image_memory_manager(self.source_web_view) if ok else None)
            self.source_web_view.loadFinished.connect(
                lambda ok: self.schedule_layout_word_state_cache_after_load("source", ok)
            )
            self.source_web_view.loadFinished.connect(lambda ok: ensure_web_view_mathjax_typeset(self.source_web_view) if ok else None)
            self.source_web_view.loadFinished.connect(lambda ok: self.enforce_source_debug_contract() if ok else None)
            self.source_web_view.loadFinished.connect(self.on_sync_view_load_finished)
            self.source_web_view.destroyed.connect(lambda _=None: self._on_sync_web_view_destroyed("source"))
            self.install_export_context_menu(self.source_web_view, "source")
            source_layout.addWidget(self.source_web_view, 1)
        else:
            self.install_export_context_menu(self.source_fallback_viewer, "source")
            self.source_fallback_viewer.setOpenExternalLinks(True)
            source_layout.addWidget(self.source_fallback_viewer, 1)
        set_source_pdf_active(self, False)
        apply_reader_font_to_text_browser(self.source_fallback_viewer, self.settings.reader_font_pt)

        translation_panel = QWidget()
        self.translation_panel = translation_panel
        translation_panel.setMinimumWidth(360)
        translation_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        translation_layout = QVBoxLayout(translation_panel)
        translation_layout.setContentsMargins(6, 0, 0, 0)
        translation_layout.setSpacing(6)
        translation_toolbar = QHBoxLayout()
        translation_toolbar.setContentsMargins(0, 0, 0, 0)
        translation_toolbar.setSpacing(8)
        translation_label = QLabel("译文")
        translation_label.setObjectName("paneTitle")
        translation_toolbar.addWidget(translation_label)
        translation_toolbar.addStretch(1)
        self.translation_toolbar_widget = QWidget()
        self.translation_toolbar_widget.setLayout(translation_toolbar)
        translation_layout.addWidget(self.translation_toolbar_widget)
        self.sync_preview_toolbar_heights()
        self.translation_fallback_viewer = QTextBrowser()
        self.translation_fallback_viewer.setObjectName("readerPane")
        self.translation_fallback_viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.translation_web_view = QWebEngineView() if WEBENGINE_AVAILABLE else None
        if self.translation_web_view:
            configure_web_view(self.translation_web_view)
            install_layout_formula_bridge(
                self.translation_web_view,
                lambda payload: self.handle_formula_ai_quote("translation", payload),
            )
            self.translation_web_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.translation_web_view.loadFinished.connect(lambda ok: self.apply_reader_font_size() if ok else None)
            self.translation_web_view.loadFinished.connect(lambda ok: install_reader_scrollbar_style(self.translation_web_view) if ok else None)
            self.translation_web_view.loadFinished.connect(lambda ok: install_layout_loading_notice_style(self.translation_web_view) if ok else None)
            self.translation_web_view.loadFinished.connect(
                lambda ok: self.schedule_layout_word_state_cache_after_load("translation", ok)
            )
            self.translation_web_view.loadFinished.connect(lambda ok: ensure_web_view_mathjax_typeset(self.translation_web_view) if ok else None)
            self.translation_web_view.loadFinished.connect(lambda ok: install_layout_image_lightbox(self.translation_web_view) if ok else None)
            self.translation_web_view.loadFinished.connect(lambda ok: install_layout_image_memory_manager(self.translation_web_view) if ok else None)
            self.translation_web_view.loadFinished.connect(lambda ok: install_layout_formula_lightbox_compat(self.translation_web_view) if ok else None)
            self.translation_web_view.loadFinished.connect(self.schedule_layout_pdf_cache_after_load)
            self.translation_web_view.loadFinished.connect(self.on_sync_view_load_finished)
            self.translation_web_view.destroyed.connect(lambda _=None: self._on_sync_web_view_destroyed("translation"))
            self.install_export_context_menu(self.translation_web_view, "translation")
            translation_layout.addWidget(self.translation_web_view, 1)
        else:
            self.install_export_context_menu(self.translation_fallback_viewer, "translation")
            self.translation_fallback_viewer.setOpenExternalLinks(True)
            translation_layout.addWidget(self.translation_fallback_viewer, 1)
        apply_reader_font_to_text_browser(self.translation_fallback_viewer, self.settings.reader_font_pt)

        preview_splitter.addWidget(source_panel)
        preview_splitter.addWidget(translation_panel)
        preview_splitter.setStretchFactor(0, 1)
        preview_splitter.setStretchFactor(1, 1)
        self.set_preview_splitter_equal_sizes()
        preview_splitter.splitterMoved.connect(self.schedule_preview_refit)
        QTimer.singleShot(0, self.set_preview_splitter_equal_sizes)
        # The reader-font control has a custom minimum height.  Re-measure once
        # the stylesheet and splitter geometry are live so the two page panes
        # start on exactly the same horizontal line.
        QTimer.singleShot(0, self.sync_preview_toolbar_heights)
        right_layout.addWidget(preview_splitter, 5)
        self.preview_suspended_notice = QLabel(
            "文献已在专注阅读窗口中显示。关闭阅读窗口后，主界面会在这里恢复。"
        )
        self.preview_suspended_notice.setObjectName("readerPane")
        self.preview_suspended_notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_suspended_notice.setWordWrap(True)
        self.preview_suspended_notice.setVisible(False)
        right_layout.addWidget(self.preview_suspended_notice, 5)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        # 解析进度仅在实际解析期间出现；保留一个完成的 100% 条会误导用户
        # 以为后台任务仍未结束。
        self.progress.setVisible(False)
        right_layout.addWidget(self.progress)
        self.log_output = QTextEdit()
        self.log_output.setObjectName("logPane")
        self.log_output.setReadOnly(True)
        self.log_output.setVisible(False)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([520, 1060])
        self._sidebar_preview_rebalance_timer = QTimer(self)
        self._sidebar_preview_rebalance_timer.setSingleShot(True)
        self._sidebar_preview_rebalance_timer.timeout.connect(self.rebalance_preview_after_sidebar_resize)
        splitter.splitterMoved.connect(self.on_main_splitter_moved)
        root.addWidget(splitter, 1)

    def apply_workbench_style(self):
        """Apply the shared application style while keeping document fonts separate."""
        shared = build_dark_premium_stylesheet()
        window_rules = f"""
        QWidget {{
            background: {COLOR_BG_BASE};
        }}

        QFrame#heroPanel {{
            background: {COLOR_BG_SURFACE_2};
            border: 1px solid {COLOR_BORDER_STRONG};
            border-radius: 0px;
        }}
        QLabel#appTitle {{
            color: {COLOR_TEXT_PRIMARY};
            font-family: {APP_DISPLAY_FONT_FAMILY_STACK};
            font-size: 23px;
            font-weight: 500;
            background: transparent;
            letter-spacing: -0.2px;
        }}
        QLabel#appSubtitle {{
            color: {COLOR_TEXT_MUTED};
            background: transparent;
            font-size: 11px;
        }}
        QLabel#pathHint {{
            color: {COLOR_TEXT_MUTED};
            background: transparent;
            font-family: {APP_MONO_FONT_FAMILY_STACK};
            font-size: 10px;
        }}
        QLabel#statusPill {{
            color: {COLOR_TEXT_SECONDARY};
            background: transparent;
            border: none;
            border-left: 1px solid {COLOR_BORDER_HAIR};
            border-radius: 0px;
            padding: 5px 12px;
            font-family: {APP_MONO_FONT_FAMILY_STACK};
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.7px;
        }}

        QFrame#controlPanel {{
            background: {COLOR_BG_SURFACE_2};
            border: 1px solid {COLOR_BORDER_HAIR};
            border-radius: 0px;
        }}
        QWidget#sideRail {{
            background: {COLOR_BG_SURFACE};
            border: 1px solid {COLOR_BORDER_HAIR};
        }}
        QWidget#workspacePanel {{
            background: transparent;
        }}
        QFrame#readerShell, QFrame#cardShell {{
            background: {COLOR_BG_SURFACE_2};
            border: 1px solid {COLOR_BORDER_HAIR};
            border-radius: 0px;
        }}

        QLabel#fieldLabel {{
            color: {COLOR_TEXT_SECONDARY};
            background: transparent;
            font-family: {APP_MONO_FONT_FAMILY_STACK};
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.5px;
        }}
        QLabel#sectionTitle, QLabel#paneTitle {{
            color: {COLOR_TEXT_PRIMARY};
            font-family: {APP_MONO_FONT_FAMILY_STACK};
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.6px;
            background: transparent;
        }}
        QFrame#readerFontControl {{
            min-height: 34px;
            background: {COLOR_BG_SURFACE_2};
            border: 1px solid {COLOR_BORDER_STRONG};
            border-radius: 0px;
        }}
        QFrame#readerFontControl:hover {{
            border-color: {COLOR_ACCENT};
        }}
        QDoubleSpinBox#readerFontSpin {{
            min-height: 32px;
            background: transparent;
            color: {COLOR_TEXT_PRIMARY};
            border: none;
            border-radius: 0px;
            padding: 0px 8px 0px 9px;
            font-family: {APP_MONO_FONT_FAMILY_STACK};
            font-size: 14px;
            font-weight: 700;
        }}
        QDoubleSpinBox#readerFontSpin:hover, QDoubleSpinBox#readerFontSpin:focus {{
            background: {COLOR_ACCENT_SOFT};
        }}
        QWidget#readerFontStepper {{
            background: transparent;
        }}
        QToolButton#readerFontStepButton {{
            min-width: 20px;
            max-width: 20px;
            min-height: 14px;
            max-height: 14px;
            padding: 0px;
            background: {COLOR_BG_INSET};
            color: {COLOR_TEXT_SECONDARY};
            border: 1px solid {COLOR_BORDER_HAIR};
            border-radius: 0px;
        }}
        QToolButton#readerFontStepButton:hover, QToolButton#readerFontStepButton:pressed {{
            background: {COLOR_ACCENT};
            border-color: {COLOR_ACCENT};
        }}
        QLabel#documentTitle {{
            color: {COLOR_TEXT_PRIMARY};
            font-family: {APP_DISPLAY_FONT_FAMILY_STACK};
            font-size: 16px;
            font-weight: 600;
            padding: 3px 0 4px 0;
            background: transparent;
        }}

        QPushButton#layoutModeButton, QPushButton#settingsButton {{
            background: {COLOR_BG_SURFACE_2};
            color: {COLOR_TEXT_PRIMARY};
            border: 1px solid {COLOR_BORDER_STRONG};
            border-radius: 0px;
            font-size: 13px;
            font-weight: 700;
            padding: 7px 14px;
        }}
        QPushButton#layoutModeButton:hover, QPushButton#settingsButton:hover {{
            background: {COLOR_ACCENT};
            color: #FFFFFF;
            border-color: {COLOR_ACCENT};
        }}
        QPushButton#layoutModeButton:checked {{
            background: {COLOR_ACCENT};
            color: #FFFFFF;
            border-color: {COLOR_ACCENT};
        }}

        QPushButton#secondaryButton:checked {{
            background: {COLOR_ACCENT};
            color: #FFFFFF;
            border-color: {COLOR_ACCENT};
        }}
        QListWidget#docList {{
            border-left: none;
            border-right: none;
        }}
        QTextBrowser#readerPane {{
            background: {COLOR_BG_SURFACE_2};
            border: 1px solid {COLOR_BORDER_HAIR};
            border-radius: 0px;
            font-family: "{bundled_reader_qt_font_family()}", {APP_SERIF_FONT_FAMILY_STACK};
        }}
        QTextEdit#logPane {{
            color: {COLOR_TEXT_SECONDARY};
            font-family: {APP_MONO_FONT_FAMILY_STACK};
            font-size: 11px;
            background: {COLOR_BG_SURFACE_2};
            border: 1px solid {COLOR_BORDER_HAIR};
            border-radius: 0px;
        }}

        QSplitter#mainSplitter::handle {{
            background: transparent;
        }}
        QSplitter#mainSplitter::handle:hover {{
            background: {COLOR_ACCENT_SOFT};
        }}
        QSplitter#previewSplitter::handle {{
            background: {COLOR_BORDER_HAIR};
        }}
        QSplitter#previewSplitter::handle:hover {{
            background: {COLOR_ACCENT};
        }}
        """
        self.setStyleSheet(shared + "\n" + window_rules)


    def apply_van_gogh_style(self):
        self.apply_workbench_style()

    def set_advanced_visible(self, visible: bool):
        for child in self.advanced_group.findChildren(QWidget):
            if child is not self.advanced_group:
                child.setVisible(visible)

    def show_mineru_options_dialog(self, _checked: bool = False, *, startup: bool = False) -> bool:
        dialog = QDialog(self)
        dialog.setWindowTitle("模型、服务与工作文件夹")
        dialog.resize(820, 470)
        dialog.setStyleSheet(self.styleSheet())
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        mineru_group = QGroupBox("MinerU 解析")
        mineru_layout = QVBoxLayout(mineru_group)
        mineru_layout.setContentsMargins(10, 12, 10, 10)
        mineru_layout.setSpacing(10)
        mineru_hint = QLabel(
            '<a href="https://mineru.net/apiManage/token">点击这里访问 MinerU 官网创建访问令牌</a>'
        )
        mineru_hint.setObjectName("pathHint")
        mineru_hint.setOpenExternalLinks(True)
        mineru_hint.setToolTip("打开 MinerU 官网")
        mineru_layout.addWidget(mineru_hint)
        row = QHBoxLayout()
        row.setSpacing(8)
        # 始终使用界面当前默认模型；这是实现细节，不要求普通用户选择。
        model_combo = QComboBox()
        model_combo.addItems(["vlm", "pipeline", "MinerU-HTML"])
        model_combo.setEditable(False)
        model_combo.setCurrentText(self.mineru_model_combo.currentText())
        row.addWidget(QLabel("MinerU 访问令牌"))
        mineru_key_input = QLineEdit()
        mineru_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        mineru_key_input.setText(app_config.load_mineru_token())
        row.addWidget(mineru_key_input, 1)
        mineru_layout.addLayout(row)
        # Preserve these values for existing settings while keeping the panel compact.
        table_check = QCheckBox("识别表格")
        formula_check = QCheckBox("识别公式")
        ocr_check = QCheckBox("强制 OCR")
        table_check.setChecked(self.table_check.isChecked())
        formula_check.setChecked(self.formula_check.isChecked())
        ocr_check.setChecked(self.ocr_check.isChecked())
        table_check.setVisible(False)
        formula_check.setVisible(False)
        ocr_check.setVisible(False)
        mineru_layout.addWidget(table_check)
        mineru_layout.addWidget(formula_check)
        mineru_layout.addWidget(ocr_check)
        layout.addWidget(mineru_group)

        work_dir_group = QGroupBox("工作文件夹")
        work_dir_layout = QVBoxLayout(work_dir_group)
        work_dir_layout.setContentsMargins(10, 12, 10, 10)
        work_dir_layout.setSpacing(8)
        work_dir_hint = QLabel("解析结果、译文、缓存和文献对话记录都会保存在当前工作文件夹中。")
        work_dir_hint.setWordWrap(True)
        work_dir_layout.addWidget(work_dir_hint)
        work_dir_row = QHBoxLayout()
        work_dir_path_label = QLabel(str(self.work_dir_path()))
        work_dir_path_label.setObjectName("pathHint")
        work_dir_path_label.setWordWrap(True)
        work_dir_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        choose_work_dir_button = QPushButton("选择工作文件夹")
        work_dir_row.addWidget(work_dir_path_label, 1)
        work_dir_row.addWidget(choose_work_dir_button)
        work_dir_layout.addLayout(work_dir_row)
        layout.addWidget(work_dir_group)

        def choose_dialog_work_dir():
            before = self.work_dir_path()
            self.choose_work_dir()
            after = self.work_dir_path()
            if before != after:
                work_dir_path_label.setText(str(after))

        choose_work_dir_button.clicked.connect(choose_dialog_work_dir)

        translation_group = QGroupBox()
        translation_layout = QVBoxLayout(translation_group)
        translation_layout.setContentsMargins(10, 12, 10, 10)
        translation_layout.setSpacing(10)
        translation_title = QLabel()
        translation_title.setObjectName("sectionTitle")
        translation_title.setOpenExternalLinks(True)
        translation_title.setTextFormat(Qt.TextFormat.RichText)
        translation_layout.addWidget(translation_title)
        translation_status = QLabel("")
        translation_layout.addWidget(translation_status)
        provider_row = QHBoxLayout()
        provider_label = QLabel("服务")
        provider_row.addWidget(provider_label)
        provider_combo = QComboBox()
        provider_combo.setMaximumWidth(170)
        populate_translation_provider_combo(provider_combo)
        saved_provider_index = provider_combo.findData(translation_provider_choice_id(self.settings.ai_provider))
        if saved_provider_index >= 0:
            provider_combo.setCurrentIndex(saved_provider_index)
        provider_row.addWidget(provider_combo)
        base_url_label = QLabel("服务地址")
        provider_row.addWidget(base_url_label)
        base_url_input = QLineEdit()
        base_url_input.setMaximumWidth(200)
        provider_row.addWidget(base_url_input, 3)
        api_key_label = QLabel("API 密钥")
        provider_row.addWidget(api_key_label)
        ai_key_input = QLineEdit()
        ai_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        provider_row.addWidget(ai_key_input, 1)
        translation_layout.addLayout(provider_row)

        model_row = QHBoxLayout()
        model_label = QLabel("模型")
        model_row.addWidget(model_label)
        translation_model_combo = QComboBox()
        translation_model_combo.setEditable(False)
        model_row.addWidget(translation_model_combo, 1)
        refresh_translation_models_button = QPushButton("刷新模型列表")
        model_row.addWidget(refresh_translation_models_button)
        translation_layout.addLayout(model_row)

        deepseek_reasoning_row = QWidget()
        deepseek_reasoning_row.setFixedHeight(34)
        deepseek_reasoning_layout = QHBoxLayout(deepseek_reasoning_row)
        deepseek_reasoning_layout.setContentsMargins(0, 2, 0, 2)
        deepseek_reasoning_layout.setSpacing(8)
        deepseek_reasoning_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        deepseek_reasoning_layout.addWidget(QLabel("DeepSeek 思考:"))
        deepseek_thinking_check = QCheckBox("启用思考")
        deepseek_thinking_check.setToolTip("关闭高速排版后默认开启；使用服务商默认思考强度。")
        deepseek_effort_combo = QComboBox()
        deepseek_effort_combo.addItem("服务商默认", "default")
        deepseek_effort_combo.addItem("高", "high")
        deepseek_effort_combo.addItem("最高", "max")
        style_reasoning_effort_combo(deepseek_effort_combo)
        deepseek_thinking_check.toggled.connect(deepseek_effort_combo.setEnabled)
        deepseek_reasoning_layout.addWidget(deepseek_thinking_check)
        deepseek_reasoning_layout.addWidget(QLabel("等级:"))
        deepseek_reasoning_layout.addWidget(deepseek_effort_combo)
        deepseek_reasoning_layout.addStretch(1)
        translation_layout.addWidget(deepseek_reasoning_row)

        deepseek_fast_layout_check = QCheckBox("DeepSeek 快速排版翻译（仅支持排版模式翻译）")
        deepseek_fast_layout_check.setToolTip(
            "利用官方服务的高缓存命中进行并发请求，费用消耗会加剧。"
            "并自动关闭 DeepSeek 思考。"
        )
        translation_layout.addWidget(deepseek_fast_layout_check)

        gemini_reasoning_row = QWidget()
        gemini_reasoning_row.setFixedHeight(34)
        gemini_reasoning_layout = QHBoxLayout(gemini_reasoning_row)
        gemini_reasoning_layout.setContentsMargins(0, 2, 0, 2)
        gemini_reasoning_layout.setSpacing(8)
        gemini_reasoning_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        gemini_reasoning_layout.addWidget(QLabel("Google 思考:"))
        gemini_thinking_check = QCheckBox("启用思考")
        gemini_thinking_check.setToolTip("启用 Gemini 的思考，并在翻译记录中显示公开摘要。")
        gemini_effort_combo = QComboBox()
        gemini_effort_combo.addItem("低", "low")
        gemini_effort_combo.addItem("中", "medium")
        gemini_effort_combo.addItem("高", "high")
        style_reasoning_effort_combo(gemini_effort_combo)
        gemini_thinking_check.toggled.connect(gemini_effort_combo.setEnabled)
        gemini_reasoning_layout.addWidget(gemini_thinking_check)
        gemini_reasoning_layout.addWidget(QLabel("强度:"))
        gemini_reasoning_layout.addWidget(gemini_effort_combo)
        gemini_reasoning_layout.addStretch(1)
        translation_layout.addWidget(gemini_reasoning_row)

        request_body_button = QPushButton()
        request_body_button.setMinimumHeight(34)
        request_body_modes = {
            "oneapi": normalize_oneapi_request_body_mode(
                getattr(self.settings.providers.get("oneapi"), "request_body_mode", "codex")
            )
        }

        def refresh_request_body_button():
            request_body_button.setVisible(False)

        def edit_request_body_construction():
            return

        request_body_button.clicked.connect(edit_request_body_construction)
        translation_layout.addWidget(request_body_button)
        layout.addWidget(translation_group)

        def fast_layout_available() -> bool:
            return bool(
                provider_combo.currentData() == "deepseek"
                and getattr(self.settings, "layout_reading_mode", False)
            )

        def apply_fast_layout_state():
            fast_enabled = fast_layout_available() and deepseek_fast_layout_check.isChecked()
            if fast_enabled:
                previous = deepseek_thinking_check.blockSignals(True)
                deepseek_thinking_check.setChecked(False)
                deepseek_thinking_check.blockSignals(previous)
            else:
                previous = deepseek_thinking_check.blockSignals(True)
                deepseek_thinking_check.setChecked(True)
                deepseek_thinking_check.blockSignals(previous)
                index = deepseek_effort_combo.findData("default")
                deepseek_effort_combo.setCurrentIndex(index if index >= 0 else 0)
            deepseek_thinking_check.setEnabled(not fast_enabled)
            deepseek_effort_combo.setEnabled(not fast_enabled and deepseek_thinking_check.isChecked())
            deepseek_reasoning_row.setToolTip(
                "高速并发翻译使用无思考请求，已自动关闭思考设置。"
                if fast_enabled else ""
            )

        def load_fast_layout_setting():
            available = fast_layout_available()
            deepseek_fast_layout_check.setVisible(available)
            previous = deepseek_fast_layout_check.blockSignals(True)
            deepseek_fast_layout_check.setChecked(
                available and bool(getattr(self.settings, "translation_deepseek_fast_layout_enabled", True))
            )
            deepseek_fast_layout_check.blockSignals(previous)
            apply_fast_layout_state()

        deepseek_fast_layout_check.toggled.connect(apply_fast_layout_state)

        def load_translation_provider():
            provider_id = provider_combo.currentData() or "zai"
            translation_title.setText(translation_provider_settings_title(provider_id))
            refresh_request_body_button()
            is_deepseek = provider_id == "deepseek"
            is_gemini = provider_id == "gemini"
            deepseek_reasoning_row.setVisible(is_deepseek)
            gemini_reasoning_row.setVisible(is_gemini)
            if is_deepseek:
                deepseek_thinking_check.setChecked(
                    bool(getattr(self.settings, "translation_deepseek_thinking_enabled", True))
                )
                effort_index = deepseek_effort_combo.findData(
                    str(getattr(self.settings, "translation_deepseek_reasoning_effort", "default") or "default")
                )
                deepseek_effort_combo.setCurrentIndex(effort_index if effort_index >= 0 else 0)
                deepseek_effort_combo.setEnabled(deepseek_thinking_check.isChecked())
            load_fast_layout_setting()
            if is_gemini:
                gemini_thinking_check.setChecked(
                    bool(getattr(self.settings, "translation_gemini_thinking_enabled", False))
                )
                effort_index = gemini_effort_combo.findData(
                    str(getattr(self.settings, "translation_gemini_reasoning_effort", "medium") or "medium")
                )
                gemini_effort_combo.setCurrentIndex(effort_index if effort_index >= 0 else 1)
                gemini_effort_combo.setEnabled(gemini_thinking_check.isChecked())
            if machine_translate.is_machine_translation_provider(provider_id):
                is_local = provider_id == machine_translate.MTRAN_SERVER_PROVIDER
                stored_provider = self.settings.providers.get(provider_id)
                ai_key_input.setText(app_config.load_secret(provider_id, "api_key") if is_local else "")
                base_url_input.setText(
                    stored_provider.base_url
                    if stored_provider and stored_provider.base_url
                    else machine_translate.MTRAN_SERVER_DEFAULT_BASE_URL
                    if is_local
                    else ""
                )
                # 机翻服务无需用户配置 LLM 接口或选择模型；隐藏整组控件，
                # 避免在本地和联网免费机翻模式下展示不可用的输入项。
                for widget in (api_key_label, ai_key_input, base_url_label, base_url_input):
                    widget.setVisible(False)
                provider_combo.setMaximumWidth(QWIDGETSIZE_MAX)
                provider_row.setStretch(1, 1)
                provider_row.setStretch(3, 0)
                provider_row.setStretch(5, 0)
                model_label.setVisible(False)
                translation_model_combo.setVisible(False)
                refresh_translation_models_button.setVisible(False)
                translation_model_combo.clear()
                translation_model_combo.addItem(machine_translate.provider_label(provider_id))
                translation_status.setText(
                    "本地免费机翻使用内置 MTranServer 离线翻译，无需配置 API 密钥、服务地址或模型。"
                    if is_local
                    else "Edge 本地翻译在本机运行；首次使用会询问是否下载语言模型。"
                    if provider_id == machine_translate.EDGE_LOCAL_PROVIDER
                    else "联网免费机翻先快速探测 Google，不可达时自动切换到 Bing；无需配置 API 密钥、服务地址或模型。"
                )
                return
            for widget in (api_key_label, ai_key_input, base_url_label, base_url_input):
                widget.setVisible(True)
            provider_combo.setMaximumWidth(170)
            provider_row.setStretch(1, 0)
            provider_row.setStretch(3, 3)
            provider_row.setStretch(5, 1)
            model_label.setVisible(True)
            translation_model_combo.setVisible(True)
            refresh_translation_models_button.setVisible(True)
            stored_provider = self.settings.providers.get(provider_id)
            ai_key_input.setText(app_config.load_secret(provider_id, "api_key"))
            default_url = provider_runtime_default_url(provider_id)
            base_url_input.setText(stored_provider.base_url if stored_provider and stored_provider.base_url else default_url)
            translation_model_combo.clear()
            if stored_provider and stored_provider.model:
                translation_model_combo.addItem(stored_provider.model, stored_provider.model)
                translation_model_combo.setCurrentIndex(0)
            if ai_key_input.text().strip() and base_url_input.text().strip():
                translation_status.setText("正在自动刷新模型列表...")
                QTimer.singleShot(50, refresh_translation_models)
            else:
                translation_status.setText("填写 API 密钥后，点击“刷新模型列表”选择模型。")

        def refresh_translation_models():
            provider_id = provider_combo.currentData() or "zai"
            if machine_translate.is_machine_translation_provider(provider_id):
                translation_status.setText("本地免费机翻使用内置语言包，不需要刷新模型。" if provider_id == machine_translate.MTRAN_SERVER_PROVIDER else "免费机翻不需要刷新模型。")
                return
            api_key = ai_key_input.text().strip()
            base_url = normalize_ai_base_url(base_url_input.text().strip(), provider_id)
            if not api_key or not base_url:
                QMessageBox.warning(dialog, "缺少配置", "请先填写 API 密钥和服务地址。")
                return
            try:
                refresh_translation_models_button.setEnabled(False)
                current_model = str(translation_model_combo.currentData() or translation_model_combo.currentText().strip())
                model_options = fetch_translation_model_options(provider_id, api_key, base_url)
                translation_model_combo.setProperty("provider_id", provider_id)
                preferred = apply_model_options_to_combo(translation_model_combo, model_options, current_model)
                translation_status.setText(f"已加载 {len(model_options)} 个模型")
                if current_model and current_model not in [option.model_id for option in model_options] and preferred:
                    translation_status.setText(
                        f"上次默认模型“{current_model}”已无法访问，已按优先原则改用“{preferred}”。"
                    )
                    QMessageBox.information(
                        dialog,
                        "默认模型已切换",
                        f"上次默认模型“{current_model}”已不在当前模型列表中。\n已自动改用“{preferred}”。",
                    )
            except Exception as exc:
                QMessageBox.critical(dialog, "刷新模型失败", str(exc))
            finally:
                refresh_translation_models_button.setEnabled(True)

        provider_combo.currentIndexChanged.connect(load_translation_provider)
        refresh_translation_models_button.clicked.connect(refresh_translation_models)
        load_translation_provider()

        about_group = QGroupBox("关于与软件更新")
        about_layout = QVBoxLayout(about_group)
        about_layout.setContentsMargins(10, 12, 10, 10)
        about_layout.setSpacing(8)

        version_row = QHBoxLayout()
        version_label = QLabel(f"当前版本：<b>v{APP_VERSION}</b>（开源版）")
        version_row.addWidget(version_label)

        check_update_btn = QPushButton("检查更新")
        check_update_btn.setMaximumWidth(120)
        version_row.addWidget(check_update_btn)

        update_status_label = QLabel("")
        update_status_label.setObjectName("pathHint")
        version_row.addWidget(update_status_label, 1)
        about_layout.addLayout(version_row)

        options_row = QHBoxLayout()
        auto_update_check = QCheckBox("启动时自动检查更新")
        auto_update_check.setChecked(bool(getattr(self.settings, "auto_check_updates", True)))
        options_row.addWidget(auto_update_check)

        mirror_accel_check = QCheckBox("启用国内镜像加速下载 (推荐)")
        mirror_accel_check.setChecked(bool(getattr(self.settings, "update_mirror_acceleration", True)))
        mirror_accel_check.hide()
        options_row.addWidget(mirror_accel_check)
        options_row.addStretch(1)
        about_layout.addLayout(options_row)

        links_row = QHBoxLayout()
        links_label = QLabel(
            f'<a href="{GITHUB_REPO_URL}">GitHub 开源仓库</a> &nbsp;|&nbsp; '
            f'<a href="{GITHUB_RELEASES_URL}">版本发布与更新日志</a> &nbsp;|&nbsp; '
            f'<a href="{GITHUB_ISSUES_URL}">反馈问题与建议</a>'
        )
        links_label.setOpenExternalLinks(True)
        links_label.setObjectName("pathHint")
        links_row.addWidget(links_label)
        links_row.addStretch(1)
        about_layout.addLayout(links_row)

        layout.addWidget(about_group)

        def run_manual_update_check():
            check_update_btn.setEnabled(False)
            check_update_btn.setText("正在检查…")
            update_status_label.setText("正在连接更新服务器…")

            def on_check_succeeded(release):
                check_update_btn.setEnabled(True)
                check_update_btn.setText("检查更新")
                if not is_newer_version(release.version, APP_VERSION):
                    update_status_label.setText(f"当前已是最新版本 (v{APP_VERSION})")
                    QMessageBox.information(dialog, "检查更新", f"当前已是最新版本：v{APP_VERSION}")
                    return
                update_status_label.setText(f"发现新版本 v{release.version}！")
                self.show_update_available_dialog(release, parent=dialog)

            def on_check_failed(msg):
                check_update_btn.setEnabled(True)
                check_update_btn.setText("检查更新")
                update_status_label.setText("检查更新失败")
                QMessageBox.warning(
                    dialog,
                    "检查更新失败",
                    f"暂时无法获取更新信息：\n\n{msg}\n\n建议您直接访问 GitHub Releases 页面查看。",
                )

            worker = UpdateCheckWorker(use_mirror=mirror_accel_check.isChecked(), parent=dialog)
            worker.succeeded.connect(on_check_succeeded)
            worker.failed.connect(on_check_failed)
            worker.finished.connect(worker.deleteLater)
            worker.start()

        check_update_btn.clicked.connect(run_manual_update_check)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        if startup:
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("稍后设置")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        guide_link = QLabel('<a href="guide">打开令牌创建指南</a>')
        guide_link.setObjectName("tokenGuideLink")
        guide_link.setTextFormat(Qt.TextFormat.RichText)
        guide_link.setToolTip("使用系统默认 PDF 阅读器打开令牌创建指南")

        def open_token_guide(_link: str = ""):
            guide_path = Path(get_base_path()) / "resources" / "指南.pdf"
            if not guide_path.is_file():
                QMessageBox.warning(dialog, "无法打开指南", f"未找到指南文件：\n{guide_path}")
                return
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(guide_path.resolve()))):
                QMessageBox.warning(dialog, "无法打开指南", "系统没有可用的 PDF 打开程序。")

        guide_link.linkActivated.connect(open_token_guide)
        footer_layout = QHBoxLayout()
        footer_layout.addWidget(guide_link)
        footer_layout.addStretch(1)
        footer_layout.addWidget(buttons)
        layout.addLayout(footer_layout)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings.auto_check_updates = auto_update_check.isChecked()
            self.settings.update_mirror_acceleration = mirror_accel_check.isChecked()
            self.mineru_model_combo.setCurrentText(model_combo.currentText())
            self.table_check.setChecked(table_check.isChecked())
            self.formula_check.setChecked(formula_check.isChecked())
            self.ocr_check.setChecked(ocr_check.isChecked())
            self.settings.mineru_model = model_combo.currentText()
            if not str(getattr(self.settings, "work_dir", "") or "").strip():
                default_work_dir = self.work_dir_path()
                default_work_dir.mkdir(parents=True, exist_ok=True)
                self.settings.work_dir = str(default_work_dir)
                self.refresh_work_dir_label()
            mineru_key = mineru_key_input.text().strip().removeprefix("Bearer ").strip()
            if mineru_key:
                app_config.save_mineru_token(mineru_key)
            provider_id = provider_combo.currentData() or "zai"
            self.settings.ai_provider = provider_id
            if provider_id == "deepseek":
                fast_enabled = fast_layout_available() and deepseek_fast_layout_check.isChecked()
                self.settings.translation_deepseek_thinking_enabled = (
                    deepseek_thinking_check.isChecked() and not fast_enabled
                )
                self.settings.translation_deepseek_reasoning_effort = str(deepseek_effort_combo.currentData() or "default")
                if bool(getattr(self.settings, "layout_reading_mode", False)):
                    self.settings.translation_deepseek_fast_layout_enabled = fast_enabled
            elif provider_id == "gemini":
                self.settings.translation_gemini_thinking_enabled = gemini_thinking_check.isChecked()
                self.settings.translation_gemini_reasoning_effort = str(gemini_effort_combo.currentData() or "medium")
            if machine_translate.is_machine_translation_provider(provider_id):
                is_local = provider_id == machine_translate.MTRAN_SERVER_PROVIDER
                self.settings.providers[provider_id] = app_config.ProviderSettings(
                    provider_id=provider_id,
                    base_url=base_url_input.text().strip() or (machine_translate.MTRAN_SERVER_DEFAULT_BASE_URL if is_local else ""),
                    model=machine_translate.provider_label(provider_id),
                )
                if is_local:
                    save_secret_with_session_fallback(self, provider_id, "api_key", ai_key_input.text().strip().removeprefix("Bearer ").strip())
                app_config.save_settings(self.settings)
                return True
            base_url = normalize_ai_base_url(base_url_input.text().strip(), provider_id)
            self.settings.providers[provider_id] = app_config.ProviderSettings(
                provider_id=provider_id,
                base_url=base_url,
                model=str(translation_model_combo.currentData() or translation_model_combo.currentText().strip()),
                request_body_mode=request_body_modes["oneapi"] if provider_id == "oneapi" else "codex",
            )
            ai_key = ai_key_input.text().strip().removeprefix("Bearer ").strip()
            if ai_key:
                save_secret_with_session_fallback(self, provider_id, "api_key", ai_key)
            app_config.save_settings(self.settings)
            return True
        return False

    def configure_mineru_api_key(self):
        current = app_config.load_mineru_token()
        token, ok = QInputDialog.getText(
            self,
            "设置 MinerU 访问令牌",
            "请输入 MinerU 访问令牌：",
            QLineEdit.EchoMode.Password,
            current,
        )
        if not ok:
            return
        token = token.strip().removeprefix("Bearer ").strip()
        if not token:
            QMessageBox.information(self, "未保存", "MinerU 访问令牌不能为空。")
            return
        if not token.isascii() or any(ch.isspace() for ch in token):
            QMessageBox.warning(self, "格式不正确", "MinerU 访问令牌只能包含无空格的 ASCII 字符。")
            return
        persisted = save_secret_with_session_fallback(self, "mineru", "api_key", token)
        if persisted:
            QMessageBox.information(self, "已保存", f"MinerU 访问令牌已保存到用户配置目录：\n{app_config.secret_path('mineru', 'api_key')}")

    def current_ai_key_available(self) -> bool:
        provider_id = self.settings.ai_provider or "zai"
        if machine_translate.is_machine_translation_provider(provider_id):
            return True
        return bool(load_provider_secret(provider_id))

    def mineru_key_available(self) -> bool:
        try:
            return bool(load_mineru_token())
        except MinerUError:
            return False

    def prompt_for_missing_startup_keys(self):
        if self._startup_key_prompt_shown:
            return
        self._startup_key_prompt_shown = True

        missing_mineru = not self.mineru_key_available()
        if not missing_mineru:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("初始设置")
        dialog.resize(520, 220)
        layout = QVBoxLayout(dialog)
        hint = QLabel("MinerU 访问令牌用于文献解析。翻译和文献对话服务可按需配置，首次使用时再填写即可。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        mineru_key_input = None
        if missing_mineru:
            row = QHBoxLayout()
            row.addWidget(QLabel("MinerU 访问令牌："))
            mineru_key_input = QLineEdit()
            mineru_key_input.setEchoMode(QLineEdit.EchoMode.Password)
            mineru_key_input.setPlaceholderText("请输入 MinerU 访问令牌")
            row.addWidget(mineru_key_input, 1)
            layout.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def save_keys():
            saved = False
            session_only = False
            if mineru_key_input is not None:
                mineru_key = mineru_key_input.text().strip().removeprefix("Bearer ").strip()
                if mineru_key:
                    session_only = not save_secret_with_session_fallback(dialog, "mineru", "api_key", mineru_key)
                    saved = True
            if saved and not session_only:
                QMessageBox.information(dialog, "已保存", f"访问凭据已保存到用户配置目录：\n{app_config.APP_DIR}")
            dialog.accept()

        buttons.accepted.connect(save_keys)
        dialog.exec()

    def show_batch_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("批量处理")
        dialog.resize(620, 420)
        layout = QVBoxLayout(dialog)
        hint = QLabel("选择多个文件后可批量解析；批量翻译会处理左侧列表中尚未翻译的解析结果。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        queue_list = QListWidget()
        for path in self.batch_parse_queue:
            queue_list.addItem(str(path))
        layout.addWidget(queue_list, 1)
        buttons_row = QHBoxLayout()
        choose_button = QPushButton("选择文件")
        parse_button = QPushButton("开始批量解析")
        parse_translate_button = QPushButton("批量解析并翻译")
        translate_button = QPushButton("批量翻译未译文档")
        close_button = QPushButton("关闭")
        buttons_row.addWidget(choose_button)
        buttons_row.addWidget(parse_button)
        buttons_row.addWidget(parse_translate_button)
        buttons_row.addWidget(translate_button)
        buttons_row.addStretch(1)
        buttons_row.addWidget(close_button)
        layout.addLayout(buttons_row)

        def choose_files():
            self.select_batch_files()
            queue_list.clear()
            for path in self.batch_parse_queue:
                queue_list.addItem(str(path))

        choose_button.clicked.connect(choose_files)
        parse_button.clicked.connect(lambda: (dialog.accept(), self.start_batch_parse()))
        parse_translate_button.clicked.connect(lambda: (dialog.accept(), self.start_batch_parse_then_translate()))
        translate_button.clicked.connect(lambda: (dialog.accept(), self.start_batch_translate()))
        close_button.clicked.connect(dialog.reject)
        dialog.exec()

    def show_log_dialog(self, running: bool, show_reasoning: bool = False):
        if self.log_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("运行记录")
            dialog.resize(760, 430)
            layout = QVBoxLayout(dialog)
            progress_panel = BatchProgressPanel()
            progress_panel.setVisible(False)
            layout.addWidget(progress_panel)

            details_toggle = QToolButton()
            details_toggle.setText("技术详情")
            details_toggle.setCheckable(True)
            details_toggle.setArrowType(Qt.ArrowType.RightArrow)
            details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            details_toggle.setVisible(False)
            layout.addWidget(details_toggle)
            output = QTextEdit()
            output.setReadOnly(True)
            output.setObjectName("logPane")
            output.setPlainText("\n".join(self.log_messages))
            layout.addWidget(output, 1)

            def toggle_details(checked: bool):
                details_toggle.setArrowType(
                    Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
                )
                output.setVisible(checked or not progress_panel.isVisible())

            details_toggle.toggled.connect(toggle_details)
            reasoning_button = QToolButton()
            reasoning_button.setText("翻译思考过程")
            reasoning_button.setCheckable(True)
            # 只有翻译任务才显示思考过程；解析任务不显示该区域。
            reasoning_button.setChecked(show_reasoning)
            reasoning_button.setArrowType(Qt.ArrowType.DownArrow if show_reasoning else Qt.ArrowType.RightArrow)
            reasoning_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            reasoning_button.setVisible(show_reasoning)
            reasoning_output = QTextEdit()
            reasoning_output.setReadOnly(True)
            reasoning_output.setObjectName("logPane")
            reasoning_output.setPlainText("".join(self._reasoning_log_parts))
            reasoning_output.setVisible(show_reasoning)

            def toggle_reasoning(checked: bool):
                reasoning_button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
                reasoning_output.setVisible(checked)

            reasoning_button.toggled.connect(toggle_reasoning)
            toggle_reasoning(show_reasoning)
            layout.addWidget(reasoning_button)
            layout.addWidget(reasoning_output, 1)
            row = QHBoxLayout()
            self.log_stop_button = QPushButton("终止任务")
            self.log_stop_button.setObjectName("dangerButton")
            self.log_stop_button.clicked.connect(self.stop_current_task)
            close_button = QPushButton("关闭")
            close_button.clicked.connect(dialog.close)
            row.addWidget(self.log_stop_button)
            row.addStretch(1)
            row.addWidget(close_button)
            layout.addLayout(row)
            self.log_dialog = dialog
            self.log_dialog_output = output
            self.batch_progress_panel = progress_panel
            self.log_details_toggle = details_toggle
            self.reasoning_toggle_button = reasoning_button
            self.reasoning_log_output = reasoning_output
            dialog.finished.connect(self.on_log_dialog_closed)
        self.log_dialog.setWindowTitle("运行记录")
        self.log_stop_button.setVisible(running)
        self.log_dialog_output.setPlainText("\n".join(self.log_messages))
        if self.reasoning_toggle_button:
            self.reasoning_toggle_button.setVisible(show_reasoning)
            self.reasoning_toggle_button.setChecked(show_reasoning)
        if self.reasoning_log_output:
            self.reasoning_log_output.setPlainText("".join(self._reasoning_log_parts))
            self._reasoning_pending_parts.clear()
            self.reasoning_log_output.setVisible(show_reasoning)
        self.log_dialog_output.verticalScrollBar().setValue(self.log_dialog_output.verticalScrollBar().maximum())
        self.log_dialog.show()
        if running:
            self.log_dialog.raise_()

    def on_log_dialog_closed(self):
        self.log_dialog = None
        self.log_dialog_output = None
        self.batch_progress_panel = None
        self.log_details_toggle = None
        self.reasoning_toggle_button = None
        self.reasoning_log_output = None

    def set_batch_progress_visible(self, visible: bool) -> None:
        """Switch the existing run-record view between summary and raw detail."""
        if self.batch_progress_panel is None or self.log_dialog_output is None:
            return
        self.batch_progress_panel.setVisible(visible)
        if self.log_details_toggle:
            self.log_details_toggle.setVisible(visible)
            self.log_details_toggle.setChecked(False)
        self.log_dialog_output.setVisible(not visible)

    def update_batch_progress_panel(self) -> None:
        if self.batch_progress_panel is None:
            return
        self.batch_progress_panel.update_state(
            parse_done=self._batch_parse_done,
            parse_total=self._batch_parse_total,
            parse_failed=self._batch_parse_failed,
            parse_skipped=self._batch_parse_skipped,
            parse_active=self._batch_parse_active_status,
            translate_done=self._batch_translate_done,
            translate_total=self._batch_translate_total,
            translate_failed=self._batch_translate_failed,
            translate_active=self._batch_translate_active_status,
            translation_enabled=bool(
                self._batch_parse_then_translate
                or self._batch_parse_translation_accepting_sources
                or self._batch_translate_total
            ),
        )

    def begin_task_ui(self, show_reasoning: bool = False):
        self._task_stop_requested = False
        self.stop_button.setVisible(False)
        self.show_log_dialog(running=True, show_reasoning=show_reasoning)

    def finish_task_ui(self):
        if self.log_dialog:
            self.log_dialog.close()

    def reset_translation_task_state(self):
        self.batch_translate_queue = []
        self._batch_layout_translate_queue = []
        self.running_translate_workers = {}
        self.running_translate_sources = {}
        self._batch_translate_total = 0
        self._batch_translate_done = 0
        self._batch_translate_failed = 0
        self._batch_translation_layout_mode = False
        self._batch_layout_translate_total = 0
        self._batch_layout_translate_done = 0
        self._batch_layout_translate_failed = 0
        self._batch_request_concurrency = 1
        self._continue_batch_translate = False
        self._continue_batch_layout_translate = False
        self.active_translation_source_path = None
        self.active_translation_preview_mode = "stream"
        self.translate_button.setEnabled(True)
        self.batch_translate_button.setEnabled(True)

    def configured_batch_concurrency(self, provider_id: str = "") -> int:
        """读取批量预算；MinerU 默认使用安全的 10 路池。"""
        if str(provider_id or "").strip().lower() == "deepseek":
            return DEEPSEEK_TRANSLATION_REQUEST_CONCURRENCY

        try:
            value = int(getattr(self.settings, "batch_concurrency", 1) or 1)
        except (TypeError, ValueError):
            value = 1

        # 旧版本把默认值持久化为 1，导致 MinerU 实际退化为逐个文件加 60 秒
        # 的人工节流。公开文档未承诺固定的 token 并发数，因此采用已有的 10
        # 路客户端上限；服务端限流仍由请求层的 Retry-After/退避处理。
        if not provider_id and value <= 1:
            value = 10

        # 未指定服务商时调用者是 MinerU 批量解析，不能随 DeepSeek 提高到 100。
        return max(1, min(10, value))

    def has_active_parse_task(self) -> bool:
        return bool(
            self.is_thread_running(self.worker)
            or self.running_parse_workers
            or self._batch_parse_waiting_for_wave
        )

    def has_active_translation_task(self) -> bool:
        return bool(
            self.is_thread_running(self.translate_worker)
            or self.running_translate_workers
        )

    def reject_new_processing_task(self, task_name: str, allow_parse_handoff: bool = False) -> bool:
        """所有解析、翻译入口共用同一互斥检查。"""
        parse_busy = self.has_active_parse_task()
        translate_busy = self.has_active_translation_task()
        if translate_busy or (parse_busy and not allow_parse_handoff):
            QMessageBox.information(
                self,
                "任务进行中",
                f"当前已有解析或翻译任务运行，无法开始{task_name}。请等待任务结束或点击停止任务。",
            )
            return True
        return False

    def normalized_unique_batch_paths(self, paths: list[Path]) -> list[Path]:
        """按规范化绝对路径去重，避免同一文件在一个批次中被重复派发。"""
        result: list[Path] = []
        seen: set[str] = set()
        for raw_path in paths:
            path = Path(raw_path)
            if not path.exists() or not is_supported_input_file(path):
                continue
            try:
                key = os.path.normcase(str(path.resolve()))
            except OSError:
                key = os.path.normcase(str(path.absolute()))
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
        return result

    def effective_batch_translation_concurrency(
        self,
        job_config: TranslationJobConfig,
        sources: list[Path],
    ) -> tuple[int, int]:
        """拆分文档并发和单文档内部并发，确保二者乘积不超过总请求预算。"""
        provider_id = str(
            job_config.ai_config.provider_id or ""
        ).strip().lower()
        budget = self.configured_batch_concurrency(provider_id)

        if is_free_machine_translation_config(job_config.ai_config):
            # 免费机翻已有独立的本地并行参数，批量层只处理一篇文档。
            return 1, 1

        uses_chunked_mode = (
            str(job_config.mode or "").strip().lower() in {"chunked", "chunks"}
            or any(self.is_long_pdf_source(source) for source in sources)
        )

        if not uses_chunked_mode:
            # 全文连续模式每篇文档只有一个模型请求，因此预算全部用于文档并发。
            return min(budget, max(1, len(sources))), 1

        # 使用接近平方根的均衡拆分：
        # 文档较少时把更多请求槽分配给单篇文档的分块；
        # 文档较多时同时推进多篇，且 document * inner 始终不超过 budget。
        document_concurrency = max(
            1,
            min(
                len(sources),
                max(1, int(budget ** 0.5)),
            ),
        )
        inner_budget = max(1, budget // document_concurrency)
        inner_concurrency = normalize_translation_request_concurrency(
            provider_id,
            inner_budget,
        )
        return document_concurrency, inner_concurrency

    def task_stop_requested(self) -> bool:
        return self._task_stop_requested

    def select_input_file(self):
        file_filter = (
            "支持的文献 (*.epub *.pdf *.png *.jpg *.jpeg *.jp2 *.webp *.gif *.bmp "
            "*.doc *.docx *.ppt *.pptx *.xls *.xlsx *.html *.htm);;EPUB 电子书 (*.epub);;All Files (*)"
        )
        file_path, _ = QFileDialog.getOpenFileName(self, "选择要解析的文件", "", file_filter)
        if not file_path:
            return
        self.pdf_input.setText(file_path)
        input_path = Path(file_path)
        existing_dir = latest_output_dir_for_file(input_path)
        out_dir = output_dir_for_pdf(input_path)
        self.output_label.setText(f"新解析输出目录: {out_dir}")
        existing = (existing_dir / "full.cleaned.md") if existing_dir else None
        if existing and existing.exists():
            self.load_markdown(existing)
            action = self.choose_existing_parsed_document_action()
            if action == "retranslate":
                self.append_log(f"检测到已有解析结果，直接开始重新翻译：{existing_dir.name}。")
                self.confirm_retranslate_current_document()
                return
            if action != "reparse":
                self.append_log("已取消操作。")
                return
            self.append_log(f"已选择重新解析并翻译：{existing_dir.name}。")
            # The user has already explicitly chosen re-parsing in the dialog
            # above, so do not ask the legacy duplicate-parse question again.
            self.start(skip_duplicate_confirmation=True)
            return
        # 文件选择是单文件工作流的主操作：选定有效文件后，直接复用原有的
        # “解析并翻译”流程（包括重复解析、密钥和翻译选项确认）。
        self.start()

    def choose_existing_parsed_document_action(self) -> str:
        """Ask how to handle a source file that already has parsed Markdown.

        Re-translation is deliberately the first/default action: it is the
        common intent for selecting a document that is already in the library.
        """
        dialog = QMessageBox(self)
        dialog.setWindowTitle("检测到已解析文档")
        dialog.setIcon(QMessageBox.Icon.Question)
        # Reserve enough horizontal room for the three Chinese action labels;
        # QMessageBox otherwise compresses ActionRole buttons on Windows.
        dialog.setFixedWidth(600)
        dialog.setText("该文件已经解析过。")
        dialog.setInformativeText(
            "请选择接下来的操作。\n\n"
            "重新解析并翻译会请求MinerU重新解析文献后再重新翻译。"
        )
        retranslate_button = dialog.addButton("重新翻译", QMessageBox.ButtonRole.AcceptRole)
        retranslate_button.setObjectName("primaryButton")
        reparse_button = dialog.addButton("重新解析并翻译", QMessageBox.ButtonRole.ActionRole)
        reparse_button.setMinimumWidth(160)
        cancel_button = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(retranslate_button)
        dialog.setEscapeButton(cancel_button)
        dialog.exec()
        if dialog.clickedButton() is retranslate_button:
            return "retranslate"
        if dialog.clickedButton() is reparse_button:
            return "reparse"
        return "cancel"

    def select_batch_files(self):
        file_filter = (
            "支持的文献 (*.epub *.pdf *.png *.jpg *.jpeg *.jp2 *.webp *.gif *.bmp "
            "*.doc *.docx *.ppt *.pptx *.xls *.xlsx *.html *.htm);;EPUB 电子书 (*.epub);;All Files (*)"
        )
        files, _ = QFileDialog.getOpenFileNames(self, "选择批量解析文件", "", file_filter)
        self.batch_parse_queue = [Path(item) for item in files if item and is_supported_input_file(Path(item))]
        if self.batch_parse_queue:
            self.pdf_input.setText(str(self.batch_parse_queue[0]))
            self.output_label.setText(f"批量队列: {len(self.batch_parse_queue)} 个文件，首个输出目录 {output_dir_for_pdf(self.batch_parse_queue[0])}")
            self.append_log(f"已加入批量解析队列：共 {len(self.batch_parse_queue)} 个文件")

    def confirm_duplicate_parse(self, pdf_path: Path) -> bool:
        existing_dir = latest_output_dir_for_file(pdf_path)
        if not existing_dir:
            return True
        choice = QMessageBox.question(
            self,
            "检测到同名解析结果",
            f"已解析过同名文档:\n{existing_dir}\n\n是否继续解析？继续后会写入编号目录，例如 {output_dir_for_pdf(pdf_path).name}。",
        )
        return choice == QMessageBox.StandardButton.Yes

    def start_batch_parse(self, preserve_parse_translate: bool = False):
        if not preserve_parse_translate:
            self._batch_parse_then_translate = False
            self._batch_parse_success_markdowns = []
        if self.reject_new_processing_task("批量解析"):
            return
        if not self.batch_parse_queue:
            text = self.pdf_input.text().strip()
            if text:
                self.batch_parse_queue = [Path(text)]
        self.batch_parse_queue = self.normalized_unique_batch_paths(self.batch_parse_queue)
        if not self.batch_parse_queue:
            QMessageBox.information(self, "队列为空", "请先点击“批量选择”添加文件。")
            return
        if any(input_requires_mineru(path) for path in self.batch_parse_queue):
            try:
                load_mineru_token()
            except MinerUError:
                self.configure_mineru_api_key()
                try:
                    load_mineru_token()
                except MinerUError:
                    QMessageBox.information(self, "需要 MinerU 访问令牌", "批次中含有需要 MinerU 的文件，请先设置访问令牌。")
                    return

        # 批量开始时一次性冻结解析参数，避免用户在等待下一批期间修改界面，
        # 导致同一批文档使用不同模型、OCR、表格或公式设置。
        self._batch_parse_options = ParseOptions(
            model_version=self.mineru_model_combo.currentText().strip() or DEFAULT_MODEL_VERSION,
            enable_table=self.table_check.isChecked(),
            enable_formula=self.formula_check.isChecked(),
            is_ocr=self.ocr_check.isChecked(),
        )
        self.settings.mineru_model = self._batch_parse_options.model_version
        app_config.save_settings(self.settings)
        self._batch_parse_wave_size = self.configured_batch_concurrency()

        self.clear_logs()
        self._task_stop_requested = False
        self.begin_task_ui()
        self._batch_parse_total = len(self.batch_parse_queue)
        self._batch_parse_done = 0
        self._batch_parse_failed = 0
        self._batch_parse_skipped = 0
        self._batch_parse_wave_index = 0
        self._batch_parse_next_wave_earliest = 0.0
        self._batch_parse_waiting_for_wave = False
        self._batch_parse_active_status = {}
        self._batch_translate_active_status = {}
        self._batch_parse_translation_accepting_sources = bool(self._batch_parse_then_translate)
        self.run_button.setEnabled(False)
        self.file_button.setEnabled(False)
        self.batch_run_button.setEnabled(False)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.set_batch_progress_visible(True)
        if self._batch_parse_then_translate:
            self.start_parse_translation_pipeline()
        self.append_log(f"批量解析已开始：共 {self._batch_parse_total} 个文件（最多同时解析 {self._batch_parse_wave_size} 个）。")
        self.update_batch_progress_panel()
        self.dispatch_next_parse_wave()

    def start_parse_translation_pipeline(self) -> None:
        """Prepare a translation queue that accepts documents as parsing ends."""
        config = getattr(self, "_batch_translation_config", None)
        if not config:
            self._batch_parse_then_translate = False
            self._batch_parse_translation_accepting_sources = False
            self.append_log("批量翻译配置不可用，本次仅解析文档。")
            return
        sources = list(self.batch_parse_queue)
        document_concurrency, request_concurrency = self.effective_batch_translation_concurrency(
            config,
            sources,
        )
        self._batch_translate_concurrency = document_concurrency
        self._batch_layout_translate_concurrency = document_concurrency
        self._batch_request_concurrency = request_concurrency
        self._batch_translation_layout_mode = bool(self._batch_parse_translate_layout_mode)
        self._batch_translate_total = self._batch_parse_total
        self._batch_translate_done = 0
        self._batch_translate_failed = 0
        self._batch_layout_translate_total = self._batch_parse_total
        self._batch_layout_translate_done = 0
        self._batch_layout_translate_failed = 0
        self.batch_translate_queue = []
        self._batch_layout_translate_queue = []
        title = "排版翻译" if self._batch_translation_layout_mode else "翻译"
        self.append_log(
            f"解析完成的文档将自动接续{title}（文档并发 {document_concurrency}，"
            f"请求并发 {request_concurrency}）。"
        )

    def enqueue_parsed_document_for_translation(self, source: Path) -> None:
        """Start background translation without waiting for the parse queue."""
        if not source.exists() or not self._batch_parse_translation_accepting_sources:
            return
        if self._batch_translation_layout_mode and not load_layout_preview_bundle(source):
            self._batch_translate_total = max(0, self._batch_translate_total - 1)
            self._batch_layout_translate_total = max(0, self._batch_layout_translate_total - 1)
            self.append_log(f"[{source.parent.name}] 未找到版面数据，已跳过排版翻译。")
            self.update_batch_progress_panel()
            return
        queue = self._batch_layout_translate_queue if self._batch_translation_layout_mode else self.batch_translate_queue
        if source in queue or source in self.running_translate_sources.values():
            return
        queue.append(source)
        self._batch_translate_active_status[source.parent.name] = "等待翻译"
        self.append_log(f"[{source.parent.name}] 解析完成，已开始后台翻译。")
        if self._batch_translation_layout_mode:
            self.dispatch_batch_layout_translation()
        else:
            self.dispatch_batch_translation()
        self.update_batch_progress_panel()

    def handle_batch_parse_worker_log(self, source: Path, prefix: str, message: str) -> None:
        """Keep detailed worker output while exposing one readable live state."""
        compact = str(message or "").strip()
        page_match = re.search(r"解析中:\s*(\d+)\s*/\s*(\d+)\s*页", compact)
        if page_match:
            status = f"第 {page_match.group(1)}/{page_match.group(2)} 页"
        elif "上传" in compact:
            status = "正在上传"
        elif "等待 MinerU" in compact or "精准解析状态" in compact:
            status = "MinerU 正在解析"
        elif "下载" in compact:
            status = "正在下载结果"
        elif "整理" in compact:
            status = "正在整理结果"
        else:
            status = "准备中"
        self._batch_parse_active_status[source.name] = status
        self.append_log(f"{prefix} {compact}")
        self.update_batch_progress_panel()

    def handle_batch_translate_worker_log(self, source: Path, prefix: str, message: str) -> None:
        compact = str(message or "").strip()
        progress_match = re.search(r"已完成\s*(\d+)\s*/\s*(\d+)", compact)
        if progress_match:
            status = f"已处理 {progress_match.group(1)}/{progress_match.group(2)} 段"
        elif "提取到" in compact:
            status = "正在准备内容"
        elif "完成" in compact:
            status = "正在收尾"
        else:
            status = "正在翻译"
        self._batch_translate_active_status[source.parent.name] = status
        self.append_log(f"{prefix} {compact}")
        self.update_batch_progress_panel()

    def start_batch_parse_then_translate(self):
        if self.reject_new_processing_task("批量解析并翻译"):
            return
        saved_config = self.load_saved_translation_config()
        dialog = TranslationOptionsDialog(self, provider_id=saved_config.provider_id if saved_config else self.settings.ai_provider, allow_parse_only=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        parse_only = bool(dialog.parse_only_check and dialog.parse_only_check.isChecked())
        if not parse_only:
            ai_config = dialog.selected_ai_config
            if not ai_config:
                return
            self._batch_translation_config = TranslationJobConfig(
                ai_config=ai_config,
                source_language=str(dialog.source_combo.currentData() or dialog.source_combo.currentText().strip() or "英文"),
                target_language=dialog.target_combo.currentText().strip() or "简体中文",
                mode=dialog.mode_combo.currentData() or "full_context",
                reference_paths=list(dialog.reference_paths),
                local_machine_parallelism=machine_translate.normalize_parallelism(dialog.local_parallel_spin.value()),
            )
            self.save_translation_preferences(
                self._batch_translation_config.target_language,
                self._batch_translation_config.mode,
                self._batch_translation_config.reference_paths,
                self._batch_translation_config.source_language,
                self._batch_translation_config.local_machine_parallelism,
            )
            self._batch_parse_translate_layout_mode = bool(
                self.settings.layout_reading_mode
                and not any(is_epub_input_file(path) for path in self.batch_parse_queue)
            )
            if self.settings.layout_reading_mode and not self._batch_parse_translate_layout_mode:
                self.append_log("批次包含 EPUB，已自动使用适合可重排电子书的流式翻译。")
            self._batch_parse_then_translate = True
        else:
            self._batch_parse_then_translate = False
        self._batch_parse_success_markdowns = []
        self.start_batch_parse(preserve_parse_translate=True)

    def finish_batch_parse(self):
        stopped = bool(self._task_stop_requested)
        success_count = max(0, self._batch_parse_done - self._batch_parse_failed - self._batch_parse_skipped)
        if stopped:
            self.append_log(
                f"批量解析已停止：成功 {success_count} 个，失败 {self._batch_parse_failed} 个，跳过 {self._batch_parse_skipped} 个。"
            )
        elif self._batch_parse_failed or self._batch_parse_skipped:
            self.append_log(
                f"批量解析完成：成功 {success_count} 个，失败 {self._batch_parse_failed} 个，跳过 {self._batch_parse_skipped} 个。"
            )
        else:
            self.append_log(f"批量解析完成：成功 {success_count} 个。")

        pipeline_translation = bool(self._batch_parse_then_translate)

        self.batch_parse_queue = []
        self.running_parse_workers.clear()
        self.running_parse_sources.clear()
        # Keep the completed parse count visible until the final background
        # translation finishes.  A new batch always initializes these fields.
        if not pipeline_translation:
            self._batch_parse_total = 0
            self._batch_parse_done = 0
            self._batch_parse_failed = 0
            self._batch_parse_skipped = 0
        self._batch_parse_options = None
        self._batch_parse_wave_index = 0
        self._batch_parse_next_wave_earliest = 0.0
        self._batch_parse_waiting_for_wave = False
        self._batch_parse_active_status = {}
        if self._batch_parse_timer.isActive():
            self._batch_parse_timer.stop()
        self.run_button.setEnabled(True)
        self.file_button.setEnabled(True)
        self.batch_run_button.setEnabled(True)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.refresh_docs()

        self._batch_parse_translation_accepting_sources = False
        self._batch_parse_then_translate = False
        self._batch_parse_success_markdowns = []
        self._task_stop_requested = False
        self.update_batch_progress_panel()
        if pipeline_translation:
            # Translation is dispatched immediately when each parsed document
            # becomes available.  The parser is now closed to new sources, so
            # this may finish the last queued translation and close the task.
            self.maybe_finish_batch_translation()
        else:
            self.finish_task_ui()

    def schedule_next_parse_wave_if_needed(self):
        if self._task_stop_requested:
            if not self.running_parse_workers:
                self.finish_batch_parse()
            return
        if self.running_parse_workers:
            return
        if not self.batch_parse_queue:
            self.finish_batch_parse()
            return
        remaining = max(0.0, self._batch_parse_next_wave_earliest - time.monotonic())
        if remaining > 0:
            if not self._batch_parse_waiting_for_wave:
                self.append_log(f"下一批解析将在 {max(1, math.ceil(remaining))} 秒后启动。")
            self._batch_parse_waiting_for_wave = True
            if self._batch_parse_timer.isActive():
                self._batch_parse_timer.stop()
            self._batch_parse_timer.start(max(1, int(remaining * 1000)))
            return
        self.dispatch_next_parse_wave()

    def dispatch_next_parse_wave(self):
        if self._task_stop_requested:
            self.schedule_next_parse_wave_if_needed()
            return
        if self.running_parse_workers:
            return
        self._batch_parse_waiting_for_wave = False
        launch_paths: list[Path] = []
        while self.batch_parse_queue and len(launch_paths) < self._batch_parse_wave_size:
            pdf_path = self.batch_parse_queue.pop(0)
            if not self.confirm_duplicate_parse(pdf_path):
                self.append_log(f"已跳过同名解析文件：{pdf_path.name}")
                self._batch_parse_done += 1
                self._batch_parse_skipped += 1
                if self._batch_parse_translation_accepting_sources:
                    self._batch_translate_total = max(0, self._batch_translate_total - 1)
                    self._batch_layout_translate_total = max(0, self._batch_layout_translate_total - 1)
                continue
            launch_paths.append(pdf_path)
        if not launch_paths:
            self.update_batch_progress_panel()
            self.schedule_next_parse_wave_if_needed()
            return
        self._batch_parse_wave_index += 1
        self.append_log(f"正在启动本批次 {len(launch_paths)} 个解析任务…")
        self._batch_parse_next_wave_earliest = time.monotonic() + self._batch_parse_wave_interval_seconds
        for pdf_path in launch_paths:
            self.start_batch_parse_file(pdf_path)
        if self._batch_parse_total:
            self.progress.setValue(int(self._batch_parse_done * 100 / self._batch_parse_total))
        self.update_batch_progress_panel()

    def start_batch_parse_file(self, pdf_path: Path):
        try:
            # 在主线程派发时立即原子预留输出目录，避免同名文件并发写入同一路径。
            output_dir = output_dir_for_pdf(pdf_path, reserve=True)
        except Exception as exc:
            self._batch_parse_done += 1
            self._batch_parse_failed += 1
            if self._batch_parse_translation_accepting_sources:
                self._batch_translate_total = max(0, self._batch_translate_total - 1)
                self._batch_layout_translate_total = max(0, self._batch_layout_translate_total - 1)
            self.append_log(f"[{pdf_path.name}] 无法创建独立输出目录: {exc}")
            self.update_batch_progress_panel()
            self.schedule_next_parse_wave_if_needed()
            return
        app_config.remember_recent_file(self.settings, str(pdf_path))
        options = self._batch_parse_options or ParseOptions()
        worker = create_document_parse_worker(pdf_path, output_dir, options)
        worker_id = id(worker)
        self.running_parse_workers[worker_id] = worker
        self.running_parse_sources[worker_id] = pdf_path
        self._batch_parse_active_status[pdf_path.name] = "本地解析" if is_epub_input_file(pdf_path) else "准备上传"
        label = f"[解析 {self._batch_parse_done + len(self.running_parse_workers)}/{self._batch_parse_total}][{pdf_path.name}]"
        worker.log_signal.connect(
            lambda text, src=pdf_path, prefix=label: self.handle_batch_parse_worker_log(src, prefix, text)
        )
        worker.finished_signal.connect(
            lambda success, message, markdown_path, wid=worker_id: self.finish_batch_parse_item(wid, success, message, markdown_path)
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self.update_batch_progress_panel()

    def finish_batch_parse_item(self, worker_id: int, success: bool, message: str, markdown_path: str):
        worker = self.running_parse_workers.pop(worker_id, None)
        pdf_path = self.running_parse_sources.pop(worker_id, None)
        if worker is self.worker:
            self.worker = None
        self._batch_parse_done += 1
        label = f"[{pdf_path.name}]" if pdf_path else "[批量解析]"
        if pdf_path:
            self._batch_parse_active_status.pop(pdf_path.name, None)
        if success:
            self.append_log(f"{label} {message}")
            if markdown_path:
                self._batch_parse_success_markdowns.append(Path(markdown_path))
                if self._batch_parse_translation_accepting_sources:
                    self.enqueue_parsed_document_for_translation(Path(markdown_path))
            if (
                markdown_path
                and pdf_path
                and self.current_original_path
                and pdf_path.resolve() == self.current_original_path.resolve()
            ):
                self.load_markdown(Path(markdown_path))
        else:
            self._batch_parse_failed += 1
            if self._batch_parse_translation_accepting_sources:
                self._batch_translate_total = max(0, self._batch_translate_total - 1)
                self._batch_layout_translate_total = max(0, self._batch_layout_translate_total - 1)
            self.append_log(f"{label} 失败: {message}")
        if self._batch_parse_total:
            self.progress.setValue(int(self._batch_parse_done * 100 / self._batch_parse_total))
        self.update_batch_progress_panel()
        self.schedule_next_parse_wave_if_needed()

    def refresh_docs(self):
        self.docs = scan_parsed_docs(current_work_dir())
        self.docs = self.sorted_docs_by_saved_order(self.docs)
        self.cleanup_embedded_ai_orphans()
        current_folder = None
        current_path = self.current_source_path or self.current_markdown_path
        if current_path:
            try:
                current_folder = current_path.parent.resolve()
            except Exception:
                current_folder = current_path.parent

        self.doc_list.clear()
        selected_item = None
        for doc in self.docs:
            item = QListWidgetItem(doc.title)
            item.setToolTip(str(doc.markdown_path))
            item.setData(256, str(doc.markdown_path))
            self.doc_list.addItem(item)
            try:
                doc_folder = doc.folder.resolve()
            except Exception:
                doc_folder = doc.folder
            if current_folder is not None and doc_folder == current_folder:
                selected_item = item

        if selected_item is not None:
            self.doc_list.setCurrentItem(selected_item)
            self.doc_list.scrollToItem(selected_item)
        elif self.docs and self.open_last_document_if_available():
            return
        elif self.docs and (self.current_markdown_path is None or not self.current_markdown_path.exists()):
            self.load_markdown(self.docs[0].markdown_path)

    @staticmethod
    def document_path_key(path: Path) -> str:
        """Stable key for persisted document ordering and last-open restore."""
        try:
            return str(path.resolve())
        except OSError:
            return str(path.absolute())

    def sorted_docs_by_saved_order(self, docs: list[ParsedDoc]) -> list[ParsedDoc]:
        saved_order = list(getattr(self.settings, "document_order", []) or [])
        positions = {path: index for index, path in enumerate(saved_order)}
        # Newly parsed documents follow the existing manual order instead of
        # reshuffling it; their scan order remains the fallback order.
        return sorted(
            docs,
            key=lambda doc: positions.get(self.document_path_key(doc.markdown_path), len(positions)),
        )

    def save_document_list_order(self, *args):
        self.settings.document_order = [
            self.document_path_key(Path(str(self.doc_list.item(index).data(256))))
            for index in range(self.doc_list.count())
        ]
        app_config.save_settings(self.settings)

    def open_last_document_if_available(self) -> bool:
        last_path = str(getattr(self.settings, "last_open_document", "") or "")
        if not last_path:
            return False
        for index, doc in enumerate(self.docs):
            if self.document_path_key(doc.markdown_path) == last_path:
                item = self.doc_list.item(index)
                if item is not None:
                    self.doc_list.setCurrentItem(item)
                    self.doc_list.scrollToItem(item)
                self.load_markdown(doc.markdown_path)
                return True
        return False

    def remember_open_document(self):
        markdown_path = getattr(self, "current_markdown_path", None)
        if markdown_path and markdown_path.exists():
            self.settings.last_open_document = self.document_path_key(markdown_path)
            app_config.save_settings(self.settings)

    def ensure_embedded_chat(self):
        if self.embedded_chat_window is not None:
            return self.embedded_chat_window
        try:
            import AI_api_base
            adapter = build_mineru_document_tool_adapter(AI_api_base, reader_window_factory=ReaderWindow)
            AI_api_base.set_document_tool_adapter(adapter)
            chat = AI_api_base.ChatWindow(embedded=True)
            chat.reference_quote_reveal_callback = self.reveal_reference_quote
            chat.conversation_history_path = lambda: app_config.chat_history_path(self.settings)
            chat.embedded_document_loaded_callback = self.on_embedded_document_conversation_loaded
            # Save the workspace's key-points instruction without merging the
            # translation and document-chat provider or model selections.
            chat.shared_settings_save_callback = app_config.save_settings
            # 同一服务商的密钥可复用，但服务商、服务地址和模型选择仍分别保存。
            chat.shared_secret_save_callback = app_config.save_secret
            if hasattr(chat, "sync_from_app_settings"):
                chat.sync_from_app_settings(self.settings, self.ai_provider_keys_for_chat())
            chat.load_conversation_sessions()
            chat.refresh_conversation_history_list()
            chat.setStyleSheet(self.styleSheet())
            if hasattr(chat, "apply_embedded_compact_style"):
                chat.apply_embedded_compact_style()
            self.embedded_chat_window = chat
            self.ai_placeholder_label.setVisible(False)
            for index in range(self.ai_sidebar_layout.count() - 1, -1, -1):
                item = self.ai_sidebar_layout.itemAt(index)
                if item and item.spacerItem():
                    self.ai_sidebar_layout.takeAt(index)
            self.ai_sidebar_layout.insertWidget(0, chat, 1)
            return chat
        except Exception as exc:
            self.ai_placeholder_label.setText(f"文献对话加载失败：{exc}")
            self.ai_placeholder_label.setVisible(True)
            return None

    def reveal_reference_quote(self, quote: dict) -> bool:
        """在主阅读区打开引用所属文献并滚动至文字、图片或公式的位置。"""
        if not isinstance(quote, dict):
            return False
        path_text = str(quote.get("document_path") or quote.get("source_markdown_path") or quote.get("markdown_path") or quote.get("path") or "").strip()
        target = str(quote.get("formula_tex") or quote.get("text") or "").strip()
        if not path_text or not target:
            return False
        path = Path(path_text)
        if not path.exists():
            return False
        try:
            current_path = (self.current_markdown_path or self.current_source_path)
            is_current = bool(current_path and current_path.resolve() == path.resolve())
        except OSError:
            is_current = False
        if not is_current:
            self.load_markdown(path)

        quote, resolution = resolve_reference_quote(
            quote,
            source_path=self.current_source_path,
            translation_path=(self.current_layout_translation_path if quote.get("render_mode") == "layout" else self.current_translation_path),
            live_translation=(self.live_layout_translation_markdown if quote.get("render_mode") == "layout" else self.live_translation_markdown),
        )
        if resolution == "translation_stale":
            self.append_log("引用的译文版本已更新，已改按原文锚点定位。")

        self.raise_()
        self.activateWindow()
        # 文献切换和 WebEngine 加载是异步的。延迟一次能覆盖同文献定位，也能
        # 覆盖由引用气泡触发的文献切换；文本/公式均复用阅读器的查找能力。
        pane = str(quote.get("pane") or "source")
        if pane != "translation" and pdf_view_is_active(self):
            QTimer.singleShot(0, lambda item=dict(quote): focus_pdf_reference_quote(self, item))
            return True
        web_view = self.translation_web_view if pane == "translation" else self.source_web_view
        fallback_viewer = self.translation_fallback_viewer if pane == "translation" else self.source_fallback_viewer
        item = dict(quote)
        if not is_current and web_view:
            delivered = {"value": False}

            def deliver_focus(_ok=True):
                if delivered["value"]:
                    return
                delivered["value"] = True
                try:
                    web_view.loadFinished.disconnect(on_loaded)
                except (TypeError, RuntimeError):
                    pass
                QTimer.singleShot(80, lambda: focus_reference_quote(web_view, fallback_viewer, item))

            def on_loaded(ok: bool):
                if ok:
                    deliver_focus()

            web_view.loadFinished.connect(on_loaded)
            # 网络/缓存异常时仍保留已有页面上的安全兜底，而不是让点击无响应。
            QTimer.singleShot(1800, deliver_focus)
        else:
            QTimer.singleShot(0, lambda: focus_reference_quote(web_view, fallback_viewer, item))
        return True

    def reveal_text(self, text: str, pane: str = "", image_src: str = "", page: int | None = None, anchor_ratio: float | None = None, scroll_ratio: float | None = None):
        """在主界面原文、译文两个阅读面板中定位指定内容。"""
        text = str(text or "").strip()
        if not text:
            return
        web_views = (self.translation_web_view, self.source_web_view) if pane == "translation" else (self.source_web_view, self.translation_web_view)
        fallback_views = (self.translation_fallback_viewer, self.source_fallback_viewer) if pane == "translation" else (self.source_fallback_viewer, self.translation_fallback_viewer)
        try:
            page_number = int(page) if page is not None else 0
        except (TypeError, ValueError):
            page_number = 0
        try:
            page_ratio = max(0.0, min(1.0, float(anchor_ratio))) if anchor_ratio is not None else None
        except (TypeError, ValueError):
            page_ratio = None
        if page_number > 0:
            if pane != "translation" and pdf_view_is_active(self):
                focus_pdf_reference_quote(
                    self,
                    {
                        "type": "image" if image_src else ("formula" if text.startswith("\\") else "text"),
                        "text": text,
                        "anchor_page": page_number,
                        "anchor_ratio": page_ratio,
                    },
                )
                return
            page_index = page_number - 1
            ratio_json = json.dumps(page_ratio)
            script = f"""(() => {{ const node = document.querySelector('[data-sync-page-index=\"{page_index}\"]'); if (!node) return; node.scrollIntoView({{block: 'start', behavior: 'auto'}}); const ratio = {ratio_json}; if (ratio !== null) window.scrollBy(0, node.getBoundingClientRect().height * ratio - window.innerHeight * .22); }})();"""
            for web_view in web_views:
                if web_view:
                    web_view.page().runJavaScript(script)
            return
        try:
            document_ratio = max(0.0, min(1.0, float(scroll_ratio))) if scroll_ratio is not None else None
        except (TypeError, ValueError):
            document_ratio = None
        if document_ratio is not None:
            ratio_json = json.dumps(document_ratio)
            script = f"""(() => {{ const root = document.scrollingElement || document.documentElement; window.scrollTo(0, (root.scrollHeight - root.clientHeight) * {ratio_json}); }})();"""
            for web_view in web_views:
                if web_view:
                    web_view.page().runJavaScript(script)
            return
        if image_src:
            source_json = json.dumps(image_src, ensure_ascii=False)
            script = f"""(() => {{ const source = {source_json}; const image = [...document.images].find((node) => node.currentSrc === source || node.src === source || node.getAttribute('src') === source); if (image) image.scrollIntoView({{block: 'center', behavior: 'smooth'}}); }})();"""
            for web_view in web_views:
                if web_view:
                    web_view.page().runJavaScript(script)
            return
        if text.startswith("\\"):
            tex_json = json.dumps(text, ensure_ascii=False)
            script = f"""(() => {{ const tex = {tex_json}.replace(/\\s+/g, ''); const node = [...document.querySelectorAll('annotation')].find((item) => item.textContent.replace(/\\s+/g, '') === tex); const host = node && (node.closest('mjx-container') || node.parentElement); if (host) host.scrollIntoView({{block: 'center', behavior: 'smooth'}}); }})();"""
            for web_view in web_views:
                if web_view:
                    web_view.page().runJavaScript(script)
            return
        for web_view in web_views:
            if web_view:
                try:
                    web_view.page().findText("")
                    web_view.page().findText(text)
                    return
                except RuntimeError:
                    pass
        for viewer in fallback_views:
            cursor = viewer.document().find(text)
            if cursor and not cursor.isNull():
                viewer.setTextCursor(cursor)
                viewer.ensureCursorVisible()
                viewer.setFocus()
                return

    def on_left_nav_changed(self, index: int):
        if index != 1:
            return
        # Creating the embedded chat also creates WebEngine-backed widgets and
        # may start a model-list request.  During an active translation the
        # translation worker is continuously delivering preview updates; doing
        # that work synchronously from the navigation click can re-enter the
        # workbench while Qt is dispatching the same event and has caused native
        # WebEngine crashes.  Keep the AI page responsive, but let the current
        # event finish before attaching the document and refreshing models.
        if self.has_active_translation_task():
            if getattr(self, "_ai_open_initialization_scheduled", False):
                return
            self._ai_open_initialization_scheduled = True

            def initialize_ai_after_navigation():
                self._ai_open_initialization_scheduled = False
                # The user is allowed to use AI while translation runs.  The
                # deferred callback is intentionally not cancelled when the
                # worker is still active; only the unsafe synchronous re-entry
                # is avoided.
                if self.left_stack.currentIndex() != 1:
                    return
                self.load_embedded_ai_for_current_document()
                self.refresh_models_when_ai_first_opened()

            QTimer.singleShot(0, initialize_ai_after_navigation)
            return

        self.load_embedded_ai_for_current_document()
        self.refresh_models_when_ai_first_opened()

    def refresh_models_when_ai_first_opened(self):
        """Refresh the selected chat provider's model list when document chat opens."""
        # Model refresh may run before a document is selected.
        chat = self.embedded_chat_window or self.ensure_embedded_chat()
        if not chat or not hasattr(chat, "fetch_models"):
            return
        if hasattr(chat, "current_ai_key_available") and not chat.current_ai_key_available():
            # 对话是按需功能：第一次真正打开时才询问对话服务密钥，绝不在程序启动时
            # 用翻译服务商代替对话服务商要求用户填写。
            chat.prompt_for_missing_startup_keys(include_document_tool=False)
        # fetch_models 会在已有请求运行时保持单线程，不阻塞 UI；因此此处无需
        # Refresh when the document-chat panel is opened so the model list stays current.
        chat.fetch_models(silent=True)

    def document_chat_session_id(self, markdown_path: Path | None = None) -> str:
        path = markdown_path or self.current_source_path or self.current_markdown_path
        if not path:
            return ""
        try:
            resolved = str(path.parent.resolve())
        except Exception:
            resolved = str(path.parent)
        return "doc-chat-" + hashlib.sha1(resolved.encode("utf-8", errors="replace")).hexdigest()

    def load_embedded_ai_for_current_document(self):
        if not self.current_source_path:
            return
        chat = self.ensure_embedded_chat()
        if not chat:
            return
        session_id = self.document_chat_session_id(self.current_source_path)
        if session_id and session_id != self._embedded_chat_doc_key:
            # 正在生成时 ChatWindow 会延迟切换；只有真正载入成功后才更新 key，
            # 防止后续同步误以为已切到新文献而永久显示旧会话。
            if chat.load_document_conversation(session_id, self.current_source_path.parent.name, self.current_source_path):
                self._embedded_chat_doc_key = session_id

    def on_embedded_document_conversation_loaded(self, session_id: str):
        """Receive both immediate and deferred embedded-chat document switches."""
        self._embedded_chat_doc_key = str(session_id or "")

    def ai_provider_keys_for_chat(self) -> dict[str, str]:
        keys: dict[str, str] = {}
        for provider_id in getattr(self.settings, "providers", {}) or {}:
            if machine_translate.is_machine_translation_provider(provider_id):
                continue
            key = app_config.load_secret(provider_id, "api_key")
            if key:
                keys[provider_id] = key
        return keys

    def delete_embedded_ai_for_document(self, markdown_path: Path):
        chat = self.ensure_embedded_chat()
        if not chat:
            return
        session_id = self.document_chat_session_id(markdown_path)
        if session_id:
            chat.delete_document_conversation(session_id)

    def cleanup_embedded_ai_orphans(self):
        if self.embedded_chat_window is None:
            return
        valid_ids = {self.document_chat_session_id(doc.markdown_path) for doc in self.docs}
        self.embedded_chat_window.delete_document_conversations_except(valid_ids)

    def open_doc_item(self, item: QListWidgetItem):
        path = Path(item.data(256))
        self.load_markdown(path)

    def current_layout_body_font_pt(self) -> float | None:
        memory = getattr(self.settings, "layout_body_font_by_document", {})
        value = memory.get(layout_body_font_document_key(self.current_source_path)) if isinstance(memory, dict) else None
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def layout_body_font_pt_for_document(self, path: Path | str | None) -> float | None:
        """Return the explicit layout-body override stored for ``path``."""
        memory = getattr(self.settings, "layout_body_font_by_document", {})
        value = memory.get(layout_body_font_document_key(path)) if isinstance(memory, dict) else None
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def current_layout_pdf_cache_version(self, html_path: Path | str | None = None) -> str:
        # PDF export can be initiated from an independent reader window.  Its
        # document is not necessarily the main window's current document, so
        # bind the derived-PDF cache to the HTML artifact being printed.
        value = (
            self.layout_body_font_pt_for_document(html_path)
            if html_path is not None
            else self.current_layout_body_font_pt()
        )
        suffix = "auto" if value is None else f"{value:.2f}pt"
        return f"{LAYOUT_PDF_CACHE_VERSION}-{suffix}"

    def set_layout_body_font_for_current_document(self, value: float, source=None):
        key = layout_body_font_document_key(self.current_source_path)
        if not key:
            return
        memory = getattr(self.settings, "layout_body_font_by_document", None)
        if not isinstance(memory, dict):
            memory = {}
            self.settings.layout_body_font_by_document = memory
        memory[key] = round(float(value) * 2.0) / 2.0
        app_config.save_settings(self.settings)
        self.reader_font_spin.blockSignals(True)
        self.reader_font_spin.setValue(memory[key])
        self.reader_font_spin.blockSignals(False)
        self.apply_reader_font_size()
        for reader in list(self.reader_windows):
            if reader is source or layout_body_font_document_key(reader.source_path) != key:
                continue
            reader.layout_body_font_pt = memory[key]
            reader.reader_font_spin.blockSignals(True)
            reader.reader_font_spin.setValue(memory[key])
            reader.reader_font_spin.blockSignals(False)
            reader.apply_reader_font_size()

    def clear_layout_body_font_for_document(self, path: Path | str | None):
        """Return a retranslated document to automatic body-font fitting."""
        key = layout_body_font_document_key(path)
        memory = getattr(self.settings, "layout_body_font_by_document", None)
        if key and isinstance(memory, dict) and key in memory:
            memory.pop(key, None)
            app_config.save_settings(self.settings)
        # Reader windows cache the current spin-box value locally.  Clear it
        # too, otherwise refreshing the replacement HTML would immediately
        # restore the old override even though its saved preference was gone.
        for reader in list(self.reader_windows):
            if layout_body_font_document_key(getattr(reader, "source_path", None)) == key:
                reader.layout_body_font_pt = None

    def on_reader_font_changed(self, value: float):
        if self.settings.layout_reading_mode:
            self.set_layout_body_font_for_current_document(float(value))
            return
        self.settings.reader_font_pt = int(value)
        app_config.save_settings(self.settings)
        self.apply_reader_font_size()
        for reader in list(self.reader_windows):
            if hasattr(reader, "reader_font_spin"):
                reader.reader_font_spin.blockSignals(True)
                reader.reader_font_spin.setValue(self.settings.reader_font_pt)
                reader.reader_font_spin.blockSignals(False)
                reader.reader_font_pt = self.settings.reader_font_pt
                reader.apply_reader_font_size()

    def apply_reader_font_size(self):
        if self.settings.layout_reading_mode:
            value = self.current_layout_body_font_pt()
            if value is None:
                self.refresh_layout_body_font_display()
                return
            script = layout_body_font_script(value)
            for web_view in (self.source_web_view, self.translation_web_view):
                if web_view:
                    web_view.page().runJavaScript(script)
            return
        apply_reader_font_to_web_view(self.source_web_view, self.settings.reader_font_pt)
        apply_reader_font_to_web_view(self.translation_web_view, self.settings.reader_font_pt)
        apply_reader_font_to_text_browser(self.source_fallback_viewer, self.settings.reader_font_pt)
        apply_reader_font_to_text_browser(self.translation_fallback_viewer, self.settings.reader_font_pt)

    def refresh_layout_body_font_display(self, attempt: int = 0, view_index: int = 0):
        """Reflect the current fitted body size without persisting an override."""
        if not self.settings.layout_reading_mode or self.current_layout_body_font_pt() is not None:
            return
        document_key = layout_body_font_document_key(self.current_source_path)
        if not document_key:
            return
        views = [view for view in (self.source_web_view, self.translation_web_view) if view]
        if not views:
            return
        if view_index >= len(views):
            if attempt < 600:
                QTimer.singleShot(100, lambda: self.refresh_layout_body_font_display(attempt + 1, 0))
            return

        def receive(payload):
            if document_key != layout_body_font_document_key(self.current_source_path):
                return
            data = decode_web_javascript_payload(payload) or {}
            try:
                value = float(data.get("fontPt") or 0)
            except (TypeError, ValueError):
                value = 0.0
            if data.get("ready") is True and value > 0:
                self._detected_layout_body_font_pt = value
                self.reader_font_spin.blockSignals(True)
                self.reader_font_spin.setValue(value)
                self.reader_font_spin.blockSignals(False)
                return
            self.refresh_layout_body_font_display(attempt, view_index + 1)

        views[view_index].page().runJavaScript(layout_body_font_probe_script(), receive)

    def show_doc_list_context_menu(self, pos):
        item = self.doc_list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        delete_action = menu.addAction("删除此解析结果")
        action = menu.exec(self.doc_list.mapToGlobal(pos))
        if action == delete_action:
            self.delete_doc_item(item)

    def delete_doc_item(self, item: QListWidgetItem):
        markdown_path = Path(item.data(256))
        folder = markdown_path.parent
        if not folder.exists() or folder == current_work_dir():
            return
        confirm = QMessageBox.question(
            self,
            "删除解析结果",
            f"确定删除解析结果文件夹吗？\n{folder}\n\n源文件不会被删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            is_current_document = bool(
                self.current_source_path and self.current_source_path.parent == folder
            )
            # A QPdfDocument (and a ReaderWindow opened for this document) can
            # retain a Windows file handle. Detach every visible preview before
            # attempting rmtree(), rather than reporting a misleading delete
            # failure for the document currently shown by this application.
            if is_current_document:
                self.reset_sync_scroll_runtime()
                release_source_pdf(self)
                self.show_placeholder(self.source_web_view, self.source_fallback_viewer, "正在关闭当前文献预览…")
                self.show_placeholder(self.translation_web_view, self.translation_fallback_viewer, "正在关闭当前文献预览…")
            for reader in list(self.reader_windows):
                reader_source = getattr(reader, "source_path", None)
                reader_original = getattr(reader, "original_path", None)
                if not any(path and path.parent == folder for path in (reader_source, reader_original)):
                    continue
                reader.close()
                reader.deleteLater()
                if reader in self.reader_windows:
                    self.reader_windows.remove(reader)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QCoreApplication.processEvents()
            self.delete_embedded_ai_for_document(markdown_path)
            shutil.rmtree(folder)
            memory = getattr(self.settings, "layout_body_font_by_document", None)
            if isinstance(memory, dict):
                memory.pop(layout_body_font_document_key(markdown_path), None)
                app_config.save_settings(self.settings)
            self.append_log(f"已删除解析记录：{folder.name if hasattr(folder, 'name') else folder}")
            if is_current_document:
                self.current_markdown_path = None
                self.current_source_path = None
                self.current_original_path = None
                self.current_translation_path = None
                self.current_layout_translation_path = None
                self.live_translation_markdown = ""
                self.live_layout_translation_markdown = ""
                self.title_label.setText("未打开文档")
                self.show_placeholder(self.source_web_view, self.source_fallback_viewer, "请选择左侧解析结果。")
                self.show_placeholder(self.translation_web_view, self.translation_fallback_viewer, "暂无译文。")
            self.refresh_docs()
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", str(exc))

    def load_markdown(self, markdown_path: Path):
        if not markdown_path.exists():
            return
        self.capture_current_scroll_state()
        self.reset_sync_scroll_runtime()
        self.leave_layout_paired_canvas()
        folder = markdown_path.parent
        self._current_document_is_epub = is_epub_markdown_path(markdown_path)
        if self._current_document_is_epub and self.settings.layout_reading_mode:
            self.settings.layout_reading_mode = False
            app_config.save_settings(self.settings)
            self.append_log("EPUB 为流式重排电子书，已自动切换为流式阅读模式。")
        source_path = folder / "full.cleaned.md"
        translation_path = latest_translation_path(folder)
        if markdown_path.name == "full.cleaned.md":
            source_path = markdown_path
        elif markdown_path.name.startswith("full.") and markdown_path.name != "full.cleaned.md" and not source_path.exists():
            source_path = markdown_path
            translation_path = markdown_path
        original_path = None
        meta_path = folder / "mineru_task.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
                raw_source = meta.get("source_file") or meta.get("source_pdf") or ""
                if raw_source:
                    candidate = Path(raw_source)
                    if candidate.exists():
                        original_path = candidate
            except Exception:
                original_path = None
        original_path = find_stored_original(folder, meta) or original_path

        self.current_markdown_path = markdown_path
        self.remember_open_document()
        self.current_source_path = source_path if source_path.exists() else None
        self._detected_layout_body_font_pt = None
        self.current_original_path = original_path
        self.current_translation_path = translation_path if translation_path and translation_path.exists() else None
        layout_translation_path = layout_translation_preview_html_path(source_path) if source_path and source_path.exists() else None
        self.current_layout_translation_path = (
            layout_translation_path if layout_translation_path and layout_translation_path.exists() else None
        )
        # 文献切换时立即把该篇自己的排版正文字号带入控件；未调整过的文献
        # 继续交给自动排版决定字号，不继承上一篇的选择。
        self.update_layout_mode_controls()
        source_key = str((source_path if source_path.exists() else markdown_path).resolve())
        self.live_translation_markdown = self.live_translation_by_source.get(
            source_key,
            self.current_translation_path.read_text(encoding="utf-8", errors="replace")
            if self.current_translation_path
            else "",
        )
        self.live_layout_translation_markdown = self.live_layout_translation_by_source.get(source_key, "")
        display_name = str(meta.get("source_display_name") or Path(str(meta.get("source_pdf") or "")).name or folder.name)
        # 论文名称可能很长；ElidedLabel 会按可用宽度显示省略号，完整名称可悬停查看。
        self.title_label.setText(f"正在显示：{display_name}")

        if self.current_source_path:
            self.show_source_preview()
        self.show_current_translation_for_mode()
        self.ensure_current_layout_translation_preview()
        self.update_sync_scroll_availability()
        self.update_translate_button_visibility()
        # The document list does not create chat controls; synchronize the
        # session only after the user opens document chat.
        if hasattr(self, "left_stack") and self.left_stack.currentIndex() == 1:
            self.load_embedded_ai_for_current_document()
        # Wire the synchronous scroll signal for the (possibly new) web view.
        self._connect_scroll_signal()
        # Suppress spurious scroll captures while the new page is loading.
        import time as _time
        self._scroll_capture_suppressed_until = _time.monotonic() + 2.0
        # Restore after page loads (loadFinished is most reliable) + fixed fallbacks.
        _restore_gen = getattr(self, '_restore_scroll_gen', 0) + 1
        self._restore_scroll_gen = _restore_gen
        def _restore_if_current():
            if getattr(self, '_restore_scroll_gen', 0) == _restore_gen:
                self.restore_current_scroll_state()
        def _on_load_finished(ok, _fn=_restore_if_current, _wv=self.source_web_view):
            try:
                _wv.loadFinished.disconnect(_on_load_finished)
            except Exception:
                pass
            QTimer.singleShot(200, _fn)
            QTimer.singleShot(1000, _fn)
        if self.source_web_view:
            try:
                self.source_web_view.loadFinished.connect(_on_load_finished)
            except Exception:
                pass
        # Fallback timers in case loadFinished already fired or is delayed.
        QTimer.singleShot(800, _restore_if_current)
        QTimer.singleShot(2000, _restore_if_current)

    def ensure_current_layout_translation_preview(self):
        source_path = self.current_source_path
        output_path = self.current_layout_translation_path
        if not source_path or not output_path or not output_path.exists():
            return
        source_layout_path = layout_preview_html_path(source_path, strict_fit=False, debug_overlay=False)
        if layout_translation_preview_is_current(output_path, source_layout_path):
            return
        key = str(source_path.resolve())
        existing = self._layout_preview_refresh_workers.get(key)
        if existing and self.is_thread_running(existing):
            return
        if not load_layout_translation_bundle(source_path):
            return
        worker = LayoutPreviewRefreshWorker(source_path, output_path)
        self._layout_preview_refresh_workers[key] = worker
        worker.finished_signal.connect(self.finish_layout_preview_refresh)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def finish_layout_preview_refresh(self, markdown_path: str, html_path: str, error: str):
        try:
            key = str(Path(markdown_path).resolve())
        except OSError:
            key = markdown_path
        self._layout_preview_refresh_workers.pop(key, None)
        if error:
            self.append_log(f"更新排版预览遇到问题，已保留当前预览：{error}")
            return
        if not html_path or not self.current_source_path:
            return
        try:
            is_current = self.current_source_path.resolve() == Path(markdown_path).resolve()
        except OSError:
            is_current = str(self.current_source_path) == markdown_path
        if is_current and self.settings.layout_reading_mode:
            self.current_layout_translation_path = Path(html_path)
            self.show_html_in_view(
                self.current_layout_translation_path,
                self.translation_web_view,
                self.translation_fallback_viewer,
            )

    def position_layout_transition_overlay(self, web_view) -> None:
        """Keep a reader-only transition overlay aligned with its web view."""
        if not web_view:
            return
        state = self._layout_transition_states.get(id(web_view))
        if not state:
            return
        overlay = state.get("overlay")
        image = state.get("image")
        badge = state.get("badge")
        if not overlay or not image or not badge:
            return
        rect = web_view.rect()
        overlay.setGeometry(rect)
        image.setGeometry(rect)
        badge_height = 38
        badge_width = max(180, min(max(1, rect.width() - 32), 420))
        badge.setGeometry(
            max(16, rect.width() - badge_width - 16),
            max(12, rect.height() - badge_height - 16),
            badge_width,
            badge_height,
        )

    def position_layout_transition_overlays(self) -> None:
        for web_view in (
            getattr(self, "source_web_view", None),
            getattr(self, "translation_web_view", None),
        ):
            self.position_layout_transition_overlay(web_view)
            self.position_layout_retranslation_notice(web_view)

    def position_layout_retranslation_notice(self, web_view) -> None:
        notice = self._layout_retranslation_notices.get(id(web_view)) if web_view else None
        if not notice or not web_view:
            return
        width = max(220, min(max(1, web_view.width() - 32), 460))
        notice.setGeometry(max(16, web_view.width() - width - 16), 16, width, 38)

    def show_layout_retranslation_notice(self, web_view) -> None:
        """Keep the published layout readable while a replacement is generated."""
        if not web_view or not web_view.isVisible():
            return
        self.clear_layout_retranslation_notice(web_view)
        notice = QLabel("正在重新翻译 · 当前显示上一版，导出仍为上一版", web_view)
        notice.setObjectName("layoutRetranslationNotice")
        notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        notice.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        notice.setStyleSheet(
            "background: rgba(24, 35, 49, 0.92); color: #f8fafc; "
            "border: 1px solid rgba(148, 163, 184, 0.72); border-radius: 8px; "
            "padding: 7px 12px; font-size: 12px;"
        )
        self._layout_retranslation_notices[id(web_view)] = notice
        self.position_layout_retranslation_notice(web_view)
        notice.show()
        notice.raise_()

    def clear_layout_retranslation_notice(self, web_view) -> None:
        if not web_view:
            return
        notice = self._layout_retranslation_notices.pop(id(web_view), None)
        if notice:
            notice.hide()
            notice.deleteLater()

    def clear_all_layout_retranslation_notices(self) -> None:
        for notice in list(self._layout_retranslation_notices.values()):
            notice.hide()
            notice.deleteLater()
        self._layout_retranslation_notices.clear()

    def clear_layout_transition_overlay(self, web_view, generation: int | None = None) -> None:
        if not web_view:
            return
        key = id(web_view)
        state = self._layout_transition_states.get(key)
        if not state:
            return
        if generation is not None and state.get("generation") != generation:
            return
        self._layout_transition_states.pop(key, None)
        overlay = state.get("overlay")
        if overlay:
            overlay.hide()
            overlay.deleteLater()

    def clear_all_layout_transition_overlays(self) -> None:
        for state in list(self._layout_transition_states.values()):
            overlay = state.get("overlay")
            if overlay:
                overlay.hide()
                overlay.deleteLater()
        self._layout_transition_states.clear()

    def begin_layout_transition_overlay(self, web_view) -> int | None:
        """Freeze the last visible reader frame while the next layout fits.

        The fixed-layout HTML intentionally hides pages until its whole-document
        collision pass has completed.  Replacing that HTML-level safeguard
        would make PDF and Word output vulnerable to an unfinished layout.
        Instead, retain a Qt-only frame above the visible reader until the new
        document explicitly reports its ready state.
        """
        if not web_view or not web_view.isVisible() or web_view.width() < 2 or web_view.height() < 2:
            return None
        self.clear_layout_transition_overlay(web_view)
        self._layout_transition_generation += 1
        generation = self._layout_transition_generation
        snapshot = web_view.grab()
        overlay = QWidget(web_view)
        overlay.setObjectName("layoutTransitionOverlay")
        overlay.setStyleSheet("QWidget#layoutTransitionOverlay { background: #f6f3ee; }")
        image = QLabel(overlay)
        image.setScaledContents(True)
        if not snapshot.isNull():
            image.setPixmap(snapshot)
        else:
            image.setStyleSheet("background: #f6f3ee;")
        badge = QLabel("正在整理排版，完成后自动切换", overlay)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(
            "background: rgba(24, 35, 49, 0.92); color: #f8fafc; "
            "border: 1px solid rgba(148, 163, 184, 0.72); border-radius: 8px; "
            "padding: 7px 12px; font-size: 12px;"
        )
        self._layout_transition_states[id(web_view)] = {
            "generation": generation,
            "overlay": overlay,
            "image": image,
            "badge": badge,
        }
        self.position_layout_transition_overlay(web_view)
        overlay.show()
        overlay.raise_()
        return generation

    def wait_for_layout_transition_ready(self, web_view, target_url: QUrl, generation: int | None) -> None:
        """Remove the reader overlay only after the newly loaded page is safe."""
        if generation is None or not web_view:
            return

        def clear_if_current():
            self.clear_layout_transition_overlay(web_view, generation)

        def on_load_finished(ok: bool):
            state = self._layout_transition_states.get(id(web_view))
            if not state or state.get("generation") != generation:
                try:
                    web_view.loadFinished.disconnect(on_load_finished)
                except (TypeError, RuntimeError):
                    pass
                return
            if web_view.url().toString() != target_url.toString():
                # A previously queued navigation can finish after a newer
                # request has armed this transition.  It must not dismiss the
                # newer request's retained frame.
                return
            try:
                web_view.loadFinished.disconnect(on_load_finished)
            except (TypeError, RuntimeError):
                pass
            if not ok:
                clear_if_current()
                return
            poll_ready()

        def poll_ready(attempt: int = 0):
            state = self._layout_transition_states.get(id(web_view))
            if not state or state.get("generation") != generation:
                return
            try:
                if web_view.url().toString() != target_url.toString():
                    if attempt >= 150:
                        clear_if_current()
                        return
                    QTimer.singleShot(100, lambda: poll_ready(attempt + 1))
                    return
            except RuntimeError:
                clear_if_current()
                return

            def receive_ready(ready):
                state = self._layout_transition_states.get(id(web_view))
                if not state or state.get("generation") != generation:
                    return
                if ready is True:
                    clear_if_current()
                    return
                # A failed or unusually slow fit should expose the generated
                # page's own diagnostic rather than indefinitely freezing an
                # older reading frame.  This is visual recovery only; no file
                # or export state is changed.
                if attempt >= 150:
                    self.append_log("排版渲染响应稍慢，已显示当前页面状态；导出时将自动等待版面就绪。")
                    clear_if_current()
                    return
                QTimer.singleShot(100, lambda: poll_ready(attempt + 1))

            try:
                web_view.page().runJavaScript(layout_fit_ready_probe_script(), receive_ready)
            except RuntimeError:
                clear_if_current()

        web_view.loadFinished.connect(on_load_finished)
        # A cached 500+ page document can be reader-ready before deferred
        # MathJax lets WebEngine emit loadFinished. Poll the explicit layout
        # readiness contract during loading so the retained-frame overlay does
        # not mask an already usable document.
        QTimer.singleShot(50, poll_ready)

    def load_layout_html_with_transition(self, html_path: Path, web_view, fallback_viewer: QTextBrowser) -> bool:
        """Navigate a visible layout reader without exposing its cold blank state."""
        if not web_view:
            fallback_viewer.setSource(QUrl.fromLocalFile(str(html_path.resolve())))
            self.apply_reader_font_size()
            return False
        target_url = QUrl.fromLocalFile(str(html_path.resolve()))
        # A complete inline fit cache is already the finalized reader frame.
        # Covering it creates a second readiness state that can outlive the
        # document itself (the pure reader has no such extra cover).
        generation = (
            None
            if layout_html_has_complete_disk_fit_cache(html_path)
            else self.begin_layout_transition_overlay(web_view)
        )
        # Arm before navigation.  WebEngine usually emits loadFinished
        # asynchronously, but history restoration can be very fast on a warm
        # document; connecting first removes that small race.
        self.wait_for_layout_transition_ready(web_view, target_url, generation)
        changed = set_or_reload_web_view_url(web_view, target_url)
        if not changed:
            self.clear_layout_transition_overlay(web_view, generation)
        QTimer.singleShot(250, self.apply_reader_font_size)
        QTimer.singleShot(500, self.install_sync_scroll_bridge)
        self.schedule_layout_debug_overlay_update()
        return changed

    def show_markdown_in_view(self, markdown_path: Path, web_view, fallback_viewer: QTextBrowser, prefer_layout: bool = False, strict_fit: bool = False):
        if self.is_suspended_main_preview_target(web_view):
            return
        if web_view is self.source_web_view:
            set_source_pdf_active(self, False)
        if web_view is self.translation_web_view:
            self._translation_live_page_ready = False
            self._translation_live_pending_markdown = ""
        html_path = (
            render_layout_preview_html(
                markdown_path,
                self.append_log,
                strict_fit=strict_fit,
                debug_overlay=False,
            )
            if prefer_layout
            else render_preview_html(markdown_path, self.append_log)
        )
        if prefer_layout:
            upgrade_layout_loading_notice_html(html_path)
        if web_view and html_path and html_path.exists():
            if prefer_layout:
                self.clear_layout_retranslation_notice(web_view)
                self.load_layout_html_with_transition(html_path, web_view, fallback_viewer)
            else:
                self.clear_layout_retranslation_notice(web_view)
                self.clear_layout_transition_overlay(web_view)
                set_or_reload_web_view_url(web_view, QUrl.fromLocalFile(str(html_path)))
                QTimer.singleShot(250, self.apply_reader_font_size)
            # 主界面会复用 WebEngine 历史页；补做 MathJax 启动确认，避免
            # 延迟脚本尚未就绪时保留 Pandoc 输出的原始 TeX。
            QTimer.singleShot(250, lambda: ensure_web_view_mathjax_typeset(web_view))
            QTimer.singleShot(500, self.install_sync_scroll_bridge)
            QTimer.singleShot(1200, self.install_sync_scroll_bridge)
            if prefer_layout:
                self.schedule_layout_debug_overlay_update()
        elif html_path and html_path.exists():
            fallback_viewer.setSource(QUrl.fromLocalFile(str(html_path)))
            self.apply_reader_font_size()
        else:
            markdown_text = markdown_path.read_text(encoding="utf-8", errors="replace")
            if web_view:
                escaped = html.escape(markdown_text)
                web_view.setHtml(
                    f"""
                    <!doctype html>
                    <html>
                    <head>
                    <meta charset="utf-8">
                    <style>{bundled_reader_font_face_css()}</style>
                    </head>
                    <body style="font-family:{READER_SERIF_FONT_STACK};padding:18px;color:#222;line-height:1.6;">
                    <pre style="white-space:pre-wrap;word-break:break-word;font-family:{READER_SERIF_FONT_STACK};">{escaped}</pre>
                    </body></html>
                    """,
                    QUrl.fromLocalFile(str(markdown_path.parent.resolve())),
                )
            else:
                fallback_viewer.setSearchPaths([str(markdown_path.parent)])
                fallback_viewer.setMarkdown(markdown_text)
            self.apply_reader_font_size()

    def show_html_path(self, html_path: Path, web_view, fallback_viewer: QTextBrowser):
        if self.is_suspended_main_preview_target(web_view):
            return
        if web_view is self.translation_web_view:
            self._translation_live_page_ready = False
            self._translation_live_pending_markdown = ""
        if web_view:
            self.clear_layout_retranslation_notice(web_view)
            self.clear_layout_transition_overlay(web_view)
            set_or_reload_web_view_url(web_view, QUrl.fromLocalFile(str(html_path.resolve())))
            QTimer.singleShot(250, self.apply_reader_font_size)
            QTimer.singleShot(500, self.install_sync_scroll_bridge)
        else:
            fallback_viewer.setSource(QUrl.fromLocalFile(str(html_path.resolve())))
            self.apply_reader_font_size()

    def show_html_in_view(self, html_path: Path, web_view, fallback_viewer: QTextBrowser):
        if self.is_suspended_main_preview_target(web_view):
            return
        if web_view is self.source_web_view:
            set_source_pdf_active(self, False)
        upgrade_layout_loading_notice_html(html_path)
        if web_view:
            if is_layout_preview_html_path(html_path):
                self.clear_layout_retranslation_notice(web_view)
                self.load_layout_html_with_transition(html_path, web_view, fallback_viewer)
            else:
                self.clear_layout_retranslation_notice(web_view)
                self.clear_layout_transition_overlay(web_view)
                set_or_reload_web_view_url(web_view, QUrl.fromLocalFile(str(html_path.resolve())))
                QTimer.singleShot(500, self.install_sync_scroll_bridge)
                self.schedule_layout_debug_overlay_update()
        else:
            fallback_viewer.setSource(QUrl.fromLocalFile(str(html_path.resolve())))
        QTimer.singleShot(250, self.apply_reader_font_size)

    def can_show_layout_paired_canvas(self) -> bool:
        # Keep source-mode changes isolated to the source pane.  The paired
        # canvas embeds source and translation into one HTML document, so toggling
        # "显示解析文件" changes the translation viewport and fit calculations.
        return False

    def layout_paired_source_page_path(self) -> Path | None:
        if not self.current_source_path:
            return None
        if self.show_parsed_source_check.isChecked():
            source_layout_path = render_layout_preview_html(
                self.current_source_path,
                self.append_log,
                strict_fit=True,
                debug_overlay=False,
            )
            return source_layout_path if source_layout_path and source_layout_path.exists() else None
        original_path = self.current_original_path
        if original_path and original_path.exists() and original_path.suffix.lower() == ".pdf":
            if self.layout_development_mode_enabled():
                pdf_preview_path = render_original_pdf_debug_preview_html(original_path, self.current_source_path)
            else:
                pdf_preview_path = render_original_pdf_preview_html(original_path)
            return pdf_preview_path if pdf_preview_path and pdf_preview_path.exists() else None
        return None

    def show_layout_paired_canvas(self) -> bool:
        if not self.can_show_layout_paired_canvas():
            return False
        source_page_path = self.layout_paired_source_page_path()
        if not source_page_path or not source_page_path.exists():
            return False
        paired_path = render_paired_layout_preview_html(
            source_page_path,
            self.current_layout_translation_path,
            self.current_source_path,
            debug=False,
        )
        if not paired_path or not paired_path.exists():
            return False
        self._layout_paired_canvas_active = True
        if hasattr(self, "translation_panel"):
            self.translation_panel.setVisible(False)
        if self._sync_poll_timer is not None:
            self._sync_poll_timer.stop()
        set_or_reload_web_view_url(self.source_web_view, QUrl.fromLocalFile(str(paired_path.resolve())))
        QTimer.singleShot(250, self.apply_reader_font_size)
        return True

    def leave_layout_paired_canvas(self):
        if not self._layout_paired_canvas_active:
            return
        self._layout_paired_canvas_active = False
        if hasattr(self, "translation_panel"):
            self.translation_panel.setVisible(True)
        if self._sync_poll_timer is not None:
            self._sync_poll_timer.stop()

    def show_current_translation_for_mode(self):
        if self.settings.layout_reading_mode:
            self.leave_layout_paired_canvas()
            if self.show_layout_paired_canvas():
                self.update_sync_scroll_availability()
                return
            if self.current_layout_translation_path and self.current_layout_translation_path.exists():
                self.show_html_in_view(self.current_layout_translation_path, self.translation_web_view, self.translation_fallback_viewer)
            elif self.live_layout_translation_markdown.strip():
                self.show_live_translation(self.live_layout_translation_markdown, mode="layout")
            else:
                self.show_placeholder(
                    self.translation_web_view,
                    self.translation_fallback_viewer,
                    "暂无排版译文。点击“翻译当前文档”开始生成保留版面的译文。",
                )
            self.update_sync_scroll_availability()
            self.update_translate_button_visibility()
            return
        if self.current_translation_path:
            self.show_markdown_in_view(self.current_translation_path, self.translation_web_view, self.translation_fallback_viewer)
        elif self.live_translation_markdown.strip():
            self.show_live_translation(self.live_translation_markdown, mode="stream")
        else:
            self.show_placeholder(self.translation_web_view, self.translation_fallback_viewer, "暂无译文。点击“翻译当前文档”开始翻译。")
        self.update_sync_scroll_availability()
        self.update_translate_button_visibility()

    def current_mode_has_translation(self) -> bool:
        if self.settings.layout_reading_mode:
            translation_path = self.current_layout_translation_path
            live_translation = self.live_layout_translation_markdown
        else:
            translation_path = self.current_translation_path
            live_translation = self.live_translation_markdown
        return bool(translation_path and translation_path.exists()) or bool(live_translation.strip())

    def offer_translation_for_current_mode(self):
        source = self.current_source_path or self.current_markdown_path
        if not source or not source.exists() or self.current_mode_has_translation():
            return
        if self.is_thread_running(self.translate_worker) or self.running_translate_workers:
            return
        mode_name = "排版模式" if self.settings.layout_reading_mode else "流式模式"
        choice = QMessageBox.question(
            self,
            "当前模式暂无译文",
            f"当前文档在{mode_name}下还没有译文。\n\n是否立即翻译？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if choice == QMessageBox.StandardButton.Yes:
            self.translate_current_doc()

    def update_translate_button_visibility(self):
        if not hasattr(self, "translate_button"):
            return
        has_source = bool(self.current_source_path and self.current_source_path.exists())
        if not has_source:
            self.translate_button.setVisible(False)
            return
        has_translation = bool(
            self.current_layout_translation_path and self.current_layout_translation_path.exists()
        ) if self.settings.layout_reading_mode else bool(
            self.current_translation_path and self.current_translation_path.exists()
        )
        self.translate_button.setVisible(not has_translation)

    def show_source_preview(self):
        if self.is_suspended_main_preview_target():
            return
        if not self.current_source_path:
            self.show_placeholder(self.source_web_view, self.source_fallback_viewer, "暂无原文或解析文件。")
            return
        if self.settings.layout_reading_mode and self.show_parsed_source_check.isChecked():
            if self.show_layout_paired_canvas():
                return
            self.leave_layout_paired_canvas()
            self.show_markdown_in_view(self.current_source_path, self.source_web_view, self.source_fallback_viewer, prefer_layout=True, strict_fit=True)
            self.update_sync_scroll_availability()
            return
        if self.settings.layout_reading_mode and not self.show_parsed_source_check.isChecked():
            if self.show_layout_paired_canvas():
                return
            original_path = self.current_original_path
            self.leave_layout_paired_canvas()
            if not original_path or not original_path.exists():
                self.stop_source_preview_worker()
                self._source_preview_generation += 1
                self.show_placeholder(self.source_web_view, self.source_fallback_viewer, "原始文件缺失，无法显示原始文件。")
                return
            source_path = original_path
            if self.layout_development_mode_enabled() and source_path.suffix.lower() == ".pdf" and self.current_source_path:
                debug_pdf_html = render_original_pdf_debug_preview_html(source_path, self.current_source_path)
                if debug_pdf_html and debug_pdf_html.exists():
                    self.apply_source_preview_payload("url", debug_pdf_html.resolve().as_uri())
                    return
            if source_path.suffix.lower() == ".pdf" and load_source_pdf(self, source_path):
                self.stop_source_preview_worker()
                self._source_preview_generation += 1
                self.update_sync_scroll_availability()
                return
            set_source_pdf_active(self, False)
            cached = self.cached_source_preview_payload(source_path, self.current_source_path, preview_tools.PreviewMode.ORIGINAL, prefer_layout=False)
            if cached:
                self.apply_source_preview_payload(*cached)
                return
            self._source_preview_generation += 1
            generation = self._source_preview_generation
            self.show_placeholder(self.source_web_view, self.source_fallback_viewer, "正在加载原始文件，请稍候...")
            self.stop_source_preview_worker()
            worker = PreviewRenderWorker(
                generation,
                str(source_path),
                str(self.current_source_path),
                preview_tools.PreviewMode.ORIGINAL.value,
                False,
            )
            self.source_preview_worker = worker
            worker.finished_signal.connect(self.on_source_preview_ready)
            worker.finished.connect(lambda: self.clear_source_preview_worker(worker))
            worker.finished.connect(worker.deleteLater)
            worker.start()
            return
        if self.settings.layout_reading_mode:
            if self.show_layout_paired_canvas():
                return
            self.leave_layout_paired_canvas()
        else:
            self.leave_layout_paired_canvas()
        if self.settings.layout_reading_mode:
            self.show_layout_restoration_check.blockSignals(True)
            self.show_layout_restoration_check.setChecked(True)
            self.show_layout_restoration_check.blockSignals(False)
        mode = preview_tools.PreviewMode.PARSED if self.show_parsed_source_check.isChecked() else preview_tools.PreviewMode.ORIGINAL
        prefer_layout = bool(self.show_parsed_source_check.isChecked() and self.show_layout_restoration_check.isChecked())
        source_path = self.current_original_path or self.current_source_path
        if (
            mode == preview_tools.PreviewMode.ORIGINAL
            and source_path
            and source_path.suffix.lower() == ".pdf"
            and not self.layout_development_mode_enabled()
            and load_source_pdf(self, source_path)
        ):
            self.stop_source_preview_worker()
            self._source_preview_generation += 1
            self.update_sync_scroll_availability()
            return
        set_source_pdf_active(self, False)
        cached = self.cached_source_preview_payload(source_path, self.current_source_path, mode, prefer_layout=prefer_layout)
        if cached:
            self.apply_source_preview_payload(*cached)
            return
        self._source_preview_generation += 1
        generation = self._source_preview_generation
        self.show_placeholder(self.source_web_view, self.source_fallback_viewer, "正在生成预览，请稍候...")
        self.stop_source_preview_worker()
        worker = PreviewRenderWorker(
            generation,
            str(source_path),
            str(self.current_source_path) if self.current_source_path else "",
            mode.value,
            prefer_layout,
        )
        self.source_preview_worker = worker
        worker.finished_signal.connect(self.on_source_preview_ready)
        worker.finished.connect(lambda: self.clear_source_preview_worker(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def is_thread_running(self, worker: QThread | None) -> bool:
        if not worker:
            return False
        try:
            return bool(worker.isRunning())
        except RuntimeError:
            return False

    def stop_source_preview_worker(self):
        worker = self.source_preview_worker
        if not worker:
            return
        try:
            if worker.isRunning():
                worker.requestInterruption()
        except RuntimeError:
            self.source_preview_worker = None

    def clear_source_preview_worker(self, worker):
        if self.source_preview_worker is worker:
            self.source_preview_worker = None

    def cached_source_preview_payload(
        self,
        source_path: Path,
        parsed_markdown: Path | None,
        mode: PreviewMode,
        prefer_layout: bool = False,
    ) -> tuple[str, str | Path] | None:
        if mode == preview_tools.PreviewMode.PARSED and parsed_markdown and parsed_markdown.exists():
            if prefer_layout:
                html_path = layout_preview_html_path(
                    parsed_markdown,
                    debug_overlay=False,
                )
                bundle = load_layout_preview_bundle(parsed_markdown)
                dependencies = [parsed_markdown, Path(__file__).resolve()]
                if bundle:
                    dependencies.append(bundle["layout_path"])
                    if bundle.get("content_path"):
                        dependencies.append(bundle["content_path"])
                    if bundle.get("model_path"):
                        dependencies.append(bundle["model_path"])
                if bundle and multi_file_cache_is_fresh(html_path, dependencies):
                    return ("url", html_path.resolve().as_uri())
            else:
                safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", parsed_markdown.stem)
                html_path = parsed_markdown.with_name(f"preview.{safe_stem}.html")
                if preview_tools.polished_preview_cache_is_fresh(
                    html_path,
                    preview_tools.preview_html_dependencies(parsed_markdown),
                ):
                    return ("url", html_path.resolve().as_uri())
            return None
        if not source_path or not source_path.exists():
            return None
        suffix = source_path.suffix.lower()
        if suffix == ".pdf":
            pdf_html = render_original_pdf_preview_html(source_path)
            if pdf_html and pdf_html.exists():
                return ("url", pdf_html.resolve().as_uri())
            return ("url", source_path.resolve().as_uri())
        if suffix in {".html", ".htm"}:
            return ("url", source_path.resolve().as_uri())
        if suffix in IMAGE_SUFFIXES:
            return ("html", simple_file_html(source_path))
        if suffix in PANDOC_OFFICE_SUFFIXES:
            out = source_path.with_name(f"original.{re.sub(r'[^A-Za-z0-9_.-]+', '_', source_path.stem)}.html")
            if html_preview_cache_is_fresh(out, source_path):
                return ("url", out.resolve().as_uri())
            return None
        return ("html", simple_file_html(source_path))

    def on_source_preview_ready(self, generation: int, kind: str, payload: str, error: str):
        if generation != self._source_preview_generation:
            return
        if error:
            self.append_log(f"外部预览渲染异常，已切换为内置预览：{error}")
        self.apply_source_preview_payload(kind, payload)

    def source_payload_is_debug_parse(self, kind: str, payload) -> bool:
        if not (self.settings.layout_reading_mode and not self.show_parsed_source_check.isChecked()):
            return False
        payload_text = str(payload or "")
        if kind == "markdown":
            return True
        if kind == "html":
            return "layout-translation-preview" in payload_text or "layout-preview" in payload_text
        if kind != "url":
            return False
        if "preview_layout_paired_current." in payload_text:
            return not self.sync_scroll_check.isChecked()
        if "preview_layout_current." in payload_text or "preview_layout_current_debug." in payload_text:
            return True
        if self.current_source_path:
            parsed_preview_uri = self.current_source_path.with_name(
                f"preview.{re.sub(r'[^A-Za-z0-9_.-]+', '_', self.current_source_path.stem)}.html"
            ).resolve().as_uri()
            source_uri = self.current_source_path.resolve().as_uri()
            if payload_text in {source_uri, parsed_preview_uri} or payload_text.endswith(self.current_source_path.name):
                return True
        return False

    def source_url_is_debug_parse(self, url_text: str) -> bool:
        if not (self.settings.layout_reading_mode and not self.show_parsed_source_check.isChecked()):
            return False
        if not url_text:
            return False
        if "preview_layout_paired_current." in url_text:
            return not self.sync_scroll_check.isChecked()
        if "preview_layout_current." in url_text or "preview_layout_current_debug." in url_text:
            return True
        if self.current_source_path:
            parsed_preview_uri = self.current_source_path.with_name(
                f"preview.{re.sub(r'[^A-Za-z0-9_.-]+', '_', self.current_source_path.stem)}.html"
            ).resolve().as_uri()
            source_uri = self.current_source_path.resolve().as_uri()
            return url_text in {source_uri, parsed_preview_uri} or url_text.endswith(self.current_source_path.name)
        return False

    def enforce_source_debug_contract(self):
        if not self.source_web_view:
            return
        if not self.source_url_is_debug_parse(self.source_web_view.url().toString()):
            return
        self.append_log("已自动切换至原始文件视图。")
        self.stop_source_preview_worker()
        self._source_preview_generation += 1
        QTimer.singleShot(0, self.show_source_preview)

    def apply_source_preview_payload(self, kind: str, payload):
        if self.is_suspended_main_preview_target():
            return
        set_source_pdf_active(self, False)
        if self.source_payload_is_debug_parse(kind, payload):
            self.append_log("已自动切回原始文件预览。")
            self.show_source_preview()
            return
        if kind == "url":
            if self.source_web_view:
                source_url = qurl_from_payload(str(payload))
                source_path = Path(source_url.toLocalFile()) if source_url.isLocalFile() else None
                if source_path and is_layout_preview_html_path(source_path):
                    self.load_layout_html_with_transition(
                        source_path,
                        self.source_web_view,
                        self.source_fallback_viewer,
                    )
                    return
                self.clear_layout_transition_overlay(self.source_web_view)
                set_or_reload_web_view_url(self.source_web_view, source_url)
                QTimer.singleShot(250, self.apply_reader_font_size)
                QTimer.singleShot(250, lambda: ensure_web_view_mathjax_typeset(self.source_web_view))
                QTimer.singleShot(500, self.install_sync_scroll_bridge)
                self.schedule_layout_debug_overlay_update()
            else:
                self.source_fallback_viewer.setSource(qurl_from_payload(str(payload)))
                self.apply_reader_font_size()
        elif kind == "markdown":
            path = Path(payload)
            self.show_markdown_in_view(path, self.source_web_view, self.source_fallback_viewer)
        else:
            if self.source_web_view:
                self.clear_layout_transition_overlay(self.source_web_view)
                self.source_web_view.setHtml(str(payload), QUrl.fromLocalFile(str(WORKSPACE)))
                QTimer.singleShot(250, self.apply_reader_font_size)
                QTimer.singleShot(500, self.install_sync_scroll_bridge)
                self.schedule_layout_debug_overlay_update()
            else:
                self.source_fallback_viewer.setHtml(str(payload))
                self.apply_reader_font_size()
        self.update_sync_scroll_availability()

    def on_source_preview_mode_changed(self, checked: bool):
        was_paired = self._layout_paired_canvas_active
        self.set_current_mode_show_parsed_source(checked)
        self.stop_source_preview_worker()
        self._source_preview_generation += 1
        if self.settings.layout_reading_mode and not checked:
            self.reset_sync_page_scaling()
            self.show_placeholder(self.source_web_view, self.source_fallback_viewer, "正在切换回原始文件...")
        app_config.save_settings(self.settings)
        self.show_source_preview()
        if self.settings.layout_reading_mode:
            if was_paired:
                self.show_current_translation_for_mode()
            self.schedule_layout_debug_overlay_update(delay_ms=700)
        self.update_sync_scroll_availability()

    def layout_development_mode_enabled(self) -> bool:
        return bool(self.settings.layout_reading_mode and getattr(self.settings, "layout_development_mode", False))

    def schedule_layout_debug_overlay_update(self, delay_ms: int = 550):
        if not self.settings.layout_reading_mode:
            return
        enabled = self.layout_development_mode_enabled()
        QTimer.singleShot(delay_ms, lambda: apply_layout_debug_mode_to_web_view(self.source_web_view, enabled))
        QTimer.singleShot(delay_ms, lambda: apply_layout_debug_mode_to_web_view(self.translation_web_view, enabled))

    def on_layout_development_mode_toggled(self, checked: bool):
        self.settings.layout_development_mode = bool(checked)
        app_config.save_settings(self.settings)
        if self.settings.layout_reading_mode:
            self.schedule_layout_debug_overlay_update(delay_ms=50)

    def on_layout_restoration_toggled(self, checked: bool):
        if checked and bool(getattr(self, "_current_document_is_epub", False)):
            self.show_layout_restoration_check.blockSignals(True)
            self.show_layout_restoration_check.setChecked(False)
            self.show_layout_restoration_check.blockSignals(False)
            return
        self.settings.show_layout_restoration = checked
        if checked:
            self.settings.layout_reading_mode = True
            if hasattr(self, "layout_reading_mode_button"):
                self.layout_reading_mode_button.blockSignals(True)
                self.layout_reading_mode_button.setChecked(True)
                self.update_layout_mode_button_text()
                self.layout_reading_mode_button.blockSignals(False)
        app_config.save_settings(self.settings)
        self.show_source_preview()
        self.update_sync_scroll_availability()

    def update_layout_mode_button_text(self):
        if not hasattr(self, "layout_reading_mode_button"):
            return
        self.layout_reading_mode_button.blockSignals(True)
        self.layout_reading_mode_button.set_layout_mode(bool(self.settings.layout_reading_mode))
        self.layout_reading_mode_button.blockSignals(False)

    def update_layout_mode_controls(self):
        is_epub = bool(getattr(self, "_current_document_is_epub", False))
        if is_epub and self.settings.layout_reading_mode:
            self.settings.layout_reading_mode = False
            app_config.save_settings(self.settings)
        is_layout = bool(self.settings.layout_reading_mode)
        if hasattr(self, "layout_reading_mode_button"):
            self.layout_reading_mode_button.setEnabled(not is_epub)
            self.layout_reading_mode_button.setToolTip(
                "EPUB 仅支持流式阅读。" if is_epub else ""
            )
            self.update_layout_mode_button_text()
        if hasattr(self, "reader_font_spin"):
            self.reader_font_spin.blockSignals(True)
            self.reader_font_spin.setRange(
                LAYOUT_BODY_FONT_MIN_PT if is_layout else READER_FONT_MIN_PT,
                LAYOUT_BODY_FONT_MAX_PT if is_layout else READER_FONT_MAX_PT,
            )
            self.reader_font_spin.setValue(
                (
                    self.current_layout_body_font_pt()
                    or self._detected_layout_body_font_pt
                    or self.reader_font_spin.value()
                )
                if is_layout
                else self.settings.reader_font_pt
            )
            self.reader_font_spin.blockSignals(False)
        if hasattr(self, "show_parsed_source_check"):
            self.show_parsed_source_check.setText("显示解析文件")
            self.show_parsed_source_check.setVisible(not is_layout)
        if hasattr(self, "layout_development_check"):
            self.layout_development_check.setVisible(False)
            self.layout_development_check.blockSignals(True)
            self.layout_development_check.setChecked(bool(getattr(self.settings, "layout_development_mode", False)))
            self.layout_development_check.blockSignals(False)
        if hasattr(self, "sync_scroll_check"):
            self.sync_scroll_check.setVisible(False)
        self.sync_preview_toolbar_heights()

    def sync_preview_toolbar_heights(self):
        """Keep the source and translation preview headers on the same baseline.

        The source header contains mode-dependent controls.  Its natural height
        therefore changes when switching between stream and layout reading,
        while the translation header only contains a label.  Releasing the
        previous fixed height before measuring makes the result reflect the
        current visibility state rather than the height from the last mode.
        """
        toolbar_widgets = (
            getattr(self, "source_toolbar_widget", None),
            getattr(self, "translation_toolbar_widget", None),
        )
        if any(widget is None or widget.layout() is None for widget in toolbar_widgets):
            return

        for widget in toolbar_widgets:
            widget.setMinimumHeight(0)
            widget.setMaximumHeight(QWIDGETSIZE_MAX)
            layout = widget.layout()
            layout.invalidate()
            layout.activate()
            widget.updateGeometry()

        shared_height = max(widget.sizeHint().height() for widget in toolbar_widgets)
        shared_height = max(1, int(shared_height))
        for widget in toolbar_widgets:
            widget.setFixedHeight(shared_height)
            widget.updateGeometry()

    def current_mode_show_parsed_source(self) -> bool:
        return (
            bool(getattr(self.settings, "layout_show_parsed_source", False))
            if self.settings.layout_reading_mode
            else bool(getattr(self.settings, "stream_show_parsed_source", self.settings.show_parsed_source))
        )

    def set_current_mode_show_parsed_source(self, checked: bool):
        checked = bool(checked)
        if self.settings.layout_reading_mode:
            self.settings.layout_show_parsed_source = checked
        else:
            self.settings.stream_show_parsed_source = checked
        self.settings.show_parsed_source = checked

    def current_mode_scroll_key(self) -> str:
        if not self.current_source_path:
            return ""
        mode = "layout" if self.settings.layout_reading_mode else "stream"
        return f"{str(self.current_source_path.resolve())}|{mode}"

    def _connect_scroll_signal(self):
        """Connect scrollPositionChanged on the source web view page.

        Uses a direct Qt signal (no JS round-trip) so the position is always
        accurate and persisted synchronously.
        """
        wv = getattr(self, "source_web_view", None)
        if wv is self._scroll_signal_connected_view:
            return  # already wired
        # Disconnect old connection if any
        old_wv = self._scroll_signal_connected_view
        if old_wv is not None:
            try:
                old_wv.page().scrollPositionChanged.disconnect(self._on_scroll_position_changed)
            except Exception:
                pass
        self._scroll_signal_connected_view = wv
        if wv is None:
            return
        try:
            wv.page().scrollPositionChanged.connect(self._on_scroll_position_changed)
        except Exception:
            pass

    def _on_scroll_position_changed(self, pos):
        """Called by Qt whenever the page scrolls — no JS, no race condition."""
        key = self.current_mode_scroll_key()
        if not key:
            return
        # Ignore position changes during the suppression window (page is
        # still loading and the browser may emit spurious scroll-to-top).
        import time as _time
        if _time.monotonic() < getattr(self, "_scroll_capture_suppressed_until", 0):
            return
        wv = getattr(self, "source_web_view", None)
        if wv is None:
            return
        top = float(pos.y())
        # Guard: don't overwrite a meaningful position with 0 from a loading page.
        existing = self._mode_scroll_positions.get(key)
        if existing and top == 0 and (existing.get("top") or 0) > 2:
            return
        # Compute ratio via JS (scrollHeight only available in JS) — but only
        # if we need it for legacy compat. For restoration we use top directly.
        # Use a best-effort ratio = 0 when scrollHeight unknown; restore prefers top.
        position = {"top": max(0.0, top), "ratio": 0.0}
        # Try to compute ratio asynchronously; update silently if available.
        def _update_ratio(payload):
            try:
                import json as _json
                data = _json.loads(payload) if isinstance(payload, str) else (payload or {})
                ratio = float((data or {}).get("ratio") or 0)
                if ratio > 0:
                    position["ratio"] = max(0.0, min(1.0, ratio))
            except Exception:
                pass
            if self._mode_scroll_positions.get(key) != position:
                self._mode_scroll_positions[key] = dict(position)
                self.settings.reader_scroll_positions = dict(self._mode_scroll_positions)
                app_config.save_settings(self.settings)
        try:
            script = (
                "(() => { const r = document.scrollingElement || document.documentElement;"
                " const max = Math.max(1, r.scrollHeight - window.innerHeight);"
                " return JSON.stringify({ratio: r.scrollTop / max}); })()"
            )
            wv.page().runJavaScript(script, _update_ratio)
        except Exception:
            # If JS fails, persist top alone immediately.
            if self._mode_scroll_positions.get(key) != position:
                self._mode_scroll_positions[key] = dict(position)
                self.settings.reader_scroll_positions = dict(self._mode_scroll_positions)
                app_config.save_settings(self.settings)

    def capture_current_scroll_state(self):
        """Legacy periodic capture — kept as a fallback for views that may not
        emit scrollPositionChanged (e.g. while scroll signal is not yet wired)."""
        key = self.current_mode_scroll_key()
        if not key or not self.source_web_view:
            return
        import time as _time
        if _time.monotonic() < getattr(self, "_scroll_capture_suppressed_until", 0):
            return
        script = """
        (() => {
          const root = document.scrollingElement || document.documentElement;
          const max = Math.max(1, Number(root.scrollHeight || 0) - Number(window.innerHeight || 0));
          return JSON.stringify({ratio: Number(root.scrollTop || 0) / max, top: Number(root.scrollTop || 0)});
        })();
        """

        def store(payload):
            try:
                ratio = float((decode_web_javascript_payload(payload) or {}).get("ratio") or 0)
            except Exception:
                ratio = 0.0
            data = decode_web_javascript_payload(payload) or {}
            top = float(data.get("top") or 0)
            position = {
                "ratio": max(0.0, min(1.0, ratio)),
                "top": max(0.0, top),
            }
            existing = self._mode_scroll_positions.get(key)
            if existing and top == 0 and ratio == 0 and (existing.get("top") or 0) > 2:
                return
            if self._mode_scroll_positions.get(key) != position:
                self._mode_scroll_positions[key] = position
                self.settings.reader_scroll_positions = dict(self._mode_scroll_positions)
                app_config.save_settings(self.settings)

        self.source_web_view.page().runJavaScript(script, store)

    def restore_current_scroll_state(self):
        key = self.current_mode_scroll_key()
        if not key or key not in self._mode_scroll_positions or not self.source_web_view:
            self._scroll_capture_suppressed_until = 0
            return
        top = float(self._mode_scroll_positions.get(key, {}).get("top") or 0)
        ratio = float(self._mode_scroll_positions.get(key, {}).get("ratio") or 0)
        # Restore using absolute top when available; fall back to ratio.
        script = f"""
        (() => {{
          const root = document.scrollingElement || document.documentElement;
          const max = Math.max(0, Number(root.scrollHeight || 0) - Number(window.innerHeight || 0));
          const rememberedTop = {top:.3f};
          const target = rememberedTop > 0 ? Math.min(rememberedTop, max) : max * {ratio:.8f};
          root.scrollTop = target;
        }})();
        """
        self.source_web_view.page().runJavaScript(script)
        self._scroll_capture_suppressed_until = 0

    def on_layout_reading_mode_toggled(self, checked: bool):
        if checked and bool(getattr(self, "_current_document_is_epub", False)):
            self.layout_reading_mode_button.blockSignals(True)
            self.layout_reading_mode_button.setChecked(False)
            self.layout_reading_mode_button.blockSignals(False)
            self.update_layout_mode_button_text()
            QMessageBox.information(self, "EPUB 仅支持流式模式", "EPUB 是可重排电子书，暂不支持排版模式。")
            return
        previous_layout_mode = bool(self.settings.layout_reading_mode)
        checked = bool(checked)
        self.capture_current_scroll_state()
        self.reset_sync_scroll_runtime()
        self.set_current_mode_show_parsed_source(self.show_parsed_source_check.isChecked())
        if not self.settings.layout_reading_mode:
            self._stream_sync_scroll = bool(self.sync_scroll_check.isChecked())
            self.settings.stream_sync_scroll = self._stream_sync_scroll
        if not checked:
            self.leave_layout_paired_canvas()
        self.settings.layout_reading_mode = checked
        if checked:
            self.settings.sync_scroll = True
        else:
            self.settings.sync_scroll = bool(self._stream_sync_scroll)
        self.settings.show_parsed_source = self.current_mode_show_parsed_source()
        self.settings.show_layout_restoration = checked
        self.update_layout_mode_button_text()
        self.update_layout_mode_controls()
        self.show_parsed_source_check.blockSignals(True)
        self.show_layout_restoration_check.blockSignals(True)
        self.sync_scroll_check.blockSignals(True)
        self.show_parsed_source_check.setChecked(self.current_mode_show_parsed_source())
        if checked:
            self.sync_scroll_check.setChecked(True)
        else:
            self.sync_scroll_check.setChecked(bool(self._stream_sync_scroll))
        self.show_layout_restoration_check.setChecked(checked)
        self.show_parsed_source_check.blockSignals(False)
        self.show_layout_restoration_check.blockSignals(False)
        self.sync_scroll_check.blockSignals(False)
        app_config.save_settings(self.settings)
        self.show_source_preview()
        if self.current_source_path or self.current_markdown_path:
            self.show_current_translation_for_mode()
        if checked:
            self.ensure_current_layout_translation_preview()
        self.update_sync_scroll_availability()
        import time as _time
        self._scroll_capture_suppressed_until = _time.monotonic() + 2.0
        QTimer.singleShot(650, self.restore_current_scroll_state)
        QTimer.singleShot(1250, self.restore_current_scroll_state)
        QTimer.singleShot(2500, self.restore_current_scroll_state)
        self.update_translate_button_visibility()
        if previous_layout_mode != checked:
            self.offer_translation_for_current_mode()

    def update_sync_scroll_availability(self):
        translation_available = (
            bool(self.current_layout_translation_path or self.live_layout_translation_markdown.strip())
            if self.settings.layout_reading_mode
            else bool(self.current_translation_path or self.live_translation_markdown.strip())
        )
        if self.settings.layout_reading_mode:
            self.sync_scroll_check.blockSignals(True)
            self.sync_scroll_check.setChecked(True)
            self.sync_scroll_check.setEnabled(True)
            self.sync_scroll_check.setVisible(False)
            self.sync_scroll_check.blockSignals(False)
            self.settings.sync_scroll = True
            self.sync_preview_toolbar_heights()
            return
        source_sync_available = (
            self.show_parsed_source_check.isChecked()
        )
        enabled = (
            source_sync_available
            and self.current_source_path is not None
            and translation_available
            and self.source_web_view is not None
            and self.translation_web_view is not None
        )
        self.sync_scroll_check.setVisible(False)
        self.sync_scroll_check.setEnabled(enabled)
        if not enabled:
            # 译文重建期间保留流式同步偏好，完成后控件会按原状态自动恢复可用。
            self.sync_scroll_check.blockSignals(True)
            self.sync_scroll_check.setChecked(bool(self._stream_sync_scroll))
            self.sync_scroll_check.blockSignals(False)
        self.sync_preview_toolbar_heights()

    def on_sync_scroll_toggled(self, checked: bool):
        if self.settings.layout_reading_mode:
            self.sync_scroll_check.blockSignals(True)
            self.sync_scroll_check.setChecked(True)
            self.sync_scroll_check.blockSignals(False)
            self.settings.sync_scroll = True
            app_config.save_settings(self.settings)
            self.show_layout_paired_canvas()
            return
        self.settings.sync_scroll = checked
        self.settings.stream_sync_scroll = checked
        self._stream_sync_scroll = checked
        app_config.save_settings(self.settings)
        if checked:
            self.install_sync_scroll_bridge()
            QTimer.singleShot(120, self.sync_translation_to_source_now)
        else:
            was_paired = self._layout_paired_canvas_active
            self.leave_layout_paired_canvas()
            self.reset_sync_page_scaling()
            if was_paired:
                self.show_source_preview()
                self.show_current_translation_for_mode()

    def reset_sync_page_scaling(self):
        script = """
        (() => {
          window.__mineruForcedPageMetrics = null;
          if (window.__mineruFitLayoutPages) window.__mineruFitLayoutPages();
        })();
        """
        self._run_sync_javascript(self.source_web_view, script)
        self._run_sync_javascript(self.translation_web_view, script)

    def sync_translation_to_source_now(self):
        if (
            not self.sync_scroll_check.isChecked()
            or not self.source_web_view
            or not self.translation_web_view
            or (not self.settings.layout_reading_mode and not self.show_parsed_source_check.isChecked())
        ):
            return
        if pdf_view_is_active(self):
            payload = pdf_sync_payload(self.source_pdf_view)
            if payload:
                self.apply_sync_payload_to_target(self.translation_web_view, payload)
            return
        script = """
        (() => {
          const api = window.syncScrollApi;
          if (!api) return null;
          const payload = api.syncPayload ? api.syncPayload() : {
            ratio: api.scrollRatio(),
            heading: api.currentHeadingPosition ? api.currentHeadingPosition() : null
          };
          return JSON.stringify(payload);
        })();
        """
        generation = self._sync_poll_generation

        def apply_payload(payload):
            payload = decode_web_javascript_payload(payload)
            if generation != self._sync_poll_generation or not payload:
                return
            self.apply_sync_payload_to_target(self.translation_web_view, payload)

        self._run_sync_javascript(self.source_web_view, script, apply_payload)

    @staticmethod
    def _sync_web_view_page(web_view):
        if web_view is None:
            return None
        try:
            page = web_view.page()
            if page is None:
                return None
            page.url()
            return page
        except RuntimeError:
            return None

    def _run_sync_javascript(self, web_view, script: str, callback=None) -> bool:
        page = self._sync_web_view_page(web_view)
        if page is None:
            return False
        try:
            if callback is None:
                page.runJavaScript(script)
            else:
                page.runJavaScript(script, callback)
            return True
        except RuntimeError:
            return False

    def apply_sync_payload_to_target(self, target, payload):
        if target is None or not payload:
            return
        self._syncing_scroll = True
        safe_payload = json.dumps(payload, ensure_ascii=False)
        if not self._run_sync_javascript(
            target,
            f"""
            (() => {{
              const payload = {safe_payload};
              const api = window.syncScrollApi;
              if (!api) return;
              if (api.scrollToSyncPayload && api.scrollToSyncPayload(payload, false)) return;
              if (!api.scrollToHeadingPosition || !api.scrollToHeadingPosition(payload.heading, false)) {{
                api.scrollToRatio(Number(payload.ratio || 0), false);
              }}
            }})();
            """,
        ):
            self._syncing_scroll = False
            return
        QTimer.singleShot(16, lambda: setattr(self, "_syncing_scroll", False))

    def ensure_sync_poll_timer(self):
        if self._sync_poll_timer is not None:
            return
        self._sync_poll_timer = QTimer(self)
        self._sync_poll_timer.setInterval(16)
        self._sync_poll_timer.timeout.connect(self.poll_sync_scroll_bridge)

    def poll_sync_scroll_bridge(self):
        if (
            (not self.settings.layout_reading_mode and self._syncing_scroll)
            or self._sync_poll_inflight
            or not self.sync_scroll_check.isChecked()
            or (not self.settings.layout_reading_mode and not self.show_parsed_source_check.isChecked())
            or not self.source_web_view
            or not self.translation_web_view
        ):
            return
        if pdf_view_is_active(self):
            poll_translation_web_to_pdf(self)
            return

        self._sync_poll_inflight = True
        generation = self._sync_poll_generation
        state = {"pane": "", "userScrollAt": 0, "payload": None}
        pending = {"count": 2}

        def finish_one():
            if generation != self._sync_poll_generation:
                return
            pending["count"] -= 1
            if pending["count"] > 0:
                return
            self._sync_poll_inflight = False
            pane = state["pane"]
            user_scroll_at = int(state["userScrollAt"] or 0)
            payload = state["payload"]
            if not pane or not payload or user_scroll_at <= 0:
                return
            if pane == "source":
                if user_scroll_at <= self._last_source_user_scroll_at:
                    return
                self._last_source_user_scroll_at = user_scroll_at
                self.apply_sync_payload_to_target(self.translation_web_view, payload)
            else:
                if user_scroll_at <= self._last_translation_user_scroll_at:
                    return
                self._last_translation_user_scroll_at = user_scroll_at
                self.apply_sync_payload_to_target(self.source_web_view, payload)

        def handle_result(pane):
            def _inner(result):
                try:
                    result = decode_web_javascript_payload(result) or {}
                    user_scroll_at = int((result or {}).get("userScrollAt") or 0)
                    payload = (result or {}).get("payload")
                    if user_scroll_at > int(state["userScrollAt"] or 0) and payload:
                        state["pane"] = pane
                        state["userScrollAt"] = user_scroll_at
                        state["payload"] = payload
                finally:
                    finish_one()
            return _inner

        script = """
        (() => {
          if (!window.__mineruGetSyncState) return null;
          return JSON.stringify(window.__mineruGetSyncState());
        })();
        """
        def request_state(web_view, callback):
            if not self._run_sync_javascript(web_view, script, callback):
                callback(None)

        request_state(self.source_web_view, handle_result("source"))
        request_state(self.translation_web_view, handle_result("translation"))

    def install_sync_scroll_bridge(self):
        if not self.sync_scroll_check.isChecked() or not self.source_web_view or not self.translation_web_view:
            if self._sync_poll_timer is not None:
                self._sync_poll_timer.stop()
            return
        if not self.settings.layout_reading_mode and not self.show_parsed_source_check.isChecked():
            if self._sync_poll_timer is not None:
                self._sync_poll_timer.stop()
            return
        # The main and focused readers share the same scroll bridge.
        script_template = """
        (() => {
          const bridgeVersion = 2;
          if (window.__mineruSyncInstalled === bridgeVersion) return;
          window.__mineruSyncInstalled = bridgeVersion;
          window.__mineruLastUserScrollAt = 0;
          window.__mineruLastObservedScrollTop = -1;
          window.__mineruGetSyncState = () => {
            const api = window.syncScrollApi;
            if (!api) return null;
            const root = document.scrollingElement || document.documentElement;
            const scrollTop = Number(root ? root.scrollTop : 0);
            const now = Date.now();
            if (Math.abs(scrollTop - Number(window.__mineruLastObservedScrollTop || 0)) > 0.5) {
              if (now >= Number(window.__mineruProgrammaticScrollUntil || 0)) {
                window.__mineruLastUserScrollAt = now;
              }
              window.__mineruLastObservedScrollTop = scrollTop;
            }
            const payload = api.syncPayload ? api.syncPayload() : {
              ratio: api.scrollRatio(),
              heading: api.currentHeadingPosition ? api.currentHeadingPosition() : null
            };
            return {
              userScrollAt: Number(window.__mineruLastUserScrollAt || 0),
              scrollTop,
              payload
            };
          };
          window.addEventListener('scroll', () => {
            if (Date.now() < Number(window.__mineruProgrammaticScrollUntil || 0)) return;
            window.__mineruLastUserScrollAt = Date.now();
          }, { passive: true });
        })();
        """
        if not self.settings.layout_reading_mode:
            script_template = """
            (() => {
              if (window.__mineruSyncInstalled) return;
              window.__mineruSyncInstalled = true;
              window.__mineruLastUserScrollAt = 0;
              window.__mineruGetSyncState = () => {
                const api = window.syncScrollApi;
                if (!api) return null;
                const payload = api.syncPayload ? api.syncPayload() : {
                  ratio: api.scrollRatio(),
                  heading: api.currentHeadingPosition ? api.currentHeadingPosition() : null
                };
                return {
                  userScrollAt: Number(window.__mineruLastUserScrollAt || 0),
                  payload
                };
              };
              window.addEventListener('scroll', () => {
                if (Date.now() < Number(window.__mineruProgrammaticScrollUntil || 0)) return;
                window.__mineruLastUserScrollAt = Date.now();
              }, { passive: true });
            })();
            """
        if not pdf_view_is_active(self):
            self._run_sync_javascript(self.source_web_view, script_template)
        self._run_sync_javascript(self.translation_web_view, script_template)
        self.ensure_sync_poll_timer()
        self._sync_poll_timer.start()
        QTimer.singleShot(40, self.sync_translation_to_source_now)

    def open_reader_mode(self):
        layout_mode = bool(self.settings.layout_reading_mode)
        reader_translation_path = self.current_layout_translation_path if layout_mode else self.current_translation_path
        reader_live_translation = self.live_layout_translation_markdown if layout_mode else self.live_translation_markdown
        if not self.current_source_path and not reader_translation_path and not reader_live_translation.strip():
            QMessageBox.information(self, "暂无可阅读内容", "请先打开一个解析结果，或先开始翻译。")
            return
        suspend_main = not self.reader_windows
        if suspend_main:
            self.suspend_main_preview_for_reader()
        try:
            reader = ReaderWindow(
                self.current_source_path,
                reader_translation_path,
                reader_live_translation,
                self.current_original_path,
                self.show_parsed_source_check.isChecked(),
                self,
            )
        except Exception as exc:
            if suspend_main:
                self.resume_main_preview_after_readers()
            QMessageBox.critical(self, "打开阅读模式失败", str(exc))
            return
        reader.closed.connect(lambda window=reader: self.remove_reader_window(window))
        reader.destroyed.connect(lambda _=None, window=reader: self.remove_reader_window(window))
        self.reader_windows.append(reader)
        reader.show()
        reader.raise_()
        reader.activateWindow()

    def remove_reader_window(self, reader: ReaderWindow):
        if reader in self.reader_windows:
            self.reader_windows.remove(reader)
        if not self.reader_windows:
            self.resume_main_preview_after_readers()

    def install_export_context_menu(self, widget: QWidget, pane: str, owner_window: QWidget | None = None):
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widget.customContextMenuRequested.connect(
            lambda pos, p=pane, w=widget, owner=owner_window: self.show_export_context_menu(p, w, pos, owner)
        )

    def show_export_context_menu(self, pane: str, widget: QWidget, pos, owner_window: QWidget | None = None):
        if WEBENGINE_AVAILABLE and QWebEngineView is not None and isinstance(widget, QWebEngineView):
            script = """
                (() => {
                  const selectedText = window.syncScrollApi
                    ? window.syncScrollApi.selectedText()
                    : String(window.getSelection ? window.getSelection() : '');
                  const el = document.elementFromPoint(%d, %d);
                  const img = el && (el.closest ? el.closest('img') : null);
                  const selection = window.getSelection ? window.getSelection() : null;
                  const range = selection && selection.rangeCount ? selection.getRangeAt(0) : null;
                  const anchorFor = (rect) => {
                    if (!rect) return null;
                    const node = document.elementFromPoint(rect.left + 1, rect.top + 1);
                    const page = node && node.closest ? node.closest('[data-sync-page-index]') : null;
                    const pageRect = page && page.getBoundingClientRect();
                    const root = document.scrollingElement || document.documentElement;
                    return {
                      anchor_page: page ? Number(page.dataset.syncPageIndex || 0) + 1 : null,
                      anchor_ratio: pageRect && pageRect.height ? Math.max(0, Math.min(1, (rect.top - pageRect.top) / pageRect.height)) : null,
                      anchor_rect: pageRect && pageRect.width && pageRect.height ? {
                        x: Math.max(0, Math.min(1, (rect.left - pageRect.left) / pageRect.width)),
                        y: Math.max(0, Math.min(1, (rect.top - pageRect.top) / pageRect.height)),
                        width: Math.max(.01, Math.min(1, rect.width / pageRect.width)),
                        height: Math.max(.01, Math.min(1, rect.height / pageRect.height))
                      } : null,
                      scroll_ratio: root && root.scrollHeight > root.clientHeight ? root.scrollTop / (root.scrollHeight - root.clientHeight) : 0
                    };
                  };
                  const context = {
                    selectedText: selectedText || '',
                    selectionAnchor: anchorFor(range && range.getBoundingClientRect()),
                    image: img ? {
                      src: img.currentSrc || img.src || '',
                      rawSrc: img.getAttribute('src') || '',
                      alt: img.alt || '',
                      title: img.title || '',
                      anchor: anchorFor(img.getBoundingClientRect())
                    } : null
                  };
                  return JSON.stringify(context);
                })();
            """ % (int(pos.x()), int(pos.y()))

            def show_after_probe(payload):
                payload = decode_web_javascript_payload(payload) or {}
                self.show_export_context_menu_with_context(
                    pane,
                    widget,
                    pos,
                    owner_window,
                    selected_text=str(payload.get("selectedText") or ""),
                    image_context=payload.get("image") if isinstance(payload.get("image"), dict) else None,
                    selection_anchor=payload.get("selectionAnchor") if isinstance(payload.get("selectionAnchor"), dict) else None,
                )

            widget.page().runJavaScript(script, show_after_probe)
            return

        selected_text = widget.textCursor().selectedText() if isinstance(widget, QTextBrowser) else ""
        self.show_export_context_menu_with_context(
            pane,
            widget,
            pos,
            owner_window,
            selected_text=selected_text,
            image_context=None,
        )

    def show_export_context_menu_with_context(
        self,
        pane: str,
        widget: QWidget,
        pos,
        owner_window: QWidget | None = None,
        selected_text: str = "",
        image_context: dict | None = None,
        selection_anchor: dict | None = None,
    ):
        menu = QMenu(self)
        title = self.export_content_label(pane)
        # The main and focused readers share one context menu.
        # 重新翻译基于当前解析 Markdown，不依赖右键所在栏位，因此原文/译文栏都提供该入口。
        retranslate_action = menu.addAction("重新翻译")
        menu.addSeparator()
        can_ask_ai = bool(str(selected_text or "").strip() or image_context)
        ask_action = menu.addAction("提问...") if can_ask_ai else None
        # 三种文献 AI 分析任务统一放在右键菜单，不占界面按鈕空间。
        key_points_action = menu.addAction("要点提炼")
        mindmap_action = menu.addAction("思维导图")
        logic_flow_action = menu.addAction("思路流程")
        menu.addSeparator()
        pdf_action = menu.addAction(f"导出{title}为 PDF")
        word_action = menu.addAction(f"导出{title}为 Word")
        html_action = menu.addAction(f"导出{title}为 HTML")
        md_action = menu.addAction(f"导出{title}为 Markdown")
        source_for_export = (
            owner_window.source_path if isinstance(owner_window, ReaderWindow) else self.current_source_path
        )
        epub_action = (
            menu.addAction(f"导出{title}为 EPUB")
            if is_epub_markdown_path(source_for_export)
            else None
        )
        action = menu.exec(widget.mapToGlobal(pos))
        if action == retranslate_action:
            if isinstance(owner_window, ReaderWindow):
                self.confirm_retranslate_from_reader(owner_window)
            else:
                self.confirm_retranslate_current_document()
        elif ask_action is not None and action == ask_action:
            self.ask_ai_from_pane(
                pane,
                widget,
                selected_text=selected_text,
                image_context=image_context,
                selection_anchor=selection_anchor,
                owner_window=owner_window,
            )
        elif action == key_points_action:
            self.submit_document_task_from_pane("key_points", pane, owner_window=owner_window)
        elif action == mindmap_action:
            self.submit_document_task_from_pane("paper_mindmap", pane, owner_window=owner_window)
        elif action == logic_flow_action:
            self.submit_document_task_from_pane("paper_logic_flow", pane, owner_window=owner_window)
        elif action == pdf_action:
            self.export_pane_document(pane, "pdf")
        elif action == word_action:
            # 右键“导出 Word”默认启用 Word 精校；若当前环境不可用，check_word_refine_available 会返回 False。
            # Keep the default actions identical in both reader views.
            self.export_pane_document(
                pane,
                "docx",
                refine_word=check_word_refine_available()[0],
                export_style=resolve_export_style(getattr(self.settings, "export_style", None)),
            )
        elif action == html_action:
            self.export_pane_document(pane, "html")
        elif action == md_action:
            self.export_pane_document(pane, "md")
        elif epub_action is not None and action == epub_action:
            self.export_pane_document(pane, "epub")

    def confirm_retranslate_current_document(self):
        """主界面右键菜单入口：按当前保存的翻译配置重新翻译当前文档。"""
        source = self.current_source_path or self.current_markdown_path
        if not source or not source.exists():
            QMessageBox.information(self, "无法重新翻译", "当前没有可用于重新翻译的解析 Markdown。")
            return
        if self.is_thread_running(self.translate_worker):
            QMessageBox.information(self, "正在翻译", "请等待当前翻译结束或点击停止任务后，再重新翻译。")
            return
        dialog = TranslationOptionsDialog(self, provider_id=self.settings.ai_provider, retranslate=True)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected_ai_config:
            return
        ai_config = dialog.selected_ai_config
        job_config = self.build_saved_translation_job_config(ai_config)
        source_key = str(source.resolve())
        if self.settings.layout_reading_mode:
            is_current_source = bool(
                self.current_source_path
                and self.current_source_path.exists()
                and self.current_source_path.resolve() == source.resolve()
            )
            has_published_preview = bool(
                is_current_source
                and self.current_layout_translation_path
                and self.current_layout_translation_path.exists()
            )
            self.clear_layout_body_font_for_document(source)
            clear_layout_translation_artifacts(
                source,
                job_config.target_language,
                job_config.reference_paths,
                job_config.ai_config.model,
                job_config.ai_config.provider_id,
                preserve_published_preview=True,
            )
            self.live_layout_translation_markdown = ""
            self.live_layout_translation_by_source.pop(source_key, None)
            if has_published_preview:
                self.show_layout_retranslation_notice(self.translation_web_view)
                self.append_log("已开始重新翻译：新译文生成期间，仍可正常阅读和导出当前版本。")
            for reader in list(self.reader_windows):
                if (
                    reader.layout_reading_mode
                    and reader.source_path
                    and reader.source_path.resolve() == source.resolve()
                ):
                    reader.live_translation_markdown = ""
            self.start_layout_translation_job(
                source,
                job_config.ai_config,
                job_config.target_language,
                source_language=job_config.source_language,
                local_machine_parallelism=job_config.local_machine_parallelism,
                reference_paths=job_config.reference_paths,
                translation_mode=job_config.mode,
                force=True,
                preserve_existing_preview=has_published_preview,
            )
            return
        clear_translation_artifacts(
            source,
            job_config.target_language,
            job_config.mode,
            job_config.reference_paths,
            job_config.ai_config.model,
            job_config.ai_config.provider_id,
            job_config.ai_config.custom_translation_instruction,
        )
        self.current_translation_path = None
        self.live_translation_markdown = ""
        self.live_translation_by_source.pop(source_key, None)
        self.show_placeholder(
            self.translation_web_view,
            self.translation_fallback_viewer,
            "旧译文已清除，正在按当前配置重新翻译...",
        )
        for reader in list(self.reader_windows):
            if (
                not reader.layout_reading_mode
                and reader.source_path
                and reader.source_path.resolve() == source.resolve()
            ):
                reader.translation_path = None
                reader.live_translation_markdown = ""
                reader.refresh_content()
        self.retranslate_document_with_current_settings(source)

    def confirm_retranslate_from_reader(self, reader: ReaderWindow):
        source = reader.source_path
        if not source or not source.exists():
            QMessageBox.information(self, "无法重新翻译", "当前阅读窗口没有可用于重新翻译的解析 Markdown。")
            return
        if self.is_thread_running(self.translate_worker):
            QMessageBox.information(self, "正在翻译", "请等待当前翻译结束或点击停止任务后，再重新翻译。")
            return
        dialog = TranslationOptionsDialog(self, provider_id=self.settings.ai_provider, retranslate=True)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected_ai_config:
            return
        ai_config = dialog.selected_ai_config
        job_config = self.build_saved_translation_job_config(ai_config)
        source_key = str(source.resolve())
        if reader.layout_reading_mode:
            is_current_source = bool(
                self.current_source_path
                and self.current_source_path.exists()
                and self.current_source_path.resolve() == source.resolve()
            )
            has_published_preview = bool(
                is_current_source
                and self.current_layout_translation_path
                and self.current_layout_translation_path.exists()
            )
            self.clear_layout_body_font_for_document(source)
            clear_layout_translation_artifacts(
                source,
                job_config.target_language,
                job_config.reference_paths,
                job_config.ai_config.model,
                job_config.ai_config.provider_id,
                preserve_published_preview=True,
            )
            if is_current_source:
                self.live_layout_translation_markdown = ""
                if has_published_preview:
                    self.show_layout_retranslation_notice(self.translation_web_view)
                    self.append_log("已开始重新翻译：新译文生成期间，仍可正常阅读和导出当前版本。")
            self.live_layout_translation_by_source.pop(source_key, None)
            for open_reader in list(self.reader_windows):
                if (
                    open_reader.layout_reading_mode
                    and open_reader.source_path
                    and open_reader.source_path.resolve() == source.resolve()
                ):
                    open_reader.live_translation_markdown = ""
            self.start_layout_translation_job(
                source,
                job_config.ai_config,
                job_config.target_language,
                source_language=job_config.source_language,
                local_machine_parallelism=job_config.local_machine_parallelism,
                reference_paths=job_config.reference_paths,
                translation_mode=job_config.mode,
                force=True,
                preserve_existing_preview=has_published_preview,
            )
            return
        clear_translation_artifacts(
            source,
            job_config.target_language,
            job_config.mode,
            job_config.reference_paths,
            job_config.ai_config.model,
            job_config.ai_config.provider_id,
            job_config.ai_config.custom_translation_instruction,
        )
        if self.current_source_path and self.current_source_path.resolve() == source.resolve():
            self.current_translation_path = None
            self.live_translation_markdown = ""
            self.show_placeholder(
                self.translation_web_view,
                self.translation_fallback_viewer,
                "旧译文已清除，正在按当前配置重新翻译...",
            )
        self.live_translation_by_source.pop(source_key, None)
        for open_reader in list(self.reader_windows):
            if (
                not open_reader.layout_reading_mode
                and open_reader.source_path
                and open_reader.source_path.resolve() == source.resolve()
            ):
                open_reader.translation_path = None
                open_reader.live_translation_markdown = ""
                open_reader.refresh_content()
        self.retranslate_document_with_current_settings(source)

    def open_ai_for_context(self, owner_window: QWidget | None = None):
        """Open the document-chat panel for the view that initiated the action."""
        if isinstance(owner_window, ReaderWindow):
            # A context-menu action in pure reading mode must not silently move
            # the shared chat back to the workbench.  Reveal its local rail
            # first, then attach the quote to that same chat instance.
            owner_window.reader_ai_button.setChecked(True)
            if not owner_window.reader_ai_panel.isVisible():
                owner_window.toggle_reader_ai_sidebar(True)
            if not owner_window.ensure_reader_ai_chat():
                return None
            return owner_window.reader_ai_chat_window

        chat = self.ensure_embedded_chat()
        if not chat:
            return None
        self.left_stack.setCurrentIndex(1)
        self.ai_page_button.setChecked(True)
        self.load_embedded_ai_for_current_document()
        return chat

    def ask_ai_from_pane(
        self,
        pane: str,
        widget: QWidget,
        selected_text: str = "",
        image_context: dict | None = None,
        selection_anchor: dict | None = None,
        owner_window: QWidget | None = None,
    ):
        reader = owner_window if isinstance(owner_window, ReaderWindow) else None
        markdown_path = (
            reader.source_path if pane == "source" and reader else
            (reader.translation_path or reader.source_path) if reader else
            self.current_source_path if pane == "source" else (self.current_translation_path or self.current_source_path)
        )
        if not markdown_path or not markdown_path.exists():
            QMessageBox.information(self, "暂无文档", "请先打开一个解析后的文档。")
            return

        def reference_metadata(anchor: dict | None = None) -> dict:
            source = (reader.source_path if reader else self.current_source_path) or canonical_reference_document_path(markdown_path)
            metadata = {
                "document_path": str(source) if source else "",
                "source_markdown_path": str(source) if source else "",
                "render_mode": "layout" if bool(getattr(reader, "layout_reading_mode", self.settings.layout_reading_mode)) else "stream",
            }
            if pane == "translation":
                layout_mode = bool(getattr(reader, "layout_reading_mode", self.settings.layout_reading_mode))
                translation = reader.translation_path if reader else (self.current_layout_translation_path if layout_mode else self.current_translation_path)
                live = reader.live_translation_markdown if reader else (self.live_layout_translation_markdown if layout_mode else self.live_translation_markdown)
                metadata["translation_revision"] = reference_revision(translation, live)
                metadata["source_anchor"] = dict(anchor or {})
            return metadata

        def image_context_local_path(image_info: dict | None) -> Path | None:
            if not isinstance(image_info, dict):
                return None
            candidates = [
                str(image_info.get("rawSrc") or "").strip(),
                str(image_info.get("src") or "").strip(),
            ]
            for value in candidates:
                if not value or value.startswith("data:"):
                    continue
                url = QUrl(value)
                if url.isLocalFile():
                    path = Path(url.toLocalFile())
                else:
                    path = Path(value)
                    if not path.is_absolute():
                        path = markdown_path.parent / value
                if path.exists() and path.is_file():
                    return path.resolve()
            return None

        def open_chat_with_selection(selection_text: str, image_info: dict | None = None):
            selection_text = str(selection_text or "").strip()
            image_info = image_info if isinstance(image_info, dict) else None
            if not selection_text and not image_info:
                return
            chat = self.open_ai_for_context(reader)
            if not chat:
                return
            if image_info:
                image_label = str(image_info.get("alt") or image_info.get("title") or image_info.get("src") or "右键图片").strip()
                chat.set_pending_reference_quote({
                    "type": "image",
                    "text": f"右键图片：{image_label}",
                    "image_src": str(image_info.get("rawSrc") or image_info.get("src") or ""),
                    "pane": pane,
                    **(image_info.get("anchor") if isinstance(image_info.get("anchor"), dict) else {}),
                    **reference_metadata(image_info.get("anchor") if isinstance(image_info.get("anchor"), dict) else None),
                    "markdown_path": str(markdown_path),
                    "title": markdown_path.parent.name,
                })
                image_path = image_context_local_path(image_info)
                if image_path is not None:
                    image = QImage(str(image_path))
                    if not image.isNull():
                        chat.add_pasted_input_image(image, image_path.name)
                chat.input_box.setPlainText("请解释这张图片的含义，并结合全文说明它在论文中的作用。")
            elif selection_text:
                chat.set_pending_reference_quote({
                    "text": selection_text,
                    "pane": pane,
                    **(selection_anchor if isinstance(selection_anchor, dict) else {}),
                    **reference_metadata(selection_anchor if isinstance(selection_anchor, dict) else None),
                    "markdown_path": str(markdown_path),
                    "title": markdown_path.parent.name,
                })
                chat.input_box.setPlainText("请解释这段内容的含义，并结合全文说明它在论文中的作用。")
            chat.input_box.setFocus()

        if selected_text.strip() or image_context:
            open_chat_with_selection(selected_text, image_context)
            return

        if WEBENGINE_AVAILABLE and QWebEngineView is not None and isinstance(widget, QWebEngineView):
            widget.page().runJavaScript(
                "window.syncScrollApi ? window.syncScrollApi.selectedText() : String(window.getSelection ? window.getSelection() : '')",
                open_chat_with_selection,
            )
        elif isinstance(widget, QTextBrowser):
            open_chat_with_selection(widget.textCursor().selectedText())
        else:
            open_chat_with_selection("")

    def handle_formula_ai_quote(self, pane: str, payload: dict, owner_window: QWidget | None = None):
        """Append one MathJax formula to the current document-chat reference list."""
        if not isinstance(payload, dict):
            return
        tex = str(payload.get("tex") or "").strip()
        if not tex:
            return
        page_value = payload.get("page")
        try:
            page_number = int(page_value) if page_value is not None else None
        except (TypeError, ValueError):
            page_number = None
        source_path = (
            getattr(owner_window, "source_path", None)
            if isinstance(owner_window, ReaderWindow)
            else (self.current_source_path or self.current_markdown_path)
        )
        if not source_path:
            return

        if isinstance(owner_window, ReaderWindow):
            owner_window.reader_ai_button.setChecked(True)
            if not owner_window.ensure_reader_ai_chat():
                return
            chat = owner_window.reader_ai_chat_window
        else:
            chat = self.ensure_embedded_chat()
            if not chat:
                return
            self.left_stack.setCurrentIndex(1)
            self.ai_page_button.setChecked(True)
            self.load_embedded_ai_for_current_document()

        location = f"第 {page_number} 页" if page_number else "排版页面"
        is_layout = bool(getattr(owner_window, "layout_reading_mode", self.settings.layout_reading_mode))
        translation_path = (
            getattr(owner_window, "translation_path", None)
            if isinstance(owner_window, ReaderWindow)
            else (self.current_layout_translation_path if is_layout else self.current_translation_path)
        )
        live_translation = (
            getattr(owner_window, "live_translation_markdown", "")
            if isinstance(owner_window, ReaderWindow)
            else (self.live_layout_translation_markdown if is_layout else self.live_translation_markdown)
        )
        anchor = {
            "anchor_page": payload.get("anchor_page") or page_number,
            "anchor_ratio": payload.get("anchor_ratio"),
            "anchor_rect": payload.get("anchor_rect") if isinstance(payload.get("anchor_rect"), dict) else None,
            "anchor_point": payload.get("anchor_point") if isinstance(payload.get("anchor_point"), dict) else None,
        }
        quote = {
            "type": "formula",
            "text": f"公式（{location}）：\n\\[\n{tex}\n\\]",
            "formula_tex": tex,
            "page": page_number,
            **anchor,
            "pane": pane,
            "markdown_path": str(source_path),
            "document_path": str(source_path),
            "source_markdown_path": str(source_path),
            "render_mode": "layout" if is_layout else "stream",
            "translation_revision": reference_revision(translation_path, live_translation) if pane == "translation" else "",
            "source_anchor": dict(anchor) if pane == "translation" else {},
            "title": source_path.parent.name,
        }
        if hasattr(chat, "append_pending_reference_quote"):
            chat.append_pending_reference_quote(quote)
        else:
            chat.set_pending_reference_quote(quote)
        if not chat.input_box.toPlainText().strip():
            chat.input_box.setPlainText("请解释所引用公式的含义、各变量定义及其在全文中的作用。")
        chat.input_box.setFocus()

    def extract_key_points_from_pane(self, pane: str, owner_window=None):
        """向后兼容入口，直接委托给通用方法。"""
        self.submit_document_task_from_pane("key_points", pane, owner_window=owner_window)

    def submit_document_task_from_pane(
        self, task_type: str, pane: str, owner_window=None
    ):
        """
        从阅读区右键菜单触发文献 AI 分析任务（要点提炼 / 思维导图 / 思路流程）。

        任务立即发送，不需要用户再次点击发送按鈕。
        """
        reader = owner_window if isinstance(owner_window, ReaderWindow) else None
        markdown_path = (
            reader.source_path if pane == "source" and reader else
            (reader.translation_path or reader.source_path) if reader else
            self.current_source_path if pane == "source" else (self.current_translation_path or self.current_source_path)
        )
        if not markdown_path or not markdown_path.exists():
            QMessageBox.information(self, "暂无文档", "请先打开一个解析后的文档。")
            return

        chat = self.open_ai_for_context(reader)
        if not chat:
            return
        ok = chat.prepare_document_ai_task(task_type, {
            "markdown_path": str(markdown_path),
            "document_path": str(reader.source_path) if reader and reader.source_path else str(markdown_path),
            "source_markdown_path": str(reader.source_path) if reader and reader.source_path else str(markdown_path),
            "pane": pane,
            "render_mode": "layout" if bool(getattr(reader, "layout_reading_mode", self.settings.layout_reading_mode)) else "stream",
            "title": markdown_path.parent.name,
        })
        if ok:
            # 直接触发发送，与 Zotero 插件的一键生成行为保持一致。
            chat.send_message()
    def remove_chat_window(self, chat: QWidget):
        if chat in self.chat_windows:
            self.chat_windows.remove(chat)

    def show_export_dialog(self):
        has_layout_translation = bool(self.current_layout_translation_path and self.current_layout_translation_path.exists())
        if (
            not self.current_source_path
            and not self.current_translation_path
            and not self.live_translation_markdown.strip()
            and not has_layout_translation
        ):
            QMessageBox.information(self, "暂无可导出内容", "请先打开一个解析结果，或先完成/开始翻译。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("导出文档")
        dialog.resize(520, 420)
        layout = QVBoxLayout(dialog)
        current_style = resolve_export_style(getattr(self.settings, "export_style", None))

        pane_row = QHBoxLayout()
        pane_row.addWidget(QLabel("内容:"))
        pane_combo = QComboBox()
        if self.current_source_path:
            pane_combo.addItem("解析版原文", "source")
        if self.current_translation_path or self.live_translation_markdown.strip() or has_layout_translation:
            pane_combo.addItem("译文", "translation")
        pane_row.addWidget(pane_combo, 1)
        layout.addLayout(pane_row)

        format_row = QHBoxLayout()
        format_row.addWidget(QLabel("格式:"))
        format_combo = QComboBox()
        format_combo.addItem("PDF", "pdf")
        format_combo.addItem("Word (.docx)", "docx")
        format_combo.addItem("HTML", "html")
        format_combo.addItem("Markdown", "md")
        current_document_is_epub = is_epub_markdown_path(self.current_source_path)
        if current_document_is_epub:
            format_combo.addItem("EPUB3", "epub")
        format_row.addWidget(format_combo, 1)
        layout.addLayout(format_row)

        word_refine_check = QCheckBox("Word 精校")
        word_refine_check.setChecked(True)
        word_refine_ok, word_refine_message = check_word_refine_available()
        if not word_refine_ok:
            word_refine_check.setChecked(False)
        word_refine_check.setEnabled(word_refine_ok)
        word_refine_check.setToolTip(word_refine_message)
        layout.addWidget(word_refine_check)

        style_group = QGroupBox("导出样式")
        style_layout = QVBoxLayout(style_group)

        body_font_row = QHBoxLayout()
        body_font_row.addWidget(QLabel("正文字体"))
        body_cjk_combo = QComboBox()
        body_cjk_combo.addItems(EXPORT_FONT_CJK_OPTIONS)
        body_cjk_combo.setCurrentText(current_style.body_font_cjk)
        body_font_row.addWidget(body_cjk_combo, 1)
        body_latin_combo = QComboBox()
        body_latin_combo.addItems(EXPORT_FONT_LATIN_OPTIONS)
        body_latin_combo.setCurrentText(current_style.body_font_latin)
        body_font_row.addWidget(body_latin_combo, 1)
        style_layout.addLayout(body_font_row)

        heading_font_row = QHBoxLayout()
        heading_font_row.addWidget(QLabel("标题字体"))
        heading_cjk_combo = QComboBox()
        heading_cjk_combo.addItems(EXPORT_FONT_CJK_OPTIONS)
        heading_cjk_combo.setCurrentText(current_style.heading_font_cjk)
        heading_font_row.addWidget(heading_cjk_combo, 1)
        heading_latin_combo = QComboBox()
        heading_latin_combo.addItems(EXPORT_FONT_LATIN_OPTIONS)
        heading_latin_combo.setCurrentText(current_style.heading_font_latin)
        heading_font_row.addWidget(heading_latin_combo, 1)
        style_layout.addLayout(heading_font_row)

        body_metric_row = QHBoxLayout()
        body_metric_row.addWidget(QLabel("正文字号"))
        body_font_combo = QComboBox()
        for label, _ in EXPORT_FONT_SIZE_OPTIONS:
            body_font_combo.addItem(label)
        body_font_combo.setCurrentText(export_font_size_label_from_pt(current_style.body_font_pt, "小四"))
        body_metric_row.addWidget(body_font_combo)
        body_metric_row.addWidget(QLabel("行距"))
        line_spacing_spin = QSpinBox()
        line_spacing_spin.setRange(15, 30)
        line_spacing_spin.setValue(current_style.line_spacing_pt)
        line_spacing_spin.setSuffix(" pt")
        body_metric_row.addWidget(line_spacing_spin)
        body_metric_row.addWidget(QLabel("图片宽度"))
        image_width_spin = QSpinBox()
        image_width_spin.setRange(25, 100)
        image_width_spin.setValue(clamp_int(current_style.image_width_percent, 25, 100, 45))
        image_width_spin.setSuffix("%")
        body_metric_row.addWidget(image_width_spin)
        style_layout.addLayout(body_metric_row)

        heading_size_row = QHBoxLayout()
        heading_size_row.addWidget(QLabel("标题字号"))
        heading1_combo = QComboBox()
        heading2_combo = QComboBox()
        heading3_combo = QComboBox()
        for label, _ in EXPORT_FONT_SIZE_OPTIONS:
            heading1_combo.addItem(label)
            heading2_combo.addItem(label)
            heading3_combo.addItem(label)
        heading1_combo.setCurrentText(export_font_size_label_from_pt(current_style.heading1_pt, "小三"))
        heading2_combo.setCurrentText(export_font_size_label_from_pt(current_style.heading2_pt, "四号"))
        heading3_combo.setCurrentText(export_font_size_label_from_pt(current_style.heading3_pt, "小四"))
        heading_size_row.addWidget(heading1_combo)
        heading_size_row.addWidget(heading2_combo)
        heading_size_row.addWidget(heading3_combo)
        style_layout.addLayout(heading_size_row)
        layout.addWidget(style_group)

        def sync_export_options():
            is_docx = format_combo.currentData() == "docx"
            is_layout_export = self.should_use_layout_export(str(pane_combo.currentData() or "source"))
            word_refine_check.setVisible(is_docx)
            style_group.setVisible(format_combo.currentData() in {"pdf", "docx", "html"})
            style_group.setEnabled(not is_layout_export)
            if is_layout_export:
                style_group.setTitle("导出样式（排版模式以排版状态为主）")
            else:
                style_group.setTitle("导出样式")

        format_combo.currentIndexChanged.connect(sync_export_options)
        pane_combo.currentIndexChanged.connect(sync_export_options)
        sync_export_options()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            pane = str(pane_combo.currentData() or "source")
            selected_format = str(format_combo.currentData() or "")
            if selected_format == "epub" and not is_epub_markdown_path(self.current_source_path):
                QMessageBox.information(self, "无法导出 EPUB", "EPUB 导出仅适用于 EPUB 原文件。")
                return
            is_layout_export = self.should_use_layout_export(pane)
            export_style = None
            if not is_layout_export:
                body_font_pt = export_font_size_pt_from_label(body_font_combo.currentText(), current_style.body_font_pt)
                heading1_pt = export_font_size_pt_from_label(heading1_combo.currentText(), current_style.heading1_pt)
                heading2_pt = export_font_size_pt_from_label(heading2_combo.currentText(), current_style.heading2_pt)
                heading3_pt = export_font_size_pt_from_label(heading3_combo.currentText(), current_style.heading3_pt)
                export_style = ExportStyleSettings(
                    preset_id="tsinghua_default",
                    body_font_cjk=body_cjk_combo.currentText().strip() or "宋体",
                    body_font_latin=body_latin_combo.currentText().strip() or "Times New Roman",
                    heading_font_cjk=heading_cjk_combo.currentText().strip() or "黑体",
                    heading_font_latin=heading_latin_combo.currentText().strip() or "Arial",
                    body_font_pt=body_font_pt,
                    heading1_pt=heading1_pt,
                    heading2_pt=heading2_pt,
                    heading3_pt=heading3_pt,
                    caption_font_pt=max(9, body_font_pt - 1),
                    line_spacing_pt=line_spacing_spin.value(),
                    first_line_indent_cm=current_style.first_line_indent_cm,
                    image_width_percent=image_width_spin.value(),
                )
                self.settings.export_style = export_style
                app_config.save_settings(self.settings)
            self.export_pane_document(
                pane,
                format_combo.currentData(),
                refine_word=word_refine_check.isChecked(),
                export_style=export_style,
            )

    def pane_markdown_path(self, pane: str) -> Path | None:
        if pane == "source":
            return self.current_source_path if self.current_source_path and self.current_source_path.exists() else None
        if self.current_translation_path and self.current_translation_path.exists():
            return self.current_translation_path
        if self.live_translation_markdown.strip():
            base_dir = (self.current_source_path or self.current_markdown_path or WORKSPACE).parent
            live_path = base_dir / ".live_translation_export.md"
            live_path.write_text(self.live_translation_markdown, encoding="utf-8")
            return live_path
        return None

    @staticmethod
    def export_content_label(pane: str) -> str:
        return "解析版原文" if pane == "source" else "译文"

    def layout_export_source_path(self) -> Path | None:
        return self.current_source_path if self.current_source_path and self.current_source_path.exists() else None

    def should_use_layout_export(self, pane: str) -> bool:
        return bool(
            self.settings.layout_reading_mode
            or (
                pane == "source"
                and self.show_parsed_source_check.isChecked()
                and self.show_layout_restoration_check.isChecked()
            )
        )

    def layout_export_html_path(self, pane: str, export_style: ExportStyleSettings | None = None) -> Path | None:
        source_path = self.layout_export_source_path()
        if not source_path:
            return None
        if pane == "translation":
            if self.current_layout_translation_path and self.current_layout_translation_path.exists():
                return self.current_layout_translation_path
            raise MinerUError("当前文档还没有排版译文 HTML，请先在排版阅读模式下完成翻译后再导出排版译文。")
        return render_layout_preview_html(
            source_path,
            self.append_log,
            export_style,
            strict_fit=bool(self.settings.layout_reading_mode),
            debug_overlay=False,
        )

    def export_default_stem(self) -> str:
        if self.current_original_path and self.current_original_path.exists():
            return self.current_original_path.stem
        if self.current_source_path:
            return self.current_source_path.parent.name
        if self.current_markdown_path:
            return self.current_markdown_path.parent.name
        return "document"

    @staticmethod
    def export_file_type_label(file_format: str) -> str:
        return {
            "pdf": "PDF",
            "docx": "Word",
            "html": "HTML",
            "md": "Markdown",
            "epub": "EPUB",
        }.get(file_format.lower(), file_format.upper())

    def export_feedback_name(self, title: str, file_format: str) -> str:
        return f"{title}（{self.export_file_type_label(file_format)}）文件"

    def begin_export_feedback(self, export_name: str, target_path: Path) -> QProgressDialog:
        self.append_log(f"正在导出：{target_path.name if hasattr(target_path, 'name') else target_path}")
        dialog = QProgressDialog(f"正在导出{export_name}中，请稍后...", "", 0, 0, self)
        dialog.setWindowTitle("正在导出")
        dialog.setCancelButton(None)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)
        dialog.setProperty("exportBaseMessage", f"正在导出{export_name}中，请稍后...")
        dialog.show()
        QApplication.processEvents()
        return dialog

    def update_export_feedback(self, dialog: QProgressDialog | None, message: str):
        if dialog:
            base_message = str(dialog.property("exportBaseMessage") or "正在导出文件中，请稍后...")
            dialog.setLabelText(f"{base_message}\n{message}")
            QApplication.processEvents()

    def end_export_feedback(self, dialog: QProgressDialog | None):
        if dialog:
            dialog.close()
            dialog.deleteLater()
            QApplication.processEvents()

    def capture_layout_runtime_state(self, pane: str, html_path: Path) -> dict | None:
        """Capture visible-reader styles for every layout export."""
        if not html_path or not html_path.exists():
            return None
        state_cache_path = layout_state_cache_path(html_path)
        cached_state = read_current_layout_cache_payload(state_cache_path, html_path, "state")
        if not WEBENGINE_AVAILABLE:
            return cached_state if isinstance(cached_state, dict) else None
        web_view = self.translation_web_view if pane == "translation" else self.source_web_view
        if not web_view:
            return cached_state if isinstance(cached_state, dict) else None
        try:
            loaded_path = web_view.url().toLocalFile()
            if not loaded_path or Path(loaded_path).resolve() != html_path.resolve():
                return cached_state if isinstance(cached_state, dict) else None
        except Exception:
            return cached_state if isinstance(cached_state, dict) else None

        loop = QEventLoop(self)
        result: dict[str, object] = {"value": None}

        def receive(value):
            result["value"] = value
            loop.quit()

        def probe_ready_response(ready):
            if ready is True:
                web_view.page().runJavaScript(layout_docx_runtime_state_script(), receive)
            elif loop.isRunning():
                QTimer.singleShot(100, probe_ready)

        def probe_ready():
            web_view.page().runJavaScript(
                "Boolean(document.body && document.body.dataset.layoutFitState === 'ready')",
                probe_ready_response,
            )

        timeout = QTimer(self)
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        # The reader persists this state as soon as fitting completes.  Export
        # should only bridge a very small race, never block the GUI for the old
        # two-minute cold-layout timeout.
        timeout.start(3000)
        probe_ready()
        loop.exec()
        timeout.stop()
        timeout.deleteLater()

        value = result.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return cached_state if isinstance(cached_state, dict) else None
        if not isinstance(value, dict) or not isinstance(value.get("pages"), list):
            return cached_state if isinstance(cached_state, dict) else None
        try:
            write_layout_fit_disk_cache(html_path, value)
            write_layout_cache_payload(state_cache_path, html_path, "state", value)
        except OSError as exc:
            self.append_log(f"缓存状态更新稍有延迟，正在继续导出：{exc}")
        self.append_log("导出排版已同步当前的字号、行距与对齐设置。")
        return value

    def schedule_layout_word_state_cache_after_load(self, pane: str, ok: bool):
        """Capture final layout state asynchronously; never make export wait."""
        if not ok or not self.settings.layout_reading_mode:
            return
        web_view = self.translation_web_view if pane == "translation" else self.source_web_view
        if not web_view:
            return
        try:
            loaded_url = web_view.url()
            loaded_file = loaded_url.toLocalFile() if loaded_url.isLocalFile() else ""
            if not loaded_file:
                return
            html_path = Path(loaded_file).resolve()
        except (OSError, RuntimeError):
            return
        if not html_path.is_file() or not is_layout_preview_html_path(html_path):
            return
        # Like PDF caching, capturing a full layout state while streamed
        # translation is still rewriting the preview only preserves a stale
        # revision and needlessly serializes every page.  Keep one deferred
        # request for the completed translation instead.
        if pane == "translation" and self.has_active_translation_task():
            pending = getattr(self, "_pending_layout_word_state_paths", set())
            identity = str(html_path)
            if identity not in pending:
                pending.add(identity)
                self._pending_layout_word_state_paths = pending

                def retry_after_translation():
                    if self.has_active_translation_task():
                        QTimer.singleShot(300, retry_after_translation)
                        return
                    self._pending_layout_word_state_paths.discard(identity)
                    try:
                        current_path = Path(web_view.url().toLocalFile()).resolve()
                    except (OSError, RuntimeError):
                        return
                    if current_path == html_path:
                        self.schedule_layout_word_state_cache_after_load(pane, True)

                QTimer.singleShot(300, retry_after_translation)
            return

        def poll_ready(attempt: int = 0):
            if not web_view or attempt >= 1200:
                return
            try:
                if Path(web_view.url().toLocalFile()).resolve() != html_path:
                    return
            except (OSError, RuntimeError):
                return

            def receive_ready(ready):
                if ready is not True:
                    QTimer.singleShot(100, lambda: poll_ready(attempt + 1))
                    return

                def receive_state(raw_value):
                    if not isinstance(raw_value, str):
                        return
                    try:
                        state = json.loads(raw_value)
                    except (TypeError, ValueError):
                        return
                    if not isinstance(state, dict) or not isinstance(state.get("pages"), list):
                        return
                    try:
                        wrote_fit = write_layout_fit_disk_cache(html_path, state)
                        write_layout_cache_payload(layout_state_cache_path(html_path), html_path, "state", state)
                        if wrote_fit:
                            self.append_log("排版已就绪，后续切换视图或导出将秒级直接复用。")
                    except OSError as exc:
                        self.append_log(f"保存排版状态缓存失败: {exc}")

                web_view.page().runJavaScript(layout_docx_runtime_state_script(), receive_state)

            web_view.page().runJavaScript(
                "Boolean(document.body && document.body.dataset.layoutFitState === 'ready')",
                receive_ready,
            )

        poll_ready()

    def export_pane_document(
        self,
        pane: str,
        file_format: str,
        refine_word: bool = False,
        export_style: ExportStyleSettings | None = None,
    ):
        if file_format == "epub" and not is_epub_markdown_path(self.current_source_path):
            QMessageBox.information(self, "无法导出 EPUB", "EPUB 导出仅适用于 EPUB 原文件。")
            return
        markdown_path = self.pane_markdown_path(pane)
        title = self.export_content_label(pane)
        use_layout_preview = self.should_use_layout_export(pane)
        if use_layout_preview:
            # 排版导出使用已完成的页面几何和最终拟合的文字样式；流式设置不能覆盖它。
            export_style = None
        layout_source_path = self.layout_export_source_path() if use_layout_preview else None
        if not markdown_path and not layout_source_path:
            QMessageBox.information(self, "暂无可导出内容", f"当前没有可导出的{title}内容。")
            return

        suffix = "docx" if file_format == "docx" else file_format
        default_base = layout_source_path or markdown_path or WORKSPACE
        default_path = default_base.with_name(f"{self.export_default_stem()}.{suffix}")
        filters = {
            "pdf": "PDF 文件 (*.pdf)",
            "docx": "Word 文档 (*.docx)",
            "html": "HTML 文件 (*.html)",
            "md": "Markdown 文件 (*.md)",
            "epub": "EPUB 电子书 (*.epub)",
        }
        save_path, _ = QFileDialog.getSaveFileName(self, f"导出{title}", str(default_path), filters[file_format])
        if not save_path:
            return
        out_path = Path(save_path)
        if out_path.suffix.lower() != f".{suffix}":
            out_path = out_path.with_suffix(f".{suffix}")

        export_name = self.export_feedback_name(title, file_format)
        busy_dialog = self.begin_export_feedback(export_name, out_path)
        async_export = False
        try:
            if file_format == "md":
                if use_layout_preview and pane == "translation" and not markdown_path:
                    raise MinerUError("排版译文没有独立 Markdown 产物，请导出 HTML、PDF 或 Word。")
                if not markdown_path:
                    raise MinerUError(f"当前没有可导出的{title} Markdown。")
                self.update_export_feedback(busy_dialog, "正在复制 Markdown…")
                shutil.copyfile(markdown_path, out_path)
                self.end_export_feedback(busy_dialog)
                busy_dialog = None
                self.finish_export(out_path, export_name=export_name)
            elif file_format == "epub":
                original = self.current_original_path
                if pane == "source" and original and original.is_file() and original.suffix.lower() == ".epub":
                    self.update_export_feedback(busy_dialog, "正在复制原始 EPUB…")
                    shutil.copyfile(original, out_path)
                    validation = validate_epub(out_path)
                    if not validation.get("valid"):
                        raise MinerUError("原始 EPUB 校验失败：" + "；".join(validation.get("errors") or []))
                    warnings = list(validation.get("warnings") or [])
                else:
                    if not markdown_path:
                        raise MinerUError(f"当前没有可导出的{title} Markdown。")
                    self.update_export_feedback(busy_dialog, "正在生成 EPUB3 并校验目录、章节与资源…")
                    warnings = export_markdown_to_epub(
                        markdown_path,
                        out_path,
                        target_language=str(getattr(self.settings, "translation_target_language", "") or ""),
                        translated=pane == "translation",
                        log=self.append_log,
                    )
                self.end_export_feedback(busy_dialog)
                busy_dialog = None
                self.finish_export(out_path, extra_warnings=warnings, export_name=export_name)
            elif file_format == "html":
                self.update_export_feedback(busy_dialog, "正在生成 HTML…")
                html_path = (
                    self.layout_export_html_path(pane, export_style)
                    if use_layout_preview
                    else render_export_html(markdown_path, self.append_log, export_style)
                )
                if not html_path or not html_path.exists():
                    raise MinerUError("无法生成 HTML 预览文件。")
                self.update_export_feedback(busy_dialog, "正在写入 HTML 文件…")
                shutil.copyfile(html_path, out_path)
                self.end_export_feedback(busy_dialog)
                busy_dialog = None
                self.finish_export(out_path, export_name=export_name)
            elif file_format == "docx":
                if use_layout_preview:
                    self.update_export_feedback(busy_dialog, "正在生成可编辑的排版 Word…")
                    layout_html_path = self.layout_export_html_path(pane, export_style)
                    if not layout_html_path or not layout_html_path.exists():
                        raise MinerUError("无法生成排版 HTML，不能导出可编辑的所见排版 Word。")
                    runtime_state = self.capture_layout_runtime_state(pane, layout_html_path)
                    render_layout_editable_html_docx(
                        layout_html_path,
                        out_path,
                        runtime_state=runtime_state,
                        log=self.append_log,
                    )
                    export_warnings = [
                        "排版 Word 已按界面最终坐标、字号、行距、段落间距和对齐方式导出，"
                        "不再在 Word 写入层二次放大字号；文字和公式容器会按内容扩展，"
                        "正文和公式均可编辑，论文原有插图保持为图片。"
                    ]
                else:
                    export_warnings = self.export_markdown_with_pandoc(
                        markdown_path,
                        out_path,
                        refine_word=refine_word,
                        export_style=export_style,
                        progress=lambda message: self.update_export_feedback(busy_dialog, message),
                    )
                self.end_export_feedback(busy_dialog)
                busy_dialog = None
                self.finish_export(out_path, extra_warnings=export_warnings, export_name=export_name)
            elif file_format == "pdf":
                layout_html_path = (
                    self.layout_export_html_path(pane, export_style)
                    if use_layout_preview
                    else None
                )
                if use_layout_preview and layout_html_path:
                    # PDF and Word share the exact final state of the visible
                    # reader. Persist it before the hidden PDF view loads.
                    runtime_state = self.capture_layout_runtime_state(pane, layout_html_path)
                    if runtime_state is not None:
                        # This is an internal derived cache, never the PDF
                        # selected by the user. Rebuild it from current state.
                        invalidate_layout_pdf_cache(layout_html_path)
                async_export = self.export_markdown_to_pdf(
                    layout_source_path if use_layout_preview else markdown_path,
                    out_path,
                    export_style=export_style,
                    prebuilt_html_path=layout_html_path,
                    layout_mode=use_layout_preview,
                    progress=lambda message: self.update_export_feedback(busy_dialog, message),
                    completion=lambda _success: self.end_export_feedback(busy_dialog),
                    export_name=export_name,
                )
                if not async_export:
                    self.end_export_feedback(busy_dialog)
                    busy_dialog = None
                    self.finish_export(out_path, export_name=export_name)
        except Exception as exc:
            self.end_export_feedback(busy_dialog)
            busy_dialog = None
            QMessageBox.critical(self, "导出失败", str(exc))
        finally:
            if not async_export:
                self.end_export_feedback(busy_dialog)

    def export_markdown_with_pandoc(
        self,
        markdown_path: Path,
        out_path: Path,
        refine_word: bool = False,
        export_style: ExportStyleSettings | None = None,
        progress=None,
    ) -> list[str]:
        out_path = out_path.resolve()
        pandoc = find_pandoc()
        if not pandoc:
            raise MinerUError("没有找到 pandoc.exe，无法导出 Word。")
        export_markdown, table_placeholders = make_word_export_markdown(markdown_path, export_style)
        warnings: list[str] = []
        cmd = [
            str(pandoc),
            str(export_markdown),
            "-f",
            EXPORT_MARKDOWN_FORMAT,
            "--standalone",
            "--wrap=none",
            "--resource-path",
            str(markdown_path.parent),
            "-o",
            str(out_path),
        ]
        try:
            if progress:
                progress("正在调用 Pandoc 生成 Word…")
            subprocess.run(
                cmd,
                cwd=str(markdown_path.parent),
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
                **hidden_subprocess_kwargs(),
            )
            if progress:
                progress("正在重建表格结构…")
            replace_docx_placeholders_with_tables(out_path, table_placeholders)
            if progress:
                progress("正在整理 Word 版式…")
            postprocess_exported_docx(out_path, export_style)
            if refine_word:
                if progress:
                    progress("正在执行 Word 精校（公式/表格）…")
                refined, message = refine_docx_tables_with_word_omath(out_path, table_placeholders, log=self.append_log)
                if progress:
                    progress("正在回写最终版式…")
                postprocess_exported_docx(out_path, export_style)
                if not refined and "没有检测到" not in message and "没有可供" not in message:
                    warnings.append(f"已导出 Word，但“Word 精校”未完成: {message}")
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise MinerUError(f"Pandoc 导出 Word 失败: {detail}") from exc
        finally:
            try:
                export_markdown.unlink()
            except OSError:
                pass
        return warnings

    def export_markdown_to_pdf(
        self,
        markdown_path: Path,
        out_path: Path,
        export_style: ExportStyleSettings | None = None,
        prebuilt_html_path: Path | None = None,
        layout_mode: bool = False,
        progress=None,
        completion=None,
        export_name: str | None = None,
    ) -> bool:
        if progress:
            progress("正在生成导出 HTML…")
        html_path = prebuilt_html_path or render_export_html(markdown_path, self.append_log, export_style)
        if WEBENGINE_AVAILABLE and html_path and html_path.exists():
            if progress:
                progress("正在渲染 PDF…")
            return bool(
                self.export_html_to_pdf(
                    html_path,
                    out_path,
                    layout_mode=layout_mode,
                    completion=completion,
                    export_name=export_name,
                )
            )

        pandoc = find_pandoc()
        if not pandoc:
            raise MinerUError("没有找到 pandoc.exe，且当前环境没有 WebEngine，无法导出 PDF。")
        export_markdown = make_export_markdown(markdown_path, export_style)
        cmd = [
            str(pandoc),
            str(export_markdown),
            "-f",
            EXPORT_MARKDOWN_FORMAT,
            "--standalone",
            "--wrap=none",
            "--resource-path",
            str(markdown_path.parent),
            "-o",
            str(out_path),
        ]
        if EXPORT_FILTER.exists():
            cmd.extend(["--lua-filter", str(EXPORT_FILTER)])
        try:
            if progress:
                progress("正在调用 Pandoc 生成 PDF…")
            subprocess.run(
                cmd,
                cwd=str(markdown_path.parent),
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
                **hidden_subprocess_kwargs(),
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise MinerUError(f"Pandoc 导出 PDF 失败: {detail}") from exc
        finally:
            try:
                export_markdown.unlink()
            except OSError:
                pass
        return False

    def layout_pdf_reader_scale(self, html_path: Path) -> float:
        """Return the completed reader-page scale captured with layout state."""
        state = read_current_layout_cache_payload(layout_state_cache_path(html_path), html_path, "state")
        if isinstance(state, dict):
            pages = state.get("pages")
            if isinstance(pages, list):
                for page in pages:
                    if not isinstance(page, dict):
                        continue
                    try:
                        scale = float(page.get("page_scale"))
                    except (TypeError, ValueError):
                        continue
                    if 0.1 <= scale <= 8.0:
                        return scale
        # Fresh output has an inline 920px-wide reading-page preview before
        # the live view persists its final state.  This is a stable fallback
        # and avoids reverting to the smaller source-PDF typography.
        page_size = layout_pdf_page_size_from_html(html_path)
        return 920.0 / page_size[0] if page_size and page_size[0] > 0 else 1.0

    def pdf_page_layout_for_export(self, html_path: Path, layout_mode: bool = False) -> QPageLayout:
        if layout_mode:
            page_size = layout_pdf_page_size_from_html(html_path)
            if page_size:
                page_width, page_height = page_size
                reader_scale = self.layout_pdf_reader_scale(html_path)
                page_width *= reader_scale
                # Chromium quantizes a custom QPageLayout before printing.
                # The result can be a fraction of a point shorter, turning a
                # page-height shell into a nearly empty overflow page. The
                # extra point is below the source content box.
                page_height = page_height * reader_scale + LAYOUT_PDF_PAGE_HEIGHT_ALLOWANCE_PT
                orientation = (
                    QPageLayout.Orientation.Landscape
                    if page_width > page_height
                    else QPageLayout.Orientation.Portrait
                )
                return QPageLayout(
                    QPageSize(
                        QSizeF(page_width, page_height),
                        QPageSize.Unit.Point,
                        "Layout PDF page",
                    ),
                    orientation,
                    QMarginsF(0, 0, 0, 0),
                    QPageLayout.Unit.Point,
                )
        return QPageLayout(
            QPageSize(QPageSize.PageSizeId.A4),
            QPageLayout.Orientation.Portrait,
            QMarginsF(31.8, 25.4, 31.8, 25.4),
            QPageLayout.Unit.Millimeter,
        )

    @staticmethod
    def layout_pdf_prepare_script(
        reader_scale: float = 1.0,
        body_font_pt: float | None = None,
    ) -> str:
        """Freeze pages at the reader's effective scale before PDF printing."""
        safe_reader_scale = max(0.1, min(8.0, float(reader_scale)))
        safe_body_font_pt = (
            "null"
            if body_font_pt is None
            else f"{max(LAYOUT_BODY_FONT_MIN_PT, min(LAYOUT_BODY_FONT_MAX_PT, float(body_font_pt))):.2f}"
        )
        script = r"""
        (() => {
          window.__mineruPdfExportMode = true;
          if (document.body) document.body.classList.add('layout-pdf-export');
          // A hidden export view cannot rely on the window that happened to
          // be visible when the user changed the font.  Reapply the explicit
          // per-document body-font preference here so PDF output has the same
          // source of truth as every reader window.
          const userBodyFontPt = __MINERU_BODY_FONT_PT__;
          if (Number.isFinite(userBodyFontPt)) {
            for (const node of document.querySelectorAll(
              '.layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"]'
            )) {
              node.style.fontSize = `${userBodyFontPt}pt`;
              node.dataset.userBodyFontPt = userBodyFontPt.toFixed(2);
            }
            if (document.body) document.body.dataset.userBodyFontPt = userBodyFontPt.toFixed(2);
          }
          const styleId = 'mineru-layout-pdf-print-style';
          if (!document.getElementById(styleId)) {
            const style = document.createElement('style');
            style.id = styleId;
            style.textContent = '@page { size: auto !important; margin: 0 !important; }'
              + '@media print { body { background: #ffffff !important; padding: 0 !important; }'
              + 'body.layout-fit-pending::before { display: none !important; }'
              // Keep exactly one forced break between layout pages. The
              // legacy property is reset first because Chromium can otherwise
              // combine it with break-after and emit a second blank sheet.
              + '.layout-page-wrap { margin: 0 !important; page-break-after: auto !important; break-after: page !important; }'
              + '.layout-page-wrap:last-of-type { page-break-after: auto !important; break-after: auto !important; }'
              + '.layout-page-shell { max-width: none !important; box-shadow: none !important; opacity: 1 !important; } }';
            document.head.appendChild(style);
          }
          // Preserve the reader's *effective* typography.  Coordinates and
          // glyphs scale together, so wrapping stays identical to the reader.
          const cssPxPerPoint = 96 / 72;
          const readerScale = __MINERU_READER_SCALE__;
          const pages = [];
          for (const shell of document.querySelectorAll('.layout-page-shell')) {
            const page = shell.querySelector('.layout-page');
            if (!page) continue;
            const pageWidth = Number.parseFloat(shell.dataset.pageWidth || page.style.width || '1');
            const pageHeight = Number.parseFloat(shell.dataset.pageHeight || page.style.height || '1');
            if (!(pageWidth > 0) || !(pageHeight > 0)) continue;
            shell.style.maxWidth = 'none';
            const printScale = readerScale * cssPxPerPoint;
            shell.style.width = `${pageWidth * printScale}px`;
            // Chromium may quantize a custom QPageLayout a fraction of a
            // point shorter than its requested height.  Keep the layout box
            // one CSS pixel inside the physical sheet: otherwise that tiny
            // overflow first creates a continuation page, then the forced
            // break below turns it into a completely blank page.
            const printBlockHeight = Math.max(1, pageHeight * printScale - 1);
            shell.style.height = `${printBlockHeight}px`;
            // The interactive reader uses CSS zoom.  Leaving that zoom on a
            // hidden print view compounds its width calculations with the
            // transform below, which is particularly visible in an equation's
            // reserved number gutter.
            page.style.zoom = '1';
            page.style.transform = `scale(${printScale})`;
            page.style.transformOrigin = 'top left';
            pages.push({ pageWidth, pageHeight });
          }
          // Measure the equation after the print scale is installed.  This
          // keeps the MathJax SVG and its separately-positioned tag in the
          // same coordinate system immediately before Chromium prints.
          if (window.__mineruFitLayoutEquations) window.__mineruFitLayoutEquations();
          const ready = Boolean(
            document.body
            && document.body.dataset.layoutFitState === 'ready'
            && !document.body.classList.contains('layout-fit-pending')
            && pages.length > 0
          );
          return JSON.stringify({ ready, pageCount: pages.length, pages, cssPxPerPoint, readerScale });
        })();
        """
        return (
            script.replace("__MINERU_READER_SCALE__", f"{safe_reader_scale:.8f}")
            .replace("__MINERU_BODY_FONT_PT__", safe_body_font_pt)
        )

    def schedule_layout_pdf_cache_after_load(self, ok: bool):
        """Build the finalized PDF only after the visible HTML fit has settled."""
        if (
            not ok
            or not self.settings.layout_reading_mode
            or not self.current_layout_translation_path
            or not self.current_layout_translation_path.exists()
            or not self.translation_web_view
        ):
            return
        html_path = self.current_layout_translation_path
        # Live layout previews reload this view as individual translation
        # batches arrive.  A PDF generated from any of those transient HTML
        # revisions is both expensive and immediately stale.  Defer exactly
        # one cache request until the active translation worker has finished;
        # the normal ready poll below then observes the final visible revision.
        if self.has_active_translation_task():
            pending = getattr(self, "_pending_layout_pdf_cache_paths", set())
            identity = str(html_path.resolve())
            if identity not in pending:
                pending.add(identity)
                self._pending_layout_pdf_cache_paths = pending

                def retry_after_translation():
                    if self.has_active_translation_task():
                        QTimer.singleShot(300, retry_after_translation)
                        return
                    self._pending_layout_pdf_cache_paths.discard(identity)
                    if self.current_layout_translation_path != html_path:
                        return
                    self.schedule_layout_pdf_cache_after_load(True)

                QTimer.singleShot(300, retry_after_translation)
            return
        try:
            loaded_path = Path(self.translation_web_view.url().toLocalFile()).resolve()
            if loaded_path != html_path.resolve():
                return
        except (OSError, RuntimeError):
            return
        def poll_ready(attempt: int = 0):
            if not self.translation_web_view or attempt >= 1200:
                return
            try:
                current_path = Path(self.translation_web_view.url().toLocalFile()).resolve()
                if current_path != html_path.resolve():
                    return
            except (OSError, RuntimeError):
                return

            def receive(ready):
                if ready is True:
                    self.ensure_background_layout_pdf_cache(html_path)
                else:
                    QTimer.singleShot(100, lambda: poll_ready(attempt + 1))

            self.translation_web_view.page().runJavaScript(
                "Boolean(document.body && document.body.classList.contains('layout-translated') "
                "&& document.body.dataset.layoutFitState === 'ready')",
                receive,
            )

        poll_ready()

    def ensure_background_layout_pdf_cache(self, html_path: Path):
        cache_path, cache_meta_path = layout_pdf_cache_paths(html_path)
        if (
            read_current_layout_cache_payload(cache_meta_path, html_path, "pdf") == self.current_layout_pdf_cache_version(html_path)
            and cache_path.exists()
            and cache_path.stat().st_size > 1024
        ):
            return
        identity = layout_artifact_identity(html_path)
        if not identity:
            return
        active = getattr(self, "_background_layout_pdf_identities", set())
        if identity in active:
            return
        active.add(identity)
        self._background_layout_pdf_identities = active
        generation = getattr(self, "_layout_pdf_cache_generation", 0)

        def finished(_success: bool):
            self._background_layout_pdf_identities.discard(identity)

        def accept_result() -> bool:
            # A translation can start after this background WebEngine print has
            # been queued.  Such a PDF belongs to the previous visible
            # revision and must never become the new cache or produce a
            # misleading "ready" notification.
            return generation == getattr(self, "_layout_pdf_cache_generation", 0)

        self.export_html_to_pdf(
            html_path,
            cache_path,
            layout_mode=True,
            notify=False,
            completion=finished,
            accept_layout_cache_result=accept_result,
        )

    def export_html_to_pdf(
        self,
        html_path: Path,
        out_path: Path,
        layout_mode: bool = False,
        notify: bool = True,
        completion=None,
        export_name: str | None = None,
        accept_layout_cache_result=None,
    ):
        if notify:
            self.append_log(f"正在生成排版 PDF：{out_path.name if hasattr(out_path, 'name') else out_path}")

        def report_success(path: Path):
            if callable(completion):
                completion(True)
            if notify:
                self.finish_export(path, export_name=export_name)

        def report_failure(message: str):
            if callable(completion):
                completion(False)
            if notify:
                QMessageBox.critical(self, "导出失败", message)

        def accept_layout_result() -> bool:
            return not callable(accept_layout_cache_result) or bool(accept_layout_cache_result())

        cache_path = None
        cache_meta_path = None
        print_target = out_path
        if layout_mode:
            cache_path, cache_meta_path = layout_pdf_cache_paths(html_path)
            pdf_cache_version = self.current_layout_pdf_cache_version(html_path)
            cached_pdf = read_current_layout_cache_payload(cache_meta_path, html_path, "pdf")
            if cached_pdf == pdf_cache_version and cache_path.exists() and cache_path.stat().st_size > 1024:
                if not accept_layout_result():
                    if callable(completion):
                        completion(False)
                    return False
                if cache_path.resolve() != out_path.resolve():
                    shutil.copy2(cache_path, out_path)
                if notify:
                    self.append_log("已直接复用已生成的排版 PDF。")
                if callable(completion):
                    completion(True)
                return False
            print_target = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp.pdf")
        view = QWebEngineView()
        configure_web_view(view)
        if not hasattr(self, "_pdf_export_views"):
            self._pdf_export_views = []
        self._pdf_export_views.append(view)

        def cleanup():
            if view in self._pdf_export_views:
                self._pdf_export_views.remove(view)
            view.deleteLater()

        def cleanup_print_target():
            if layout_mode and print_target != out_path:
                try:
                    if print_target.exists():
                        print_target.unlink()
                except OSError:
                    pass

        def on_print_finished(file_path: str, success: bool):
            cleanup()
            if success:
                if layout_mode and cache_path is not None and cache_meta_path is not None:
                    try:
                        if not accept_layout_result():
                            cleanup_print_target()
                            if callable(completion):
                                completion(False)
                            return
                        os.replace(str(Path(file_path)), str(cache_path))
                        write_layout_cache_payload(
                            cache_meta_path,
                            html_path,
                            "pdf",
                            pdf_cache_version,
                        )
                        if cache_path.resolve() != out_path.resolve():
                            shutil.copy2(cache_path, out_path)
                        if notify:
                            self.append_log("排版 PDF 生成完成并已就绪。")
                        report_success(out_path)
                    except OSError as exc:
                        cleanup_print_target()
                        report_failure(f"保存最终排版 PDF 失败: {exc}")
                else:
                    report_success(Path(file_path))
            else:
                cleanup_print_target()
                report_failure("PDF 打印失败。")

        def on_load_finished(ok: bool):
            if not ok:
                cleanup()
                cleanup_print_target()
                report_failure("PDF 预览页面加载失败。")
                return

            def print_after_ready(result=None):
                result = decode_web_javascript_payload(result) or {}
                if layout_mode and not (
                    isinstance(result, dict)
                    and result.get("ready") is True
                    and int(result.get("pageCount") or 0) > 0
                ):
                    cleanup()
                    cleanup_print_target()
                    detail = str(result.get("state") or "unknown") if isinstance(result, dict) else "unknown"
                    report_failure(f"全文排版尚未就绪，已停止生成 PDF（状态: {detail}）。")
                    return
                view.page().pdfPrintingFinished.connect(on_print_finished)
                page_layout = self.pdf_page_layout_for_export(html_path, layout_mode)

                def print_pdf(prepared=None):
                    prepared = decode_web_javascript_payload(prepared) or {}
                    if layout_mode and not (
                        isinstance(prepared, dict)
                        and prepared.get("ready") is True
                        and int(prepared.get("pageCount") or 0) > 0
                    ):
                        cleanup()
                        cleanup_print_target()
                        report_failure("PDF 打印前版面校验失败，未生成空白文件。")
                        return
                    view.page().printToPdf(str(print_target), page_layout)

                if layout_mode:
                    view.page().runJavaScript(
                        self.layout_pdf_prepare_script(
                            self.layout_pdf_reader_scale(html_path),
                            self.layout_body_font_pt_for_document(html_path),
                        ),
                        print_pdf,
                    )
                else:
                    print_pdf()

            wait_script = """
            (() => {
              const pageCount = document.querySelectorAll('.layout-page-wrap').length;
              const isLayout = pageCount > 0;
              if (isLayout && document.body.dataset.layoutFitState !== 'ready') {
                if (window.__mineruPrepareLayoutExport) {
                  try { window.__mineruPrepareLayoutExport(); } catch (_error) {}
                }
              }
              if (isLayout && document.body.dataset.layoutFitState !== 'ready') {
                return JSON.stringify({
                  ready: false,
                  state: document.body.dataset.layoutFitState || 'missing',
                  pageCount
                });
              }
              return JSON.stringify({
                ready: true,
                state: isLayout ? document.body.dataset.layoutFitState : 'flow',
                pageCount: isLayout ? pageCount : 1,
                incompleteImages: Array.from(document.images || []).filter((img) => !img.complete).length
              });
            })();
            """
            view.page().runJavaScript(wait_script, print_after_ready)

        view.loadFinished.connect(on_load_finished)
        view.setUrl(QUrl.fromLocalFile(str(html_path.resolve())))
        return True

    def finish_export(
        self,
        out_path: Path,
        extra_warnings: list[str] | None = None,
        export_name: str | None = None,
    ):
        self.append_log(f"导出成功：{out_path}")
        if not export_name:
            export_name = f"{self.export_file_type_label(out_path.suffix.lstrip('.'))}文件"
        # QMessageBox 在部分 Windows/Qt 组合中仍会触发系统提示音。
        # 导出完成改用普通 Qt 窗口，彻底绕开 Windows MessageBox 路径。
        dialog = QDialog(self)
        dialog.setWindowTitle("导出完成")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        message = QLabel(f"{export_name}已导出到：\n{out_path}\n\n是否直接打开？", dialog)
        message.setWordWrap(True)
        layout.addWidget(message)
        buttons = QDialogButtonBox(dialog)
        open_button = buttons.addButton("直接打开", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        open_button.setDefault(False)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(out_path.resolve()))):
                QMessageBox.warning(self, "无法打开文件", f"系统默认程序无法打开：\n{out_path}")

    def show_placeholder(self, web_view, fallback_viewer: QTextBrowser, text: str):
        if self.is_suspended_main_preview_target(web_view):
            return
        if web_view is self.source_web_view:
            set_source_pdf_active(self, False)
        font_face_css = bundled_reader_font_face_css()
        html = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<style>{font_face_css}</style></head>"
            f"<body style='font-family:{READER_SERIF_FONT_STACK};padding:24px;color:#666;'>"
            f"{text}</body></html>"
        )
        if web_view:
            self.clear_layout_retranslation_notice(web_view)
            self.clear_layout_transition_overlay(web_view)
            web_view.setHtml(html)
        else:
            fallback_viewer.setHtml(html)

    def show_live_translation(self, markdown: str, mode: str = "stream"):
        active_key = str(self.active_translation_source_path.resolve()) if self.active_translation_source_path else ""
        target_cache = self.live_layout_translation_by_source if mode == "layout" else self.live_translation_by_source
        if self.active_translation_source_path:
            target_cache[active_key] = markdown
        for reader in list(self.reader_windows):
            if active_key and reader.source_path and str(reader.source_path.resolve()) != active_key:
                continue
            if bool(reader.layout_reading_mode) != (mode == "layout"):
                continue
            reader.show_live_translation(markdown)

        current_key = str(self.current_source_path.resolve()) if self.current_source_path else ""
        if active_key and current_key and current_key != active_key:
            return
        if mode == "layout":
            self.live_layout_translation_markdown = markdown
        else:
            self.live_translation_markdown = markdown
        if bool(self.settings.layout_reading_mode) != (mode == "layout"):
            return
        if self.is_suspended_main_preview_target(self.translation_web_view):
            return

        if self.translation_web_view:
            self._translation_live_pending_markdown = markdown

            def update_live_page():
                safe_text = json.dumps(self._translation_live_pending_markdown, ensure_ascii=False)
                js = f"""
                (() => {{
                    const el = document.getElementById('live-content');
                    if (el) {{
                        el.textContent = {safe_text};
                        window.scrollTo(0, document.body.scrollHeight);
                    }}
                }})();
                """
                self.translation_web_view.page().runJavaScript(js)

            if not self._translation_live_page_ready:
                escaped = html.escape(markdown)
                live_html = f"""
                <!doctype html>
                <html>
                <head>
                <meta charset="utf-8">
                <style>
                    {bundled_reader_font_face_css()}
                    html, body {{
                        margin: 0;
                        min-height: 100%;
                        background: #fbfcfd;
                        color: #1f2933;
                    }}
                    body {{
                        padding: 22px;
                        box-sizing: border-box;
                        font-family: {READER_SERIF_FONT_STACK};
                        line-height: 1.65;
                        font-size: 15px;
                    }}
                    pre {{
                        white-space: pre-wrap;
                        word-wrap: break-word;
                        font-family: {READER_SERIF_FONT_STACK};
                        font-size: 14px;
                        margin: 0;
                    }}
                </style>
                </head>
                <body>
                    <pre id="live-content">{escaped}</pre>
                    <script>window.scrollTo(0, document.body.scrollHeight);</script>
                </body>
                </html>
                """
                def on_live_page_loaded(ok: bool):
                    try:
                        self.translation_web_view.loadFinished.disconnect(on_live_page_loaded)
                    except Exception:
                        pass
                    if ok:
                        update_live_page()
                        self.apply_reader_font_size()

                self.translation_web_view.loadFinished.connect(on_live_page_loaded)
                self.translation_web_view.setHtml(live_html)
                self._translation_live_page_ready = True
            else:
                update_live_page()
                self.apply_reader_font_size()
        else:
            self.translation_fallback_viewer.setMarkdown(markdown)
            self.apply_reader_font_size()
            self.translation_fallback_viewer.verticalScrollBar().setValue(
                self.translation_fallback_viewer.verticalScrollBar().maximum()
            )

    def show_live_translation_delta(self, delta: str, reset: bool = False, mode: str = "stream"):
        """Publish one translation suffix while preserving all legacy full-snapshot paths."""
        delta = str(delta or "")
        active_key = str(self.active_translation_source_path.resolve()) if self.active_translation_source_path else ""
        target_cache = self.live_layout_translation_by_source if mode == "layout" else self.live_translation_by_source
        previous = target_cache.get(active_key, "") if active_key else (
            self.live_layout_translation_markdown if mode == "layout" else self.live_translation_markdown
        )
        markdown = delta if reset else previous + delta
        if active_key:
            target_cache[active_key] = markdown
        for reader in list(self.reader_windows):
            if active_key and reader.source_path and str(reader.source_path.resolve()) != active_key:
                continue
            if bool(reader.layout_reading_mode) != (mode == "layout"):
                continue
            reader.show_live_translation_delta(delta, reset=reset)

        current_key = str(self.current_source_path.resolve()) if self.current_source_path else ""
        if active_key and current_key and current_key != active_key:
            return
        if mode == "layout":
            self.live_layout_translation_markdown = markdown
        else:
            self.live_translation_markdown = markdown
        if bool(self.settings.layout_reading_mode) != (mode == "layout"):
            return
        if self.is_suspended_main_preview_target(self.translation_web_view):
            return
        if not self.translation_web_view or not self._translation_live_page_ready:
            self.show_live_translation(markdown, mode=mode)
            return
        safe_delta = json.dumps(delta, ensure_ascii=False)
        reset_js = "true" if reset else "false"
        self.translation_web_view.page().runJavaScript(
            f"""
            (() => {{
                const el = document.getElementById('live-content');
                if (!el) return;
                if ({reset_js}) el.textContent = {safe_delta};
                else {{
                    let tail = el.lastChild;
                    if (!tail || tail.nodeType !== Node.TEXT_NODE || tail.data.length > 16384) {{
                        tail = document.createTextNode('');
                        el.appendChild(tail);
                    }}
                    tail.appendData({safe_delta});
                }}
                window.scrollTo(0, document.body.scrollHeight);
            }})();
            """
        )

    def load_saved_translation_config(self) -> AITranslateConfig | None:
        provider_id = self.settings.ai_provider or "zai"
        if machine_translate.is_machine_translation_provider(provider_id):
            provider = self.settings.providers.get(provider_id)
            is_local = provider_id == machine_translate.MTRAN_SERVER_PROVIDER
            base_url = provider.base_url if provider and provider.base_url else (machine_translate.MTRAN_SERVER_DEFAULT_BASE_URL if is_local else "")
            api_key = app_config.load_secret(provider_id, "api_key") if is_local else ""
            return AITranslateConfig(provider_id, api_key, base_url, machine_translate.provider_label(provider_id))
        provider = self.settings.providers.get(provider_id)
        api_key = app_config.load_secret(provider_id, "api_key")
        if not api_key:
            try:
                config = build_ai_endpoint_config(provider_id)
                api_key = config.api_key
                base_url = config.base_url
            except Exception:
                return None
        else:
            base_url = provider.base_url if provider and provider.base_url else provider_runtime_default_url(provider_id)
        model = provider.model if provider and provider.model else ""
        if not model:
            return None
        return AITranslateConfig(
            provider_id,
            api_key,
            normalize_ai_base_url(base_url, provider_id),
            model,
            request_body_mode=getattr(provider, "request_body_mode", "codex"),
            thinking_mode=(
                "enabled"
                if (
                    provider_id == "deepseek"
                    and getattr(self.settings, "translation_deepseek_thinking_enabled", True)
                    and not (
                        getattr(self.settings, "layout_reading_mode", False)
                        and getattr(self.settings, "translation_deepseek_fast_layout_enabled", True)
                    )
                )
                or (
                    provider_id == "gemini" and getattr(self.settings, "translation_gemini_thinking_enabled", False)
                )
                else "disabled"
            ),
            reasoning_effort=(
                getattr(self.settings, "translation_gemini_reasoning_effort", "medium")
                if provider_id == "gemini"
                else getattr(self.settings, "translation_deepseek_reasoning_effort", "default")
            ),
            deepseek_fast_layout_translation=(
                provider_id == "deepseek"
                and bool(getattr(self.settings, "translation_deepseek_fast_layout_enabled", True))
            ),
            custom_translation_instruction=str(
                getattr(self.settings, "translation_custom_instruction", "") or ""
            ),
        )

    def save_translation_preferences(
        self,
        target_language: str,
        mode: str,
        reference_paths: list[str] | None = None,
        source_language: str | None = None,
        local_machine_parallelism: int | None = None,
    ):
        if source_language is not None:
            self.settings.translation_source_language = (source_language or "英文").strip() or "英文"
        if local_machine_parallelism is not None:
            self.settings.local_machine_parallelism = machine_translate.normalize_parallelism(local_machine_parallelism)
        self.settings.translation_target_language = (target_language or "简体中文").strip() or "简体中文"
        self.settings.translation_mode = mode or "full_context"
        self.settings.translation_reference_paths = list(reference_paths or [])
        app_config.save_settings(self.settings)

    def build_saved_translation_job_config(self, ai_config: AITranslateConfig) -> TranslationJobConfig:
        return TranslationJobConfig(
            ai_config=ai_config,
            source_language=(getattr(self.settings, "translation_source_language", "英文") or "英文").strip() or "英文",
            target_language=(self.settings.translation_target_language or "简体中文").strip() or "简体中文",
            mode=self.settings.translation_mode or "full_context",
            reference_paths=list(self.settings.translation_reference_paths or []),
            local_machine_parallelism=machine_translate.normalize_parallelism(getattr(self.settings, "local_machine_parallelism", machine_translate.MTRAN_SERVER_DEFAULT_PARALLELISM)),
        )

    def is_long_pdf_source(self, source: Path, page_limit: int = 150) -> bool:
        """判断解析结果对应的原 PDF 是否超过安全页数。"""
        original_pdf = find_stored_original(source.parent)
        if not original_pdf or original_pdf.suffix.lower() != ".pdf":
            return False
        try:
            document = pdfium.PdfDocument(str(original_pdf))
            try:
                return len(document) > page_limit
            finally:
                document.close()
        except Exception:
            return False

    def recommend_chunked_mode_for_long_pdf(self, source: Path, mode: str) -> str:
        """单篇翻译时提示用户将长文档切换为分块模式。"""
        if not self.is_long_pdf_source(source):
            return mode
        prompt = QMessageBox(self)
        prompt.setWindowTitle("长文档翻译建议")
        prompt.setTextFormat(Qt.TextFormat.RichText)
        prompt.setText(
            "当前所译文档超过150页，建议将翻译模式切换为"
            "<span style='font-size:18pt; font-weight:700;'>分块翻译</span>继续全文连续翻译。"
        )
        prompt.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        prompt.setDefaultButton(QMessageBox.StandardButton.Yes)
        return "chunked" if prompt.exec() == QMessageBox.StandardButton.Yes else mode

    def clone_translation_job_config_for_source(
        self,
        base: TranslationJobConfig,
        source: Path,
        request_concurrency: int = 1,
        log_mode_change: bool = False,
    ) -> TranslationJobConfig:
        """复制配置，并对长文档应用批量模式下的安全规则。"""
        mode = base.mode
        if str(mode or "").strip().lower() not in {"chunked", "chunks"} and self.is_long_pdf_source(source):
            mode = "chunked"
            if log_mode_change:
                self.append_log(f"[{source.parent.name}] 文档篇幅较长（超过 150 页），已自动采用分块连贯翻译模式。")
        return TranslationJobConfig(
            ai_config=base.ai_config,
            source_language=base.source_language,
            target_language=base.target_language,
            mode=mode,
            reference_paths=list(base.reference_paths),
            local_machine_parallelism=base.local_machine_parallelism,
            request_concurrency=request_concurrency if mode == "chunked" else 1,
        )

    def batch_translation_job_config_for_source(self, source: Path) -> TranslationJobConfig:
        """为实际工作线程生成独立配置，避免线程间共享可变字段。"""
        return self.clone_translation_job_config_for_source(
            self._batch_translation_config,
            source,
            request_concurrency=self._batch_request_concurrency,
            log_mode_change=True,
        )

    def batch_stream_translation_candidates(self, job_config: TranslationJobConfig) -> list[Path]:
        candidates: list[Path] = []
        for doc in self.docs:
            if doc.markdown_path.name != "full.cleaned.md":
                continue
            effective_config = self.clone_translation_job_config_for_source(job_config, doc.markdown_path)
            if not translation_output_matches_job(doc.markdown_path, effective_config):
                candidates.append(doc.markdown_path)
        return candidates

    def batch_layout_translation_candidates(self, job_config: TranslationJobConfig) -> list[Path]:
        candidates: list[Path] = []
        for doc in self.docs:
            if doc.markdown_path.name != "full.cleaned.md" or not load_layout_preview_bundle(doc.markdown_path):
                continue
            effective_config = self.clone_translation_job_config_for_source(job_config, doc.markdown_path)
            if not layout_translation_matches_job(doc.markdown_path, effective_config):
                candidates.append(doc.markdown_path)
        return candidates

    def clear_translate_worker(self, worker):
        if self.translate_worker is worker:
            self.translate_worker = None

    def handle_translation_preview_for_source(self, source: Path, markdown: str, mode: str):
        source_key = str(source.resolve())
        target_cache = self.live_layout_translation_by_source if mode == "layout" else self.live_translation_by_source
        target_cache[source_key] = markdown
        for reader in list(self.reader_windows):
            if not reader.source_path or str(reader.source_path.resolve()) != source_key:
                continue
            if bool(reader.layout_reading_mode) != (mode == "layout"):
                continue
            reader.show_live_translation(markdown)

        current_key = str(self.current_source_path.resolve()) if self.current_source_path else ""
        if current_key and current_key != source_key:
            return
        if mode == "layout":
            self.live_layout_translation_markdown = markdown
        else:
            self.live_translation_markdown = markdown
        self.show_live_translation(markdown, mode=mode)

    def confirm_edge_model_download(self, request: dict) -> None:
        """Answer a worker's one-time local Edge model download request on the GUI thread."""
        try:
            source = str(request.get("source") or "自动检测")
            target = str(request.get("target") or "目标语言")
            answer = QMessageBox.question(
                self,
                "下载 Edge 本地翻译模型",
                f"Edge 本地翻译需要下载 {source} → {target} 的本地翻译模型。\n\n"
                "模型仅保存在本机并可在后续离线使用；是否现在下载？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            request["approved"] = answer == QMessageBox.Yes
        finally:
            event = request.get("event")
            if event is not None:
                event.set()

    def start_translation_job(
        self,
        source: Path,
        job_config: TranslationJobConfig,
        allow_parse_handoff: bool = False,
    ):
        if self.reject_new_processing_task("翻译", allow_parse_handoff=allow_parse_handoff):
            return False
        job_config.mode = self.recommend_chunked_mode_for_long_pdf(source, job_config.mode)
        self.save_translation_preferences(
            job_config.target_language, job_config.mode, job_config.reference_paths,
            job_config.source_language, job_config.local_machine_parallelism,
        )
        self.translate_button.setEnabled(False)
        self.batch_translate_button.setEnabled(False)
        self.clear_logs()
        self.begin_task_ui(show_reasoning=not is_free_machine_translation_config(job_config.ai_config))
        self.active_translation_source_path = source
        self.active_translation_preview_mode = "stream"
        # The previous document may have left a completed HTML page loaded.
        # Start a fresh live page so the first streamed content is visible.
        self._translation_live_page_ready = False
        self._translation_live_pending_markdown = ""
        worker = TranslateWorker(str(source), job_config)
        self.translate_worker = worker
        worker.edge_download_signal.connect(self.confirm_edge_model_download)
        worker.log_signal.connect(self.append_log)
        worker.reasoning_signal.connect(self.append_reasoning_log)
        worker.preview_delta_signal.connect(
            lambda delta, reset: self.show_live_translation_delta(delta, reset=reset, mode="stream")
        )
        worker.finished_signal.connect(self.finish_translation)
        worker.finished.connect(lambda: self.clear_translate_worker(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()
        return True

    def start_layout_translation_job(
        self,
        source: Path,
        ai_config: AITranslateConfig,
        target_language: str,
        source_language: str = "英文",
        local_machine_parallelism: int = machine_translate.MTRAN_SERVER_DEFAULT_PARALLELISM,
        reference_paths: list[str] | None = None,
        translation_mode: str = "full_context",
        force: bool = False,
        preserve_existing_preview: bool = False,
        allow_parse_handoff: bool = False,
    ):
        if self.reject_new_processing_task("排版翻译", allow_parse_handoff=allow_parse_handoff):
            return False
        if not load_layout_preview_bundle(source):
            QMessageBox.critical(self, "缺少版面数据", "当前文档缺少 MinerU layout.json，无法进行排版翻译。请重新解析该文档。")
            return False
        translation_mode = self.recommend_chunked_mode_for_long_pdf(source, translation_mode)
        # This entrypoint is itself the authoritative “layout translation”
        # context. Do not rely on the reader's current visual mode: users can
        # start a layout retranslation while viewing the stream layout.
        ai_config = copy.copy(ai_config)
        fast_enabled = bool(
            ai_config.provider_id == "deepseek"
            and getattr(self.settings, "translation_deepseek_fast_layout_enabled", True)
        )
        ai_config.deepseek_fast_layout_translation = fast_enabled
        if fast_enabled:
            ai_config.thinking_mode = "disabled"
            fast_status_log = "DeepSeek 高速并发翻译已启用：将执行标题预热、正文缓存探针和最多 100 路并发。"
        elif ai_config.provider_id == "deepseek":
            fast_status_log = "DeepSeek 高速并发翻译未启用：使用当前常规排版翻译模式。"
        else:
            fast_status_log = ""
        self.save_translation_preferences(
            target_language, translation_mode, reference_paths, source_language, local_machine_parallelism,
        )
        self.translate_button.setEnabled(False)
        self.batch_translate_button.setEnabled(False)
        self.clear_logs()
        if fast_status_log:
            self.append_log(fast_status_log)
        self.begin_task_ui(show_reasoning=not is_free_machine_translation_config(ai_config))
        # Invalidate any PDF print queued by the previously displayed
        # translation before this new revision starts streaming.
        self._layout_pdf_cache_generation = getattr(self, "_layout_pdf_cache_generation", 0) + 1
        self.active_translation_source_path = source
        self.active_translation_preview_mode = "layout"
        # show_placeholder() replaces the page and has no #live-content node.
        # Reset this flag so the first progress/stream update builds a live page
        # instead of silently trying to update the placeholder.
        self._translation_live_page_ready = False
        self._translation_live_pending_markdown = ""
        is_current_source = bool(
            self.current_source_path
            and self.current_source_path.exists()
            and self.current_source_path.resolve() == source.resolve()
        )
        has_published_preview = bool(
            preserve_existing_preview
            and is_current_source
            and self.current_layout_translation_path
            and self.current_layout_translation_path.exists()
        )
        if has_published_preview:
            self.show_layout_retranslation_notice(self.translation_web_view)
        elif is_current_source:
            self.show_placeholder(self.translation_web_view, self.translation_fallback_viewer, "正在生成排版译文，请稍候...")
        worker = LayoutTranslateWorker(
            str(source),
            ai_config,
            target_language,
            source_language=source_language,
            local_machine_parallelism=local_machine_parallelism,
            request_concurrency=translation_request_concurrency_limit(
                ai_config.provider_id
            ),
            reference_paths=reference_paths,
            translation_mode=translation_mode,
            force=force,
        )
        self.translate_worker = worker
        worker.edge_download_signal.connect(self.confirm_edge_model_download)
        worker.log_signal.connect(self.append_log)
        worker.reasoning_signal.connect(self.append_reasoning_log)
        worker.preview_signal.connect(lambda markdown: self.show_live_translation(markdown, mode="layout"))
        worker.finished_signal.connect(self.finish_layout_translation)
        worker.finished.connect(lambda: self.clear_translate_worker(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()
        return True

    def ensure_translation_model_configured(self) -> AITranslateConfig | None:
        config = self.load_saved_translation_config()
        if config:
            return config
        QMessageBox.information(self, "需要翻译模型", "请先在“模型、服务与工作文件夹”中配置翻译 API 密钥和模型。")
        self.show_mineru_options_dialog()
        self.settings = app_config.load_settings()
        return self.load_saved_translation_config()

    def retranslate_document_with_current_settings(self, source: Path):
        if not source or not source.exists():
            QMessageBox.critical(self, "错误", "当前解析后的 Markdown 不存在，无法重新翻译。")
            return
        ai_config = self.ensure_translation_model_configured()
        if not ai_config:
            return
        job_config = self.build_saved_translation_job_config(ai_config)
        if job_config.reference_paths and not is_free_machine_translation_config(ai_config) and is_lightweight_ai_model(ai_config.model):
            QMessageBox.warning(
                self,
                "建议使用更强模型",
                "当前保存的翻译配置包含参考文件。该模式会把完整参考语料直接放入翻译上下文，"
                "建议使用支持长上下文的模型；mini、flash、lite 等轻量模型可能丢失细节或忽略参考语感。",
            )
        self.start_translation_job(source, job_config)

    def translate_current_doc(self):
        source = self.current_source_path or self.current_markdown_path
        if not source or not source.exists():
            QMessageBox.critical(self, "错误", "请先在左侧打开一个解析后的 Markdown。")
            return
        dialog = TranslationOptionsDialog(self, provider_id=self.settings.ai_provider)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        ai_config = dialog.selected_ai_config
        if not ai_config:
            return
        if dialog.reference_paths and not is_free_machine_translation_config(ai_config) and is_lightweight_ai_model(ai_config.model):
            QMessageBox.warning(
                self,
                "建议使用更强模型",
                "你已选择参考文件。该功能会把完整参考语料直接放入翻译上下文，需要模型同时阅读原文、参考语料和翻译约束，"
                "建议使用支持长上下文的模型；mini、flash、lite 等轻量模型可能丢失细节、忽略参考语感、超出上下文或过度模仿参考风格。",
            )
        target_language = dialog.target_combo.currentText().strip() or "简体中文"
        source_language = str(dialog.source_combo.currentData() or dialog.source_combo.currentText().strip() or "英文")
        local_parallelism = machine_translate.normalize_parallelism(dialog.local_parallel_spin.value())
        mode = dialog.mode_combo.currentData() or "full_context"
        self.save_translation_preferences(target_language, mode, dialog.reference_paths, source_language, local_parallelism)
        if self.settings.layout_reading_mode:
            self.start_layout_translation_job(
                source,
                ai_config,
                target_language,
                source_language=source_language,
                local_machine_parallelism=local_parallelism,
                reference_paths=dialog.reference_paths,
                translation_mode=mode,
            )
            return
        job_config = TranslationJobConfig(
            ai_config=ai_config,
            source_language=source_language,
            target_language=target_language,
            mode=mode,
            reference_paths=dialog.reference_paths,
            local_machine_parallelism=local_parallelism,
        )
        self.start_translation_job(source, job_config)

    def start_batch_translate(self):
        if self.reject_new_processing_task("批量翻译"):
            return
        layout_batch = bool(self.settings.layout_reading_mode)
        dialog = TranslationOptionsDialog(self, provider_id=self.settings.ai_provider)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        ai_config = dialog.selected_ai_config
        if not ai_config:
            return
        if dialog.reference_paths and not is_free_machine_translation_config(ai_config) and is_lightweight_ai_model(ai_config.model):
            QMessageBox.warning(
                self,
                "建议使用更强模型",
                "你已选择参考文件。批量翻译会把同一完整直接参考语料包用于本批文档，"
                "建议使用支持长上下文的模型；mini、flash、lite 等轻量模型可能丢失细节、忽略参考语感、超出上下文或过度模仿参考风格。",
            )
        self._batch_translation_config = TranslationJobConfig(
            ai_config=ai_config,
            source_language=str(dialog.source_combo.currentData() or dialog.source_combo.currentText().strip() or "英文"),
            target_language=dialog.target_combo.currentText().strip() or "简体中文",
            mode=dialog.mode_combo.currentData() or "full_context",
            reference_paths=list(dialog.reference_paths),
            local_machine_parallelism=machine_translate.normalize_parallelism(dialog.local_parallel_spin.value()),
        )

        # Match existing outputs against the complete current translation configuration.
        if layout_batch:
            candidates = self.batch_layout_translation_candidates(self._batch_translation_config)
        else:
            candidates = self.batch_stream_translation_candidates(self._batch_translation_config)
        if not candidates:
            message = (
                "左侧解析结果中没有需要按当前配置生成的排版译文。"
                if layout_batch
                else "左侧解析结果中没有需要按当前配置生成的流式译文。"
            )
            QMessageBox.information(self, "暂无待翻译文档", message)
            return

        document_concurrency, request_concurrency = self.effective_batch_translation_concurrency(
            self._batch_translation_config,
            candidates,
        )
        self._batch_translate_concurrency = document_concurrency
        self._batch_layout_translate_concurrency = document_concurrency
        self._batch_request_concurrency = request_concurrency
        self._task_stop_requested = False
        self._batch_translation_layout_mode = layout_batch
        self.save_translation_preferences(
            self._batch_translation_config.target_language,
            self._batch_translation_config.mode,
            self._batch_translation_config.reference_paths,
            self._batch_translation_config.source_language,
            self._batch_translation_config.local_machine_parallelism,
        )
        self.clear_logs()
        self.begin_task_ui(show_reasoning=not is_free_machine_translation_config(ai_config))
        self._batch_parse_total = 0
        self._batch_parse_done = 0
        self._batch_parse_failed = 0
        self._batch_parse_skipped = 0
        self._batch_parse_active_status = {}
        self._batch_translate_active_status = {}
        self.set_batch_progress_visible(True)
        self.translate_button.setEnabled(False)
        self.batch_translate_button.setEnabled(False)

        if layout_batch:
            self._batch_layout_translate_queue = list(candidates)
            self._batch_translate_total = len(candidates)
            self._batch_translate_done = 0
            self._batch_translate_failed = 0
            self._batch_layout_translate_total = len(candidates)
            self._batch_layout_translate_done = 0
            self._batch_layout_translate_failed = 0
            self.append_log(
                f"开始批量排版翻译，共 {len(candidates)} 个文档。"
                f"文档并发 {document_concurrency}，单文档内部请求并发 {request_concurrency}。"
            )
            self.update_batch_progress_panel()
            self.dispatch_batch_layout_translation()
            return

        self.batch_translate_queue = list(candidates)
        self._batch_translate_total = len(candidates)
        self._batch_translate_done = 0
        self._batch_translate_failed = 0
        self.append_log(
            f"开始批量翻译，共 {len(candidates)} 个文档。"
            f"文档并发 {document_concurrency}，单文档内部请求并发 {request_concurrency}。"
        )
        self.update_batch_progress_panel()
        self.dispatch_batch_translation()

    def start_batch_translate_for_sources(self, sources: list[Path], layout_batch: bool, title_prefix: str = "批量翻译"):
        if self.has_active_parse_task() or self.has_active_translation_task():
            self.append_log(f"{title_prefix}：检测到其他任务正在运行，已取消接续翻译。")
            self.finish_task_ui()
            return
        candidates = [Path(path) for path in sources if path and Path(path).exists()]
        missing_layout_count = 0
        if layout_batch:
            layout_candidates = [path for path in candidates if load_layout_preview_bundle(path)]
            missing_layout_count = len(candidates) - len(layout_candidates)
            candidates = layout_candidates
        if missing_layout_count:
            self.append_log(f"{title_prefix}：{missing_layout_count} 个文档未找到排版数据，已跳过排版翻译。")
        if not candidates:
            self.append_log(f"{title_prefix}：当前没有可翻译的文档。")
            self.finish_task_ui()
            return
        if not getattr(self, "_batch_translation_config", None):
            self.append_log(f"{title_prefix}：缺少有效翻译配置，已跳过接续翻译。")
            self.finish_task_ui()
            return

        document_concurrency, request_concurrency = self.effective_batch_translation_concurrency(
            self._batch_translation_config,
            candidates,
        )
        self._batch_translate_concurrency = document_concurrency
        self._batch_layout_translate_concurrency = document_concurrency
        self._batch_request_concurrency = request_concurrency
        self._task_stop_requested = False
        self._batch_translation_layout_mode = bool(layout_batch)
        self._batch_translate_total = len(candidates)
        self._batch_translate_done = 0
        self._batch_translate_failed = 0
        if self.log_dialog is None:
            self.begin_task_ui(show_reasoning=not is_free_machine_translation_config(self._batch_translation_config.ai_config))
        else:
            self.show_log_dialog(running=True, show_reasoning=not is_free_machine_translation_config(self._batch_translation_config.ai_config))
        self.set_batch_progress_visible(True)
        self.translate_button.setEnabled(False)
        self.batch_translate_button.setEnabled(False)
        if layout_batch:
            self._batch_layout_translate_queue = list(candidates)
            self._batch_layout_translate_total = len(candidates)
            self._batch_layout_translate_done = 0
            self._batch_layout_translate_failed = 0
            self.append_log(
                f"{title_prefix}：开始批量排版翻译，共 {len(candidates)} 个文档"
                f"（文档并发 {document_concurrency}，请求并发 {request_concurrency}）。"
            )
            self.update_batch_progress_panel()
            self.dispatch_batch_layout_translation()
        else:
            self.batch_translate_queue = list(candidates)
            self.append_log(
                f"{title_prefix}：开始批量翻译，共 {len(candidates)} 个文档"
                f"（文档并发 {document_concurrency}，请求并发 {request_concurrency}）。"
            )
            self.update_batch_progress_panel()
            self.dispatch_batch_translation()

    def dispatch_batch_translation(self):
        while (
            not self._task_stop_requested
            and self.batch_translate_queue
            and len(self.running_translate_workers) < self._batch_translate_concurrency
        ):
            self.start_batch_translate_worker(self.batch_translate_queue.pop(0))
        self.maybe_finish_batch_translation()

    def dispatch_batch_layout_translation(self):
        while (
            not self._task_stop_requested
            and self._batch_layout_translate_queue
            and len(self.running_translate_workers) < self._batch_layout_translate_concurrency
        ):
            self.start_batch_layout_translate_worker(self._batch_layout_translate_queue.pop(0))
        self.maybe_finish_batch_translation()

    def start_batch_translate_worker(self, source: Path):
        ordinal = self._batch_translate_done + len(self.running_translate_workers) + 1
        label = f"[翻译 {ordinal}/{self._batch_translate_total}][{source.parent.name}]"
        job_config = self.batch_translation_job_config_for_source(source)
        worker = TranslateWorker(str(source), job_config)
        worker.edge_download_signal.connect(self.confirm_edge_model_download)
        worker_id = id(worker)
        self.running_translate_workers[worker_id] = worker
        self.running_translate_sources[worker_id] = source
        self._batch_translate_active_status[source.parent.name] = "正在准备内容"
        worker.log_signal.connect(
            lambda text, src=source, prefix=label: self.handle_batch_translate_worker_log(src, prefix, text)
        )
        worker.reasoning_signal.connect(self.append_reasoning_log)
        worker.preview_signal.connect(lambda markdown, src=source: self.handle_translation_preview_for_source(src, markdown, "stream"))
        worker.finished_signal.connect(
            lambda success, message, markdown_path, wid=worker_id: self.finish_batch_translate_item(
                wid, success, message, markdown_path, False
            )
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self.update_batch_progress_panel()

    def start_batch_layout_translate_worker(self, source: Path):
        ordinal = self._batch_translate_done + len(self.running_translate_workers) + 1
        label = f"[排版翻译 {ordinal}/{self._batch_translate_total}][{source.parent.name}]"
        # Batch workers can start while a previously viewed layout still has a
        # background PDF print queued.  Make that obsolete before dispatch.
        self._layout_pdf_cache_generation = getattr(self, "_layout_pdf_cache_generation", 0) + 1
        job_config = self.batch_translation_job_config_for_source(source)
        worker = LayoutTranslateWorker(
            str(source),
            job_config.ai_config,
            job_config.target_language,
            source_language=job_config.source_language,
            local_machine_parallelism=job_config.local_machine_parallelism,
            request_concurrency=job_config.request_concurrency,
            reference_paths=job_config.reference_paths,
            translation_mode=job_config.mode,
        )
        worker.edge_download_signal.connect(self.confirm_edge_model_download)
        worker_id = id(worker)
        self.running_translate_workers[worker_id] = worker
        self.running_translate_sources[worker_id] = source
        self._batch_translate_active_status[source.parent.name] = "正在准备版面"
        worker.log_signal.connect(
            lambda text, src=source, prefix=label: self.handle_batch_translate_worker_log(src, prefix, text)
        )
        worker.reasoning_signal.connect(self.append_reasoning_log)
        worker.preview_signal.connect(lambda markdown, src=source: self.handle_translation_preview_for_source(src, markdown, "layout"))
        worker.finished_signal.connect(
            lambda success, message, html_path, wid=worker_id: self.finish_batch_translate_item(
                wid, success, message, html_path, True
            )
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self.update_batch_progress_panel()

    def maybe_finish_batch_translation(self):
        queue = self._batch_layout_translate_queue if self._batch_translation_layout_mode else self.batch_translate_queue
        if self.running_translate_workers:
            return
        if queue and not self._task_stop_requested:
            return
        # Parse-and-translate is a pipeline: an empty queue only means the
        # parser has not yielded its next document yet, not that work is done.
        if self._batch_parse_translation_accepting_sources and not self._task_stop_requested:
            self.update_batch_progress_panel()
            return
        success_count = max(0, self._batch_translate_done - self._batch_translate_failed)
        title = "批量排版翻译" if self._batch_translation_layout_mode else "批量翻译"
        if self._task_stop_requested:
            self.append_log(f"{title}已停止。")
        elif self._batch_translate_failed:
            self.append_log(f"{title}完成：成功 {success_count} 个，失败 {self._batch_translate_failed} 个。")
        else:
            self.append_log(f"{title}完成：成功 {success_count} 个。")
        self._task_stop_requested = False
        self._batch_translate_active_status = {}
        self.reset_translation_task_state()
        self.refresh_docs()
        self.update_translate_button_visibility()
        self.finish_task_ui()

    def finish_batch_translate_item(self, worker_id: int, success: bool, message: str, output_path: str, is_layout: bool):
        worker = self.running_translate_workers.pop(worker_id, None)
        source = self.running_translate_sources.pop(worker_id, None)
        if worker is self.translate_worker:
            self.translate_worker = None
        self._batch_translate_done += 1
        if is_layout:
            self._batch_layout_translate_done += 1
        label = f"[{source.parent.name}]" if source else "[批量翻译]"
        if source:
            self._batch_translate_active_status.pop(source.parent.name, None)
        if success:
            self.append_log(f"{label} {message}")
            if source:
                source_key = str(source.resolve())
                if is_layout:
                    self.live_layout_translation_by_source.pop(source_key, None)
                else:
                    self.live_translation_by_source.pop(source_key, None)
            if source and self.current_source_path and source.resolve() == self.current_source_path.resolve():
                if is_layout:
                    path = Path(output_path)
                    self.current_layout_translation_path = path if path.exists() else None
                    self.live_layout_translation_markdown = ""
                    if self.current_layout_translation_path:
                        self.show_html_in_view(
                            self.current_layout_translation_path,
                            self.translation_web_view,
                            self.translation_fallback_viewer,
                        )
                elif output_path:
                    self.load_markdown(Path(output_path))
            if success and source and output_path:
                path = Path(output_path)
                for reader in list(self.reader_windows):
                    if not reader.source_path or reader.source_path.resolve() != source.resolve():
                        continue
                    if bool(reader.layout_reading_mode) != is_layout:
                        continue
                    reader.translation_path = path if path.exists() else None
                    reader.live_translation_markdown = ""
                    reader.refresh_content()
        else:
            self._batch_translate_failed += 1
            if is_layout:
                self._batch_layout_translate_failed += 1
            cache_protected = (
                is_layout
                and str(message).startswith("DEEPSEEK_FAST_CACHE_PROTECTION:")
            )
            display_message = (
                str(message).split(":", 1)[1]
                if cache_protected
                else str(message)
            )
            if cache_protected:
                self._task_stop_requested = True
                self._batch_layout_translate_queue.clear()
                QMessageBox.warning(self, "DeepSeek 高速翻译已停止", display_message)
            self.append_log(f"{label} 失败: {display_message}")
        self.update_batch_progress_panel()
        if self._batch_translation_layout_mode:
            self.dispatch_batch_layout_translation()
        else:
            self.dispatch_batch_translation()
        self.update_translate_button_visibility()

    def finish_translation(self, success: bool, message: str, markdown_path: str):
        if self._task_stop_requested:
            self.append_log("翻译已停止。")
            self.reset_translation_task_state()
            self._task_stop_requested = False
            self.finish_task_ui()
            return
        self.translate_button.setEnabled(True)
        self.batch_translate_button.setEnabled(True)
        if success:
            self.append_log(message)
            self.refresh_docs()
            active_source = self.active_translation_source_path
            if active_source:
                source_key = str(active_source.resolve())
                self.live_translation_by_source.pop(source_key, None)
                self.live_translation_markdown = ""
            self.active_translation_source_path = None
            self.load_markdown(Path(markdown_path))
            for reader in list(self.reader_windows):
                if not active_source or not reader.source_path or reader.layout_reading_mode:
                    continue
                if reader.source_path.resolve() != active_source.resolve():
                    continue
                reader.translation_path = Path(markdown_path)
                reader.live_translation_markdown = ""
                reader.refresh_content()
        else:
            if self.active_translation_source_path:
                source_key = str(self.active_translation_source_path.resolve())
                self.live_translation_by_source.pop(source_key, None)
                if self.current_source_path and self.current_source_path.resolve() == self.active_translation_source_path.resolve():
                    self.live_translation_markdown = ""
            self.active_translation_source_path = None
            QMessageBox.critical(self, "翻译失败", message)
            self.append_log(f"翻译失败: {message}")
        if getattr(self, "_continue_batch_translate", False):
            self._continue_batch_translate = False
            self.dispatch_batch_translation()
        else:
            self._task_stop_requested = False
            self.finish_task_ui()
        self.update_translate_button_visibility()

    def finish_layout_translation(self, success: bool, message: str, html_path: str):
        if self._task_stop_requested:
            self.append_log("排版翻译已停止。")
            self.clear_layout_retranslation_notice(self.translation_web_view)
            self.reset_translation_task_state()
            self._task_stop_requested = False
            self.finish_task_ui()
            return
        self.translate_button.setEnabled(True)
        self.batch_translate_button.setEnabled(True)
        if success:
            path = Path(html_path)
            active_source = self.active_translation_source_path
            is_current_source = bool(
                active_source
                and self.current_source_path
                and self.current_source_path.exists()
                and active_source.resolve() == self.current_source_path.resolve()
            )
            if is_current_source:
                self.clear_layout_retranslation_notice(self.translation_web_view)
                self.current_layout_translation_path = path if path.exists() else None
                self.live_layout_translation_markdown = ""
            self.append_log(message)
            if is_current_source and self.current_layout_translation_path and self.settings.layout_reading_mode:
                self.show_html_in_view(
                    self.current_layout_translation_path,
                    self.translation_web_view,
                    self.translation_fallback_viewer,
                )
            if active_source:
                source_key = str(active_source.resolve())
                self.live_layout_translation_by_source.pop(source_key, None)
                for reader in list(self.reader_windows):
                    if not reader.source_path or not reader.layout_reading_mode:
                        continue
                    if reader.source_path.resolve() != active_source.resolve():
                        continue
                    reader.translation_path = path if path.exists() else None
                    reader.live_translation_markdown = ""
                    reader.refresh_content()
            self.refresh_docs()
        else:
            if self.active_translation_source_path:
                source_key = str(self.active_translation_source_path.resolve())
                self.live_layout_translation_by_source.pop(source_key, None)
                if self.current_source_path and self.current_source_path.resolve() == self.active_translation_source_path.resolve():
                    self.clear_layout_retranslation_notice(self.translation_web_view)
                    self.live_layout_translation_markdown = ""
                    if self.current_layout_translation_path and self.current_layout_translation_path.exists():
                        self.append_log("重新翻译未完成，已自动保留原译文和导出内容。")
            cache_protected = str(message).startswith("DEEPSEEK_FAST_CACHE_PROTECTION:")
            display_message = (
                str(message).split(":", 1)[1]
                if cache_protected
                else str(message)
            )
            if not getattr(self, "_continue_batch_layout_translate", False):
                if cache_protected:
                    QMessageBox.warning(self, "DeepSeek 高速翻译已停止", display_message)
                else:
                    QMessageBox.critical(self, "排版翻译失败", display_message)
            self.append_log(f"排版翻译失败: {display_message}")
        self.active_translation_source_path = None
        if getattr(self, "_continue_batch_layout_translate", False):
            self._continue_batch_layout_translate = False
            self._batch_layout_translate_done += 1
            if not success:
                self._batch_layout_translate_failed += 1
                self.append_log("已跳过异常文档，继续处理下一篇…")
            self.dispatch_batch_layout_translation()
            return
        self._task_stop_requested = False
        self.finish_task_ui()
        self.update_translate_button_visibility()

    def stop_current_task(self):
        stopped = False
        if self.is_thread_running(self.worker):
            self._task_stop_requested = True
            self._pending_parse_translation_config = None
            self.worker.requestInterruption()
            self.append_log("正在停止解析任务…")
            stopped = True

        if self.running_parse_workers or self._batch_parse_waiting_for_wave:
            self._task_stop_requested = True
            # 停止的是整条复合任务，不能在解析工作线程退出后继续自动翻译。
            self._batch_parse_then_translate = False
            self._batch_parse_translation_accepting_sources = False
            self.batch_parse_queue = []
            if self._batch_parse_timer.isActive():
                self._batch_parse_timer.stop()
            self._batch_parse_waiting_for_wave = False
            for worker in list(self.running_parse_workers.values()):
                worker.requestInterruption()
            self.append_log("正在停止批量解析任务…")
            if not self.running_parse_workers:
                # 等待下一批期间没有线程会再触发 finished_signal，必须立即统一收尾。
                self.finish_batch_parse()
            stopped = True

        if self.is_thread_running(self.translate_worker):
            self._task_stop_requested = True
            self.translate_worker.requestInterruption()
            self.clear_layout_retranslation_notice(self.translation_web_view)
            self.append_log("正在停止翻译任务…")
            # 不在这里提前清空状态；finish_translation/finish_layout_translation
            # 会在工作线程真正退出后执行唯一一次收尾。
            stopped = True

        if self.running_translate_workers:
            self._task_stop_requested = True
            self.batch_translate_queue = []
            self._batch_layout_translate_queue = []
            for worker in list(self.running_translate_workers.values()):
                worker.requestInterruption()
            self.append_log("正在停止批量翻译任务…")
            stopped = True

        if not stopped:
            self.append_log("当前没有正在运行的解析或翻译任务。")

    def append_log(self, message: str):
        self.log_messages.append(message)
        self.log_output.append(message)
        self.log_output.verticalScrollBar().setValue(self.log_output.verticalScrollBar().maximum())
        if self.log_dialog_output:
            self.log_dialog_output.append(message)
            self.log_dialog_output.verticalScrollBar().setValue(self.log_dialog_output.verticalScrollBar().maximum())

    def append_reasoning_log(self, text: str):
        if not text or not str(text).strip():
            return
        self._reasoning_log_parts.append(text)
        self._reasoning_pending_parts.append(text)
        if self.reasoning_log_output and not self._reasoning_flush_timer.isActive():
            self._reasoning_flush_timer.start(120)

    def flush_reasoning_log_output(self):
        if not self.reasoning_log_output or not self._reasoning_pending_parts:
            return
        self.reasoning_log_output.moveCursor(QTextCursor.MoveOperation.End)
        self.reasoning_log_output.insertPlainText("".join(self._reasoning_pending_parts))
        self._reasoning_pending_parts.clear()
        self.reasoning_log_output.verticalScrollBar().setValue(self.reasoning_log_output.verticalScrollBar().maximum())

    def clear_logs(self):
        self.log_messages = []
        self._reasoning_log_parts.clear()
        self._reasoning_pending_parts.clear()
        if self._reasoning_flush_timer.isActive():
            self._reasoning_flush_timer.stop()
        self.log_output.clear()
        if self.log_dialog_output:
            self.log_dialog_output.clear()
        if self.reasoning_log_output:
            self.reasoning_log_output.clear()

    def start(self, skip_duplicate_confirmation: bool = False):
        if self.reject_new_processing_task("解析"):
            return
        pdf_path_text = self.pdf_input.text().strip()
        if not pdf_path_text or not Path(pdf_path_text).is_file():
            QMessageBox.critical(self, "错误", "请选择有效文件。")
            return

        pdf_path = Path(pdf_path_text)
        if not is_supported_input_file(pdf_path):
            QMessageBox.critical(self, "错误", f"文档解析器暂不支持此文件类型: {pdf_path.suffix}")
            return
        if input_requires_mineru(pdf_path):
            try:
                load_mineru_token()
            except MinerUError:
                self.configure_mineru_api_key()
                try:
                    load_mineru_token()
                except MinerUError:
                    QMessageBox.information(self, "需要 MinerU 访问令牌", "请先设置 MinerU 访问令牌后再开始解析。")
                    return
        if not skip_duplicate_confirmation and not self.confirm_duplicate_parse(pdf_path):
            return
        self._pending_parse_translation_config = None
        self._pending_parse_layout_mode = bool(self.settings.layout_reading_mode and not is_epub_input_file(pdf_path))
        if is_epub_input_file(pdf_path) and self.settings.layout_reading_mode:
            self.append_log("EPUB 为流式重排电子书，自动采用流式阅读与翻译。")
        saved_config = self.load_saved_translation_config()
        dialog = TranslationOptionsDialog(self, provider_id=saved_config.provider_id if saved_config else self.settings.ai_provider, allow_parse_only=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        parse_only = bool(dialog.parse_only_check and dialog.parse_only_check.isChecked())
        if not parse_only:
            ai_config = dialog.selected_ai_config
            if not ai_config:
                return
            if dialog.reference_paths and not is_free_machine_translation_config(ai_config) and is_lightweight_ai_model(ai_config.model):
                QMessageBox.warning(
                    self,
                    "建议使用更强模型",
                    "你已选择参考文件。该功能会把完整参考语料直接放入翻译上下文，需要模型同时阅读原文、参考语料和翻译约束，"
                    "建议使用支持长上下文的模型；mini、flash、lite 等轻量模型可能丢失细节、忽略参考语感、超出上下文或过度模仿参考风格。",
                )
            target_language = dialog.target_combo.currentText().strip() or "简体中文"
            source_language = str(dialog.source_combo.currentData() or dialog.source_combo.currentText().strip() or "英文")
            local_parallelism = machine_translate.normalize_parallelism(dialog.local_parallel_spin.value())
            mode = dialog.mode_combo.currentData() or "full_context"
            self.save_translation_preferences(target_language, mode, dialog.reference_paths, source_language, local_parallelism)
            self._pending_parse_translation_config = TranslationJobConfig(
                ai_config=ai_config,
                source_language=source_language,
                target_language=target_language,
                mode=mode,
                reference_paths=dialog.reference_paths,
                local_machine_parallelism=local_parallelism,
            )
        self.clear_logs()
        self.begin_task_ui()
        self.start_parse_file(pdf_path)

    def start_parse_file(self, pdf_path: Path, continue_batch: bool = False):
        try:
            output_dir = output_dir_for_pdf(pdf_path, reserve=True)
        except Exception as exc:
            QMessageBox.critical(self, "无法创建输出目录", str(exc))
            self.append_log(f"无法创建解析输出目录: {exc}")
            self.finish_task_ui()
            return
        self.output_label.setText(f"输出目录: {output_dir}")
        self.run_button.setEnabled(False)
        self.file_button.setEnabled(False)
        self.batch_run_button.setEnabled(False)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self._continue_batch_parse = continue_batch
        app_config.remember_recent_file(self.settings, str(pdf_path))
        self.settings.mineru_model = self.mineru_model_combo.currentText().strip() or DEFAULT_MODEL_VERSION
        app_config.save_settings(self.settings)
        options = ParseOptions(
            model_version=self.mineru_model_combo.currentText().strip() or DEFAULT_MODEL_VERSION,
            enable_table=self.table_check.isChecked(),
            enable_formula=self.formula_check.isChecked(),
            is_ocr=self.ocr_check.isChecked(),
        )
        self.worker = create_document_parse_worker(pdf_path, output_dir, options)
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.progress.setValue)
        self.worker.finished_signal.connect(self.finish)
        self.worker.start()

    def finish(self, success: bool, message: str, markdown_path: str):
        self.run_button.setEnabled(True)
        self.file_button.setEnabled(True)
        self.batch_run_button.setEnabled(True)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        pending_translation_config = self._pending_parse_translation_config
        pending_layout_mode = bool(self._pending_parse_layout_mode)
        self._pending_parse_translation_config = None
        if self._task_stop_requested:
            self._task_stop_requested = False
            self._pending_parse_layout_mode = False
            self.append_log("解析任务已停止。")
            self.finish_task_ui()
            return
        if success:
            self.append_log(message)
            self.refresh_docs()
            if markdown_path:
                parsed_path = Path(markdown_path)
                self.load_markdown(parsed_path)
                if pending_translation_config:
                    self.append_log("文档解析完成，正在接续启动翻译…")
                    self.active_translation_source_path = parsed_path
                    if pending_layout_mode:
                        self.start_layout_translation_job(
                            parsed_path,
                            pending_translation_config.ai_config,
                            pending_translation_config.target_language,
                            source_language=pending_translation_config.source_language,
                            local_machine_parallelism=pending_translation_config.local_machine_parallelism,
                            reference_paths=pending_translation_config.reference_paths,
                            translation_mode=pending_translation_config.mode,
                            allow_parse_handoff=True,
                        )
                    else:
                        self.start_translation_job(
                            parsed_path,
                            pending_translation_config,
                            allow_parse_handoff=True,
                        )
                    return
        else:
            QMessageBox.critical(self, "失败", message)
            self.append_log(f"失败: {message}")
        if getattr(self, "_continue_batch_parse", False):
            self._continue_batch_parse = False
            self.dispatch_next_parse_wave()
        else:
            self.finish_task_ui()


def main():
    os.environ["LITMTRANS_TRANSLATION_REQUEST_AUDIT"] = "1" if TRANSLATION_REQUEST_AUDIT_ENABLED else "0"
    os.environ["QT_LOGGING_RULES"] = "qt.pdf.links.*=false"
    app = QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    # 仅消息提示框静音；文件选择器仍保持 Windows 原生体验。
    configure_silent_application()

    # 强制加载随程序打包的思源宋体。
    # 若打包时遗漏字体，启动阶段直接明确报错，不能静默退回用户电脑上的系统字体。
    try:
        register_bundled_reader_font()
    except Exception as exc:
        QMessageBox.critical(
            None,
            "内置字体加载失败",
            "无法加载内置思源宋体：\n"
            "resources/fonts/SourceHanSerifCN-Regular.ttf\n\n"
            "请检查 PyInstaller 打包配置是否包含 resources/fonts 目录。\n\n"
            f"错误信息：{exc}",
        )
        sys.exit(1)

    # Windows 下显式设置 AppUserModelID，避免任务栏仍显示默认 Python 图标。
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except Exception as exc:
            print(f"警告: 设置 AppUserModelID 时出错: {exc}")

    # 设置应用级图标；即使失败也不影响程序继续运行。
    try:
        icon_path = os.path.join(get_base_path(), "resources", "icon.ico")
        if os.path.exists(icon_path):
            app.setWindowIcon(QIcon(icon_path))
        else:
            print(f"警告: 应用程序图标文件未找到: {icon_path}")
    except Exception as exc:
        print(f"警告: 设置应用程序图标时出错: {exc}")

    app_font = QFont("Times New Roman")
    if hasattr(app_font, "setFamilies"):
        app_font.setFamilies(
            [
                "Times New Roman",
                "SimSun",
                "Noto Serif CJK SC",
                "PMingLiU",
                "MingLiU",
                "Yu Mincho",
                "MS Mincho",
                "Batang",
            ]
        )
    app_font.setPointSize(10)
    app.setFont(app_font)

    # Apply the shared application style to native dialogs and standalone controls.
    app.setStyle("Fusion")
    apply_monochrome_app_style(app)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
