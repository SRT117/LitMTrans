import base64
import os
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pypdfium2 as pdfium
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QUrl
from PySide6.QtGui import QPageLayout
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import OT_ui
import AI_chat
import LS_pipeline
from PB_layout import (
    LAYOUT_FIT_CACHE_VERSION,
    equation_number_right_for_bbox,
    group_flow_streams,
    is_layout_metadata_text,
    local_column_right_for_bbox,
    layout_docx_apply_equation_clearance,
    layout_docx_dom_text_anchor,
    layout_docx_formula_point_scale,
    layout_docx_items_from_html,
    layout_docx_output_font_size,
    layout_docx_safe_text_height,
    layout_docx_image_anchor,
    mark_equation_dense_body_items,
    merge_vertical_body_items,
    promote_text_items_to_body,
    render_layout_editable_html_docx,
    render_original_pdf_preview_html,
    render_layout_generic_block,
    render_layout_text_block,
)


MINIMAL_TEXT_PDF = base64.b64decode(
    "JVBERi0xLjQKMSAwIG9iago8PCAvVHlwZSAvQ2F0YWxvZyAvUGFnZXMgMiAwIFIgPj4KZW5kb2JqCjIgMCBvYmoKPDwgL1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgPj4KZW5kb2JqCjMgMCBvYmoKPDwgL1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvTWVkaWFCb3ggWzAgMCA2MDAgODAwXSAvUmVzb3VyY2VzIDw8IC9Gb250IDw8IC9GMSA0IDAgUiA+PiA+PiAvQ29udGVudHMgNSAwIFIgPj4KZW5kb2JqCjQgMCBvYmoKPDwgL1R5cGUgL0ZvbnQgL1N1YnR5cGUgL1R5cGUxIC9CYXNlRm9udCAvSGVsdmV0aWNhID4+CmVuZG9iago1IDAgb2JqCjw8IC9MZW5ndGggNDUgPj4Kc3RyZWFtCkJUCi9GMSAyNCBUZgoxMDAgNzAwIFRkCihWZWN0b3IgcHJldmlldykgVGoKRVQKZW5kc3RyZWFtCmVuZG9iagp4cmVmCjAgNgowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMDkgMDAwMDAgbiAKMDAwMDAwMDA1OCAwMDAwMCBuIAowMDAwMDAxMTUgMDAwMDAgbiAKMDAwMDAwMjc1IDAwMDAwIG4gCjAwMDAwMDM0NSAwMDAwMCBuIAp0cmFpbGVyCjw8IC9TaXplIDYgL1Jvb3QgMSAwIFIgPj4Kc3RhcnR4cmVmCjQzOQolJUVPRgo="
)


