"""The offline schema checker behind codex role validation: it types TOML
values the way codex's serde does and refuses vocabulary it lacks."""

from __future__ import annotations

import datetime

import pytest
from theozolith_knowledge import schemacheck
from theozolith_knowledge.schemacheck import SchemaViolation, UnsupportedSchema, check, render_path


def _violation(instance, schema) -> str:
    with pytest.raises(SchemaViolation) as info:
        check(instance, schema)
    return str(info.value)


def test_booleans_are_never_integers_and_integers_are_numbers():
    check(1, {"type": "integer"})
    check(1, {"type": "number"})
    check(1.5, {"type": "number"})
    assert _violation(True, {"type": "integer"}) == "expected integer, got boolean"
    assert _violation(True, {"type": "number"}) == "expected number, got boolean"
    assert _violation(1.5, {"type": "integer"}) == "expected integer, got float"
    assert _violation(1, {"type": "boolean"}) == "expected boolean, got integer"


def test_toml_native_kinds_satisfy_no_json_type():
    for value, kind in [
        (datetime.datetime(2026, 9, 5, 1, 2, 3), "datetime"),
        (datetime.date(2026, 9, 5), "date"),
        (datetime.time(1, 2, 3), "time"),
    ]:
        assert _violation(value, {"type": "string"}) == f"expected string, got {kind}"
        assert (
            _violation(value, {"type": ["string", "null"]})
            == f"expected string or null, got {kind}"
        )


def test_type_lists_and_tables_read_as_toml_words():
    check("x", {"type": ["integer", "string"]})
    assert _violation([], {"type": "object"}) == "expected table, got array"
    assert _violation({}, {"type": "array"}) == "expected array, got table"


def test_integers_outside_toml_64_bit_range_are_refused_everywhere():
    check(2**63 - 1, {"type": "integer"})
    check(-(2**63), {})
    assert "outside TOML's 64-bit integer range" in _violation(2**63, {})
    assert "outside TOML's 64-bit integer range" in _violation(-(2**63) - 1, {"type": "number"})


def test_integer_formats_are_enforced_as_serde_widths():
    check(65535, {"type": "integer", "format": "uint16"})
    assert "does not fit uint16 (0 to 65535)" in _violation(
        65536, {"type": "integer", "format": "uint16"}
    )
    assert "does not fit uint (0 to" in _violation(-1, {"type": "integer", "format": "uint"})
    assert "does not fit int32" in _violation(2**31, {"type": "integer", "format": "int32"})
    check(1.5, {"type": "number", "format": "double"})
    check(3, {"type": "number", "format": "double"})
    with pytest.raises(UnsupportedSchema, match="unsupported format 'uri'"):
        check("x", {"type": "string", "format": "uri"})


def test_minimum_and_maximum_render_integral_bounds_plainly():
    assert _violation(-1, {"type": "integer", "minimum": 0.0}) == "-1 is below the minimum of 0"
    assert _violation(2, {"type": "number", "maximum": 1.0}) == "2 is above the maximum of 1"
    assert _violation(2, {"maximum": 1.5}) == "2 is above the maximum of 1.5"
    check(True, {"minimum": 5})  # numeric keywords ignore non-numbers


def test_string_keywords():
    assert _violation("", {"minLength": 1}) == "must be at least 1 character(s), got 0"
    assert _violation("abc", {"maxLength": 2}) == "must be at most 2 character(s), got 3"
    assert (
        _violation("a b", {"pattern": "^[a-z_-]+$"})
        == "'a b' does not match the pattern ^[a-z_-]+$"
    )
    check("ok_name", {"pattern": "^[a-z_-]+$", "minLength": 1, "maxLength": 64})


def test_enum_keeps_booleans_apart_from_numbers():
    check("read-only", {"enum": ["read-only", "workspace-write"]})
    check(1, {"enum": [1, 2]})
    assert _violation(True, {"enum": [1]}) == "True is not one of 1"
    assert _violation("yolo", {"enum": ["a", "b"]}) == "'yolo' is not one of 'a', 'b'"


CLOSED = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "args": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["command"],
    "additionalProperties": False,
}


def test_closed_tables_refuse_unknown_fields_and_require_named_ones():
    check({"command": "npx", "args": ["-y"]}, CLOSED)
    assert _violation({"command": "x", "comand": "y"}, CLOSED) == "unknown field 'comand'"
    assert _violation({"args": []}, CLOSED) == "missing required field 'command'"
    assert (
        _violation({"command": "x", "args": ["a", 1]}, CLOSED)
        == "args[1]: expected string, got integer"
    )


def test_open_tables_and_maps():
    check(
        {"command": "x", "extra": 1},
        {"type": "object", "properties": {"command": {"type": "string"}}},
    )
    map_schema = {"type": "object", "additionalProperties": {"type": "string"}}
    check({"A": "1", "B": "2"}, map_schema)
    assert _violation({"A": 1}, map_schema) == "A: expected string, got integer"
    check({"anything": [1, {}]}, {"type": "object", "additionalProperties": True})


