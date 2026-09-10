"""Read printable 3MF geometry and build verified one-model Bambu projects."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import numpy as np
import trimesh

from .input_safety import (
    MAX_COMPONENT_DEPTH,
    MAX_PRINTABLE_OBJECTS,
    MAX_STL_TRIANGLES,
    UnsafeInputError,
    parse_xml_bytes,
    read_zip_json,
    read_zip_member,
    read_zip_xml,
    safe_archive_name,
    sha256_zip_member,
    validate_stl_file,
    validate_zip_archive,
)
from .io_utils import atomic_write_new_bytes
from .report import PrintSettings

if TYPE_CHECKING:
    from .geometry_features import LocalModifierPlan


CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PRODUCTION_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
RELATIONSHIP_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MODEL_PATH = "3D/3dmodel.model"
MODEL_SETTINGS_PATH = "Metadata/model_settings.config"
PROJECT_SETTINGS_PATH = "Metadata/project_settings.config"
LAYER_CONFIG_RANGES_PATH = "Metadata/layer_config_ranges.xml"
IDENTITY_3MF = "1 0 0 0 1 0 0 0 1 0 0 0"
IDENTITY_4X4 = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"

# These members describe template geometry, painting, slicing or previews. They
# are not printer/process profile data and must never be applied to a new STL.
GEOMETRY_SPECIFIC_MEMBERS = {
    "Metadata/cut_information.xml",
    "Metadata/filament_sequence.json",
    "Metadata/layer_config_ranges.xml",
    "Metadata/slice_info.config",
}

ET.register_namespace("", CORE_NS)
ET.register_namespace("p", PRODUCTION_NS)
ET.register_namespace("BambuStudio", "http://schemas.bambulab.com/package/2021")


class Project3MFError(RuntimeError):
    """Raised when a 3MF cannot be read or made unambiguous."""


@dataclass(frozen=True)
class ExtractedObjectGeometry:
    """One printable build item extracted without mixing it with its neighbours."""

    instance_index: int
    object_id: str
    name: str
    output_stl_path: Path
    triangle_count: int
    geometry_signature: str
    output_sha256: str
    transform: tuple[float, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["output_stl_path"] = str(self.output_stl_path)
        return result


@dataclass(frozen=True)
class ExtractedProjectGeometry:
    source_path: Path
    output_stl_path: Path
    plate: int
    printable_object_count: int
    object_names: tuple[str, ...]
    triangle_count: int
    source_sha256: str
    output_sha256: str
    objects: tuple[ExtractedObjectGeometry, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source_path"] = str(self.source_path)
        result["output_stl_path"] = str(self.output_stl_path)
        result["objects"] = [item.to_dict() for item in self.objects]
        return result


@dataclass(frozen=True)
class ObjectPrintConfiguration:
    """Native Bambu object-level process overrides for one source object."""

    object_id: str
    name: str
    settings: PrintSettings
    support_mode: str
    local_modifier_plan: LocalModifierPlan | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "name": self.name,
            "settings": asdict(self.settings),
            "support_mode": self.support_mode,
            "local_modifier_plan": (
                self.local_modifier_plan.to_dict() if self.local_modifier_plan else None
            ),
        }


@dataclass(frozen=True)
class PreparedMultiObjectProject:
    source_project_path: Path
    template_path: Path
    project_path: Path
    printable_object_count: int
    object_names: tuple[str, ...]
    triangle_count: int
    source_project_sha256: str
    template_sha256: str
    project_sha256: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("source_project_path", "template_path", "project_path"):
            result[key] = str(result[key])
        return result


@dataclass(frozen=True)
class PreparedProject:
    source_stl_path: Path
    template_path: Path
    project_path: Path
    support_mode: str
    object_name: str
    triangle_count: int
    geometry_signature: str
    source_stl_sha256: str
    template_sha256: str
    project_sha256: str
    local_modifier_range_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("source_stl_path", "template_path", "project_path"):
            result[key] = str(result[key])
        return result


@dataclass(frozen=True)
class ReadyProjectVerification:
    project_path: Path
    valid: bool
    printable_object_count: int
    object_names: tuple[str, ...]
    triangle_count: int
    geometry_signature: str | None
    support_mode: str | None
    print_settings: dict[str, Any]
    embedded_gcode_path: str | None
    embedded_gcode_sha256: str | None
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["project_path"] = str(self.project_path)
        return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _geometry_signature(mesh: trimesh.Trimesh) -> str:
    """Hash triangle side lengths, invariant to rigid transforms and mesh ordering."""
    triangles = np.asarray(mesh.triangles, dtype=np.float64)
    if triangles.ndim != 3 or triangles.shape[1:] != (3, 3) or len(triangles) == 0:
        raise Project3MFError("cannot fingerprint an empty or invalid mesh")
    edges = np.stack(
        (
            np.linalg.norm(triangles[:, 0] - triangles[:, 1], axis=1),
            np.linalg.norm(triangles[:, 1] - triangles[:, 2], axis=1),
            np.linalg.norm(triangles[:, 2] - triangles[:, 0], axis=1),
        ),
        axis=1,
    )
    quantized = np.rint(np.sort(edges, axis=1) * 100_000.0).astype("<i8")
    order = np.lexsort((quantized[:, 2], quantized[:, 1], quantized[:, 0]))
    return hashlib.sha256(quantized[order].tobytes()).hexdigest()


def _same_geometry(expected: trimesh.Trimesh, actual: trimesh.Trimesh) -> bool:
    """Compare triangle shapes while allowing slicer float32 round-tripping.

    Bambu Studio may arrange a model and then serialize transformed coordinates
    with single-precision accuracy.  On a full-size build plate that harmless
    round-trip can perturb a triangle area by slightly more than a fixed
    0.00005 mm² even though every edge and face is still present.  Scale the
    absolute tolerance to the model size while retaining the complete sorted
    edge and area distributions, so a genuinely different mesh is rejected.
    """
    if len(expected.faces) != len(actual.faces):
        return False

    def descriptor(mesh: trimesh.Trimesh) -> tuple[np.ndarray, np.ndarray]:
        triangles = np.asarray(mesh.triangles, dtype=np.float64)
        edge_lengths = np.sort(
            np.concatenate(
                (
                    np.linalg.norm(triangles[:, 0] - triangles[:, 1], axis=1),
                    np.linalg.norm(triangles[:, 1] - triangles[:, 2], axis=1),
                    np.linalg.norm(triangles[:, 2] - triangles[:, 0], axis=1),
                )
            )
        )
        areas = np.sort(
            np.linalg.norm(
                np.cross(
                    triangles[:, 1] - triangles[:, 0],
                    triangles[:, 2] - triangles[:, 0],
                ),
                axis=1,
            )
            * 0.5
        )
        return edge_lengths, areas

    expected_edges, expected_areas = descriptor(expected)
    actual_edges, actual_areas = descriptor(actual)
    linear_scale = max(
        float(np.ptp(np.asarray(expected.vertices, dtype=np.float64), axis=0).max()),
        float(np.ptp(np.asarray(actual.vertices, dtype=np.float64), axis=0).max()),
        1.0,
    )
    # 3MF stores decimal coordinates and the slicer may arrange objects around
    # the 256 mm bed before serialising them again.  The combined decimal and
    # float32 round-trip is slightly larger than a single float32 conversion,
    # especially for meshes containing microscopic exporter slivers.  These
    # tolerances remain far below FFF positioning resolution while preventing
    # a valid, unchanged model from being rejected after that round-trip.
    edge_atol = max(5e-5, linear_scale * 5e-7)
    area_atol = max(5e-4, linear_scale * linear_scale * 1e-7)
    return bool(
        np.allclose(expected_edges, actual_edges, rtol=2e-6, atol=edge_atol)
        and np.allclose(expected_areas, actual_areas, rtol=4e-6, atol=area_atol)
    )


def _safe_member_path(raw: str) -> str:
    # 3MF production relationships use OPC part URIs ("/3D/...").  The
    # leading slash is URI syntax, not an archive extraction root.
    if raw.startswith("/") and not raw.startswith("//"):
        raw = raw[1:]
    try:
        return safe_archive_name(raw)
    except UnsafeInputError as exc:
        raise Project3MFError(str(exc)) from exc


def _parse_transform(raw: str | None) -> np.ndarray:
    if not raw:
        return np.eye(4)
    try:
        values = [float(item) for item in raw.split()]
    except ValueError as exc:
        raise Project3MFError(f"invalid 3MF transform: {raw}") from exc
    if len(values) != 12 or not all(math.isfinite(value) for value in values):
        raise Project3MFError(f"invalid 3MF transform: {raw}")
    result = np.eye(4)
    result[:3, :3] = np.asarray(values[:9], dtype=float).reshape((3, 3)).T
    result[:3, 3] = values[9:12]
    return result


def _unit_scale(root: ET.Element) -> float:
    unit = root.get("unit", "millimeter").casefold()
    scales = {
        "micron": 0.001,
        "millimeter": 1.0,
        "centimeter": 10.0,
        "meter": 1000.0,
        "inch": 25.4,
        "foot": 304.8,
    }
    try:
        return scales[unit]
    except KeyError as exc:
        raise Project3MFError(f"unsupported 3MF unit: {unit}") from exc


def _object_names(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        payload = read_zip_member(archive, MODEL_SETTINGS_PATH)
    except KeyError:
        return {}
    try:
        root = parse_xml_bytes(payload, context=MODEL_SETTINGS_PATH)
    except (ET.ParseError, UnsafeInputError) as exc:
        raise Project3MFError("cannot parse Bambu model settings") from exc
    names: dict[str, str] = {}
    for obj in root.findall("./object"):
        object_id = obj.get("id")
        name = next(
            (
                item.get("value")
                for item in obj.findall("./metadata")
                if item.get("key") == "name" and item.get("value")
            ),
            None,
        )
        if object_id and name:
            names[object_id] = name
    return names


def _plate_object_ids(archive: zipfile.ZipFile, plate: int) -> set[str] | None:
    try:
        payload = read_zip_member(archive, MODEL_SETTINGS_PATH)
    except KeyError:
        return None
    try:
        root = parse_xml_bytes(payload, context=MODEL_SETTINGS_PATH)
    except (ET.ParseError, UnsafeInputError) as exc:
        raise Project3MFError("cannot parse Bambu plate settings") from exc
    plates = root.findall("./plate")
    if not plates:
        return None
    if plate > len(plates):
        raise Project3MFError(f"3MF has {len(plates)} plate(s), requested plate {plate}")
    result: set[str] = set()
    for instance in plates[plate - 1].findall("./model_instance"):
        object_id = next(
            (
                item.get("value")
                for item in instance.findall("./metadata")
                if item.get("key") == "object_id"
            ),
            None,
        )
        if object_id:
            result.add(object_id)
    return result or None


def _load_model_root(
    archive: zipfile.ZipFile,
    path: str,
    cache: dict[str, ET.Element],
) -> ET.Element:
    path = _safe_member_path(path)
    if path not in cache:
        try:
            cache[path] = read_zip_xml(archive, path)
        except KeyError as exc:
            raise Project3MFError(f"3MF model part is missing: {path}") from exc
        except ET.ParseError as exc:
            raise Project3MFError(f"cannot parse 3MF model part: {path}") from exc
    return cache[path]


def _resolve_object_meshes(
    archive: zipfile.ZipFile,
    part_path: str,
    object_id: str,
    transform: np.ndarray,
    cache: dict[str, ET.Element],
    stack: set[tuple[str, str]],
    *,
    depth: int = 0,
    budget: dict[str, int] | None = None,
) -> list[trimesh.Trimesh]:
    if depth > MAX_COMPONENT_DEPTH:
        raise Project3MFError(
            f"3MF component nesting exceeds the safety limit ({MAX_COMPONENT_DEPTH})"
        )
    if budget is None:
        budget = {"triangles": 0, "meshes": 0}
    key = (_safe_member_path(part_path), object_id)
    if key in stack:
        raise Project3MFError(f"cyclic 3MF component reference: {part_path}#{object_id}")
    stack.add(key)
    root = _load_model_root(archive, part_path, cache)
    namespace = {"m": CORE_NS}
    obj = next(
        (item for item in root.findall("./m:resources/m:object", namespace) if item.get("id") == object_id),
        None,
    )
    if obj is None:
        raise Project3MFError(f"3MF object is missing: {part_path}#{object_id}")
    meshes: list[trimesh.Trimesh] = []
    mesh_node = obj.find("./m:mesh", namespace)
    if mesh_node is not None:
        try:
            vertices = np.asarray(
                [
                    [float(item.get(axis, "nan")) for axis in ("x", "y", "z")]
                    for item in mesh_node.findall("./m:vertices/m:vertex", namespace)
                ],
                dtype=float,
            )
            faces = np.asarray(
                [
                    [int(item.get(axis, "-1")) for axis in ("v1", "v2", "v3")]
                    for item in mesh_node.findall("./m:triangles/m:triangle", namespace)
                ],
                dtype=np.int64,
            )
        except (TypeError, ValueError) as exc:
            raise Project3MFError(f"invalid mesh values in {part_path}#{object_id}") from exc
        if vertices.ndim != 2 or vertices.shape[1:] != (3,) or not np.isfinite(vertices).all():
            raise Project3MFError(f"invalid vertices in {part_path}#{object_id}")
        if faces.ndim != 2 or faces.shape[1:] != (3,) or len(faces) == 0:
            raise Project3MFError(f"empty or invalid triangles in {part_path}#{object_id}")
        if faces.min() < 0 or faces.max() >= len(vertices):
            raise Project3MFError(f"triangle index outside vertex array in {part_path}#{object_id}")
        budget["triangles"] += len(faces)
        budget["meshes"] += 1
        if budget["triangles"] > MAX_STL_TRIANGLES:
            raise Project3MFError("3MF geometry exceeds the 8,000,000 triangle safety limit")
        if budget["meshes"] > MAX_PRINTABLE_OBJECTS * 4:
            raise Project3MFError("3MF resolves to too many component meshes")
        vertices = vertices * _unit_scale(root)
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
        mesh.apply_transform(transform)
        meshes.append(mesh)
    for component in obj.findall("./m:components/m:component", namespace):
        child_id = component.get("objectid")
        if not child_id:
            raise Project3MFError(f"component without objectid in {part_path}#{object_id}")
        child_path = component.get(f"{{{PRODUCTION_NS}}}path") or part_path
        child_transform = transform @ _parse_transform(component.get("transform"))
        meshes.extend(
            _resolve_object_meshes(
                archive,
                child_path,
                child_id,
                child_transform,
                cache,
                stack,
                depth=depth + 1,
                budget=budget,
            )
        )
    stack.remove(key)
    if not meshes:
        raise Project3MFError(f"3MF object has no mesh or components: {part_path}#{object_id}")
    return meshes


def extract_printable_stl(
    project: str | Path,
    output: str | Path,
    *,
    plate: int = 1,
    object_output_dir: str | Path | None = None,
) -> ExtractedProjectGeometry:
    """Extract a plate as both a verified combined STL and per-build-item STLs."""
    source_path = Path(project).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if source_path.suffix.lower() != ".3mf" or not source_path.is_file():
        raise Project3MFError("3MF extraction requires an existing .3mf file")
    if output_path.suffix.lower() != ".stl":
        raise Project3MFError("extracted geometry output must use the .stl extension")
    if output_path.exists() or output_path == source_path:
        raise Project3MFError(f"extracted STL output already exists: {output_path}")
    if not output_path.parent.is_dir():
        raise Project3MFError(f"extracted STL parent not found: {output_path.parent}")
    if plate < 1:
        raise Project3MFError("plate must be a positive one-based index")
    objects_dir = (
        Path(object_output_dir).expanduser().resolve()
        if object_output_dir is not None
        else output_path.parent / f"{output_path.stem}.objects"
    )
    if objects_dir.exists():
        raise Project3MFError(f"object extraction output already exists: {objects_dir}")
    if not objects_dir.parent.is_dir():
        raise Project3MFError(f"object extraction parent not found: {objects_dir.parent}")
    source_hash = _sha256(source_path)
    object_payloads: list[tuple[int, str, str, trimesh.Trimesh, tuple[float, ...]]] = []
    try:
        with zipfile.ZipFile(source_path) as archive:
            validate_zip_archive(archive)
            root = _load_model_root(archive, MODEL_PATH, {})
            namespace = {"m": CORE_NS}
            requested_ids = _plate_object_ids(archive, plate)
            names_by_id = _object_names(archive)
            build_items = [
                item
                for item in root.findall("./m:build/m:item", namespace)
                if item.get("printable", "1") != "0"
                and (requested_ids is None or item.get("objectid") in requested_ids)
            ]
            if not build_items:
                raise Project3MFError(f"3MF plate {plate} has no printable build items")
            if len(build_items) > MAX_PRINTABLE_OBJECTS:
                raise Project3MFError(
                    f"3MF has too many printable objects: {len(build_items)}"
                )
            cache = {MODEL_PATH: root}
            mesh_budget = {"triangles": 0, "meshes": 0}
            meshes: list[trimesh.Trimesh] = []
            names: list[str] = []
            for instance_index, item in enumerate(build_items, start=1):
                object_id = item.get("objectid")
                if not object_id:
                    raise Project3MFError("3MF build item has no objectid")
                name = names_by_id.get(object_id, f"object-{object_id}")
                names.append(name)
                transform = _parse_transform(item.get("transform"))
                item_meshes = _resolve_object_meshes(
                    archive,
                    MODEL_PATH,
                    object_id,
                    transform,
                    cache,
                    set(),
                    budget=mesh_budget,
                )
                item_mesh = trimesh.util.concatenate(item_meshes)
                item_mesh.merge_vertices()
                item_mesh.remove_unreferenced_vertices()
                if len(item_mesh.faces) == 0 or not np.isfinite(item_mesh.vertices).all():
                    raise Project3MFError(f"printable object {name!r} has invalid geometry")
                meshes.append(item_mesh)
                object_payloads.append(
                    (
                        instance_index,
                        object_id,
                        name,
                        item_mesh,
                        tuple(float(value) for value in transform.reshape(-1)),
                    )
                )
    except (zipfile.BadZipFile, UnsafeInputError) as exc:
        raise Project3MFError(f"invalid 3MF archive: {source_path}") from exc
    merged = trimesh.util.concatenate(meshes)
    merged.merge_vertices()
    merged.remove_unreferenced_vertices()
    if len(merged.faces) == 0 or not np.isfinite(merged.vertices).all():
        raise Project3MFError("extracted 3MF geometry is empty or non-finite")
    payload = merged.export(file_type="stl")
    if not isinstance(payload, bytes):
        raise Project3MFError("cannot serialize extracted geometry as binary STL")
    objects_dir.mkdir()
    extracted_objects: list[ExtractedObjectGeometry] = []
    try:
        for instance_index, object_id, name, item_mesh, transform in object_payloads:
            object_path = objects_dir / f"object-{instance_index:03d}-id-{object_id}.stl"
            object_bytes = item_mesh.export(file_type="stl")
            if not isinstance(object_bytes, bytes):
                raise Project3MFError(f"cannot serialize printable object {name!r}")
            atomic_write_new_bytes(object_path, object_bytes)
            verification = trimesh.load_mesh(object_path, process=False)
            if (
                not isinstance(verification, trimesh.Trimesh)
                or len(verification.faces) != len(item_mesh.faces)
            ):
                raise Project3MFError(
                    f"object extraction verification changed {name!r}"
                )
            extracted_objects.append(
                ExtractedObjectGeometry(
                    instance_index=instance_index,
                    object_id=object_id,
                    name=name,
                    output_stl_path=object_path,
                    triangle_count=len(item_mesh.faces),
                    geometry_signature=_geometry_signature(item_mesh),
                    output_sha256=_sha256(object_path),
                    transform=transform,
                )
            )
        atomic_write_new_bytes(output_path, payload)
    except Exception:
        output_path.unlink(missing_ok=True)
        for child in objects_dir.glob("*"):
            child.unlink(missing_ok=True)
        objects_dir.rmdir()
        raise
    try:
        verification = trimesh.load_mesh(output_path, process=False)
        if not isinstance(verification, trimesh.Trimesh) or len(verification.faces) != len(merged.faces):
            raise Project3MFError("extracted STL verification changed the triangle count")
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    if _sha256(source_path) != source_hash:
        output_path.unlink(missing_ok=True)
        raise Project3MFError("source 3MF changed during geometry extraction")
    return ExtractedProjectGeometry(
        source_path=source_path,
        output_stl_path=output_path,
        plate=plate,
        printable_object_count=len(build_items),
        object_names=tuple(names),
        triangle_count=len(merged.faces),
        source_sha256=source_hash,
        output_sha256=_sha256(output_path),
        objects=tuple(extracted_objects),
    )


def extract_stl_objects(
    stl: str | Path,
    output: str | Path,
    *,
    object_output_dir: str | Path | None = None,
) -> ExtractedProjectGeometry:
    """Separate meaningful disconnected STL bodies without losing tiny shells."""
    source_path = Path(stl).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if source_path.suffix.lower() != ".stl" or not source_path.is_file():
        raise Project3MFError("STL object extraction requires an existing STL")
    if output_path.suffix.lower() != ".stl" or output_path.exists():
        raise Project3MFError(f"invalid or existing extracted STL output: {output_path}")
    if not output_path.parent.is_dir():
        raise Project3MFError(f"extracted STL parent not found: {output_path.parent}")
    objects_dir = (
        Path(object_output_dir).expanduser().resolve()
        if object_output_dir is not None
        else output_path.parent / f"{output_path.stem}.objects"
    )
    if objects_dir.exists() or not objects_dir.parent.is_dir():
        raise Project3MFError(f"invalid STL object output directory: {objects_dir}")
    source_hash = _sha256(source_path)
    try:
        validate_stl_file(source_path)
    except UnsafeInputError as exc:
        raise Project3MFError(f"unsafe or malformed STL: {exc}") from exc
    loaded = trimesh.load_mesh(source_path, process=False)
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise Project3MFError("STL did not load as one non-empty mesh")
    mesh = loaded.copy()
    mesh.merge_vertices()
    mesh.remove_unreferenced_vertices()
    if not np.isfinite(mesh.vertices).all():
        raise Project3MFError("STL contains non-finite vertices")
    faces = np.asarray(mesh.faces, dtype=np.int64)
    edge_vertices = np.sort(
        np.vstack((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]])),
        axis=1,
    )
    edge_faces = np.tile(np.arange(len(faces), dtype=np.int64), 3)
    order = np.lexsort((edge_vertices[:, 1], edge_vertices[:, 0]))
    sorted_edges = edge_vertices[order]
    sorted_faces = edge_faces[order]
    shared = np.all(sorted_edges[1:] == sorted_edges[:-1], axis=1)
    adjacency = np.column_stack((sorted_faces[:-1][shared], sorted_faces[1:][shared]))
    parents = np.arange(len(faces), dtype=np.int64)

    def find(index: int) -> int:
        while int(parents[index]) != index:
            parents[index] = parents[int(parents[index])]
            index = int(parents[index])
        return index

    for left, right in adjacency:
        left_root = find(int(left))
        right_root = find(int(right))
        if left_root != right_root:
            parents[right_root] = left_root
    component_faces: dict[int, list[int]] = {}
    for face_index in range(len(faces)):
        component_faces.setdefault(find(face_index), []).append(face_index)
    components: list[trimesh.Trimesh] = []
    for indices in component_faces.values():
        component = trimesh.Trimesh(
            vertices=np.asarray(mesh.vertices).copy(),
            faces=faces[np.asarray(indices, dtype=np.int64)].copy(),
            process=False,
        )
        component.remove_unreferenced_vertices()
        components.append(component)
    total_faces = max(1, len(mesh.faces))
    global_diagonal = max(float(np.linalg.norm(mesh.extents)), 1e-6)
    major_indices = [
        index
        for index, component in enumerate(components)
        if len(component.faces) >= max(12, int(total_faces * 0.02))
        or float(np.linalg.norm(component.extents)) >= global_diagonal * 0.12
    ]
    # One significant shell plus decorative fragments is one model. With two or
    # more significant shells, attach every tiny shell to its nearest major body.
    if len(major_indices) < 2:
        groups = [components]
    else:
        groups = [[components[index]] for index in major_indices]
        major_centres = [components[index].bounds.mean(axis=0) for index in major_indices]
        for index, component in enumerate(components):
            if index in major_indices:
                continue
            centre = component.bounds.mean(axis=0)
            nearest = int(
                np.argmin(
                    [float(np.linalg.norm(centre - candidate)) for candidate in major_centres]
                )
            )
            groups[nearest].append(component)
    object_meshes: list[trimesh.Trimesh] = []
    for group in groups:
        item = trimesh.util.concatenate(group)
        item.merge_vertices()
        item.remove_unreferenced_vertices()
        object_meshes.append(item)
    object_meshes.sort(key=lambda item: tuple(float(v) for v in item.bounds[0]))

    objects_dir.mkdir()
    extracted_objects: list[ExtractedObjectGeometry] = []
    try:
        combined_payload = mesh.export(file_type="stl")
        if not isinstance(combined_payload, bytes):
            raise Project3MFError("cannot serialize combined STL geometry")
        atomic_write_new_bytes(output_path, combined_payload)
        for index, item_mesh in enumerate(object_meshes, start=1):
            object_path = objects_dir / f"object-{index:03d}-id-{index}.stl"
            payload = item_mesh.export(file_type="stl")
            if not isinstance(payload, bytes):
                raise Project3MFError(f"cannot serialize STL object {index}")
            atomic_write_new_bytes(object_path, payload)
            name = (
                source_path.name
                if len(object_meshes) == 1
                else f"{source_path.stem} — модель {index}"
            )
            extracted_objects.append(
                ExtractedObjectGeometry(
                    instance_index=index,
                    object_id=str(index),
                    name=name,
                    output_stl_path=object_path,
                    triangle_count=len(item_mesh.faces),
                    geometry_signature=_geometry_signature(item_mesh),
                    output_sha256=_sha256(object_path),
                    transform=tuple(float(value) for value in np.eye(4).reshape(-1)),
                )
            )
    except Exception:
        output_path.unlink(missing_ok=True)
        for child in objects_dir.glob("*"):
            child.unlink(missing_ok=True)
        objects_dir.rmdir()
        raise
    if _sha256(source_path) != source_hash:
        raise Project3MFError("source STL changed during object extraction")
    return ExtractedProjectGeometry(
        source_path=source_path,
        output_stl_path=output_path,
        plate=1,
        printable_object_count=len(extracted_objects),
        object_names=tuple(item.name for item in extracted_objects),
        triangle_count=len(mesh.faces),
        source_sha256=source_hash,
        output_sha256=_sha256(output_path),
        objects=tuple(extracted_objects),
    )


def rebuild_extracted_geometry(
    extracted: ExtractedProjectGeometry,
    replacements: dict[int, Path],
    combined_output: str | Path,
) -> ExtractedProjectGeometry:
    """Rebuild extracted object metadata after verified per-object mesh repair."""
    output_path = Path(combined_output).expanduser().resolve()
    if output_path.suffix.lower() != ".stl" or output_path.exists():
        raise Project3MFError(f"invalid or existing rebuilt STL: {output_path}")
    if not output_path.parent.is_dir():
        raise Project3MFError(f"rebuilt STL parent not found: {output_path.parent}")
    rebuilt_objects: list[ExtractedObjectGeometry] = []
    meshes: list[trimesh.Trimesh] = []
    for item in extracted.objects:
        object_path = replacements.get(item.instance_index, item.output_stl_path).resolve()
        loaded = trimesh.load_mesh(object_path, process=False)
        if not isinstance(loaded, trimesh.Trimesh) or not len(loaded.faces):
            raise Project3MFError(f"cannot load repaired object {item.name!r}")
        mesh = loaded.copy()
        mesh.merge_vertices()
        mesh.remove_unreferenced_vertices()
        meshes.append(mesh)
        rebuilt_objects.append(
            ExtractedObjectGeometry(
                instance_index=item.instance_index,
                object_id=item.object_id,
                name=item.name,
                output_stl_path=object_path,
                triangle_count=len(mesh.faces),
                geometry_signature=_geometry_signature(mesh),
                output_sha256=_sha256(object_path),
                transform=item.transform,
            )
        )
    if not meshes:
        raise Project3MFError("rebuilt project has no printable objects")
    combined = trimesh.util.concatenate(meshes)
    combined.export(output_path, file_type="stl")
    return ExtractedProjectGeometry(
        source_path=extracted.source_path,
        output_stl_path=output_path,
        plate=extracted.plate,
        printable_object_count=len(rebuilt_objects),
        object_names=tuple(item.name for item in rebuilt_objects),
        triangle_count=sum(item.triangle_count for item in rebuilt_objects),
        source_sha256=extracted.source_sha256,
        output_sha256=_sha256(output_path),
        objects=tuple(rebuilt_objects),
    )


def _support_settings(settings: dict[str, Any], mode: str) -> None:
    if mode not in {"none", "normal", "tree"}:
        raise Project3MFError("support mode must be none, normal or tree")
    settings["enable_support"] = "0" if mode == "none" else "1"
    if mode != "none":
        settings["support_type"] = f"{mode}(auto)"
    different = settings.get("different_settings_to_system", [""])
    if not isinstance(different, list):
        different = [str(different)]
    if not different:
        different.append("")
    changed = {item for item in str(different[0]).split(";") if item}
    changed.add("enable_support")
    if mode != "none":
        changed.add("support_type")
    else:
        changed.discard("support_type")
    different[0] = ";".join(sorted(changed))
    settings["different_settings_to_system"] = different


def _apply_recommended_settings(
    settings: dict[str, Any], recommendation: PrintSettings
) -> None:
    """Apply analyzer recommendations while retaining the verified machine profile."""

    def set_array(key: str, value: int | str) -> None:
        current = settings.get(key, [])
        count = len(current) if isinstance(current, list) and current else 1
        settings[key] = [str(value)] * count

    def set_preserving_shape(key: str, value: int | str) -> None:
        current = settings.get(key)
        if isinstance(current, list):
            set_array(key, value)
        else:
            settings[key] = str(value)

    overrides = {
        "layer_height": f"{recommendation.layer_height_mm:g}",
        "initial_layer_print_height": f"{recommendation.initial_layer_height_mm:g}",
        "line_width": f"{recommendation.line_width_mm:g}",
        "wall_loops": str(recommendation.wall_loops),
        "top_shell_layers": str(recommendation.top_layers),
        "bottom_shell_layers": str(recommendation.bottom_layers),
        "brim_type": "auto_brim" if recommendation.brim else "no_brim",
        "sparse_infill_density": f"{recommendation.sparse_infill_percent}%",
        "sparse_infill_pattern": recommendation.sparse_infill_pattern,
        "top_surface_pattern": recommendation.top_surface_pattern,
        "top_surface_line_width": f"{recommendation.top_surface_line_width_mm:g}",
        "ironing_type": "topmost" if recommendation.ironing_enabled else "no ironing",
        "top_surface_density": f"{recommendation.top_surface_density_percent}%",
        "top_shell_thickness": f"{recommendation.top_shell_thickness_mm:g}",
        "seam_placement_away_from_overhangs": (
            "1" if recommendation.seam_placement_away_from_overhangs else "0"
        ),
        "seam_position": recommendation.seam_position,
        "seam_slope_type": recommendation.scarf_seam_type,
        "override_filament_scarf_seam_setting": (
            "1" if recommendation.override_filament_scarf_seam else "0"
        ),
        "wall_generator": recommendation.wall_generator,
        "support_top_z_distance": f"{recommendation.support_top_z_distance_mm:g}",
        "support_bottom_z_distance": f"{recommendation.support_bottom_z_distance_mm:g}",
        "support_object_xy_distance": f"{recommendation.support_object_xy_distance_mm:g}",
        "support_interface_top_layers": str(recommendation.support_interface_top_layers),
        "support_interface_bottom_layers": str(
            recommendation.support_interface_bottom_layers
        ),
        "support_interface_spacing": f"{recommendation.support_interface_spacing_mm:g}",
        "detect_thin_wall": "1" if recommendation.detect_thin_wall else "0",
        "detect_floating_vertical_shell": (
            "1" if recommendation.detect_floating_vertical_shell else "0"
        ),
        "bridge_no_support": "1" if recommendation.bridge_no_support else "0",
        "infill_combination": "1" if recommendation.infill_combination else "0",
        "reduce_crossing_wall": "1" if recommendation.reduce_crossing_wall else "0",
        "avoid_crossing_wall_includes_support": (
            "1" if recommendation.avoid_crossing_wall_includes_support else "0"
        ),
        "reduce_infill_retraction_mode": recommendation.reduce_infill_retraction_mode,
        "elefant_foot_compensation": f"{recommendation.elephant_foot_compensation_mm:g}",
    }
    settings.update(overrides)
    dynamic_overrides = {
        "outer_wall_speed": recommendation.outer_wall_speed_mm_s,
        "inner_wall_speed": recommendation.inner_wall_speed_mm_s,
        "sparse_infill_speed": recommendation.sparse_infill_speed_mm_s,
        "internal_solid_infill_speed": recommendation.internal_solid_infill_speed_mm_s,
        "top_surface_speed": recommendation.top_surface_speed_mm_s,
        "support_speed": recommendation.support_speed_mm_s,
        "support_interface_speed": recommendation.support_interface_speed_mm_s,
        "bridge_speed": recommendation.bridge_speed_mm_s,
        "initial_layer_speed": recommendation.initial_layer_speed_mm_s,
        "travel_speed": recommendation.travel_speed_mm_s,
        "default_acceleration": recommendation.default_acceleration_mm_s2,
        "outer_wall_acceleration": recommendation.outer_wall_acceleration_mm_s2,
        "top_surface_acceleration": recommendation.top_surface_acceleration_mm_s2,
        "initial_layer_acceleration": recommendation.initial_layer_acceleration_mm_s2,
        "travel_acceleration": recommendation.travel_acceleration_mm_s2,
        "small_perimeter_speed": f"{recommendation.small_perimeter_speed_percent}%",
        "small_perimeter_threshold": f"{recommendation.small_perimeter_threshold_mm:g}",
        "slow_down_layer_time": recommendation.slow_down_layer_time_s,
        "slow_down_min_speed": recommendation.slow_down_min_speed_mm_s,
    }
    for key, value in dynamic_overrides.items():
        set_preserving_shape(key, value)
    set_array("nozzle_temperature", recommendation.nozzle_temperature_c)
    set_array("nozzle_temperature_initial_layer", recommendation.nozzle_temperature_c)
    set_array("fan_max_speed", recommendation.fan_percent)
    set_array("fan_min_speed", recommendation.fan_percent)
    set_array("filament_max_volumetric_speed", f"{recommendation.max_volumetric_speed_mm3_s:g}")
    set_array("filament_flow_ratio", f"{recommendation.filament_flow_ratio:g}")
    set_array("retraction_length", f"{recommendation.retraction_length_mm:g}")
    set_array("retraction_speed", f"{recommendation.retraction_speed_mm_s:g}")
    set_array("wipe", "1" if recommendation.wipe_enabled else "0")
    set_array("wipe_distance", f"{recommendation.wipe_distance_mm:g}")
    set_array(
        "enable_pressure_advance",
        "1" if recommendation.enable_pressure_advance else "0",
    )
    set_array("pressure_advance", f"{recommendation.pressure_advance_k:g}")
    bed_type = str(settings.get("curr_bed_type", "")).casefold()
    bed_key = (
        "textured_plate_temp"
        if "textured" in bed_type
        else "cool_plate_temp"
        if "cool" in bed_type
        else "eng_plate_temp"
        if "engineering" in bed_type
        else "hot_plate_temp"
    )
    set_array(bed_key, recommendation.bed_temperature_c)
    set_array(f"{bed_key}_initial_layer", recommendation.bed_temperature_c)
    changed_keys = {
        *overrides,
        *dynamic_overrides,
        "nozzle_temperature",
        "nozzle_temperature_initial_layer",
        "fan_max_speed",
        "fan_min_speed",
        "filament_max_volumetric_speed",
        "filament_flow_ratio",
        "enable_pressure_advance",
        "pressure_advance",
        "retraction_length",
        "retraction_speed",
        "wipe",
        "wipe_distance",
        bed_key,
        f"{bed_key}_initial_layer",
    }
    different = settings.get("different_settings_to_system", [""])
    if not isinstance(different, list):
        different = [str(different)]
    if not different:
        different.append("")
    changed = {item for item in str(different[0]).split(";") if item}
    changed.update(changed_keys)
    different[0] = ";".join(sorted(changed))
    settings["different_settings_to_system"] = different


_OBJECT_PROCESS_KEYS = frozenset(
    {
        "layer_height",
        "line_width",
        "wall_loops",
        "top_shell_layers",
        "bottom_shell_layers",
        "brim_type",
        "sparse_infill_density",
        "sparse_infill_pattern",
        "top_surface_pattern",
        "top_surface_line_width",
        "ironing_type",
        "top_surface_density",
        "top_shell_thickness",
        "seam_placement_away_from_overhangs",
        "seam_position",
        "seam_slope_type",
        "override_filament_scarf_seam_setting",
        "wall_generator",
        "support_top_z_distance",
        "support_bottom_z_distance",
        "support_object_xy_distance",
        "support_interface_top_layers",
        "support_interface_bottom_layers",
        "support_interface_spacing",
        "detect_thin_wall",
        "detect_floating_vertical_shell",
        "bridge_no_support",
        "infill_combination",
        "reduce_crossing_wall",
        "avoid_crossing_wall_includes_support",
        "reduce_infill_retraction_mode",
        "elefant_foot_compensation",
        "outer_wall_speed",
        "inner_wall_speed",
        "sparse_infill_speed",
        "internal_solid_infill_speed",
        "top_surface_speed",
        "support_speed",
        "support_interface_speed",
        "bridge_speed",
        "small_perimeter_speed",
        "small_perimeter_threshold",
    }
)


def _object_process_values(recommendation: PrintSettings) -> dict[str, str]:
    """Return only settings that Bambu Studio accepts at object scope."""
    expanded: dict[str, Any] = {}
    _apply_recommended_settings(expanded, recommendation)
    result: dict[str, str] = {}
    for key in _OBJECT_PROCESS_KEYS:
        value = expanded.get(key)
        if value is None or isinstance(value, (dict, list, tuple)):
            continue
        result[key] = str(value)
    return result


def _replace_mesh(mesh_node: ET.Element, mesh: trimesh.Trimesh) -> None:
    mesh_node.clear()
    vertices = ET.SubElement(mesh_node, f"{{{CORE_NS}}}vertices")
    for x, y, z in mesh.vertices:
        ET.SubElement(
            vertices,
            f"{{{CORE_NS}}}vertex",
            {"x": f"{x:.9g}", "y": f"{y:.9g}", "z": f"{z:.9g}"},
        )
    triangles = ET.SubElement(mesh_node, f"{{{CORE_NS}}}triangles")
    for v1, v2, v3 in mesh.faces:
        ET.SubElement(
            triangles,
            f"{{{CORE_NS}}}triangle",
            {"v1": str(v1), "v2": str(v2), "v3": str(v3)},
        )


def _is_geometry_specific_member(name: str) -> bool:
    if name in GEOMETRY_SPECIFIC_MEMBERS or name.startswith("Auxiliaries/"):
        return True
    filename = PurePosixPath(name).name
    return name.startswith("Metadata/") and (
        filename.startswith(("plate_", "plate_no_light_", "top_", "pick_"))
        and filename.endswith((".png", ".json", ".gcode"))
    )


def _set_metadata_value(parent: ET.Element, key: str, value: str) -> None:
    metadata = next(
        (item for item in parent.findall("./metadata") if item.get("key") == key),
        None,
    )
    if metadata is None:
        metadata = ET.SubElement(parent, "metadata", {"key": key})
    metadata.set("value", value)


_LOCAL_RANGE_KEYS = frozenset(
    {
        "bridge_speed",
        "detect_thin_wall",
        "inner_wall_speed",
        "layer_height",
        "outer_wall_speed",
        "sparse_infill_speed",
        "top_shell_layers",
        "top_surface_speed",
    }
)


def _local_modifier_xml(plan: LocalModifierPlan | None) -> bytes | None:
    """Serialize Bambu Studio's native per-height process configuration."""
    if plan is None or not plan.ranges:
        return None
    root = ET.Element("objects")
    obj = ET.SubElement(root, "object", {"id": "1"})
    previous_max = -math.inf
    for item in plan.ranges:
        minimum = float(item.z_min_mm)
        maximum = float(item.z_max_mm)
        if not (math.isfinite(minimum) and math.isfinite(maximum) and maximum > minimum):
            raise Project3MFError(f"invalid local modifier Z range: {minimum}..{maximum}")
        if minimum < previous_max - 1e-5:
            raise Project3MFError("local modifier Z ranges overlap")
        previous_max = maximum
        range_node = ET.SubElement(
            obj,
            "range",
            {"min_z": f"{minimum:.9g}", "max_z": f"{maximum:.9g}"},
        )
        for key, value in sorted(item.settings.items()):
            if key not in _LOCAL_RANGE_KEYS:
                continue
            option = ET.SubElement(range_node, "option", {"opt_key": key})
            option.text = str(value)
    if not obj.findall("./range"):
        return None
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _local_modifier_xml_by_object(
    plans: dict[str, LocalModifierPlan | None],
) -> bytes | None:
    """Serialize independent height ranges for several native 3MF objects."""
    root = ET.Element("objects")
    for object_id, plan in plans.items():
        if plan is None or not plan.ranges:
            continue
        obj = ET.SubElement(root, "object", {"id": str(object_id)})
        previous_max = -math.inf
        for item in plan.ranges:
            minimum = float(item.z_min_mm)
            maximum = float(item.z_max_mm)
            if not (
                math.isfinite(minimum)
                and math.isfinite(maximum)
                and maximum > minimum
            ):
                raise Project3MFError(
                    f"invalid local modifier Z range for object {object_id}: "
                    f"{minimum}..{maximum}"
                )
            if minimum < previous_max - 1e-5:
                raise Project3MFError(
                    f"local modifier Z ranges overlap for object {object_id}"
                )
            previous_max = maximum
            range_node = ET.SubElement(
                obj,
                "range",
                {"min_z": f"{minimum:.9g}", "max_z": f"{maximum:.9g}"},
            )
            for key, value in sorted(item.settings.items()):
                if key not in _LOCAL_RANGE_KEYS:
                    continue
                option = ET.SubElement(range_node, "option", {"opt_key": key})
                option.text = str(value)
        if not obj.findall("./range"):
            root.remove(obj)
    if not root.findall("./object"):
        return None
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _read_local_modifier_ranges(archive: zipfile.ZipFile) -> tuple[tuple[float, float, dict[str, str]], ...]:
    if LAYER_CONFIG_RANGES_PATH not in archive.namelist():
        return ()
    root = read_zip_xml(archive, LAYER_CONFIG_RANGES_PATH)
    result: list[tuple[float, float, dict[str, str]]] = []
    for range_node in root.findall("./object/range"):
        settings = {
            str(option.get("opt_key")): str(option.text or "").strip()
            for option in range_node.findall("./option")
            if option.get("opt_key")
        }
        result.append(
            (
                float(range_node.get("min_z", "nan")),
                float(range_node.get("max_z", "nan")),
                settings,
            )
        )
    return tuple(result)


