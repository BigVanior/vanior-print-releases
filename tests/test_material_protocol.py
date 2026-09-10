from __future__ import annotations

import unittest

from ai_print_optimizer.material_protocol import settings_for_material
from ai_print_optimizer.project3mf import _apply_recommended_settings
from ai_print_optimizer.report import PrintSettings


class MaterialProtocolTests(unittest.TestCase):
    def settings(self, **changes: object) -> PrintSettings:
        base = PrintSettings(
            layer_height_mm=0.20,
            wall_loops=3,
            top_layers=5,
            bottom_layers=4,
            supports=True,
            brim=False,
            nozzle_temperature_c=255,
            bed_temperature_c=70,
            fan_percent=50,
        )
        return __import__("dataclasses").replace(base, **changes)

    def test_petg_caps_fast_priority_without_erasing_safe_calibration(self) -> None:
        result, decisions = settings_for_material(
            self.settings(
                nozzle_temperature_c=250,
                filament_flow_ratio=0.97,
                max_volumetric_speed_mm3_s=25.0,
                outer_wall_speed_mm_s=250,
                top_surface_speed_mm_s=250,
                support_interface_speed_mm_s=100,
            ),
            "PETG",
        )
        self.assertEqual(result.nozzle_temperature_c, 250)
        self.assertEqual(result.filament_flow_ratio, 0.97)
        self.assertEqual(result.max_volumetric_speed_mm3_s, 12.0)
        self.assertLessEqual(result.outer_wall_speed_mm_s, 120)
        self.assertLessEqual(result.top_surface_speed_mm_s, 90)
        self.assertLessEqual(result.support_interface_speed_mm_s, 50)
        self.assertTrue(result.reduce_crossing_wall)
        self.assertTrue(result.wipe_enabled)
        self.assertGreaterEqual(result.support_top_z_distance_mm, 0.22)
        self.assertTrue(any("PETG" in item for item in decisions))

    def test_petg_printdna_cannot_push_temperature_or_retraction_outside_window(self) -> None:
        result, _ = settings_for_material(
            self.settings(
                nozzle_temperature_c=220,
                retraction_length_mm=1.4,
                retraction_speed_mm_s=50.0,
                wipe_distance_mm=5.0,
            ),
            "petg",
        )
        self.assertEqual(result.nozzle_temperature_c, 235)
        self.assertEqual(result.retraction_length_mm, 1.0)
        self.assertEqual(result.retraction_speed_mm_s, 35.0)
        self.assertEqual(result.wipe_distance_mm, 3.5)

    def test_pla_protocol_does_not_change_existing_good_profile(self) -> None:
        base = self.settings(
            nozzle_temperature_c=220,
            bed_temperature_c=55,
            fan_percent=100,
            max_volumetric_speed_mm3_s=12.0,
        )
        result, _ = settings_for_material(base, "PLA")
        self.assertEqual(result.nozzle_temperature_c, 220)
        self.assertEqual(result.bed_temperature_c, 55)
        self.assertEqual(result.fan_percent, 100)
        self.assertEqual(result.max_volumetric_speed_mm3_s, 12.0)

    def test_petg_protocol_is_serialized_into_real_slicer_parameters(self) -> None:
        result, _ = settings_for_material(self.settings(), "PETG")
        project = {
            "curr_bed_type": "Textured PEI Plate",
            "different_settings_to_system": [""],
            "filament_type": ["PETG"],
        }
        _apply_recommended_settings(project, result)
        self.assertEqual(project["nozzle_temperature"], ["255"])
        self.assertEqual(project["textured_plate_temp"], ["70"])
        self.assertEqual(project["filament_max_volumetric_speed"], ["12"])
        self.assertEqual(project["fan_max_speed"], ["50"])
        self.assertEqual(project["support_top_z_distance"], "0.22")
        changed = set(project["different_settings_to_system"][0].split(";"))
        self.assertTrue(
            {"retraction_length", "retraction_speed", "wipe", "wipe_distance"}
            <= changed
        )

    def test_specific_manufacturer_product_narrows_family_limits(self) -> None:
        pla, decisions = settings_for_material(
            self.settings(nozzle_temperature_c=235, max_volumetric_speed_mm3_s=21.0),
            "eSUN PLA+",
        )
        self.assertEqual(pla.max_volumetric_speed_mm3_s, 16.0)
        self.assertEqual(pla.nozzle_temperature_c, 230)
        self.assertTrue(any("eSUN PLA+" in item for item in decisions))


if __name__ == "__main__":
    unittest.main()
