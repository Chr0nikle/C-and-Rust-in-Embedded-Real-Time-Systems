"""Every generated thesis output must have an explicit evidence entry."""
import hashlib
import json
import re
import unittest
from pathlib import Path

import sys
ANALYSIS = Path(__file__).resolve().parents[1]
THESIS = ANALYSIS.parent
REPO = THESIS.parent
sys.path.insert(0, str(ANALYSIS / "benchmark"))
import selection  # noqa: E402

GENERATED = THESIS / "generated"
MAP = ANALYSIS / "evidence-map.json"
STATUSES = {
    "reproducible",
    "reproducible_with_supplement",
    "reproducible_from_source",
    "historical_preserved",
    "mixed_historical",
    "requires_local_safety_evidence",
}


def _manifest(path: Path) -> str:
    lines = []
    for item in sorted(path.rglob("*")):
        if item.is_file():
            lines.append(f"{hashlib.sha256(item.read_bytes()).hexdigest()}  {item.relative_to(REPO).as_posix()}")
    return hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


class EvidenceTraceabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.map = json.loads(MAP.read_text())
        cls.outputs = cls.map["outputs"]

    def test_every_generated_typ_and_figure_is_mapped(self):
        generated = {
            path.relative_to(THESIS).as_posix()
            for pattern in ("generated/typ/*.typ", "generated/figures/*.svg")
            for path in THESIS.glob(pattern)
        }
        self.assertEqual(generated, set(self.outputs))

    def test_mapped_outputs_and_generators_exist(self):
        for output, record in self.outputs.items():
            self.assertTrue((THESIS / output).is_file(), output)
            self.assertTrue((REPO / record["generator"]).is_file(), record["generator"])
            self.assertIn(record["status"], STATUSES, output)
            self.assertTrue(record["evidence"], output)

    def test_all_generated_includes_and_images_resolve(self):
        pattern = re.compile(r'(?:#include\s+|image\()"([^"]+)"')
        for section in sorted((THESIS / "sections").glob("*.typ")):
            text = section.read_text()
            for relative in pattern.findall(text):
                target = (section.parent / relative).resolve()
                if "/generated/" not in target.as_posix():
                    continue
                self.assertTrue(target.is_file(), f"{section}: missing {relative}")
                mapped = target.relative_to(THESIS).as_posix()
                self.assertIn(mapped, self.outputs, f"{section}: unmapped {relative}")

    def test_campaign_lock_matches_map(self):
        lock = json.loads((ANALYSIS / "campaign.lock.json").read_text())
        self.assertEqual(lock["benchmark_campaign"], self.map["selected_campaign"])
        self.assertIn("safety_evidence", lock)
        self.assertIn("supplementary_evidence", lock)
        self.assertTrue((ANALYSIS.parent / "generated" / "data" / "static-evidence.json").is_file())

    def test_statuses_do_not_overclaim_reproducibility(self):
        lock = json.loads((ANALYSIS / "campaign.lock.json").read_text())
        campaign = lock["benchmark_campaign"]
        for output, record in self.outputs.items():
            evidence = " ".join(str(item) for item in record["evidence"])
            status = record["status"]
            if status == "reproducible":
                self.assertIn(campaign, evidence, output)
            elif status == "reproducible_with_supplement":
                self.assertIn("supplementary", evidence, output)
                self.assertTrue(lock.get("supplementary_evidence"), output)
            elif status == "requires_local_safety_evidence":
                self.assertIn("safety_evidence", evidence, output)
                self.assertTrue(lock.get("safety_evidence"), output)
            elif status in {"historical_preserved", "mixed_historical"}:
                self.assertTrue(
                    "historical" in evidence or "preserved" in evidence,
                    output,
                )

    def test_safety_evidence_hashes(self):
        lock = json.loads((ANALYSIS / "campaign.lock.json").read_text())
        evidence = lock["safety_evidence"]
        inventory = REPO / evidence["generated_inventory"]
        outputs = REPO / evidence["generated_outputs"]
        if not inventory.is_dir() or not outputs.is_dir():
            self.skipTest("local safety evidence is not present")
        self.assertEqual(_manifest(inventory), evidence["generated_inventory_sha256"])
        self.assertEqual(_manifest(outputs), evidence["generated_outputs_sha256"])
        profiles = REPO / evidence.get("profiles", "")
        if profiles.is_dir():
            self.assertEqual(_manifest(profiles), evidence["profiles_sha256"])


if __name__ == "__main__":
    unittest.main()
