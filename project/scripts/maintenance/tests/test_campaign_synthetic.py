"""End-to-end synthetic campaign validation without hardware."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CAMPAIGN = Path(__file__).resolve().parents[2] / "benchmarks" / "campaign.py"
spec = importlib.util.spec_from_file_location("campaign_synthetic", CAMPAIGN)
campaign = importlib.util.module_from_spec(spec)
spec.loader.exec_module(campaign)



class SyntheticCampaignTests(unittest.TestCase):
    def test_schema_v2_campaign_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "results" / "campaigns"
            source = results / "build-legacy"
            source.mkdir(parents=True)
            (source / "benchmark-run-1.json").write_text(json.dumps({
                "build_id": "build-legacy",
                "ticks_per_second": 1000,
                "samples": [{"case": 0, "index": 0, "ticks": 100, "variant": 0}],
            }))
            record = {
                "rtos": "freertos", "board": "stm32f4discovery", "language": "c",
                "run": 0, "result": "results/campaigns/build-legacy/benchmark-run-1.json",
                "build_id": "build-legacy", "repetitions": 1, "warmup": 0,
                "ticks_per_second": 1000, "cases": [0],
            }
            original_results = campaign.RESULTS
            campaign.RESULTS = results
            try:
                with mock.patch.object(campaign, "_git_repository_metadata", return_value={
                    "revision": "synthetic", "branch": "test", "dirty": False,
                    "status_file": None, "worktree_diff": None,
                }), mock.patch.object(campaign, "_toolchain_metadata", return_value={"toolchains": {}}), \
                     mock.patch.object(campaign, "_device_metadata", return_value=None), \
                     mock.patch.object(campaign, "_preflight_metadata", return_value=None), \
                     mock.patch.object(campaign, "_campaign_config", side_effect=lambda out, records, *args: (
                         (out / "campaign-config.json").write_text('{}\n') or "campaign-config.json"
                     )):
                    out = campaign.write_bundle(1, [record], [], [], True)
            finally:
                campaign.RESULTS = original_results

            payload = json.loads((out / "campaign.json").read_text())
            campaign.validate_campaign_payload(payload)
            self.assertEqual(payload["schema_version"], 2)
            self.assertTrue((out / "COMPLETE").is_file())
            self.assertTrue((out / "VALIDATION.json").is_file())
            self.assertTrue((out / "provenance" / "input-manifest.sha256").is_file())
            manifest = (out / "provenance" / "input-manifest.sha256").read_text()
            self.assertIn("benchmarks/build-legacy/benchmark-run-1.json", manifest)
            self.assertNotIn(str(root), manifest)



    def test_invalid_case19_samples_block_publication_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "results" / "campaigns"
            source = results / "build-invalid"
            source.mkdir(parents=True)
            (source / "benchmark-run-1.json").write_text(json.dumps({
                "build_id": "build-invalid",
                "ticks_per_second": 64000000,
                "samples": [
                    {"case": 19, "index": 0, "ticks": 100},
                    {"case": 19, "index": 1, "ticks": 0},
                ],
            }))
            record = {
                "rtos": "zephyr", "board": "nrf52840dk", "language": "c",
                "run": 0, "result": "results/campaigns/build-invalid/benchmark-run-1.json",
                "build_id": "build-invalid", "repetitions": 2, "warmup": 0,
                "ticks_per_second": 64000000, "cases": [19],
                "measurement_status": "invalid_samples",
                "sample_issues": [{
                    "case": 19, "index": 1, "variant": 0, "ticks": 0,
                    "reason": "zero_duration",
                }],
            }
            original_results = campaign.RESULTS
            campaign.RESULTS = results
            try:
                with mock.patch.object(campaign, "_git_repository_metadata", return_value={
                    "revision": "synthetic", "branch": "test", "dirty": False,
                    "status_file": None, "worktree_diff": None,
                }), mock.patch.object(campaign, "_toolchain_metadata", return_value={"toolchains": {}}), \
                     mock.patch.object(campaign, "_device_metadata", return_value=None), \
                     mock.patch.object(campaign, "_preflight_metadata", return_value=None), \
                     mock.patch.object(campaign, "_campaign_config", side_effect=lambda out, records, *args: (
                         (out / "campaign-config.json").write_text('{}\n') or "campaign-config.json"
                     )):
                    out = campaign.write_bundle(1, [record], [], [], True)
            finally:
                campaign.RESULTS = original_results
            payload = json.loads((out / "campaign.json").read_text())
            validation = json.loads((out / "VALIDATION.json").read_text())
            self.assertTrue(payload["complete"])
            self.assertEqual(payload["validation"]["status"], "complete")
            self.assertFalse(payload["validation"]["publication_ready"])
            self.assertEqual(payload["validation"]["scientific_status"], "invalid_samples")
            self.assertEqual(payload["validation"]["invalid_samples"], 1)
            self.assertEqual(validation["sample_issues"][0]["ticks"], 0)
            self.assertFalse(validation["publication_ready"])


    def test_failure_cannot_be_marked_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "results" / "campaigns"
            results.mkdir(parents=True)
            original_results = campaign.RESULTS
            campaign.RESULTS = results
            try:
                with mock.patch.object(campaign, "_git_repository_metadata", return_value={
                    "revision": "synthetic", "branch": "test", "dirty": False,
                    "status_file": None, "worktree_diff": None,
                }), mock.patch.object(campaign, "_toolchain_metadata", return_value={"toolchains": {}}), \
                     mock.patch.object(campaign, "_device_metadata", return_value=None), \
                     mock.patch.object(campaign, "_preflight_metadata", return_value=None), \
                     mock.patch.object(campaign, "_campaign_config", side_effect=lambda out, records, *args: (
                         (out / "campaign-config.json").write_text('{}\n') or "campaign-config.json"
                     )):
                    out = campaign.write_bundle(
                        1, [], [],
                        [("freertos/stm32f4discovery/c run=0", "boom", 2)],
                        True,
                    )
            finally:
                campaign.RESULTS = original_results
            payload = json.loads((out / "campaign.json").read_text())
            self.assertFalse(payload["complete"])
            self.assertEqual(payload["validation"]["status"], "incomplete")
            self.assertEqual(payload["validation"]["units_failed"], 1)
            self.assertTrue((out / "INCOMPLETE").is_file())
            self.assertFalse((out / "COMPLETE").exists())

    def test_incomplete_campaign_is_not_marked_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "results" / "campaigns"
            results.mkdir(parents=True)
            original_results = campaign.RESULTS
            campaign.RESULTS = results
            try:
                with mock.patch.object(campaign, "_git_repository_metadata", return_value={
                    "revision": "synthetic", "branch": "test", "dirty": False,
                    "status_file": None, "worktree_diff": None,
                }), mock.patch.object(campaign, "_toolchain_metadata", return_value={"toolchains": {}}), \
                     mock.patch.object(campaign, "_device_metadata", return_value=None), \
                     mock.patch.object(campaign, "_preflight_metadata", return_value=None), \
                     mock.patch.object(campaign, "_campaign_config", side_effect=lambda out, records, *args: (
                         (out / "campaign-config.json").write_text('{}\n') or "campaign-config.json"
                     )):
                    out = campaign.write_bundle(1, [], ["freertos/stm32f4discovery/c run=0"], [], False)
            finally:
                campaign.RESULTS = original_results
            payload = json.loads((out / "campaign.json").read_text())
            self.assertFalse(payload["complete"])
            self.assertEqual(payload["validation"]["status"], "incomplete")
            self.assertEqual(payload["validation"]["units_missing"], 1)
            self.assertTrue((out / "INCOMPLETE").is_file())
            self.assertFalse((out / "COMPLETE").exists())
            validation = json.loads((out / "VALIDATION.json").read_text())
            self.assertEqual(validation["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
