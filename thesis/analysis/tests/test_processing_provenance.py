"""Deterministic processing identity and output deduplication."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parents[1]
REPO = ANALYSIS.parents[1]
sys.path.insert(0, str(ANALYSIS / "benchmark"))
import analysis_provenance as provenance  # noqa: E402


class ProcessingIdentityTests(unittest.TestCase):
    def test_identity_is_deterministic_and_sensitive(self):
        base = provenance.processing_identity("in", "out", "lock", "impl")
        self.assertEqual(base, provenance.processing_identity("in", "out", "lock", "impl"))
        self.assertNotEqual(base, provenance.processing_identity("in2", "out", "lock", "impl"))
        self.assertNotEqual(base, provenance.processing_identity("in", "out2", "lock", "impl"))
        self.assertNotEqual(base, provenance.processing_identity("in", "out", "lock2", "impl"))
        self.assertNotEqual(base, provenance.processing_identity("in", "out", "lock", "impl2"))

    def test_output_identity_ignores_timestamps_but_not_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            path = root / "data" / "result.json"
            path.write_text(json.dumps({"value": 1, "generated_utc": "first"}))
            original = provenance.GENERATED
            provenance.GENERATED = root
            try:
                first = provenance._stable_output_digest([path])
                path.write_text(json.dumps({"value": 1, "generated_utc": "second"}))
                self.assertEqual(first, provenance._stable_output_digest([path]))
                path.write_text(json.dumps({"value": 2, "generated_utc": "second"}))
                self.assertNotEqual(first, provenance._stable_output_digest([path]))
            finally:
                provenance.GENERATED = original

    @unittest.skipUnless(
        (REPO / "results/campaigns/campaign-replication-20261019T214853Z").is_dir(),
        "locked campaign evidence unavailable",
    )
    def test_repeated_recording_reuses_identity(self):
        first = provenance.record_analysis_provenance("test")
        second = provenance.record_analysis_provenance("test")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
