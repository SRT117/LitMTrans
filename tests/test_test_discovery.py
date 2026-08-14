"""Keep the repository's unittest-only discovery contract explicit."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


class TestDiscoveryTests(unittest.TestCase):
    def test_test_modules_do_not_contain_pytest_style_top_level_tests(self):
        tests_dir = Path(__file__).parent
        overlooked: list[str] = []
        for path in sorted(tests_dir.glob("test_*.py")):
            module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            names = [
                node.name
                for node in module.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            ]
            if names:
                overlooked.append(f"{path.name}: {', '.join(names)}")

        self.assertEqual(
            overlooked,
            [],
            "unittest discover will skip top-level test functions: " + "; ".join(overlooked),
        )
