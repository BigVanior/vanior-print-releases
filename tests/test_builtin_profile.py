from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from ai_print_optimizer.builtin_profile import (
    BambuProfileRegistry,
    generate_builtin_profile,
)
from ai_print_optimizer.profile import validate_bambu_profile


class BuiltinProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.executable = self.root / "bambu-studio.exe"
        self.executable.write_bytes(b"test")
        self.profile_root = self.root / "resources" / "profiles" / "BBL"
        self.profile_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_profile(self, relative: str, payload: dict[str, object]) -> None:
        path = self.profile_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def install_minimal_profiles(self) -> None:
        self.write_profile(
            "machine/machine-base.json",
            {
                "name": "machine-base",
                "type": "machine",
                "printer_technology": "FFF",
                "nozzle_diameter": ["0.4"],
                "printable_height": "250",
                "machine_max_speed_x": ["500"],
            },
        )
        # Deliberately no name/type: official Bambu G-code fragments can be
        # referenced by their file stem alone.
        self.write_profile(
            "machine/Bambu Lab P1S 0.4 nozzle template machine_start_gcode.json",
            {"machine_start_gcode": "G28 ; inherited safety start"},
        )
        self.write_profile(
            "machine/p1s.json",
            {
                "name": "Bambu Lab P1S 0.4 nozzle",
                "type": "machine",
                "inherits": "machine-base",
                "include": [
                    "Bambu Lab P1S 0.4 nozzle template machine_start_gcode"
                ],
                "printer_model": "Bambu Lab P1S",
            },
        )
        self.write_profile(
            "process/process-base.json",
            {
                "name": "process-base",
                "type": "process",
                "layer_height": "0.2",
                "wall_loops": "2",
            },
        )
        self.write_profile(
            "process/standard.json",
            {
                "name": "0.20mm Standard @BBL X1C",
                "type": "process",
                "inherits": "process-base",
                "default_print_profile": "0.20mm Standard @BBL X1C",
            },
        )
        self.write_profile(
            "filament/pla-base.json",
            {
                "name": "Generic PLA @base",
                "type": "filament",
                "filament_type": ["PLA"],
                "filament_density": ["1.24"],
            },
        )
        self.write_profile(
            "filament/pla.json",
            {
                "name": "Generic PLA",
                "type": "filament",
                "inherits": "Generic PLA @base",
                "filament_id": "GFL99",
            },
        )

    def test_registry_resolves_untyped_include_by_file_stem(self) -> None:
        self.install_minimal_profiles()
        settings, sources = BambuProfileRegistry(self.profile_root).resolve(
            "Bambu Lab P1S 0.4 nozzle"
        )
        self.assertEqual(settings["machine_start_gcode"], "G28 ; inherited safety start")
        self.assertEqual(settings["machine_max_speed_x"], ["500"])
        self.assertEqual(len(sources), 3)

    def test_generated_profile_is_clean_complete_and_valid(self) -> None:
        self.install_minimal_profiles()
        destination = self.root / "generated.3mf"
        generated = generate_builtin_profile(
            destination,
            bambu_studio=self.executable,
            material="PLA",
        )
        validation = validate_bambu_profile(destination, expected_material="PLA")

        self.assertTrue(validation.valid, validation.errors)
        self.assertEqual(generated.printer_model, "Bambu Lab P1S")
        self.assertGreater(generated.setting_count, 15)
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "[Content_Types].xml",
                    "_rels/.rels",
                    "3D/3dmodel.model",
                    "3D/Objects/object_1.model",
                    "3D/_rels/3dmodel.model.rels",
                    "Metadata/model_settings.config",
                    "Metadata/project_settings.config",
                },
            )
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        self.assertEqual(settings["filament_colour"], ["#000000"])
        self.assertEqual(settings["machine_start_gcode"], "G28 ; inherited safety start")
        self.assertEqual(settings["machine_max_speed_x"], ["500"])


if __name__ == "__main__":
    unittest.main()
