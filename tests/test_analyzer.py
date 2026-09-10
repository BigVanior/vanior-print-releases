from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import trimesh

from ai_print_optimizer import (
    AnalysisError,
    analyze_stl,
    optimize_orientation,
    orient_stl,
    repair_stl,
)
from ai_print_optimizer.cli import main
from ai_print_optimizer.orientation import _score_candidate


class AnalyzerTests(unittest.TestCase):
    def export(self, mesh: trimesh.Trimesh, name: str = "model.stl") -> Path:
        path = Path(self.temp_dir.name) / name
        mesh.export(path)
        return path

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_box_metrics_and_pla_settings(self) -> None:
        path = self.export(trimesh.creation.box(extents=(20.0, 30.0, 10.0)))
        report = analyze_stl(path, material="PLA")

        np.testing.assert_allclose(report.metrics.dimensions_mm, (20.0, 30.0, 10.0))
        self.assertAlmostEqual(report.metrics.volume_mm3 or 0.0, 6000.0, places=3)
        self.assertAlmostEqual(report.metrics.surface_area_mm2, 2200.0, places=3)
        self.assertAlmostEqual(report.metrics.base_area_mm2, 600.0, places=3)
        self.assertEqual(report.metrics.body_count, 1)
        self.assertTrue(report.metrics.is_watertight)
        self.assertTrue(report.fits_build_volume)
        self.assertEqual(report.health.status, "READY")
        self.assertEqual(report.health.topology_status, "WATERTIGHT")
        self.assertEqual(report.health.boundary_edge_count, 0)
        self.assertEqual(report.health.non_manifold_edge_count, 0)
        self.assertEqual(report.material, "PLA")
        self.assertEqual(report.settings.nozzle_temperature_c, 220)

    def test_petg_analysis_starts_with_petg_specific_safe_process(self) -> None:
        path = self.export(trimesh.creation.box(extents=(20.0, 30.0, 10.0)))
        report = analyze_stl(path, material="PETG")
        settings = report.settings
        self.assertEqual(settings.nozzle_temperature_c, 255)
        self.assertEqual(settings.bed_temperature_c, 70)
        self.assertEqual(settings.fan_percent, 50)
        self.assertEqual(settings.max_volumetric_speed_mm3_s, 12.0)
        self.assertLessEqual(settings.outer_wall_speed_mm_s, 120)
        self.assertTrue(settings.reduce_crossing_wall)
        self.assertGreaterEqual(settings.support_top_z_distance_mm, 0.22)

    def test_disconnected_bodies_are_counted(self) -> None:
        left = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        right = left.copy()
        right.apply_translation((30.0, 0.0, 0.0))
        path = self.export(trimesh.util.concatenate((left, right)))

        report = analyze_stl(path)
        self.assertEqual(report.metrics.body_count, 2)
        self.assertEqual(report.health.total_body_count, 2)
        self.assertEqual(report.health.debris_body_count, 0)
        self.assertTrue(any("disconnected bodies" in item for item in report.warnings))

    def test_oversized_model_returns_cli_exit_code_two(self) -> None:
        path = self.export(trimesh.creation.box(extents=(300.0, 10.0, 10.0)))
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = main([str(path), "--material", "PETG"])
        self.assertEqual(exit_code, 2)

    def test_non_watertight_mesh_has_no_reported_volume(self) -> None:
        mesh = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        mesh.update_faces(np.arange(len(mesh.faces)) != 0)
        path = self.export(mesh)

        report = analyze_stl(path)
        self.assertFalse(report.metrics.is_watertight)
        self.assertIsNone(report.metrics.volume_mm3)
        self.assertEqual(report.health.status, "REPAIR_RECOMMENDED")
        self.assertEqual(report.health.topology_status, "OPEN")
        self.assertGreater(report.health.boundary_edge_count, 0)

    def test_tiny_fragment_is_reported_and_excluded(self) -> None:
        model = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        debris = trimesh.Trimesh(
            vertices=np.array(
                [
                    [30.0, 0.0, 0.0],
                    [30.5, 0.0, 0.0],
                    [30.0, 0.5, 0.0],
                ]
            ),
            faces=np.array([[0, 1, 2]]),
            process=False,
        )
        path = self.export(trimesh.util.concatenate((model, debris)))

        report = analyze_stl(path)
        self.assertEqual(report.health.total_body_count, 2)
        self.assertEqual(report.health.meaningful_body_count, 1)
        self.assertEqual(report.health.debris_body_count, 1)
        self.assertEqual(report.health.ignored_triangle_count, 1)
        self.assertEqual(report.health.status, "REPAIR_RECOMMENDED")
        self.assertEqual(report.health.repairability, "LIKELY_AUTOMATIC")
        self.assertEqual(report.metrics.body_count, 1)
        self.assertEqual(report.metrics.triangle_count, 12)
        self.assertTrue(report.metrics.is_watertight)
        self.assertAlmostEqual(report.metrics.volume_mm3 or 0.0, 1000.0, places=3)

    def test_high_resolution_submillimeter_fragment_is_debris(self) -> None:
        model = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        debris = trimesh.creation.icosphere(subdivisions=2, radius=0.2)
        debris.apply_translation((30.0, 0.0, 0.0))
        self.assertGreater(len(debris.faces), 20)
        path = self.export(trimesh.util.concatenate((model, debris)))

        report = analyze_stl(path)

        self.assertEqual(report.health.debris_body_count, 1)
        self.assertEqual(report.health.meaningful_body_count, 1)
        self.assertEqual(report.metrics.triangle_count, 12)

    def test_flat_surface_is_invalid_for_fdm_solid_workflow(self) -> None:
        surface = trimesh.Trimesh(
            vertices=np.array(
                [
                    [0.0, 0.0, 0.0],
                    [10.0, 0.0, 0.0],
                    [0.0, 10.0, 0.0],
                ]
            ),
            faces=np.array([[0, 1, 2]]),
            process=False,
        )
        path = self.export(surface)

        report = analyze_stl(path)
        self.assertEqual(report.health.status, "INVALID")
        self.assertEqual(report.health.repairability, "MANUAL_REVIEW")

    def test_repair_fills_triangular_hole_without_touching_source(self) -> None:
        mesh = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        mesh.update_faces(np.arange(len(mesh.faces)) != 0)
        source = self.export(mesh, "open-box.stl")
        original_bytes = source.read_bytes()
        output = Path(self.temp_dir.name) / "open-box.repaired.stl"

        result = repair_stl(source, output, max_hole_diameter_mm=20.0)
        repaired = analyze_stl(output)

        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertEqual(result.filled_holes, 1)
        self.assertEqual(result.added_triangles, 1)
        self.assertEqual(result.boundary_edges_after, 0)
        self.assertEqual(result.final_status, "READY")
        self.assertTrue(repaired.metrics.is_watertight)
        self.assertAlmostEqual(repaired.metrics.volume_mm3 or 0.0, 1000.0, places=3)

    def test_repair_removes_tiny_debris(self) -> None:
        model = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        debris = trimesh.Trimesh(
            vertices=np.array(
                [
                    [30.0, 0.0, 0.0],
                    [30.5, 0.0, 0.0],
                    [30.0, 0.5, 0.0],
                ]
            ),
            faces=np.array([[0, 1, 2]]),
            process=False,
        )
        source = self.export(trimesh.util.concatenate((model, debris)), "dirty.stl")
        output = Path(self.temp_dir.name) / "clean.stl"

        result = repair_stl(source, output)
        repaired = analyze_stl(output)

        self.assertEqual(result.removed_debris_bodies, 1)
        self.assertEqual(result.removed_triangles, 1)
        self.assertEqual(repaired.health.total_body_count, 1)
        self.assertEqual(repaired.health.status, "READY")

    def test_repair_stitches_collinear_t_junction_without_changing_shape(self) -> None:
        mesh = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        vertices = np.asarray(mesh.vertices).copy()
        faces = np.asarray(mesh.faces).copy()
        adjacent = mesh.face_adjacency[0]
        left, right = (int(value) for value in mesh.face_adjacency_edges[0])
        face_index = int(adjacent[0])
        face = [int(value) for value in faces[face_index]]
        midpoint = len(vertices)
        vertices = np.vstack((vertices, (vertices[left] + vertices[right]) / 2.0))
        replacement = None
        for index in range(3):
            first, second = face[index], face[(index + 1) % 3]
            if {first, second} == {left, right}:
                opposite = face[(index + 2) % 3]
                replacement = [[first, midpoint, opposite], [midpoint, second, opposite]]
                break
        self.assertIsNotNone(replacement)
        faces = np.vstack((np.delete(faces, face_index, axis=0), replacement))
        source = self.export(
            trimesh.Trimesh(vertices=vertices, faces=faces, process=False),
            "t-junction.stl",
        )
        output = Path(self.temp_dir.name) / "t-junction.repaired.stl"

        result = repair_stl(source, output)
        repaired = analyze_stl(output)

        self.assertEqual(result.stitched_t_junctions, 1)
        self.assertEqual(result.boundary_edges_after, 0)
        self.assertEqual(result.final_status, "READY")
        self.assertEqual(repaired.metrics.dimensions_mm, (10.0, 10.0, 10.0))
        self.assertAlmostEqual(repaired.metrics.volume_mm3 or 0.0, 1000.0, places=3)

    def test_repair_refuses_all_overwrites(self) -> None:
        source = self.export(trimesh.creation.box(), "source.stl")
        existing = self.export(trimesh.creation.box(), "existing.stl")

        with self.assertRaises(AnalysisError):
            repair_stl(source, source)
        with self.assertRaises(AnalysisError):
            repair_stl(source, existing)

    def test_repair_cli_json_contains_operation_and_analysis(self) -> None:
        mesh = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
        mesh.update_faces(np.arange(len(mesh.faces)) != 0)
        source = self.export(mesh, "cli-open-box.stl")
        output = Path(self.temp_dir.name) / "cli-repaired-box.stl"
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    str(source),
                    "--repair-output",
                    str(output),
                    "--json",
                ]
            )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["repair"]["final_status"], "READY")
        self.assertEqual(payload["analysis"]["health"]["status"], "READY")
        self.assertTrue(output.is_file())

    def test_orientation_lays_tall_box_on_its_side(self) -> None:
        source = self.export(
            trimesh.creation.box(extents=(10.0, 10.0, 100.0)),
            "tall-box.stl",
        )

        orientation = optimize_orientation(source)
        best = orientation.top_candidates[0]

        self.assertGreaterEqual(orientation.candidates_evaluated, 6)
        self.assertGreater(orientation.score_improvement, 0.0)
        self.assertAlmostEqual(best.dimensions_mm[2], 10.0, places=3)
        self.assertNotEqual(best.rotation_deg, (0.0, 0.0, 0.0))
        self.assertTrue(best.fits_build_volume)

    def test_visible_bed_contact_is_penalized_for_organic_models(self) -> None:
        dimensions = np.array([50.0, 75.0, 30.0])
        ordinary = _score_candidate(dimensions, 0.10, 0.11, 0.08, 0.05, False)
        protected = _score_candidate(dimensions, 0.10, 0.11, 0.08, 0.05, True)
        feet_only = _score_candidate(dimensions, 0.01, 0.11, 0.08, 0.001, True)

        self.assertLess(protected, ordinary - 25.0)
        self.assertGreater(feet_only, protected)

    def test_exposed_support_contact_is_penalized_for_decorative_shells(self) -> None:
        dimensions = np.array([180.0, 170.0, 80.0])
        internal_support = _score_candidate(
            dimensions, 0.02, 0.12, 0.08, 0.002, True, 0.005
        )
        facial_support = _score_candidate(
            dimensions, 0.02, 0.12, 0.08, 0.002, True, 0.030
        )

        self.assertGreater(internal_support, facial_support + 10.0)

    def test_orientation_export_is_safe_and_watertight(self) -> None:
        source = self.export(
            trimesh.creation.box(extents=(10.0, 20.0, 80.0)),
            "orientation-source.stl",
        )
        source_bytes = source.read_bytes()
        output = Path(self.temp_dir.name) / "orientation-output.stl"

        orientation, export = orient_stl(source, output)
        oriented_report = analyze_stl(output)

        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(export.score, orientation.best_score)
        self.assertTrue(oriented_report.metrics.is_watertight)
        self.assertAlmostEqual(oriented_report.metrics.dimensions_mm[2], 10.0, places=3)
        with self.assertRaises(AnalysisError):
            orient_stl(source, output)
        with self.assertRaises(AnalysisError):
            orient_stl(source, source)

    def test_orientation_cli_json_contains_ranked_candidates(self) -> None:
        source = self.export(
            trimesh.creation.box(extents=(10.0, 10.0, 100.0)),
            "orientation-cli.stl",
        )
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = main([str(source), "--orient", "--json"])
        payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(payload["orientation"]["top_candidates"]), 3)
        self.assertGreater(payload["orientation"]["score_improvement"], 0.0)
        self.assertEqual(payload["analysis"]["health"]["status"], "READY")

    def test_orientation_cli_exports_best_copy(self) -> None:
        source = self.export(
            trimesh.creation.box(extents=(10.0, 20.0, 80.0)),
            "orientation-cli-export.stl",
        )
        output = Path(self.temp_dir.name) / "orientation-cli-output.stl"
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    str(source),
                    "--orient-output",
                    str(output),
                    "--json",
                ]
            )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertTrue(output.is_file())
        self.assertEqual(
            payload["orientation_export"]["score"],
            payload["orientation"]["best_score"],
        )
        self.assertAlmostEqual(
            payload["analysis"]["metrics"]["dimensions_mm"][2],
            10.0,
            places=3,
        )

    def test_elevated_downward_surface_triggers_supports(self) -> None:
        base = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        plate = trimesh.creation.box(extents=(30.0, 30.0, 2.0))
        plate.apply_translation((0.0, 0.0, 11.0))
        path = self.export(trimesh.util.concatenate((base, plate)))

        report = analyze_stl(path)
        self.assertGreater(report.metrics.overhang_area_mm2, 0.0)
        self.assertTrue(report.settings.supports)
        self.assertIn(report.risks.support_requirement, {"MEDIUM", "HIGH"})

    def test_json_output_is_machine_readable(self) -> None:
        path = self.export(trimesh.creation.box(extents=(10.0, 10.0, 10.0)))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main([str(path), "--json"])
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["printer"], "Bambu Lab P1S")
        self.assertEqual(payload["metrics"]["dimensions_mm"], [10.0, 10.0, 10.0])
        self.assertEqual(payload["health"]["status"], "READY")

    def test_rejects_missing_and_non_stl_files(self) -> None:
        with self.assertRaises(AnalysisError):
            analyze_stl(Path(self.temp_dir.name) / "missing.stl")
        text_path = Path(self.temp_dir.name) / "model.obj"
        text_path.write_text("not an STL", encoding="utf-8")
        with self.assertRaises(AnalysisError):
            analyze_stl(text_path)


if __name__ == "__main__":
    unittest.main()
