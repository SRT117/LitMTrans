"""Regression tests for Zotero-parity document diagram tasks."""

from __future__ import annotations

import unittest
import re
from types import SimpleNamespace

from PySide6.QtCore import QRectF, QSizeF
from PySide6.QtGui import QPolygonF
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from AI_chat import ChatWindow, DOCUMENT_AI_TASKS
from AI_diagrams import (
    FLOWCHART_V2_MARKER,
    MINDMAP_V2_MARKER,
    parse_flowchart_v2,
    parse_mindmap_v2,
    render_diagram_html,
    render_flowchart_svg,
    render_mindmap_svg,
)
from AI_widgets import ChatTextBubble, DiagramViewerDialog
from OT_ui import closest_evidence_sentence, reference_focus_script, resolve_pdf_evidence_sentence


class ChatDiagramTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_mindmap_protocol_requires_one_valid_root(self):
        source = (
            f"{MINDMAP_V2_MARKER}\n"
            '{"version":2,"title":"论文地图","nodes":['
            '{"id":"root","parentId":null,"label":"论文"},'
            '{"id":"result","parentId":"root","label":"主要结果"}]}'
        )
        diagram = parse_mindmap_v2(source)
        self.assertIsNotNone(diagram)
        self.assertIn("论文地图", render_diagram_html(source))
        self.assertIsNone(parse_mindmap_v2(f"{MINDMAP_V2_MARKER}\n{{\"version\":2,\"nodes\":[]}}"))

    def test_flowchart_protocol_rejects_unknown_edge_endpoint(self):
        source = (
            f"{FLOWCHART_V2_MARKER}\n"
            '{"version":2,"title":"证据链","nodes":['
            '{"id":"gap","label":"研究缺口","type":"process"},'
            '{"id":"result","label":"结论","type":"terminator"}],'
            '"edges":[{"from":"gap","to":"result","label":"支持"}]}'
        )
        self.assertIsNotNone(parse_flowchart_v2(source))
        self.assertIn("litmtrans-diagram", render_diagram_html(source))
        invalid = source.replace('"to":"result"', '"to":"missing"')
        self.assertIsNone(parse_flowchart_v2(invalid))

    def test_valid_diagram_is_never_line_folded_before_rendering(self):
        source = (
            f"{MINDMAP_V2_MARKER}\n"
            '{\n"version":2,\n"nodes":[\n'
            '{"id":"root","parentId":null,"label":"论文"},\n'
            '{"id":"method","parentId":"root","label":"方法"}\n]}'
        )
        bubble = ChatTextBubble(source, "assistant", render_markdown=True)
        self.assertEqual(bubble.current_display_text(), source)
        self.assertFalse(bubble.is_expandable)

    def test_prepare_document_task_preserves_protocol_and_question(self):
        captured = {}
        chat = SimpleNamespace(
            document_contexts=[{"path": "paper.md"}],
            document_context_sent=False,
            shared_app_settings=None,
            settings=SimpleNamespace(key_points_prompt="优先关注实验条件"),
            set_pending_reference_quote=lambda quote: captured.update(quote),
            input_box=QPlainTextEdit(),
            _document_task_has_context=lambda: True,
        )
        prepared = ChatWindow.prepare_document_ai_task(chat, "key_points", {"title": "示例论文"})
        self.assertTrue(prepared)
        self.assertEqual(chat.input_box.toPlainText(), DOCUMENT_AI_TASKS["key_points"]["question"])
        self.assertIn(MINDMAP_V2_MARKER, captured["text"])
        self.assertIn("优先关注实验条件", captured["text"])

    def test_mindmap_balances_branches_and_preserves_long_label(self):
        source = (
            f"{MINDMAP_V2_MARKER}\n"
            '{"version":2,"title":"完整知识结构","nodes":['
            '{"id":"root","parentId":null,"label":"论文核心问题"},'
            '{"id":"background","parentId":"root","label":"研究背景、领域现状、尚未解决的理论问题以及本文选择这一问题的具体原因","kind":"background"},'
            '{"id":"method","parentId":"root","label":"实验设计、数据处理和统计验证","kind":"method"},'
            '{"id":"result","parentId":"root","label":"主要结果与机制解释","kind":"result"},'
            '{"id":"limit","parentId":"root","label":"局限与后续研究方向","kind":"limitation"}]}'
        )
        diagram = parse_mindmap_v2(source)
        svg = render_mindmap_svg(diagram)
        self.assertIn('data-kind="result"', svg)
        self.assertIn('data-kind="limitation"', svg)
        self.assertGreater(svg.count(" C "), 2)
        self.assertGreater(svg.count('<tspan class="node-title"'), 5)

    def test_flowchart_draws_semantic_shapes_and_viewer_controls(self):
        source = (
            f"{FLOWCHART_V2_MARKER}\n"
            '{"version":2,"layout":"TB","title":"研究证据链","nodes":['
            '{"id":"start","label":"研究问题","type":"terminator"},'
            '{"id":"choice","label":"证据是否一致","type":"decision"},'
            '{"id":"data","label":"实验数据库","type":"database"},'
            '{"id":"paper","label":"形成研究报告","type":"document"}],'
            '"edges":[{"from":"start","to":"choice"},{"from":"choice","to":"data","label":"是"},{"from":"data","to":"paper"}]}'
        )
        diagram = parse_flowchart_v2(source)
        svg = render_flowchart_svg(diagram)
        self.assertIn("type-decision", svg)
        self.assertIn("type-database", svg)
        self.assertIn("type-document", svg)
        viewer = DiagramViewerDialog.viewer_html(source)
        self.assertIn('id="fit"', viewer)
        self.assertIn('id="export-image"', viewer)
        self.assertIn("bridge.requestImageExport()", viewer)
        self.assertIn('id="viewport"', viewer)
        self.assertIn("pointermove", viewer)
        self.assertIn("diagram-node-popover", viewer)
        self.assertIn("Ctrl + 鼠标滚轮缩放", viewer)
        self.assertIn("centerPopover", viewer)
        self.assertIn("locateEvidence", viewer)

    def test_diagram_viewer_can_rasterize_the_complete_svg_as_png(self):
        script = DiagramViewerDialog.image_export_script()
        self.assertIn("#world svg.litmtrans-diagram", script)
        self.assertIn("canvas.width = width * 2", script)
        self.assertIn("toDataURL('image/png')", script)
        self.assertIn("bridge.exportImage", script)
        viewer = DiagramViewerDialog.viewer_html(f"{MINDMAP_V2_MARKER}\\n{{\"version\":2,\"nodes\":[{{\"id\":\"root\",\"parentId\":null,\"label\":\"论文\"}}]}}")
        self.assertIn("window.__litmtransDiagramBridge", viewer)

    def test_flowchart_long_labels_get_dynamic_rank_spacing(self):
        source = (
            f"{FLOWCHART_V2_MARKER}\n"
            '{"version":2,"layout":"LR","nodes":['
            '{"id":"first","label":"欧拉有限元与复杂分裂算法","type":"process"},'
            '{"id":"second","label":"自由场水下爆炸模拟与实验对比","type":"process"}],'
            '"edges":[{"from":"first","to":"second","label":"产生自由场对比结果"}]}'
        )
        svg = render_flowchart_svg(parse_flowchart_v2(source))
        rectangles = [
            (float(x), float(width))
            for x, width in re.findall(r'<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)"', svg)
        ]
        self.assertEqual(len(rectangles), 2)
        first_x, first_width = rectangles[0]
        second_x, _ = rectangles[1]
        self.assertGreaterEqual(second_x - (first_x + first_width), 140)

    def test_diagram_nodes_show_detail_and_preserve_evidence_actions(self):
        source = (
            f"{FLOWCHART_V2_MARKER}\n"
            '{"version":2,"nodes":[{"id":"result","type":"process","role":"result",'
            '"label":"关键结果","detail":"峰值提高 12%","evidence":[{"type":"quote","quote":"The peak increased by 12%."}]}],"edges":[]}'
        )
        svg = render_flowchart_svg(parse_flowchart_v2(source))
        self.assertIn('class="node-title"', svg)
        self.assertIn('class="node-detail"', svg)
        self.assertIn('role-result', svg)
        self.assertIn('data-node-evidence=', svg)
        self.assertIn("峰值提高 12%", svg)
        self.assertIn('font-weight:700;fill:#102a3a', svg)
        self.assertIn('font-weight:500;fill:#304b5b', svg)

    def test_diagram_priority_nodes_keep_paired_inline_dark_and_light_colours(self):
        viewer = DiagramViewerDialog.viewer_html(
            f"{MINDMAP_V2_MARKER}\\n{{\"version\":2,\"nodes\":[{{\"id\":\"root\",\"parentId\":null,\"label\":\"论文\"}}]}}"
        )
        self.assertIn('.litmtrans-diagram .node-detail { fill: #304b5b; font-weight: 500; }', viewer)
        self.assertIn('.mindmap-node.root rect, .flow-node.type-terminator > rect { fill: #1e3347;', viewer)
        self.assertIn('.mindmap-node.root text, .flow-node.type-terminator text { fill: #fff;', viewer)

        svg = render_mindmap_svg(parse_mindmap_v2(
            f"{MINDMAP_V2_MARKER}\n{{\"version\":2,\"nodes\":[{{\"id\":\"root\",\"parentId\":null,\"label\":\"论文\"}}]}}"
        ))
        self.assertIn('style="fill:#fff"', svg)
        self.assertIn('.mindmap-node.root rect,.flow-node.type-terminator>rect{fill:#1e3347;', svg)

        flow = render_flowchart_svg(parse_flowchart_v2(
            f"{FLOWCHART_V2_MARKER}\n{{\"version\":2,\"nodes\":[{{\"id\":\"start\",\"type\":\"terminator\",\"role\":\"background\",\"label\":\"开始\"}},{{\"id\":\"end\",\"type\":\"process\",\"role\":\"conclusion\",\"label\":\"结论\"}}],\"edges\":[{{\"from\":\"start\",\"to\":\"end\"}}]}}"
        ))
        self.assertEqual(flow.count('style="fill:#1e3347;stroke:#1e3347"'), 2)
        self.assertGreaterEqual(flow.count('class="node-title" style="fill:#fff"'), 2)

    def test_complete_ellipsis_and_weak_evidence_all_choose_a_sentence(self):
        text = "Introduction sentence. The wall effect decreases rapidly as the bubble moves away from the wall. Final note."
        complete = closest_evidence_sentence(text, "The wall effect decreases rapidly as the bubble moves away from the wall.")
        omitted = closest_evidence_sentence(text, "The wall effect...bubble moves away...the wall")
        weak = closest_evidence_sentence(text, "A rewritten observation with almost no literal overlap")
        self.assertEqual(text[complete[0]:complete[1]], "The wall effect decreases rapidly as the bubble moves away from the wall.")
        self.assertEqual(text[omitted[0]:omitted[1]], text[complete[0]:complete[1]])
        self.assertIsNotNone(weak)
        script = reference_focus_script({"type": "quote", "text": "aaa...bbb"})
        self.assertIn("sentenceRanges", script)
        self.assertIn("scrollIntoView({ block: 'center'", script)

    def test_native_pdf_evidence_resolver_returns_sentence_line_rects(self):
        page_text = "Preamble. The measured pressure increases by 12 percent under the test condition. Closing."
        calls = []
        selection = SimpleNamespace(
            bounds=lambda: [QPolygonF(QRectF(60, 210, 300, 18)), QPolygonF(QRectF(60, 232, 180, 18))],
            boundingRectangle=lambda: QRectF(60, 210, 300, 40),
        )
        document = SimpleNamespace(
            pageCount=lambda: 1,
            getAllText=lambda _page: SimpleNamespace(text=lambda: page_text),
            getSelectionAtIndex=lambda page, start, length: calls.append((page, start, length)) or selection,
            pagePointSize=lambda _page: QSizeF(612, 792),
        )
        view = SimpleNamespace(document=lambda: document)
        resolved = resolve_pdf_evidence_sentence(view, {"text": "measured pressure...12 percent...test condition"})
        self.assertEqual(resolved["anchor_page"], 1)
        self.assertEqual(resolved["text"], "The measured pressure increases by 12 percent under the test condition.")
        self.assertEqual(len(resolved["anchor_rects"]), 2)
        self.assertTrue(calls)

    def test_diagram_html_uses_the_preview_as_the_large_canvas_action(self):
        source = (
            f"{MINDMAP_V2_MARKER}\n"
            '{"version":2,"nodes":[{"id":"root","parentId":null,"label":"论文"}]}'
        )
        rendered = render_diagram_html(source)
        self.assertIn('class="litmtrans-diagram-preview"', rendered)
        self.assertIn('role="button"', rendered)
        self.assertNotIn("展开查看", rendered)
        self.assertNotIn("悬停节点", rendered)

    def test_python_document_diagram_prompts_match_current_semantic_density(self):
        mindmap = DOCUMENT_AI_TASKS["paper_mindmap"]
        flowchart = DOCUMENT_AI_TASKS["paper_logic_flow"]
        self.assertIn("label 应包含完整的知识认知要点", mindmap["instruction"])
        self.assertNotIn("label 极短", mindmap["instruction"])
        self.assertIn("包含完整表达语义的 label", mindmap["format"])
        self.assertIn("包含完整表达语义的 label", flowchart["format"])
        self.assertIn("Unicode", mindmap["format"])
        self.assertIn("Unicode", flowchart["format"])

    def test_all_document_diagram_tasks_require_chinese_visible_text(self):
        for task_type in ("key_points", "paper_mindmap", "paper_logic_flow"):
            instruction = DOCUMENT_AI_TASKS[task_type]["instruction"]
            self.assertIn("必须使用简体中文", instruction)
            self.assertIn("evidence 中逐字引用的 quote 必须保持论文原文", instruction)
            self.assertIn("不得因为论文原文是英文而输出整句英文", instruction)
