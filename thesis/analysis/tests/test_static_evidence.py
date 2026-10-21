"""Mixed historical/current static evidence must stay visible."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ANALYSIS / "benchmark"))
import analyze  # noqa: E402


class StaticEvidenceTests(unittest.TestCase):
    def test_mixed_status_suppresses_memory_regeneration(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp)
            (generated / "data").mkdir()
            (generated / "data" / "static-evidence.json").write_text(
                json.dumps({"status": "mixed-historical"})
            )
            original = analyze.GENERATED
            analyze.GENERATED = generated
            try:
                self.assertFalse(analyze._has_native_static())
            finally:
                analyze.GENERATED = original

    def test_complete_status_allows_memory_regeneration(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp)
            (generated / "data").mkdir()
            (generated / "data" / "static-evidence.json").write_text(
                json.dumps({"status": "complete"})
            )
            original = analyze.GENERATED
            analyze.GENERATED = generated
            try:
                self.assertTrue(analyze._has_native_static())
            finally:
                analyze.GENERATED = original

    def test_published_mixed_table_has_evidence(self):
        evidence = ANALYSIS.parent / "generated" / "data" / "static-evidence.json"
        payload = json.loads(evidence.read_text())
        self.assertEqual(payload["status"], "mixed-historical")
        self.assertEqual(
            payload["preserved_historical_keys"],
            [
                "freertos-in-rust/stm32f4/rust",
                "freertos-in-rust/stm32h723/rust",
                "rivet/stm32f4/rust",
            ],
        )
        self.assertTrue(payload["preserved_historical_rows_from_previous_table"])


if __name__ == "__main__":
    unittest.main()
