"""Tests for parse_tool_call_arguments JSON parsing."""

from __future__ import annotations

import json

from src.llm.tool_arguments import parse_tool_call_arguments


def test_parse_tool_call_arguments() -> None:
    assert parse_tool_call_arguments('{"path": "/x", "contents": "y"}') == {
        "path": "/x",
        "contents": "y",
    }
    assert parse_tool_call_arguments("") == {}
    assert parse_tool_call_arguments("   ") == {}

    truncated = '{"path": "/x", "contents": "aaa'
    assert parse_tool_call_arguments(truncated)["_raw"] == truncated

    non_dict = '"just a string"'
    assert parse_tool_call_arguments(non_dict)["_raw"] == non_dict

    # ast.literal_eval fallback can produce tuple/set/bytes — must be JSON-safe.
    py_types = parse_tool_call_arguments("{'items': (1, 2), 'tags': {'a', 'b'}, 'blob': b'xy'}")
    assert py_types["items"] == [1, 2]
    assert sorted(py_types["tags"]) == ["a", "b"]
    assert py_types["blob"] == "xy"
    json.dumps(py_types)

    tuple_key = parse_tool_call_arguments("{(1, 2): 'v'}")
    assert tuple_key == {"(1, 2)": "v"}
    json.dumps(tuple_key)
