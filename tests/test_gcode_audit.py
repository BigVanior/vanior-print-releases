import tempfile
import unittest
from pathlib import Path

from ai_print_optimizer.gcode_audit import audit_gcode


class GCodeAuditTests(unittest.TestCase):
    def test_reports_safe_flow_and_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "part.gcode"
            path.write_text(
                "; CHANGE_LAYER\nG90\nM83\nG1 X10 Y0 E0.4 F1200\n"
                "; CHANGE_LAYER\nG1 X20 Y0 E0.4 F1200\nG1 E-0.8 F1800\n",
                encoding="utf-8",
            )
            report = audit_gcode(path, maximum_volumetric_speed_mm3_s=20)
            self.assertEqual(report.status, "PASS")
            self.assertEqual(report.layer_count, 2)
            self.assertEqual(report.extrusion_moves, 2)
            self.assertEqual(report.retraction_count, 1)
            self.assertFalse(report.blocking_warnings)

    def test_blocks_flow_over_filament_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "over.gcode"
            path.write_text("; CHANGE_LAYER\nM83\nG1 X1 E2 F6000\n", encoding="utf-8")
            report = audit_gcode(path, maximum_volumetric_speed_mm3_s=10)
            self.assertEqual(report.status, "BLOCKED")
            self.assertTrue(report.blocking_warnings)

    def test_blocks_non_finite_motion_and_unsafe_temperatures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unsafe.gcode"
            path.write_text(
                "; CHANGE_LAYER\nM104 S400\nM140 S200\n"
                "G90\nM83\nG2 X10 Y0 I5 J0 E0.4 F1200\nG1 XNaN Y0\n",
                encoding="utf-8",
            )
            report = audit_gcode(path)
            self.assertEqual(report.status, "BLOCKED")
            self.assertEqual(report.extrusion_moves, 1)
            self.assertGreaterEqual(len(report.blocking_warnings), 3)

    def test_cooling_ignores_empty_layers_and_counts_extruder_only_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "separated.gcode"
            path.write_text(
                ";LAYER:0\nM83\nG1 X10 E0.4 F600\nG1 E-1 F60\n"
                ";LAYER:1\nG1 Z0.2 F1200\n"
                ";LAYER:2\nG1 Z0.4 F1200\n"
                ";LAYER:3\nG1 Z0.6 F1200\n",
                encoding="utf-8",
            )

            report = audit_gcode(path)

            self.assertEqual(report.layer_count, 4)
            self.assertEqual(report.shortest_estimated_layer_time_s, 2.0)
            self.assertFalse(report.advisory_warnings)