def create_bambu_project_from_3mf_objects(
    source: str | Path,
    template: str | Path,
    output: str | Path,
    *,
    object_configurations: tuple[ObjectPrintConfiguration, ...],
    plate: int = 1,
) -> PreparedMultiObjectProject:
    """Preserve a source plate while applying independent native object settings."""
    source_path = Path(source).expanduser().resolve()
    template_path = Path(template).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if source_path.suffix.lower() != ".3mf" or not source_path.is_file():
        raise Project3MFError("multi-object preparation requires an existing source 3MF")
    if template_path.suffix.lower() != ".3mf" or not template_path.is_file():
        raise Project3MFError("multi-object preparation requires an existing profile 3MF")
    if output_path.suffix.lower() != ".3mf":
        raise Project3MFError("prepared project output must use the .3mf extension")
    if output_path.exists() or output_path in {source_path, template_path}:
        raise Project3MFError(f"prepared project output already exists: {output_path}")
    if not output_path.parent.is_dir():
        raise Project3MFError(f"prepared project parent not found: {output_path.parent}")
    if plate < 1:
        raise Project3MFError("plate must be a positive one-based index")
    if not object_configurations:
        raise Project3MFError("at least one object configuration is required")
    configurations = {item.object_id: item for item in object_configurations}
    if len(configurations) != len(object_configurations):
        raise Project3MFError(
            "duplicate source object IDs cannot receive conflicting independent settings"
        )
    for item in object_configurations:
        if item.support_mode not in {"none", "normal", "tree"}:
            raise Project3MFError(
                f"invalid support mode for object {item.object_id}: {item.support_mode}"
            )

    source_hash = _sha256(source_path)
    template_hash = _sha256(template_path)
    try:
        with zipfile.ZipFile(source_path) as source_zip, zipfile.ZipFile(
            template_path
        ) as template_zip:
            validate_zip_archive(source_zip)
            validate_zip_archive(template_zip)
            main = read_zip_xml(source_zip, MODEL_PATH)
            model_settings = read_zip_xml(source_zip, MODEL_SETTINGS_PATH)
            settings = read_zip_json(template_zip, PROJECT_SETTINGS_PATH)
            if not isinstance(settings, dict):
                raise Project3MFError("Bambu profile settings must be an object")
            namespace = {"m": CORE_NS}
            requested_ids = _plate_object_ids(source_zip, plate)
            build = main.find("./m:build", namespace)
            if build is None:
                raise Project3MFError("source 3MF has no build section")
            selected_items = [
                item
                for item in list(build)
                if item.get("printable", "1") != "0"
                and (requested_ids is None or item.get("objectid") in requested_ids)
            ]
            if not selected_items:
                raise Project3MFError(f"3MF plate {plate} has no printable objects")
            selected_ids = {str(item.get("objectid")) for item in selected_items}
            if selected_ids != set(configurations):
                missing = sorted(selected_ids - set(configurations))
                extra = sorted(set(configurations) - selected_ids)
                raise Project3MFError(
                    "object configurations do not match the selected plate"
                    + (f"; missing: {', '.join(missing)}" if missing else "")
                    + (f"; extra: {', '.join(extra)}" if extra else "")
                )
            for item in list(build):
                if item not in selected_items:
                    build.remove(item)
                else:
                    item.set("printable", "1")

            object_nodes = {
                str(item.get("id")): item
                for item in model_settings.findall("./object")
                if item.get("id")
            }
            for object_id in selected_ids:
                if object_id not in object_nodes:
                    raise Project3MFError(
                        f"source 3MF lacks Bambu settings for object {object_id}"
                    )
            for object_id, node in list(object_nodes.items()):
                if object_id not in selected_ids:
                    model_settings.remove(node)
                    continue
                configuration = configurations[object_id]
                _set_metadata_value(node, "name", configuration.name)
                _set_metadata_value(node, "extruder", "1")
                values = _object_process_values(configuration.settings)
                values["enable_support"] = (
                    "0" if configuration.support_mode == "none" else "1"
                )
                if configuration.support_mode != "none":
                    values["support_type"] = f"{configuration.support_mode}(auto)"
                for key, value in sorted(values.items()):
                    _set_metadata_value(node, key, value)
                for metadata in node.findall(".//metadata"):
                    if metadata.get("key") == "extruder":
                        metadata.set("value", "1")

            plates = model_settings.findall("./plate")
            if not plates or plate > len(plates):
                raise Project3MFError(
                    f"source 3MF has {len(plates)} Bambu plate record(s), requested {plate}"
                )
            selected_plate = plates[plate - 1]
            for candidate in plates:
                if candidate is not selected_plate:
                    model_settings.remove(candidate)
            for instance in list(selected_plate.findall("./model_instance")):
                object_id = next(
                    (
                        metadata.get("value")
                        for metadata in instance.findall("./metadata")
                        if metadata.get("key") == "object_id"
                    ),
                    None,
                )
                if object_id not in selected_ids:
                    selected_plate.remove(instance)
            for metadata in list(selected_plate.findall("./metadata")):
                if metadata.get("key") in {
                    "thumbnail_file",
                    "thumbnail_no_light_file",
                    "top_file",
                    "pick_file",
                }:
                    selected_plate.remove(metadata)
            assemble = model_settings.find("./assemble")
            if assemble is not None:
                for item in list(assemble):
                    if item.get("object_id") not in selected_ids:
                        assemble.remove(item)

            baseline = object_configurations[0]
            _apply_recommended_settings(settings, baseline.settings)
            _support_settings(settings, "none")
            settings["filament_colour"] = [
                str((settings.get("filament_colour") or ["#000000"])[0])
            ]
            settings["filament_type"] = [
                str((settings.get("filament_type") or ["PLA"])[0])
            ]
            local_ranges = _local_modifier_xml_by_object(
                {
                    item.object_id: item.local_modifier_plan
                    for item in object_configurations
                }
            )

            replacements: dict[str, bytes] = {
                MODEL_PATH: ET.tostring(main, encoding="utf-8", xml_declaration=True),
                MODEL_SETTINGS_PATH: ET.tostring(
                    model_settings, encoding="utf-8", xml_declaration=True
                ),
                PROJECT_SETTINGS_PATH: json.dumps(
                    settings, ensure_ascii=False, indent=2
                ).encode("utf-8"),
            }
            if local_ranges is not None:
                replacements[LAYER_CONFIG_RANGES_PATH] = local_ranges

            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as output_zip:
                written: set[str] = set()
                for entry in source_zip.infolist():
                    if _is_geometry_specific_member(entry.filename):
                        continue
                    content = replacements.get(entry.filename, read_zip_member(source_zip, entry.filename))
                    # Keep source geometry and placement, but collapse accidental
                    # per-face paint/extruder assignments to the selected filament.
                    if (
                        entry.filename.startswith("3D/")
                        and entry.filename.endswith(".model")
                    ):
                        root = parse_xml_bytes(content, context=entry.filename)
                        for triangle in root.findall(".//m:triangle", namespace):
                            for attribute in list(triangle.attrib):
                                local_name = attribute.rsplit("}", 1)[-1]
                                if local_name in {"paint_color", "p1", "p2", "p3"}:
                                    triangle.attrib.pop(attribute, None)
                        content = ET.tostring(
                            root, encoding="utf-8", xml_declaration=True
                        )
                    output_zip.writestr(entry, content)
                    written.add(entry.filename)
                for name, content in replacements.items():
                    if name not in written:
                        output_zip.writestr(name, content)
    except (KeyError, zipfile.BadZipFile, ET.ParseError, json.JSONDecodeError, UnsafeInputError) as exc:
        raise Project3MFError(f"cannot prepare multi-object Bambu 3MF: {exc}") from exc

    atomic_write_new_bytes(output_path, buffer.getvalue())
    if _sha256(source_path) != source_hash or _sha256(template_path) != template_hash:
        output_path.unlink(missing_ok=True)
        raise Project3MFError(
            "source 3MF or profile changed while preparing the multi-object project"
        )
    extracted_path = output_path.parent / f".{output_path.stem}.verification.stl"
    try:
        verification = extract_printable_stl(
            output_path,
            extracted_path,
            plate=1,
            object_output_dir=output_path.parent / f".{output_path.stem}.verification.objects",
        )
        triangle_count = verification.triangle_count
        names = verification.object_names
    finally:
        extracted_path.unlink(missing_ok=True)
        verification_dir = output_path.parent / f".{output_path.stem}.verification.objects"
        if verification_dir.is_dir():
            for child in verification_dir.iterdir():
                child.unlink(missing_ok=True)
            verification_dir.rmdir()
    return PreparedMultiObjectProject(
        source_project_path=source_path,
        template_path=template_path,
        project_path=output_path,
        printable_object_count=len(selected_items),
        object_names=tuple(names),
        triangle_count=triangle_count,
        source_project_sha256=source_hash,
        template_sha256=template_hash,
        project_sha256=_sha256(output_path),
    )


