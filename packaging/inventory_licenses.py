"""Emit release dependency metadata for the PowerShell packaging script."""

from __future__ import annotations

import json
from importlib import metadata

NAMES = (
    "numpy",
    "trimesh",
    "fast-simplification",
    "shapely",
    "Pillow",
    "PySide6",
    "PySide6-Essentials",
    "PySide6-Addons",
    "shiboken6",
    "PyInstaller",
    "paho-mqtt",
)


def main() -> None:
    rows: list[dict[str, object]] = []
    for name in NAMES:
        distribution = metadata.distribution(name)
        license_name = (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or "UNKNOWN"
        )
        license_files: list[str] = []
        for item in distribution.files or ():
            normalized = str(item).replace("\\", "/").lower()
            base = normalized.rsplit("/", 1)[-1]
            if "/licenses/" in normalized or base.startswith(
                ("license", "copying", "notice")
            ):
                candidate = distribution.locate_file(item)
                if candidate.is_file():
                    license_files.append(str(candidate.resolve()))
        rows.append(
            {
                "name": distribution.metadata.get("Name") or name,
                "version": distribution.version,
                "license": license_name.splitlines()[0],
                "license_files": sorted(set(license_files)),
            }
        )
    print(json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    main()