class FormulaIntegrityTests(unittest.TestCase):
    def test_bundled_source_han_serif_is_loaded_and_preferred_for_reading(self):
        project_root = Path(__file__).resolve().parents[1]
        font_path = project_root / "resources" / "fonts" / "SourceHanSerifCN-Regular.ttf"
        license_path = project_root / "resources" / "fonts" / "LICENSE-SourceHanSerif.txt"
        self.assertTrue(font_path.is_file())
        self.assertGreater(font_path.stat().st_size, 10_000_000)
        self.assertTrue(license_path.is_file())
        self.assertIn('"Source Han Serif CN", "Times New Roman"', OT_ui.READER_SERIF_FONT_STACK)
        layout_source = Path(OT_ui.__file__).with_name("PB_layout.py").read_text(encoding="utf-8")
        self.assertIn("SERIF_READING_FONT_STACK = BUNDLED_READER_FONT_STACK", layout_source)

    def test_pdf_preview_renders_portable_png_page_assets(self):

        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "paper.pdf"
            pdf_path.write_bytes(MINIMAL_TEXT_PDF)

            html_path = render_original_pdf_preview_html(pdf_path)
            self.assertIsNotNone(html_path)
            rendered = html_path.read_text(encoding="utf-8")
            self.assertIn(".png", rendered)
            self.assertIn('loading="lazy"', rendered)
            self.assertIn('decoding="async"', rendered)
            asset = next(pdf_path.parent.glob("original_pdf_preview_assets.v7.paper/page_0001.png"))
            self.assertGreater(asset.stat().st_size, 0)

    def test_image_only_pdf_preview_falls_back_to_png_page_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "scan.pdf"
            pdf_path.write_bytes(MINIMAL_TEXT_PDF)

            html_path = render_original_pdf_preview_html(pdf_path)
            self.assertIsNotNone(html_path)
            self.assertIn(".png", html_path.read_text(encoding="utf-8"))
            asset = next(pdf_path.parent.glob("original_pdf_preview_assets.v7.scan/page_0001.png"))
            self.assertGreater(asset.stat().st_size, 0)

    def test_flow_translation_requires_original_formula_and_delimiters(self):
        source = "At \\(R / R _ { 0 } = 6\\), the afterflow dominates."
        preserved = "在 \\(R / R _ { 0 } = 6\\) 时，尾流占主导。"
        dropped_delimiters = "在 R / R _ { 0 } = 6 时，尾流占主导。"
        changed_formula = "在 \\(R / R _ { 0 } = 10\\) 时，尾流占主导。"

        self.assertEqual(LS_pipeline.math_expression_integrity_issue(source, preserved), "")
        self.assertTrue(LS_pipeline.math_expression_integrity_issue(source, dropped_delimiters))
        self.assertTrue(LS_pipeline.math_expression_integrity_issue(source, changed_formula))
        self.assertTrue(LS_pipeline.math_expression_retry_issue(source, dropped_delimiters))
        self.assertTrue(LS_pipeline.math_expression_retry_issue(source, changed_formula))

    def test_additional_or_safely_split_formulas_warn_without_paid_retry(self):
        source = "Variables \\(n_w, n_g, \\rho_w\\) and \\(p\\) are defined."
        safely_split = "变量 \\(n_w\\)、\\(n_g\\)、\\(\\rho_w\\) 和 \\(p\\) 已定义。"
        with_extra = "变量 \\(n_w, n_g, \\rho_w\\) 和 \\(p\\) 已定义，另记 \\(Z\\)。"

        self.assertTrue(LS_pipeline.math_expression_integrity_issue(source, safely_split))
        self.assertEqual(LS_pipeline.math_expression_retry_issue(source, safely_split), "")
        self.assertTrue(LS_pipeline.math_expression_integrity_issue(source, with_extra))
        self.assertEqual(LS_pipeline.math_expression_retry_issue(source, with_extra), "")

    def test_formula_retry_normalizes_ocr_and_presentation_only_differences(self):
        self.assertEqual(
            LS_pipeline.math_expression_retry_issue(r"速度为 \(u_{sw}\)。", r"速度为 \(u_{{sw}}\)。"),
            "",
        )
        self.assertEqual(
            LS_pipeline.math_expression_retry_issue(r"元素 \(\mathrm{H}\)。", r"元素 \(H\)。"),
            "",
        )
        self.assertEqual(
            LS_pipeline.math_expression_retry_issue(
                r"参数 \(C_p = 1; (ii)\)。",
                r"参数 \(C_p = 1\) (ii)。",
            ),
            "",
        )
        self.assertIn(
            "标准化源",
            LS_pipeline.math_expression_retry_issue(r"元素 \(\mathrm{H}\)。", r"元素 \(Z\)。"),
        )

    def test_retained_layout_preview_gets_loading_notice_upgrade_before_display(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview_layout_translation.html"
            path.write_text("<html><head></head><body></body></html>", encoding="utf-8")
            OT_ui.upgrade_layout_loading_notice_html(path)
            upgraded = path.read_text(encoding="utf-8")
            self.assertIn(OT_ui.LAYOUT_LOADING_NOTICE_COMPAT_MARKER, upgraded)
            self.assertIn("background: transparent", upgraded)

    def test_code_block_without_image_path_renders_text_instead_of_workspace_url(self):
        block = {
            "type": "code",
            "bbox": [50, 100, 300, 180],
            "guess_lang": "txt",
            "blocks": [
                {
                    "type": "code_body",
                    "bbox": [50, 100, 300, 180],
                    "lines": [
                        {
                            "spans": [
                                {
                                    "type": "text",
                                    "content": "manual_refresh_default\n0",
                                }
                            ]
                        }
                    ],
                }
            ],
        }

        rendered = render_layout_generic_block(block, Path.cwd(), 612.0, 792.0)

        self.assertIn('class="layout-code"', rendered)
        self.assertIn("manual_refresh_default", rendered)
        self.assertIn('data-line-ratio="1.180"', rendered)
        self.assertIn("line-height:1.180;", rendered)
        self.assertNotIn("<img", rendered)

    def test_translated_code_blocks_have_internal_overflow_fit_and_scroll_fallback(self):
        source = Path(OT_ui.__file__).with_name("PB_layout.py").read_text(encoding="utf-8")
        self.assertIn("function clampTranslatedCodeOverflow()", source)
        self.assertIn("code.scrollHeight > code.clientHeight + 1", source)
        self.assertIn("code.style.overflow = 'auto';", source)
        self.assertIn("clampTranslatedCodeOverflow();", source)
        self.assertIn("const minFont = 7.0;", source)
        self.assertIn("const minLineRatio = 1.10;", source)
        self.assertIn("line-height: inherit;", source)

    def test_translation_preview_runtime_upgrade_invalidates_old_code_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "preview_layout_current.html"
            source.write_text(
                "<!-- layout-preview version=58 fingerprint=abc -->",
                encoding="utf-8",
            )
            translated = folder / "preview_layout_translated.html"
            source_stamp = OT_ui.layout_preview_source_stamp(source)
            translated.write_text(
                f"<!-- layout-translation-runtime-v24 -->\n"
                f"<!-- layout-translation-source-stamp={source_stamp} -->",
                encoding="utf-8",
            )
            self.assertFalse(OT_ui.layout_translation_preview_is_current(translated, source))
            translated.write_text(
                f"<!-- layout-translation-runtime-v29-gallop-tail -->\n"
                f"<!-- layout-translation-source-stamp={source_stamp} -->",
                encoding="utf-8",
            )
            self.assertTrue(OT_ui.layout_translation_preview_is_current(translated, source))

    def test_warm_layout_reveals_before_deferred_mathjax_finishes(self):
        source = Path(OT_ui.__file__).with_name("PB_layout.py").read_text(encoding="utf-8")
        cache_branch = source[
            source.index("if (restoreFitCache()) {", source.index("function initializeLayout()"))
            : source.index("document.body.classList.remove('layout-fit-cache-hit')", source.index("function initializeLayout()"))
        ]
        self.assertIn("revealFinalLayout();", cache_branch)
        self.assertIn("window.MathJax.startup.promise", cache_branch)
        self.assertNotIn("window.MathJax.typesetPromise", cache_branch)
        self.assertIn(
            "document.body && document.body.classList.contains('layout-fit-cache-hit')",
            source,
        )

    def test_mathjax_completion_refits_initial_equation_geometry(self):
        source = Path(LS_pipeline.__file__).read_text(encoding="utf-8")
        start = source.index("def ensure_web_view_mathjax_typeset")
        end = source.index("\ndef ", start + 5)
        helper = source[start:end]
        self.assertIn("window.__mineruFitLayoutEquations", helper)
        self.assertIn("requestAnimationFrame(() => requestAnimationFrame(refitLayoutEquations))", helper)
        self.assertIn("window.setTimeout(refitLayoutEquations, 180)", helper)

    def test_translated_layout_passes_column_geometry_to_equations(self):
        source = (Path(__file__).resolve().parents[1] / "layout_translate_preview.py").read_text(encoding="utf-8")
        start = source.index('elif block_type == "interline_equation":')
        end = source.index("            else:", start)
        equation_branch = source[start:end]
        self.assertIn("mineru.render_layout_equation_block(", equation_branch)
        self.assertIn("column_rights,", equation_branch)

    def test_runtime_upgrade_never_migrates_page_local_fit_styles(self):
        source = Path(OT_ui.__file__).read_text(encoding="utf-8")
        worker_start = source.index("class LayoutPreviewRefreshWorker")
        worker_end = source.index("\nclass ", worker_start + 10)
        worker_source = source[worker_start:worker_end]
        self.assertNotIn("migrate_layout_fit_cache_by_page", source)
        self.assertNotIn("previous_state", worker_source)
        self.assertIn("reset_fit_cache=False", worker_source)
        self.assertIn("fresh document-wide", worker_source)


class EmbeddedAiModelRefreshTests(unittest.TestCase):
    def test_pure_reader_ai_actions_open_the_reader_rail(self):
        """Context-menu AI actions must stay in the pure reader that owns them."""
        class FakeButton:
            def __init__(self):
                self.checked = False

            def setChecked(self, value):
                self.checked = bool(value)

        class FakePanel:
            def __init__(self):
                self.visible = False

            def isVisible(self):
                return self.visible

        class FakeReader:
            def __init__(self):
                self.reader_ai_button = FakeButton()
                self.reader_ai_panel = FakePanel()
                self.reader_ai_chat_window = object()
                self.ensure_calls = 0

            def toggle_reader_ai_sidebar(self, visible):
                self.reader_ai_panel.visible = bool(visible)

            def ensure_reader_ai_chat(self):
                self.ensure_calls += 1
                return True

        reader = FakeReader()
        with patch.object(OT_ui, "ReaderWindow", FakeReader):
            chat = OT_ui.MainWindow.open_ai_for_context(SimpleNamespace(), reader)

        self.assertIs(chat, reader.reader_ai_chat_window)
        self.assertTrue(reader.reader_ai_button.checked)
        self.assertTrue(reader.reader_ai_panel.visible)
        self.assertEqual(reader.ensure_calls, 1)

    def test_entering_ai_refreshes_models_each_time(self):
        class FakeChat:
            def __init__(self):
                self.fetch_calls = []

            def current_ai_key_available(self):
                return True

            def fetch_models(self, *, silent=False):
                self.fetch_calls.append(silent)
                return True

        chat = FakeChat()
        window = SimpleNamespace(embedded_chat_window=chat)

        OT_ui.MainWindow.refresh_models_when_ai_first_opened(window)
        OT_ui.MainWindow.refresh_models_when_ai_first_opened(window)

        self.assertEqual(chat.fetch_calls, [True, True])

    def test_loading_document_does_not_resync_and_clear_models(self):
        class FakeChat:
            def __init__(self):
                self.loaded = []

            def load_document_conversation(self, *args):
                self.loaded.append(args)
                return True

        path = Path("C:/tmp/paper.md")
        chat = FakeChat()
        window = SimpleNamespace(
            current_source_path=path,
            _embedded_chat_doc_key="",
            ensure_embedded_chat=lambda: chat,
            document_chat_session_id=lambda _path: "doc-chat-test",
        )

        OT_ui.MainWindow.load_embedded_ai_for_current_document(window)

        self.assertEqual(len(chat.loaded), 1)

    def test_reentering_ai_does_not_queue_duplicate_refresh(self):
        class Input:
            def text(self):
                return "configured"

        class Worker:
            def isRunning(self):
                return True

        chat = SimpleNamespace(
            key_input=Input(),
            url_input=Input(),
            model_worker=Worker(),
            _model_refresh_provider_id="oneapi",
            _pending_model_refresh_provider_id="",
            get_current_provider=lambda: "oneapi",
            save_current_api_settings=lambda: None,
        )

        started = AI_chat.ChatWindow.fetch_models(chat, silent=True)

        self.assertFalse(started)
        self.assertEqual(chat._pending_model_refresh_provider_id, "")

    def test_entering_ai_during_translation_defers_webengine_initialization(self):
        calls = []

        class Stack:
            def currentIndex(self):
                return 1

        window = SimpleNamespace(
            left_stack=Stack(),
            _ai_open_initialization_scheduled=False,
            has_active_translation_task=lambda: True,
            load_embedded_ai_for_current_document=lambda: calls.append("load"),
            refresh_models_when_ai_first_opened=lambda: calls.append("refresh"),
        )

        with patch.object(OT_ui.QTimer, "singleShot", side_effect=lambda _delay, callback: calls.append(callback)):
            OT_ui.MainWindow.on_left_nav_changed(window, 1)

        self.assertEqual(len(calls), 1)
        self.assertTrue(callable(calls[0]))
        self.assertTrue(window._ai_open_initialization_scheduled)
        calls[0]()
        self.assertEqual(calls[-2:], ["load", "refresh"])
        self.assertFalse(window._ai_open_initialization_scheduled)


class TranslationRequestAuditTests(unittest.TestCase):
    def test_audit_uses_litmtrans_environment_variable(self):
        with patch.dict(os.environ, {"LITMTRANS_TRANSLATION_REQUEST_AUDIT": "true"}, clear=True):
            self.assertTrue(LS_pipeline.translation_request_audit_enabled())

    def test_enabled_audit_writes_complete_messages_without_api_key(self):
        config = SimpleNamespace(
            provider_id="test-provider",
            model="test-model",
            base_url="https://example.invalid/v1",
            request_body_mode="codex",
            api_key="must-not-appear",
        )
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "full source text"},
        ]
        with tempfile.TemporaryDirectory() as raw_dir:
            with patch("LS_pipeline.translation_request_audit_enabled", return_value=True):
                path = LS_pipeline.save_translation_request_audit(
                    Path(raw_dir), "流式-全文连续翻译-第1轮", config, messages, timeout=300
                )
            self.assertIsNotNone(path)
            self.assertTrue(path.exists())
            self.assertTrue(path.name.startswith("流式-全文连续翻译-第1轮-"))
            saved = path.read_text(encoding="utf-8")
            self.assertIn("system prompt", saved)
            self.assertIn("full source text", saved)
            self.assertIn("请求缓存键:", saved)
            self.assertNotIn("must-not-appear", saved)


class _HistoryItem:
    def __init__(self, url):
        self._url = url

    def url(self):
        return self._url


class _History:
    def __init__(self, back_items=None):
        self._back_items = list(back_items or [])
        self.selected = None
        self.clear_count = 0

    def backItems(self, _count):
        return self._back_items

    def forwardItems(self, _count):
        return []

    def goToItem(self, item):
        self.selected = item

    def clear(self):
        self.clear_count += 1


