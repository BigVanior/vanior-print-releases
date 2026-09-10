"""Resource and structure limits for untrusted model and slicer files."""

from __future__ import annotations

import hashlib
import json
import stat
import struct
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_ARCHIVE_ENTRIES = 8_192
MAX_ARCHIVE_MEMBER_BYTES = 536_870_912  # 512 MiB
MAX_ARCHIVE_TOTAL_BYTES = 2_147_483_648  # 2 GiB
MAX_COMPRESSION_RATIO = 1_000.0
MAX_XML_BYTES = 134_217_728  # 128 MiB
MAX_JSON_BYTES = 67_108_864  # 64 MiB
MAX_STL_BYTES = 1_073_741_824  # 1 GiB
MAX_STL_TRIANGLES = 8_000_000
MAX_COMPONENT_DEPTH = 64
MAX_PRINTABLE_OBJECTS = 2_048
MAX_GCODE_BYTES = 4_294_967_296  # 4 GiB
MAX_TEXT_LINE_BYTES = 65_536


class UnsafeInputError(ValueError):
    """Raised before an untrusted file can consume unsafe resources."""


def safe_archive_name(raw: str) -> str:
    """Validate an OPC/ZIP member name and return its canonical POSIX form."""
    if not raw or "\x00" in raw or "\\" in raw or len(raw) > 1_024:
        raise UnsafeInputError(f"unsafe archive member name: {raw!r}")
    if raw.startswith(("/", "//")):
        raise UnsafeInputError(f"absolute archive member path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafeInputError(f"unsafe archive member path: {raw!r}")
    if path.parts and ":" in path.parts[0]:
        raise UnsafeInputError(f"drive-qualified archive member path: {raw!r}")
    normalized = path.as_posix()
    if normalized != raw.rstrip("/"):
        raise UnsafeInputError(f"non-canonical archive member path: {raw!r}")
    return f"{normalized}/" if raw.endswith("/") else normalized


def validate_zip_archive(archive: zipfile.ZipFile) -> None:
    """Reject ZIP bombs, ambiguous entries and filesystem-like payloads."""
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise UnsafeInputError(
            f"archive contains too many entries: {len(entries)} > {MAX_ARCHIVE_ENTRIES}"
        )
    names: set[str] = set()
    total = 0
    for entry in entries:
        name = safe_archive_name(entry.filename)
        folded = name.rstrip("/").casefold()
        if folded in names:
            raise UnsafeInputError(f"duplicate archive member: {name}")
        names.add(folded)
        if entry.flag_bits & 0x1:
            raise UnsafeInputError(f"encrypted archive member is not supported: {name}")
        if entry.file_size < 0 or entry.compress_size < 0:
            raise UnsafeInputError(f"invalid archive member size: {name}")
        if entry.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise UnsafeInputError(f"archive member is too large: {name}")
        total += entry.file_size
        if total > MAX_ARCHIVE_TOTAL_BYTES:
            raise UnsafeInputError("archive expands beyond the 2 GiB safety limit")
        if entry.file_size and entry.compress_size == 0:
            raise UnsafeInputError(f"invalid compressed size for archive member: {name}")
        if entry.compress_size and entry.file_size / entry.compress_size > MAX_COMPRESSION_RATIO:
            raise UnsafeInputError(f"suspicious compression ratio for archive member: {name}")
        unix_mode = (entry.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if entry.create_system == 3 and file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise UnsafeInputError(f"special filesystem entry is not allowed: {name}")


def read_zip_member(
    archive: zipfile.ZipFile,
    name: str,
    *,
    maximum_bytes: int = MAX_ARCHIVE_MEMBER_BYTES,
) -> bytes:
    """Read one validated member with a hard decompressed-byte ceiling."""
    canonical = safe_archive_name(name)
    info = archive.getinfo(canonical)
    if info.file_size > maximum_bytes:
        raise UnsafeInputError(f"archive member exceeds safety limit: {canonical}")
    with archive.open(info, "r") as stream:
        payload = stream.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise UnsafeInputError(f"archive member exceeds safety limit: {canonical}")
    return payload


def sha256_zip_member(archive: zipfile.ZipFile, name: str) -> str:
    """Hash a validated member without materializing it in memory."""
    canonical = safe_archive_name(name)
    info = archive.getinfo(canonical)
    if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
        raise UnsafeInputError(f"archive member exceeds safety limit: {canonical}")
    digest = hashlib.sha256()
    total = 0
    with archive.open(info, "r") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(block)
            if total > MAX_ARCHIVE_MEMBER_BYTES:
                raise UnsafeInputError(f"archive member exceeds safety limit: {canonical}")
            digest.update(block)
    return digest.hexdigest()


def parse_xml_bytes(payload: bytes, *, context: str = "XML") -> ET.Element:
    if len(payload) > MAX_XML_BYTES:
        raise UnsafeInputError(f"{context} exceeds the 128 MiB safety limit")
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise UnsafeInputError(f"DTD/entities are not allowed in {context}")
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise UnsafeInputError(f"cannot parse {context}: {exc}") from exc


def read_zip_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    return parse_xml_bytes(
        read_zip_member(archive, name, maximum_bytes=MAX_XML_BYTES),
        context=name,
    )


def read_zip_json(archive: zipfile.ZipFile, name: str) -> Any:
    payload = read_zip_member(archive, name, maximum_bytes=MAX_JSON_BYTES)
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsafeInputError(f"cannot parse JSON member {name}: {exc}") from exc


def read_json_file(path: Path, *, maximum_bytes: int = MAX_JSON_BYTES) -> Any:
    if path.stat().st_size > maximum_bytes:
        raise UnsafeInputError(f"JSON file exceeds safety limit: {path}")
    try:
        return json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsafeInputError(f"cannot parse JSON file {path}: {exc}") from exc


def validate_stl_file(path: Path) -> None:
    """Reject oversized and clearly malformed binary STL before trimesh loads it."""
    size = path.stat().st_size
    if size <= 0:
        raise UnsafeInputError("STL is empty")
    if size > MAX_STL_BYTES:
        raise UnsafeInputError("STL exceeds the 1 GiB safety limit")
    with path.open("rb") as stream:
        header = stream.read(84)
    if len(header) < 84:
        return  # A small ASCII STL is allowed to reach the format parser.
    triangles = struct.unpack("<I", header[80:84])[0]
    expected = 84 + triangles * 50
    looks_binary = expected == size or b"\x00" in header[:80] or header[:5].lower() != b"solid"
    if not looks_binary:
        return
    if triangles > MAX_STL_TRIANGLES:
        raise UnsafeInputError(
            f"STL declares too many triangles: {triangles:,} > {MAX_STL_TRIANGLES:,}"
        )
    if expected > size:
        raise UnsafeInputError("binary STL is truncated")


def validate_model_file(path: Path, *, format_suffix: str | None = None) -> None:
    """Run cheap validation before a user model is moved or parsed."""
    if path.is_symlink():
        raise UnsafeInputError("symbolic links are not accepted as model uploads")
    suffix = (format_suffix or path.suffix).casefold()
    if suffix == ".stl":
        validate_stl_file(path)
        return
    if suffix == ".3mf":
        try:
            with zipfile.ZipFile(path) as archive:
                validate_zip_archive(archive)
                read_zip_xml(archive, "3D/3dmodel.model")
        except zipfile.BadZipFile as exc:
            raise UnsafeInputError(f"invalid 3MF archive: {path}") from exc
        return
    raise UnsafeInputError("only STL and 3MF models are supported")


def safe_output_child(root: Path, candidate: Path) -> Path:
    """Ensure a generated artifact cannot escape its isolated output directory."""
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafeInputError(f"generated path escapes output directory: {candidate}") from exc
    if resolved.is_symlink():
        raise UnsafeInputError(f"generated artifact must not be a symbolic link: {candidate}")
    return resolved


def file_identity(path: Path) -> tuple[int, int, int]:
    """Return stable-enough identity data for a before/after TOCTOU check."""
    details = path.stat(follow_symlinks=False)
    return (details.st_size, details.st_mtime_ns, getattr(details, "st_ino", 0))


def validate_gcode_file(path: Path) -> None:
    size = path.stat().st_size
    if size <= 0 or size > MAX_GCODE_BYTES:
        raise UnsafeInputError("G-code is empty or exceeds the 4 GiB safety limit")
