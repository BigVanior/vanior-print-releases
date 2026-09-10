"""Conservative STL cleanup and small-hole repair."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

from .analyzer import AnalysisError, _analyze_mesh_health, _load_stl


@dataclass(frozen=True)
class RepairResult:
    source_path: Path
    output_path: Path
    removed_debris_bodies: int
    removed_triangles: int
    filled_holes: int
    added_triangles: int
    collapsed_micro_edges: int
    stitched_t_junctions: int
    boundary_edges_before: int
    boundary_edges_after: int
    non_manifold_edges_before: int
    non_manifold_edges_after: int
    final_status: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source_path"] = str(self.source_path)
        result["output_path"] = str(self.output_path)
        return result


def _boundary_edges(mesh: trimesh.Trimesh) -> tuple[np.ndarray, np.ndarray]:
    faces = np.asarray(mesh.faces, dtype=np.int64)
    oriented = faces[:, [[0, 1], [1, 2], [2, 0]]].reshape((-1, 2))
    sorted_edges = np.sort(oriented, axis=1)
    unique, first_occurrence, counts = np.unique(
        sorted_edges,
        axis=0,
        return_index=True,
        return_counts=True,
    )
    boundary_mask = counts == 1
    return unique[boundary_mask], oriented[first_occurrence[boundary_mask]]


def _extract_simple_cycles(boundary_edges: np.ndarray) -> list[list[int]]:
    adjacency: dict[int, set[int]] = {}
    unused: set[tuple[int, int]] = set()
    for left_value, right_value in boundary_edges:
        left, right = int(left_value), int(right_value)
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
        unused.add((min(left, right), max(left, right)))

    # Manifold hole boundaries are Eulerian. Odd-degree vertices indicate a
    # crack or ambiguous topology which this conservative repair must not guess.
    if any(len(neighbors) % 2 for neighbors in adjacency.values()):
        return []

    cycles: list[list[int]] = []
    while unused:
        active_degrees = {
            vertex: sum(
                (min(vertex, neighbor), max(vertex, neighbor)) in unused
                for neighbor in neighbors
            )
            for vertex, neighbors in adjacency.items()
        }
        branching = [vertex for vertex, degree in active_degrees.items() if degree > 2]
        active = [vertex for vertex, degree in active_degrees.items() if degree > 0]
        if not active:
            break
        start = min(branching or active)
        path = [start]
        current = start

        while True:
            candidates = sorted(
                neighbor
                for neighbor in adjacency[current]
                if (min(current, neighbor), max(current, neighbor)) in unused
            )
            if not candidates:
                return []
            next_vertex = candidates[0]
            unused.remove((min(current, next_vertex), max(current, next_vertex)))
            current = next_vertex
            if current == start:
                break
            if current in path:
                return []
            path.append(current)
            if len(path) > len(boundary_edges):
                return []

        if len(path) >= 3:
            cycles.append(path)

    return cycles


def _cross_2d(left: np.ndarray, right: np.ndarray) -> float:
    return float(left[0] * right[1] - left[1] * right[0])


def _point_in_triangle(
    point: np.ndarray,
    first: np.ndarray,
    second: np.ndarray,
    third: np.ndarray,
    tolerance: float,
) -> bool:
    one = _cross_2d(second - first, point - first)
    two = _cross_2d(third - second, point - second)
    three = _cross_2d(first - third, point - third)
    return one >= -tolerance and two >= -tolerance and three >= -tolerance


def _segments_intersect(
    first_a: np.ndarray,
    first_b: np.ndarray,
    second_a: np.ndarray,
    second_b: np.ndarray,
    tolerance: float,
) -> bool:
    one = _cross_2d(first_b - first_a, second_a - first_a)
    two = _cross_2d(first_b - first_a, second_b - first_a)
    three = _cross_2d(second_b - second_a, first_a - second_a)
    four = _cross_2d(second_b - second_a, first_b - second_a)
    opposite_first = (one > tolerance and two < -tolerance) or (
        one < -tolerance and two > tolerance
    )
    opposite_second = (three > tolerance and four < -tolerance) or (
        three < -tolerance and four > tolerance
    )
    if opposite_first and opposite_second:
        return True

    coordinate_tolerance = max(1e-9, float(np.sqrt(tolerance)) * 1e-3)

    def on_segment(start: np.ndarray, end: np.ndarray, point: np.ndarray) -> bool:
        return bool(
            np.all(point >= np.minimum(start, end) - coordinate_tolerance)
            and np.all(point <= np.maximum(start, end) + coordinate_tolerance)
        )

    return bool(
        (abs(one) <= tolerance and on_segment(first_a, first_b, second_a))
        or (abs(two) <= tolerance and on_segment(first_a, first_b, second_b))
        or (abs(three) <= tolerance and on_segment(second_a, second_b, first_a))
        or (abs(four) <= tolerance and on_segment(second_a, second_b, first_b))
    )


def _is_simple_polygon(points: np.ndarray, tolerance: float) -> bool:
    count = len(points)
    for first in range(count):
        first_next = (first + 1) % count
        for second in range(first + 1, count):
            second_next = (second + 1) % count
            if first in {second, second_next} or first_next in {second, second_next}:
                continue
            if _segments_intersect(
                points[first],
                points[first_next],
                points[second],
                points[second_next],
                tolerance,
            ):
                return False
    return True


def _triangulate_cycle(
    mesh: trimesh.Trimesh,
    cycle: list[int],
    *,
    max_hole_diameter_mm: float,
    max_hole_edges: int,
    planarity_tolerance_mm: float,
) -> list[list[int]]:
    if len(cycle) > max_hole_edges:
        return []
    points_3d = np.asarray(mesh.vertices)[cycle]
    dimensions = points_3d.max(axis=0) - points_3d.min(axis=0)
    if float(np.linalg.norm(dimensions)) > max_hole_diameter_mm:
        return []

    centered = points_3d - points_3d.mean(axis=0)
    _, _, basis = np.linalg.svd(centered, full_matrices=False)
    normal = basis[-1]
    if float(np.max(np.abs(centered @ normal))) > planarity_tolerance_mm:
        return []
    points_2d = centered @ basis[:2].T
    scale = max(float(np.linalg.norm(dimensions)), 1.0)
    tolerance = scale * scale * 1e-12
    if not _is_simple_polygon(points_2d, tolerance):
        return []

    signed_area = sum(
        _cross_2d(points_2d[index], points_2d[(index + 1) % len(cycle)])
        for index in range(len(cycle))
    ) * 0.5
    if abs(signed_area) <= tolerance:
        return []
    if signed_area < 0:
        cycle = list(reversed(cycle))
        points_2d = points_2d[::-1]

    remaining = list(range(len(cycle)))
    triangles: list[list[int]] = []
    while len(remaining) > 3:
        ear_found = False
        for position, current in enumerate(remaining):
            previous = remaining[position - 1]
            following = remaining[(position + 1) % len(remaining)]
            first, second, third = (
                points_2d[previous],
                points_2d[current],
                points_2d[following],
            )
            if _cross_2d(second - first, third - second) <= tolerance:
                continue
            if any(
                _point_in_triangle(points_2d[other], first, second, third, tolerance)
                for other in remaining
                if other not in {previous, current, following}
            ):
                continue
            triangles.append([cycle[previous], cycle[current], cycle[following]])
            remaining.pop(position)
            ear_found = True
            break
        if not ear_found:
            return []
    triangles.append([cycle[index] for index in remaining])
    return triangles


def _fill_small_planar_holes(
    mesh: trimesh.Trimesh,
    *,
    max_hole_diameter_mm: float,
    max_hole_edges: int,
    planarity_tolerance_mm: float,
) -> tuple[trimesh.Trimesh, int, int]:
    boundary, oriented_boundary = _boundary_edges(mesh)
    cycles = _extract_simple_cycles(boundary)
    boundary_directions = {tuple(int(value) for value in edge) for edge in oriented_boundary}
    new_faces: list[list[int]] = []
    filled_holes = 0

    for cycle in cycles:
        triangles = _triangulate_cycle(
            mesh,
            cycle,
            max_hole_diameter_mm=max_hole_diameter_mm,
            max_hole_edges=max_hole_edges,
            planarity_tolerance_mm=planarity_tolerance_mm,
        )
        if not triangles:
            continue
        for triangle in triangles:
            directed_edges = (
                (triangle[0], triangle[1]),
                (triangle[1], triangle[2]),
                (triangle[2], triangle[0]),
            )
            if any(edge in boundary_directions for edge in directed_edges):
                triangle.reverse()
        new_faces.extend(triangles)
        filled_holes += 1

    if not new_faces:
        return mesh.copy(), 0, 0
    repaired = trimesh.Trimesh(
        vertices=np.asarray(mesh.vertices).copy(),
        faces=np.vstack((np.asarray(mesh.faces), np.asarray(new_faces, dtype=np.int64))),
        process=False,
    )
    repaired.remove_unreferenced_vertices()
    return repaired, filled_holes, len(new_faces)


def _remove_degenerate_and_duplicate_faces(
    vertices: np.ndarray, faces: np.ndarray
) -> np.ndarray:
    """Remove only faces which cannot contribute printable surface area."""
    faces = np.asarray(faces, dtype=np.int64)
    distinct = (
        (faces[:, 0] != faces[:, 1])
        & (faces[:, 1] != faces[:, 2])
        & (faces[:, 2] != faces[:, 0])
    )
    faces = faces[distinct]
    if not len(faces):
        return faces
    cross = np.cross(
        vertices[faces[:, 1]] - vertices[faces[:, 0]],
        vertices[faces[:, 2]] - vertices[faces[:, 0]],
    )
    faces = faces[np.linalg.norm(cross, axis=1) > 1e-14]
    if not len(faces):
        return faces
    canonical = np.sort(faces, axis=1)
    _, indices = np.unique(canonical, axis=0, return_index=True)
    return faces[np.sort(indices)]


def _collapse_micro_edges(
    mesh: trimesh.Trimesh, *, tolerance_mm: float
) -> tuple[trimesh.Trimesh, int]:
    """Collapse existing sub-micron sliver edges without welding nearby shells.

    Only vertices already joined by a triangle edge are eligible.  This is an
    important safety property: two intentionally separate walls are never
    merged merely because they happen to be close in space.
    """
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    if not len(faces):
        return mesh.copy(), 0
    edges = np.sort(faces[:, [[0, 1], [1, 2], [2, 0]]].reshape((-1, 2)), axis=1)
    edges = np.unique(edges, axis=0)
    lengths = np.linalg.norm(vertices[edges[:, 0]] - vertices[edges[:, 1]], axis=1)
    short_edges = edges[(lengths > 0.0) & (lengths <= tolerance_mm)]
    if not len(short_edges):
        return mesh.copy(), 0

    parent = np.arange(len(vertices), dtype=np.int64)

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = int(parent[value])
        return value

    for left_value, right_value in short_edges:
        left, right = find(int(left_value)), find(int(right_value))
        if left != right:
            parent[max(left, right)] = min(left, right)
    roots = np.asarray([find(index) for index in range(len(vertices))], dtype=np.int64)
    groups: dict[int, list[int]] = {}
    for index, root in enumerate(roots):
        groups.setdefault(int(root), []).append(index)
    collapsed_vertices = vertices.copy()
    for members in groups.values():
        if len(members) > 1:
            collapsed_vertices[members] = vertices[members].mean(axis=0)
    remapped_faces = roots[faces]
    remapped_faces = _remove_degenerate_and_duplicate_faces(
        collapsed_vertices, remapped_faces
    )
    repaired = trimesh.Trimesh(
        vertices=collapsed_vertices, faces=remapped_faces, process=False
    )
    repaired.remove_unreferenced_vertices()
    return repaired, len(short_edges)


def _stitch_collinear_t_junctions(
    mesh: trimesh.Trimesh, *, tolerance_mm: float, max_passes: int = 8
) -> tuple[trimesh.Trimesh, int, int]:
    """Split long boundary edges at matching collinear boundary vertices.

    Exporters sometimes create a T-junction where one surface has one long
    edge while its neighbour uses two or more collinear edges.  Splitting the
    existing triangle makes both descriptions identical and changes neither
    the outline nor the physical surface of the model.
    """
    repaired = mesh.copy()
    stitched = 0
    added = 0
    for _ in range(max_passes):
        boundary, _ = _boundary_edges(repaired)
        if not len(boundary):
            break
        boundary_vertices = np.unique(boundary.reshape(-1))
        vertices = np.asarray(repaired.vertices, dtype=np.float64)
        faces = np.asarray(repaired.faces, dtype=np.int64)
        changed = False
        for left_value, right_value in boundary:
            left, right = int(left_value), int(right_value)
            start, end = vertices[left], vertices[right]
            vector = end - start
            squared_length = float(np.dot(vector, vector))
            if squared_length <= tolerance_mm * tolerance_mm:
                continue
            candidates: list[tuple[float, int]] = []
            for candidate_value in boundary_vertices:
                candidate = int(candidate_value)
                if candidate in {left, right}:
                    continue
                relative = vertices[candidate] - start
                amount = float(np.dot(relative, vector) / squared_length)
                if amount <= 1e-7 or amount >= 1.0 - 1e-7:
                    continue
                closest = start + amount * vector
                if float(np.linalg.norm(vertices[candidate] - closest)) <= tolerance_mm:
                    candidates.append((amount, candidate))
            if not candidates:
                continue

            face_index = next(
                (
                    index
                    for index, face in enumerate(faces)
                    if left in face and right in face
                ),
                None,
            )
            if face_index is None:
                continue
            face = [int(value) for value in faces[face_index]]
            directed: tuple[int, int, int] | None = None
            for index in range(3):
                first, second = face[index], face[(index + 1) % 3]
                if {first, second} == {left, right}:
                    directed = (first, second, face[(index + 2) % 3])
                    break
            if directed is None:
                continue
            first, second, opposite = directed
            ordered = [value for _, value in sorted(candidates)]
            if first == right:
                ordered.reverse()
            sequence = [first, *ordered, second]
            replacement = np.asarray(
                [
                    [sequence[index], sequence[index + 1], opposite]
                    for index in range(len(sequence) - 1)
                ],
                dtype=np.int64,
            )
            faces = np.vstack((np.delete(faces, face_index, axis=0), replacement))
            repaired = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
            stitched += 1
            added += len(replacement) - 1
            changed = True
            break
        if not changed:
            break
    repaired.remove_unreferenced_vertices()
    return repaired, stitched, added


def repair_stl(
    source: str | Path,
    output: str | Path,
    *,
    max_hole_diameter_mm: float = 5.0,
    max_hole_edges: int = 100,
    planarity_tolerance_mm: float = 0.05,
) -> RepairResult:
    """Write a cleaned STL copy without ever overwriting the source or output."""
    source_path = Path(source).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if source_path == output_path:
        raise AnalysisError("repair output must be different from the source STL")
    if output_path.suffix.lower() != ".stl":
        raise AnalysisError("repair output must use the .stl extension")
    if output_path.exists():
        raise AnalysisError(f"repair output already exists: {output_path}")
    if not output_path.parent.is_dir():
        raise AnalysisError(f"repair output directory not found: {output_path.parent}")
    if not 0.0 < max_hole_diameter_mm <= 50.0:
        raise AnalysisError("max hole diameter must be between 0 and 50 mm")

    raw_mesh = _load_stl(source_path)
    before_health, cleaned_mesh = _analyze_mesh_health(raw_mesh)
    dimensions = np.asarray(cleaned_mesh.extents, dtype=np.float64)
    diagonal = max(float(np.linalg.norm(dimensions)), 1.0)
    # 0.2 micrometre is below STL/printer resolution but large enough to
    # remove the degenerate slivers produced by common CAD tessellators.
    micro_tolerance = min(0.001, max(0.0002, diagonal * 1e-7))
    collapsed_mesh, collapsed_micro_edges = _collapse_micro_edges(
        cleaned_mesh, tolerance_mm=micro_tolerance
    )
    stitched_mesh, stitched_t_junctions, stitch_triangles = (
        _stitch_collinear_t_junctions(
            collapsed_mesh, tolerance_mm=max(micro_tolerance * 2.0, diagonal * 1e-7)
        )
    )
    repaired_mesh, filled_holes, hole_triangles = _fill_small_planar_holes(
        stitched_mesh,
        max_hole_diameter_mm=max_hole_diameter_mm,
        max_hole_edges=max_hole_edges,
        planarity_tolerance_mm=planarity_tolerance_mm,
    )
    added_triangles = stitch_triangles + hole_triangles
    after_health, final_mesh = _analyze_mesh_health(repaired_mesh)

    if after_health.boundary_edge_count > before_health.boundary_edge_count:
        raise AnalysisError("repair was rejected because it created more open edges")
    if after_health.non_manifold_edge_count > before_health.non_manifold_edge_count:
        raise AnalysisError("repair was rejected because it created non-manifold edges")
    before_bounds = np.asarray(cleaned_mesh.bounds, dtype=np.float64)
    after_bounds = np.asarray(final_mesh.bounds, dtype=np.float64)
    bounds_tolerance = max(0.002, diagonal * 1e-6)
    if before_bounds.shape != after_bounds.shape or float(
        np.max(np.abs(before_bounds - after_bounds))
    ) > bounds_tolerance:
        raise AnalysisError("repair was rejected because it changed the model outline")
    if after_health.meaningful_body_count != before_health.meaningful_body_count:
        raise AnalysisError("repair was rejected because it changed the model body count")

    final_mesh.export(output_path, file_type="stl")
    return RepairResult(
        source_path=source_path,
        output_path=output_path,
        removed_debris_bodies=before_health.debris_body_count,
        removed_triangles=before_health.ignored_triangle_count,
        filled_holes=filled_holes,
        added_triangles=added_triangles,
        collapsed_micro_edges=collapsed_micro_edges,
        stitched_t_junctions=stitched_t_junctions,
        boundary_edges_before=before_health.boundary_edge_count,
        boundary_edges_after=after_health.boundary_edge_count,
        non_manifold_edges_before=before_health.non_manifold_edge_count,
        non_manifold_edges_after=after_health.non_manifold_edge_count,
        final_status=after_health.status,
    )
