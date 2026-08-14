import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "layout_family_probe.py"
SPEC = importlib.util.spec_from_file_location("layout_family_probe", MODULE_PATH)
probe = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def page(*boxes):
    return {
        "page_size": [600, 800],
        "preproc_blocks": [{"type": "text", "bbox": list(box)} for box in boxes],
    }


class LayoutFamilyProbeTests(unittest.TestCase):
    def test_repeated_wide_text_is_single_column(self):
        family, confidence, evidence = probe.classify_layout_pages([
            page((50, 180, 550, 650)),
            page((50, 120, 550, 700)),
            page((50, 140, 550, 690)),
        ])
        self.assertEqual(family, "single-column-layout")
        self.assertGreaterEqual(confidence, 75)
        self.assertTrue(all(item.profile == "single-column" for item in evidence))

    def test_parallel_text_boxes_are_two_column(self):
        family, _confidence, evidence = probe.classify_layout_pages([
            page((45, 160, 280, 680), (320, 160, 555, 680)),
            page((45, 120, 280, 700), (320, 120, 555, 700)),
        ])
        self.assertEqual(family, "two-column-layout")
        self.assertTrue(all(item.parallel_columns == 2 for item in evidence))

    def test_wide_and_parallel_pages_are_mixed(self):
        family, _confidence, evidence = probe.classify_layout_pages([
            page((45, 160, 555, 680)),
            page((45, 160, 280, 680), (320, 160, 555, 680)),
            page((45, 160, 555, 680)),
        ])
        self.assertEqual(family, "mixed-layout")
        self.assertEqual([item.profile for item in evidence], ["single-column", "two-columns", "single-column"])

    def test_three_parallel_boxes_are_three_column(self):
        family, _confidence, evidence = probe.classify_layout_pages([
            page((25, 160, 190, 680), (215, 160, 385, 680), (410, 160, 575, 680)),
            page((25, 120, 190, 700), (215, 120, 385, 700), (410, 120, 575, 700)),
        ])
        self.assertEqual(family, "three-or-more-column-layout")
        self.assertTrue(all(item.parallel_columns == 3 for item in evidence))
