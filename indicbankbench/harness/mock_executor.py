"""Execute deterministic case mocks for tool calls."""
import copy
import json

from . import tools


class MockExecutionError(Exception):
    pass


class UnknownTarget(Exception):
    """A write named an entity that has no record in this case."""


def _record_field(arg_key, keys):
    """The record field an argument filters on, or None."""
    names = [arg_key, arg_key[:-1]] if arg_key.endswith("ids") else [arg_key]
    return next((name for name in names if name in keys), None)


def _date_part(value):
    return str(value)[:10]


def _range_bound(arg_key, raw_tool_def):
    """(field, "from" or "to") for a date-typed from_<field> or to_<field> input, else None."""
    spec = next((i for i in (raw_tool_def or {}).get("inputs", []) if i.get("name") == arg_key), None)
    prefix, _, field = arg_key.partition("_")
    if spec is None or spec.get("type") != "date" or prefix not in ("from", "to"):
        return None
    return field, prefix


def _filter_records(records, arguments, raw_tool_def):
    if not records:
        return records
    keys = set().union(*(r.keys() for r in records))
    filtered = records
    for arg_key, arg_val in arguments.items():
        if arg_val is None:
            continue
        # "all" means no filter.
        if arg_val == "all":
            continue
        # Date bounds are inclusive.
        bound = _range_bound(arg_key, raw_tool_def)
        if bound is not None:
            field = _record_field(bound[0], keys)
            if field is not None:
                limit = _date_part(arg_val)
                if bound[1] == "from":
                    filtered = [r for r in filtered if field not in r or _date_part(r[field]) >= limit]
                else:
                    filtered = [r for r in filtered if field not in r or _date_part(r[field]) <= limit]
                continue
        field = _record_field(arg_key, keys)
        if field is None:
            continue
        if isinstance(arg_val, list):
            filtered = [r for r in filtered if r.get(field) in arg_val]
        else:
            filtered = [r for r in filtered if r.get(field) == arg_val]
    return filtered


def _deep_update(base, updates):
    """Recursively overlay `updates` onto `base`. Scalars overwrite; dicts merge."""
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _state_key(cfg, arguments):
    return (cfg["state_from"], cfg["match_on"], (arguments or {}).get(cfg["match_on"]), cfg["field"])


def _apply_state(mock_name, record, state):
    """Apply run-scoped updates to a read result."""
    out = record
    for (src, match_on, value, field), current in (state or {}).items():
        if src == mock_name and record.get(match_on) == value:
            if out is record:
                out = copy.deepcopy(record)
            out[field] = copy.deepcopy(current)
    return out


def _merge_state(tool_name, arguments, mock, case, state):
    """Compute state after a partial-update write."""
    cfg = mock.get("merge") or {}
    for required in ("state_from", "match_on", "field"):
        if required not in cfg:
            raise MockExecutionError(f"{tool_name}: outcome_merged mock is missing merge.{required}")

    key = _state_key(cfg, arguments)
    if state is not None and key in state:
        base = copy.deepcopy(state[key])
    else:
        source = (case.get("mock") or {}).get(cfg["state_from"])
        if not source or "records" not in source:
            raise MockExecutionError(
                f"{tool_name}: merge.state_from='{cfg['state_from']}' is not a record_set_filtered mock in this case"
            )
        match_value = (arguments or {}).get(cfg["match_on"])
        record = next((r for r in source["records"] if r.get(cfg["match_on"]) == match_value), None)
        if record is None:
            raise UnknownTarget(f"No record found for {cfg['match_on']} {match_value!r} on this customer's profile.")
        if cfg["field"] not in record:
            raise MockExecutionError(
                f"{tool_name}: {cfg['state_from']} record has no '{cfg['field']}' field to merge into"
            )
        base = copy.deepcopy(record[cfg["field"]])

    if isinstance(base, dict):
        payload = {k: v for k, v in (arguments or {}).items() if isinstance(v, dict)}
        merged = _deep_update(base, payload)
    else:
        merged = base
    if state is not None:
        state[key] = copy.deepcopy(merged)
    return cfg["field"], merged


def _not_found(exc):
    return json.dumps({
        "error_code": "NOT_FOUND",
        "error_message": str(exc),
        "retriable": False,
    })


