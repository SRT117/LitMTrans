import unittest

import PB_layout


class EquationReferencePreviewTests(unittest.TestCase):
    def test_mineru_garbled_equation_references_are_normalized_for_display(self):
        source = "In Eq. \\~2! and Eqs. \\~3! and \\~4!, the result follows."
        self.assertEqual(
            PB_layout.normalize_garbled_equation_references_for_display(source),
            "In Eq. (2) and Eqs. (3) and (4), the result follows.",
        )

    def test_normalization_does_not_change_actual_tex_row_breaks(self):
        source = "$$\\n\\begin{array}{l} a \\\\ = b \\end{array}\\n$$"
        self.assertEqual(PB_layout.normalize_garbled_equation_references_for_display(source), source)
