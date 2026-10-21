"""Deterministic evidence archive creation and verification."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ARCHIVE = Path(__file__).resolve().parents[2] / "maintenance" / "archive_evidence.py"
spec = importlib.util.spec_from_file_location("archive_evidence", ARCHIVE)
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


class ArchiveEvidenceTests(unittest.TestCase):
    def test_create_and_verify_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "results/campaigns/c1/benchmarks/b").mkdir(parents=True)
            (root / "results/campaigns/c1/benchmarks/b/run.json").write_text("{}")
            (root / "results/safety/generated_inventory").mkdir(parents=True)
            (root / "results/safety/generated_inventory/x.csv").write_text("x\n")
            (root / "results/safety/generated_outputs").mkdir(parents=True)
            (root / "results/safety/generated_outputs/y.csv").write_text("y\n")
            (root / "thesis/generated/typ").mkdir(parents=True)
            (root / "thesis/generated/typ/t.typ").write_text("#let x = 1\n")
            lock = {
                "benchmark_campaign": "c1",
                "supplementary_evidence": [],
                "safety_evidence": {
                    "generated_inventory": "results/safety/generated_inventory",
                    "generated_outputs": "results/safety/generated_outputs",
                },
            }
            original = archive.ROOT
            archive.ROOT = root
            try:
                paths, missing = archive.collect(lock)
                files = archive.walk(paths)
                output = root / "evidence.tar.gz"
                archive.write_archive(files, output, lock, missing, dry_run=False)
                self.assertEqual(archive.verify(output), 0)
            finally:
                archive.ROOT = original


if __name__ == "__main__":
    unittest.main()
