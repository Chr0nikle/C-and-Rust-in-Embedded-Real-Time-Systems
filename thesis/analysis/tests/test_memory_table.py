"""Appendix C matched rows must match the locked campaign's raw memory field."""
import importlib.util
import re
import unittest
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "campaign_memory", ANALYSIS / "benchmark" / "campaign.py"
)
campaign = importlib.util.module_from_spec(spec)
spec.loader.exec_module(campaign)

BOARDS = ("stm32f4", "stm32h723", "esp32c6", "nrf52840dk")


class MemoryTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = campaign.load_runs()
        cls.rows = {}
        section = None
        text = (ANALYSIS.parent / "generated" / "typ" / "campaign-memory.typ").read_text()
        for line in text.splitlines():
            marker = re.search(r"\*([^*]+)\*", line)
            if "colspan: 9" in line and marker:
                section = marker.group(1)
                continue
            cells = re.findall(r"\[([^\]]*)\]", line)
            if section and cells and len(cells) == 9:
                cls.rows[(section, cells[0])] = cells[1:]

    def _selected(self, rtos, board, lang):
        entries = self.runs[(rtos, board, lang)]

        def rank(entry):
            path, data = entry
            mem = data.get("memory_high_water") or {}
            return (
                1 if any(v is not None for v in mem.values()) else 0,
                campaign.run_ts(path),
                mem.get("per_task_ram_overhead_bytes") is not None,
                sum(1 for v in mem.values() if v is not None),
            )

        return max(entries, key=rank)[1]["memory_high_water"]

    def test_matched_benchmark_stack_rows_match_raw_evidence(self):
        checked = 0
        for rtos, section in (("freertos", "FreeRTOS"), ("nuttx", "NuttX"),
                              ("zephyr", "Zephyr")):
            for board in BOARDS:
                for lang, lang_label in (("bench", "C"), ("rust", "Rust")):
                    label = f"{campaign.BOARD_LABEL[board]} {lang_label}"
                    row = self.rows[(section, label)]
                    raw = self._selected(rtos, board, lang)["stack_high_water_bytes"]
                    self.assertEqual(int(row[5]), raw,
                                     f"{rtos}/{board}/{lang}: stale Appendix C row")
                    checked += 1
        self.assertEqual(checked, 24)

    def test_rust_native_rows_are_explicit_historical_summaries(self):
        self.assertIn(("Rust-native (historical summaries)", "FreeRTOS-in-Rust F4 Rust"),
                      self.rows)
        self.assertIn(("Rust-native (historical summaries)", "Rivet F4 Rust"),
                      self.rows)
        self.assertEqual(len([k for k in self.rows if k[0].startswith("Rust-native")]), 3)


if __name__ == "__main__":
    unittest.main()
