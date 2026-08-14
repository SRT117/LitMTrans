import unittest
import threading
import time
from types import SimpleNamespace
from pathlib import Path
import sys

import machine_translate as mt

import layout_translate_preview as layout_preview
import PB_layout as layout


class EchoLocalTranslator:
    """Small deterministic stand-in for MTranServer."""

    parallelism = 1
    current_provider = mt.MTRAN_SERVER_PROVIDER

    def should_translate_text(self, text):
        return bool(str(text).strip())

    def translate_batch(self, texts, endpoint_index=0):
        return [self.translate(text) for text in texts]

    def translate(self, text):
        text = str(text)
        if "FIG." in text:
            return "资料图。1。船体梁承受 UNDEX 载荷。"
        return text.replace("Introduction", "介绍").replace("demonstrated", "证明").replace("Eq.", "Eq。")


class LifecycleLocalTranslator(EchoLocalTranslator):
    def __init__(self):
        self.lifecycle: list[str] = []

    def begin_job(self):
        self.lifecycle.append("begin")

    def end_job(self):
        self.lifecycle.append("end")


class DamagedMarkerParallelTranslator:
    """Simulates batch marker loss and records the endpoint used for recovery."""

    parallelism = 4
    current_provider = mt.MTRAN_SERVER_PROVIDER

    def __init__(self):
        self.retry_starts: list[tuple[int, float]] = []
        self.primary_thread_ids: list[int] = []
        self.retry_thread_ids: list[int] = []
        self._lock = threading.Lock()

    def should_translate_text(self, text):
        return bool(str(text).strip())

    def translate_batch(self, texts, endpoint_index=0):
        with self._lock:
            self.primary_thread_ids.append(threading.get_ident())
        time.sleep(0.03)
        return ["damaged marker response" for _text in texts]

    def translate_on_endpoint(self, text, endpoint_index=0):
        with self._lock:
            self.retry_starts.append((endpoint_index, time.perf_counter()))
            self.retry_thread_ids.append(threading.get_ident())
        time.sleep(0.12)
        return text

    def translate(self, text):
        return self.translate_on_endpoint(text)


