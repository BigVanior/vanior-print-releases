from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from threading import Event

import trimesh

from ai_print_optimizer.pipeline import run_pipeline
from ai_print_optimizer.progress import (
    OperationCancelled,
    ProgressEvent,
    check_cancelled,
    emit_progress,
)


class ProgressTests(unittest.TestCase):
    def test_progress_is_clamped_and_typed(self) -> None:
        events: list[ProgressEvent] = []
        emit_progress(events.append, "test", "Проверка", 120)
        self.assertEqual(events, [ProgressEvent("test", "Проверка", 100)])

    def test_cancelled_pipeline_stops_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "box.stl"
            trimesh.creation.box().export(source)
            profile = root / "profile.3mf"
            with zipfile.ZipFile(profile, "w") as archive:
                archive.writestr("Metadata/project_settings.config", "{}")
            output = root / "result"
            cancelled = Event()
            cancelled.set()

            with self.assertRaises(OperationCancelled):
                run_pipeline(source, profile, output, cancel_event=cancelled)

            self.assertFalse(output.exists())

    def test_check_cancelled_allows_active_operation(self) -> None:
        check_cancelled(Event())


if __name__ == "__main__":
    unittest.main()