def create_bambu_project_from_stl_objects(
    extracted: ExtractedProjectGeometry,
    template: str | Path,
    output: str | Path,
    *,
    object_configurations: tuple[ObjectPrintConfiguration, ...],
) -> PreparedMultiObjectProject:
    """Build a native multi-object Bambu project from separated STL bodies."""
    source_path = extracted.source_path.expanduser().resolve()
    template_path = Path(template).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if source_path.suffix.lower() not in {".stl", ".3mf"} or not source_path.is_file():
        raise Project3MFError("multi-body preparation requires an extracted STL or 3MF source")
    if template_path.suffix.lower() != ".3mf" or not template_path.is_file():
        raise Project3MFError("multi-body preparation requires a Bambu profile 3MF")
    if output_path.suffix.lower() != ".3mf" or output_path.exists():
        raise Project3MFError(f"invalid or existing prepared project: {output_path}")
    if not output_path.parent.is_dir():
        raise Project3MFError(f"prepared project parent not found: {output_path.parent}")
    configurations = {item.object_id: item for item in object_configurations}
    expected_ids = {item.object_id for item in extracted.objects}
    if len(configurations) != len(object_configurations) or set(configurations) != expected_ids:
        raise Project3MFError("STL object configurations do not match extracted bodies")
    source_hash = _sha256(source_path)
    template_hash = _sha256(template_path)
    meshes: list[tuple[ExtractedObjectGeometry, trimesh.Trimesh]] = []
    for item in extracted.objects:
        loaded = trimesh.load_mesh(item.output_stl_path, process=False)
        if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
            raise Project3MFError(f"cannot load extracted STL object {item.name!r}")
        object_mesh = loaded.copy()
        object_mesh.merge_vertices()
        object_mesh.remove_unreferenced_vertices()
        meshes.append((item, object_mesh))
    combined_bounds = np.asarray(
        [
            np.min([mesh.bounds[0] for _, mesh in meshes], axis=0),
            np.max([mesh.bounds[1] for _, mesh in meshes], axis=0),
        ]
    )
    translation = np.array(
        [
            128.0 - float(combined_bounds[:, 0].mean()),
            128.0 - float(combined_bounds[:, 1].mean()),
            -float(combined_bounds[0, 2]),
        ]
    )
    try:
        with zipfile.ZipFile(template_path) as template_zip:
            validate_zip_archive(template_zip)
            template_main = read_zip_xml(template_zip, MODEL_PATH)
            model_settings = read_zip_xml(template_zip, MODEL_SETTINGS_PATH)
            settings = read_zip_json(template_zip, PROJECT_SETTINGS_PATH)
            if not isinstance(settings, dict):
                raise Project3MFError("Bambu profile settings must be an object")
            prototype_plate = next(iter(model_settings.findall("./plate")), None)
            if prototype_plate is None:
                raise Project3MFError("Bambu template lacks plate metadata")

            main = ET.Element(template_main.tag, dict(template_main.attrib))
            for child in list(template_main):
                local = child.tag.rsplit("}", 1)[-1]
                if local not in {"resources", "build"}:
                    main.append(copy.deepcopy(child))
            resources = ET.SubElement(main, f"{{{CORE_NS}}}resources")
            build = ET.SubElement(main, f"{{{CORE_NS}}}build")
            for item, mesh in meshes:
                mesh.apply_translation(translation)
                object_node = ET.SubElement(
                    resources,
                    f"{{{CORE_NS}}}object",
                    {"id": item.object_id, "type": "model"},
                )
                mesh_node = ET.SubElement(object_node, f"{{{CORE_NS}}}mesh")
                _replace_mesh(mesh_node, mesh)
                ET.SubElement(
                    build,
                    f"{{{CORE_NS}}}item",
                    {
                        "objectid": item.object_id,
                        "printable": "1",
                        "transform": IDENTITY_3MF,
                    },
                )

            for node in list(model_settings.findall("./object")):
                model_settings.remove(node)
            for node in list(model_settings.findall("./plate")):
                model_settings.remove(node)
            existing_assemble = model_settings.find("./assemble")
            if existing_assemble is not None:
                model_settings.remove(existing_assemble)
            for item, mesh in meshes:
                configuration = configurations[item.object_id]
                node = ET.Element("object", {"id": item.object_id})
                _set_metadata_value(node, "name", configuration.name)
                _set_metadata_value(node, "extruder", "1")
                values = _object_process_values(configuration.settings)
                values["enable_support"] = (
                    "0" if configuration.support_mode == "none" else "1"
                )
                if configuration.support_mode != "none":
                    values["support_type"] = f"{configuration.support_mode}(auto)"
                for key, value in sorted(values.items()):
                    _set_metadata_value(node, key, value)
                part = ET.SubElement(
                    node,
                    "part",
                    {"id": item.object_id, "subtype": "normal_part"},
                )
                _set_metadata_value(part, "name", configuration.name)
                _set_metadata_value(part, "source_file", source_path.name)
                _set_metadata_value(part, "matrix", IDENTITY_4X4)
                _set_metadata_value(part, "extruder", "1")
                for key in ("source_offset_x", "source_offset_y", "source_offset_z"):
                    _set_metadata_value(part, key, "0")
                mesh_stat = part.find("./mesh_stat")
                if mesh_stat is None:
                    mesh_stat = ET.SubElement(part, "mesh_stat")
                mesh_stat.set("face_count", str(len(mesh.faces)))
                model_settings.insert(0, node)

            plate_node = copy.deepcopy(prototype_plate)
            for instance in list(plate_node.findall("./model_instance")):
                plate_node.remove(instance)
            for metadata in list(plate_node.findall("./metadata")):
                if metadata.get("key") in {
                    "thumbnail_file",
                    "thumbnail_no_light_file",
                    "top_file",
                    "pick_file",
                }:
                    plate_node.remove(metadata)
            for index, (item, _) in enumerate(meshes, start=1):
                instance = ET.SubElement(plate_node, "model_instance")
                for key, value in (
                    ("object_id", item.object_id),
                    ("instance_id", "0"),
                    ("identify_id", str(index)),
                ):
                    ET.SubElement(instance, "metadata", {"key": key, "value": value})
            model_settings.append(plate_node)
            assemble = ET.SubElement(model_settings, "assemble")
            for item, _ in meshes:
                ET.SubElement(
                    assemble,
                    "assemble_item",
                    {
                        "object_id": item.object_id,
                        "instance_id": "0",
                        "transform": IDENTITY_3MF,
                        "offset": "0 0 0",
                    },
                )
                ET.SubElement(
                    assemble,
                    "assemble_item",
                    {
                        "object_id": item.object_id,
                        "volume_id": item.object_id,
                        "transform": IDENTITY_3MF,
                    },
                )

            baseline = object_configurations[0]
            _apply_recommended_settings(settings, baseline.settings)
            _support_settings(settings, "none")
            settings["filament_colour"] = [
                str((settings.get("filament_colour") or ["#000000"])[0])
            ]
            settings["filament_type"] = [
                str((settings.get("filament_type") or ["PLA"])[0])
            ]
            local_ranges = _local_modifier_xml_by_object(
                {
                    item.object_id: configurations[item.object_id].local_modifier_plan
                    for item, _ in meshes
                }
            )
            replacements = {
                MODEL_PATH: ET.tostring(main, encoding="utf-8", xml_declaration=True),
                MODEL_SETTINGS_PATH: ET.tostring(
                    model_settings, encoding="utf-8", xml_declaration=True
                ),
                PROJECT_SETTINGS_PATH: json.dumps(
                    settings, ensure_ascii=False, indent=2
                ).encode("utf-8"),
                "3D/_rels/3dmodel.model.rels": (
                    f'<?xml version="1.0" encoding="UTF-8"?>'
                    f'<Relationships xmlns="{RELATIONSHIP_NS}" />'
                ).encode(),
            }
            if local_ranges is not None:
                replacements[LAYER_CONFIG_RANGES_PATH] = local_ranges
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as output_zip:
                written: set[str] = set()
                for entry in template_zip.infolist():
                    if entry.filename.startswith("3D/Objects/"):
                        continue
                    if _is_geometry_specific_member(entry.filename):
                        continue
                    output_zip.writestr(
                        entry,
                        replacements.get(
                            entry.filename,
                            read_zip_member(template_zip, entry.filename),
                        ),
                    )
                    written.add(entry.filename)
                for name, content in replacements.items():
                    if name not in written:
                        output_zip.writestr(name, content)
    except (KeyError, zipfile.BadZipFile, ET.ParseError, json.JSONDecodeError, UnsafeInputError) as exc:
        raise Project3MFError(f"cannot prepare multi-body STL project: {exc}") from exc
    atomic_write_new_bytes(output_path, buffer.getvalue())
    if _sha256(source_path) != source_hash or _sha256(template_path) != template_hash:
        output_path.unlink(missing_ok=True)
        raise Project3MFError("STL or profile changed during multi-body preparation")
    return PreparedMultiObjectProject(
        source_project_path=source_path,
        template_path=template_path,
        project_path=output_path,
        printable_object_count=len(meshes),
        object_names=tuple(item.name for item, _ in meshes),
        triangle_count=sum(len(mesh.faces) for _, mesh in meshes),
        source_project_sha256=source_hash,
        template_sha256=template_hash,
        project_sha256=_sha256(output_path),
    )


