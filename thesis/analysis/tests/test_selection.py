"""The thesis must use the locked campaign, never the newest one."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ANALYSIS / "benchmark"))
import selection  # noqa: E402


class CampaignSelectionTests(unittest.TestCase):
    def test_lock_selects_explicit_campaign(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locked = root / "results" / "campaigns" / "campaign-old"
            newer = root / "results" / "campaigns" / "campaign-new"
            locked.mkdir(parents=True)
            newer.mkdir(parents=True)
            lock = root / "campaign.lock.json"
            lock.write_text(json.dumps({
                "schema_version": 1,
                "benchmark_campaign": "campaign-old",
                "safety_run": "safety-old",
            }))
            original_lock, original_root = selection.LOCK, selection.REPO_ROOT
            selection.LOCK, selection.REPO_ROOT = lock, root
            try:
                self.assertEqual(selection.campaign_id(), "campaign-old")
                self.assertEqual(selection.campaign_bundle(), locked.resolve())
            finally:
                selection.LOCK, selection.REPO_ROOT = original_lock, original_root

    def test_supplementary_evidence_is_explicit(self):
        lock = json.loads((ANALYSIS / "campaign.lock.json").read_text())
        entries = {entry["id"]: entry for entry in lock.get("supplementary_evidence", [])}
        entry = entries["rust-native-timing"]
        self.assertEqual(entry["status"], "missing")
        self.assertEqual(entry["campaign"], "campaign-replication-20261014T075134Z")
        self.assertTrue((ANALYSIS.parents[1] / entry["preserved_derived"]).is_file())

    def test_missing_locked_campaign_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / "campaign.lock.json"
            lock.write_text(json.dumps({
                "schema_version": 1,
                "benchmark_campaign": "absent",
                "safety_run": "safety",
            }))
            original_lock, original_root = selection.LOCK, selection.REPO_ROOT
            selection.LOCK, selection.REPO_ROOT = lock, root
            try:
                with self.assertRaises(SystemExit) as caught:
                    selection.campaign_bundle()
                self.assertIn("locked benchmark campaign not found", str(caught.exception))
            finally:
                selection.LOCK, selection.REPO_ROOT = original_lock, original_root


if __name__ == "__main__":
    unittest.main()
