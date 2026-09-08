"""Execute deterministic case mocks for tool calls."""
import copy
import json


class MockExecutionError(Exception):
    pass


def _filter_records(records, arguments, raw_tool_def):
    if not records:
        return records
    sample_keys = set(records[0].keys())
    filtered = records
    for arg_key, arg_val in arguments.items():
        if arg_val is None:
            continue
        # "all" means no filter.
        if arg_val == "all":
            continue
        field = arg_key
        if field not in sample_keys and field.endswith("ids"):
            singular = field[:-1]
            if singular in sample_keys:
                field = singular
        if field not in sample_keys:
            continue  # arg doesn't map to a filterable record field — leave unfiltered on it
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
            raise MockExecutionError(
                f"{tool_name}: no {cfg['state_from']} record with {cfg['match_on']}={match_value!r} to merge into"
            )
        if cfg["field"] not in record:
            raise MockExecutionError(
                f"{tool_name}: {cfg['state_from']} record has no '{cfg['field']}' field to merge into"
            )
        base = copy.deepcopy(record[cfg["field"]])

    # Only structured arguments update state.
    payload = {k: v for k, v in (arguments or {}).items() if isinstance(v, dict)}
    merged = _deep_update(base, payload)
    if state is not None:
        state[key] = copy.deepcopy(merged)
    return cfg["field"], merged


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
        if raw_tool_def:
            input_names = {i["name"] for i in raw_tool_def.get("inputs", [])}
            output_names = {o["name"] for o in raw_tool_def.get("outputs", [])}
            for key in input_names & output_names:
                if key in (arguments or {}) and key not in output:
                    output[key] = arguments[key]
        return json.dumps(output)

    if kind == "outcome_merged":
        refused = _rejection(mock, arguments)
        if refused is not None:
            return json.dumps(refused)
        output = dict(mock.get("output", {}))
        field, merged = _merge_state(tool_name, arguments, mock, case, state)
        output[field] = merged
        if raw_tool_def:
            input_names = {i["name"] for i in raw_tool_def.get("inputs", [])}
            output_names = {o["name"] for o in raw_tool_def.get("outputs", [])}
            for key in input_names & output_names:
                if key in (arguments or {}) and key not in output:
                    output[key] = arguments[key]
        return json.dumps(output)

    raise MockExecutionError(f"Unknown mock kind '{kind}' for tool '{tool_name}'")
