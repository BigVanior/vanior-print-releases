from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import trimesh

from ai_print_optimizer.pipeline import (
    PipelineError,
    _configure_analysis_for_print,
    _safe_for_slicer_recovery,
    _source_profile_baseline_settings,
    run_pipeline,
    verify_manifest,
)
from ai_print_optimizer.profile import SourceProfileAssessment
from ai_print_optimizer.slicer import SliceRunResult, SupportComparison


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def profile(self, **overrides: object) -> Path:
        path = self.root / "profile.3mf"
        settings = {
            "printer_model": "Bambu Lab P1S",
            "printer_settings_id": "Bambu Lab P1S 0.4 nozzle",
            "printer_technology": "FFF",
            "nozzle_diameter": "0.4",
            "filament_type": ["PLA"],
            "curr_bed_type": "Textured PEI Plate",
            "printable_height": "250",
        }
        settings.update(overrides)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "Metadata/project_settings.config",
                json.dumps(settings),
            )
        return path

    def test_small_sparse_mesh_defects_are_allowed_to_reach_verified_slicing(self) -> None:
        source = self.root / "minor-seam.stl"
        mesh = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
        mesh.update_faces(list(range(len(mesh.faces) - 1)))
        mesh.export(source)
        report = __import__("ai_print_optimizer").analyze_stl(source)
        self.assertEqual(report.health.status, "REPAIR_RECOMMENDED")
        self.assertTrue(_safe_for_slicer_recovery(report))

    def test_broad_mesh_damage_still_requires_manual_repair(self) -> None:
        source = self.root / "broad-damage.stl"
        mesh = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
        mesh.update_faces(list(range(0, len(mesh.faces), 2)))
        mesh.export(source)
        report = __import__("ai_print_optimizer").analyze_stl(source)
        self.assertFalse(_safe_for_slicer_recovery(report))

    def test_source_3mf_calibration_is_a_guardrail_not_replaced(self) -> None:
        source = self.root / "organic.stl"
        trimesh.creation.icosphere(subdivisions=3, radius=20).export(source)
        report = __import__("ai_print_optimizer").analyze_stl(source)
        assessment = SourceProfileAssessment(
            setting_count=400,
            quality_score=84.0,
            strengths=("calibrated",),
            weaknesses=(),
            guardrails={
                "filament_flow_ratio": 0.98,
                "max_volumetric_speed_mm3_s": 12.0,
                "nozzle_temperature_c": 220,
                "outer_wall_speed_mm_s": 200,
                "outer_wall_acceleration_mm_s2": 5000,
                "retraction_length_mm": 0.8,
                "retraction_speed_mm_s": 30.0,
                "wipe_enabled": "1",
                "wipe_distance_mm": 2.0,
            },
        )
        configured, _ = _configure_analysis_for_print(
            report,
            print_priority="quality+balanced",
            model_purpose="decorative",
            functional_intent="auto",
            print_setting_overrides=None,
            print_dna_profile=None,
            source_profile_assessment=assessment,
        )

        self.assertEqual(configured.settings.filament_flow_ratio, 0.98)
        self.assertEqual(configured.settings.max_volumetric_speed_mm3_s, 12.0)
        self.assertEqual(configured.settings.wall_generator, "classic")
        self.assertLessEqual(configured.settings.layer_height_mm, 0.16)
        self.assertLessEqual(configured.settings.outer_wall_speed_mm_s, 120)

    def test_petg_source_calibration_is_preserved_then_safely_capped(self) -> None:
        source = self.root / "petg-organic.stl"
        trimesh.creation.icosphere(subdivisions=2, radius=20).export(source)
        report = __import__("ai_print_optimizer").analyze_stl(source, material="PETG")
        assessment = SourceProfileAssessment(
            setting_count=420,
            quality_score=82.0,
            strengths=("calibrated",),
            weaknesses=(),
            guardrails={
                "filament_flow_ratio": 0.97,
                "max_volumetric_speed_mm3_s": 18.0,
                "nozzle_temperature_c": 250,
                "bed_temperature_c": 75,
                "fan_percent": 45,
                "retraction_length_mm": 0.9,
                "retraction_speed_mm_s": 32.0,
                "wipe_enabled": "1",
                "wipe_distance_mm": 2.5,
            },
        )
        configured, _ = _configure_analysis_for_print(
            report,
            print_priority="fast",
            model_purpose="decorative",
            functional_intent="auto",
            print_setting_overrides=None,
            print_dna_profile=None,
            source_profile_assessment=assessment,
        )
        settings = configured.settings
        self.assertEqual(settings.nozzle_temperature_c, 250)
        self.assertEqual(settings.bed_temperature_c, 75)
        self.assertEqual(settings.fan_percent, 45)
        self.assertEqual(settings.filament_flow_ratio, 0.97)
        self.assertEqual(settings.max_volumetric_speed_mm3_s, 12.0)
        self.assertLessEqual(settings.outer_wall_speed_mm_s, 120)
        self.assertLessEqual(settings.support_interface_speed_mm_s, 50)
        self.assertTrue(settings.reduce_crossing_wall)

    def test_mixed_priority_keeps_source_3mf_time_budget(self) -> None:
        source = self.root / "benchy-like.stl"
        trimesh.creation.icosphere(subdivisions=2, radius=35).export(source)
        report = __import__("ai_print_optimizer").analyze_stl(source, material="PETG")
        assessment = SourceProfileAssessment(
            setting_count=420,
            quality_score=82.0,
            strengths=("author baseline",),
            weaknesses=(),
            guardrails={
                "layer_height_mm": 0.25,
                "wall_loops": 2,
                "top_layers": 6,
                "bottom_layers": 4,
                "sparse_infill_percent": 10,
                "outer_wall_speed_mm_s": 200,
                "top_surface_speed_mm_s": 200,
                "max_volumetric_speed_mm3_s": 9.0,
                "nozzle_temperature_c": 255,
                "fan_percent": 40,
            },
        )
        configured, _ = _configure_analysis_for_print(
            report,
            print_priority="quality+balanced",
            model_purpose="decorative",
            functional_intent="auto",
            print_setting_overrides=None,
            print_dna_profile=None,
            source_profile_assessment=assessment,
        )
        settings = configured.settings
        # Physical PETG evidence showed that a 0.04 mm layer reduction plus an
        # extra wall doubled time and increased ooze. Mixed mode may move only
        # one small step and keeps the proven shell count.
        self.assertGreaterEqual(settings.layer_height_mm, 0.23)
        self.assertLessEqual(settings.layer_height_mm, 0.25)
        self.assertEqual(settings.wall_loops, 2)
        self.assertEqual(settings.sparse_infill_percent, 10)

    def test_source_profile_baseline_recreates_author_process_on_safe_printer(self) -> None:
        source = self.root / "benchy-baseline.stl"
        trimesh.creation.box(extents=(60.0, 30.0, 40.0)).export(source)
        report = __import__("ai_print_optimizer").analyze_stl(source, material="PETG")
        assessment = SourceProfileAssessment(
            setting_count=420,
            quality_score=82.0,
            strengths=("author baseline",),
            weaknesses=(),
            guardrails={
                "layer_height_mm": 0.25,
                "line_width_mm": 0.5,
                "top_surface_line_width_mm": 0.5,
                "top_surface_pattern": "alignedrectilinear",
                "wall_loops": 2,
                "top_layers": 6,
                "bottom_layers": 4,
                "sparse_infill_percent": 10,
                "outer_wall_speed_mm_s": 200,
                "top_surface_speed_mm_s": 200,
                "nozzle_temperature_c": 255,
                "filament_flow_ratio": 0.94,
                "max_volumetric_speed_mm3_s": 9.0,
                "retraction_length_mm": 0.8,
                "retraction_speed_mm_s": 30.0,
                "wipe_distance_mm": 2.0,
            },
        )
        configured, _ = _configure_analysis_for_print(
            report,
            print_priority="quality+balanced",
            model_purpose="decorative",
            functional_intent="auto",
            print_setting_overrides=None,
            print_dna_profile=None,
            source_profile_assessment=assessment,
        )

        baseline = _source_profile_baseline_settings(configured.settings, assessment)

        self.assertEqual(baseline.layer_height_mm, 0.25)
        self.assertEqual(baseline.line_width_mm, 0.5)
        self.assertEqual(baseline.top_surface_line_width_mm, 0.5)
        self.assertEqual(baseline.top_surface_pattern, "alignedrectilinear")
        self.assertEqual(baseline.wall_loops, 2)
        self.assertEqual(baseline.top_layers, 6)
        self.assertEqual(baseline.outer_wall_speed_mm_s, 200)
        self.assertEqual(baseline.nozzle_temperature_c, 255)
        self.assertEqual(baseline.filament_flow_ratio, 0.94)
        self.assertEqual(baseline.retraction_length_mm, 0.8)

    def test_pipeline_writes_hashed_manifest_without_touching_inputs(self) -> None:
        source = self.root / "box.stl"
        trimesh.creation.box(extents=(20.0, 30.0, 10.0)).export(source)
        template = self.profile()
        fake_slicer = self.root / "bambu-studio.exe"
        fake_slicer.write_bytes(b"fake slicer")
        source_hash = _hash(source)
        template_hash = _hash(template)
        output = self.root / "pipeline"

        def fake_run(
            stl: Path,
            destination: Path,
            strategy: str,
        ) -> SliceRunResult:
            destination = Path(destination)
            destination.mkdir(parents=True)
            gcode = destination / "ready-to-print.gcode"
            gcode.write_text("; FEATURE: Outer wall\nG1 X1 Y1 E1\n", encoding="utf-8")
            ready = destination / "ready-to-print.3mf"
            ready.write_bytes(b"verified ready project")
            (destination / "result.json").write_text("{}", encoding="utf-8")
            return SliceRunResult(
                source_path=Path(stl),
                output_dir=destination,
                slicer_path=fake_slicer,
                return_code=0,
                error_string="Success.",
                success=True,
                layer_height_mm=0.2,
                wall_loops=2,
                sparse_infill_percent=10.0,
                total_print_time_s=60.0,
                total_used_g=1.0,
                total_estimated_length_m=0.33,
                total_estimated_cost=0.02,
                plates=(),
                gcode_files=(gcode,),
                gcode_roles=(),
                support_estimated_length_m=0.0,
                support_estimated_mass_g=0.0,
                metric_limitations=(),
                ready_project_path=ready,
            )

        def fake_compare(stl: Path, profile: Path, destination: Path, **_: object):
            destination = Path(destination)
            destination.mkdir()
            none = fake_run(Path(stl), destination / "none", "none")
            normal = fake_run(Path(stl), destination / "normal", "normal")
            tree = fake_run(Path(stl), destination / "tree", "tree")
            ready_project = destination / "ready-to-print.3mf"
            ready_gcode = destination / "ready-to-print.gcode"
            ready_project.write_bytes(none.ready_project_path.read_bytes())  # type: ignore[union-attr]
            ready_gcode.write_bytes(none.gcode_files[0].read_bytes())
            return SupportComparison(
                source_path=Path(stl),
                output_dir=destination,
                source_sha256=_hash(Path(stl)),
                profile_template_path=Path(profile),
                profile_template_sha256=_hash(Path(profile)),
                none=none,
                normal=normal,
                tree=tree,
                recommended="none",
                recommendation_reason="test",
                ready_project_path=ready_project,
                ready_gcode_path=ready_gcode,
            )

        with mock.patch(
            "ai_print_optimizer.pipeline.compare_stl_supports",
            side_effect=fake_compare,
        ):
            result = run_pipeline(source, template, output)

        self.assertEqual(result.mode, "print-strategy-comparison")
        self.assertEqual(_hash(source), source_hash)
        self.assertEqual(_hash(template), template_hash)
        self.assertTrue(result.orientation_export.output_path.is_file())
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["application"]["version"], "0.6.6")
        self.assertTrue(manifest["stages"]["profile_validation"]["valid"])
        self.assertEqual(manifest["inputs"][0]["sha256"], source_hash)
        self.assertEqual(manifest["inputs"][1]["sha256"], template_hash)
        artifact_paths = {item["path"] for item in manifest["artifacts"]}
        self.assertIn("artifacts/model.oriented.stl", artifact_paths)
        self.assertIn("ready-to-print.3mf", artifact_paths)
        self.assertIn("ready-to-print.gcode", artifact_paths)
        verification = verify_manifest(result.manifest_path)
        self.assertTrue(verification.valid)
        self.assertGreaterEqual(verification.checked_files, 4)

        (output / "ready-to-print.gcode").write_text("tampered", encoding="utf-8")
        damaged = verify_manifest(result.manifest_path)
        self.assertFalse(damaged.valid)
        self.assertTrue(any("SHA-256 mismatch" in item for item in damaged.errors))

    def test_pipeline_refuses_existing_output_before_writing(self) -> None:
        source = self.root / "box.stl"
        trimesh.creation.box().export(source)
        template = self.profile()
        output = self.root / "existing"
        output.mkdir()

        with self.assertRaises(PipelineError):
            run_pipeline(source, template, output)

    def test_pipeline_rejects_incompatible_profile_before_writing(self) -> None:
        source = self.root / "box.stl"
        trimesh.creation.box().export(source)
        template = self.profile(printer_model="Bambu Lab A1")
        output = self.root / "must-not-exist"

        with self.assertRaisesRegex(PipelineError, "printer mismatch"):
            run_pipeline(source, template, output)

        self.assertFalse(output.exists())

    def test_pipeline_rejects_unknown_print_priority_before_writing(self) -> None:
        source = self.root / "box.stl"
        trimesh.creation.box().export(source)
        template = self.profile()
        output = self.root / "invalid-priority"

        with self.assertRaisesRegex(PipelineError, "unsupported print priority"):
            run_pipeline(source, template, output, print_priority="turbo")

        self.assertFalse(output.exists())

    def test_pipeline_rejects_unknown_model_purpose_before_writing(self) -> None:
        source = self.root / "box.stl"
        trimesh.creation.box().export(source)
        template = self.profile()
        output = self.root / "invalid-purpose"

        with self.assertRaisesRegex(PipelineError, "unsupported model purpose"):
            run_pipeline(source, template, output, model_purpose="unknown")

        self.assertFalse(output.exists())

    def test_pipeline_applies_print_dna_after_surface_decisions(self) -> None:
        source = self.root / "learned-box.stl"
        trimesh.creation.box(extents=(20.0, 20.0, 8.0)).export(source)
        template = self.profile()
        captured: dict[str, object] = {}

        def capture_settings(*_: object, **kwargs: object) -> None:
            captured["settings"] = kwargs["recommended_settings"]
            captured["optimization_plan"] = kwargs["optimization_plan"]
            raise RuntimeError("captured")

        dna = {
            "key": {
                "printer_model": "Bambu Lab P1S",
                "nozzle_diameter_mm": 0.4,
                "material": "PLA",
                "spool": "default",
            },
            "sample_count": 1,
            "confidence": "LOW",
            "defect_rates": {"rough_top": 1.0},
            "adjustments": {
                "top_layers_delta": 1,
                "top_surface_speed_multiplier": 0.85,
            },
            "updated_utc": "2026-08-27T00:00:00+00:00",
        }
        with mock.patch(
            "ai_print_optimizer.pipeline.compare_stl_supports",
            side_effect=capture_settings,
        ), self.assertRaisesRegex(RuntimeError, "captured"):
            run_pipeline(
                source,
                template,
                self.root / "learned-output",
                print_dna_profile=dna,
            )

        settings = captured["settings"]
        self.assertGreaterEqual(settings.top_layers, 7)  # type: ignore[union-attr]
        self.assertLess(settings.top_surface_speed_mm_s, 80)  # type: ignore[union-attr]
        plan = captured["optimization_plan"]
        self.assertGreaterEqual(len(plan.candidates), 2)  # type: ignore[union-attr]

    def test_manual_overrides_disable_parameter_search_but_keep_validation(self) -> None:
        source = self.root / "manual-box.stl"
        trimesh.creation.box().export(source)
        captured: dict[str, object] = {}

        def capture_plan(*_: object, **kwargs: object) -> None:
            captured["plan"] = kwargs["optimization_plan"]
            raise RuntimeError("captured")

        with mock.patch(
            "ai_print_optimizer.pipeline.compare_stl_supports",
            side_effect=capture_plan,
        ), self.assertRaisesRegex(RuntimeError, "captured"):
            run_pipeline(
                source,
                self.profile(),
                self.root / "manual-output",
                print_setting_overrides={"layer_height_mm": 0.18},
            )

        candidates = captured["plan"].candidates  # type: ignore[union-attr]
        self.assertGreater(len(candidates), 1)
        self.assertTrue(
            all(item.settings.layer_height_mm == 0.18 for item in candidates)
        )

    def test_every_body_is_oriented_before_multi_model_arrangement(self) -> None:
        tall = trimesh.creation.box(extents=(8.0, 8.0, 60.0))
        flat = trimesh.creation.box(extents=(30.0, 20.0, 4.0))
        flat.apply_translation((50.0, 0.0, 0.0))
        source = self.root / "two-bodies.stl"
        trimesh.util.concatenate((tall, flat)).export(source)
        captured: dict[str, object] = {}

        def capture_multi(*args: object, **kwargs: object) -> None:
            captured["geometry"] = args[2]
            captured["configurations"] = kwargs["object_configurations"]
            captured["use_extracted_geometry"] = kwargs["use_extracted_geometry"]
            raise RuntimeError("captured-oriented-multi")

        with mock.patch(
            "ai_print_optimizer.pipeline.optimize_multi_object_project",
            side_effect=capture_multi,
        ), self.assertRaisesRegex(RuntimeError, "captured-oriented-multi"):
            run_pipeline(
                source,
                self.profile(),
                self.root / "multi-output",
                quality_search=False,
            )

        geometry = captured["geometry"]
        self.assertTrue(captured["use_extracted_geometry"])
        self.assertEqual(len(captured["configurations"]), 2)  # type: ignore[arg-type]
        self.assertEqual(geometry.printable_object_count, 2)  # type: ignore[union-attr]
        self.assertTrue(  # type: ignore[union-attr]
            all("model.objects.oriented" in str(item.output_stl_path) for item in geometry.objects)
        )


if __name__ == "__main__":
    unittest.main()
