"""Reproducibility guard for the complexity analysis."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parents[3] / "thesis" / "analysis"
spec = importlib.util.spec_from_file_location("complexity", ANALYSIS / "integration" / "complexity.py")
complexity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(complexity)


class ComplexityTests(unittest.TestCase):
    def test_complexity_regenerates_identically(self):
        if not complexity.RCA.exists():
            self.skipTest("rust-code-analysis-cli is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            original_data = complexity.DATA
            original_typ = complexity.TYP
            complexity.DATA = Path(tmp)
            complexity.TYP = Path(tmp)
            try:
                complexity.main()
            finally:
                complexity.DATA = original_data
                complexity.TYP = original_typ
            self.assertEqual(
                (Path(tmp) / "complexity.json").read_text(),
                (original_data / "complexity.json").read_text(),
                "complexity.json is stale: regenerate with python3 analysis/integration/complexity.py",
            )
            self.assertEqual(
                (Path(tmp) / "complexity.typ").read_text(),
                (original_typ / "complexity.typ").read_text(),
                "complexity.typ is stale: regenerate with python3 analysis/integration/complexity.py",
            )


if __name__ == "__main__":
    unittest.main()
