"""Scope and build-file traversal guards for the integration-surface table."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "surface", ANALYSIS / "integration" / "surface.py"
)
surface = importlib.util.module_from_spec(spec)
spec.loader.exec_module(surface)


class IntegrationSurfaceScopeTests(unittest.TestCase):
    def _system(self, root: Path, name: str) -> Path:
        fw = root / "firmware" / "c-kernels" / name
        (fw / "c").mkdir(parents=True)
        (fw / "c" / "port.c").write_text("int port;\n")
        (fw / "c" / "port.h").write_text("int port;\n")
        (fw / "c" / "CMakeLists.txt").write_text("project(x)\n")
        (fw / "c" / "Makefile").write_text("all:\n")
        (fw / "c" / "Kconfig").write_text("config X\n")
        (fw / "c" / "build").mkdir()
        (fw / "c" / "build" / "generated.c").write_text("int generated;\n")
        (fw / "c" / "vendor").mkdir()
        (fw / "c" / "vendor" / "vendor.c").write_text("int vendor;\n")
        (fw / "c" / "blink").mkdir()
        (fw / "c" / "blink" / "main.c").write_text("int example;\n")
        (fw / "rust" / "src").mkdir(parents=True)
        (fw / "rust" / "src" / "lib.rs").write_text("pub fn x() {}\n")
        (fw / "rust" / "Cargo.toml").write_text("[package]\nname='x'\n")
        (fw / "rust" / "target").mkdir()
        (fw / "rust" / "target" / "ignored.rs").write_text("ignored\n")
        (fw / "rust-other" / "board" / "src").mkdir(parents=True)
        (fw / "rust-other" / "board" / "src" / "main.rs").write_text("fn main() {}\n")
        (fw / "rust-other" / "board" / "Cargo.toml").write_text("[package]\n")
        return fw

    def test_sources_and_build_files_use_explicit_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fw = self._system(root, "test")
            source_roots, build_roots = surface.adapter_roots(fw)
            names = sorted(p.name for p in surface.source_files(fw, source_roots))
            self.assertEqual(names, ["lib.rs", "main.rs", "port.c", "port.h"])
            build_names = sorted(p.name for p in surface.build_files(fw, build_roots))
            self.assertEqual(
                build_names,
                ["CMakeLists.txt", "Cargo.toml", "Cargo.toml", "Kconfig", "Makefile"],
            )

    def test_build_files_are_not_limited_to_source_suffixes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fw = self._system(root, "test")
            _, build_roots = surface.adapter_roots(fw)
            found = {p.name for p in surface.build_files(fw, build_roots)}
            self.assertTrue({"Cargo.toml", "Makefile", "CMakeLists.txt", "Kconfig"} <= found)

    def test_vendor_generated_and_example_trees_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fw = self._system(root, "test")
            source_roots, build_roots = surface.adapter_roots(fw)
            all_paths = [str(p) for p in surface.source_files(fw, source_roots)]
            self.assertFalse(any("vendor" in p or "build" in p or "blink" in p for p in all_paths))
            build_paths = [str(p) for p in surface.build_files(fw, build_roots)]
            self.assertFalse(any("vendor" in p or "target" in p or "blink" in p for p in build_paths))

    def test_system_without_project_build_files_returns_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            fw = Path(temporary) / "firmware" / "c-kernels" / "empty"
            fw.mkdir(parents=True)
            roots, builds = surface.adapter_roots(fw)
            self.assertEqual(roots, [])
            self.assertEqual(list(surface.build_files(fw, builds)), [])


if __name__ == "__main__":
    unittest.main()
