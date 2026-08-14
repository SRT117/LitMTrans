import os
import unittest
from unittest.mock import patch

from PB_layout import render_flow_stream, single_column_body_promotion_enabled
from layout_single_column import (
    inherit_stable_single_column_short_items,
    infer_single_column_profile,
    promote_stable_single_column_items,
)


def page(*blocks):
    return {"page_size": [600, 800], "preproc_blocks": list(blocks)}


def text(bbox, lines=5):
    return {"type": "text", "bbox": list(bbox), "lines": [{} for _ in range(lines)]}


class SingleColumnBodyPromotionTests(unittest.TestCase):
    def test_repeated_wide_text_builds_a_profile(self):
        profile = infer_single_column_profile([
            page(text([48, 180, 552, 610]), text([48, 615, 552, 740])),
            page(text([48, 50, 552, 430]), text([48, 435, 552, 730])),
        ])
        self.assertIsNotNone(profile)
        self.assertEqual(profile.supporting_pages, frozenset({0, 1}))
        self.assertGreaterEqual(profile.supporting_blocks, 3)

    def test_one_front_page_wide_block_cannot_prove_a_profile(self):
        profile = infer_single_column_profile([
            page(text([48, 180, 552, 610])),
            page(text([60, 180, 280, 610]), text([320, 180, 540, 610])),
        ])
        self.assertIsNone(profile)

    def test_repeated_two_column_lanes_cannot_trigger_single_column_profile(self):
        profile = infer_single_column_profile([
            page(text([48, 150, 280, 610]), text([320, 150, 552, 610])),
            page(text([48, 100, 280, 700]), text([320, 100, 552, 700])),
        ])
        self.assertIsNone(profile)

    def test_only_substantial_matching_full_width_items_are_promoted(self):
        profile = infer_single_column_profile([
            page(text([48, 180, 552, 610]), text([48, 615, 552, 740])),
            page(text([48, 50, 552, 430]), text([48, 435, 552, 730])),
        ])
        flow_items = [
            {"kind": "text", "bbox": [48, 180, 552, 610], "original_line_count": 8, "debug_role": "text"},
            {"kind": "text", "bbox": [48, 110, 310, 124], "original_line_count": 1, "debug_role": "text"},
            {"kind": "text", "bbox": [48, 130, 540, 150], "original_line_count": 2, "debug_role": "text"},
        ]
        promoted = promote_stable_single_column_items(flow_items, 600, 800, profile)
        self.assertEqual(promoted[0]["debug_role"], "body_candidate")
        self.assertEqual(promoted[0]["column_key"], "single-column")
        self.assertEqual(promoted[1]["debug_role"], "text")
        self.assertEqual(promoted[2]["debug_role"], "text")

    def test_later_lane_aligned_short_text_inherits_without_becoming_anchor(self):
        profile = infer_single_column_profile([
            page(text([48, 180, 552, 610]), text([48, 615, 552, 740])),
            page(text([48, 50, 552, 430]), text([48, 435, 552, 730])),
        ])
        flow_items = [
            {"kind": "text", "bbox": [48, 430, 552, 700], "original_line_count": 7, "page_index": 3, "debug_role": "body_candidate"},
            # Page-zero author/address geometry is never recovered by this pass.
            {"kind": "text", "bbox": [48, 110, 250, 126], "original_line_count": 1, "page_index": 0, "debug_role": "text"},
            # A later derivation transition is ink-tight on the right but shares
            # the proven reading lane's left edge.
            {"kind": "text", "bbox": [48, 350, 230, 366], "original_line_count": 1, "page_index": 3, "debug_role": "text"},
            # A right-side equation number is not aligned with the reading lane.
            {"kind": "text", "bbox": [500, 350, 530, 366], "original_line_count": 1, "page_index": 3, "debug_role": "text"},
        ]
        inherited = inherit_stable_single_column_short_items(flow_items, 600, 800, profile)
        self.assertEqual(inherited[1]["debug_role"], "text")
        self.assertEqual(inherited[2]["debug_role"], "body_inherited")
        self.assertEqual(inherited[2]["column_key"], "single-column")
        self.assertEqual(inherited[3]["debug_role"], "text")

    def test_short_text_is_not_inherited_on_a_parallel_column_page(self):
        profile = infer_single_column_profile([
            page(text([48, 180, 552, 610]), text([48, 615, 552, 740])),
            page(text([48, 50, 552, 430]), text([48, 435, 552, 730])),
        ])
        flow_items = [
            # A mixed-layout page can still contain an unrelated full-width
            # block, but two parallel reading lanes veto the short-text pass.
            {"kind": "text", "bbox": [48, 50, 552, 170], "original_line_count": 4, "page_index": 3, "debug_role": "body_candidate"},
            {"kind": "text", "bbox": [48, 220, 280, 620], "original_line_count": 8, "page_index": 3, "debug_role": "body_candidate"},
            {"kind": "text", "bbox": [320, 220, 552, 620], "original_line_count": 8, "page_index": 3, "debug_role": "body_candidate"},
            {"kind": "text", "bbox": [48, 180, 230, 196], "original_line_count": 1, "page_index": 3, "debug_role": "text"},
        ]
        inherited = inherit_stable_single_column_short_items(flow_items, 600, 800, profile)
        self.assertEqual(inherited[-1]["debug_role"], "text")

    def test_inherited_short_text_uses_body_baseline_with_a_distinct_marker(self):
        item = {
            "kind": "text",
            "bbox": [48, 350, 230, 366],
            "html": "短正文",
            "plain_text": "短正文",
            "original_line_count": 1,
            "debug_role": "body_inherited",
            "indent_px": 0,
        }
        rendered = render_flow_stream(
            {"bbox": item["bbox"], "items": [item], "debug_role": "body_inherited", "page_index": 3},
            600,
            800,
            [],
            {"body_text": (12.0, 1.2)},
        )
        self.assertIn('data-style-kind="body_text"', rendered)
        self.assertIn('data-body-inherited="1"', rendered)
        self.assertIn('font-size:12.00px', rendered)

    def test_environment_switch_restores_legacy_path(self):
        with patch.dict(os.environ, {"LITMTRANS_SINGLE_COLUMN_BODY_PROMOTION": "0"}, clear=False):
            self.assertFalse(single_column_body_promotion_enabled())
        with patch.dict(os.environ, {"LITMTRANS_SINGLE_COLUMN_BODY_PROMOTION": "1"}, clear=False):
            self.assertTrue(single_column_body_promotion_enabled())
