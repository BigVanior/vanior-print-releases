from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from ai_print_optimizer.profile import assess_bambu_profile, validate_bambu_profile


class ProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def profile(self, **overrides: object) -> Path:
        settings: dict[str, object] = {
            "printer_model": "Bambu Lab P1S",
            "printer_settings_id": "Bambu Lab P1S 0.4 nozzle",
            "printer_technology": "FFF",
            "nozzle_diameter": "0.4",
            "filament_type": ["PLA"],
            "curr_bed_type": "Textured PEI Plate",
            "printable_height": "250",
        }
        settings.update(overrides)
        path = self.root / "profile.3mf"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "Metadata/project_settings.config",
                json.dumps(settings),
            )
        return path

    def test_valid_profile_reports_active_hardware_and_material(self) -> None:
        result = validate_bambu_profile(self.profile(), expected_material="PLA")

        self.assertTrue(result.valid)
        self.assertEqual(result.printer_model, "Bambu Lab P1S")
        self.assertEqual(result.nozzle_diameter_mm, 0.4)
        self.assertEqual(result.filament_types, ("PLA",))
        self.assertEqual(result.printable_height_mm, 250.0)

    def test_profile_mismatch_is_explicit(self) -> None:
        result = validate_bambu_profile(
            self.profile(
                printer_model="Bambu Lab A1",
                nozzle_diameter="0.6",
                filament_type=["PETG"],
            ),
            expected_material="PLA",
        )

        self.assertFalse(result.valid)
        self.assertEqual(len(result.errors), 3)
        self.assertTrue(any("printer mismatch" in item for item in result.errors))
        self.assertTrue(any("nozzle mismatch" in item for item in result.errors))
        self.assertTrue(any("material mismatch" in item for item in result.errors))

    def test_source_assessment_preserves_calibrated_material_limits(self) -> None:
        result = assess_bambu_profile(
            self.profile(
                layer_height="0.20",
                line_width="0.5",
                top_surface_line_width="0.48",
                top_surface_pattern="alignedrectilinear",
                wall_generator="classic",
                filament_flow_ratio=["0.98"],
                filament_max_volumetric_speed=["12"],
                nozzle_temperature=["220"],
                outer_wall_speed="200",
                inner_wall_speed="400",
                internal_solid_infill_speed="300",
                bridge_speed="100",
                default_acceleration="15000",
                retraction_length=["0.8"],
                retraction_speed=["30"],
            )
        )

        self.assertEqual(result.guardrails["filament_flow_ratio"], 0.98)
        self.assertEqual(result.guardrails["max_volumetric_speed_mm3_s"], 12.0)
        self.assertEqual(result.guardrails["line_width_mm"], 0.5)
        self.assertEqual(result.guardrails["top_surface_line_width_mm"], 0.48)
        self.assertEqual(result.guardrails["top_surface_pattern"], "alignedrectilinear")
        self.assertEqual(result.guardrails["inner_wall_speed_mm_s"], 400)
        self.assertEqual(result.guardrails["default_acceleration_mm_s2"], 15000)
        self.assertIn("ретракта", " ".join(result.strengths))


if __name__ == "__main__":
    unittest.main()
