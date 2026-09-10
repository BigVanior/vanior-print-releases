from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from ai_print_optimizer.workspace import (
    append_learning_event,
    ensure_workspace,
    import_model,
    next_project_output,
    next_ready_file,
)


class WorkspaceTests(unittest.TestCase):
    def test_managed_import_output_and_shared_learning_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = ensure_workspace(root / "VANIOR PRINT")
            self.assertEqual(
                json.loads((layout.learning / "learning-store.json").read_text(encoding="utf-8"))["scope"],
                "local-user-shared-across-versions",
            )
            external = root / "external" / "part.stl"
            external.parent.mkdir()
            external.write_bytes(b"solid test\nendsolid test\n")

            managed = import_model(external, layout)
            self.assertEqual(managed.parent, layout.uploads)
            self.assertEqual(managed.read_bytes(), b"solid test\nendsolid test\n")
            self.assertFalse(external.exists())

            project = next_project_output(managed, layout)
            self.assertEqual(project.parent, layout.ready / "Проекты")
            ready = next_ready_file("part-optimized", ".3mf", layout)
            self.assertEqual(ready.parent, layout.ready)

            append_learning_event(
                layout,
                "model_imported",
                {"sha256": "abc", "format": ".stl"},
                application_version="3.1.0",
            )
            event = json.loads(layout.learning_events.read_text(encoding="utf-8"))
            self.assertEqual(event["schema"], "vanior-learning-event-v1")
            self.assertEqual(event["event_type"], "model_imported")

    def test_same_name_with_different_content_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = ensure_workspace(root / "workspace")
            first = root / "a" / "model.3mf"
            second = root / "b" / "model.3mf"
            first.parent.mkdir(); second.parent.mkdir()
            model = b'''<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" unit="millimeter"><resources/><build/></model>'''
            with zipfile.ZipFile(first, "w") as archive:
                archive.writestr("3D/3dmodel.model", model)
                archive.writestr("marker", "first")
            with zipfile.ZipFile(second, "w") as archive:
                archive.writestr("3D/3dmodel.model", model)
                archive.writestr("marker", "second")
            first_managed = import_model(first, layout)
            second_managed = import_model(second, layout)
            self.assertNotEqual(first_managed, second_managed)
            with zipfile.ZipFile(first_managed) as archive:
                self.assertEqual(archive.read("marker"), b"first")
            with zipfile.ZipFile(second_managed) as archive:
                self.assertEqual(archive.read("marker"), b"second")
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())

    def test_public_install_keeps_source_and_writes_ready_files_to_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloads = root / "Downloads"
            downloads.mkdir()
            with mock.patch.dict(
                "os.environ", {"VANIOR_PRINT_DOWNLOADS": str(downloads)}
            ):
                layout = ensure_workspace(root / "LocalData", personal_mode=False)
            source = root / "original.stl"
            source.write_bytes(b"solid test\nendsolid test\n")

            selected = import_model(source, layout)

            self.assertEqual(selected, source.resolve())
            self.assertTrue(source.is_file())
            self.assertFalse(layout.personal_mode)
            self.assertFalse(layout.move_imports)
            self.assertEqual(layout.ready, downloads / "VANIOR PRINT")
            self.assertEqual(next_ready_file("result", ".3mf", layout).parent, layout.ready)

    def test_compound_ready_suffix_is_preserved_for_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = ensure_workspace(Path(temporary) / "workspace")
            first = next_ready_file("part-optimized", ".gcode.3mf", layout)
            first.write_bytes(b"first")

            second = next_ready_file("part-optimized", ".gcode.3mf", layout)

            self.assertEqual(first.name, "part-optimized.gcode.3mf")
            self.assertEqual(second.name, "part-optimized-2.gcode.3mf")


if __name__ == "__main__":
    unittest.main()
