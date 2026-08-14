import json
import subprocess
import unittest
from pathlib import Path


class LayoutGallopRegressionTests(unittest.TestCase):
    def test_galloping_search_probes_an_unvisited_final_tick(self):
        source = (Path(__file__).resolve().parents[1] / "PB_layout.py").read_text(encoding="utf-8")

        start = source.index("function gallopingGrow(start, max, step, collides)")
        end = source.index("\n  function tuneNodes", start)
        function_source = source[start:end]
        result = subprocess.run(
            [
                "node",
                "-e",
                function_source
                + "\nconst probes = [];"
                + "const result = gallopingGrow(4.8, 13, 0.35, value => {"
                + "probes.push(Number(value.toFixed(2))); return value > 10.2; });"
                + "console.log(JSON.stringify({ value: Number(result.value.toFixed(2)), probes }));",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        outcome = json.loads(result.stdout)

        self.assertEqual(outcome["value"], 10.05)
        self.assertIn(12.85, outcome["probes"])
        self.assertIn("if (firstBad === -1 && lastOk < ticks)", source)


if __name__ == "__main__":
    unittest.main()
