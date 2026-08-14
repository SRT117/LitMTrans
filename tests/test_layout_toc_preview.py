import unittest

import PB_layout


class LayoutContentsPreviewTests(unittest.TestCase):
    def test_contents_text_block_gets_a_dedicated_row_grid(self):
        contents = "\n".join(
            [
                "1. Application Overview ..... 15",
                "1.1. Autodyn Application Layout ..... 16",
                "1.2. Autodyn Toolbar ..... 16",
                "1.3. Navigation Bar ..... 18",
                "2. Pull Down Menus ..... 45",
                "2.1. Pull-Down Menu - File ..... 46",
                "2.1.1. Pull-Down Menu - File - New ..... 48",
            ]
        )
        page = {
            "page_idx": 0,
            "preproc_blocks": [
                {
                    "type": "text",
                    "bbox": [52, 79, 561, 240],
                    "lines": [{"spans": [{"type": "text", "content": contents}]}],
                }
            ],
        }

        flow_items, absolute_blocks = PB_layout.streamable_layout_items(page, 612.0, 792.0)
        streams = PB_layout.group_flow_streams(flow_items, absolute_blocks, 612.0)
        rendered = PB_layout.render_flow_stream(streams[0], 612.0, 792.0, [])

        self.assertEqual(flow_items[0]["debug_role"], "toc")
        self.assertEqual(flow_items[0]["original_line_count"], 7)
        self.assertIn('class="toc-row toc-level-1"', rendered)
        self.assertIn('class="toc-leader"', rendered)
        self.assertIn('<span class="toc-page">48</span>', rendered)
        self.assertIn('data-toc="1"', rendered)

    def test_ordinary_numbered_list_is_not_misclassified_as_contents(self):
        rows = PB_layout.parse_toc_rows(
            [{"spans": [{"type": "text", "content": "1. First step\n2. Second step\n3. Third step"}]}]
        )

        self.assertIsNone(rows)

    def test_embedded_keyword_rows_count_as_visual_lines(self):
        block = {
            "type": "text",
            "lines": [{"spans": [{"type": "text", "content": (
                "Keywords:\nUnderwater explosion\nShock wave\nBubble motion\n"
                "Eulerian finite element formulation\nContinuous simulation"
            )}]}],
            "_layout_original_line_count": 1,
        }

        self.assertEqual(PB_layout.layout_visual_line_count(block["lines"]), 6)
        self.assertEqual(PB_layout.layout_original_line_count(block), 6)
        self.assertEqual(
            PB_layout.layout_original_line_count({"lines": [{"spans": [{"type": "text", "content": "Abstract"}]}]}),
            1,
        )
