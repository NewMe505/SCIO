"""Pydantic model → provider-safe JSON Schema ("wire schema").

Providers' constrained decoding supports a subset of JSON Schema. This module inlines
``$ref``s, drops unsupported keywords (length/numeric constraints, patterns, titles,
defaults) and makes every object closed with all properties required. The *full*
Pydantic model is still validated locally after the call, so nothing is lost: the
provider guarantees the shape, local validation enforces the constraints.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

STRIPPED_KEYS: frozenset[str] = frozenset(
    {
        "title",
        "default",
        "examples",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "pattern",
        "minProperties",
        "maxProperties",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
)

SUPPORTED_FORMATS: frozenset[str] = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)


class WireSchemaError(ValueError):
    pass


def wire_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    """Return the provider-safe schema for ``model_cls``."""
    raw = model_cls.model_json_schema()
    defs: dict[str, Any] = raw.pop("$defs", {})
    result = _walk(raw, defs, stack=(), path=model_cls.__name__)
    assert isinstance(result, dict)
    return result


def _walk(node: Any, defs: dict[str, Any], *, stack: tuple[str, ...], path: str) -> Any:
    if isinstance(node, list):
        return [_walk(n, defs, stack=stack, path=path) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        if name in stack:
            raise WireSchemaError(f"{path}: recursive schema via {name!r} is not supported")
        if name not in defs:
            raise WireSchemaError(f"{path}: unresolved $ref {node['$ref']!r}")
        merged = copy.deepcopy(defs[name])
        for k, v in node.items():
            if k != "$ref":
                merged[k] = v  # keep sibling description etc.
        return _walk(merged, defs, stack=(*stack, name), path=path)

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in STRIPPED_KEYS:
            continue
        if key == "format":
            if value in SUPPORTED_FORMATS:
                out[key] = value
            continue
        if key == "properties":
            out[key] = {
                pk: _walk(pv, defs, stack=stack, path=f"{path}.{pk}") for pk, pv in value.items()
            }
        elif key in ("anyOf", "allOf", "oneOf", "prefixItems"):
            out[key] = [_walk(v, defs, stack=stack, path=path) for v in value]
        elif key in ("items", "not") and isinstance(value, dict):
            out[key] = _walk(value, defs, stack=stack, path=path)
        elif key == "additionalProperties":
            if value is not False:
                raise WireSchemaError(
                    f"{path}: free-form mappings (dict fields) are not supported in structured "
                    "output; use a list of key/value models"
                )
            out[key] = False
        else:
            out[key] = value

    if out.get("type") == "object" or "properties" in out:
        props = out.get("properties", {})
        out["type"] = "object"
        out["required"] = list(props.keys())
        out["additionalProperties"] = False
    return out
