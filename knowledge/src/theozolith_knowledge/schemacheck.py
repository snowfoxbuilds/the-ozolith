"""A JSON Schema (draft-07 subset) checker for the vendored codex config schema.

theozolith-knowledge has no runtime dependencies (ADR-0007: the laptop-only
knowledge user installs nothing else), so it cannot lean on the `jsonschema`
package. This module implements exactly the keyword subset the vendored codex
schema uses (``codexrole.CODEX_SCHEMA_BASELINE``) and REFUSES any other
keyword it meets: a schema update that brings new vocabulary fails loudly
instead of silently validating less than it claims. ``schema_problems``
walks a whole schema up front so the refusal never waits for the one role
file that happens to reach the unsupported node.

Instances are the Python values ``tomllib`` produces, typed the way codex's
serde deserialization types TOML: a boolean is never an integer (Python's
bool-is-int does not apply), an integer satisfies "number" but a float never
satisfies "integer", TOML datetimes, dates, and times satisfy no JSON type,
and an integer outside the 64-bit range codex's TOML parser accepts is
refused wherever it appears. ``format`` carries serde's integer widths
(``uint16``, ``int64``, ...) and is enforced as a range.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterator
from dataclasses import dataclass

VALIDATION_KEYWORDS = frozenset(
    {
        "$ref",
        "type",
        "enum",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
    }
)
ANNOTATION_KEYWORDS = frozenset(
    {"$schema", "$id", "title", "description", "default", "examples", "deprecated", "definitions"}
)
JSON_TYPES = frozenset({"string", "integer", "number", "boolean", "array", "object", "null"})

_INTEGER_FORMATS = {
    "int8": (-(2**7), 2**7 - 1),
    "int16": (-(2**15), 2**15 - 1),
    "int32": (-(2**31), 2**31 - 1),
    "int64": (-(2**63), 2**63 - 1),
    "uint8": (0, 2**8 - 1),
    "uint16": (0, 2**16 - 1),
    "uint32": (0, 2**32 - 1),
    "uint64": (0, 2**64 - 1),
    "uint": (0, 2**64 - 1),
}
_FLOAT_FORMATS = frozenset({"float", "double"})
TOML_INTEGER_RANGE = _INTEGER_FORMATS["int64"]

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

Location = tuple[str | int, ...]


class UnsupportedSchema(ValueError):
    """The schema uses vocabulary this checker does not implement — a
    validator/schema mismatch, never a data error."""


@dataclass(frozen=True)
class SchemaViolation(ValueError):
    """The instance does not satisfy the schema. ``path`` locates the
    offending value; ``str()`` renders it TOML-style (``mcp_servers.docs.args[1]``)."""

    path: Location
    message: str

    @property
    def location(self) -> str:
        return render_path(self.path)

    def __str__(self) -> str:
        return f"{self.location}: {self.message}" if self.path else self.message


def render_path(path: Location) -> str:
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            key = part if _BARE_KEY_RE.match(part) else f'"{part}"'
            out += key if not out else f".{key}"
    return out


def kind_of(value: object) -> str:
    """The TOML kind of a ``tomllib`` value, in the words a TOML author uses."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "table"
    if isinstance(value, datetime.datetime):
        return "datetime"
    if isinstance(value, datetime.date):
        return "date"
    if isinstance(value, datetime.time):
        return "time"
    return type(value).__name__


def _satisfies_type(value: object, json_type: str) -> bool:
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "object":
        return isinstance(value, dict)
    if json_type == "null":
        return False  # TOML has no null
    raise UnsupportedSchema(f"unknown type {json_type!r}")


def _describe_type(json_type: str) -> str:
    return "table" if json_type == "object" else json_type


