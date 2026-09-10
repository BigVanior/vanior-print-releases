"""Generate clean Bambu projects from installed official system profiles."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_new_bytes
from .project3mf import (
    CORE_NS,
    IDENTITY_3MF,
    IDENTITY_4X4,
    PRODUCTION_NS,
    RELATIONSHIP_NS,
)


class BuiltinProfileError(RuntimeError):
    """Raised when official Bambu profiles cannot be resolved safely."""


@dataclass(frozen=True)
class GeneratedProfile:
    path: Path
    printer_model: str
    printer_settings_id: str
    process_settings_id: str
    filament_settings_id: str
    material: str
    nozzle_diameter_mm: float
    bed_type: str
    setting_count: int
    source_files: tuple[Path, ...]


PROFILE_METADATA_KEYS = {
    "compatible_printers",
    "compatible_printers_condition",
    "description",
    "from",
    "include",
    "inherits",
    "instantiation",
    "name",
    "setting_id",
    "type",
    "url",
}

DEFAULT_MACHINE = "Bambu Lab P1S 0.4 nozzle"
DEFAULT_PROCESS = "0.20mm Standard @BBL X1C"
DEFAULT_FILAMENTS = {
    "PLA": "Generic PLA",
    "PETG": "Generic PETG",
}


class BambuProfileRegistry:
    """Resolve Bambu's name-based inheritance from its installed JSON files."""

    def __init__(self, profile_root: str | Path):
        self.root = Path(profile_root).expanduser().resolve()
        if not self.root.is_dir():
            raise BuiltinProfileError(f"Bambu profile directory not found: {self.root}")
        self._paths: dict[str, Path] = {}
        self._payloads: dict[str, dict[str, Any]] = {}
        for path in sorted(self.root.rglob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            # Bambu keeps reusable G-code and option fragments next to the
            # typed profiles.  These snippets often have no ``type`` and some
            # releases omit ``name`` as well, while ``include`` still refers
            # to them by file name.  Index both representations so resolving
            # an official machine profile never silently loses safety G-code.
            names = {path.stem}
            declared_name = payload.get("name")
            if isinstance(declared_name, str) and declared_name:
                names.add(declared_name)
            for name in names:
                self._paths.setdefault(name, path)
                self._payloads.setdefault(name, payload)

    def resolve(self, name: str) -> tuple[dict[str, Any], tuple[Path, ...]]:
        used: list[Path] = []
        resolved = self._resolve(name, set(), used)
        return resolved, tuple(dict.fromkeys(used))

    def _resolve(
        self,
        name: str,
        stack: set[str],
        used: list[Path],
    ) -> dict[str, Any]:
        if name in stack:
            raise BuiltinProfileError(f"cyclic Bambu profile inheritance: {name}")
        payload = self._payloads.get(name)
        path = self._paths.get(name)
        if payload is None or path is None:
            raise BuiltinProfileError(f"Bambu system profile not found: {name}")
        stack.add(name)
        result: dict[str, Any] = {}
        parent = payload.get("inherits")
        if isinstance(parent, str) and parent:
            result.update(self._resolve(parent, stack, used))
        includes = payload.get("include", [])
        if isinstance(includes, str):
            includes = [includes]
        if isinstance(includes, list):
            for included in includes:
                if isinstance(included, str) and included:
                    result.update(self._resolve(included, stack, used))
        result.update(payload)
        used.append(path)
        stack.remove(name)
        return result


def _settings_only(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in PROFILE_METADATA_KEYS
    }


def _cube_model() -> bytes:
    ET.register_namespace("", CORE_NS)
    ET.register_namespace("p", PRODUCTION_NS)
    vertices = (
        (0, 0, 0),
        (1, 0, 0),
        (1, 1, 0),
        (0, 1, 0),
        (0, 0, 1),
        (1, 0, 1),
        (1, 1, 1),
        (0, 1, 1),
    )
    faces = (
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    )
    model = ET.Element(
        f"{{{CORE_NS}}}model",
        {"unit": "millimeter", "requiredextensions": "p"},
    )
    ET.SubElement(
        model,
        f"{{{CORE_NS}}}metadata",
        {"name": "BambuStudio:3mfVersion"},
    ).text = "1"
    resources = ET.SubElement(model, f"{{{CORE_NS}}}resources")
    obj = ET.SubElement(
        resources,
        f"{{{CORE_NS}}}object",
        {
            "id": "1",
            "type": "model",
            f"{{{PRODUCTION_NS}}}UUID": "00020000-81cb-4c03-9d28-80fed5dfa1dc",
        },
    )
    mesh = ET.SubElement(obj, f"{{{CORE_NS}}}mesh")
    vertex_root = ET.SubElement(mesh, f"{{{CORE_NS}}}vertices")
    for x, y, z in vertices:
        ET.SubElement(
            vertex_root,
            f"{{{CORE_NS}}}vertex",
            {"x": str(x), "y": str(y), "z": str(z)},
        )
    face_root = ET.SubElement(mesh, f"{{{CORE_NS}}}triangles")
    for v1, v2, v3 in faces:
        ET.SubElement(
            face_root,
            f"{{{CORE_NS}}}triangle",
            {"v1": str(v1), "v2": str(v2), "v3": str(v3)},
        )
    return ET.tostring(model, encoding="utf-8", xml_declaration=True)


def _main_model() -> bytes:
    ET.register_namespace("", CORE_NS)
    ET.register_namespace("p", PRODUCTION_NS)
    model = ET.Element(
        f"{{{CORE_NS}}}model",
        {"unit": "millimeter", "requiredextensions": "p"},
    )
    for name, value in (
        ("Application", "BambuStudio-02.08.02.60"),
        ("BambuStudio:3mfVersion", "1"),
        ("Title", "VANIOR PRINT profile"),
    ):
        ET.SubElement(model, f"{{{CORE_NS}}}metadata", {"name": name}).text = value
    resources = ET.SubElement(model, f"{{{CORE_NS}}}resources")
    obj = ET.SubElement(
        resources,
        f"{{{CORE_NS}}}object",
        {
            "id": "2",
            "type": "model",
            f"{{{PRODUCTION_NS}}}UUID": "00000002-61cb-4c03-9d28-80fed5dfa1dc",
        },
    )
    components = ET.SubElement(obj, f"{{{CORE_NS}}}components")
    ET.SubElement(
        components,
        f"{{{CORE_NS}}}component",
        {
            "objectid": "1",
            f"{{{PRODUCTION_NS}}}path": "/3D/Objects/object_1.model",
            f"{{{PRODUCTION_NS}}}UUID": "00020000-b206-40ff-9872-83e8017abed1",
            "transform": IDENTITY_3MF,
        },
    )
    build = ET.SubElement(
        model,
        f"{{{CORE_NS}}}build",
        {f"{{{PRODUCTION_NS}}}UUID": "2c7c17d8-22b5-4d84-8835-1976022ea369"},
    )
    ET.SubElement(
        build,
        f"{{{CORE_NS}}}item",
        {
            "objectid": "2",
            "printable": "1",
            "transform": IDENTITY_3MF,
            f"{{{PRODUCTION_NS}}}UUID": "00000002-b1ec-4553-aec9-835e5b724bb4",
        },
    )
    return ET.tostring(model, encoding="utf-8", xml_declaration=True)


def _model_settings() -> bytes:
    root = ET.Element("config")
    obj = ET.SubElement(root, "object", {"id": "2"})
    ET.SubElement(obj, "metadata", {"key": "name", "value": "profile-carrier"})
    ET.SubElement(obj, "metadata", {"key": "extruder", "value": "1"})
    ET.SubElement(obj, "metadata", {"face_count": "12"})
    part = ET.SubElement(
        obj,
        "part",
        {
            "id": "1",
            "subtype": "normal_part",
            "uuid": "fde643fc-53b0-4c3b-a68d-27ea05d3c44e",
        },
    )
    for key, value in (
        ("name", "profile-carrier"),
        ("source_file", "profile-carrier.stl"),
        ("source_object_id", "0"),
        ("source_volume_id", "0"),
        ("matrix", IDENTITY_4X4),
        ("source_offset_x", "0"),
        ("source_offset_y", "0"),
        ("source_offset_z", "0"),
    ):
        ET.SubElement(part, "metadata", {"key": key, "value": value})
    ET.SubElement(part, "mesh_stat", {"face_count": "12"})
    plate = ET.SubElement(root, "plate")
    ET.SubElement(plate, "metadata", {"key": "plater_id", "value": "1"})
    instance = ET.SubElement(plate, "model_instance")
    ET.SubElement(instance, "metadata", {"key": "object_id", "value": "2"})
    ET.SubElement(instance, "metadata", {"key": "instance_id", "value": "0"})
    ET.SubElement(instance, "metadata", {"key": "identify_id", "value": "1"})
    assemble = ET.SubElement(root, "assemble")
    ET.SubElement(
        assemble,
        "assemble_item",
        {"object_id": "2", "instance_id": "0", "transform": IDENTITY_3MF, "offset": "0 0 0"},
    )
    ET.SubElement(
        assemble,
        "assemble_item",
        {"object_id": "2", "volume_id": "0", "transform": IDENTITY_3MF},
    )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _relationships() -> bytes:
    root = ET.Element("Relationships", {"xmlns": RELATIONSHIP_NS})
    ET.SubElement(
        root,
        "Relationship",
        {
            "Id": "rel-1",
            "Target": "/3D/Objects/object_1.model",
            "Type": "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel",
        },
    )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def generate_builtin_profile(
    output: str | Path,
    *,
    bambu_studio: str | Path,
    material: str = "PLA",
    bed_type: str = "Textured PEI Plate",
    machine_name: str = DEFAULT_MACHINE,
    process_name: str = DEFAULT_PROCESS,
) -> GeneratedProfile:
    """Create a clean one-object 3MF using installed official Bambu profiles."""
    output_path = Path(output).expanduser().resolve()
    if output_path.exists() or output_path.suffix.lower() != ".3mf":
        raise BuiltinProfileError(f"generated profile output must be a new .3mf: {output_path}")
    if not output_path.parent.is_dir():
        raise BuiltinProfileError(f"generated profile parent not found: {output_path.parent}")
    slicer_path = Path(bambu_studio).expanduser().resolve()
    bbl_root = slicer_path.parent / "resources" / "profiles" / "BBL"
    registry = BambuProfileRegistry(bbl_root)
    material_name = DEFAULT_FILAMENTS.get(material.upper())
    if material_name is None:
        raise BuiltinProfileError(f"no built-in filament profile for material: {material}")
    machine, machine_sources = registry.resolve(machine_name)
    process, process_sources = registry.resolve(process_name)
    filament, filament_sources = registry.resolve(material_name)

    settings: dict[str, Any] = {}
    settings.update(_settings_only(process))
    settings.update(_settings_only(machine))
    settings.update(_settings_only(filament))
    settings.update(
        {
            "printer_model": machine.get("printer_model", "Bambu Lab P1S"),
            "printer_settings_id": machine_name,
            "print_settings_id": process_name,
            "filament_settings_id": [material_name],
            "filament_ids": [str(filament.get("filament_id", ""))],
            "filament_type": filament.get("filament_type", [material.upper()]),
            "filament_colour": ["#000000"],
            "default_filament_colour": [""],
            "filament_colour_type": ["0"],
            "curr_bed_type": bed_type,
            "different_settings_to_system": [""],
            "enable_support": "0",
            "support_type": "normal(auto)",
            "flush_volumes_matrix": ["0"],
            "flush_volumes_vector": ["140", "140"],
        }
    )
    nozzle_values = machine.get("nozzle_diameter", ["0.4"])
    if not isinstance(nozzle_values, list):
        nozzle_values = [nozzle_values]
    nozzle = float(nozzle_values[0])

    content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="config" ContentType="application/octet-stream"/>
</Types>"""
    root_rels = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rel0" Target="/3D/3dmodel.model" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""
    from io import BytesIO

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("3D/3dmodel.model", _main_model())
        archive.writestr("3D/Objects/object_1.model", _cube_model())
        archive.writestr("3D/_rels/3dmodel.model.rels", _relationships())
        archive.writestr("Metadata/model_settings.config", _model_settings())
        archive.writestr(
            "Metadata/project_settings.config",
            json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    atomic_write_new_bytes(output_path, buffer.getvalue())
    return GeneratedProfile(
        path=output_path,
        printer_model=str(settings["printer_model"]),
        printer_settings_id=machine_name,
        process_settings_id=process_name,
        filament_settings_id=material_name,
        material=material.upper(),
        nozzle_diameter_mm=nozzle,
        bed_type=bed_type,
        setting_count=len(settings),
        source_files=tuple(dict.fromkeys((*machine_sources, *process_sources, *filament_sources))),
    )
