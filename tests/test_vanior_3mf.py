from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

import trimesh

from ai_print_optimizer.report import PrintSettings
from ai_print_optimizer.vanior_3mf import (
    GCODE_PATH,
    MANIFEST_PATH,
    MODEL_PATH,
    MODEL_SETTINGS_PATH,
    MODEL_SETTINGS_RELS_PATH,
    OBJECT_PATH,
    PROJECT_SETTINGS_PATH,
    SETTINGS_PATH,
    create_vanior_gcode_3mf,
    upgrade_legacy_vanior_gcode_3mf,
    verify_vanior_gcode_3mf,
)
from ai_print_optimizer.vanior_slice import slice_stl_to_gcode


def _settings() -> PrintSettings:
    return PrintSettings(
        layer_height_mm=0.2,
        wall_loops=2,
        top_layers=4,
        bottom_layers=4,
        supports=False,
        brim=True,
        nozzle_temperature_c=220,
        bed_temperature_c=55,
        fan_percent=100,
        sparse_infill_percent=15,
        outer_wall_speed_mm_s=80,
        inner_wall_speed_mm_s=120,
        sparse_infill_speed_mm_s=120,
        internal_solid_infill_speed_mm_s=100,
        initial_layer_speed_mm_s=40,
        travel_speed_mm_s=300,
        max_volumetric_speed_mm3_s=21.0,
    )


class Vanior3MFTests(unittest.TestCase):
    def test_package_contains_standard_model_verified_gcode_and_vanior_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "box.stl"
            gcode = root / "box.gcode"
            package = root / "box.gcode.3mf"
            trimesh.creation.box(extents=(12.0, 10.0, 3.0)).export(source)
            settings = _settings()
            sliced = slice_stl_to_gcode(source, gcode, settings)

            created = create_vanior_gcode_3mf(
                source,
                gcode,
                package,
                settings,
                sliced,
                material="PLA",
                color="#7D2AE8",
            )
            verification = verify_vanior_gcode_3mf(created)

            self.assertTrue(verification.valid, verification.errors)
            self.assertGreater(verification.vertex_count, 0)
            self.assertGreater(verification.triangle_count, 0)
            with zipfile.ZipFile(created) as archive:
                names = set(archive.namelist())
                self.assertIn(MODEL_PATH, names)
                self.assertIn(OBJECT_PATH, names)
                self.assertIn(GCODE_PATH, names)
                self.assertIn(SETTINGS_PATH, names)
                self.assertIn(MANIFEST_PATH, names)
                self.assertEqual(archive.read(GCODE_PATH), gcode.read_bytes())
                self.assertIn(PROJECT_SETTINGS_PATH, names)
                self.assertIn(MODEL_SETTINGS_PATH, names)
                self.assertIn(MODEL_SETTINGS_RELS_PATH, names)
                model_xml = archive.read(MODEL_PATH).decode("utf-8")
                self.assertIn("VANIOR PRINT-", model_xml)
                self.assertIn("VANIOR PRINT", model_xml)

    def test_verifier_rejects_gcode_changed_after_manifest_was_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "box.stl"
            gcode = root / "box.gcode"
            package = root / "box.gcode.3mf"
            damaged = root / "damaged.gcode.3mf"
            trimesh.creation.box(extents=(8.0, 8.0, 2.0)).export(source)
            settings = _settings()
            sliced = slice_stl_to_gcode(source, gcode, settings)
            create_vanior_gcode_3mf(
                source, gcode, package, settings, sliced, material="PLA"
            )
            with zipfile.ZipFile(package) as source_archive, zipfile.ZipFile(
                damaged, "w", compression=zipfile.ZIP_DEFLATED
            ) as target_archive:
                for info in source_archive.infolist():
                    content = source_archive.read(info.filename)
                    if info.filename == GCODE_PATH:
                        content += b"; tampered\n"
                    target_archive.writestr(info.filename, content)

            verification = verify_vanior_gcode_3mf(damaged)

            self.assertFalse(verification.valid)
            self.assertTrue(
                any("G-code" in error or "SHA-256" in error for error in verification.errors),
                verification.errors,
            )

    def test_verifier_rejects_duplicate_archive_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "box.stl"
            gcode = root / "box.gcode"
            package = root / "box.gcode.3mf"
            trimesh.creation.box(extents=(8.0, 8.0, 2.0)).export(source)
            settings = _settings()
            sliced = slice_stl_to_gcode(source, gcode, settings)
            create_vanior_gcode_3mf(
                source, gcode, package, settings, sliced, material="PLA"
            )

            with warnings.catch_warnings(), zipfile.ZipFile(package, "a") as archive:
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(SETTINGS_PATH, b"{}")

            verification = verify_vanior_gcode_3mf(package)

            self.assertFalse(verification.valid)
            self.assertTrue(
                any("duplicate" in error or "повтор" in error for error in verification.errors),
                verification.errors,
            )

    def test_legacy_package_is_upgraded_without_overwriting_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "box.stl"
            gcode = root / "box.gcode"
            current = root / "current.gcode.3mf"
            legacy = root / "legacy.gcode.3mf"
            trimesh.creation.box(extents=(10.0, 10.0, 4.0)).export(source)
            settings = _settings()
            sliced = slice_stl_to_gcode(source, gcode, settings)
            create_vanior_gcode_3mf(
                source, gcode, current, settings, sliced, material="PLA"
            )
            with zipfile.ZipFile(current) as archive:
                entries = {name: archive.read(name) for name in archive.namelist()}
            entries[MODEL_PATH] = entries[OBJECT_PATH]
            for name in (
                OBJECT_PATH,
                PROJECT_SETTINGS_PATH,
                MODEL_SETTINGS_PATH,
                MODEL_SETTINGS_RELS_PATH,
            ):
                entries.pop(name, None)
            manifest = json.loads(entries.pop(MANIFEST_PATH))
            manifest["application_version"] = "0.5.8"
            manifest["files"] = {
                name: hashlib.sha256(content).hexdigest().upper()
                for name, content in entries.items()
            }
            entries[MANIFEST_PATH] = (
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            with zipfile.ZipFile(legacy, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, content in entries.items():
                    archive.writestr(name, content)
            source_hash = hashlib.sha256(legacy.read_bytes()).hexdigest()

            upgraded = upgrade_legacy_vanior_gcode_3mf(legacy)

            self.assertNotEqual(upgraded, legacy)
            self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(), source_hash)
            self.assertTrue(verify_vanior_gcode_3mf(upgraded).valid)


if __name__ == "__main__":
    unittest.main()
