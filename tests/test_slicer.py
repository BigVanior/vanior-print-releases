from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

import trimesh

from ai_print_optimizer.optimization_protocol import (
    OptimizationCandidate,
    OptimizationPlan,
)
from ai_print_optimizer.report import PrintSettings
from ai_print_optimizer.slicer import (
    SlicerError,
    _parse_result,
    _used_model_filament_slots,
    analyze_gcode_roles,
    compare_stl_supports,
    compare_supports,
    create_profile_carrier,
    create_support_variant,
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SlicerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def project(self, name: str = "project.3mf") -> Path:
        path = self.root / name
        settings = {
            "enable_support": "0",
            "support_type": "normal(auto)",
            "filament_cost": ["20"],
            "filament_density": ["1.25"],
            "filament_diameter": ["1.75"],
        }
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "Metadata/project_settings.config",
                json.dumps(settings),
            )
            archive.writestr(
                "3D/3dmodel.model",
                """<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
 xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
 xmlns:BambuStudio="http://schemas.bambulab.com/package/2021"
 unit="millimeter" requiredextensions="p">
 <metadata name="BambuStudio:3mfVersion">1</metadata>
 <resources><object id="1" type="model" /></resources>
 <build><item objectid="1" printable="1" /></build>
</model>""",
            )
        return path

    def result_json(self, output: Path, *, time_s: float = 100.0, mass_g: float = 10.0) -> Path:
        output.mkdir()
        payload = {
            "return_code": 0,
            "error_string": "Success.",
            "layer_height": 0.2,
            "wall_loops": 3,
            "sparse_infill_density": 15,
            "sliced_plates": [
                {
                    "id": 1,
                    "total_predication": time_s,
                    "main_predication": time_s - 5,
                    "sliced_time": 250,
                    "generate_support_material_time": 50,
                    "triangle_count": 100,
                    "warning_message": "",
                    "feature_type_times": {"Support": 12.5, "Outer wall": 20},
                    "filaments": [
                        {
                            "id": 1,
                            "filament_id": "TEST",
                            "total_used_g": mass_g,
                            "main_used_g": mass_g - 1,
                        }
                    ],
                }
            ],
        }
        result = output / "result.json"
        result.write_text(json.dumps(payload), encoding="utf-8")
        (output / "plate_1.gcode").write_text(
            "; CHANGE_LAYER\nG90\nM83\nG1 X10 Y0 E0.4 F1200\n",
            encoding="utf-8",
        )
        return result

    def test_support_variant_changes_copy_only(self) -> None:
        source = self.project()
        source_hash = _hash(source)
        output = self.root / "tree.3mf"

        create_support_variant(source, output, "tree(auto)")

        self.assertEqual(_hash(source), source_hash)
        with zipfile.ZipFile(output) as archive:
            settings = json.loads(
                archive.read("Metadata/project_settings.config").decode("utf-8")
            )
            self.assertIn(b'printable="1"', archive.read("3D/3dmodel.model"))
        self.assertEqual(settings["enable_support"], "1")
        self.assertEqual(settings["support_type"], "tree(auto)")
        self.assertIn(
            "enable_support",
            settings["different_settings_to_system"][0].split(";"),
        )
        self.assertIn(
            "support_type",
            settings["different_settings_to_system"][0].split(";"),
        )
        with self.assertRaises(SlicerError):
            create_support_variant(source, output, "normal(auto)")

        none_output = self.root / "none.3mf"
        create_support_variant(source, none_output, "none")
        with zipfile.ZipFile(none_output) as archive:
            none_settings = json.loads(
                archive.read("Metadata/project_settings.config").decode("utf-8")
            )
        self.assertEqual(none_settings["enable_support"], "0")

    def test_profile_carrier_disables_template_objects_only(self) -> None:
        source = self.project()
        source_hash = _hash(source)
        output = self.root / "carrier.3mf"

        create_profile_carrier(source, output)

        self.assertEqual(_hash(source), source_hash)
        with zipfile.ZipFile(output) as archive:
            model = archive.read("3D/3dmodel.model")
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        self.assertIn(b'printable="0"', model)
        self.assertEqual(settings["filament_cost"], ["20"])

    def test_result_parser_uses_authoritative_slicer_metrics(self) -> None:
        project = self.project()
        output = self.root / "slice"
        result_path = self.result_json(output)

        result = _parse_result(result_path, project, output, Path("bambu-studio.exe"))

        self.assertTrue(result.success)
        self.assertEqual(result.total_print_time_s, 100)
        self.assertEqual(result.total_used_g, 10)
        self.assertAlmostEqual(result.total_estimated_cost or 0, 0.2)
        self.assertAlmostEqual(result.plates[0].support_feature_time_s, 12.5)
        self.assertEqual(result.plates[0].overhead_used_g, 1)
        self.assertEqual(len(result.gcode_files), 1)

    def test_result_parser_rejects_non_finite_metrics(self) -> None:
        project = self.project()
        output = self.root / "unsafe-slice"
        result_path = self.result_json(output)
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload["sliced_plates"][0]["total_used_g"] = float("nan")
        payload["sliced_plates"][0]["filaments"][0]["total_used_g"] = float("nan")
        result_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(SlicerError, "unsafe total_used_g"):
            _parse_result(result_path, project, output, Path("bambu-studio.exe"))

    def test_detects_multiple_model_filaments_in_slicer_result(self) -> None:
        project = self.project()
        output = self.root / "multi-color-slice"
        result_path = self.result_json(output)
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload["sliced_plates"][0]["filaments"].append(
            {
                "id": 2,
                "filament_id": "RED",
                "total_used_g": 2.0,
                "main_used_g": 1.5,
            }
        )
        result_path.write_text(json.dumps(payload), encoding="utf-8")

        result = _parse_result(result_path, project, output, Path("bambu-studio.exe"))

        self.assertEqual(_used_model_filament_slots(result), (1, 2))

    def test_gcode_role_parser_estimates_support_mass(self) -> None:
        gcode = self.root / "roles.gcode"
        gcode.write_text(
            """M83
; FEATURE: Outer wall
G1 X10 Y0 E2
G1 E1
G1 X20 Y0 E-0.5
; FEATURE: Support
G1 X30 Y0 E3
; FEATURE: Support interface
G1 X40 Y0 E1
""",
            encoding="utf-8",
        )

        metrics = analyze_gcode_roles(
            gcode,
            filament_densities_g_cm3=[1.0],
            filament_diameters_mm=[2.0],
        )

        self.assertAlmostEqual(metrics.deposited_length_m, 0.006)
        self.assertAlmostEqual(metrics.support_length_m, 0.004)
        self.assertAlmostEqual(metrics.estimated_mass_g or 0.0, 0.0188495559)
        self.assertAlmostEqual(metrics.support_mass_g or 0.0, 0.0125663706)

    def test_gcode_role_parser_tracks_material_switches(self) -> None:
        gcode = self.root / "multi-material.gcode"
        gcode.write_text(
            """M83
M620 S0A
; FEATURE: Support
G1 X10 E10
M620 S1A
; FEATURE: Support interface
G1 Y10 E10
""",
            encoding="utf-8",
        )

        metrics = analyze_gcode_roles(
            gcode,
            filament_densities_g_cm3=[1.0, 2.0],
            filament_diameters_mm=[2.0, 2.0],
        )

        self.assertAlmostEqual(metrics.support_length_m, 0.02)
        self.assertAlmostEqual(metrics.support_mass_g or 0.0, 0.0942477796)

    def test_stl_support_comparison_publishes_verified_winner(self) -> None:
        template = self.project()
        stl = self.root / "model.stl"
        trimesh.creation.box().export(stl)
        stl_hash = _hash(stl)
        template_hash = _hash(template)
        output = self.root / "stl-comparison"

        def fake_strategy(
            source_path: Path,
            settings_project: Path,
            destination: Path,
            strategy: str,
            **_: object,
        ):
            self.assertEqual(source_path, stl)
            self.assertEqual(settings_project, template)
            destination.mkdir()
            result_path = destination / "result.json"
            ready_project = destination / "ready-to-print.3mf"
            ready_project.write_bytes(f"verified-{strategy}".encode())
            (destination / "plate_1.gcode").write_text(
                "; CHANGE_LAYER\nG90\nM83\nG1 X10 Y0 E0.4 F1200\n",
                encoding="utf-8",
            )
            payload = {
                "return_code": 0,
                "error_string": "Success.",
                "layer_height": 0.2,
                "wall_loops": 2,
                "sparse_infill_density": 10,
                "sliced_plates": [
                    {
                        "id": 1,
                        "total_predication": 90 if strategy == "tree" else 100,
                        "main_predication": 80,
                        "filaments": [
                            {
                                "id": 1,
                                "filament_id": "TEST",
                                "total_used_g": 9 if strategy == "tree" else 10,
                                "main_used_g": 9 if strategy == "tree" else 10,
                            }
                        ],
                    }
                ],
            }
            result_path.write_text(json.dumps(payload), encoding="utf-8")
            parsed = _parse_result(
                result_path,
                template,
                destination,
                Path("bambu-studio.exe"),
                source_path=source_path,
            )
            return replace(parsed, ready_project_path=ready_project)

        with (
            mock.patch(
                "ai_print_optimizer.slicer.discover_bambu_studio",
                return_value=Path("bambu-studio.exe"),
            ),
            mock.patch("ai_print_optimizer.slicer._run_stl_strategy", side_effect=fake_strategy),
        ):
            comparison = compare_stl_supports(
                stl, template, output, support_requirement="HIGH"
            )

        self.assertEqual(comparison.recommended, "tree")
        self.assertEqual(comparison.profile_template_sha256, template_hash)
        self.assertEqual(_hash(stl), stl_hash)
        self.assertEqual(_hash(template), template_hash)
        self.assertEqual((output / "ready-to-print.3mf").read_bytes(), b"verified-tree")
        self.assertTrue((output / "ready-to-print.gcode").is_file())

    def test_stl_comparison_selects_fastest_quality_eligible_real_slice(self) -> None:
        template = self.project()
        stl = self.root / "quality-search.stl"
        trimesh.creation.box().export(stl)
        output = self.root / "quality-search"
        baseline = PrintSettings(0.16, 3, 6, 4, True, False, 210, 55, 100)
        faster = replace(
            baseline,
            layer_height_mm=0.20,
            inner_wall_speed_mm_s=340,
        )
        plan = OptimizationPlan(
            2,
            92.0,
            92.0,
            (
                OptimizationCandidate(
                    "quality-baseline", "Baseline", baseline, 100.0, 100.0, True, (), ()
                ),
                OptimizationCandidate(
                    "surface-efficient", "Efficient", faster, 95.0, 98.0, True, (), ("faster",)
                ),
            ),
            (),
        )

        def fake_strategy(
            source_path: Path,
            settings_project: Path,
            destination: Path,
            strategy: str,
            **kwargs: object,
        ):
            settings = kwargs.get("recommended_settings")
            is_candidate = getattr(settings, "layer_height_mm", 0.16) == 0.20
            destination.mkdir()
            ready = destination / "ready-to-print.3mf"
            ready.write_bytes(f"{strategy}-{getattr(settings, 'layer_height_mm', 0)}".encode())
            (destination / "plate_1.gcode").write_text(
                "; CHANGE_LAYER\nG90\nM83\nG1 X10 Y0 E0.4 F1200\n",
                encoding="utf-8",
            )
            payload = {
                "return_code": 0,
                "error_string": "Success.",
                "layer_height": getattr(settings, "layer_height_mm", 0.16),
                "wall_loops": 3,
                "sparse_infill_density": 15,
                "sliced_plates": [
                    {
                        "id": 1,
                        "total_predication": 60 if is_candidate else 100 if strategy == "tree" else 110,
                        "main_predication": 55,
                        "filaments": [{"id": 1, "filament_id": "TEST", "total_used_g": 8, "main_used_g": 8}],
                    }
                ],
            }
            result_path = destination / "result.json"
            result_path.write_text(json.dumps(payload), encoding="utf-8")
            parsed = _parse_result(
                result_path,
                template,
                destination,
                Path("bambu-studio.exe"),
                source_path=source_path,
            )
            return replace(parsed, ready_project_path=ready)

        with (
            mock.patch(
                "ai_print_optimizer.slicer.discover_bambu_studio",
                return_value=Path("bambu-studio.exe"),
            ),
            mock.patch("ai_print_optimizer.slicer._run_stl_strategy", side_effect=fake_strategy),
        ):
            comparison = compare_stl_supports(
                stl,
                template,
                output,
                support_requirement="HIGH",
                recommended_settings=baseline,
                optimization_plan=plan,
            )

        self.assertEqual(comparison.recommended, "tree")
        self.assertIsNotNone(comparison.quality_optimization)
        self.assertEqual(
            comparison.quality_optimization.selected_candidate,  # type: ignore[union-attr]
            "surface-efficient",
        )
        self.assertEqual((output / "ready-to-print.3mf").read_bytes(), b"tree-0.2")

    def test_source_profile_time_guard_rejects_double_duration_result(self) -> None:
        template = self.project("author.3mf")
        stl = self.root / "benchy.stl"
        trimesh.creation.box().export(stl)
        output = self.root / "source-budget"
        configured = PrintSettings(0.21, 3, 6, 4, False, False, 255, 70, 40)

        def fake_strategy(
            source_path: Path,
            settings_project: Path,
            destination: Path,
            strategy: str,
            **kwargs: object,
        ):
            is_author = destination.name == "source-profile-baseline"
            destination.mkdir()
            ready = destination / "ready-to-print.3mf"
            ready.write_bytes(b"author" if is_author else f"vanior-{strategy}".encode())
            (destination / "plate_1.gcode").write_text(
                "; CHANGE_LAYER\nG90\nM83\nG1 X10 Y0 E0.4 F1200\n",
                encoding="utf-8",
            )
            result_path = destination / "result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "return_code": 0,
                        "error_string": "Success.",
                        "layer_height": 0.25 if is_author else 0.21,
                        "wall_loops": 2 if is_author else 3,
                        "sparse_infill_density": 10,
                        "sliced_plates": [
                            {
                                "id": 1,
                                "total_predication": 1600 if is_author else 3500,
                                "main_predication": 1500 if is_author else 3400,
                                "filaments": [
                                    {
                                        "id": 1,
                                        "filament_id": "PETG",
                                        "total_used_g": 13,
                                        "main_used_g": 13,
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            parsed = _parse_result(
                result_path,
                template,
                destination,
                Path("bambu-studio.exe"),
                source_path=source_path,
            )
            return replace(parsed, ready_project_path=ready)

        with (
            mock.patch(
                "ai_print_optimizer.slicer.discover_bambu_studio",
                return_value=Path("bambu-studio.exe"),
            ),
            mock.patch("ai_print_optimizer.slicer._run_stl_strategy", side_effect=fake_strategy),
        ):
            comparison = compare_stl_supports(
                stl,
                template,
                output,
                recommended_settings=configured,
                preferred_strategy="none",
                source_time_budget_ratio=1.25,
            )

        self.assertTrue(comparison.source_time_guard_applied)
        self.assertEqual(comparison.selected_run, comparison.source_profile_baseline)
        self.assertEqual((output / "ready-to-print.3mf").read_bytes(), b"author")
        self.assertIn("+25%", comparison.recommendation_reason)

    def test_comparison_recommends_balanced_winner_and_preserves_source(self) -> None:
        source = self.project()
        source_hash = _hash(source)
        output = self.root / "comparison"

        def fake_slice(project: Path, destination: Path, **_: object):
            is_tree = "tree-auto" in str(project)
            result_path = self.result_json(
                Path(destination),
                time_s=90 if is_tree else 100,
                mass_g=9 if is_tree else 10,
            )
            parsed = _parse_result(
                result_path,
                Path(project),
                Path(destination),
                Path("bambu-studio.exe"),
            )
            ready = Path(destination) / "ready-to-print.3mf"
            ready.write_bytes(Path(project).read_bytes())
            return replace(parsed, ready_project_path=ready)

        with mock.patch("ai_print_optimizer.slicer.slice_3mf", side_effect=fake_slice):
            comparison = compare_supports(
                source, output, support_requirement="HIGH"
            )

        self.assertEqual(comparison.recommended, "tree")
        self.assertEqual(_hash(source), source_hash)
        self.assertTrue((output / "none.3mf").is_file())
        self.assertTrue((output / "normal-auto.3mf").is_file())
        self.assertTrue((output / "tree-auto.3mf").is_file())
        self.assertTrue((output / "ready-to-print.3mf").is_file())


if __name__ == "__main__":
    unittest.main()
