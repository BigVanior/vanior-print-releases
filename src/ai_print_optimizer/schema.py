"""Bundled release schemas and dependency-free structural validation."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

SCHEMAS = {
    "pipeline-manifest": "pipeline-manifest.schema.json",
    "batch-summary": "batch-summary.schema.json",
}


def load_schema(name: str) -> dict[str, Any]:
    try:
        filename = SCHEMAS[name]
    except KeyError as exc:
        raise ValueError(f"unknown schema: {name}") from exc
    resource = files("ai_print_optimizer").joinpath("schemas", filename)
    payload = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"schema is not an object: {name}")
    return payload


def _type_matches(value: Any, expected: str | list[str]) -> bool:
    if isinstance(expected, list):
        return any(_type_matches(value, item) for item in expected)
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def _validate_node(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    if (
        isinstance(expected_type, str)
        or isinstance(expected_type, list)
        and all(isinstance(item, str) for item in expected_type)
    ) and not _type_matches(value, expected_type):
        expected_label = (
            " or ".join(expected_type)
            if isinstance(expected_type, list)
            else expected_type
        )
        return [f"{path}: expected {expected_label}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected constant {schema['const']!r}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key}")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, child_schema in properties.items():
                if key in value and isinstance(child_schema, dict):
                    errors.extend(_validate_node(value[key], child_schema, f"{path}.{key}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(_validate_node(item, schema["items"], f"{path}[{index}]"))
    return errors


def validate_release_document(payload: Any, schema_name: str) -> tuple[str, ...]:
    return tuple(_validate_node(payload, load_schema(schema_name), "$"))