def _rejection(mock, arguments):
    """Return a configured response for an invalid argument combination."""
    cfg = mock.get("reject_when")
    keys = set((cfg or {}).get("payload_has_all_of") or ())
    if not keys:
        return None

    def offends(node):
        if not isinstance(node, dict):
            return False
        return keys.issubset(node) or any(offends(v) for v in node.values())

    return dict(cfg.get("output") or {}) if offends(arguments or {}) else None


def _output_field_name(raw_tool_def, fallback):
    outputs = (raw_tool_def or {}).get("outputs") or []
    if outputs:
        return outputs[0]["name"]
    return fallback


def _echo_identifying_inputs(output, arguments, raw_tool_def):
    """Copy arguments that are also declared outputs into the reply, if the mock omits them."""
    if not raw_tool_def:
        return
    output_names = {o["name"] for o in raw_tool_def.get("outputs", [])}
    for spec in raw_tool_def.get("inputs", []):
        key = spec["name"]
        if key in output_names and key in (arguments or {}) and key not in output:
            output[key] = arguments[key]


def _whole_floats_to_int(arguments, inputs):
    """Integer arguments sent as whole floats (5.0), as ints."""
    specs = {spec["name"]: spec for spec in inputs or []}
    out = {}
    for key, value in arguments.items():
        spec = specs.get(key) or {}
        if spec.get("type") == "integer" and isinstance(value, float) and value.is_integer():
            value = int(value)
        elif isinstance(value, dict) and spec.get("properties"):
            nested = [dict(sub, name=name) for name, sub in spec["properties"].items()]
            value = _whole_floats_to_int(value, nested)
        out[key] = value
    return out


def execute(tool_name, arguments, case, raw_tools_by_name, state=None):
    """Execute a mock tool call and return JSON output."""
    mock = case.get("mock", {}).get(tool_name)
    raw_tool_def = raw_tools_by_name.get(tool_name)

    if mock is None:
        return json.dumps(
            {
                "error_code": "UNEXPECTED_TOOL_CALL",
                "error_message": f"No mock configured for '{tool_name}' in this case.",
                "retriable": False,
            }
        )

    # Use the same schema validation as S3.
    if raw_tool_def is not None:
        from .grader import _validate_call_schema

        schema_errors = _validate_call_schema(tool_name, arguments or {}, raw_tool_def)
        if schema_errors:
            return json.dumps(
                {
                    "error_code": "SCHEMA_VALIDATION_ERROR",
                    "error_message": "; ".join(schema_errors),
                    "retriable": False,
                }
            )
        arguments = _whole_floats_to_int(arguments or {}, raw_tool_def.get("inputs"))
        arguments = tools.resolve_today(arguments, raw_tool_def, (case.get("setup") or {}).get("current_date"))

    kind = mock.get("kind")
    if kind == "record_set_filtered":
        filtered = _filter_records(mock.get("records", []), arguments or {}, raw_tool_def)
        field_name = _output_field_name(raw_tool_def, "records")
        page_args = arguments or {}
        offset = page_args.get("offset") or 0
        limit = page_args.get("limit")
        page = filtered[offset:] if limit is None else filtered[offset : offset + limit]
        if state:
            page = [_apply_state(tool_name, r, state) for r in page]
        output = {field_name: page}
        # Include configured scalar output alongside records.
        output.update(mock.get("static_output", {}))
        # Default total_count to the full filtered result set.
        if raw_tool_def and "total_count" not in output:
            if any(o.get("name") == "total_count" for o in raw_tool_def.get("outputs", [])):
                output["total_count"] = len(filtered)
        return json.dumps(output)

    if kind == "outcome_fixed":
        output = dict(mock.get("output", {}))
        _echo_identifying_inputs(output, arguments, raw_tool_def)
        return json.dumps(output)

    if kind == "outcome_merged":
        refused = _rejection(mock, arguments)
        if refused is not None:
            return json.dumps(refused)
        output = dict(mock.get("output", {}))
        try:
            field, merged = _merge_state(tool_name, arguments, mock, case, state)
        except UnknownTarget as exc:
            return _not_found(exc)
        # A single-value field must not overwrite a key the case wrote.
        if isinstance(merged, dict) or field not in output:
            output[field] = merged
        _echo_identifying_inputs(output, arguments, raw_tool_def)
        return json.dumps(output)

    raise MockExecutionError(f"Unknown mock kind '{kind}' for tool '{tool_name}'")