def create_bambu_project_from_stl(
    stl: str | Path,
    template: str | Path,
    output: str | Path,
    *,
    support_mode: str,
    recommended_settings: PrintSettings | None = None,
    local_modifier_plan: LocalModifierPlan | None = None,
) -> PreparedProject:
    """Replace template geometry with an STL while preserving Bambu settings."""
    stl_path = Path(stl).expanduser().resolve()
    template_path = Path(template).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if stl_path.suffix.lower() != ".stl" or not stl_path.is_file():
        raise Project3MFError("prepared Bambu project requires an existing STL")
    if template_path.suffix.lower() != ".3mf" or not template_path.is_file():
        raise Project3MFError("prepared Bambu project requires an existing 3MF template")
    if output_path.suffix.lower() != ".3mf":
        raise Project3MFError("prepared project output must use the .3mf extension")
    if output_path.exists() or output_path in {stl_path, template_path}:
        raise Project3MFError(f"prepared project output already exists: {output_path}")
    if not output_path.parent.is_dir():
        raise Project3MFError(f"prepared project parent not found: {output_path.parent}")
    stl_hash = _sha256(stl_path)
    template_hash = _sha256(template_path)
    loaded = trimesh.load_mesh(stl_path, process=False)
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise Project3MFError("STL did not load as one non-empty mesh")
    mesh = loaded.copy()
    mesh.merge_vertices()
    mesh.remove_unreferenced_vertices()
    if not np.isfinite(mesh.vertices).all():
        raise Project3MFError("STL contains non-finite vertices")
    try:
        with zipfile.ZipFile(template_path) as source_zip:
            validate_zip_archive(source_zip)
            main = read_zip_xml(source_zip, MODEL_PATH)
            model_settings = read_zip_xml(source_zip, MODEL_SETTINGS_PATH)
            settings = read_zip_json(source_zip, PROJECT_SETTINGS_PATH)
            if not isinstance(settings, dict):
                raise Project3MFError("Bambu project settings must be an object")
            namespace = {"m": CORE_NS}
            build_items = main.findall("./m:build/m:item", namespace)
            resources = main.find("./m:resources", namespace)
            if not build_items or resources is None:
                raise Project3MFError("3MF template has no reusable build object")
            selected_item = build_items[0]
            selected_id = selected_item.get("objectid")
            if not selected_id:
                raise Project3MFError("3MF template build item has no objectid")
            selected_object = next(
                (item for item in resources.findall("./m:object", namespace) if item.get("id") == selected_id),
                None,
            )
            if selected_object is None:
                raise Project3MFError("3MF template build object is missing")
            components = selected_object.find("./m:components", namespace)
            if components is None or not list(components):
                raise Project3MFError("3MF template object does not use a reusable model part")
            selected_component = next(iter(components))
            part_path = selected_component.get(f"{{{PRODUCTION_NS}}}path")
            part_object_id = selected_component.get("objectid")
            if not part_path or not part_object_id:
                raise Project3MFError("3MF template component has no external model path")
            part_path = _safe_member_path(part_path)
            part_root = read_zip_xml(source_zip, part_path)
            part_object = next(
                (
                    item
                    for item in part_root.findall("./m:resources/m:object", namespace)
                    if item.get("id") == part_object_id
                ),
                None,
            )
            mesh_node = part_object.find("./m:mesh", namespace) if part_object is not None else None
            if mesh_node is None:
                raise Project3MFError("3MF template model part has no replaceable mesh")

            build = main.find("./m:build", namespace)
            if build is None:
                raise Project3MFError("3MF template has no build section")
            for item in list(build):
                if item is not selected_item:
                    build.remove(item)
            for item in list(resources):
                if item is not selected_object:
                    resources.remove(item)
            for item in list(components):
                if item is not selected_component:
                    components.remove(item)
            selected_item.set("printable", "1")
            selected_item.set("transform", IDENTITY_3MF)
            selected_component.set("transform", IDENTITY_3MF)
            part_resources = part_root.find("./m:resources", namespace)
            if part_resources is None:
                raise Project3MFError("3MF template model part has no resources")
            for item in list(part_resources):
                if item is not part_object:
                    part_resources.remove(item)
            _replace_mesh(mesh_node, mesh)

            object_setting = next(
                (item for item in model_settings.findall("./object") if item.get("id") == selected_id),
                None,
            )
            if object_setting is None:
                raise Project3MFError("3MF template object settings are missing")
            for item in list(model_settings.findall("./object")):
                if item is not object_setting:
                    model_settings.remove(item)
            for metadata in object_setting.findall("./metadata"):
                if metadata.get("key") == "name":
                    metadata.set("value", stl_path.name)
                if "face_count" in metadata.attrib:
                    metadata.set("face_count", str(len(mesh.faces)))
                if metadata.get("key") == "enable_support":
                    metadata.set("value", "0" if support_mode == "none" else "1")
                if metadata.get("key") == "support_type" and support_mode != "none":
                    metadata.set("value", f"{support_mode}(auto)")
            # An STL has no material painting. Use the template's first filament
            # as its single base material, but never copy the template object's
            # per-height or per-face color decisions.
            _set_metadata_value(object_setting, "extruder", "1")
            parts = object_setting.findall("./part")
            if not parts:
                raise Project3MFError("3MF template part settings are missing")
            selected_part = parts[0]
            for item in parts[1:]:
                object_setting.remove(item)
            for metadata in selected_part.findall("./metadata"):
                key = metadata.get("key")
                if key in {"name", "source_file"}:
                    metadata.set("value", stl_path.name)
                elif key == "matrix":
                    metadata.set("value", IDENTITY_4X4)
                elif key in {"source_offset_x", "source_offset_y", "source_offset_z"}:
                    metadata.set("value", "0")
                elif key == "extruder":
                    metadata.set("value", "1")
            mesh_stat = selected_part.find("./mesh_stat")
            if mesh_stat is not None:
                mesh_stat.set("face_count", str(len(mesh.faces)))

            plates = model_settings.findall("./plate")
            if not plates:
                raise Project3MFError("3MF template plate settings are missing")
            selected_plate = plates[0]
            for item in plates[1:]:
                model_settings.remove(item)
            for metadata in list(selected_plate.findall("./metadata")):
                if metadata.get("key") in {
                    "thumbnail_file",
                    "thumbnail_no_light_file",
                    "top_file",
                    "pick_file",
                }:
                    selected_plate.remove(metadata)
            instances = list(selected_plate.findall("./model_instance"))
            instance = next(
                (
                    candidate
                    for candidate in instances
                    if any(
                        item.get("key") == "object_id"
                        and item.get("value") == selected_id
                        for item in candidate.findall("./metadata")
                    )
                ),
                None,
            )
            if instance is None:
                instance = ET.SubElement(selected_plate, "model_instance")
            for candidate in instances:
                if candidate is not instance:
                    selected_plate.remove(candidate)
            instance_values = {
                item.get("key"): item
                for item in instance.findall("./metadata")
                if item.get("key")
            }
            for key, value in (
                ("object_id", selected_id),
                ("instance_id", "0"),
                ("identify_id", "1"),
            ):
                metadata = instance_values.get(key)
                if metadata is None:
                    metadata = ET.SubElement(instance, "metadata", {"key": key})
                if key != "identify_id" or not metadata.get("value"):
                    metadata.set("value", value)
            assemble = model_settings.find("./assemble")
            if assemble is None:
                assemble = ET.SubElement(model_settings, "assemble")
            else:
                assemble.clear()
            ET.SubElement(
                assemble,
                "assemble_item",
                {
                    "object_id": selected_id,
                    "instance_id": "0",
                    "transform": IDENTITY_3MF,
                    "offset": "0 0 0",
                },
            )
            ET.SubElement(
                assemble,
                "assemble_item",
                {
                    "object_id": selected_id,
                    "volume_id": "0",
                    "transform": IDENTITY_3MF,
                },
            )
            if recommended_settings is not None:
                _apply_recommended_settings(settings, recommended_settings)
            _support_settings(settings, support_mode)
            local_ranges = _local_modifier_xml(local_modifier_plan)

            rels_path = "3D/_rels/3dmodel.model.rels"
            rels_root = read_zip_xml(source_zip, rels_path)
            for relationship in list(rels_root):
                target = _safe_member_path(relationship.get("Target", ""))
                if target != part_path:
                    rels_root.remove(relationship)
            clean_rels = ET.Element("Relationships", {"xmlns": RELATIONSHIP_NS})
            for relationship in rels_root:
                ET.SubElement(clean_rels, "Relationship", dict(relationship.attrib))
            replacements = {
                MODEL_PATH: ET.tostring(main, encoding="utf-8", xml_declaration=True),
                part_path: ET.tostring(part_root, encoding="utf-8", xml_declaration=True),
                MODEL_SETTINGS_PATH: ET.tostring(
                    model_settings, encoding="utf-8", xml_declaration=True
                ),
                PROJECT_SETTINGS_PATH: json.dumps(
                    settings, ensure_ascii=False, indent=2
                ).encode("utf-8"),
                rels_path: ET.tostring(clean_rels, encoding="utf-8", xml_declaration=True),
            }
            if local_ranges is not None:
                replacements[LAYER_CONFIG_RANGES_PATH] = local_ranges
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as output_zip:
                written: set[str] = set()
                for entry in source_zip.infolist():
                    if entry.filename.startswith("3D/Objects/") and entry.filename != part_path:
                        continue
                    if _is_geometry_specific_member(entry.filename):
                        continue
                    output_zip.writestr(
                        entry,
                        replacements.get(
                            entry.filename,
                            read_zip_member(source_zip, entry.filename),
                        ),
                    )
                    written.add(entry.filename)
                for name, content in replacements.items():
                    if name not in written:
                        output_zip.writestr(name, content)
    except (KeyError, zipfile.BadZipFile, ET.ParseError, json.JSONDecodeError, UnsafeInputError) as exc:
        raise Project3MFError(f"cannot prepare Bambu 3MF: {exc}") from exc
    atomic_write_new_bytes(output_path, buffer.getvalue())
    if _sha256(stl_path) != stl_hash or _sha256(template_path) != template_hash:
        output_path.unlink(missing_ok=True)
        raise Project3MFError("STL or template changed while preparing the Bambu project")
    return PreparedProject(
        source_stl_path=stl_path,
        template_path=template_path,
        project_path=output_path,
        support_mode=support_mode,
        object_name=stl_path.name,
        triangle_count=len(mesh.faces),
        geometry_signature=_geometry_signature(mesh),
        source_stl_sha256=stl_hash,
        template_sha256=template_hash,
        project_sha256=_sha256(output_path),
        local_modifier_range_count=(
            len(local_modifier_plan.ranges) if local_modifier_plan is not None else 0
        ),
    )


