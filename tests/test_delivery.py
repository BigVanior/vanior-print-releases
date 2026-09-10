from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from ai_print_optimizer.delivery import DeliveryError, publish_single_3mf


def _ready_project(path: Path, *, embedded_gcode: bool = True) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr("3D/3dmodel.model", "model")
        if embedded_gcode:
            archive.writestr("Metadata/plate_1.gcode", "; ready")


class DeliveryTests(unittest.TestCase):
    def test_publish_creates_exactly_one_named_3mf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "ready-to-print.3mf"
            source = root / "Svarchik.stl"
            output = root / "Svarchik-result"
            _ready_project(project)
            source.touch()

            delivered = publish_single_3mf(project, output, source)

            self.assertEqual(delivered.name, "Svarchik_готово.3mf")
            self.assertEqual(list(output.iterdir()), [delivered])
            self.assertEqual(delivered.read_bytes(), project.read_bytes())

    def test_publish_rejects_project_without_embedded_gcode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project.3mf"
            _ready_project(project, embedded_gcode=False)

            with self.assertRaises(DeliveryError):
                publish_single_3mf(project, root / "result", root / "model.stl")

            self.assertFalse((root / "result").exists())

    def test_publish_never_overwrites_existing_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project.3mf"
            output = root / "result"
            _ready_project(project)
            output.mkdir()

            with self.assertRaises(DeliveryError):
                publish_single_3mf(project, output, root / "model.stl")


if __name__ == "__main__":
    unittest.main()
