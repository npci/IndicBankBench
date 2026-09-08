"""Load and resolve model-facing and harness-only tool definitions."""
import json
from pathlib import Path

from . import contract

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = Path(__file__).resolve().parents[1]
HARNESS_SPEC_PATH = BENCH_ROOT / "harness_tool_spec.json"
MODEL_SCHEMAS_PATH = BENCH_ROOT / "tools.json"


class ContractMismatch(Exception):
    """The harness-only spec and the model-facing schema file describe different tool sets."""


_TYPE_MAP = {
    "string": {"type": "string"},
    "date": {"type": "string", "format": "date"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "object": {"type": "object"},
    "array<string>": {"type": "array", "items": {"type": "string"}},
}


def _json_type(input_def):
    """Convert an input definition to JSON Schema."""
    schema = dict(_TYPE_MAP.get(input_def["type"], {"type": "string"}))
    if "enum" in input_def:
        schema["enum"] = input_def["enum"]
    if "description" in input_def:
        schema["description"] = input_def["description"]
    properties = input_def.get("properties")
    if properties:
        schema["properties"] = {name: _json_type(spec) for name, spec in properties.items()}
        nested_required = [name for name, spec in properties.items() if spec.get("required")]
        if nested_required:
            schema["required"] = nested_required
    return schema


def load_tool_definitions(path=None, model_schemas_path=None):
    """Load merged raw and canonical tool definitions."""
    path = Path(path) if path else HARNESS_SPEC_PATH
    with open(path) as f:
        data = json.load(f)
    model_schemas = load_model_schemas(model_schemas_path)

    # Reject a truncated tool specification.
    if len(data["tools"]) < 30:
        raise ContractMismatch(
            f"{path.name} declares only {len(data['tools'])} tools — expected ~32; refusing to "
            f"load a near-empty spec, which would make every test that loops over it pass "
            f"vacuously")

    spec_names = {t["name"] for t in data["tools"]}
    if spec_names != set(model_schemas):
        raise ContractMismatch(
            f"{path.name} and the model-facing schema file disagree on which tools exist — "
            f"only in {path.name}: {sorted(spec_names - set(model_schemas))}; "
            f"only in the model file: {sorted(set(model_schemas) - spec_names)}"
        )

    merged = []
    for spec in data["tools"]:
        function = model_schemas[spec["name"]]["function"]
        entry = dict(spec)
        entry["description"] = function.get("description", "")
        entry["parameters"] = function.get("parameters", {})
        merged.append(entry)

    raw = dict(data)
    raw["tools"] = merged
    by_name = {t["name"]: contract.normalize(t) for t in merged}
    return raw, by_name


def load_model_schemas(path=None):
    """Load model-facing schemas by tool name."""
    path = Path(path) if path else MODEL_SCHEMAS_PATH
    with open(path) as f:
        data = json.load(f)
    return {entry["function"]["name"]: entry for entry in data["tools"]}


def to_openai_schema(tool_def):
    """Convert a tool definition to an OpenAI function schema."""
    properties = {}
    required = []
    for inp in tool_def.get("inputs", []):
        properties[inp["name"]] = _json_type(inp)
        if inp.get("required"):
            required.append(inp["name"])
    return {
        "type": "function",
        "function": {
            "name": tool_def["name"],
            "description": tool_def.get("description", ""),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def resolve_tools_exposed(tools_exposed, tool_defs_by_name, model_schemas_by_name=None):
    """Resolve case tools to harness definitions and OpenAI schemas."""
    if model_schemas_by_name is None:
        model_schemas_by_name = load_model_schemas()
    raw_by_name = {}
    schemas = []
    for entry in tools_exposed:
        if isinstance(entry, str):
            if entry not in tool_defs_by_name:
                raise KeyError(
                    f"tools_exposed names '{entry}' but it is not in harness_tool_spec.json"
                )
            tool_def = tool_defs_by_name[entry]
            schema = model_schemas_by_name.get(entry) or to_openai_schema(tool_def)
        elif isinstance(entry, dict):
            tool_def = contract.normalize(entry)
            schema = to_openai_schema(tool_def)
        else:
            raise TypeError(f"tools_exposed entry must be str or dict, got {type(entry)}")
        raw_by_name[tool_def["name"]] = tool_def
        schemas.append(schema)
    return raw_by_name, schemas
