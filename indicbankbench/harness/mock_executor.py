"""Mock tool executor — ARCHITECTURE.md.

Three mock `kind`s:
  - record_set_filtered (read tools): exact/enum match on listed filter args; an
    omitted optional filter returns all; a call arg that doesn't correspond to any
    record field is ignored for filtering. `offset`/`limit` are honored as real
    pagination (see execute) rather than treated as filters.
  - outcome_fixed (write tools): the mock specifies one fixed
    {status, message, reference_id, ...}; the executor echoes the call's own
    identifying argument(s) into the matching output field(s) (any input name that
    is also an output name), so authors don't repeat it in the mock.
  - outcome_merged (partial-update write tools): the reply is COMPUTED — the entity's
    current state is read from another mock, the call's own payload is deep-merged into
    it, and the result is returned. See _merge_state below for why a fixed reply is not
    good enough for these.

All three wrap the result under the tool's own output field name (from the contract's
single top-level array property / the mock's "output" dict), so the shape matches what the
tool's real schema promises.
"""
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
        # "all" is the catalog-wide sentinel meaning "no filter on this dimension" — never a
        # literal record value. Treat it as a no-op, not a value to match.
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
    """Overlay any run-scoped mutation onto a record being served by a read mock, so a read
    after a write does not contradict the write's own reply."""
    out = record
    for (src, match_on, value, field), current in (state or {}).items():
        if src == mock_name and record.get(match_on) == value:
            if out is record:
                out = copy.deepcopy(record)
            out[field] = copy.deepcopy(current)
    return out


def _merge_state(tool_name, arguments, mock, case, state):
    """Compute the post-update state for a partial-update write.

    WHY THIS KIND EXISTS. `outcome_fixed` returns the same payload whatever the model sent, so
    a mock could only ever assert the *intended* end state. Two failures follow. An over-broad
    write — disabling a channel the customer never mentioned — got a reply showing it had not
    happened, so a model reporting faithfully from that reply looked correct while the error was
    laundered. And a case that needs two calls in sequence could not express itself at all: with
    a fixed reply, the state after call one is indistinguishable from the state after call two.

    So the reply is derived instead: current state, plus exactly what this call carried. The
    tool contract's own promise — "only provided fields change; omitted fields keep their
    current values" — becomes something the mock enforces rather than something an author
    hand-maintains a copy of.

    State is threaded per `run_case`, never stored on the case dict: cases are shared across
    passes and worker threads, so mutating one would leak a card's state between runs.
    """
    cfg = mock.get("merge") or {}
    for required in ("state_from", "match_on", "field"):
        if required not in cfg:
            raise MockExecutionError(f"{tool_name}: outcome_merged mock is missing merge.{required}")

    key = _state_key(cfg, arguments)
    if state is not None and key in state:
        base = copy.deepcopy(state[key])          # a prior call in this same run already moved it
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

    # Only structured arguments carry the update; identifiers and scalars are not part of it.
    payload = {k: v for k, v in (arguments or {}).items() if isinstance(v, dict)}
    merged = _deep_update(base, payload)
    if state is not None:
        state[key] = copy.deepcopy(merged)
    return cfg["field"], merged


def _rejection(mock, arguments):
    """A backend enforcing its own documented constraint.

    Some contracts forbid a shape the schema still accepts — set_card_controls' "do not make
    changes to a channel's enable/disable status and daily limit in the same tool call" is one:
    both fields are valid individually, so S3 cannot catch the combination. Returning a fake
    success there would give the model no signal it erred, which is the same failure the schema
    validation above exists to prevent.

    `reject_when.payload_has_all_of` names the keys that must not co-occur inside a single
    sub-object of the payload, at any depth.
    """
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
    """Returns the JSON string to use as the `tool` message content.

    `state` is a per-run dict owned by runner.run_case, used only by outcome_merged so a
    second call in the same conversation sees the first one's effect. Omitting it keeps
    every existing mock kind byte-identical.
    """
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

    # Validate the call against the tool schema, like a real endpoint would (a 400 on bad
    # input). Reuses grader's S3 validator, so a call the mock rejects here is exactly one
    # S3 would fail — no divergence.
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
        # Honor offset/limit like a real paginated API: paging past the authored window
        # returns an EMPTY list, so a model that paginates sees the set end and stops.
        # A non-paginated call passes neither arg -> offset 0 / no limit -> returns all.
        page_args = arguments or {}
        offset = page_args.get("offset") or 0
        limit = page_args.get("limit")
        page = filtered[offset:] if limit is None else filtered[offset : offset + limit]
        if state:
            page = [_apply_state(tool_name, r, state) for r in page]
        output = {field_name: page}
        # Some read tools return scalar fields ALONGSIDE the record array — notably
        # get_transaction_history's `total_count` (total matching rows across all
        # pages, which can exceed the returned page). Authors supply these via
        # mock["static_output"]; e.g. a summarization/pagination case sets
        # {"total_count": 45} while returning only 10 records.
        output.update(mock.get("static_output", {}))
        # Convenience default: if the schema declares a `total_count` output and the
        # mock didn't set one, use the returned-record count (correct for the
        # non-paginated cases; the paginated case overrides via static_output).
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
