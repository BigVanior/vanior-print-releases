"""Privacy-preserving federation client for community PrintDNA learning."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .print_dna import KNOWN_DEFECTS, KNOWN_REGIONS


class LearningSyncError(RuntimeError):
    """Raised when a community learning synchronization cannot be completed."""


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        return {"schema_version": 2, "profiles": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 2, "profiles": {}}
    return value if isinstance(value, dict) else {"schema_version": 2, "profiles": {}}


def build_anonymous_feedback_batch(path: str | Path, *, limit: int = 100) -> dict[str, Any]:
    """Return only print telemetry useful for training, never paths/models/photos/notes."""
    store = _read_json(Path(path).expanduser().resolve())
    rows: list[dict[str, Any]] = []
    profiles = store.get("profiles", {})
    if isinstance(profiles, dict):
        for record in profiles.values():
            if not isinstance(record, dict) or not isinstance(record.get("key"), dict):
                continue
            key = record["key"]
            safe_key = {
                "printer_model": str(key.get("printer_model", ""))[:100],
                "nozzle_diameter_mm": float(key.get("nozzle_diameter_mm", 0.4)),
                "material": str(key.get("material", ""))[:40].upper(),
            }
            feedback = record.get("feedback", [])
            if not isinstance(feedback, list):
                continue
            for item in feedback[-limit:]:
                if not isinstance(item, dict):
                    continue
                allowed_parameters = {
                    "printer", "nozzle", "material", "priority", "purpose",
                    "layer_height_mm", "wall_loops", "sparse_infill_percent",
                    "supports", "support_top_z_distance_mm", "support_type",
                    "outer_wall_speed_mm_s", "top_surface_speed_mm_s",
                }
                snapshot = {
                    str(name)[:80]: value
                    for name, value in dict(item.get("parameter_snapshot") or {}).items()
                    if str(name) in allowed_parameters
                    if isinstance(value, (str, int, float, bool)) or value is None
                }
                rows.append({
                    "key": safe_key,
                    # Anonymous protocol v1 remains on a 1–5 scale. Convert
                    # the local 1–10 value so older community services remain
                    # compatible while the application keeps finer feedback.
                    "quality_rating": max(
                        1,
                        min(5, (int(item.get("quality_rating", 6)) + 1) // 2),
                    ),
                    "support_removal_rating": item.get("support_removal_rating"),
                    "dimensional_rating": item.get("dimensional_rating"),
                    "defects": [name for name in item.get("defects", []) if name in KNOWN_DEFECTS],
                    "defect_layer_index": item.get("defect_layer_index"),
                    "defect_region": item.get("defect_region") if item.get("defect_region") in KNOWN_REGIONS else "whole",
                    "parameter_snapshot": snapshot,
                })
    return {"schema": "vanior-anonymous-feedback-v1", "feedback": rows[-limit:]}


def _validated_endpoint(endpoint: str) -> str:
    parsed = urllib.parse.urlparse(endpoint.strip())
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise LearningSyncError("community endpoint must be an HTTPS URL without credentials")
    return endpoint.rstrip("/")


def sync_community_learning(
    local_store: str | Path,
    global_store: str | Path,
    endpoint: str,
    *,
    timeout_s: float = 20.0,
) -> dict[str, Any]:
    """Upload an anonymous batch and atomically download the aggregated model."""
    base = _validated_endpoint(endpoint)
    body = json.dumps(build_anonymous_feedback_batch(local_store), separators=(",", ":")).encode("utf-8")
    context = ssl.create_default_context()
    try:
        request = urllib.request.Request(
            f"{base}/feedback", data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout_s, context=context) as response:
            if response.status not in {200, 201, 202, 204}:
                raise LearningSyncError(f"feedback server returned HTTP {response.status}")
        request = urllib.request.Request(f"{base}/model", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout_s, context=context) as response:
            payload = response.read(16 * 1024 * 1024 + 1)
            if len(payload) > 16 * 1024 * 1024:
                raise LearningSyncError("community model is too large")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise LearningSyncError(f"community synchronization failed: {exc}") from exc
    try:
        model = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LearningSyncError("community server returned malformed JSON") from exc
    if not isinstance(model, dict) or model.get("schema_version") != 2 or not isinstance(model.get("profiles"), dict):
        raise LearningSyncError("community model has an unsupported schema")
    destination = Path(global_store).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {"uploaded": len(build_anonymous_feedback_batch(local_store)["feedback"]), "profiles": len(model["profiles"])}
