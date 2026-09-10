from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import trimesh

from ai_print_optimizer.analyzer import analyze_stl
from ai_print_optimizer.optimization_protocol import (
    CandidateSliceMetrics,
    build_optimization_plan,
    evaluate_optimization_plan,
    select_independent_optimization_candidate,
)


class OptimizationProtocolTests(unittest.TestCase):
    def report(self, mesh: trimesh.Trimesh, *, purpose: str = "decorative"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "model.stl"
        mesh.export(path)
        report = analyze_stl(path)
        return replace(
            report,
            settings=replace(
                report.settings,
                priority="balanced",
                model_purpose=purpose,
                layer_height_mm=0.16,
                outer_wall_speed_mm_s=90,
                top_surface_speed_mm_s=70,
                wall_loops=5 if purpose == "functional" else 3,
                sparse_infill_percent=30 if purpose == "functional" else 15,
                top_layers=6,
            ),
        )

    def test_candidates_never_accelerate_visible_surfaces(self) -> None:
        report = self.report(trimesh.creation.icosphere(subdivisions=3, radius=20))
        plan = build_optimization_plan(report)

        self.assertGreaterEqual(len(plan.candidates), 2)
        baseline = plan.candidates[0]
        self.assertTrue(baseline.eligible)
        for candidate in plan.candidates[1:]:
            self.assertLessEqual(
                candidate.settings.outer_wall_speed_mm_s,
                baseline.settings.outer_wall_speed_mm_s,
            )
            self.assertLessEqual(
                candidate.settings.top_surface_speed_mm_s,
                baseline.settings.top_surface_speed_mm_s,
            )
            self.assertTrue(candidate.settings.detect_thin_wall)
            if candidate.eligible:
                self.assertGreaterEqual(candidate.quality_score, plan.quality_floor)
                self.assertGreaterEqual(candidate.reliability_score, plan.reliability_floor)

    def test_functional_candidates_preserve_walls_and_infill(self) -> None:
        report = self.report(trimesh.creation.box(extents=(80, 60, 40)), purpose="functional")
        plan = build_optimization_plan(report)
        baseline = plan.candidates[0].settings
        for candidate in plan.candidates:
            self.assertGreaterEqual(candidate.settings.wall_loops, baseline.wall_loops)
            self.assertGreaterEqual(
                candidate.settings.sparse_infill_percent,
                baseline.sparse_infill_percent,
            )
            self.assertFalse(candidate.settings.infill_combination)

    def test_real_metrics_choose_fastest_candidate_above_floors(self) -> None:
        plan = build_optimization_plan(self.report(trimesh.creation.box(extents=(60, 50, 30))))
        metrics = {
            candidate.identifier: CandidateSliceMetrics(
                True,
                1200.0 - index * 120.0,
                20.0 - index,
            )
            for index, candidate in enumerate(plan.candidates)
        }
        result = evaluate_optimization_plan(plan, metrics)
        eligible = [item for item in plan.candidates if item.eligible]

        self.assertEqual(result.selected_candidate, eligible[-1].identifier)
        self.assertGreater(result.time_saved_percent, 0)
        self.assertGreaterEqual(result.selected_quality_score, result.quality_floor)

    def test_slicer_warning_rejects_otherwise_fast_candidate(self) -> None:
        plan = build_optimization_plan(self.report(trimesh.creation.box(extents=(60, 50, 30))))
        metrics = {
            plan.candidates[0].identifier: CandidateSliceMetrics(True, 1000.0, 20.0),
        }
        for candidate in plan.candidates[1:]:
            metrics[candidate.identifier] = CandidateSliceMetrics(
                True,
                700.0,
                18.0,
                ("floating regions detected",),
            )
        result = evaluate_optimization_plan(plan, metrics)

        self.assertEqual(result.selected_candidate, plan.candidates[0].identifier)

    def test_manual_settings_produce_baseline_only_plan(self) -> None:
        plan = build_optimization_plan(
            self.report(trimesh.creation.box()),
            allow_setting_changes=False,
        )
        self.assertEqual(len(plan.candidates), 1)

    def test_independent_selector_respects_quality_gates(self) -> None:
        plan = build_optimization_plan(
            self.report(trimesh.creation.box(extents=(60, 50, 30)))
        )

        selected, payload = select_independent_optimization_candidate(plan)

        self.assertTrue(selected.eligible)
        self.assertGreaterEqual(selected.quality_score, plan.quality_floor)
        self.assertGreaterEqual(selected.reliability_score, plan.reliability_floor)
        self.assertEqual(payload["selected_candidate"], selected.identifier)
        self.assertEqual(len(payload["evaluations"]), len(plan.candidates))
        self.assertEqual(
            sum(bool(item["selected"]) for item in payload["evaluations"]), 1
        )

    def test_quality_combination_protects_complex_visible_surfaces(self) -> None:
        report = self.report(trimesh.creation.icosphere(subdivisions=3, radius=20))
        report = replace(
            report,
            settings=replace(report.settings, priority="quality+balanced"),
        )
        plan = build_optimization_plan(report)

        self.assertEqual(plan.protocol_version, 5)
        self.assertGreaterEqual(plan.quality_floor, 96.0)
        self.assertNotIn("minimum-safe-time", {item.identifier for item in plan.candidates})
        self.assertTrue(
            all(item.settings.layer_height_mm <= report.settings.layer_height_mm for item in plan.candidates)
        )

    def test_unsafe_baseline_can_never_be_published_as_fallback(self) -> None:
        plan = build_optimization_plan(
            self.report(trimesh.creation.box()), allow_setting_changes=False
        )
        metrics = {
            plan.candidates[0].identifier: CandidateSliceMetrics(
                True,
                1000.0,
                20.0,
                blocking_warnings=("temperature command outside profile",),
            )
        }
        with self.assertRaises(ValueError):
            evaluate_optimization_plan(plan, metrics)


if __name__ == "__main__":
    unittest.main()
