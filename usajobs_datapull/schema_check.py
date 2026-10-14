"""Pure-Python validator for the JSON Schema subset our schemas use.

Avoids `jsonschema` (its rpds-py dependency is a compiled extension).
Supported: type, required, properties, additionalProperties, items, enum,
pattern, minLength, minimum. Any other keyword is an error.
"""
from __future__ import annotations

import re

SUPPORTED = {"$schema", "$id", "title", "description", "type", "required", "properties",
             "additionalProperties", "items", "enum", "pattern", "minLength", "minimum"}

_TYPES = {
    "null": lambda v: v is None,
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def errors(instance, schema: dict, path: str = "$") -> list[str]:
    """Every way `instance` fails `schema`, as readable messages. [] = valid."""
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise ValueError(f"schema at {path} uses unsupported keyword(s): {sorted(unknown)}")
    out: list[str] = []

    if "type" in schema:
        allowed = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[t](instance) for t in allowed):
            return [f"{path}: expected {'/'.join(allowed)}, got {type(instance).__name__}"]
    if "enum" in schema and instance not in schema["enum"]:
        out.append(f"{path}: {instance!r} not one of {schema['enum']}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            out.append(f"{path}: shorter than {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            out.append(f"{path}: {instance!r} doesn't match {schema['pattern']}")
    if _TYPES["number"](instance) and "minimum" in schema and instance < schema["minimum"]:
        out.append(f"{path}: {instance} < minimum {schema['minimum']}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            out += errors(item, schema["items"], f"{path}[{i}]")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                out.append(f"{path}: missing required '{key}'")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in props:
                out += errors(value, props[key], f"{path}.{key}")
            elif extra is False:
                out.append(f"{path}: unexpected field '{key}'")
            elif isinstance(extra, dict):
                out += errors(value, extra, f"{path}.{key}")
    return out
