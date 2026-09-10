import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from ai_print_optimizer.reliability import (
    JobJournal,
    VerifiedSliceCache,
    build_diagnostic_bundle,
    create_application_backup,
    create_backup,
    restore_application_data_backup,
)


class ReliabilityTests(unittest.TestCase):
    def test_journal_cache_backup_and_private_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = JobJournal(root / "job.json", "j1", {"operation": "optimize"})
            journal.checkpoint("slice", "test", 50)
            journal.finish(manifest="manifest.json")
            self.assertEqual(json.loads((root / "job.json").read_text())["status"], "complete")

            source = root / "slice"; source.mkdir()
            for name in VerifiedSliceCache.REQUIRED:
                (source / name).write_bytes(name.encode())
            cache = VerifiedSliceCache(root / "cache")
            cache.store("abc", source)
            restored = root / "restored"; restored.mkdir()
            self.assertTrue(cache.restore("abc", restored))
            self.assertEqual((restored / "plate_1.gcode").read_bytes(), b"plate_1.gcode")

            data = root / "history.json"; data.write_text("{}", encoding="utf-8")
            backup = create_backup((data,), root / "backup.zip")
            with zipfile.ZipFile(backup) as archive:
                self.assertIn("backup.json", archive.namelist())

            application = root / "application"
            (application / "_internal").mkdir(parents=True)
            (application / "VANIOR PRINT.exe").write_bytes(b"exe")
            (application / "_internal" / "runtime.dll").write_bytes(b"dll")
            (application / "3. Backup").mkdir()
            (application / "3. Backup" / "old.zip").write_bytes(b"old")
            app_backup = create_application_backup(
                application,
                root / "application-backup.zip",
                data_files=(data,),
                excluded_names=("3. Backup",),
            )
            with zipfile.ZipFile(app_backup) as archive:
                names = archive.namelist()
                self.assertIn("application/VANIOR PRINT.exe", names)
                self.assertIn("application/_internal/runtime.dll", names)
                self.assertIn("data/history.json", names)
                self.assertNotIn("application/3. Backup/old.zip", names)

            photos = root / "print_dna_photos"
            photos.mkdir()
            (photos / "evidence.jpg").write_bytes(b"photo")
            richer_backup = create_application_backup(
                application,
                root / "data-backup.zip",
                data_files=(data,),
                data_roots=(photos,),
            )
            data.write_text('{"changed": true}', encoding="utf-8")
            restored_photos = root / "restored-photos"
            restored_files = restore_application_data_backup(
                richer_backup,
                data_files={"history.json": data},
                data_roots={"print_dna_photos": restored_photos},
            )
            self.assertIn(data.resolve(), restored_files)
            self.assertEqual(data.read_text(encoding="utf-8"), "{}")
            self.assertEqual((restored_photos / "evidence.jpg").read_bytes(), b"photo")
            model = root / "secret.stl"; model.write_bytes(b"model")
            diagnostic = build_diagnostic_bundle(
                root / "diagnostic.zip", documents=(journal.path, model)
            )
            with zipfile.ZipFile(diagnostic) as archive:
                self.assertIn("diagnostics/job.json", archive.namelist())
                self.assertNotIn("diagnostics/secret.stl", archive.namelist())
