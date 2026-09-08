"""Normalize tool definitions into the harness contract."""

# Input types enforced by the grader.
CHECKED_TYPES = {"string", "date", "boolean", "object", "integer", "number", "array<string>"}

_SCALARS = {
    ("string", None): "string",
    ("string", "date"): "date",
    ("string", "date-time"): "datetime",
    ("integer", None): "integer",
    ("number", None): "number",
    ("boolean", None): "boolean",
    ("object", None): "object",
}

# JSON Schema keywords preserved by conversion.
_REPRESENTABLE = {"type", "description", "enum", "default", "properties", "required", "items", "examples", "format"}


class ContractError(Exception):
    """A tool definition uses a construct the canonical shape cannot represent."""


def _check_keywords(spec, path):
    unknown = sorted(set(spec) - _REPRESENTABLE)
    if unknown:
        raise ContractError(
            f"{path}: JSON Schema keyword(s) {unknown} cannot be represented in the canonical "
            f"shape. Converting would silently drop them; extend contract.py instead."
        )


def _legacy_type(spec, path, *, checked):
    """Map a JSON Schema type onto the legacy vocabulary."""
    jtype, fmt = spec.get("type"), spec.get("format")

    if jtype == "array":
        item_type = (spec.get("items") or {}).get("type")
        if item_type is None:
            raise ContractError(f"{path}: array without a declared items.type")
        legacy = f"array<{item_type}>"
    else:
        legacy = _SCALARS.get((jtype, fmt))
        if legacy is None:
            raise ContractError(
                f"{path}: cannot represent {{type: {jtype!r}, format: {fmt!r}}} in the canonical shape"
            )

    if checked and legacy not in CHECKED_TYPES:
        raise ContractError(
            f"{path}: input type {legacy!r} is not in grader._TYPE_CHECKS, so S3 would accept any "
            f"value for it without complaint. Add a check for it before using this type on an input."
        )
    return legacy


def _input(name, spec, required, path):
    _check_keywords(spec, path)
    out = {"name": name, "type": _legacy_type(spec, path, checked=True), "required": bool(required)}
    for key in ("description", "enum", "default"):
        if key in spec:
            out[key] = spec[key]
    if spec.get("examples"):
        out["example"] = spec["examples"][0]

    nested = spec.get("properties")
    if nested:
        # Convert object-level required fields to per-field flags.
        req = set(spec.get("required") or [])
        out["properties"] = {n: _input(n, s, n in req, f"{path}.{n}") for n, s in nested.items()}
    return out


def _output(name, spec, required, path):
    _check_keywords(spec, path)
    out = {"name": name, "type": _legacy_type(spec, path, checked=False), "required": bool(required)}
    if "description" in spec:
        out["description"] = spec["description"]
    items = spec.get("items")
    if isinstance(items, dict) and items.get("properties"):
        # Legacy items records only the declared field names.
        out["items"] = {
            n: (s.get("description") or s.get("type", "")) for n, s in items["properties"].items()
        }
    return out


def _outputs(schema, tool_name):
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    outs = [_output(n, s, n in required, f"{tool_name}.output.{n}") for n, s in props.items()]

    # Record mocks use the first array output as their wrapper.
    arrays = [o for o in outs if o["type"].startswith("array<")]
    if len(arrays) == 1:
        outs.remove(arrays[0])
        outs.insert(0, arrays[0])
    elif len(arrays) > 1:
        raise ContractError(
            f"{tool_name}: output_schema has {len(arrays)} array properties "
            f"({[o['name'] for o in arrays]}); the result wrapper is ambiguous."
        )
    return outs


def normalize(tool_def):
    """Return a canonical contract for extended or inline tool definitions."""
    if "parameters" not in tool_def:
        legacy = dict(tool_def)
        legacy.setdefault("description", "")
        legacy.setdefault("inputs", [])
        legacy.setdefault("outputs", [])
        legacy.setdefault("prerequisites", [])
        legacy.setdefault("requires_confirmation", False)
        return legacy

    name = tool_def["name"]
    params = tool_def.get("parameters") or {}
    _check_keywords(params, f"{name}.parameters")
    required = set(params.get("required") or [])

    return {
        "name": name,
        "description": tool_def.get("description", ""),
        "action_type": tool_def.get("action_type"),
        "requires_confirmation": bool(tool_def.get("requires_confirmation")),
        "inputs": [
            _input(n, s, n in required, f"{name}.parameters.{n}")
            for n, s in (params.get("properties") or {}).items()
        ],
        "outputs": _outputs(tool_def.get("output_schema") or {}, name),
        "prerequisites": [{"tool": p.get("tool")} for p in tool_def.get("call_before") or []],
    }
