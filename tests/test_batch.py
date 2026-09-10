from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import trimesh

from ai_print_optimizer.batch import _model_directory, run_batch


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.models = self.root / "models"
        self.models.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def profile(self) -> Path:
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
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Metadata/project_settings.config", json.dumps(settings))
        return path

    def model(self, name: str) -> Path:
        path = self.models / name
        trimesh.creation.box().export(path)
        return path

    def test_batch_continues_after_failure_and_writes_standalone_reports(self) -> None:
        first = self.model("first.stl")
        second = self.model("second & unsafe.stl")
        output = self.root / "batch"

        def fake_pipeline(source: Path, _: Path, destination: Path, **__: object):
            if Path(source) == second:
                raise RuntimeError("synthetic failure")
            destination = Path(destination)
            destination.mkdir()
            manifest = destination / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            return SimpleNamespace(
                mode="single-slice",
                manifest_path=manifest,
                slice_result=SimpleNamespace(
                    total_print_time_s=120.0,
                    total_used_g=3.5,
                ),
                support_comparison=None,
            )

        with mock.patch("ai_print_optimizer.batch.run_pipeline", side_effect=fake_pipeline):
            result = run_batch(self.models, self.profile(), output)

        self.assertEqual(result.completed, 1)
        self.assertEqual(result.failed, 1)
        self.assertTrue(result.summary_path.is_file())
        summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["schema_version"], 1)
        self.assertIsNotNone(result.error_log_path)
        self.assertTrue(result.error_log_path.is_file())  # type: ignore[union-attr]
        self.assertIn("synthetic failure", result.error_log_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        report = result.html_report_path.read_text(encoding="utf-8")
        self.assertIn("first.stl", report)
        self.assertIn("second &amp; unsafe.stl", report)
        self.assertNotIn("<script", report.lower())
        self.assertEqual(result.items[0].source_path, first)

    def test_resume_verifies_manifest_and_skips_completed_model(self) -> None:
        source = self.model("ready.stl")
        output = self.root / "batch"
        output.mkdir()
        model_output = output / _model_directory(source)
        model_output.mkdir()
        manifest = model_output / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "application": {
                        "name": "ai-print-optimizer",
                        "version": "1.0.0",
                    },
                    "created_utc": "2026-08-23T00:00:00+00:00",
                    "runtime": {},
                    "inputs": [
                        {
                            "role": "source_stl",
                            "path": str(source),
                            "sha256": _hash(source),
                        }
                    ],
                    "parameters": {"mode": "single-slice", "material": "PLA"},
                    "stages": {
                        "slice_result": {
                            "total_print_time_s": 60.0,
                            "total_used_g": 2.0,
                        },
                        "support_comparison": None,
                    },
                    "artifacts": [],
                }
            ),
            encoding="utf-8",
        )

        with mock.patch("ai_print_optimizer.batch.run_pipeline") as pipeline:
            result = run_batch(self.models, self.profile(), output, resume=True)

        pipeline.assert_not_called()
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.failed, 0)
        self.assertIsNone(result.error_log_path)
        self.assertIn("resume-001", result.summary_path.name)


if __name__ == "__main__":
    unittest.main()
