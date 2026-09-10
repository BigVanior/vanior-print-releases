from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import trimesh

from ai_print_optimizer.simplification import SimplificationError, simplify_stl


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SimplificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_simplification_reduces_faces_and_preserves_watertight_geometry(self) -> None:
        source = self.root / "sphere.stl"
        trimesh.creation.icosphere(subdivisions=3, radius=20.0).export(source)
        source_hash = _hash(source)
        output = self.root / "sphere.simplified.stl"

        result = simplify_stl(source, output, target_faces=700)

        self.assertEqual(_hash(source), source_hash)
        self.assertTrue(output.is_file())
        self.assertEqual(result.final_status, "READY")
        self.assertLessEqual(result.triangles_after, 700)
        self.assertGreater(result.reduction_percent, 40.0)
        self.assertLessEqual(result.max_dimension_error_mm, 0.1)
        self.assertLessEqual(result.volume_error_percent, 1.0)

    def test_simplification_refuses_overwrite_and_non_reduction(self) -> None:
        source = self.root / "box.stl"
        trimesh.creation.box().export(source)
        existing = self.root / "existing.stl"
        existing.write_bytes(b"keep")

        with self.assertRaises(SimplificationError):
            simplify_stl(source, existing, target_faces=100)
        with self.assertRaises(SimplificationError):
            simplify_stl(source, self.root / "new.stl", target_faces=100)


if __name__ == "__main__":
    unittest.main()