class _WebView:
    def __init__(self, current_url=None, history=None):
        self._url = current_url or QUrl()
        self._history = history or _History()
        self.reload_count = 0
        self.stop_count = 0
        self.set_urls = []

    def url(self):
        return self._url

    def reload(self):
        self.reload_count += 1

    def stop(self):
        self.stop_count += 1

    def setUrl(self, url):
        self._url = url
        self.set_urls.append(url)

    def history(self):
        return self._history


class _VisibleWidget:
    def __init__(self):
        self.visible = True
        self.clear_count = 0

    def hide(self):
        self.visible = False

    def show(self):
        self.visible = True

    def clear(self):
        self.clear_count += 1


class _MainPreviewHarness:
    is_suspended_main_preview_target = OT_ui.MainWindow.is_suspended_main_preview_target
    suspend_main_preview_for_reader = OT_ui.MainWindow.suspend_main_preview_for_reader
    clear_suspended_preview_history = OT_ui.MainWindow.clear_suspended_preview_history

    def __init__(self):
        self._main_preview_suspended = False
        self.reader_windows = []
        self.source_web_view = _WebView(QUrl("file:///source.html"))
        self.translation_web_view = _WebView(QUrl("file:///translation.html"))
        self.source_fallback_viewer = _VisibleWidget()
        self.translation_fallback_viewer = _VisibleWidget()
        self.preview_splitter = _VisibleWidget()
        self.preview_suspended_notice = _VisibleWidget()
        self.preview_suspended_notice.visible = False
        self._translation_live_page_ready = True
        self._translation_live_pending_markdown = "pending"
        self.capture_count = 0

    def capture_current_scroll_state(self):
        self.capture_count += 1

    def reset_sync_scroll_runtime(self):
        pass

    def clear_all_layout_transition_overlays(self):
        pass

    def clear_all_layout_retranslation_notices(self):
        pass


