from __future__ import annotations

import struct
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from ai_print_optimizer.input_safety import (
    UnsafeInputError,
    parse_xml_bytes,
    validate_stl_file,
    validate_zip_archive,
)
from ai_print_optimizer.project3mf import Project3MFError, extract_printable_stl


class InputSafetyTests(unittest.TestCase):
    def test_archive_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unsafe.3mf"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("../outside.txt", "payload")
            with (
                zipfile.ZipFile(path) as archive,
                self.assertRaisesRegex(UnsafeInputError, "unsafe archive"),
            ):
                validate_zip_archive(archive)

    def test_archive_rejects_duplicate_case_insensitive_member(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.3mf"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("Metadata/config.json", "{}")
                    archive.writestr("metadata/CONFIG.json", "{}")
            with (
                zipfile.ZipFile(path) as archive,
                self.assertRaisesRegex(UnsafeInputError, "duplicate"),
            ):
                validate_zip_archive(archive)

    def test_archive_rejects_extreme_compression_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bomb.3mf"
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("Metadata/repeated.bin", b"0" * 4_000_000)
            with (
                zipfile.ZipFile(path) as archive,
                self.assertRaisesRegex(UnsafeInputError, "compression ratio"),
            ):
                validate_zip_archive(archive)

    def test_xml_rejects_dtd_and_entities(self) -> None:
        payload = b'<!DOCTYPE x [<!ENTITY y "boom">]><x>&y;</x>'
        with self.assertRaisesRegex(UnsafeInputError, "DTD/entities"):
            parse_xml_bytes(payload)

    def test_binary_stl_rejects_impossible_triangle_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "truncated.stl"
            path.write_bytes(b"binary\x00".ljust(80, b"\x00") + struct.pack("<I", 50))
            with self.assertRaisesRegex(UnsafeInputError, "truncated"):
                validate_stl_file(path)

    def test_3mf_rejects_excessive_component_depth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "deep.3mf"
            objects = []
            for object_id in range(1, 67):
                objects.append(
                    f'<object id="{object_id}" type="model"><components>'
                    f'<component objectid="{object_id + 1}" />'
                    "</components></object>"
                )
            objects.append(
                '<object id="67" type="model"><mesh><vertices>'
                '<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>'
                '<vertex x="0" y="1" z="0"/></vertices><triangles>'
                '<triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
            )
            model = (
                '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
                'unit="millimeter"><resources>'
                + "".join(objects)
                + '</resources><build><item objectid="1"/></build></model>'
            )
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("3D/3dmodel.model", model)
            with self.assertRaisesRegex(Project3MFError, "nesting exceeds"):
                extract_printable_stl(path, root / "deep.stl")


if __name__ == "__main__":
    unittest.main()
