from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_print_optimizer.print_dna import (
    PrintDNAKey,
    PrintFeedback,
    apply_print_dna,
    load_combined_print_dna_profile,
    load_print_dna_profile,
    record_print_feedback,
)
from ai_print_optimizer.report import PrintSettings


def _settings() -> PrintSettings:
    return PrintSettings(
        layer_height_mm=0.20,
        wall_loops=3,
        top_layers=5,
        bottom_layers=4,
        supports=True,
        brim=False,
        nozzle_temperature_c=220,
        bed_temperature_c=55,
        fan_percent=100,
    )


class PrintDNATests(unittest.TestCase):
    def test_v1_store_migrates_and_photo_is_copied_with_layer_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "print_dna.json"
            path.write_text('{"schema_version": 1, "profiles": {}}', encoding="utf-8")
            photo = root / "print.jpg"; photo.write_bytes(b"fake-jpeg")
            key = PrintDNAKey("Bambu Lab P1S", 0.4, "PLA")
            record_print_feedback(
                path,
                key,
                PrintFeedback(
                    "one", "part", 3, defects=("rough_top",),
                    photo_path=str(photo), defect_layer_index=42,
                    defect_region="top", parameter_snapshot={"layer_height": 0.16},
                ),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = payload["profiles"][key.identifier]["feedback"][0]
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(record["quality_rating"], 3)
            self.assertEqual(record["defect_layer_index"], 42)
            self.assertEqual(record["defect_region"], "top")
            self.assertTrue(Path(record["photo_path"]).is_file())
            self.assertTrue(record["photo_sha256"])

    def test_feedback_is_isolated_by_printer_nozzle_and_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "print_dna.json"
            pla = PrintDNAKey("Bambu Lab P1S", 0.4, "PLA")
            petg = PrintDNAKey("Bambu Lab P1S", 0.4, "PETG")

            learned = record_print_feedback(
                path,
                pla,
                PrintFeedback("one", "part", 2, defects=("rough_top",)),
            )

            self.assertEqual(learned.sample_count, 1)
            self.assertIn("top_layers_delta", learned.adjustments)
            self.assertEqual(load_print_dna_profile(path, petg).sample_count, 0)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 3)

    def test_overall_quality_accepts_ten_point_scale(self) -> None:
        self.assertEqual(PrintFeedback("one", "part", 10).normalized().quality_rating, 10)
        with self.assertRaisesRegex(Exception, "between 1 and 10"):
            PrintFeedback("one", "part", 11).normalized()

    def test_v2_quality_rating_keeps_its_meaning_after_ten_point_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "print_dna.json"
            key = PrintDNAKey("Bambu Lab P1S", 0.4, "PLA")
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "profiles": {
                            key.identifier: {
                                "key": key.to_dict(),
                                "feedback": [
                                    {
                                        "project_id": "legacy",
                                        "quality_rating": 4,
                                        "defects": [],
                                    }
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            record_print_feedback(path, key, PrintFeedback("new", "part", 9))
            payload = json.loads(path.read_text(encoding="utf-8"))
            ratings = [
                item["quality_rating"]
                for item in payload["profiles"][key.identifier]["feedback"]
            ]
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(ratings, [8, 9])

    def test_learned_defects_create_bounded_setting_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "print_dna.json"
            key = PrintDNAKey("Bambu Lab P1S", 0.4, "PLA")
            profile = record_print_feedback(
                path,
                key,
                PrintFeedback(
                    "one",
                    "part",
                    2,
                    support_removal_rating=1,
                    dimensional_rating=1,
                    defects=(
                        "rough_top",
                        "support_scars",
                        "stringing",
                        "visible_seam",
                        "weak_part",
                        "dimensional_error",
                        "warping",
                    ),
                ),
            )

            applied = apply_print_dna(_settings(), profile)

            self.assertGreater(applied.settings.top_layers, 5)
            self.assertGreater(applied.settings.wall_loops, 3)
            self.assertGreater(applied.settings.support_top_z_distance_mm, 0.20)
            self.assertLess(applied.settings.nozzle_temperature_c, 220)
            self.assertTrue(applied.settings.brim)
            self.assertTrue(applied.decisions)

    def test_good_results_do_not_drift_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "print_dna.json"
            key = PrintDNAKey("Bambu Lab P1S", 0.4, "PLA")
            profile = record_print_feedback(
                path,
                key,
                PrintFeedback("one", "part", 5, support_removal_rating=5, dimensional_rating=5),
            )

            applied = apply_print_dna(_settings(), profile)

            self.assertEqual(applied.settings, _settings())
            self.assertEqual(applied.adjustments, {})

    def test_top_underfill_increases_track_overlap_without_extra_heat_dwell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "print_dna.json"
            key = PrintDNAKey("Bambu Lab P1S", 0.4, "PETG")
            profile = record_print_feedback(
                path,
                key,
                PrintFeedback(
                    "petg-benchy",
                    "Benchy",
                    2,
                    defects=("top_underfill",),
                    defect_region="top",
                ),
            )

            applied = apply_print_dna(_settings(), profile)

            self.assertEqual(applied.settings.top_layers, 6)
            self.assertAlmostEqual(applied.settings.top_surface_line_width_mm, 0.46)
            self.assertEqual(applied.settings.top_surface_speed_mm_s, 200)

    def test_easy_support_removal_with_scars_improves_interface_not_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "print_dna.json"
            key = PrintDNAKey("Bambu Lab P1S", 0.4, "PLA")
            profile = record_print_feedback(
                path,
                key,
                PrintFeedback(
                    "parkour", "figure", 3,
                    support_removal_rating=4,
                    defects=("support_scars", "rough_outer_wall", "stringing", "bed_contact_roughness"),
                ),
            )
            applied = apply_print_dna(_settings(), profile)

            self.assertEqual(applied.settings.support_top_z_distance_mm, 0.20)
            self.assertGreater(applied.settings.support_interface_top_layers, 3)
            self.assertLess(applied.settings.support_interface_spacing_mm, 0.4)
            self.assertLess(applied.settings.outer_wall_speed_mm_s, 200)
            self.assertEqual(applied.settings.wall_generator, "classic")
            self.assertTrue(applied.settings.reduce_crossing_wall)

    def test_supported_underside_roughness_calibrates_gap_and_interface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "print_dna.json"
            key = PrintDNAKey("Bambu Lab P1S", 0.4, "PLA")
            profile = record_print_feedback(
                path,
                key,
                PrintFeedback(
                    "parkour-latest", "figure", 4,
                    support_removal_rating=4,
                    defects=("support_scars", "support_underside_roughness"),
                    defect_region="supported_underside",
                ),
            )
            base = _settings()
            base = PrintSettings(
                **{
                    **base.__dict__,
                    "layer_height_mm": 0.12,
                    "support_interface_top_layers": 4,
                    "support_interface_spacing_mm": 0.28,
                    "support_interface_speed_mm_s": 55,
                }
            )

            applied = apply_print_dna(base, profile)

            self.assertAlmostEqual(applied.settings.support_top_z_distance_mm, 0.16)
            self.assertEqual(applied.settings.support_bottom_z_distance_mm, 0.20)
            self.assertEqual(applied.settings.support_interface_top_layers, 6)
            self.assertAlmostEqual(applied.settings.support_interface_spacing_mm, 0.20)
            self.assertEqual(applied.settings.support_interface_speed_mm_s, 38)

    def test_repeated_stringing_across_distinct_prints_uses_stronger_bounded_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "print_dna.json"
            key = PrintDNAKey("Bambu Lab P1S", 0.4, "PLA")
            record_print_feedback(
                path,
                key,
                PrintFeedback("parkour-first", "figure", 3, defects=("stringing",)),
            )
            profile = record_print_feedback(
                path,
                key,
                PrintFeedback("parkour-latest", "figure", 4, defects=("stringing",)),
            )

            applied = apply_print_dna(_settings(), profile)

            self.assertEqual(applied.settings.nozzle_temperature_c, 210)
            self.assertAlmostEqual(applied.settings.retraction_length_mm, 1.0)
            self.assertAlmostEqual(applied.settings.retraction_speed_mm_s, 40.0)
            self.assertAlmostEqual(applied.settings.wipe_distance_mm, 3.0)

    def test_second_petg_benchy_iteration_keeps_geometry_and_uses_conservative_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "print_dna.json"
            key = PrintDNAKey("Bambu Lab P1S", 0.4, "PETG")
            record_print_feedback(
                path,
                key,
                PrintFeedback(
                    "petg-benchy-first",
                    "Benchy",
                    3,
                    defects=("top_underfill", "stringing"),
                    defect_region="top",
                ),
            )
            profile = record_print_feedback(
                path,
                key,
                PrintFeedback(
                    "petg-benchy-second",
                    "Benchy",
                    4,
                    defects=("stringing",),
                ),
            )
            base = PrintSettings(
                **{
                    **_settings().__dict__,
                    "nozzle_temperature_c": 255,
                    "retraction_length_mm": 0.8,
                    "retraction_speed_mm_s": 30.0,
                    "wipe_distance_mm": 2.0,
                    "travel_speed_mm_s": 700,
                }
            )

            applied = apply_print_dna(base, profile)

            self.assertEqual(applied.settings.nozzle_temperature_c, 245)
            self.assertAlmostEqual(applied.settings.retraction_length_mm, 1.0)
            self.assertAlmostEqual(applied.settings.retraction_speed_mm_s, 40.0)
            self.assertAlmostEqual(applied.settings.wipe_distance_mm, 3.0)
            self.assertEqual(applied.settings.travel_speed_mm_s, 700)
            self.assertEqual(applied.settings.layer_height_mm, base.layer_height_mm)
            self.assertEqual(applied.settings.top_layers, 6)
            self.assertAlmostEqual(applied.settings.top_surface_line_width_mm, 0.46)

    def test_same_physical_print_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photo = root / "print.jpg"; photo.write_bytes(b"physical-print")
            path = root / "print_dna.json"
            key = PrintDNAKey("Bambu Lab P1S", 0.4, "PLA")
            feedback = PrintFeedback("parkour", "figure", 3, photo_path=str(photo))
            record_print_feedback(path, key, feedback)
            profile = record_print_feedback(path, key, feedback)
            self.assertEqual(profile.sample_count, 1)

    def test_local_weight_changes_tuning_but_not_confidence_sample_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / "local.json"
            community = root / "community.json"
            key = PrintDNAKey("Bambu Lab P1S", 0.4, "Bambu Lab PLA Basic")
            record_print_feedback(
                local,
                key,
                PrintFeedback("one", "part", 2, defects=("stringing",)),
            )
            profile = load_combined_print_dna_profile(local, community, key)
            self.assertEqual(profile.sample_count, 1)
            self.assertEqual(profile.confidence, "LOW")
            self.assertGreaterEqual(profile.defect_rates["stringing"], 0.99)


if __name__ == "__main__":
    unittest.main()
