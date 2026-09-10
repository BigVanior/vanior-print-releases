from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ai_print_optimizer
from ai_print_optimizer.diagnostics import run_diagnostics
from ai_print_optimizer.io_utils import atomic_write_new_text
from ai_print_optimizer.schema import load_schema, validate_release_document


class ReleaseTests(unittest.TestCase):
    def test_atomic_writer_refuses_overwrite_and_leaves_no_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "report.json"
            atomic_write_new_text(target, "first\n")

            with self.assertRaises(FileExistsError):
                atomic_write_new_text(target, "second\n")

            self.assertEqual(target.read_text(encoding="utf-8"), "first\n")
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_bundled_schemas_detect_missing_required_keys(self) -> None:
        schema = load_schema("pipeline-manifest")
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        errors = validate_release_document({"schema_version": 1}, "pipeline-manifest")
        self.assertTrue(any("missing required key application" in item for item in errors))

    def test_diagnostics_cover_release_dependencies_and_slicer(self) -> None:
        versions = {
            "ai-print-optimizer": "0.5",
            "numpy": "2.5.2",
            "trimesh": "4.12.2",
            "fast-simplification": "0.1.13",
            "shapely": "2.1.2",
            "Pillow": "12.3.0",
            "PySide6": "6.11.2",
            "paho-mqtt": "2.1.0",
        }

        with (
            mock.patch(
                "ai_print_optimizer.diagnostics.importlib.metadata.distribution",
                side_effect=lambda name: mock.Mock(
                    version=versions[name],
                    locate_file=lambda _path: Path(ai_print_optimizer.__file__).parent.parent,
                ),
            ),
        ):
            report = run_diagnostics()

        self.assertTrue(report.ok)
        names = {check.name for check in report.checks}
        self.assertIn("schema:pipeline-manifest", names)
        self.assertIn("schema:batch-summary", names)
        self.assertIn("atomic-write", names)
        self.assertIn("independent-engine", names)

    def test_schema_validator_supports_nullable_release_fields(self) -> None:
        payload = {
            "schema_version": 1,
            "application": {"name": "ai-print-optimizer", "version": "1.0.0"},
            "input_dir": "/input",
            "output_dir": "/output",
            "profile_template_path": "/profile.3mf",
            "created_utc": "2026-08-23T00:00:00+00:00",
            "completed": 0,
            "skipped": 0,
            "failed": 0,
            "items": [],
            "summary_path": "/output/batch-summary.json",
            "html_report_path": "/output/batch-report.html",
            "error_log_path": None,
        }
        self.assertEqual(validate_release_document(payload, "batch-summary"), ())
        payload["error_log_path"] = 42
        errors = validate_release_document(payload, "batch-summary")
        self.assertIn("expected string or null", errors[0])

    def test_public_api_is_unique_and_resolvable(self) -> None:
        self.assertEqual(ai_print_optimizer.__version__, "0.6.6")
        self.assertEqual(
            len(ai_print_optimizer.__all__),
            len(set(ai_print_optimizer.__all__)),
        )
        for name in ai_print_optimizer.__all__:
            self.assertTrue(hasattr(ai_print_optimizer, name), name)

    def test_windows_packaging_exposes_trust_metadata_and_avoids_opaque_packing(self) -> None:
        root = Path(__file__).resolve().parents[1]
        build_script = (root / "packaging" / "build_windows.ps1").read_text(encoding="utf-8")
        installer = (root / "packaging" / "installer.iss").read_text(encoding="utf-8")
        manifest = (root / "packaging" / "app.manifest").read_text(encoding="utf-8")
        version_info = (root / "packaging" / "version_info.template.txt").read_text(encoding="utf-8")
        self.assertIn("--onedir", build_script)
        self.assertIn("--noupx", build_script)
        self.assertIn("--version-file", build_script)
        self.assertIn('requestedExecutionLevel level="asInvoker"', manifest)
        self.assertIn("AppPublisherURL=", installer)
        self.assertIn("PrivilegesRequired=lowest", installer)
        self.assertIn("Compression=zip/9", installer)
        self.assertIn("SolidCompression=no", installer)
        self.assertIn('Name: "{app}\\_internal\\slicer"', installer)
        for field in ("CompanyName", "FileDescription", "FileVersion", "ProductVersion"):
            self.assertIn(field, version_info)

    def test_store_packaging_has_full_trust_manifest_and_legal_gate(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = (root / "packaging" / "AppxManifest.template.xml").read_text(
            encoding="utf-8"
        )
        build_script = (root / "packaging" / "build_msix.ps1").read_text(
            encoding="utf-8"
        )
        windows_build = (root / "packaging" / "build_windows.ps1").read_text(
            encoding="utf-8"
        )
        notices = (root / "packaging" / "THIRD_PARTY_NOTICES_RU.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn('EntryPoint="Windows.FullTrustApplication"', manifest)
        self.assertIn('rescap:Capability Name="runFullTrust"', manifest)
        self.assertIn("VANIOR_STORE_IDENTITY_NAME", build_script)
        self.assertIn("MakeAppx", build_script)
        self.assertIn("stage_legal_notices.ps1", windows_build)
        self.assertNotIn("BambuStudio-AGPL", notices)
        for dependency in ("PySide6", "NumPy", "trimesh", "Pillow", "Paho MQTT"):
            self.assertIn(dependency, notices)

    def test_installer_eula_matches_public_terms(self) -> None:
        root = Path(__file__).resolve().parents[1]
        public_terms = (root / "docs" / "TERMS_OF_USE_RU.md").read_text(
            encoding="utf-8"
        )
        installer_terms = (root / "packaging" / "TERMS_OF_USE_RU.txt").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            public_terms.removeprefix("# "),
            installer_terms,
        )


if __name__ == "__main__":
    unittest.main()