def _same(a: object, b: object) -> bool:
    """Equality that keeps booleans apart from numbers (``1 == True`` in Python)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _resolve_ref(root: dict, ref: str) -> object:
    if ref == "#":
        return root
    if not ref.startswith("#/"):
        raise UnsupportedSchema(f"non-local $ref {ref!r}")
    node: object = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise UnsupportedSchema(f"unresolvable $ref {ref!r}")
        node = node[part]
    return node


def _subschemas(schema: object) -> Iterator[tuple[str, object]]:
    """Every child schema of a schema node, labelled by the keyword path."""
    if not isinstance(schema, dict):
        return
    for keyword in ("properties", "definitions"):
        for name, sub in schema.get(keyword, {}).items():
            yield f"{keyword}/{name}", sub
    for keyword in ("items", "additionalProperties", "not"):
        if keyword in schema and not isinstance(schema[keyword], bool):
            yield keyword, schema[keyword]
    for keyword in ("allOf", "anyOf", "oneOf"):
        for index, sub in enumerate(schema.get(keyword, [])):
            yield f"{keyword}/{index}", sub


def schema_problems(schema: dict) -> list[str]:
    """Everything in ``schema`` this checker would refuse, each located by
    its keyword path. Empty means every node is fully supported."""
    problems: list[str] = []

    def walk(node: object, where: str) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            problems.append(f"{where}: schema node is {kind_of(node)}, not a table or boolean")
            return
        for keyword in node:
            if keyword not in VALIDATION_KEYWORDS and keyword not in ANNOTATION_KEYWORDS:
                problems.append(f"{where}: unsupported keyword {keyword!r}")
        if "items" in node and isinstance(node["items"], list):
            problems.append(f"{where}: tuple-form 'items' is unsupported")
        for json_type in _listed(node.get("type")):
            if json_type not in JSON_TYPES:
                problems.append(f"{where}: unknown type {json_type!r}")
        fmt = node.get("format")
        if fmt is not None and fmt not in _INTEGER_FORMATS and fmt not in _FLOAT_FORMATS:
            problems.append(f"{where}: unsupported format {fmt!r}")
        if "$ref" in node:
            try:
                _resolve_ref(schema, node["$ref"])
            except UnsupportedSchema as exc:
                problems.append(f"{where}: {exc}")
        for label, sub in _subschemas(node):
            walk(sub, f"{where}/{label}")

    walk(schema, "#")
    return problems


def _listed(value: object) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def check(instance: object, schema: dict) -> None:
    """Raise ``SchemaViolation`` if ``instance`` does not satisfy ``schema``
    (a draft-07 document whose ``$ref``s are local), ``UnsupportedSchema``
    if the schema uses vocabulary this checker lacks."""
    _check(instance, schema, (), schema, frozenset())


def _check(value: object, schema: object, path: Location, root: dict, refs: frozenset[str]) -> None:
    if schema is True:
        return
    if schema is False:
        raise SchemaViolation(path, "no value is allowed here")
    if not isinstance(schema, dict):
        raise UnsupportedSchema(f"schema node is {kind_of(schema)}, not a table or boolean")
    for keyword in schema:
        if keyword not in VALIDATION_KEYWORDS and keyword not in ANNOTATION_KEYWORDS:
            raise UnsupportedSchema(f"unsupported keyword {keyword!r}")
    fmt = schema.get("format")
    if fmt is not None and fmt not in _INTEGER_FORMATS and fmt not in _FLOAT_FORMATS:
        raise UnsupportedSchema(f"unsupported format {fmt!r}")

    if isinstance(value, int) and not isinstance(value, bool):
        low, high = TOML_INTEGER_RANGE
        if not low <= value <= high:
            raise SchemaViolation(path, f"{value} is outside TOML's 64-bit integer range")

    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in refs:
            raise UnsupportedSchema(f"cyclic $ref {ref!r} consumes no data")
        _check(value, _resolve_ref(root, ref), path, root, refs | {ref})

    if "type" in schema:
        allowed = _listed(schema["type"])
        if not any(_satisfies_type(value, t) for t in allowed):
            expected = " or ".join(_describe_type(t) for t in allowed)
            raise SchemaViolation(path, f"expected {expected}, got {kind_of(value)}")

    if "enum" in schema and not any(_same(value, option) for option in schema["enum"]):
        options = ", ".join(repr(option) for option in schema["enum"])
        raise SchemaViolation(path, f"{value!r} is not one of {options}")

    if isinstance(value, int | float) and not isinstance(value, bool):
        _check_number(value, schema, path)
    if isinstance(value, str):
        _check_string(value, schema, path)
    if isinstance(value, dict):
        _check_table(value, schema, path, root)
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _check(item, schema["items"], (*path, index), root, frozenset())

    for sub in schema.get("allOf", []):
        _check(value, sub, path, root, refs)
    if "anyOf" in schema:
        _check_branches(value, schema["anyOf"], path, root, refs, exactly_one=False)
    if "oneOf" in schema:
        _check_branches(value, schema["oneOf"], path, root, refs, exactly_one=True)
    if "not" in schema:
        _check_not(value, schema["not"], path, root, refs)


def _check_number(value: int | float, schema: dict, path: Location) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        raise SchemaViolation(path, f"{value} is below the minimum of {_num(schema['minimum'])}")
    if "maximum" in schema and value > schema["maximum"]:
        raise SchemaViolation(path, f"{value} is above the maximum of {_num(schema['maximum'])}")
    fmt = schema.get("format")
    if isinstance(value, int) and fmt in _INTEGER_FORMATS:
        low, high = _INTEGER_FORMATS[fmt]
        if not low <= value <= high:
            raise SchemaViolation(path, f"{value} does not fit {fmt} ({low} to {high})")


def _num(bound: object) -> str:
    if isinstance(bound, float) and bound.is_integer():
        return str(int(bound))
    return str(bound)


def _check_string(value: str, schema: dict, path: Location) -> None:
    if "minLength" in schema and len(value) < schema["minLength"]:
        raise SchemaViolation(
            path, f"must be at least {schema['minLength']} character(s), got {len(value)}"
        )
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        raise SchemaViolation(
            path, f"must be at most {schema['maxLength']} character(s), got {len(value)}"
        )
    if "pattern" in schema and not re.search(schema["pattern"], value):
        raise SchemaViolation(path, f"{value!r} does not match the pattern {schema['pattern']}")


def _check_table(value: dict, schema: dict, path: Location, root: dict) -> None:
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in value:
            raise SchemaViolation(path, f"missing required field {name!r}")
    for name, item in value.items():
        if name in properties:
            _check(item, properties[name], (*path, name), root, frozenset())
    if "additionalProperties" not in schema:
        return
    extra = schema["additionalProperties"]
    for name, item in value.items():
        if name in properties:
            continue
        if extra is False:
            raise SchemaViolation(path, f"unknown field {name!r}")
        _check(item, extra, (*path, name), root, frozenset())


def _check_branches(
    value: object,
    branches: list,
    path: Location,
    root: dict,
    refs: frozenset[str],
    *,
    exactly_one: bool,
) -> None:
    failures: list[SchemaViolation] = []
    matches = 0
    for branch in branches:
        try:
            _check(value, branch, path, root, refs)
        except SchemaViolation as exc:
            failures.append(exc)
        else:
            matches += 1
    if matches == 0:
        raise _combined(failures, path)
    if exactly_one and matches > 1:
        raise SchemaViolation(
            path, f"matches {matches} of the allowed shapes, where exactly one is allowed"
        )


def _combined(failures: list[SchemaViolation], path: Location) -> SchemaViolation:
    # A branch that failed deeper inside the value diagnosed a real shape and
    # points at the actual mistake; same-depth failures are the alternatives.
    deepest = max(failures, key=lambda exc: len(exc.path))
    if len(deepest.path) > len(path):
        return deepest
    reasons = "; ".join(dict.fromkeys(exc.message for exc in failures))
    return SchemaViolation(path, f"matches none of the allowed shapes ({reasons})")


def _check_not(
    value: object, excluded: object, path: Location, root: dict, refs: frozenset
) -> None:
    try:
        _check(value, excluded, path, root, refs)
    except SchemaViolation:
        return
    if isinstance(excluded, dict) and set(excluded) <= {"required"} and excluded.get("required"):
        fields = " and ".join(repr(name) for name in excluded["required"])
        raise SchemaViolation(path, f"fields {fields} cannot be set together")
    raise SchemaViolation(path, "matches a shape that is not allowed here")
