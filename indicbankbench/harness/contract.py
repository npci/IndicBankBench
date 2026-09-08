"""Normalise a tool definition into the one canonical shape the rest of the harness reads.

`tool_definitions.json` uses `schema_version: 3.0-extended`, expressing inputs/outputs as JSON
Schema (`parameters`, `output_schema`) instead of the flat `inputs`/`outputs` lists the grader and
mock executor read. Both shapes are normalised here into the canonical (legacy) form so downstream
readers are unchanged, and inline axis-10 tools (still legacy-shaped) are an identity case.

Canonical form: `name`, `description`, `category`, `action_type`, `requires_confirmation`,
`inputs`, `outputs`, `prerequisites`, `notes`, `related_tools`.

FAIL LOUD, NEVER DROP: JSON Schema can express constraints the legacy shape cannot (`anyOf`,
`minimum`, `pattern`, `maxItems`). Silently discarding one would make S3 quietly stop enforcing
it, so anything unrepresentable raises `ContractError`. Inputs are type-checked by
`grader._TYPE_CHECKS`, so unknown input types raise; outputs are descriptive and wider types are
allowed through.
"""

# Legacy type vocabulary that grader._TYPE_CHECKS actually enforces. A type outside this set is
# not "unsupported" in general — it is unsupported *for an input*, where it would pass unchecked.
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

# JSON Schema keywords the legacy shape can carry. Anything else would be dropped by conversion.
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
        # THE REQUIRED BRIDGE. JSON Schema marks required fields object-level
        # (`required: [names]`); grader._validate_value reads a per-field boolean
        # (`sub_spec.get("required")`). Without this conversion every nested object would look
        # like it had no required fields at all, and S3 would silently stop enforcing them —
        # which is exactly what create_fd / create_rd's nominee traps rest on.
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
        # Legacy `items` is a flat {field: description} map. Only its KEYS are load-bearing
        # (they enumerate a record's declared fields); the values are author documentation.
        out["items"] = {
            n: (s.get("description") or s.get("type", "")) for n, s in items["properties"].items()
        }
    return out


def _outputs(schema, tool_name):
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    outs = [_output(n, s, n in required, f"{tool_name}.output.{n}") for n, s in props.items()]

    # mock_executor._output_field_name uses outputs[0].name as the result wrapper for
    # record_set_filtered mocks, so the array property must lead. Ordering here rather than
    # changing mock_executor keeps that reader untouched — and fixes a latent bug: under the old
    # file get_deposit_loan_rates listed `product_type` first, so its wrapper would have been
    # `product_type` rather than `rates`, saved only by it always being mocked outcome_fixed.
    arrays = [o for o in outs if o["type"].startswith("array<")]
    if len(arrays) == 1:
        outs.remove(arrays[0])
        outs.insert(0, arrays[0])
    elif len(arrays) > 1:
        raise ContractError(
            f"{tool_name}: output_schema has {len(arrays)} array properties "
            f"({[o['name'] for o in arrays]}); the result wrapper is ambiguous."
        )
    # Zero arrays is legitimate — a flat read tool such as get_gold_rate. Such a tool simply must
    # not be mocked record_set_filtered; test_mock_wrapper_resolves covers that.
    return outs


def normalize(tool_def):
    """Accept either the 3.0-extended contract shape or the legacy/inline shape; return canonical.

    Legacy definitions pass through with defaults filled in, so inline axis-10 tools authored in
    case files keep working unchanged.
    """
    if "parameters" not in tool_def:
        legacy = dict(tool_def)
        legacy.setdefault("description", "")
        legacy.setdefault("inputs", [])
        legacy.setdefault("outputs", [])
        legacy.setdefault("prerequisites", [])
        legacy.setdefault("notes", [])
        legacy.setdefault("related_tools", [])
        legacy.setdefault("requires_confirmation", False)
        return legacy

    name = tool_def["name"]
    params = tool_def.get("parameters") or {}
    _check_keywords(params, f"{name}.parameters")
    required = set(params.get("required") or [])

    return {
        "name": name,
        # The candidate never sees this text — it reads new_tool_definitions_v1.json. This is the
        # authoring-side summary, kept so to_openai_schema() has something on the fallback path.
        "description": tool_def.get("summary", ""),
        "category": tool_def.get("category"),
        "action_type": tool_def.get("action_type"),
        "requires_confirmation": bool(tool_def.get("requires_confirmation")),
        "inputs": [
            _input(n, s, n in required, f"{name}.parameters.{n}")
            for n, s in (params.get("properties") or {}).items()
        ],
        "outputs": _outputs(tool_def.get("output_schema") or {}, name),
        # `call_before` replaced `prerequisites`. Authoring input only — A4 gates on
        # gold.dependencies, never on this.
        "prerequisites": [
            {"tool": p.get("tool"), "reason": p.get("reason")} for p in tool_def.get("call_before") or []
        ],
        "notes": tool_def.get("notes") or [],
        "related_tools": tool_def.get("related_tools") or [],
    }
