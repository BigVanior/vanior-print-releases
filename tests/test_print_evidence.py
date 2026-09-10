from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_print_optimizer.print_evidence import (
    PhysicalPrintComparison,
    record_physical_comparison,
)


class PrintEvidenceTests(unittest.TestCase):
    def test_comparison_copies_all_photos_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = []
            for index in range(4):
                path = root / f"{index}.jpg"
                path.write_bytes(f"photo-{index}".encode())
                photos.append(path)
            comparison = PhysicalPrintComparison(
                comparison_id="parkour-ab-1",
                project_id="project-1",
                source_name="PARKOUR.3mf",
                reference_photos=tuple(photos[:2]),
                vanior_photos=tuple(photos[2:]),
                observations={"stringing": True},
                source_settings={"mvs": 12},
                vanior_settings={"mvs": 21},
            )
            first = record_physical_comparison(root / "learning", comparison)
            second = record_physical_comparison(root / "learning", comparison)

            self.assertEqual(first["comparison_id"], second["comparison_id"])
            self.assertEqual(len(first["reference_photos"]), 2)
            self.assertEqual(len(first["vanior_photos"]), 2)
            self.assertEqual(
                len((root / "learning" / "physical-comparisons-v1.jsonl").read_text().splitlines()),
                1,
            )


if __name__ == "__main__":
    unittest.main()
