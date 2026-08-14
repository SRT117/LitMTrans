"""Regression tests for document-list order and startup restore settings."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import OT_common


class DocumentListPersistenceTests(unittest.TestCase):
    def test_workbench_settings_round_trip_document_state(self):
        first = str(Path("C:/papers/first/full.cleaned.md"))
        second = str(Path("C:/papers/second/full.cleaned.md"))
        settings = OT_common.AppSettings(
            document_order=[second, first],
            last_open_document=first,
        )

        with tempfile.TemporaryDirectory() as temporary_dir:
            settings_path = Path(temporary_dir) / "settings_workbench.json"
            with patch.object(OT_common, "SETTINGS_PATH", settings_path):
                OT_common.save_settings(settings)
                restored = OT_common.load_settings()

        self.assertEqual(restored.document_order, [second, first])
        self.assertEqual(restored.last_open_document, first)


if __name__ == "__main__":
    unittest.main()
