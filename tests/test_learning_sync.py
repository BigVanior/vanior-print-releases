from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_print_optimizer.learning_sync import (
    LearningSyncError,
    build_anonymous_feedback_batch,
)


class LearningSyncTests(unittest.TestCase):
    def test_anonymous_batch_excludes_paths_names_notes_and_photos(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "print-dna.json"
            store.write_text(json.dumps({
                "schema_version": 2,
                "profiles": {
                    "profile": {
                        "key": {"printer_model": "P1S", "nozzle_diameter_mm": 0.4, "material": "PLA", "spool": "secret"},
                        "feedback": [{
                            "project_id": "private-id", "source_name": "secret-model.stl",
                            "quality_rating": 10, "notes": "private note",
                            "photo_path": "C:/private/photo.jpg", "photo_sha256": "secret",
                            "defects": ["rough_top"], "defect_region": "top",
                            "parameter_snapshot": {"layer_height_mm": 0.2, "ready_3mf": "secret.3mf"},
                        }],
                    }
                },
            }), encoding="utf-8")
            batch = build_anonymous_feedback_batch(store)
            encoded = json.dumps(batch)
            for secret in ("private-id", "secret-model", "private note", "photo.jpg", "secret.3mf", "spool"):
                self.assertNotIn(secret, encoded)
            self.assertEqual(batch["feedback"][0]["parameter_snapshot"], {"layer_height_mm": 0.2})
            self.assertEqual(batch["feedback"][0]["quality_rating"], 5)

    def test_sync_endpoint_must_be_https(self) -> None:
        from ai_print_optimizer.learning_sync import sync_community_learning
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaises(LearningSyncError),
        ):
            sync_community_learning(
                Path(temporary) / "local.json",
                Path(temporary) / "global.json",
                "http://example.com",
            )