def verify_ready_project(
    project: str | Path,
    *,
    expected_object_name: str | None = None,
    expected_triangle_count: int | None = None,
    expected_geometry_signature: str | None = None,
    expected_geometry_path: str | Path | None = None,
    expected_support_mode: str | None = None,
    expected_print_settings: PrintSettings | None = None,
    expected_object_configurations: tuple[ObjectPrintConfiguration, ...] | None = None,
    expected_local_modifier_plan: LocalModifierPlan | None = None,
    expected_printable_count: int | None = 1,
    expected_single_color: bool = False,
    external_gcode: str | Path | None = None,
) -> ReadyProjectVerification:
    """Verify that a ready 3MF has one printable model and matching embedded G-code."""
    project_path = Path(project).expanduser().resolve()
    errors: list[str] = []
    names: list[str] = []
    triangle_count = 0
    geometry_signature: str | None = None
    support_mode: str | None = None
    verified_settings: dict[str, Any] = {}
    embedded_path: str | None = None
    embedded_hash: str | None = None
    printable_count = 0
    object_metadata: dict[str, dict[str, str]] = {}
    try:
        with zipfile.ZipFile(project_path) as archive:
            validate_zip_archive(archive)
            root = read_zip_xml(archive, MODEL_PATH)
            model_settings = read_zip_xml(archive, MODEL_SETTINGS_PATH)
            settings = read_zip_json(archive, PROJECT_SETTINGS_PATH)
            namespace = {"m": CORE_NS}
            printable_items = [
                item
                for item in root.findall("./m:build/m:item", namespace)
                if item.get("printable", "1") != "0"
            ]
            printable_ids = {item.get("objectid") for item in printable_items}
            printable_count = len(printable_items)
            if None in printable_ids or printable_count == 0:
                errors.append(f"expected at least one printable object, found {printable_count}")
            elif (
                expected_printable_count is not None
                and printable_count != expected_printable_count
            ):
                errors.append(
                    f"expected {expected_printable_count} printable object(s), "
                    f"found {printable_count}"
                )
            for obj in model_settings.findall("./object"):
                if obj.get("id") not in printable_ids:
                    continue
                object_id = str(obj.get("id"))
                object_metadata[object_id] = {
                    str(item.get("key")): str(item.get("value", ""))
                    for item in obj.findall("./metadata")
                    if item.get("key")
                }
                name = next(
                    (
                        item.get("value")
                        for item in obj.findall("./metadata")
                        if item.get("key") == "name" and item.get("value")
                    ),
                    f"object-{obj.get('id')}",
                )
                names.append(str(name))
                part_counts = [
                    int(item.get("face_count", "0"))
                    for item in obj.findall("./part/mesh_stat")
                ]
                triangle_count += sum(part_counts)
                if expected_single_color:
                    for metadata in obj.findall(".//metadata"):
                        if metadata.get("key") == "extruder" and metadata.get(
                            "value", "1"
                        ) not in {"", "0", "1"}:
                            errors.append(
                                "printable STL inherited a non-primary extruder assignment"
                            )
            if expected_object_configurations is not None:
                expected_ids = {item.object_id for item in expected_object_configurations}
                metadata_for_expected = dict(object_metadata)
                if set(object_metadata) != expected_ids:
                    by_name = {
                        values.get("name", ""): values
                        for values in object_metadata.values()
                        if values.get("name")
                    }
                    if (
                        len(by_name) == len(object_metadata)
                        and all(item.name in by_name for item in expected_object_configurations)
                    ):
                        metadata_for_expected = {
                            item.object_id: by_name[item.name]
                            for item in expected_object_configurations
                        }
                    else:
                        errors.append(
                            "object setting IDs differ: expected "
                            f"{sorted(expected_ids)}, found {sorted(object_metadata)}"
                        )
                for configuration in expected_object_configurations:
                    actual = metadata_for_expected.get(configuration.object_id, {})
                    expected_values = _object_process_values(configuration.settings)
                    expected_values["enable_support"] = (
                        "0" if configuration.support_mode == "none" else "1"
                    )
                    if configuration.support_mode != "none":
                        expected_values["support_type"] = (
                            f"{configuration.support_mode}(auto)"
                        )
                    for key, expected_value in expected_values.items():
                        if actual.get(key) != expected_value:
                            errors.append(
                                f"object {configuration.object_id} setting mismatch for "
                                f"{key}: expected {expected_value!r}, "
                                f"found {actual.get(key)!r}"
                            )
            printable_meshes: list[trimesh.Trimesh] = []
            cache = {MODEL_PATH: root}
            for item in root.findall("./m:build/m:item", namespace):
                if item.get("printable", "1") == "0":
                    continue
                object_id = item.get("objectid")
                if not object_id:
                    continue
                printable_meshes.extend(
                    _resolve_object_meshes(
                        archive,
                        MODEL_PATH,
                        object_id,
                        _parse_transform(item.get("transform")),
                        cache,
                        set(),
                    )
                )
            if printable_meshes:
                actual_mesh = trimesh.util.concatenate(printable_meshes)
                actual_mesh.merge_vertices()
                actual_mesh.remove_unreferenced_vertices()
                actual_triangle_count = len(actual_mesh.faces)
                geometry_signature = _geometry_signature(actual_mesh)
                if triangle_count != actual_triangle_count:
                    errors.append(
                        "model metadata triangle count differs from actual geometry: "
                        f"{triangle_count} vs {actual_triangle_count}"
                    )
            if expected_single_color:
                painted_faces = 0
                for cached_root in cache.values():
                    painted_faces += sum(
                        1
                        for triangle in cached_root.findall(".//m:triangle", namespace)
                        if triangle.get("paint_color")
                    )
                if painted_faces:
                    errors.append(
                        f"printable STL inherited color painting on {painted_faces} face(s)"
                    )
                if "Metadata/layer_config_ranges.xml" in archive.namelist():
                    ranges = read_zip_xml(archive, "Metadata/layer_config_ranges.xml")
                    foreign_extruders = [
                        option.get("text", option.text or "").strip()
                        for option in ranges.findall(".//option")
                        if option.get("opt_key") == "extruder"
                        and (option.get("text", option.text or "").strip() not in {"", "0", "1"})
                    ]
                    if foreign_extruders:
                        errors.append(
                            "printable STL inherited layer-range color assignments"
                        )
            if expected_local_modifier_plan is not None:
                actual_ranges = _read_local_modifier_ranges(archive)
                expected_ranges = tuple(
                    (
                        float(item.z_min_mm),
                        float(item.z_max_mm),
                        {
                            key: str(value)
                            for key, value in item.settings.items()
                            if key in _LOCAL_RANGE_KEYS
                        },
                    )
                    for item in expected_local_modifier_plan.ranges
                )
                if len(actual_ranges) != len(expected_ranges):
                    errors.append(
                        "local modifier range count mismatch: expected "
                        f"{len(expected_ranges)}, found {len(actual_ranges)}"
                    )
                else:
                    for index, (actual, expected) in enumerate(
                        zip(actual_ranges, expected_ranges), start=1
                    ):
                        if not (
                            math.isclose(actual[0], expected[0], abs_tol=1e-4)
                            and math.isclose(actual[1], expected[1], abs_tol=1e-4)
                            and actual[2] == expected[2]
                        ):
                            errors.append(f"local modifier range {index} differs from plan")
            if expected_object_name and names != [expected_object_name]:
                errors.append(
                    f"printable object mismatch: expected {expected_object_name!r}, found {names!r}"
                )
            if expected_triangle_count is not None and triangle_count != expected_triangle_count:
                errors.append(
                    f"triangle count mismatch: expected {expected_triangle_count}, found {triangle_count}"
                )
            if (
                expected_geometry_signature is not None
                and geometry_signature != expected_geometry_signature
            ):
                errors.append("printable geometry fingerprint differs from the source STL")
            if expected_geometry_path is not None and printable_meshes:
                expected_path = Path(expected_geometry_path).expanduser().resolve()
                expected_mesh = trimesh.load_mesh(expected_path, process=False)
                if not isinstance(expected_mesh, trimesh.Trimesh) or len(expected_mesh.faces) == 0:
                    errors.append(f"cannot load expected geometry: {expected_path}")
                else:
                    expected_mesh.merge_vertices()
                    expected_mesh.remove_unreferenced_vertices()
                    if not _same_geometry(expected_mesh, actual_mesh):
                        errors.append(
                            "printable geometry fingerprint differs from the source STL"
                        )
            enabled = str(settings.get("enable_support", "0")) == "1"
            if enabled:
                raw_type = str(settings.get("support_type", ""))
                support_mode = "tree" if raw_type.startswith("tree") else "normal"
            else:
                support_mode = "none"
            if expected_support_mode and support_mode != expected_support_mode:
                errors.append(
                    f"support mode mismatch: expected {expected_support_mode}, found {support_mode}"
                )
            verified_settings = {
                key: settings.get(key)
                for key in (
                    "layer_height",
                    "initial_layer_print_height",
                    "line_width",
                    "wall_loops",
                    "top_shell_layers",
                    "bottom_shell_layers",
                    "brim_type",
                    "sparse_infill_density",
                    "sparse_infill_pattern",
                    "top_surface_pattern",
                    "top_surface_line_width",
                    "ironing_type",
                    "top_surface_density",
                    "top_shell_thickness",
                    "seam_placement_away_from_overhangs",
                    "wall_generator",
                    "support_top_z_distance",
                    "support_bottom_z_distance",
                    "support_object_xy_distance",
                    "support_interface_top_layers",
                    "support_interface_bottom_layers",
                    "support_interface_spacing",
                    "outer_wall_speed",
                    "inner_wall_speed",
                    "sparse_infill_speed",
                    "internal_solid_infill_speed",
                    "top_surface_speed",
                    "support_speed",
                    "support_interface_speed",
                    "bridge_speed",
                    "initial_layer_speed",
                    "travel_speed",
                    "default_acceleration",
                    "outer_wall_acceleration",
                    "top_surface_acceleration",
                    "initial_layer_acceleration",
                    "travel_acceleration",
                    "nozzle_temperature",
                    "nozzle_temperature_initial_layer",
                    "fan_max_speed",
                    "fan_min_speed",
                    "curr_bed_type",
                    "textured_plate_temp",
                    "textured_plate_temp_initial_layer",
                    "cool_plate_temp",
                    "cool_plate_temp_initial_layer",
                    "eng_plate_temp",
                    "eng_plate_temp_initial_layer",
                    "hot_plate_temp",
                    "hot_plate_temp_initial_layer",
                    "filament_max_volumetric_speed",
                    "filament_flow_ratio",
                    "retraction_length",
                    "retraction_speed",
                    "wipe",
                    "wipe_distance",
                )
                if key in settings
            }
            if expected_print_settings is not None:
                scalar_expectations = {
                    "layer_height": f"{expected_print_settings.layer_height_mm:g}",
                    "initial_layer_print_height": (
                        f"{expected_print_settings.initial_layer_height_mm:g}"
                    ),
                    "line_width": f"{expected_print_settings.line_width_mm:g}",
                    "wall_loops": str(expected_print_settings.wall_loops),
                    "top_shell_layers": str(expected_print_settings.top_layers),
                    "bottom_shell_layers": str(expected_print_settings.bottom_layers),
                    "brim_type": (
                        "auto_brim" if expected_print_settings.brim else "no_brim"
                    ),
                    "sparse_infill_density": (
                        f"{expected_print_settings.sparse_infill_percent}%"
                    ),
                    "sparse_infill_pattern": expected_print_settings.sparse_infill_pattern,
                    "top_surface_pattern": expected_print_settings.top_surface_pattern,
                    "top_surface_line_width": (
                        f"{expected_print_settings.top_surface_line_width_mm:g}"
                    ),
                    "ironing_type": (
                        "topmost" if expected_print_settings.ironing_enabled else "no ironing"
                    ),
                    "top_surface_density": (
                        f"{expected_print_settings.top_surface_density_percent}%"
                    ),
                    "top_shell_thickness": (
                        f"{expected_print_settings.top_shell_thickness_mm:g}"
                    ),
                    "seam_placement_away_from_overhangs": (
                        "1"
                        if expected_print_settings.seam_placement_away_from_overhangs
                        else "0"
                    ),
                    "seam_position": expected_print_settings.seam_position,
                    "seam_slope_type": expected_print_settings.scarf_seam_type,
                    "override_filament_scarf_seam_setting": (
                        "1" if expected_print_settings.override_filament_scarf_seam else "0"
                    ),
                    "wall_generator": expected_print_settings.wall_generator,
                    "support_top_z_distance": (
                        f"{expected_print_settings.support_top_z_distance_mm:g}"
                    ),
                    "support_bottom_z_distance": (
                        f"{expected_print_settings.support_bottom_z_distance_mm:g}"
                    ),
                    "support_object_xy_distance": (
                        f"{expected_print_settings.support_object_xy_distance_mm:g}"
                    ),
                    "support_interface_top_layers": str(
                        expected_print_settings.support_interface_top_layers
                    ),
                    "support_interface_bottom_layers": str(
                        expected_print_settings.support_interface_bottom_layers
                    ),
                    "support_interface_spacing": (
                        f"{expected_print_settings.support_interface_spacing_mm:g}"
                    ),
                    "detect_thin_wall": (
                        "1" if expected_print_settings.detect_thin_wall else "0"
                    ),
                    "detect_floating_vertical_shell": (
                        "1"
                        if expected_print_settings.detect_floating_vertical_shell
                        else "0"
                    ),
                    "bridge_no_support": (
                        "1" if expected_print_settings.bridge_no_support else "0"
                    ),
                    "infill_combination": (
                        "1" if expected_print_settings.infill_combination else "0"
                    ),
                    "reduce_crossing_wall": (
                        "1" if expected_print_settings.reduce_crossing_wall else "0"
                    ),
                    "avoid_crossing_wall_includes_support": (
                        "1"
                        if expected_print_settings.avoid_crossing_wall_includes_support
                        else "0"
                    ),
                    "reduce_infill_retraction_mode": (
                        expected_print_settings.reduce_infill_retraction_mode
                    ),
                }
                for key, expected_value in scalar_expectations.items():
                    if str(settings.get(key, "")) != expected_value:
                        errors.append(
                            f"print setting mismatch for {key}: expected "
                            f"{expected_value}, found {settings.get(key)!r}"
                        )

                def require_array(key: str, expected_value: float | str) -> None:
                    values = settings.get(key, [])
                    if not isinstance(values, list) or not values or any(
                        str(item) != str(expected_value) for item in values
                    ):
                        errors.append(
                            f"print setting mismatch for {key}: expected all "
                            f"{expected_value}, found {values!r}"
                        )

                def require_shaped(key: str, expected_value: int) -> None:
                    value = settings.get(key)
                    if isinstance(value, list):
                        matches = bool(value) and all(
                            str(item) == str(expected_value) for item in value
                        )
                    else:
                        matches = str(value) == str(expected_value)
                    if not matches:
                        errors.append(
                            f"print setting mismatch for {key}: expected "
                            f"{expected_value}, found {value!r}"
                        )

                dynamic_expectations = {
                    "outer_wall_speed": expected_print_settings.outer_wall_speed_mm_s,
                    "inner_wall_speed": expected_print_settings.inner_wall_speed_mm_s,
                    "sparse_infill_speed": expected_print_settings.sparse_infill_speed_mm_s,
                    "internal_solid_infill_speed": expected_print_settings.internal_solid_infill_speed_mm_s,
                    "top_surface_speed": expected_print_settings.top_surface_speed_mm_s,
                    "support_speed": expected_print_settings.support_speed_mm_s,
                    "support_interface_speed": expected_print_settings.support_interface_speed_mm_s,
                    "bridge_speed": expected_print_settings.bridge_speed_mm_s,
                    "initial_layer_speed": expected_print_settings.initial_layer_speed_mm_s,
                    "travel_speed": expected_print_settings.travel_speed_mm_s,
                    "default_acceleration": expected_print_settings.default_acceleration_mm_s2,
                    "outer_wall_acceleration": expected_print_settings.outer_wall_acceleration_mm_s2,
                    "top_surface_acceleration": expected_print_settings.top_surface_acceleration_mm_s2,
                    "initial_layer_acceleration": expected_print_settings.initial_layer_acceleration_mm_s2,
                    "travel_acceleration": expected_print_settings.travel_acceleration_mm_s2,
                    "small_perimeter_speed": (
                        f"{expected_print_settings.small_perimeter_speed_percent}%"
                    ),
                    "small_perimeter_threshold": (
                        f"{expected_print_settings.small_perimeter_threshold_mm:g}"
                    ),
                    "slow_down_layer_time": expected_print_settings.slow_down_layer_time_s,
                    "slow_down_min_speed": expected_print_settings.slow_down_min_speed_mm_s,
                }
                for key, expected_value in dynamic_expectations.items():
                    require_shaped(key, expected_value)

                require_array(
                    "nozzle_temperature", expected_print_settings.nozzle_temperature_c
                )
                require_array(
                    "nozzle_temperature_initial_layer",
                    expected_print_settings.nozzle_temperature_c,
                )
                require_array("fan_max_speed", expected_print_settings.fan_percent)
                require_array("fan_min_speed", expected_print_settings.fan_percent)
                require_array(
                    "filament_max_volumetric_speed",
                    f"{expected_print_settings.max_volumetric_speed_mm3_s:g}",
                )
                require_array(
                    "filament_flow_ratio",
                    f"{expected_print_settings.filament_flow_ratio:g}",
                )
                require_array(
                    "retraction_length",
                    f"{expected_print_settings.retraction_length_mm:g}",
                )
                require_array(
                    "retraction_speed",
                    f"{expected_print_settings.retraction_speed_mm_s:g}",
                )
                require_array("wipe", "1" if expected_print_settings.wipe_enabled else "0")
                require_array(
                    "wipe_distance",
                    f"{expected_print_settings.wipe_distance_mm:g}",
                )
                bed_type = str(settings.get("curr_bed_type", "")).casefold()
                bed_key = (
                    "textured_plate_temp"
                    if "textured" in bed_type
                    else "cool_plate_temp"
                    if "cool" in bed_type
                    else "eng_plate_temp"
                    if "engineering" in bed_type
                    else "hot_plate_temp"
                )
                require_array(bed_key, expected_print_settings.bed_temperature_c)
                require_array(
                    f"{bed_key}_initial_layer",
                    expected_print_settings.bed_temperature_c,
                )
            gcode_names = sorted(
                name
                for name in archive.namelist()
                if name.startswith("Metadata/plate_") and name.endswith(".gcode")
            )
            if len(gcode_names) != 1:
                errors.append(f"expected one embedded plate G-code, found {len(gcode_names)}")
            else:
                embedded_path = gcode_names[0]
                embedded_hash = sha256_zip_member(archive, embedded_path)
                if external_gcode is not None:
                    external_path = Path(external_gcode).expanduser().resolve()
                    if not external_path.is_file():
                        errors.append(f"external G-code is missing: {external_path}")
                    elif _sha256(external_path) != embedded_hash:
                        errors.append("embedded and external G-code SHA-256 values differ")
    except (
        OSError,
        KeyError,
        ValueError,
        zipfile.BadZipFile,
        ET.ParseError,
        json.JSONDecodeError,
        Project3MFError,
        UnsafeInputError,
    ) as exc:
        errors.append(f"cannot inspect ready project: {exc}")
    return ReadyProjectVerification(
        project_path=project_path,
        valid=not errors,
        printable_object_count=printable_count,
        object_names=tuple(names),
        triangle_count=triangle_count,
        geometry_signature=geometry_signature,
        support_mode=support_mode,
        print_settings=verified_settings,
        embedded_gcode_path=embedded_path,
        embedded_gcode_sha256=embedded_hash,
        errors=tuple(errors),
    )