def test_nested_locations_render_toml_style():
    schema = {
        "type": "object",
        "properties": {
            "mcp_servers": {"type": "object", "additionalProperties": CLOSED},
        },
    }
    assert (
        _violation({"mcp_servers": {"docs": {"command": "x", "args": [1]}}}, schema)
        == "mcp_servers.docs.args[0]: expected string, got integer"
    )
    assert (
        _violation({"mcp_servers": {"my server": {"command": 1}}}, schema)
        == 'mcp_servers."my server".command: expected string, got integer'
    )
    assert render_path(()) == ""
    assert render_path(("a", 0, "b.c")) == 'a[0]."b.c"'


def test_local_refs_resolve_and_combine_with_all_of():
    schema = {
        "definitions": {"Effort": {"type": "string", "minLength": 1}},
        "type": "object",
        "properties": {
            "effort": {"allOf": [{"$ref": "#/definitions/Effort"}], "description": "doc"},
            "again": {"$ref": "#/definitions/Effort"},
        },
    }
    check({"effort": "low", "again": "high"}, schema)
    assert _violation({"effort": ""}, schema) == "effort: must be at least 1 character(s), got 0"
    assert _violation({"again": 2}, schema) == "again: expected string, got integer"


def test_any_of_reports_the_deepest_branch_or_every_alternative():
    branches = {
        "anyOf": [
            {"type": "string"},
            {
                "type": "object",
                "properties": {"kind": {"type": "string"}},
                "additionalProperties": False,
            },
        ]
    }
    check("token", branches)
    check({"kind": "oauth"}, branches)
    assert _violation(5, branches) == (
        "matches none of the allowed shapes "
        "(expected string, got integer; expected table, got integer)"
    )
    # A branch that got INSIDE the table diagnosed the real mistake.
    assert _violation({"kind": 1}, branches) == "kind: expected string, got integer"


def test_one_of_requires_exactly_one_match():
    schema = {"oneOf": [{"type": "integer"}, {"type": "number"}]}
    check(1.5, schema)
    assert _violation(1, schema) == "matches 2 of the allowed shapes, where exactly one is allowed"
    assert "matches none of the allowed shapes" in _violation("x", schema)


def test_not_names_fields_that_cannot_be_combined():
    schema = {"type": "object", "allOf": [{"not": {"required": ["exclude", "filters"]}}]}
    check({"exclude": 1}, schema)
    assert _violation({"exclude": 1, "filters": 2}, schema) == (
        "fields 'exclude' and 'filters' cannot be set together"
    )
    assert _violation(1, {"not": {"type": "integer"}}) == "matches a shape that is not allowed here"


def test_boolean_schemas():
    check({"anything": 1}, True)
    assert _violation(1, False) == "no value is allowed here"


@pytest.mark.parametrize(
    ("schema", "problem"),
    [
        ({"const": 1}, "unsupported keyword 'const'"),
        ({"patternProperties": {}}, "unsupported keyword 'patternProperties'"),
        ({"items": [{"type": "string"}]}, "tuple-form 'items' is unsupported"),
        ({"type": "date"}, "unknown type 'date'"),
        ({"format": "uri"}, "unsupported format 'uri'"),
        ({"$ref": "#/definitions/Missing"}, "unresolvable $ref '#/definitions/Missing'"),
        ({"$ref": "http://example.invalid/schema"}, "non-local $ref"),
        ({"properties": {"x": 3}}, "#/properties/x: schema node is integer"),
    ],
)
def test_schema_problems_finds_unsupported_vocabulary(schema, problem):
    problems = schemacheck.schema_problems(schema)
    assert any(problem in entry for entry in problems), problems


def test_check_refuses_unsupported_vocabulary_it_meets():
    with pytest.raises(UnsupportedSchema, match="unsupported keyword 'const'"):
        check(1, {"const": 1})
    with pytest.raises(UnsupportedSchema, match="unknown type 'date'"):
        check(1, {"type": "date"})
    with pytest.raises(UnsupportedSchema, match="cyclic"):
        check(
            1,
            {
                "definitions": {"a": {"$ref": "#/definitions/b"}, "b": {"$ref": "#/definitions/a"}},
                "$ref": "#/definitions/a",
            },
        )


def test_pathological_depth_surfaces_as_a_recursion_error_for_the_caller_to_map():
    schema = {
        "definitions": {
            "n": {"type": "object", "additionalProperties": {"$ref": "#/definitions/n"}}
        },
        "$ref": "#/definitions/n",
    }
    nested: dict = {}
    for _ in range(5000):
        nested = {"a": nested}
    with pytest.raises(RecursionError):
        check(nested, schema)
