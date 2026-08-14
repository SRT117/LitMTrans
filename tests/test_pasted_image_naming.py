from datetime import datetime
import unittest

from AI_widgets import pasted_image_name


class PastedImageNamingTests(unittest.TestCase):
    def test_name_contains_local_timestamp_milliseconds_and_sequence(self):
        captured_at = datetime(2026, 8, 4, 10, 34, 55, 123000)

        self.assertEqual(
            pasted_image_name(captured_at, 2),
            "粘贴图片-20260804-103455-123-02.png",
        )

    def test_successive_images_have_distinguishable_names(self):
        captured_at = datetime(2026, 8, 4, 10, 34, 55, 123000)

        self.assertNotEqual(
            pasted_image_name(captured_at, 1),
            pasted_image_name(captured_at, 2),
        )


if __name__ == "__main__":
    unittest.main()