class PreviewNavigationTests(unittest.TestCase):
    def test_pure_reader_soft_suspends_both_main_preview_pages(self):
        window = _MainPreviewHarness()
        with patch.object(OT_ui, "release_source_pdf"), patch.object(OT_ui.QTimer, "singleShot"):
            window.suspend_main_preview_for_reader()

        self.assertTrue(window._main_preview_suspended)
        self.assertEqual(window.capture_count, 1)
        self.assertEqual(window.source_web_view.url().toString(), "about:blank")
        self.assertEqual(window.translation_web_view.url().toString(), "about:blank")
        self.assertEqual(window.source_web_view.history().clear_count, 1)
        self.assertEqual(window.translation_web_view.history().clear_count, 1)
        self.assertFalse(window.preview_splitter.visible)
        self.assertTrue(window.preview_suspended_notice.visible)
        self.assertEqual(window.source_fallback_viewer.clear_count, 1)
        self.assertEqual(window.translation_fallback_viewer.clear_count, 1)

    def test_last_reader_close_resumes_main_preview_once(self):
        first = object()
        second = object()
        resume_calls = []
        window = SimpleNamespace(
            reader_windows=[first, second],
            resume_main_preview_after_readers=lambda: resume_calls.append(True),
        )

        OT_ui.MainWindow.remove_reader_window(window, first)
        self.assertEqual(resume_calls, [])
        OT_ui.MainWindow.remove_reader_window(window, second)
        self.assertEqual(resume_calls, [True])
        OT_ui.MainWindow.remove_reader_window(window, second)
        self.assertEqual(resume_calls, [True, True])

    def test_layout_state_cache_round_trip_uses_current_artifact_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            html_path = Path(directory) / "preview.html"
            cache_path = Path(directory) / "preview.final-layout-state.json"
            html_path.write_text("<main></main>", encoding="utf-8")

            OT_ui.write_layout_cache_payload(cache_path, html_path, "state", {"pages": [1]})

            self.assertEqual(
                OT_ui.read_current_layout_cache_payload(cache_path, html_path, "state"),
                {"pages": [1]},
            )
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    def test_blank_suspended_page_never_starts_layout_cache_polling(self):
        view = _WebView(QUrl("about:blank"))
        window = SimpleNamespace(
            settings=SimpleNamespace(layout_reading_mode=True),
            source_web_view=view,
            translation_web_view=None,
        )

        with patch.object(OT_ui.QTimer, "singleShot") as single_shot:
            OT_ui.MainWindow.schedule_layout_word_state_cache_after_load(window, "source", True)

        single_shot.assert_not_called()

    @staticmethod
    def body_item(bbox, lines=2, text="body"):
        return {
            "kind": "text",
            "debug_role": "body_candidate",
            "column_key": "column-0",
            "side": "left",
            "bbox": bbox,
            "original_line_count": lines,
            "plain_text": text,
            "html": text,
        }

    def test_single_line_body_fragment_merges_with_direct_column_neighbor(self):
        items = [
            self.body_item([50, 100, 170, 112], lines=1, text="short sentence."),
            self.body_item([50, 112, 300, 155], lines=4, text="following paragraph."),
        ]
        merged = merge_vertical_body_items(items, [])
        body = [item for item in merged if item.get("debug_role") == "merged_body"]
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["bbox"], [50.0, 100.0, 300.0, 155.0])

    def test_single_line_body_fragment_does_not_cross_other_text(self):
        items = [
            self.body_item([50, 100, 170, 112], lines=1, text="short sentence."),
            {"kind": "text", "debug_role": "text", "bbox": [50, 112, 300, 124], "plain_text": "caption"},
            self.body_item([50, 124, 300, 165], lines=4, text="following paragraph."),
        ]
        merged = merge_vertical_body_items(items, [])
        body = [item for item in merged if item.get("debug_role") == "merged_body"]
        self.assertEqual(len(body), 2)

    def test_single_line_body_fragment_does_not_bridge_large_blank_gap(self):
        items = [
            self.body_item([50, 100, 170, 112], lines=1, text="short sentence."),
            self.body_item([50, 160, 300, 205], lines=4, text="distant paragraph."),
        ]
        merged = merge_vertical_body_items(items, [])
        body = [item for item in merged if item.get("debug_role") == "merged_body"]
        self.assertEqual(len(body), 2)

    def test_adjacent_body_fragments_merge_using_larger_column_width(self):
        items = [
            self.body_item([50, 100, 180, 124], lines=2, text="narrow continuation."),
            self.body_item([50, 124, 300, 164], lines=3, text="full-width continuation."),
        ]
        merged = merge_vertical_body_items(items, [])
        body = [item for item in merged if item.get("debug_role") == "merged_body"]
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["bbox"], [50.0, 100.0, 300.0, 164.0])

    def test_short_fragment_indent_is_preserved_inside_merged_body(self):
        first = self.body_item([50, 100, 300, 124], lines=2, text="preceding paragraph.")
        indented = self.body_item([50, 124, 300, 148], lines=2, text="indented short paragraph.")
        indented["debug_lines"] = [
            {"bbox": [68, 124, 220, 136]},
            {"bbox": [51, 136, 180, 148]},
        ]

        merged = merge_vertical_body_items([first, indented], [])
        body = [item for item in merged if item.get("debug_role") == "merged_body"]

        self.assertEqual(len(body), 1)
        self.assertEqual(len(body[0]["paragraphs"]), 2)
        self.assertEqual(body[0]["paragraphs"][0]["indent_px"], 0.0)
        self.assertEqual(body[0]["paragraphs"][1]["indent_px"], 18.0)

    def test_title_uses_its_geometry_estimate_as_fit_baseline(self):
        rendered = render_layout_text_block(
            {
                "type": "title",
                "bbox": [50, 100, 180, 112],
                "lines": [{"spans": [{"type": "text", "content": "II. METHODS"}]}],
            },
            612.0,
            809.0,
        )

        self.assertIn('data-block-kind="title"', rendered)
        self.assertIn('data-base-font="', rendered)
        self.assertNotIn('data-base-font="8.00"', rendered)

    def test_large_top_title_is_kept_out_of_secondary_title_group(self):
        rendered = render_layout_text_block(
            {
                "type": "title",
                "bbox": [50, 80, 500, 145],
                "lines": [{"spans": [{"type": "text", "content": "A long article title"}]}],
            },
            612.0,
            809.0,
        )

        self.assertIn('class="layout-block type-title main-title"', rendered)

    def test_secondary_titles_share_a_baseline_then_expand_only_underfilled_outliers(self):
        source = (Path(OT_ui.__file__).parent / "PB_layout.py").read_text(encoding="utf-8")
        self.assertIn("tuneEach('.layout-block.type-title.main-title'", source)
        self.assertIn("tuneGroup('.layout-block.type-title:not(.main-title)'", source)
        self.assertIn("includeGroupPeers: true", source)
        self.assertIn("expandUnderfilledTitles('.layout-block.type-title:not(.main-title)'", source)
        self.assertIn("function titleFrameFill(node)", source)
        self.assertIn("function clusterTitleFontSizes(selector, maxDifference = 1.0)", source)
        self.assertIn("clusterTitleFontSizes('.layout-block.type-title', 1.0)", source)

    def test_short_wrapped_titles_borrow_right_only_after_rendered_geometry_checks(self):
        source = (Path(OT_ui.__file__).parent / "PB_layout.py").read_text(encoding="utf-8")
        start = source.index("function keepShortTitlesOnOneLine(selector, options = {})")
        end = source.index("// Monotone collision-constrained growth", start)
        repair = source[start:end]

        self.assertIn("renderedTextLineCount(node) <= 1", repair)
        self.assertNotIn("dataset.originalLines", repair)
        self.assertIn("node.style.whiteSpace = 'nowrap';", repair)
        self.assertIn("borrowedWidth > maxBorrowPx", repair)
        self.assertIn("requiredWidth / ownWidth > maxWidthRatio", repair)
        self.assertIn("textCollisionDetails([node]", repair)
        self.assertIn("avoidPageOverflow: true", repair)
        self.assertIn(
            "keepShortTitlesOnOneLine('.layout-block.type-title:not(.main-title)'",
            source,
        )

    def test_license_line_is_not_body_metadata(self):
        self.assertTrue(is_layout_metadata_text("Published under an exclusive license by AIP Publishing."))

    def test_column_width_does_not_borrow_from_distant_body_box(self):
        rights = {
            "column-0": 382.0,
            "column-0_left": 49.0,
            "_body_boxes": [
                {"column_key": "column-0", "left": 49.0, "right": 382.0, "top": 307.0, "bottom": 330.0},
            ],
        }
        self.assertIsNone(local_column_right_for_bbox([49.0, 544.0, 299.0, 704.0], "column-0", 612.0, rights))

    def test_numbered_equation_uses_inferred_middle_column_anchor_without_changing_formula_bbox(self):
        rights = {
            "_body_boxes": [
                {"column_key": "column-0", "left": 42.0, "right": 190.0, "top": 300.0, "bottom": 360.0},
                {"column_key": "column-1", "left": 224.0, "right": 372.0, "top": 298.0, "bottom": 362.0},
                {"column_key": "column-2", "left": 406.0, "right": 570.0, "top": 301.0, "bottom": 361.0},
            ]
        }
        number_right = equation_number_right_for_bbox([252.0, 326.0, 344.0, 350.0], 612.0, rights)
        self.assertEqual(number_right, 372.0)

    def test_numbered_equation_uses_local_single_column_text_when_not_body_fitted(self):
        rights = {
            "_body_boxes": [],
            "_text_boxes": [
                {"column_key": "full", "left": 36.0, "right": 560.0, "top": 300.0, "bottom": 420.0},
            ],
        }
        number_right = equation_number_right_for_bbox([78.0, 350.0, 212.0, 371.0], 612.0, rights)
        self.assertEqual(number_right, 560.0)

    def test_cross_column_numbered_equation_uses_outer_edge_of_span(self):
        rights = {
            "_body_boxes": [
                {"column_key": "column-0", "left": 42.0, "right": 285.0, "top": 300.0, "bottom": 360.0},
                {"column_key": "column-1", "left": 325.0, "right": 570.0, "top": 300.0, "bottom": 360.0},
            ]
        }
        number_right = equation_number_right_for_bbox([78.0, 326.0, 520.0, 350.0], 612.0, rights)
        self.assertEqual(number_right, 570.0)

    def test_metadata_stream_does_not_merge_into_body_stream(self):
        metadata = {
            "kind": "text", "debug_role": "text", "side": "left",
            "bbox": [50.0, 100.0, 372.0, 112.0], "plain_text": "Published under an exclusive license.",
        }
        body = self.body_item([49.0, 152.0, 299.0, 220.0], lines=6, text="Body paragraph.")
        streams = group_flow_streams([metadata, body], [], 612.0)
        self.assertEqual(len(streams), 2)

    def test_previous_body_context_promotes_short_figure_page_prose(self):
        item = self.body_item([49.0, 323.0, 298.0, 344.0], lines=2, text="Short continuation after a figure.")
        item.pop("debug_role")
        promoted = promote_text_items_to_body(
            [item],
            612.0,
            809.0,
            {
                "has_previous_body": True,
                "neighbor_column_profiles": {"column-0": [(49.0 / 612.0, 298.0 / 612.0)]},
            },
        )
        self.assertEqual(promoted[0].get("debug_role"), "body_candidate")

    def test_equation_sandwiched_short_body_keeps_global_body_font(self):
        note = self.body_item([49.0, 501.0, 299.0, 545.0], lines=4, text="Short equation explanation.")
        note["debug_role"] = "merged_body"
        marked = mark_equation_dense_body_items(
            [note],
            [
                {"type": "interline_equation", "bbox": [83.0, 475.0, 265.0, 496.0]},
                {"type": "interline_equation", "bbox": [112.0, 550.0, 235.0, 572.0]},
            ],
            809.0,
        )
        self.assertEqual(marked[0].get("debug_role"), "merged_body")
        self.assertTrue(marked[0].get("equation_dense"))

    def test_same_unchanged_local_preview_is_not_reloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview.html"
            path.write_text("first", encoding="utf-8")
            url = QUrl.fromLocalFile(str(path))
            view = _WebView()

            self.assertTrue(OT_ui.set_or_reload_web_view_url(view, url))
            self.assertFalse(OT_ui.set_or_reload_web_view_url(view, url))
            self.assertEqual(view.reload_count, 0)

            path.write_text("second version", encoding="utf-8")
            self.assertTrue(OT_ui.set_or_reload_web_view_url(view, url))
            self.assertEqual(view.reload_count, 1)

    def test_layout_reader_transition_only_targets_generated_layout_artifacts(self):
        self.assertTrue(OT_ui.is_layout_preview_html_path("preview_layout_current.full.cleaned.html"))
        self.assertTrue(OT_ui.is_layout_preview_html_path("preview_layout_translated_current.full.cleaned.html"))
        self.assertFalse(OT_ui.is_layout_preview_html_path("preview.full.cleaned.html"))
        self.assertFalse(OT_ui.is_layout_preview_html_path("original_pdf_preview.html"))

    def test_complete_disk_fit_cache_skips_redundant_main_window_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview_layout_translated.html"
            path.write_text(
                f'<html><body data-layout-cache-version="{LAYOUT_FIT_CACHE_VERSION}"><script data-layout-fit-disk-cache>'
                f'window.__mineruDiskFitCache={{"version":"{LAYOUT_FIT_CACHE_VERSION}","complete":true,"count":1,'
                '"styles":[{"f":"9px","l":"1.2","o":"multi"}]};'
                '</script><main class="layout-doc"></main></body></html>',
                encoding="utf-8",
            )
            self.assertTrue(OT_ui.layout_html_has_complete_disk_fit_cache(path))

            view = _WebView()
            calls = []
            owner = SimpleNamespace(
                begin_layout_transition_overlay=lambda _view: calls.append("cover"),
                wait_for_layout_transition_ready=lambda _view, _url, generation: calls.append(generation),
                clear_layout_transition_overlay=lambda *_args: None,
                apply_reader_font_size=lambda: None,
                install_sync_scroll_bridge=lambda: None,
                schedule_layout_debug_overlay_update=lambda: None,
            )
            changed = OT_ui.MainWindow.load_layout_html_with_transition(
                owner,
                path,
                view,
                SimpleNamespace(setSource=lambda _url: None),
            )

            self.assertTrue(changed)
            self.assertNotIn("cover", calls)
            self.assertIn(None, calls)

    def test_disk_fit_cache_writer_uses_the_single_public_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview_layout_translated.html"
            path.write_text(
                f'<html><body data-layout-cache-version="{LAYOUT_FIT_CACHE_VERSION}" '
                'data-layout-cache-key="document" data-layout-cache-scope="scope">'
                '<script data-layout-fit-disk-cache></script><main class="layout-doc"></main></body></html>',
                encoding="utf-8",
            )
            state = {
                "fit_cache": {
                    "version": LAYOUT_FIT_CACHE_VERSION,
                    "complete": True,
                    "count": 1,
                    "styles": [{"f": "9px", "l": "1.2", "o": "multi"}],
                }
            }

            self.assertTrue(OT_ui.write_layout_fit_disk_cache(path, state))
            output = path.read_text(encoding="utf-8")
            versions = set(re.findall(r"layout-fit-[a-z-]*v\d+[a-z0-9_-]*", output))
            self.assertEqual(versions, {LAYOUT_FIT_CACHE_VERSION})
            self.assertIn("body.dataset.layoutCacheVersion", output)
            self.assertTrue(OT_ui.layout_html_has_complete_disk_fit_cache(path))

            state["fit_cache"]["version"] = "incompatible"
            self.assertFalse(OT_ui.write_layout_fit_disk_cache(path, state))

    def test_empty_disk_fit_marker_still_requires_layout_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview_layout_translated.html"
            path.write_text(
                '<script data-layout-fit-disk-cache></script>',
                encoding="utf-8",
            )
            self.assertFalse(OT_ui.layout_html_has_complete_disk_fit_cache(path))

            path.write_text(
                f'<body data-layout-cache-version="{LAYOUT_FIT_CACHE_VERSION}">'
                '<script data-layout-fit-disk-cache>'
                'window.__mineruDiskFitCache={"version":"old-cache","complete":true,"count":1,'
                '"styles":[{"f":"9px","l":"1.2","o":"multi"}]};</script></body>',
                encoding="utf-8",
            )
            self.assertFalse(OT_ui.layout_html_has_complete_disk_fit_cache(path))

            path.write_text(
                f'<body data-layout-cache-version="{LAYOUT_FIT_CACHE_VERSION}">'
                '<script data-layout-fit-disk-cache>'
                f'window.__mineruDiskFitCache={{"version":"{LAYOUT_FIT_CACHE_VERSION}","complete":true,"count":1,'
                '"styles":[{}]};</script></body>',
                encoding="utf-8",
            )
            self.assertFalse(OT_ui.layout_html_has_complete_disk_fit_cache(path))

        source = Path(OT_ui.__file__).read_text(encoding="utf-8")
        wait_start = source.index("    def wait_for_layout_transition_ready")
        wait_end = source.index("\n    def load_layout_html_with_transition", wait_start)
        wait_source = source[wait_start:wait_end]
        self.assertIn("web_view.url().toString() != target_url.toString()", wait_source)
        self.assertIn("poll_ready(attempt + 1)", wait_source)
        self.assertFalse(OT_ui.is_layout_preview_html_path("preview_layout_current.full.cleaned.pdf"))

    def test_reader_transition_waits_for_the_same_complete_layout_contract(self):
        probe = OT_ui.layout_fit_ready_probe_script()
        self.assertIn("layoutFitState === 'ready'", probe)
        self.assertIn("layout-fit-pending", probe)
        self.assertIn("layout-page-wrap", probe)

    def test_layout_body_font_override_only_targets_body_text_and_keeps_unitless_leading(self):
        script = OT_ui.layout_body_font_script(11.5)
        self.assertIn('data-style-kind="body_text"', script)
        self.assertIn('data-flow-kind="text"', script)
        self.assertIn("node.style.fontSize", script)
        self.assertNotIn("node.style.lineHeight =", script)
        self.assertIn("requestedPt = 11.50", script)

    def test_layout_body_font_probe_reports_fitted_computed_size_without_mutating_it(self):
        script = OT_ui.layout_body_font_probe_script()
        self.assertIn("getComputedStyle(node).fontSize", script)
        self.assertIn("px * 72 / 96", script)
        self.assertIn("layoutFitState === 'ready'", script)
        self.assertNotIn("node.style.fontSize =", script)

    def test_layout_body_font_document_key_canonicalizes_a_document_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "full.cleaned.md"
            source.write_text("paper", encoding="utf-8")
            translation = folder / "full.zh.md"
            self.assertEqual(
                OT_ui.layout_body_font_document_key(translation),
                OT_ui.layout_body_font_document_key(source),
            )

    def test_retranslation_clears_the_documents_font_override(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "full.cleaned.md"
            source.write_text("paper", encoding="utf-8")
            key = OT_ui.layout_body_font_document_key(source)
            reader = SimpleNamespace(source_path=source, layout_body_font_pt=11.5)
            window = SimpleNamespace(
                settings=SimpleNamespace(layout_body_font_by_document={key: 11.5}),
                reader_windows=[reader],
            )

            with patch.object(OT_ui.app_config, "save_settings") as save_settings:
                OT_ui.MainWindow.clear_layout_body_font_for_document(window, source)

            self.assertNotIn(key, window.settings.layout_body_font_by_document)
            self.assertIsNone(reader.layout_body_font_pt)
            save_settings.assert_called_once_with(window.settings)

    def test_layout_pdf_cache_version_uses_the_exported_documents_font_override(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "full.cleaned.md"
            source.write_text("paper", encoding="utf-8")
            layout_html = folder / "preview_layout_translation.html"
            layout_html.write_text("layout", encoding="utf-8")
            key = OT_ui.layout_body_font_document_key(source)
            window = SimpleNamespace(
                settings=SimpleNamespace(layout_body_font_by_document={key: 11.5}),
            )
            window.layout_body_font_pt_for_document = (
                lambda path: OT_ui.MainWindow.layout_body_font_pt_for_document(window, path)
            )

            version = OT_ui.MainWindow.current_layout_pdf_cache_version(window, layout_html)

            self.assertTrue(version.endswith("-11.50pt"))

    def test_reader_transition_is_excluded_from_pdf_and_word_export_inputs(self):
        source = Path(OT_ui.__file__).read_text(encoding="utf-8")
        transition_start = source.index("    def begin_layout_transition_overlay")
        transition_end = source.index("\n    def show_markdown_in_view", transition_start)
        transition_source = source[transition_start:transition_end]
        self.assertIn("web_view.grab()", transition_source)
        self.assertNotIn("write_text", transition_source)
        self.assertNotIn("replace(", transition_source)

        export_start = source.index("    def export_pane_document")
        export_end = source.index("\n    def export_markdown_with_pandoc", export_start)
        export_source = source[export_start:export_end]
        self.assertNotIn("layoutTransitionOverlay", export_source)
        self.assertNotIn("begin_layout_transition_overlay", export_source)

    def test_layout_pdf_preserves_dom_font_size_during_page_point_scaling(self):
        script = OT_ui.MainWindow.layout_pdf_prepare_script(body_font_pt=11.5)
        self.assertIn("const cssPxPerPoint = 96 / 72", script)
        self.assertIn("const readerScale =", script)
        self.assertIn("const printScale = readerScale * cssPxPerPoint", script)
        self.assertIn("const printBlockHeight = Math.max(1, pageHeight * printScale - 1)", script)
        self.assertIn("shell.style.height = `${printBlockHeight}px`", script)
        self.assertIn("page-break-after: auto !important; break-after: page !important", script)
        self.assertIn(".layout-page-wrap:last-of-type", script)
        self.assertIn("page.style.zoom = '1'", script)
        self.assertIn("window.__mineruFitLayoutEquations", script)
        self.assertIn("const userBodyFontPt = 11.50", script)
        self.assertIn('data-style-kind="body_text"', script)
        self.assertIn("node.style.fontSize = `${userBodyFontPt}pt`", script)

    def test_layout_pdf_page_size_has_bottom_quantization_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            html_path = Path(directory) / "layout.html"
            html_path.write_text(
                '<section class="layout-page-wrap" data-page-width="612" data-page-height="792"></section>',
                encoding="utf-8",
            )
            window = SimpleNamespace(layout_pdf_reader_scale=lambda _path: 1.5)
            page_layout = OT_ui.MainWindow.pdf_page_layout_for_export(window, html_path, layout_mode=True)

            page_rect = page_layout.fullRect(QPageLayout.Unit.Point)
            self.assertAlmostEqual(page_rect.width(), 612 * 1.5)
            self.assertAlmostEqual(
                page_rect.height(),
                792 * 1.5 + OT_ui.LAYOUT_PDF_PAGE_HEIGHT_ALLOWANCE_PT,
            )

    def test_layout_editable_docx_has_one_valid_picture_shape_per_source_image(self):
        one_pixel_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first_image = folder / "first.png"
            second_image = folder / "second.png"
            first_image.write_bytes(one_pixel_png)
            second_image.write_bytes(one_pixel_png)
            html_path = folder / "layout.html"
            html_path.write_text(
                '<section class="layout-page-wrap" data-page-width="612" data-page-height="792">'
                '<div class="layout-page">'
                f'<div class="layout-block type-image" style="left:10px;top:20px;width:100px;height:70px"><img src="{first_image.resolve().as_uri()}"></div>'
                f'<div class="layout-block type-image" style="left:210px;top:220px;width:120px;height:80px"><img src="{second_image.resolve().as_uri()}"></div>'
                '</div></section>',
                encoding="utf-8",
            )
            out_path = folder / "layout.docx"
            render_layout_editable_html_docx(html_path, out_path)

            with zipfile.ZipFile(out_path) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
                relationships_xml = archive.read("word/_rels/document.xml.rels").decode("utf-8")
                settings_xml = archive.read("word/settings.xml").decode("utf-8")
                media_files = [name for name in archive.namelist() if name.startswith("word/media/")]

            self.assertEqual(len(media_files), 2)
            self.assertEqual(document_xml.count("<v:imagedata "), 2)
            self.assertEqual(relationships_xml.count("/relationships/image\""), 2)
            self.assertIn('type="#_x0000_t75"', document_xml)
            self.assertIn('o:spid="_x0000_s1025"', document_xml)
            self.assertIn('w:name="compatibilityMode"', settings_xml)

    def test_layout_docx_image_anchor_uses_a_standard_word_picture_shape(self):
        anchor = layout_docx_image_anchor(
            {"bbox": [10, 20, 110, 90]},
            "rId7",
            "layout_html_shape_7",
        )
        self.assertIn('type="#_x0000_t75"', anchor)
        self.assertIn('o:spid="_x0000_s1031"', anchor)
        self.assertIn('r:id="rId7"', anchor)

    def test_layout_docx_keeps_chart_images_at_their_html_coordinates(self):
        one_pixel_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first_image = folder / "first.png"
            second_image = folder / "second.png"
            first_image.write_bytes(one_pixel_png)
            second_image.write_bytes(one_pixel_png)
            html_path = folder / "layout.html"
            html_path.write_text(
                '<section class="layout-page-wrap" data-page-width="612" data-page-height="792">'
                '<div class="layout-page">'
                '<div class="layout-block type-chart_caption" style="left:8px;top:20px;width:18px;height:12px">(a)</div>'
                f'<div class="layout-block type-chart_body" style="left:10px;top:20px;width:100px;height:70px"><img src="{first_image.resolve().as_uri()}"></div>'
                '<div class="layout-block type-chart_caption" style="left:8px;top:100px;width:18px;height:12px">(b)</div>'
                f'<div class="layout-block type-chart_body" style="left:10px;top:100px;width:100px;height:70px"><img src="{second_image.resolve().as_uri()}"></div>'
                '<div class="layout-block type-chart_caption" style="left:10px;top:180px;width:300px;height:20px">Figure 1</div>'
                '</div></section>',
                encoding="utf-8",
            )

            items = layout_docx_items_from_html(html_path)[0]["items"]
            image_items = [item for item in items if item.get("image_path")]
            self.assertEqual([item["bbox"][1] for item in image_items], [20.0, 100.0])

            out_path = folder / "layout.docx"
            render_layout_editable_html_docx(html_path, out_path)
            with zipfile.ZipFile(out_path) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")

            self.assertNotIn("(a)", document_xml)
            self.assertNotIn("(b)", document_xml)
            self.assertIn("Figure 1", document_xml)

    def test_layout_pdf_refits_equation_geometry_without_blocking_on_animation_frames(self):
        source = Path(OT_ui.__file__).read_text(encoding="utf-8")
        export_source = source[source.index("    def export_html_to_pdf"):]
        self.assertNotIn("math-pending", export_source)
        self.assertNotIn("layoutMathState", export_source)

        layout_source = (Path(OT_ui.__file__).parent / "PB_layout.py").read_text(encoding="utf-8")
        self.assertNotIn("currentSpace - rightOverflow", layout_source)

    def test_visible_reader_state_replaces_an_older_local_storage_cache_for_pdf(self):
        source = Path(OT_ui.__file__).read_text(encoding="utf-8")
        disk_cache_start = source.index("def write_layout_fit_disk_cache")
        disk_cache_end = source.index("\n\nPREVIEW_HISTORY_REUSE_LIMIT", disk_cache_start)
        disk_cache_source = source[disk_cache_start:disk_cache_end]
        self.assertNotIn("window.__mineruInitialFitCache||!value", disk_cache_source)
        self.assertIn("invalidate_layout_pdf_cache(layout_html_path)", source)

    def test_invalidating_layout_pdf_cache_keeps_the_html_and_state_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            html_path = Path(directory) / "layout.html"
            html_path.write_text("layout", encoding="utf-8")
            pdf_cache, meta_cache = OT_ui.layout_pdf_cache_paths(html_path)
            state_cache = OT_ui.layout_state_cache_path(html_path)
            pdf_cache.write_bytes(b"cached pdf")
            meta_cache.write_text("cached meta", encoding="utf-8")
            state_cache.write_text("reader state", encoding="utf-8")
            OT_ui.invalidate_layout_pdf_cache(html_path)
            self.assertTrue(html_path.exists())
            self.assertTrue(state_cache.exists())
            self.assertFalse(pdf_cache.exists())
            self.assertFalse(meta_cache.exists())

    def test_layout_preview_interpolates_the_serif_font_stack(self):
        source = Path(OT_ui.__file__).with_name("PB_layout.py").read_text(encoding="utf-8")
        self.assertIn('layout_css.replace(\n            "{SERIF_READING_FONT_STACK}",\n            SERIF_READING_FONT_STACK,', source)
        self.assertIn("font-family: {SERIF_READING_FONT_STACK};", source)
        self.assertIn('min(1.22, inferred_body_style[1] + 0.04)', source)
        self.assertIn('page.style.zoom = String(scale)', source)
        self.assertIn('zoom:{scale:.6f}', source)

    def test_recent_unchanged_page_uses_webengine_history(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.html"
            second = Path(directory) / "second.html"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            first_url = QUrl.fromLocalFile(str(first))
            second_url = QUrl.fromLocalFile(str(second))
            item = _HistoryItem(first_url)
            history = _History([item])
            view = _WebView(second_url, history)
            view._preview_signature_by_url = {
                first_url.toString(): OT_ui.web_view_url_content_signature(first_url)
            }

            self.assertTrue(OT_ui.set_or_reload_web_view_url(view, first_url))
            self.assertIs(history.selected, item)
            self.assertEqual(view.set_urls, [])

    def test_preview_history_is_trimmed_before_a_third_document(self):
        with tempfile.TemporaryDirectory() as directory:
            first_url = QUrl.fromLocalFile(str(Path(directory) / "first.html"))
            second_url = QUrl.fromLocalFile(str(Path(directory) / "second.html"))
            third_url = QUrl.fromLocalFile(str(Path(directory) / "third.html"))
            history = _History()
            view = _WebView(second_url, history)
            view._preview_signature_by_url = {
                first_url.toString(): (first_url.toString(), 1, 1),
                second_url.toString(): (second_url.toString(), 1, 1),
            }

            with patch.object(OT_ui, "web_view_url_content_signature", return_value=(third_url.toString(), 1, 1)):
                self.assertTrue(OT_ui.set_or_reload_web_view_url(view, third_url))

            self.assertEqual(history.clear_count, 1)
            self.assertEqual(len(view._preview_signature_by_url), 2)

    def test_resize_refit_never_runs_full_layout_fill(self):
        source = Path(OT_ui.__file__).read_text(encoding="utf-8")
        for start in (source.index("class ReaderWindow"), source.index("class MainWindow")):
            method_start = source.index("    def refit_preview_pages", start)
            method_end = source.index("\n    def ", method_start + 5)
            method = source[method_start:method_end]
            self.assertIn("__mineruFitLayoutPages", method)
            self.assertNotIn("__mineruRunLayoutFill", method)
            self.assertNotIn("__mineruFitLayoutEquations", method)

    def test_generated_layout_runtime_uses_atomic_fit_and_persistent_cache(self):
        source = (Path(OT_ui.__file__).parent / "PB_layout.py").read_text(encoding="utf-8")
        self.assertIn("function restoreFitCache()", source)
        self.assertIn("layout_fit_cache_bootstrap_html", source)
        self.assertIn("complete: true", source)
        self.assertNotIn("requestIdleCallback", source)
        self.assertNotIn("content-visibility: auto", source)
        self.assertIn("function runAtomicInitialFit()", source)
        self.assertIn("layout-fit-pending", source)
        self.assertIn("data-layout-progress", source)
        self.assertNotIn("contentVisibility = 'visible'", source)
        self.assertIn("全文已收敛", source)
        self.assertIn("全局限制", source)
        self.assertIn("bodyInspection", source)
        self.assertEqual(LAYOUT_FIT_CACHE_VERSION, "layout-fit-cache-v12-logical-span-lines")
        self.assertIn("function syncInheritedBodyFontToBodyGroup()", source)
        self.assertIn(".map((node) => parseFloat(node.style.fontSize || node.dataset.baseFont || '0') || 0)", source)
        self.assertNotIn("layoutControlFontSize(node, 0)", source)
        self.assertIn("data-layout-cache-version", source)
        self.assertIn("payload.version !== fitCacheVersion", source)
        cache_versions = set(re.findall(r"layout-fit-[a-z-]*v\d+[a-z0-9_-]*", source))
        self.assertEqual(cache_versions, {LAYOUT_FIT_CACHE_VERSION})
        self.assertIn("body-iteration-collision", source)
        self.assertIn("function horizontalBoxesOverlap", source)
        self.assertIn("bodyColumnIndependentFit: true", source)
        self.assertIn("bodyTextCollisionGeometry: true", source)
        self.assertIn("sourceIsBodyText", source)
        self.assertIn("barrierUsesTextGeometry", source)
        self.assertIn("function rectUnion(rects)", source)
        self.assertIn("barrier.contentBounds", source)
        self.assertIn("Broad phase: a union of the barrier's actual rendered rects", source)
        self.assertNotIn("function runLazyInitialFit()", source)

    def test_layout_flow_stream_overflow_is_visible_while_page_shell_clips(self):
        source = (Path(OT_ui.__file__).parent / "PB_layout.py").read_text(encoding="utf-8")
        css_start = source.index('    layout_css = """', source.index("def render_layout_preview_html"))
        css_end = source.index('"""', css_start + len('    layout_css = """'))
        layout_css = source[css_start:css_end]
        shell_rule = re.search(r"(?ms)^\.layout-page-shell \{(.*?)^\}", layout_css)
        flow_rule = re.search(r"(?ms)^\.layout-flow-stream \{(.*?)^\}", layout_css)

        self.assertIsNotNone(shell_rule)
        self.assertIsNotNone(flow_rule)
        self.assertIn("overflow: hidden;", shell_rule.group(1))
        self.assertIn("overflow: visible;", flow_rule.group(1))

    def test_body_collision_line_backoff_uses_the_shared_1_02_minimum(self):
        source = (Path(OT_ui.__file__).parent / "PB_layout.py").read_text(encoding="utf-8")
        start = source.index("function continueUnderfilledNodes(nodes, options)")
        end = source.index("function clampTranslatedOverflow()", start)
        body_iteration = source[start:end]
        self.assertIn("collisionMinLineRatio: 1.02", source)
        self.assertIn(": 1.02;", body_iteration)
        self.assertIn("const sourceMinLineRatio = minLineRatio;", body_iteration)
        self.assertIn("applyGroup([source], fontSize, lineRatio);", body_iteration)

    def test_production_layout_does_not_publish_diagnostic_hover_titles(self):
        source = (Path(OT_ui.__file__).parent / "PB_layout.py").read_text(encoding="utf-8")
        self.assertIn("function setDiagnosticTitle(node, text)", source)
        self.assertIn(
            "node.title = document.body.classList.contains('layout-debug') ? (text || '') : '';",
            source,
        )
        self.assertNotIn("node.title = node.dataset.fitLabel;", source)
        self.assertNotIn("node.title = label;", source)

    def test_layout_exports_require_ready_state_without_async_promise_callbacks(self):
        layout_source = (Path(OT_ui.__file__).parent / "PB_layout.py").read_text(encoding="utf-8")
        ui_source = Path(OT_ui.__file__).read_text(encoding="utf-8")
        state_start = layout_source.index("def layout_docx_runtime_state_script")
        state_end = layout_source.index("\ndef ", state_start + 5)
        state_script = layout_source[state_start:state_end]
        self.assertIn("layout-not-ready", state_script)
        self.assertNotIn("__mineruRunLayoutFill", state_script)
        self.assertNotIn("typesetPromise", state_script)
        self.assertNotIn("querySelectorAll('*')", state_script)
        export_source = ui_source[ui_source.index("def export_html_to_pdf"):]
        self.assertIn("__mineruPrepareLayoutExport", export_source)
        self.assertIn("未生成空白文件", export_source)
        self.assertIn(OT_ui.LAYOUT_PDF_CACHE_VERSION, ui_source)
        self.assertIn("page-quantization", OT_ui.LAYOUT_PDF_CACHE_VERSION)

    def test_layout_word_keeps_page_coordinate_text_units_and_text_block_boundaries(self):
        self.assertEqual(layout_docx_output_font_size(8.0), 8.0)
        self.assertEqual(layout_docx_output_font_size(13.0), 13.0)
        self.assertEqual(layout_docx_formula_point_scale(r"\frac{a}{b}"), 1.0)
        self.assertEqual(layout_docx_safe_text_height({}, 42.0, 8.0), 42.0)
        self.assertEqual(layout_docx_safe_text_height({}, 12.0, 8.0, formula_text=r"\frac{a}{b}"), 16.0)
        state_script = (Path(OT_ui.__file__).parent / "PB_layout.py").read_text(encoding="utf-8")
        self.assertIn("font_size: fontSize,", state_script)
        self.assertIn('w:lineRule="exact"', state_script)
        self.assertIn('mso-fit-shape-to-text:t', state_script)
        self.assertTrue(hasattr(OT_ui, "QImage"))

    def test_layout_docx_equation_and_text_clearance_prevents_vertical_overlap(self):
        # Two equations with multi-level TeX followed by text in the same column
        items = [
            {
                "bbox": [54.0, 100.0, 230.0, 18.0],
                "type": "interline_equation",
                "font_size": 9.0,
                "node": {
                    "tag": "div",
                    "attrs": {},
                    "children": [
                        {"tag": "span", "attrs": {"class": "layout-math"}, "children": [{"tag": "text", "text": r"\[\frac{a + b}{c + d}\]"}]}
                    ],
                },
            },
            {
                "bbox": [54.0, 125.0, 230.0, 14.0],
                "type": "text",
                "font_size": 9.0,
                "node": {"tag": "p", "attrs": {}, "children": [{"tag": "text", "text": "根据上式计算结果得出"}]},
            },
            {
                "bbox": [54.0, 145.0, 230.0, 18.0],
                "type": "interline_equation",
                "font_size": 9.0,
                "node": {
                    "tag": "div",
                    "attrs": {},
                    "children": [
                        {"tag": "span", "attrs": {"class": "layout-math"}, "children": [{"tag": "text", "text": r"\[\sum_{i=1}^n \frac{x_i}{y_i}\]"}]}
                    ],
                },
            },
            {
                "bbox": [54.0, 170.0, 230.0, 14.0],
                "type": "text",
                "font_size": 9.0,
                "node": {"tag": "p", "attrs": {}, "children": [{"tag": "text", "text": "最终代入方程得解"}]},
            },
        ]
        layout_docx_apply_equation_clearance(items)
        # All items in the same column must have strictly monotonic, non-overlapping vertical spans
        for index in range(len(items) - 1):
            curr_y = items[index]["bbox"][1]
            next_y = items[index + 1]["bbox"][1]
            self.assertGreater(next_y, curr_y, f"Item {index+1} y ({next_y}) should be strictly below Item {index} y ({curr_y})")

    def test_layout_docx_title_line_ratio_enforces_safe_height_for_exact_rule(self):
        title_item = {
            "bbox": [54.0, 50.0, 230.0, 16.0],
            "type": "title",
            "font_size": 13.0,
            "line_ratio": 0.995,  # Low line ratio from source PDF that would clip glyph tops under exact rule
            "para_gap": 0.0,
            "node": {"tag": "h2", "attrs": {}, "children": [{"tag": "text", "text": "4 结果与讨论"}]},
        }
        xml = layout_docx_dom_text_anchor(title_item, "test_shape_1", {})
        # With effective_line_ratio >= 1.18, 13.0 * 20 * 1.18 = 306.8 -> 307 twips
        line_match = re.search(r'w:line="(\d+)"', xml)
        self.assertIsNotNone(line_match)
        line_twips = int(line_match.group(1))
        min_safe_twips = int(round(13.0 * 20.0 * 1.18))
        self.assertGreaterEqual(line_twips, min_safe_twips)

    def test_layout_formula_viewer_preserves_mathjax_menu_and_appends_ai_action(self):
        source = (Path(OT_ui.__file__).parent / "PB_layout.py").read_text(encoding="utf-8")
        self.assertIn("function initLayoutFormulaInteractions()", source)
        self.assertIn("getMathItemsWithin(container)", source)
        self.assertIn("layout-formula-lightbox", source)
        self.assertIn("CtxtMenu_ContextMenu", source)
        self.assertIn("item.textContent = '提问'", source)
        self.assertNotIn("preventDefault();\n      const container", source[source.index("function initLayoutFormulaInteractions()"):])

    def test_multiple_formula_quotes_are_aggregated_in_order(self):
        chat = SimpleNamespace(pending_reference_quotes=[
            {"type": "formula", "text": "公式一", "formula_tex": "a+b"},
            {"type": "formula", "text": "公式二", "formula_tex": "c+d"},
        ])
        combined = AI_chat.ChatWindow.combined_pending_reference_quote(chat)
        self.assertEqual(len(combined["quotes"]), 2)
        self.assertLess(combined["text"].index("公式一"), combined["text"].index("公式二"))

    def test_layout_export_cache_is_bound_to_current_html_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            html_path = Path(directory) / "layout.html"
            cache_path = Path(directory) / "layout.state.json"
            html_path.write_text("first generation", encoding="utf-8")
            OT_ui.write_layout_cache_payload(cache_path, html_path, "state", {"pages": [1]})
            self.assertEqual(
                OT_ui.read_current_layout_cache_payload(cache_path, html_path, "state"),
                {"pages": [1]},
            )
            html_path.write_text("second generation with different size", encoding="utf-8")
            self.assertIsNone(OT_ui.read_current_layout_cache_payload(cache_path, html_path, "state"))

    def test_retranslation_cache_clear_keeps_the_published_layout_until_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            markdown_path = root / "full.cleaned.md"
            markdown_path.write_text("source", encoding="utf-8")
            layout_html = OT_ui.layout_translation_preview_html_path(markdown_path)
            layout_pdf = layout_html.with_name(f"{layout_html.stem}.final-layout.pdf")
            state = layout_html.with_name(f"{layout_html.stem}.final-layout-state.json")
            cache = root / "layout_translation_blocks.zh.json"
            for path in (layout_html, layout_pdf, state, cache):
                path.write_text("published" if path != cache else "stale-cache", encoding="utf-8")

            LS_pipeline.clear_layout_translation_artifacts(
                markdown_path,
                "简体中文",
                preserve_published_preview=True,
            )

            self.assertTrue(layout_html.exists())
            self.assertTrue(layout_pdf.exists())
            self.assertTrue(state.exists())
            self.assertFalse(cache.exists())

    def test_layout_publication_rolls_back_both_artifacts_if_second_replace_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            html_path = root / "published.html"
            bundle_path = root / "published.json"
            html_tmp = root / ".published.html.tmp"
            bundle_tmp = root / ".published.json.tmp"
            html_path.write_text("old-html", encoding="utf-8")
            bundle_path.write_text("old-bundle", encoding="utf-8")
            html_tmp.write_text("new-html", encoding="utf-8")
            bundle_tmp.write_text("new-bundle", encoding="utf-8")
            original_replace = Path.replace

            def fail_bundle_publish(path, target):
                if path == bundle_tmp:
                    raise OSError("simulated bundle replacement failure")
                return original_replace(path, target)

            with patch.object(Path, "replace", new=fail_bundle_publish):
                with self.assertRaises(OSError):
                    LS_pipeline.publish_layout_translation_artifacts(
                        html_tmp,
                        html_path,
                        bundle_tmp,
                        bundle_path,
                    )

            self.assertEqual(html_path.read_text(encoding="utf-8"), "old-html")
            self.assertEqual(bundle_path.read_text(encoding="utf-8"), "old-bundle")


class SyncScrollInteropTests(unittest.TestCase):
    def test_layout_image_manager_keeps_text_and_unloads_only_far_images(self):
        scripts = []

        class FakePage:
            def runJavaScript(self, script):
                scripts.append(script)

        class FakeView:
            def page(self):
                return FakePage()

        OT_ui.install_layout_image_memory_manager(FakeView())

        self.assertEqual(len(scripts), 1)
        self.assertIn("contentVisibility", scripts[0])
        self.assertIn("IntersectionObserver", scripts[0])
        self.assertIn("mineruDeferredSrc", scripts[0])
        self.assertIn("image.removeAttribute('src')", scripts[0])
        self.assertNotIn("page.remove()", scripts[0])

    def test_sync_callback_json_decoder_accepts_pyside_and_legacy_payloads(self):
        payload = {"ratio": 0.5, "heading": None}
        self.assertEqual(OT_ui.decode_web_javascript_payload('{"ratio": 0.5, "heading": null}'), payload)
        self.assertEqual(OT_ui.decode_web_javascript_payload(payload), payload)
        self.assertIsNone(OT_ui.decode_web_javascript_payload("not json"))
        self.assertIsNone(OT_ui.decode_web_javascript_payload(None))
        source = Path(OT_ui.__file__).read_text(encoding="utf-8")
        self.assertIn("return JSON.stringify(context);", source)
        self.assertIn("return JSON.stringify({ratio:", source)
        self.assertIn("return JSON.stringify({ ready, pageCount: pages.length, pages, cssPxPerPoint, readerScale });", source)

    def test_initial_sync_uses_serialized_webengine_payload(self):
        source = object()
        target = object()
        captured = []
        window = SimpleNamespace(
            sync_scroll_check=SimpleNamespace(isChecked=lambda: True),
            show_parsed_source_check=SimpleNamespace(isChecked=lambda: True),
            settings=SimpleNamespace(layout_reading_mode=False),
            source_web_view=source,
            translation_web_view=target,
            _sync_poll_generation=7,
        )

        def run_javascript(web_view, _script, callback=None):
            self.assertIs(web_view, source)
            callback('{"ratio": 0.5, "heading": null}')
            return True

        window._run_sync_javascript = run_javascript
        window.apply_sync_payload_to_target = lambda web_view, payload: captured.append((web_view, payload))

        OT_ui.MainWindow.sync_translation_to_source_now(window)

        self.assertEqual(captured, [(target, {"ratio": 0.5, "heading": None})])

    def test_poll_sync_bridge_accepts_serialized_webengine_state(self):
        source = object()
        target = object()
        captured = []
        window = SimpleNamespace(
            _syncing_scroll=False,
            _sync_poll_inflight=False,
            _sync_poll_generation=3,
            _last_source_user_scroll_at=0,
            _last_translation_user_scroll_at=0,
            sync_scroll_check=SimpleNamespace(isChecked=lambda: True),
            show_parsed_source_check=SimpleNamespace(isChecked=lambda: True),
            settings=SimpleNamespace(layout_reading_mode=False),
            source_web_view=source,
            translation_web_view=target,
        )

        def run_javascript(web_view, _script, callback=None):
            result = (
                '{"userScrollAt": 100, "payload": {"ratio": 0.25, "heading": null}}'
                if web_view is source
                else '{"userScrollAt": 0, "payload": {"ratio": 0, "heading": null}}'
            )
            callback(result)
            return True

        window._run_sync_javascript = run_javascript
        window.apply_sync_payload_to_target = lambda web_view, payload: captured.append((web_view, payload))

        OT_ui.MainWindow.poll_sync_scroll_bridge(window)

        self.assertEqual(captured, [(target, {"ratio": 0.25, "heading": None})])


@unittest.skipUnless(OT_ui.PDF_VIEW_AVAILABLE, "QtPdfWidgets is unavailable")
class PdfSourceViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_demand_rendered_pdf_exposes_page_sync_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "three-pages.pdf"
            document = pdfium.PdfDocument.new()
            try:
                for _ in range(3):
                    document.new_page(612, 792)
                document.save(str(pdf_path))
            finally:
                document.close()

            view = OT_ui.create_synced_pdf_view()
            owner = SimpleNamespace(
                source_pdf_view=view,
                source_web_view=None,
                source_fallback_viewer=SimpleNamespace(setVisible=lambda _visible: None),
                _source_pdf_active=False,
                _syncing_scroll=False,
                install_sync_scroll_bridge=lambda: None,
                sync_translation_to_source_now=lambda: None,
            )

            self.assertTrue(OT_ui.load_source_pdf(owner, pdf_path))
            try:
                self.assertEqual(view.document().pageCount(), 3)
                view.pageNavigator().jump(1, QPointF(0.0, 396.0), 0)
                payload = OT_ui.pdf_sync_payload(view)

                self.assertEqual(payload["layoutPage"], 1)
                self.assertGreaterEqual(payload["pageOffsetRatio"], 0.0)
                self.assertLessEqual(payload["pageOffsetRatio"], 1.0)
                self.assertEqual(payload["viewportAnchorRatio"], 0.5)
            finally:
                OT_ui.release_source_pdf(owner)
                view.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

    def test_release_source_pdf_releases_file_before_directory_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "deletable.pdf"
            document = pdfium.PdfDocument.new()
            try:
                document.new_page(612, 792)
                document.save(str(pdf_path))
            finally:
                document.close()

            view = OT_ui.create_synced_pdf_view()
            owner = SimpleNamespace(
                source_pdf_view=view,
                source_web_view=None,
                source_fallback_viewer=SimpleNamespace(setVisible=lambda _visible: None),
                _source_pdf_active=False,
                _syncing_scroll=False,
                install_sync_scroll_bridge=lambda: None,
                sync_translation_to_source_now=lambda: None,
            )
            try:
                self.assertTrue(OT_ui.load_source_pdf(owner, pdf_path))
                OT_ui.release_source_pdf(owner)
                pdf_path.unlink()
                self.assertFalse(pdf_path.exists(), "releasing the PDF view must unlock the source file")
            finally:
                OT_ui.release_source_pdf(owner)
                view.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

    def test_translation_payload_jumps_back_to_pdf_page(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "four-pages.pdf"
            document = pdfium.PdfDocument.new()
            try:
                for _ in range(4):
                    document.new_page(612, 792)
                document.save(str(pdf_path))
            finally:
                document.close()

            view = OT_ui.create_synced_pdf_view()
            owner = SimpleNamespace(
                source_pdf_view=view,
                source_web_view=None,
                source_fallback_viewer=SimpleNamespace(setVisible=lambda _visible: None),
                _source_pdf_active=False,
                _syncing_scroll=False,
                install_sync_scroll_bridge=lambda: None,
                sync_translation_to_source_now=lambda: None,
            )
            self.assertTrue(OT_ui.load_source_pdf(owner, pdf_path))
            try:
                view.resize(900, 900)
                view.show()
                QTest.qWait(120)

                self.assertTrue(
                    OT_ui.scroll_pdf_view_to_payload(
                        owner,
                        {
                            "layoutPage": 2,
                            "pageOffsetRatio": 0.25,
                            "viewportAnchorRatio": 0.5,
                        },
                    )
                )
                QTest.qWait(80)
                round_trip = OT_ui.pdf_sync_payload(view)
                self.assertEqual(round_trip["layoutPage"], 2)
                self.assertAlmostEqual(round_trip["pageOffsetRatio"], 0.25, places=2)
            finally:
                OT_ui.release_source_pdf(owner)
                view.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

    def test_pdf_reference_anchor_scrolls_and_shows_highlight(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "reference.pdf"
            document = pdfium.PdfDocument.new()
            try:
                document.new_page(612, 792)
                document.save(str(pdf_path))
            finally:
                document.close()

            view = OT_ui.create_synced_pdf_view()
            owner = SimpleNamespace(
                source_pdf_view=view,
                source_web_view=None,
                source_fallback_viewer=SimpleNamespace(setVisible=lambda _visible: None),
                _source_pdf_active=False,
                _syncing_scroll=False,
                install_sync_scroll_bridge=lambda: None,
                sync_translation_to_source_now=lambda: None,
            )
            self.assertTrue(OT_ui.load_source_pdf(owner, pdf_path))
            try:
                view.resize(800, 900)
                view.show()
                QTest.qWait(120)
                quote = {
                    "type": "formula",
                    "anchor_page": 1,
                    "anchor_rect": {
                        "x": 0.2,
                        "y": 0.3,
                        "width": 0.4,
                        "height": 0.05,
                    },
                }

                self.assertTrue(OT_ui.focus_pdf_reference_quote(owner, quote))
                self.app.processEvents()
                self.assertTrue(view._reference_overlay.isVisible())
                highlight = OT_ui.pdf_reference_highlight_rect(view, quote)
                self.assertIsNotNone(highlight)
                self.assertGreater(highlight.width(), 10)
                self.assertGreater(highlight.height(), 10)
            finally:
                OT_ui.release_source_pdf(owner)
                view.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

    def test_pdf_canvas_and_page_gap_match_layout_preview(self):
        view = OT_ui.create_synced_pdf_view()
        try:
            self.assertEqual(view.pageSpacing(), 18)
            self.assertEqual(view.documentMargins().left(), 10)
            self.assertEqual(
                view.palette().color(view.palette().ColorRole.Dark).name(),
                "#f6f3ee",
            )
        finally:
            view.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
