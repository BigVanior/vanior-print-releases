"""Create and verify self-contained VANIOR G-code 3MF archives.

The core model part follows the royalty-free 3MF Core specification. VANIOR
metadata is deliberately kept in its own namespace/files so an incomplete
third-party slicer profile cannot make the document an invalid project.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw

from .input_safety import (
    MAX_ARCHIVE_MEMBER_BYTES,
    UnsafeInputError,
    parse_xml_bytes,
    read_zip_json,
    read_zip_member,
    sha256_zip_member,
    validate_zip_archive,
)
from .io_utils import atomic_write_new_bytes
from .report import PrintSettings
from .vanior_slice import VaniorSliceResult, load_positioned_mesh
from .version import __version__

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PRODUCTION_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
BAMBU_NS = "http://schemas.bambulab.com/package/2021"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
MODEL_REL_TYPE = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
THUMBNAIL_REL_TYPE = (
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"
)
MODEL_PATH = "3D/3dmodel.model"
OBJECT_PATH = "3D/Objects/object_1.model"
GCODE_PATH = "Metadata/plate_1.gcode"
GCODE_MD5_PATH = "Metadata/plate_1.gcode.md5"
SLICE_INFO_PATH = "Metadata/slice_info.config"
PROJECT_SETTINGS_PATH = "Metadata/project_settings.config"
MODEL_SETTINGS_PATH = "Metadata/model_settings.config"
MODEL_SETTINGS_RELS_PATH = "Metadata/_rels/model_settings.config.rels"
SETTINGS_PATH = "Metadata/vanior_settings.json"
MANIFEST_PATH = "Metadata/vanior_manifest.json"
THUMBNAIL_PATH = "Metadata/plate_1.png"
MAX_IN_MEMORY_PACKAGE_BYTES = 640 * 1024 * 1024


class Vanior3MFError(RuntimeError):
    """Raised when an independent 3MF package cannot be trusted."""


@dataclass(frozen=True)
class Vanior3MFVerification:
    valid: bool
    path: Path
    errors: tuple[str, ...]
    entries: tuple[str, ...]
    vertex_count: int
    triangle_count: int
    gcode_sha256: str


def _xml_bytes(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _model_xml(source: Path) -> bytes:
    ET.register_namespace("", CORE_NS)
    ET.register_namespace("p", PRODUCTION_NS)
    ET.register_namespace("BambuStudio", BAMBU_NS)
    model = ET.Element(
        f"{{{CORE_NS}}}model",
        {
            "unit": "millimeter",
            "{http://www.w3.org/XML/1998/namespace}lang": "ru-RU",
        },
    )
    for name, value in (
        ("Title", source.stem),
        ("Designer", "VANIOR PRINT"),
        ("Application", f"VANIOR PRINT-{__version__}"),
        ("BambuStudio:3mfVersion", "1"),
        ("Description", "Самостоятельно рассчитанное одноцветное задание печати"),
        ("CreationDate", datetime.now(UTC).date().isoformat()),
    ):
        node = ET.SubElement(model, f"{{{CORE_NS}}}metadata", {"name": name})
        node.text = value
    resources = ET.SubElement(model, f"{{{CORE_NS}}}resources")
    obj = ET.SubElement(
        resources,
        f"{{{CORE_NS}}}object",
        {
            "id": "2",
            "type": "model",
            "name": source.stem,
            f"{{{PRODUCTION_NS}}}UUID": "00000001-61cb-4c03-9d28-80fed5dfa1dc",
        },
    )
    components = ET.SubElement(obj, f"{{{CORE_NS}}}components")
    ET.SubElement(
        components,
        f"{{{CORE_NS}}}component",
        {
            "objectid": "1",
            f"{{{PRODUCTION_NS}}}path": f"/{OBJECT_PATH}",
            f"{{{PRODUCTION_NS}}}UUID": "00010000-b206-40ff-9872-83e8017abed1",
            "transform": "1 0 0 0 1 0 0 0 1 0 0 0",
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
            "transform": "1 0 0 0 1 0 0 0 1 0 0 0",
            f"{{{PRODUCTION_NS}}}UUID": "00000002-b1ec-4553-aec9-835e5b724bb4",
        },
    )
    return _xml_bytes(model)


def _object_model_xml(vertices: np.ndarray, faces: np.ndarray) -> bytes:
    ET.register_namespace("", CORE_NS)
    ET.register_namespace("p", PRODUCTION_NS)
    model = ET.Element(f"{{{CORE_NS}}}model", {"unit": "millimeter"})
    resources = ET.SubElement(model, f"{{{CORE_NS}}}resources")
    obj = ET.SubElement(
        resources,
        f"{{{CORE_NS}}}object",
        {
            "id": "1",
            "type": "model",
            f"{{{PRODUCTION_NS}}}UUID": "00010000-81cb-4c03-9d28-80fed5dfa1dc",
        },
    )
    mesh = ET.SubElement(obj, f"{{{CORE_NS}}}mesh")
    vertex_nodes = ET.SubElement(mesh, f"{{{CORE_NS}}}vertices")
    for x, y, z in vertices:
        ET.SubElement(
            vertex_nodes,
            f"{{{CORE_NS}}}vertex",
            {"x": f"{float(x):.6f}", "y": f"{float(y):.6f}", "z": f"{float(z):.6f}"},
        )
    triangle_nodes = ET.SubElement(mesh, f"{{{CORE_NS}}}triangles")
    for v1, v2, v3 in faces:
        ET.SubElement(
            triangle_nodes,
            f"{{{CORE_NS}}}triangle",
            {"v1": str(int(v1)), "v2": str(int(v2)), "v3": str(int(v3))},
        )
    return _xml_bytes(model)


def _project_settings_json(
    settings: PrintSettings,
    *,
    material: str,
    color: str,
    support_strategy: str | None = None,
) -> bytes:
    """Return the one-filament machine settings required by P1S firmware.

    This record is descriptive only: the embedded G-code remains generated and
    audited by VANIOR Slice.  The hardware dialect is not evidence that Bambu
    Studio was used to create the job.
    """
    material_name = material.upper()
    preset = "Generic PETG" if material_name == "PETG" else "Generic PLA"
    actual_support = support_strategy or ("normal" if settings.supports else "none")
    payload: dict[str, object] = {
        "printer_model": "Bambu Lab P1S",
        "printer_settings_id": "Bambu Lab P1S 0.4 nozzle",
        "print_settings_id": "VANIOR Slice embedded G-code",
        "printer_technology": "FFF",
        "nozzle_diameter": ["0.4"],
        "curr_bed_type": "Textured PEI Plate",
        "filament_settings_id": [preset],
        "filament_ids": [""],
        "filament_type": [material_name],
        "filament_colour": [color.upper()],
        "default_filament_colour": [""],
        "filament_colour_type": ["0"],
        "filament_diameter": ["1.75"],
        "filament_density": ["1.27" if material_name == "PETG" else "1.24"],
        "layer_height": f"{float(settings.layer_height_mm):.3f}",
        "initial_layer_print_height": f"{float(settings.initial_layer_height_mm):.3f}",
        "wall_loops": str(int(settings.wall_loops)),
        "top_shell_layers": str(int(settings.top_layers)),
        "bottom_shell_layers": str(int(settings.bottom_layers)),
        "sparse_infill_density": f"{int(settings.sparse_infill_percent)}%",
        "sparse_infill_pattern": str(settings.sparse_infill_pattern),
        "enable_support": "1" if settings.supports else "0",
        "support_type": "tree(auto)" if actual_support == "tree" else "normal(auto)",
        "brim_type": "auto_brim" if settings.brim else "no_brim",
        "different_settings_to_system": [""],
        "flush_volumes_matrix": ["0"],
        "flush_volumes_vector": ["140", "140"],
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _model_settings_xml(source: Path, faces: np.ndarray) -> bytes:
    root = ET.Element("config")
    obj = ET.SubElement(root, "object", {"id": "2"})
    for key, value in (("name", source.stem), ("extruder", "1")):
        ET.SubElement(obj, "metadata", {"key": key, "value": value})
    part = ET.SubElement(obj, "part", {"id": "1", "subtype": "normal_part"})
    for key, value in (
        ("name", source.stem),
        ("source_file", source.name),
        ("source_object_id", "0"),
        ("source_volume_id", "0"),
        ("matrix", "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"),
        ("source_offset_x", "0"),
        ("source_offset_y", "0"),
        ("source_offset_z", "0"),
    ):
        ET.SubElement(part, "metadata", {"key": key, "value": value})
    ET.SubElement(part, "mesh_stat", {"face_count": str(len(faces))})
    plate = ET.SubElement(root, "plate")
    for key, value in (
        ("plater_id", "1"),
        ("plater_name", "VANIOR PRINT"),
        ("locked", "false"),
        ("gcode_file", GCODE_PATH),
        ("thumbnail_file", THUMBNAIL_PATH),
    ):
        ET.SubElement(plate, "metadata", {"key": key, "value": value})
    instance = ET.SubElement(plate, "model_instance")
    for key, value in (("object_id", "2"), ("instance_id", "0"), ("identify_id", "1")):
        ET.SubElement(instance, "metadata", {"key": key, "value": value})
    assemble = ET.SubElement(root, "assemble")
    ET.SubElement(
        assemble,
        "assemble_item",
        {
            "object_id": "2",
            "instance_id": "0",
            "transform": "1 0 0 0 1 0 0 0 1 0 0 0",
            "offset": "0 0 0",
        },
    )
    return _xml_bytes(root)


def _content_types_xml() -> bytes:
    ET.register_namespace("", CONTENT_TYPES_NS)
    root = ET.Element(f"{{{CONTENT_TYPES_NS}}}Types")
    defaults = {
        "rels": "application/vnd.openxmlformats-package.relationships+xml",
        "model": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
        "png": "image/png",
        "json": "application/json",
        "gcode": "text/x.gcode",
        "md5": "text/plain",
        "config": "application/xml",
    }
    for extension, content_type in defaults.items():
        ET.SubElement(
            root,
            f"{{{CONTENT_TYPES_NS}}}Default",
            {"Extension": extension, "ContentType": content_type},
        )
    return _xml_bytes(root)


def _root_relationships_xml() -> bytes:
    ET.register_namespace("", REL_NS)
    root = ET.Element(f"{{{REL_NS}}}Relationships")
    ET.SubElement(
        root,
        f"{{{REL_NS}}}Relationship",
        {"Id": "rel-model", "Type": MODEL_REL_TYPE, "Target": f"/{MODEL_PATH}"},
    )
    ET.SubElement(
        root,
        f"{{{REL_NS}}}Relationship",
        {"Id": "rel-thumbnail", "Type": THUMBNAIL_REL_TYPE, "Target": f"/{THUMBNAIL_PATH}"},
    )
    return _xml_bytes(root)


def _model_relationships_xml() -> bytes:
    ET.register_namespace("", REL_NS)
    root = ET.Element(f"{{{REL_NS}}}Relationships")
    ET.SubElement(
        root,
        f"{{{REL_NS}}}Relationship",
        {
            "Id": "rel-object-1",
            "Type": MODEL_REL_TYPE,
            "Target": f"/{OBJECT_PATH}",
        },
    )
    return _xml_bytes(root)


def _model_settings_relationships_xml() -> bytes:
    ET.register_namespace("", REL_NS)
    root = ET.Element(f"{{{REL_NS}}}Relationships")
    ET.SubElement(
        root,
        f"{{{REL_NS}}}Relationship",
        {
            "Id": "rel-gcode",
            "Type": "http://schemas.bambulab.com/package/2021/gcode",
            "Target": f"/{GCODE_PATH}",
        },
    )
    return _xml_bytes(root)


def _slice_info_xml(
    sliced: VaniorSliceResult,
    *,
    material: str,
    color: str,
    supports: bool,
) -> bytes:
    root = ET.Element("config")
    header = ET.SubElement(root, "header")
    ET.SubElement(header, "header_item", {"key": "X-VANIOR-Client-Type", "value": "slicer"})
    ET.SubElement(
        header,
        "header_item",
        {"key": "X-VANIOR-Client-Version", "value": __version__},
    )
    plate = ET.SubElement(root, "plate")
    for key, value in (
        ("index", "1"),
        ("printer_model_id", "C12"),
        ("nozzle_diameters", "0.4"),
        ("gcode_file", GCODE_PATH),
        ("thumbnail_file", THUMBNAIL_PATH),
        ("prediction", str(max(0, round(sliced.estimated_print_time_s)))),
        ("weight", f"{max(0.0, sliced.estimated_mass_g):.3f}"),
        ("outside", "false"),
        ("support_used", "true" if supports else "false"),
        ("support_type", sliced.support_strategy),
    ):
        ET.SubElement(plate, "metadata", {"key": key, "value": value})
    ET.SubElement(
        plate,
        "filament",
        {
            "id": "1",
            "type": material.upper(),
            "color": color,
            "used_g": f"{max(0.0, sliced.estimated_mass_g):.3f}",
            "used_m": f"{max(0.0, sliced.extrusion_length_mm) / 1000.0:.5f}",
            "used_for_object": "true",
            "used_for_support": "true" if supports else "false",
        },
    )
    ET.SubElement(
        plate,
        "nozzle",
        {
            "id": "0",
            "extruder_id": "1",
            "nozzle_diameter": "0.4",
            "volume_type": "Standard",
        },
    )
    return _xml_bytes(root)


def _thumbnail_png(vertices: np.ndarray, faces: np.ndarray, size: int = 512) -> bytes:
    image = Image.new("RGBA", (size, size), (7, 9, 25, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    minimum = vertices[:, :2].min(axis=0)
    maximum = vertices[:, :2].max(axis=0)
    span = np.maximum(maximum - minimum, 1e-6)
    scale = min((size - 64) / float(span[0]), (size - 64) / float(span[1]))
    center = (minimum + maximum) * 0.5
    order = np.argsort(vertices[faces, 2].mean(axis=1))
    stride = max(1, math.ceil(len(order) / 100_000))
    for face_index in order[::stride]:
        triangle = vertices[faces[int(face_index)]]
        points = [
            (
                size * 0.5 + (float(x) - float(center[0])) * scale,
                size * 0.5 - (float(y) - float(center[1])) * scale,
            )
            for x, y in triangle[:, :2]
        ]
        brightness = int(90 + 100 * np.clip(float(triangle[:, 2].mean()) / max(1.0, float(vertices[:, 2].max())), 0, 1))
        draw.polygon(points, fill=(110, 45, brightness, 235))
    draw.rectangle((1, 1, size - 2, size - 2), outline=(160, 70, 255, 255), width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(
        stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=False
    ) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, entries[name])
    return stream.getvalue()


def create_vanior_gcode_3mf(
    source: str | Path,
    gcode: str | Path,
    output: str | Path,
    settings: PrintSettings,
    sliced: VaniorSliceResult,
    *,
    material: str,
    color: str = "#000000",
) -> Path:
    """Publish one self-contained, independently generated ``.gcode.3mf``."""
    source_path = Path(source).expanduser().resolve()
    gcode_path = Path(gcode).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if not output_path.name.casefold().endswith(".gcode.3mf"):
        raise Vanior3MFError("Печатный контейнер должен иметь расширение .gcode.3mf.")
    if output_path.exists():
        raise Vanior3MFError("Генератор 3MF не перезаписывает существующий файл.")
    if not gcode_path.is_file() or gcode_path.stat().st_size <= 0:
        raise Vanior3MFError("Проверенный G-code не найден.")
    if gcode_path.stat().st_size > MAX_ARCHIVE_MEMBER_BYTES:
        raise Vanior3MFError("G-code превышает безопасный предел упаковки 512 МиБ.")
    if sliced.audit.status == "BLOCKED" or sliced.audit.blocking_warnings:
        raise Vanior3MFError("Заблокированный G-code нельзя упаковать в 3MF.")
    mesh = load_positioned_mesh(source_path)
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    gcode_bytes = gcode_path.read_bytes()
    settings_payload = {
        "schema": "vanior-print-settings-v1",
        "application_version": __version__,
        "printer": "Bambu Lab P1S (профиль VANIOR)",
        "material": material.upper(),
        "filament_color": color,
        "settings": asdict(settings),
    }
    entries = {
        "[Content_Types].xml": _content_types_xml(),
        "_rels/.rels": _root_relationships_xml(),
        MODEL_PATH: _model_xml(source_path),
        OBJECT_PATH: _object_model_xml(vertices, faces),
        "3D/_rels/3dmodel.model.rels": _model_relationships_xml(),
        GCODE_PATH: gcode_bytes,
        GCODE_MD5_PATH: hashlib.md5(gcode_bytes, usedforsecurity=False).hexdigest().encode("ascii"),
        SLICE_INFO_PATH: _slice_info_xml(
            sliced,
            material=material,
            color=color,
            supports=sliced.support_strategy != "none",
        ),
        PROJECT_SETTINGS_PATH: _project_settings_json(
            settings,
            material=material,
            color=color,
            support_strategy=sliced.support_strategy,
        ),
        MODEL_SETTINGS_PATH: _model_settings_xml(source_path, faces),
        MODEL_SETTINGS_RELS_PATH: _model_settings_relationships_xml(),
        SETTINGS_PATH: (json.dumps(settings_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        THUMBNAIL_PATH: _thumbnail_png(vertices, faces),
    }
    manifest = {
        "schema": "vanior-gcode-3mf-v1",
        "application_version": __version__,
        "source_name": source_path.name,
        "engine": sliced.engine,
        "engine_stage": sliced.engine_stage,
        "support_strategy": sliced.support_strategy,
        "support_candidates": list(sliced.support_candidates),
        "gcode_audit": sliced.audit.to_dict(),
        "vertex_count": len(vertices),
        "triangle_count": len(faces),
        "files": {
            name: hashlib.sha256(content).hexdigest().upper()
            for name, content in entries.items()
        },
    }
    entries[MANIFEST_PATH] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_new_bytes(output_path, _zip_bytes(entries))
    verification = verify_vanior_gcode_3mf(output_path)
    if not verification.valid:
        output_path.unlink(missing_ok=True)
        raise Vanior3MFError("Проверка созданного 3MF не пройдена: " + "; ".join(verification.errors))
    return output_path


def _safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and "\\" not in name


def verify_vanior_gcode_3mf(path: str | Path) -> Vanior3MFVerification:
    archive_path = Path(path).expanduser().resolve()
    errors: list[str] = []
    entries: tuple[str, ...] = ()
    vertex_count = 0
    triangle_count = 0
    gcode_sha256 = ""
    required = {
        "[Content_Types].xml",
        "_rels/.rels",
        MODEL_PATH,
        OBJECT_PATH,
        "3D/_rels/3dmodel.model.rels",
        GCODE_PATH,
        GCODE_MD5_PATH,
        SLICE_INFO_PATH,
        PROJECT_SETTINGS_PATH,
        MODEL_SETTINGS_PATH,
        MODEL_SETTINGS_RELS_PATH,
        SETTINGS_PATH,
        MANIFEST_PATH,
        THUMBNAIL_PATH,
    }
    try:
        if not archive_path.is_file():
            raise Vanior3MFError("3MF не найден.")
        if archive_path.stat().st_size > MAX_IN_MEMORY_PACKAGE_BYTES:
            raise Vanior3MFError("Печатный 3MF превышает безопасный предел 640 МиБ.")
        with zipfile.ZipFile(archive_path) as archive:
            validate_zip_archive(archive)
            infos = archive.infolist()
            entries = tuple(info.filename for info in infos)
            if len(entries) != len(set(entries)):
                errors.append("архив содержит повторяющиеся пути")
            if any(not _safe_archive_name(name) for name in entries):
                errors.append("архив содержит небезопасный путь")
            missing = sorted(required.difference(entries))
            if missing:
                errors.append("отсутствуют обязательные части: " + ", ".join(missing))
            if sum(info.file_size for info in infos) > 1_000_000_000:
                errors.append("распакованный архив превышает безопасный размер")
            if errors:
                raise Vanior3MFError("; ".join(errors))
            model = parse_xml_bytes(read_zip_member(archive, MODEL_PATH), context=MODEL_PATH)
            object_model = parse_xml_bytes(
                read_zip_member(archive, OBJECT_PATH), context=OBJECT_PATH
            )
            metadata = {
                node.get("name", ""): node.text or ""
                for node in model.findall(f"{{{CORE_NS}}}metadata")
            }
            if not metadata.get("Application", "").startswith("VANIOR PRINT-"):
                errors.append("неверный производитель печатного задания")
            if metadata.get("BambuStudio:3mfVersion") != "1":
                errors.append("неверная версия проектного формата Bambu Studio")
            components = model.findall(f".//{{{CORE_NS}}}component")
            if not any(
                item.get(f"{{{PRODUCTION_NS}}}path") == f"/{OBJECT_PATH}"
                for item in components
            ):
                errors.append("главная модель не ссылается на печатаемый объект")
            vertex_count = len(object_model.findall(f".//{{{CORE_NS}}}vertex"))
            triangle_count = len(object_model.findall(f".//{{{CORE_NS}}}triangle"))
            if vertex_count < 3 or triangle_count < 1:
                errors.append("3MF не содержит печатаемой сетки")
            if not model.findall(f".//{{{CORE_NS}}}build/{{{CORE_NS}}}item"):
                errors.append("3MF не содержит элемента build")
            gcode_bytes = read_zip_member(
                archive, GCODE_PATH, maximum_bytes=MAX_ARCHIVE_MEMBER_BYTES
            )
            gcode_sha256 = hashlib.sha256(gcode_bytes).hexdigest().upper()
            expected_md5 = (
                read_zip_member(archive, GCODE_MD5_PATH, maximum_bytes=256)
                .decode("ascii")
                .strip()
                .lower()
            )
            actual_md5 = hashlib.md5(gcode_bytes, usedforsecurity=False).hexdigest()
            if expected_md5 != actual_md5:
                errors.append("MD5 G-code не совпадает")
            if b"generated by VANIOR Slice" not in gcode_bytes:
                errors.append("G-code создан неизвестным движком")
            manifest = read_zip_json(archive, MANIFEST_PATH)
            if not isinstance(manifest, dict):
                raise Vanior3MFError("Манифест 3MF должен быть объектом JSON.")
            if manifest.get("schema") != "vanior-gcode-3mf-v1":
                errors.append("неизвестная схема манифеста")
            hashes = manifest.get("files") or {}
            if not isinstance(hashes, dict):
                raise Vanior3MFError("Список контрольных сумм 3MF повреждён.")
            expected_hashed_entries = set(entries).difference({MANIFEST_PATH})
            if set(hashes) != expected_hashed_entries:
                errors.append("манифест не покрывает все и только части печатного задания")
            for name, expected_hash in hashes.items():
                if name not in entries:
                    errors.append(f"манифест ссылается на отсутствующий файл: {name}")
                    continue
                actual_hash = sha256_zip_member(archive, name).upper()
                if actual_hash != expected_hash:
                    errors.append(f"SHA-256 не совпадает: {name}")
            parse_xml_bytes(
                read_zip_member(archive, SLICE_INFO_PATH), context=SLICE_INFO_PATH
            )
            project_settings = read_zip_json(archive, PROJECT_SETTINGS_PATH)
            if not isinstance(project_settings, dict):
                raise Vanior3MFError("Конфигурация печати должна быть объектом JSON.")
            if project_settings.get("printer_model") != "Bambu Lab P1S":
                errors.append("неверный совместимый профиль принтера")
            model_settings = parse_xml_bytes(
                read_zip_member(archive, MODEL_SETTINGS_PATH), context=MODEL_SETTINGS_PATH
            )
            if model_settings.find("./object") is None or model_settings.find("./plate") is None:
                errors.append("неполная совместимая конфигурация модели")
            settings = read_zip_json(archive, SETTINGS_PATH)
            if not isinstance(settings, dict):
                raise Vanior3MFError("Настройки VANIOR должны быть объектом JSON.")
            if settings.get("schema") != "vanior-print-settings-v1":
                errors.append("неизвестная схема настроек")
            with Image.open(
                io.BytesIO(
                    read_zip_member(archive, THUMBNAIL_PATH, maximum_bytes=16 * 1024 * 1024)
                )
            ) as thumbnail:
                thumbnail.verify()
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        ET.ParseError,
        zipfile.BadZipFile,
        UnsafeInputError,
        Vanior3MFError,
    ) as exc:
        if not errors:
            errors.append(str(exc))
    return Vanior3MFVerification(
        valid=not errors,
        path=archive_path,
        errors=tuple(errors),
        entries=entries,
        vertex_count=vertex_count,
        triangle_count=triangle_count,
        gcode_sha256=gcode_sha256,
    )


def upgrade_legacy_vanior_gcode_3mf(path: str | Path) -> Path:
    """Create a Bambu-compatible copy of a trusted pre-0.5.9 VANIOR package.

    The source archive is never overwritten.  This keeps old completed jobs
    usable without repeating slicing while retaining the original artifact for
    recovery and comparison.
    """
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file() or not source_path.name.casefold().endswith(".gcode.3mf"):
        raise Vanior3MFError("Старый пакет VANIOR не найден.")
    legacy_required = {
        MODEL_PATH,
        GCODE_PATH,
        GCODE_MD5_PATH,
        SLICE_INFO_PATH,
        SETTINGS_PATH,
        MANIFEST_PATH,
        THUMBNAIL_PATH,
        "[Content_Types].xml",
        "_rels/.rels",
    }
    try:
        if source_path.stat().st_size > MAX_IN_MEMORY_PACKAGE_BYTES:
            raise Vanior3MFError("Старый пакет превышает безопасный предел 640 МиБ.")
        with zipfile.ZipFile(source_path) as archive:
            validate_zip_archive(archive)
            infos = archive.infolist()
            names = {item.filename for item in infos}
            if len(names) != len(infos) or any(
                not _safe_archive_name(item.filename) for item in infos
            ):
                raise Vanior3MFError("Старый 3MF содержит небезопасную структуру.")
            missing = sorted(legacy_required.difference(names))
            if missing:
                raise Vanior3MFError(
                    "Это не полный пакет VANIOR: " + ", ".join(missing)
                )
            manifest = json.loads(archive.read(MANIFEST_PATH))
            if manifest.get("schema") != "vanior-gcode-3mf-v1":
                raise Vanior3MFError("Неизвестная схема старого пакета VANIOR.")
            entries = {item.filename: archive.read(item.filename) for item in infos}
    except (OSError, KeyError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise Vanior3MFError(f"Не удалось прочитать старый пакет VANIOR: {exc}") from exc

    for name, expected in (manifest.get("files") or {}).items():
        content = entries.get(name)
        if content is None or hashlib.sha256(content).hexdigest().upper() != str(expected).upper():
            raise Vanior3MFError(f"Контрольная сумма старого пакета не совпадает: {name}")
    gcode_bytes = entries[GCODE_PATH]
    if b"generated by VANIOR Slice" not in gcode_bytes:
        raise Vanior3MFError("Старый G-code создан неизвестным движком.")
    expected_md5 = entries[GCODE_MD5_PATH].decode("ascii").strip().lower()
    if hashlib.md5(gcode_bytes, usedforsecurity=False).hexdigest() != expected_md5:
        raise Vanior3MFError("MD5 старого G-code не совпадает.")

    settings_payload = json.loads(entries[SETTINGS_PATH])
    if settings_payload.get("schema") != "vanior-print-settings-v1":
        raise Vanior3MFError("Неизвестная схема настроек старого пакета.")
    try:
        settings = PrintSettings(**settings_payload["settings"])
        legacy_model = ET.fromstring(entries[MODEL_PATH])
        vertices = np.asarray(
            [
                (float(node.get("x", "0")), float(node.get("y", "0")), float(node.get("z", "0")))
                for node in legacy_model.findall(f".//{{{CORE_NS}}}vertex")
            ],
            dtype=np.float64,
        )
        faces = np.asarray(
            [
                (int(node.get("v1", "0")), int(node.get("v2", "0")), int(node.get("v3", "0")))
                for node in legacy_model.findall(f".//{{{CORE_NS}}}triangle")
            ],
            dtype=np.int64,
        )
    except (KeyError, TypeError, ValueError, ET.ParseError) as exc:
        raise Vanior3MFError(f"Не удалось восстановить старую модель VANIOR: {exc}") from exc
    if len(vertices) < 3 or len(faces) < 1:
        raise Vanior3MFError("Старый пакет VANIOR не содержит печатаемой сетки.")

    source_name = str(manifest.get("source_name") or source_path.stem)
    source_hint = Path(source_name)
    material = str(settings_payload.get("material") or "PLA").upper()
    color = str(settings_payload.get("filament_color") or "#000000")
    entries[MODEL_PATH] = _model_xml(source_hint)
    entries[OBJECT_PATH] = _object_model_xml(vertices, faces)
    entries["3D/_rels/3dmodel.model.rels"] = _model_relationships_xml()
    entries[PROJECT_SETTINGS_PATH] = _project_settings_json(
        settings, material=material, color=color
    )
    entries[MODEL_SETTINGS_PATH] = _model_settings_xml(source_hint, faces)
    entries[MODEL_SETTINGS_RELS_PATH] = _model_settings_relationships_xml()
    manifest["application_version"] = __version__
    manifest["vertex_count"] = len(vertices)
    manifest["triangle_count"] = len(faces)
    manifest["upgraded_from"] = source_path.name
    entries.pop(MANIFEST_PATH, None)
    manifest["files"] = {
        name: hashlib.sha256(content).hexdigest().upper()
        for name, content in entries.items()
    }
    entries[MANIFEST_PATH] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    base = source_path.name[: -len(".gcode.3mf")]
    destination = source_path.with_name(f"{base}-compatible.gcode.3mf")
    counter = 2
    while destination.exists():
        verification = verify_vanior_gcode_3mf(destination)
        if verification.valid:
            return destination
        destination = source_path.with_name(f"{base}-compatible-{counter}.gcode.3mf")
        counter += 1
    atomic_write_new_bytes(destination, _zip_bytes(entries))
    verification = verify_vanior_gcode_3mf(destination)
    if not verification.valid:
        destination.unlink(missing_ok=True)
        raise Vanior3MFError(
            "Проверка обновлённого 3MF не пройдена: " + "; ".join(verification.errors)
        )
    return destination
