from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import trimesh

from ai_print_optimizer.analyzer import analyze_stl
from ai_print_optimizer.model_purpose import (
    ModelPurposeError,
    select_model_purpose,
    settings_for_model_purpose,
)
from ai_print_optimizer.print_priority import settings_for_priority


class ModelPurposeTests(unittest.TestCase):
    def test_box_is_classified_as_functional_cad_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bracket.stl"
            trimesh.creation.box(extents=(40, 30, 8)).export(path)
            report = analyze_stl(path)

        self.assertEqual(report.purpose.classification, "functional")
        self.assertEqual(report.purpose.confidence, "HIGH")
        self.assertGreater(report.purpose.axis_aligned_surface_ratio, 0.95)

    def test_organic_sphere_is_classified_as_decorative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "figure.stl"
            trimesh.creation.icosphere(subdivisions=3, radius=12).export(path)
            report = analyze_stl(path)

        self.assertEqual(report.purpose.classification, "decorative")
        self.assertGreater(report.purpose.curved_surface_ratio, 0.5)

    def test_functional_settings_prioritize_strength(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "part.stl"
            trimesh.creation.box(extents=(20, 20, 10)).export(path)
            report = analyze_stl(path)
        settings = settings_for_model_purpose(report.settings, "functional")

        self.assertGreaterEqual(settings.wall_loops, 5)
        self.assertGreaterEqual(settings.sparse_infill_percent, 30)
        self.assertEqual(settings.sparse_infill_pattern, "gyroid")
        self.assertGreaterEqual(settings.top_shell_thickness_mm, 1.2)

    def test_decorative_settings_prioritize_visible_surfaces_and_support_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "figure.stl"
            trimesh.creation.icosphere(subdivisions=2, radius=10).export(path)
            report = analyze_stl(path)
        settings = settings_for_model_purpose(report.settings, "decorative")

        self.assertEqual(settings.top_surface_pattern, "monotonicline")
        self.assertLessEqual(settings.top_surface_speed_mm_s, 80)
        self.assertGreaterEqual(settings.top_layers, 6)
        self.assertGreaterEqual(settings.support_top_z_distance_mm, 0.2)
        self.assertGreaterEqual(settings.support_object_xy_distance_mm, 0.4)

    def test_visible_surface_quality_is_identical_for_every_model_purpose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.stl"
            trimesh.creation.box(extents=(20, 20, 10)).export(path)
            base = analyze_stl(path).settings

        decorative = settings_for_model_purpose(
            base,
            "decorative",
            max_dimension_mm=30,
            curved_surface_ratio=0.8,
        )
        functional = settings_for_model_purpose(
            base,
            "functional",
            max_dimension_mm=30,
            curved_surface_ratio=0.8,
        )
        surface_fields = (
            "top_surface_pattern",
            "top_surface_line_width_mm",
            "top_surface_density_percent",
            "top_layers",
            "top_shell_thickness_mm",
            "top_surface_speed_mm_s",
            "top_surface_acceleration_mm_s2",
            "outer_wall_speed_mm_s",
            "outer_wall_acceleration_mm_s2",
            "seam_placement_away_from_overhangs",
            "support_top_z_distance_mm",
            "support_bottom_z_distance_mm",
            "support_object_xy_distance_mm",
            "support_interface_bottom_layers",
            "surface_detail_profile",
            "seam_position",
            "scarf_seam_type",
            "override_filament_scarf_seam",
            "small_perimeter_speed_percent",
            "small_perimeter_threshold_mm",
            "slow_down_layer_time_s",
            "slow_down_min_speed_mm_s",
        )
        for field in surface_fields:
            self.assertEqual(
                getattr(decorative, field),
                getattr(functional, field),
                field,
            )
        self.assertEqual(decorative.wall_generator, "classic")
        self.assertEqual(functional.wall_generator, "arachne")
        self.assertGreaterEqual(decorative.support_interface_top_layers, 4)
        self.assertLess(decorative.support_interface_spacing_mm, functional.support_interface_spacing_mm)

        self.assertLess(decorative.wall_loops, functional.wall_loops)
        self.assertLess(
            decorative.sparse_infill_percent,
            functional.sparse_infill_percent,
        )

    def test_small_curved_model_gets_photo_validated_detail_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "skull.stl"
            trimesh.creation.icosphere(subdivisions=2, radius=10).export(path)
            base = analyze_stl(path).settings

        balanced = settings_for_model_purpose(
            base,
            "decorative",
            max_dimension_mm=20,
            curved_surface_ratio=0.8,
        )

        self.assertEqual(balanced.surface_detail_profile, "small-curved")
        self.assertEqual(balanced.layer_height_mm, 0.12)
        self.assertEqual(balanced.nozzle_temperature_c, 210)
        self.assertEqual(balanced.outer_wall_speed_mm_s, 100)
        self.assertEqual(balanced.outer_wall_acceleration_mm_s2, 3_500)
        self.assertEqual(balanced.top_surface_speed_mm_s, 100)
        self.assertEqual(balanced.top_surface_acceleration_mm_s2, 2_000)
        self.assertEqual(balanced.wall_loops, 2)
        self.assertEqual(balanced.sparse_infill_percent, 10)
        self.assertEqual(balanced.small_perimeter_threshold_mm, 0.0)
        self.assertEqual(balanced.small_perimeter_speed_percent, 100)
        self.assertEqual(balanced.seam_position, "back")
        self.assertEqual(balanced.scarf_seam_type, "external")
        self.assertTrue(balanced.override_filament_scarf_seam)
        self.assertEqual(balanced.slow_down_layer_time_s, 4)
        self.assertEqual(balanced.slow_down_min_speed_mm_s, 30)

    def test_small_curved_layer_height_still_respects_user_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "figure.stl"
            trimesh.creation.icosphere(subdivisions=2, radius=10).export(path)
            base = analyze_stl(path).settings

        expected = {"quality": 0.08, "balanced": 0.12, "fast": 0.16}
        expected_detail = {
            "quality": (50, 5.0, 10, 20),
            "balanced": (100, 0.0, 4, 30),
            "fast": (100, 0.0, 3, 35),
        }
        for priority, layer_height in expected.items():
            prioritized = settings_for_priority(base, priority)
            settings = settings_for_model_purpose(
                prioritized,
                "decorative",
                max_dimension_mm=20,
                curved_surface_ratio=0.8,
            )
            self.assertEqual(settings.layer_height_mm, layer_height, priority)
            self.assertEqual(
                (
                    settings.small_perimeter_speed_percent,
                    settings.small_perimeter_threshold_mm,
                    settings.slow_down_layer_time_s,
                    settings.slow_down_min_speed_mm_s,
                ),
                expected_detail[priority],
                priority,
            )

    def test_manual_override_wins_and_invalid_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "box.stl"
            trimesh.creation.box().export(path)
            assessment = analyze_stl(path).purpose
        self.assertEqual(select_model_purpose(assessment, "decorative"), "decorative")
        with self.assertRaises(ModelPurposeError):
            select_model_purpose(assessment, "unknown")


if __name__ == "__main__":
    unittest.main()
