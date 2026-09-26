"""Parse tool-call argument JSON from provider responses."""

from __future__ import annotations

import ast
import json
from typing import Any

from src.core.logger import logger

_JSON_SAFE_KEY_TYPES = (str, int, float, bool, type(None))


def _json_safe(value: Any) -> Any:
    """Recursively convert ast.literal_eval byproducts to JSON-safe types.

    tuple/set become list, bytes become str, and non-scalar dict keys are
    stringified — otherwise a poisoned tool_call would crash json.dumps on
    every later history replay.
    """
    if isinstance(value, dict):
        return {
            (key if isinstance(key, _JSON_SAFE_KEY_TYPES) else str(key)): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def parse_tool_call_arguments(raw: str) -> dict[str, Any]:
    """Parse tool arguments; fall back to ast.literal_eval then ``{"_raw": ...}``."""
    text = (raw or "").strip()
    if not text:
        return {}

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Failed to parse tool call arguments as JSON: {}. Raw: {}",
            exc,
            text[:200],
        )
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                return {"_raw": text}
            if isinstance(parsed, dict):
                return _json_safe(parsed)
            return {"_raw": text}
        return {"_raw": text}

    if isinstance(parsed, dict):
        return parsed
    return {"_raw": text}


def _empty_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}}


def sanitize_tool_parameters(tool: dict[str, Any], *, strict: bool = False) -> dict[str, Any]:
    """Sanitize a tool definition's ``parameters`` schema.

    Lenient mode (default, provider wire conversion): non-dict parameters are
    replaced with an empty object schema (with warning); a dict missing
    ``type``/``properties`` gets those keys filled in place.

    Strict mode (validation/logging paths): a dict whose ``type`` is not
    ``"object"`` is replaced wholesale with the empty object schema.
    """
    name = tool.get("name", "")
    parameters = tool.get("parameters", _empty_schema())

    if not isinstance(parameters, dict):
        if not strict:
            logger.warning(f"Tool '{name}' parameters is not a dict, using empty schema")
        return _empty_schema()

    if strict:
        if parameters.get("type") != "object":
            return _empty_schema()
        return parameters

    if "type" not in parameters:
        parameters["type"] = "object"
    if "properties" not in parameters:
        parameters["properties"] = {}
    return parameters
