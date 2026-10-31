import json
from pathlib import Path
import unittest

from parabox.benchmark.evaluation.analyze import analyze

REPORT = Path(__file__).resolve().parents[2] / "report"


class PublishedSummaryTests(unittest.TestCase):
    def test_published_summaries_match_recomputed_analysis(self):
        for name in ("release", "functions", "ternary-no-reader"):
            with self.subTest(bundle=name):
                stored = json.loads((REPORT / name / "summary.json").read_text())
                self.assertEqual(analyze(REPORT / name), stored)


if __name__ == "__main__":
    unittest.main()
