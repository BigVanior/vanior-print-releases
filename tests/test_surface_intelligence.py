from __future__ import annotations

import unittest

import trimesh

from ai_print_optimizer.report import PrintSettings
from ai_print_optimizer.surface_intelligence import (
    analyze_surfaces,
    settings_for_surfaces,
)


class SurfaceIntelligenceTests(unittest.TestCase):
    def test_every_face_is_assigned_to_exactly_one_role(self) -> None:
        mesh = trimesh.creation.box(extents=(20.0, 30.0, 10.0))
        result = analyze_surfaces(mesh)

        self.assertEqual(sum(item.face_count for item in result.roles), len(mesh.faces))
        self.assertAlmostEqual(sum(item.area_ratio for item in result.roles), 1.0)
        self.assertGreater(result.ratio("top_visible"), 0.0)
        self.assertGreater(result.ratio("bed_contact"), 0.0)
        self.assertGreater(result.ratio("precision_candidate"), 0.0)

    def test_curved_model_receives_surface_detail_limits(self) -> None:
        mesh = trimesh.creation.icosphere(subdivisions=3, radius=15.0)
        intelligence = analyze_surfaces(mesh)
        base = PrintSettings(
            layer_height_mm=0.24,
            wall_loops=2,
            top_layers=4,
            bottom_layers=3,
            supports=False,
            brim=False,
            nozzle_temperature_c=220,
            bed_temperature_c=55,
            fan_percent=100,
            priority="balanced",
        )

        settings, decisions = settings_for_surfaces(base, intelligence)

        self.assertGreater(intelligence.ratio("curved_visible"), 0.25)
        self.assertLessEqual(settings.layer_height_mm, 0.16)
        self.assertLessEqual(settings.outer_wall_speed_mm_s, 110)
        self.assertEqual(settings.wall_generator, "classic")
        self.assertTrue(decisions)

    def test_top_surface_settings_are_applied_without_changing_material(self) -> None:
        intelligence = analyze_surfaces(trimesh.creation.box(extents=(30, 30, 5)))
        base = PrintSettings(
            layer_height_mm=0.20,
            wall_loops=3,
            top_layers=4,
            bottom_layers=4,
            supports=False,
            brim=False,
            nozzle_temperature_c=235,
            bed_temperature_c=70,
            fan_percent=80,
        )

        settings, _ = settings_for_surfaces(base, intelligence)

        self.assertGreaterEqual(settings.top_layers, 6)
        self.assertLessEqual(settings.top_surface_speed_mm_s, 80)
        self.assertEqual(settings.nozzle_temperature_c, 235)
        self.assertEqual(settings.bed_temperature_c, 70)


if __name__ == "__main__":
    unittest.main()
