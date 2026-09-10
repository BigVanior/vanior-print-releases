from __future__ import annotations

import unittest

from ai_print_optimizer.print_priority import (
    PrintPriorityError,
    normalize_print_priority,
    settings_for_nozzle,
    settings_for_priority,
)
from ai_print_optimizer.project3mf import _apply_recommended_settings
from ai_print_optimizer.report import PrintSettings


def base_settings() -> PrintSettings:
    return PrintSettings(
        layer_height_mm=0.16,
        wall_loops=4,
        top_layers=5,
        bottom_layers=4,
        supports=True,
        brim=True,
        nozzle_temperature_c=220,
        bed_temperature_c=55,
        fan_percent=100,
    )


class PrintPriorityTests(unittest.TestCase):
    def test_priorities_change_quality_speed_and_strength_monotonically(self) -> None:
        base = base_settings()
        quality = settings_for_priority(base, "quality")
        balanced = settings_for_priority(base, "balanced")
        fast = settings_for_priority(base, "fast")

        self.assertLess(quality.layer_height_mm, balanced.layer_height_mm)
        self.assertLess(balanced.layer_height_mm, fast.layer_height_mm)
        self.assertGreater(quality.wall_loops, balanced.wall_loops)
        self.assertGreaterEqual(balanced.wall_loops, fast.wall_loops)
        self.assertGreaterEqual(quality.sparse_infill_percent, balanced.sparse_infill_percent)
        self.assertGreater(balanced.sparse_infill_percent, fast.sparse_infill_percent)
        self.assertLess(quality.outer_wall_speed_mm_s, balanced.outer_wall_speed_mm_s)
        self.assertLess(balanced.outer_wall_speed_mm_s, fast.outer_wall_speed_mm_s)
        for candidate in (quality, balanced, fast):
            self.assertEqual(candidate.supports, base.supports)
            self.assertEqual(candidate.brim, base.brim)
            self.assertEqual(candidate.nozzle_temperature_c, base.nozzle_temperature_c)
            self.assertEqual(candidate.bed_temperature_c, base.bed_temperature_c)

    def test_unknown_priority_is_rejected(self) -> None:
        with self.assertRaises(PrintPriorityError):
            settings_for_priority(base_settings(), "turbo")

    def test_multiple_priorities_are_blended_deterministically(self) -> None:
        base = base_settings()
        quality = settings_for_priority(base, "quality")
        fast = settings_for_priority(base, "fast")
        mixed = settings_for_priority(base, "fast+quality")

        self.assertEqual(normalize_print_priority("fast+quality"), "quality+fast")
        self.assertEqual(mixed.priority, "quality+fast")
        self.assertGreater(mixed.layer_height_mm, quality.layer_height_mm)
        self.assertLess(mixed.layer_height_mm, fast.layer_height_mm)
        self.assertGreater(mixed.outer_wall_speed_mm_s, quality.outer_wall_speed_mm_s)
        self.assertLess(mixed.outer_wall_speed_mm_s, fast.outer_wall_speed_mm_s)
        self.assertEqual(
            mixed,
            settings_for_priority(base, "quality+fast"),
        )

    def test_three_priorities_average_all_endpoint_values(self) -> None:
        base = base_settings()
        mixed = settings_for_priority(base, "fast+balanced+quality")
        self.assertEqual(mixed.priority, "quality+balanced+fast")
        self.assertEqual(mixed.layer_height_mm, 0.173)
        self.assertEqual(mixed.sparse_infill_percent, 17)

    def test_strength_is_explicit_and_balanced_keeps_structural_floor(self) -> None:
        base = base_settings()
        strength = settings_for_priority(base, "strength")
        balanced = settings_for_priority(base, "balanced")
        self.assertGreaterEqual(strength.wall_loops, 6)
        self.assertGreaterEqual(strength.sparse_infill_percent, 40)
        self.assertGreaterEqual(balanced.wall_loops, 4)
        self.assertGreaterEqual(balanced.sparse_infill_percent, 20)

    def test_priority_is_written_to_bambu_process_parameters(self) -> None:
        settings = {
            "curr_bed_type": "Textured PEI Plate",
            "different_settings_to_system": [""],
            "outer_wall_speed": ["200", "350"],
            "default_acceleration": ["10000", "10000"],
        }
        quality = settings_for_priority(base_settings(), "quality")
        _apply_recommended_settings(settings, quality)

        self.assertEqual(settings["layer_height"], "0.12")
        self.assertEqual(settings["sparse_infill_density"], "20%")
        self.assertEqual(settings["sparse_infill_pattern"], "gyroid")
        self.assertEqual(settings["outer_wall_speed"], ["80", "80"])
        self.assertEqual(settings["default_acceleration"], ["6000", "6000"])
        changed = set(settings["different_settings_to_system"][0].split(";"))
        self.assertIn("outer_wall_speed", changed)
        self.assertIn("sparse_infill_density", changed)

    def test_nozzle_scaling_changes_physical_line_and_layer_geometry(self) -> None:
        balanced = settings_for_priority(base_settings(), "balanced")
        small = settings_for_nozzle(balanced, 0.2)
        large = settings_for_nozzle(balanced, 0.8)

        self.assertEqual(small.layer_height_mm, 0.1)
        self.assertEqual(small.line_width_mm, 0.21)
        self.assertEqual(large.layer_height_mm, 0.4)
        self.assertEqual(large.line_width_mm, 0.84)
        self.assertGreaterEqual(large.support_object_xy_distance_mm, 0.8)
        self.assertEqual(settings_for_nozzle(balanced, 0.4), balanced)


if __name__ == "__main__":
    unittest.main()
