import unittest

import trimesh

from ai_print_optimizer.functional_intent import (
    infer_functional_intent,
    settings_for_functional_intent,
)
from ai_print_optimizer.geometry_features import (
    analyze_geometry_features,
    build_local_modifier_plan,
    plan_support_exit,
)
from ai_print_optimizer.report import PrintSettings, PurposeAssessment


class GeometryFeatureTests(unittest.TestCase):
    def test_broad_thin_plate_is_detected_by_surface_intersection(self) -> None:
        mesh = trimesh.creation.box(extents=(0.3, 20.0, 20.0))
        features = analyze_geometry_features(mesh, nozzle_diameter_mm=0.4)
        self.assertTrue(features.thin_walls)
        self.assertAlmostEqual(features.minimum_estimated_wall_mm or 0.0, 0.3, places=2)
        self.assertTrue(any(item.severity == "HIGH" for item in features.thin_walls))

    def test_ranges_are_relative_non_overlapping_and_bambu_safe(self) -> None:
        mesh = trimesh.creation.box(extents=(10, 12, 20))
        features = analyze_geometry_features(mesh)
        plan = build_local_modifier_plan(
            mesh,
            features,
            priority="balanced",
            base_layer_height_mm=0.2,
            top_layers=5,
        )
        self.assertTrue(plan.ranges)
        self.assertGreaterEqual(plan.ranges[0].z_min_mm, 0)
        self.assertAlmostEqual(plan.ranges[-1].z_max_mm, 20, places=3)
        self.assertTrue(all("layer_height" in item.settings for item in plan.ranges))
        self.assertTrue(
            all(left.z_max_mm <= right.z_min_mm for left, right in zip(plan.ranges, plan.ranges[1:]))
        )
        exit_plan = plan_support_exit(mesh, features)
        self.assertEqual(exit_plan.recommended_strategy, "none")

    def test_user_functional_intent_drives_strength_settings(self) -> None:
        mesh = trimesh.creation.box(extents=(20, 20, 5))
        purpose = PurposeAssessment("functional", "HIGH", 0.8, 0.8, 0.1, 0.8, ())
        intent = infer_functional_intent(mesh, purpose, requested="gear")
        base = PrintSettings(0.2, 3, 5, 4, False, False, 220, 55, 100)
        settings, decisions = settings_for_functional_intent(base, intent)
        self.assertEqual(intent.category, "gear")
        self.assertGreaterEqual(settings.wall_loops, 6)
        self.assertGreaterEqual(settings.sparse_infill_percent, 40)
        self.assertTrue(decisions)
