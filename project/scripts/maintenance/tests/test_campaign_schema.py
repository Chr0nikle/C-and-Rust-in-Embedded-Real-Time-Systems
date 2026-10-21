"""Campaign schema-v2 validation and manifest determinism."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

CAMPAIGN = Path(__file__).resolve().parents[2] / "benchmarks" / "campaign.py"
spec = importlib.util.spec_from_file_location("campaign_schema", CAMPAIGN)
campaign = importlib.util.module_from_spec(spec)
spec.loader.exec_module(campaign)


def valid_payload() -> dict:
    return {
        "schema_version": 2,
        "campaign_id": "campaign-test",
        "created": "2026-10-21T00:00:00Z",
        "complete": True,
        "repository": {
            "revision": "abc123",
            "dirty": False,
            "branch": "main",
            "status_file": None,
            "worktree_diff": None,
        },
        "software": {
            "suite_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "runs_per_combo": 1,
            "repetitions": 10,
            "warmup": 1,
        },
        "environment": {
            "toolchains": {"python": "3.14"},
            "devices_file": None,
            "preflight_file": None,
            "input_manifest": "c" * 64,
        },
        "targets": [{"rtos": "freertos", "board": "b", "language": "c"}],
        "units": [
            {
                "rtos": "freertos", "board": "b", "language": "c", "run": 0,
                "result": None, "build_id": None, "repetitions": 10,
                "warmup": 1, "ticks_per_second": 1000, "cases": [0],
                "validation_status": "missing",
            }
        ],
        "validation": {
            "status": "complete", "units_recorded": 1,
            "units_missing": 0, "units_failed": 0,
        },
    }


class CampaignSchemaTests(unittest.TestCase):
    def test_valid_record(self):
        campaign.validate_campaign_payload(valid_payload())

    def test_missing_required_field(self):
        payload = valid_payload()
        del payload["validation"]
        with self.assertRaisesRegex(ValueError, "missing required field 'validation'"):
            campaign.validate_campaign_payload(payload)

    def test_invalid_language_enum(self):
        payload = valid_payload()
        payload["units"][0]["language"] = "python"
        with self.assertRaisesRegex(ValueError, "expected one of"):
            campaign.validate_campaign_payload(payload)

    def test_invalid_hash_and_timestamp(self):
        payload = valid_payload()
        payload["software"]["suite_sha256"] = "short"
        payload["created"] = "yesterday"
        with self.assertRaises(ValueError) as caught:
            campaign.validate_campaign_payload(payload)
        message = str(caught.exception)
        self.assertIn("suite_sha256", message)
        self.assertIn("UTC date-time", message)

    def test_input_manifest_is_deterministic_and_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "benchmarks" / "build").mkdir(parents=True)
            (root / "benchmarks" / "build" / "run.json").write_text("{}")
            (root / "campaign-config.json").write_text("{}")
            first = campaign._input_manifest(root)
            second = campaign._input_manifest(root)
            self.assertEqual(first, second)
            text = (root / "provenance" / "input-manifest.sha256").read_text()
            self.assertNotIn(str(root), text)
            (root / "benchmarks" / "build" / "run.json").write_text('{"changed": true}')
            third = campaign._input_manifest(root)
            self.assertNotEqual(first, third)

    def test_toolchain_metadata_includes_project_local_embedded_gcc(self):
        import tempfile
        from pathlib import Path as _Path
        local = _Path(__file__).resolve().parents[3]
        arm = list(local.glob(".local/toolchains/arm-none-eabi/*/bin/arm-none-eabi-gcc"))
        if not arm:
            self.skipTest("project-local ARM toolchain unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            payload = campaign._toolchain_metadata(_Path(temporary))
        self.assertIn("arm_gcc", payload["toolchains"])
        self.assertIn("arm_gcc", payload["toolchain_paths"])


if __name__ == "__main__":
    unittest.main()