class LocalAcademicTranslationTests(unittest.TestCase):
    def test_image_footnote_is_extracted_for_translation_and_dynamic_fit(self):
        footnote = {
            "type": "image_footnote",
            "bbox": [348, 621, 559, 645],
            "lines": [{"spans": [{"type": "text", "content": "Open Access This article is licensed."}]}],
        }
        page_info = [{
            "preproc_blocks": [{
                "type": "image",
                "bbox": [304, 621, 347, 639],
                "blocks": [footnote],
            }],
        }]

        records = layout_preview.iter_translatable_blocks(page_info)
        self.assertEqual([(record.block_type, record.text) for record in records], [
            ("image_footnote", "Open Access This article is licensed."),
        ])
        layout_preview.apply_translations(
            records,
            {records[0].block_id: "开放获取：本文采用知识共享许可。"},
        )

        rendered = layout.render_layout_generic_block(
            page_info[0]["preproc_blocks"][0], Path.cwd(), 595.0, 790.0
        )
        self.assertIn('class="layout-block type-image_footnote"', rendered)
        self.assertIn('data-block-kind="image_footnote"', rendered)
        self.assertIn('data-line-ratio="1.200"', rendered)
        self.assertIn("开放获取：本文采用知识共享许可。", rendered)
        self.assertNotIn("Open Access This article is licensed.", rendered)

        runtime = Path(layout.__file__).read_text(encoding="utf-8")
        self.assertIn("tuneCaptionGroup('.layout-block.type-image_footnote');", runtime)

    def test_damaged_marker_retries_are_parallel_and_distributed_across_servers(self):
        translator = DamagedMarkerParallelTranslator()
        items = [
            (f"p{index}", f"English expression \\(x_{index}\\) requires translation.")
            for index in range(8)
        ]

        started = time.perf_counter()
        result = mt.translate_text_items_batched(translator, items)
        elapsed = time.perf_counter() - started

        self.assertEqual(result, dict(items))
        self.assertEqual(len(translator.retry_starts), len(items))
        self.assertEqual({endpoint for endpoint, _when in translator.retry_starts}, {0, 1, 2, 3})
        self.assertTrue(set(translator.retry_thread_ids).issubset(translator.primary_thread_ids))
        # Eight synchronous retries would take about 0.96 seconds.  Four
        # workers finish them in two waves, leaving room for slower CI hosts.
        self.assertLess(elapsed, 0.70)
        # The executor is scoped to this translation call; its worker threads
        # must not stay resident after the result has been returned.
        self.assertFalse(
            any(thread.name.startswith("mtranserver") for thread in threading.enumerate())
        )

    def test_local_translator_job_is_released_after_translation(self):
        translator = LifecycleLocalTranslator()
        result = mt.translate_text_items_batched(translator, [("p", "Introduction")])
        self.assertEqual(result["p"], "介绍")
        self.assertEqual(translator.lifecycle, ["begin", "end"])

    def test_plain_text_citations_are_recovered_as_protected_superscripts(self):
        restored = mt.restore_plain_text_citation_markup(
            "Zhang 6 demonstrated the response. Rupture occurs. 5 However, loading follows, 3,4 and continues, 16 investi- gating damage."
        )
        self.assertIn("Zhang <sup>6</sup> demonstrated", restored)
        self.assertIn("<sup>5</sup> However", restored)
        self.assertIn("<sup>3,4</sup> and", restored)
        self.assertIn("<sup>16</sup> investi- gating", restored)

    def test_structural_headings_and_caption_are_normalized(self):
        self.assertEqual(mt.normalize_local_academic_translation("ABSTRACT", "抽象", "简体中文"), "摘要")
        self.assertEqual(mt.normalize_local_academic_translation("INTRODUCTION", "介绍", "简体中文"), "引言")
        self.assertEqual(mt.normalize_local_academic_translation("MATERIALS AND METHODS", "材料和方法", "简体中文"), "材料与方法")
        self.assertEqual(mt.normalize_local_academic_translation("I. INTRODUCTION", "我。介绍", "简体中文"), "I. 引言")
        self.assertEqual(
            mt.normalize_local_academic_translation(
                "FIG. 1. The hull girder subjected to UNDEX load.",
                "资料图。1。船体梁承受 UNDEX 载荷。",
                "简体中文",
            ),
            "图 1. 船体梁承受 UNDEX 载荷。",
        )

    def test_section_and_list_prefixes_are_preserved_once_with_source_punctuation(self):
        cases = {
            "IV. ANALYSIS AND DISCUSSION": ("IV. IV. 分析与讨论", "IV. 分析与讨论"),
            "A. Analysis of shock-induced afterflow": ("A. A。后压力分析", "A. 后压力分析"),
            "1. Impact of shock wave": ("1。 1. 冲击波分析", "1. 冲击波分析"),
            "1.2. Nested item": ("1.2。 1.2. 子项", "1.2. 子项"),
            "(1) First item": ("(1)。 第一项", "(1) 第一项"),
        }
        for source, (machine_output, expected) in cases.items():
            with self.subTest(source=source):
                self.assertEqual(
                    mt.normalize_local_academic_translation(source, machine_output, "简体中文"),
                    expected,
                )
                self.assertTrue(mt.split_inline_tokens(source)[0][0])

    def test_record_translation_bypasses_metadata_and_keeps_citations(self):
        records = [
            SimpleNamespace(block_id="h", text="AFFILIATIONS"),
            SimpleNamespace(block_id="f", text="FIG. 1. The hull girder subjected to UNDEX load."),
            SimpleNamespace(block_id="c", text="Zhang 6 demonstrated the response."),
        ]
        result = mt.translate_record_texts(records, "简体中文", translator=EchoLocalTranslator())
        self.assertEqual(result["h"], "作者单位")
        self.assertTrue(result["f"].startswith("图 1."))
        self.assertIn("<sup>6</sup>", result["c"])

    def test_stream_markdown_uses_layout_academic_repairs(self):
        source = (
            "## ABSTRACT\n\n"
            "## I. INTRODUCTION\n\n"
            "Yan-jie Qi (祁妍洁); Han-cheng Wang (王晗程)\n\n"
            "Zhang 6 demonstrated the response given by Eq. (2).\n\n"
            "FIG. 1. The hull girder subjected to UNDEX load.\n\n"
            "![IMAGE_001](images/image_001.jpg)\n"
        )

        result = mt.translate_markdown_document(
            source,
            "简体中文",
            translator=EchoLocalTranslator(),
        )

        self.assertIn("## 摘要", result)
        self.assertIn("## I. 引言", result)
        self.assertIn("Yan-jie Qi (祁妍洁); Han-cheng Wang (王晗程)", result)
        self.assertIn("Zhang <sup>6</sup> 证明", result)
        self.assertIn("式 (2)", result)
        self.assertIn("图 1. 船体梁承受 UNDEX 载荷。", result)
        self.assertIn("![IMAGE_001](images/image_001.jpg)", result)

    def test_layout_translation_keeps_affiliation_markers_for_local_nmt(self):
        block = {
            "lines": [
                {"spans": [{"type": "text", "content": "<sup>1</sup>State Key Laboratory, Beijing, China"}]},
                {"spans": [{"type": "text", "content": "<sup>2</sup>Chongqing Company, China"}]},
            ]
        }
        text = layout_preview.plain_block_text(block)
        self.assertIn("<sup>1</sup>", text)
        self.assertIn("<sup>2</sup>", text)
        self.assertIn("State Key", text)

    def test_layout_ai_formula_loss_is_selected_for_existing_retry_without_masking_input(self):
        record = SimpleNamespace(
            block_id="p001_b0001",
            page=1,
            block_type="text",
            text="At \\(R / R _ { 0 } = 6\\), the afterflow dominates.",
        )
        self.assertEqual(
            layout_preview.inline_formula_integrity_issue(record, "在 \\(R / R _ { 0 } = 6\\) 时，尾流占主导。"),
            "",
        )
        self.assertTrue(layout_preview.inline_formula_integrity_issue(record, "在 R / R _ { 0 } = 6 时，尾流占主导。"))
        retry = layout_preview.records_needing_retry(
            [record],
            {record.block_id: "在 R / R _ { 0 } = 6 时，尾流占主导。"},
            "简体中文",
        )
        self.assertEqual([item.block_id for item in retry], [record.block_id])

    def test_indent_uses_leftmost_fragment_of_the_first_ocr_line(self):
        bbox = [50, 100, 550, 160]
        boxes = [
            [180, 101, 300, 110],  # inline formula on the first line
            [52, 102, 170, 110],   # actual first-line start
            [51, 115, 540, 124],
            [51, 128, 540, 137],
        ]
        self.assertEqual(layout.estimate_first_line_indent(bbox, boxes), 0.0)


class LayoutFitRevisionTests(unittest.TestCase):
    def test_each_real_retranslation_gets_a_new_fit_revision(self):
        bundle = {}
        first = layout_preview.ensure_layout_fit_revision(bundle, reset=True)
        second = layout_preview.ensure_layout_fit_revision(bundle, reset=True)
        self.assertNotEqual(first, second)

    def test_runtime_only_preview_refresh_keeps_fit_revision(self):
        bundle = {}
        first = layout_preview.ensure_layout_fit_revision(bundle, reset=True)
        refreshed = layout_preview.ensure_layout_fit_revision(bundle, reset=False)
        self.assertEqual(first, refreshed)


if __name__ == "__main__":
    unittest.main()
