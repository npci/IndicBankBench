"""Tests for the two-file tool contract — the invariants that fail *silently*.

The candidate is handed schemas from `new_tool_definitions_v1.json`, but S3 validates its
arguments against `tool_definitions.json`'s `inputs`, S2 reads `action_type` from there, and
mock_executor takes result-wrapper and echo field names from its `outputs`. Nothing at runtime
notices if the two files disagree: the model is simply graded against a schema it was never
shown, and every affected case fails for a reason that looks like a model error.

The same applies to `judge_advisory`. It carries both scorable `Q_*` metrics and the control
flag `behavior_advisory`; leaking the flag into the judge's metric list produced a real,
clean-looking, wrong number (an invented score for a non-metric, averaged into quality_score
across 13 cases) with nothing in the output hinting at it.

Run:  python -m unittest discover -s indicbankbench/tests
"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from indicbankbench.harness import judge, tools  # noqa: E402
from indicbankbench.harness.grader import _validate_call_schema  # noqa: E402

CASE_BANK = Path(__file__).resolve().parents[1] / "case_bank"


def _cases():
    """Every live case. Raises if it yields nothing.

    Ten tests loop over this and assert only *inside* the loop, so an empty iterator turns all ten
    green at once. That is not hypothetical: `MockOutputShape` reported a clean bank for 63 drifted
    records because an inner lookup silently found nothing. Guarding the generator protects every
    dependent test with one assertion, and cannot drift out of sync the way per-test guards do.
    """
    found = 0
    for path in sorted(CASE_BANK.rglob("*.json")):
        if "_retired" in path.parts:
            continue
        found += 1
        yield path, json.loads(path.read_text())
    if not found:
        raise AssertionError(
            f"_cases() yielded nothing from {CASE_BANK} — every test that loops over it would "
            f"pass vacuously")


class SchemaParity(unittest.TestCase):
    def test_both_files_agree_on_every_tool(self):
        problems = tools.assert_schema_parity()
        self.assertEqual(
            problems,
            [],
            "tool_definitions.json and new_tool_definitions_v1.json disagree — S3 would grade "
            "against a schema the candidate was never shown:\n  " + "\n  ".join(problems),
        )

    def test_every_exposed_tool_reaches_the_model_with_its_full_description(self):
        """A named tool missing from the model-facing file silently falls back to a schema
        built from the terse authoring description — so the candidate loses the disclosed
        prerequisites that cases are allowed to gate on."""
        model_schemas = tools.load_model_schemas()
        missing = set()
        for _path, case in _cases():
            for entry in case["tools_exposed"]:
                # dicts are inline fabricated axis-10 tools; they have no counterpart by design
                if isinstance(entry, str) and entry not in model_schemas:
                    missing.add(entry)
        self.assertEqual(set(), missing)


class Disclosure(unittest.TestCase):
    """Everything a case is allowed to gate on must reach the candidate.

    Gating rests on the doctrine "never gate on something the model was not told", and since the
    two-file split that means: a `call_before` prerequisite or a `notes` rule may be required of a
    model ONLY because it appears in the model-facing description. `assert_schema_parity()` cannot
    see this — it compares `parameters` only — so the two files could drift on descriptions, which
    is exactly where all the disclosure now lives, with nothing catching it.

    Note this deliberately does NOT compare descriptions verbatim. The contract's `summary` is
    terse and the model file composes summary + category + action type + prerequisites + notes, so
    the two SHOULD differ. The invariant is containment: whatever the contract declares as a
    prerequisite or a note must be findable in what the model reads.
    """

    def test_every_declared_prerequisite_is_disclosed(self):
        raw, _ = tools.load_tool_definitions()
        model = tools.load_model_schemas()
        undisclosed = []
        for tool in raw["tools"]:
            description = model[tool["name"]]["function"]["description"]
            for pre in tool.get("call_before") or []:
                if pre.get("tool") and pre["tool"] not in description:
                    undisclosed.append(f"{tool['name']} requires {pre['tool']}, unmentioned in its description")
        self.assertEqual([], undisclosed, "\n  " + "\n  ".join(undisclosed))

    @staticmethod
    def _squash(text):
        """Alphanumerics only, lowercased.

        The composer reformats freely — it expands a `;`-packed contract note into `--`
        sub-bullets, re-wraps lines and re-punctuates. Comparing raw text produces false
        positives on all of that. Squashing to alphanumerics keeps the check meaningful (the
        characters must still appear in order) while surviving every reformatting seen so far.
        """
        return re.sub(r"[^a-z0-9]", "", text.lower())

    def test_every_note_is_disclosed(self):
        raw, _ = tools.load_tool_definitions()
        model = tools.load_model_schemas()
        missing = []
        for tool in raw["tools"]:
            disclosed = self._squash(model[tool["name"]]["function"]["description"])
            for note in tool.get("notes") or []:
                # `;` packs several sub-rules into one note; the composer splits them onto their
                # own lines, so check each fragment rather than the note as a whole.
                for fragment in note.split(";"):
                    # Fragments under 3 words are dropped as punctuation debris from the split.
                    # It WOULD silently drop a short rule such as "no default" if one were added.
                    if len(fragment.split()) < 3:
                        continue
                    if self._squash(fragment) not in disclosed:
                        missing.append(f"{tool['name']}: note fragment not disclosed — {fragment.strip()[:70]!r}")
        self.assertEqual([], missing, "\n  " + "\n  ".join(missing))


class ContractShape(unittest.TestCase):
    """The contract file must present, through the harness, the same parameter set the
    candidate is shown. This is the test that would have caught the 3.0-extended shape
    change on the day it landed: the readers in grader.py / mock_executor.py consume
    canonical `inputs`, and a contract that stops supplying them fails silently — S3
    starts rejecting every argument as 'not in schema' while looking like a model error."""

    def test_every_tool_yields_the_parameters_the_model_is_shown(self):
        _, defs = tools.load_tool_definitions()
        model = tools.load_model_schemas()
        broken = []
        for name, raw in sorted(defs.items()):
            canonical = {i["name"] for i in raw.get("inputs", [])}
            shown = set(model[name]["function"].get("parameters", {}).get("properties", {}))
            if canonical != shown:
                broken.append(f"{name}: harness sees {sorted(canonical)}, model is shown {sorted(shown)}")
        self.assertEqual([], broken, "\n  " + "\n  ".join(broken))


class GoldCallRoundTrip(unittest.TestCase):
    """Every reference call in every case must pass the same S3 validator the candidate's
    calls are graded by. Gold is schema-clean by construction, so a failure here never
    means the case is wrong — it means the contract and the validator have diverged."""

    def test_every_gold_call_passes_s3(self):
        _, defs = tools.load_tool_definitions()
        model = tools.load_model_schemas()
        failures = []
        for path, case in _cases():
            try:
                raw, _ = tools.resolve_tools_exposed(case["tools_exposed"], defs, model)
            except KeyError:
                continue  # unresolvable tools are CaseBankPreflight's job, not this test's
            for call in case["gold"].get("tool_calls", []):
                errs = _validate_call_schema(call["tool"], call.get("arguments", {}), raw.get(call["tool"]))
                if errs:
                    failures.append(f"{path.relative_to(CASE_BANK)}: {'; '.join(errs)}")
        self.assertEqual(
            [], failures,
            f"{len(failures)} gold calls rejected by S3:\n  " + "\n  ".join(failures[:15]),
        )


class CaseBankPreflight(unittest.TestCase):
    """resolve_tools_exposed raises on an unknown tool name, which is correct — but it dies
    on the first offender, so a contract change that drops a tool surfaces one case at a
    time. This names every offender at once."""

    def test_every_exposed_tool_name_resolves(self):
        _, defs = tools.load_tool_definitions()
        offenders = {}
        for path, case in _cases():
            for entry in case["tools_exposed"]:
                if isinstance(entry, str) and entry not in defs:
                    offenders.setdefault(entry, []).append(str(path.relative_to(CASE_BANK)))
        summary = [f"{tool} — {len(files)} cases (e.g. {files[0]})" for tool, files in sorted(offenders.items())]
        self.assertEqual([], summary, "unresolvable tool names:\n  " + "\n  ".join(summary))


class MockWrapperResolution(unittest.TestCase):
    """A record_set_filtered mock is wrapped under `outputs[0].name`. That is only meaningful if
    the tool actually returns an array, so the check is scoped to tools the bank really mocks as
    record sets — not to every read tool. Two read tools (get_gold_rate, get_request_status)
    return a flat object and are legitimately array-less; the rule is about how they are MOCKED,
    not what they are."""

    def test_every_record_set_mock_wraps_under_a_declared_array(self):
        _, defs = tools.load_tool_definitions()
        offenders = {}
        for path, case in _cases():
            for name, mock in (case.get("mock") or {}).items():
                # `name not in defs` skips a mock for an INLINE axis-10 tool, which declares its
                # own outputs and has no contract entry.
                if mock.get("kind") != "record_set_filtered" or name not in defs:
                    continue
                arrays = [o["name"] for o in defs[name].get("outputs", []) if o["type"].startswith("array<")]
                if len(arrays) != 1:
                    offenders.setdefault(name, (arrays, str(path.relative_to(CASE_BANK))))
        summary = [
            f"{name} mocked as a record set but its output_schema has {len(arrays)} array "
            f"properties {arrays} — records would be wrapped under a scalar field "
            f"(e.g. {where})"
            for name, (arrays, where) in sorted(offenders.items())
        ]
        self.assertEqual([], summary, "\n  " + "\n  ".join(summary))


class MockHygiene(unittest.TestCase):
    """An `_comment` at the MOCK level is author documentation and is never serialised. An
    `_comment` INSIDE a record, a `static_output`, or an `outcome_fixed` `output` is handed
    straight to the candidate: `_filter_records` returns record dicts verbatim and `execute()`
    json.dumps them, so every key in them reaches the model.

    This is not hypothetical. Seven cards cases currently ship the case's own answer in the tool
    response — "Diagnosis: pos.international is DISABLED … (the decoy: flipping it fixes
    nothing)", "the trap: reporting ₹2,00,000 as 'available' would be wrong" — concentrated in
    the hardest axes. It is also why the cards domain's scores are not comparable to any other
    domain's until they are fixed and re-run.

    The scan recurses: cards records nest a `controls` object and deposits records nest
    `nominee`, so a note buried one level down would leak just as completely.
    """

    @staticmethod
    def _leaked_keys(node, path):
        found = []
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.startswith("_"):
                    found.append(f"{path}.{key}")
                found.extend(MockHygiene._leaked_keys(value, f"{path}.{key}"))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                found.extend(MockHygiene._leaked_keys(item, f"{path}[{i}]"))
        return found

    def test_no_author_notes_are_serialised_to_the_model(self):
        leaks = []
        for path, case in _cases():
            for tool, mock in (case.get("mock") or {}).items():
                # Deliberately NOT scanning `mock` itself — a sibling of records/output is the
                # correct, invisible placement. Only what execute() serialises is in scope.
                for surface in ("records", "output", "static_output"):
                    if surface in mock:
                        for hit in self._leaked_keys(mock[surface], f"mock.{tool}.{surface}"):
                            leaks.append(f"{path.relative_to(CASE_BANK)} :: {hit}")
        self.assertEqual(
            [], leaks,
            f"{len(leaks)} author note(s) reach the candidate inside a tool response:\n  "
            + "\n  ".join(leaks),
        )


class AdvisoryMetricFiltering(unittest.TestCase):
    def _case(self, judge_advisory):
        return {
            "case_id": "t.1",
            "axis": "7_wrong_info",
            "target_behavior": "clarify",
            "gold": {"expected_resolution": "rubric"},
            "grading": {
                "judge_gate": {"behavior_class": "clarify", "axis_gate_rule": "bar"},
                "judge_advisory": judge_advisory,
            },
        }

    def test_control_flag_is_not_sent_to_the_judge_as_a_metric(self):
        case = self._case(["behavior_advisory", "Q_refusal", "Q_tone"])
        messages = judge.build_judge_messages(case, [{"role": "system", "content": "s"}])
        rendered = messages[1]["content"]
        line = next(l for l in rendered.splitlines() if "ADVISORY METRICS" in l)
        self.assertIn("Q_refusal", line)
        self.assertIn("Q_tone", line)
        self.assertNotIn("behavior_advisory", line)

    def test_heuristic_judge_does_not_score_the_control_flag(self):
        case = self._case(["behavior_advisory", "Q_refusal"])
        result = judge.heuristic_judge(case, [{"role": "assistant", "content": "no such FD?"}])
        self.assertEqual({"Q_refusal": 1.0}, result["sub_scores"])

    def test_the_flag_still_reaches_the_grader(self):
        """Filtering is for the judge prompt only — grader.py:425 must still see the flag in
        judge_advisory, or the behaviour gate it disables would silently switch back on."""
        case = self._case(["behavior_advisory", "Q_refusal"])
        self.assertIn("behavior_advisory", case["grading"]["judge_advisory"])


class TransactionRecordShape(unittest.TestCase):
    """`get_transaction_history` fixtures against CONVENTIONS.md §13.

    The drift this pins cost a 16-case migration across two domains. It was invisible because
    a wrong-but-plausible record still renders fine in a transcript: `description` instead of
    `transaction_note` reads the same to a human, and a missing `running_balance` just looks
    like a terse statement. Only a scan catches it, so the scan lives here now.
    """

    FIELDS = ["date", "amount", "type", "channel", "reference_id", "transaction_note",
              "status", "running_balance"]
    CHANNELS = {"upi", "neft", "imps", "rtgs", "atm", "pos", "cheque", "internal"}
    STATUSES = {"success", "pending", "failed", "reversed"}

    def _fixtures(self):
        """Raises if it yields nothing — the fourth generator, feeding four tests here.

        `_cases()`' own guard only catches an empty case bank. It does not catch a FULL bank in
        which nothing yields transaction records: rename the `get_transaction_history` mock key and
        this quietly produces nothing while three of the four tests below, which have no count
        guard of their own, go green.
        """
        found = 0
        for path, case in _cases():
            mock = (case.get("mock") or {}).get("get_transaction_history")
            if mock and mock.get("records"):
                found += 1
                yield path.relative_to(CASE_BANK), mock["records"]
        if not found:
            raise AssertionError(
                "_fixtures() yielded nothing — three TransactionRecordShape tests would pass "
                "vacuously")

    def test_every_record_carries_exactly_the_declared_fields(self):
        for rel, records in self._fixtures():
            for i, record in enumerate(records):
                with self.subTest(case=str(rel), row=i):
                    self.assertEqual(self.FIELDS, list(record),
                                     "field set and order must match §13 exactly")

    def test_channel_and_status_stay_inside_the_contract_enums(self):
        """`all` is the filter sentinel and must never appear as a record value — `mock_executor`
        reads it as 'no filter', so a record carrying it would match every query."""
        for rel, records in self._fixtures():
            for i, record in enumerate(records):
                with self.subTest(case=str(rel), row=i):
                    self.assertIn(record["channel"], self.CHANNELS)
                    self.assertIn(record["status"], self.STATUSES)

    def test_dates_are_newest_first_and_carry_a_time(self):
        for rel, records in self._fixtures():
            dates = [r["date"] for r in records]
            with self.subTest(case=str(rel)):
                for date in dates:
                    self.assertRegex(date, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
                self.assertEqual(sorted(dates, reverse=True), dates,
                                 "the contract returns newest first, and running_balance "
                                 "cannot chain in any other order")

    def test_running_balance_chains_and_never_goes_negative(self):
        """The part a careless migration gets wrong while still looking right: each older row's
        balance is the newer row's balance with the newer transaction undone. A row with a
        deliberately null amount (axis-2 fixtures) breaks the chain by design, so stop there."""
        for rel, records in self._fixtures():
            with self.subTest(case=str(rel)):
                for newer, older in zip(records, records[1:]):
                    if newer["amount"] is None:
                        break
                    undone = (newer["running_balance"] + newer["amount"]
                              if newer["type"] == "debit" else
                              newer["running_balance"] - newer["amount"])
                    self.assertEqual(undone, older["running_balance"],
                                     f"chain breaks after {newer['date']}")
                oldest = records[-1]
                if oldest["amount"] is not None:
                    opening = (oldest["running_balance"] + oldest["amount"]
                               if oldest["type"] == "debit" else
                               oldest["running_balance"] - oldest["amount"])
                    self.assertGreaterEqual(opening, 0, "opening balance would be negative")


class MockOutputShape(unittest.TestCase):
    """Every mock against its tool's declared `output_schema`.

    `TransactionRecordShape` pins one tool exhaustively; this pins every tool loosely — no mock may
    emit a field the contract does not declare. That is the direction that actually misleads: a
    model shown `description` where the contract promised `transaction_note` will use what it sees,
    and the case then grades a fiction. Undeclared fields are checked rather than missing ones
    because several tools legitimately omit optional outputs.
    """

    def _mocks(self):
        """Raises if it yields nothing — see the note on `_cases()`."""
        _raw, canonical = tools.load_tool_definitions()
        found = 0
        for path, case in _cases():
            inline = {t["name"] for t in case.get("tools_exposed") or [] if isinstance(t, dict)}
            for name, mock in (case.get("mock") or {}).items():
                if name in inline:          # fabricated tools declare their own outputs
                    continue
                self.assertIn(name, canonical,
                              f"{path.name} mocks '{name}', which is not a real tool")
                found += 1
                yield path.relative_to(CASE_BANK), name, mock, canonical[name]
        if not found:
            raise AssertionError("_mocks() yielded nothing — both shape tests would pass vacuously")

    @staticmethod
    def _wrapper(tool):
        outs = {o["name"]: o for o in tool.get("outputs") or []}
        arrays = [n for n, o in outs.items() if str(o.get("type", "")).startswith("array")]
        return outs, (arrays[0] if arrays else None)

    def test_no_record_emits_an_undeclared_field(self):
        """The first version of this test looked for the item fields under `item_fields` or
        `properties`, found neither — `contract.normalize()` puts them under `items` — and
        `continue`d. It reported a clean bank while 63 records across 16 cases emitted fields the
        contract never declared, including a whole family of `get_products_and_offers` safety nets
        on name/summary instead of title/description. Hence the assert below: a shape check that
        cannot find the shape must fail, not pass quietly.
        """
        checked = 0
        for rel, name, mock, tool in self._mocks():
            if mock.get("records") is None:
                continue
            outs, wrapper = self._wrapper(tool)
            self.assertIsNotNone(
                wrapper, f"{name} is mocked as a record set but declares no array output")
            item = outs[wrapper].get("items")
            self.assertIsInstance(
                item, dict,
                f"cannot resolve declared item fields for {name}.{wrapper} — the check would "
                f"otherwise skip silently, which is how this drift survived")
            for i, record in enumerate(mock["records"]):
                extra = [k for k in record if k not in item and not k.startswith("_")]
                with self.subTest(case=str(rel), tool=name, row=i):
                    self.assertEqual([], extra, f"{name} does not declare {extra}")
                checked += 1
        self.assertGreater(checked, 100, "far fewer records checked than the bank contains")

    def test_no_outcome_emits_an_undeclared_field(self):
        for rel, name, mock, tool in self._mocks():
            if mock.get("output") is None:
                continue
            outs, _ = self._wrapper(tool)
            extra = [k for k in mock["output"] if k not in outs and not k.startswith("_")]
            with self.subTest(case=str(rel), tool=name):
                self.assertEqual([], extra, f"{name} does not declare {extra}")


class InvariantScope(unittest.TestCase):
    """S2 is active exactly where a write tool is exposed (CONVENTIONS.md §7a)."""

    def test_s2_tracks_write_exposure(self):
        raw, _ = tools.load_tool_definitions()
        writes = {t["name"] for t in raw["tools"] if t.get("action_type") == "write"}
        for path, case in _cases():
            exposed = {t["name"] if isinstance(t, dict) else t
                       for t in case.get("tools_exposed") or []}
            with self.subTest(case=str(path.relative_to(CASE_BANK))):
                self.assertEqual(
                    bool(exposed & writes),
                    "S2" in case["grading"]["invariants_active"],
                    "S2 must be active iff the case exposes a write tool — on everywhere else it "
                    "overstates coverage, off where a write exists it disables a real gate")


class InlineToolDisclosure(unittest.TestCase):
    """Axis-10 tools authored inside a case, against CONVENTIONS.md §9.

    `to_openai_schema()` emits `name`, `description` and `parameters` and nothing else, so an
    inline tool's `action_type` and `requires_confirmation` never reach the candidate unless the
    author folds them into the description. That is not cosmetic. `book_branch_appointment` was
    `action_type: write` with `requires_confirmation: True` in a case with S2 active, while its
    description said neither — so S2 could fail a model for skipping a confirmation it was never
    told to obtain. Gating on an undisclosed rule is the one thing the contract split exists to
    prevent.
    """

    def _inline_tools(self):
        """Raises if it yields nothing — the sharpest instance of the problem in `_cases()`.

        All four inline-tool tests are fed from here. If a change to how `tools_exposed` carries a
        fabricated tool stopped it matching `isinstance(entry, dict)`, every one of them would go
        green while nothing was checked at all.
        """
        found = 0
        for path, case in _cases():
            for entry in case.get("tools_exposed") or []:
                if isinstance(entry, dict):
                    found += 1
                    yield path.relative_to(CASE_BANK), case, entry
        if not found:
            raise AssertionError(
                "_inline_tools() yielded nothing — all four inline-tool tests would pass vacuously")

    def test_every_inline_tool_declares_category_and_action_type(self):
        for rel, _case, tool in self._inline_tools():
            with self.subTest(case=str(rel), tool=tool["name"]):
                self.assertIn("Category:", tool["description"])
                self.assertIn("Action type:", tool["description"],
                              "an inline tool formatted unlike the real ones is a tell")

    def test_a_confirmation_requirement_is_stated_in_the_description(self):
        for rel, _case, tool in self._inline_tools():
            if not tool.get("requires_confirmation"):
                continue
            with self.subTest(case=str(rel), tool=tool["name"]):
                self.assertIn("REQUIRES explicit customer confirmation", tool["description"],
                              "S2 reads requires_confirmation, but the model only ever sees "
                              "the description — so the rule has to be in there")

    def test_inline_tools_keep_the_keys_the_harness_reads(self):
        for rel, _case, tool in self._inline_tools():
            with self.subTest(case=str(rel), tool=tool["name"]):
                for key in ("name", "description", "action_type", "requires_confirmation",
                            "inputs", "outputs"):
                    self.assertIn(key, tool)

    def test_an_inline_tool_name_never_shadows_a_real_one(self):
        """A fabricated tool sharing a real tool's name would resolve against the contract
        instead, silently turning an axis-10 case into an ordinary one."""
        _, real = tools.load_tool_definitions()
        for rel, _case, tool in self._inline_tools():
            with self.subTest(case=str(rel), tool=tool["name"]):
                self.assertNotIn(tool["name"], real)


class DisclosedPrerequisiteIsReachable(unittest.TestCase):
    """A prerequisite the contract tells the model to call must not be a trap.

    Read `prerequisites` (not `call_before`) here: `contract.normalize()` renames the field.
    """

    # Cases where a declared prerequisite is genuinely unreachable and that is correct: the
    # customer's request has no transaction to locate, so `get_transaction_history` could not be
    # called even in principle. Keyed on that fact, not on the case id.
    NO_TRANSACTION_TO_LOCATE = {
        "csc.get_insurance_details.confusing_intent.001",   # death claim on a policy
        "csc.raise_request.confusing_intent.001",           # email-address update
        "csc.search_knowledge_base.bad_response.001",       # a policy question; raise_request is a fallback
    }

    def test_every_disclosed_prerequisite_is_reachable(self):
        _, defs = tools.load_tool_definitions()
        offenders, checked = [], 0
        for path, case in _cases():
            exposed = {e if isinstance(e, str) else e["name"] for e in case["tools_exposed"]}
            gold = case["gold"]
            gates = {k: v for k, v in (gold.get("arg_gate") or {}).items()
                     if not k.startswith("_") and isinstance(v, dict)}
            allowed = ({t["tool"] for t in gold.get("tool_calls") or []}
                       | {t for t, v in gates.items() if v.get("optional")})
            mocked = set(case.get("mock") or {})
            for tool in allowed:
                prereqs = [p["tool"] for p in (defs.get(tool) or {}).get("prerequisites") or []]
                for pre in prereqs:
                    if pre not in exposed or pre in allowed:
                        continue
                    checked += 1
                    # A tool may declare several prerequisites that are ALTERNATIVES rather than
                    # all-required — get_deposit_closure_quote names both get_fd_details and
                    # get_rd_details. An FD case correctly allows the first and forbids the second.
                    if any(alt in allowed for alt in prereqs if alt != pre):
                        continue
                    # Mocked-but-forbidden is survivable: the model gets a real response instead of
                    # an error loop, and fails cleanly on A2. It is NOT free — following a disclosed
                    # prerequisite still costs A2 — but the two current instances were reviewed and
                    # accepted rather than being an oversight.
                    if pre in mocked:
                        continue
                    if case["case_id"] in self.NO_TRANSACTION_TO_LOCATE:
                        continue
                    offenders.append(
                        f"{path.relative_to(CASE_BANK)}: {tool} declares {pre} as a prerequisite, "
                        f"but it is exposed, not allowed and not mocked — a model following the "
                        f"disclosed instruction gets UNEXPECTED_TOOL_CALL")
        self.assertGreater(checked, 3, "the prerequisite scan examined almost nothing")
        self.assertEqual([], offenders, "\n  " + "\n  ".join(offenders))


class EnumValueIsObtainable(unittest.TestCase):
    """Every enum value a gold call passes must be gettable from the conversation.

    An enum field the customer has not named invites the model to ask; a scripted turn cannot
    answer an unanticipated question, so the write never happens and A1 fails on a case the model
    reasoned through correctly. The risk is NOT confined to optional fields
    — a REQUIRED enum the customer has not named is the stronger hazard, because the model is
    obliged to obtain a value and cannot proceed without one.

    This test cannot decide whether a value is *inferable*; only whether it was stated. Its job is
    to force that judgement to be explicit, so a NEW case that neither states the value nor
    declares itself an inference test fails loudly rather than stalling in a future run.
    """

    # Cases where the value is deliberately inferred rather than stated. Each is a real test of
    # that inference, not an oversight.
    INFERENCE_BY_DESIGN = {
        "cards.block_card.confusing_intent.001": "'chip won't read' -> damaged; the case IS the inference test",
        "cards.toggle_card_freeze.happy.001": "'make sure nobody can use it' + recoverable -> freeze, not block",
        "acct.stop_cheque_payment.confusing_intent.001": "'the deal has fallen through' -> dispute",
        "csc.agentic.dispute_unauthorized.001": "'a debit I did not make' -> unauthorized_debit",
        "csc.raise_request.not_enough_info.001": "'I never made that purchase' -> unauthorized_debit; "
                                                 "same inference as csc.agentic.dispute_unauthorized.001, and "
                                                 "scripting the customer to say 'unauthorized debit' would put "
                                                 "the enum token in their mouth to satisfy the matcher",
        "csc.get_insurance_details.confusing_intent.001": "no claim tool exists -> category 'other'",
        "csc.raise_request.happy.001": "'unfair non-maintenance fee' -> charges_dispute",
        "depl.create_fd.confusing_intent.001": "'interest paid out monthly to live on' -> payout_type monthly",
        "depl.update_deposit_renewal.happy.001": "the enum choice IS the test; stating it would remove the case",
        "acct.get_transaction_history.confusing_intent.001": "'did my salary come through?' -> type credit",
        "calc.calculate_fd_maturity.happy.001": "'fixed deposit' -> product_type fd; the enum uses the abbreviation",
        "calc.calculate_rd_maturity.confusing_intent.001": "'save Rs 5,000 every month' -> rd; recognising it IS the test",
        "calc.calculate_rd_maturity.happy.001": "'recurring deposit' -> product_type rd; the enum uses the abbreviation",
        "calc.get_deposit_loan_rates.happy.001": "'fixed deposit' -> product_type fd; the enum uses the abbreviation",
        "depl.create_fd.context_switching.001": "'recurring deposit rate' -> rd; the enum uses the abbreviation",
        "depl.get_gold_rate.confusing_intent.001": "'pledging jewellery for a loan' -> purity 22K; the inference IS the test",
        "calc.get_deposit_loan_rates.confusing_intent.001": "'housing loan' -> home_loan; mapping the synonym IS the test",
        "csc.get_all_requests.context_switching.001": "'FD products' -> deposit; inferring the product type IS the test",
        "depl.agentic.book_fd.002": "customer says 'fixed deposit' -> fd; inferring the product type IS the test",
        "depl.agentic.book_rd.001": "customer says 'recurring deposit' -> rd; inferring the product type IS the test",
        "csc.get_products_and_offers.irrelevant_rag.002": "'FD offers' -> deposit; inferring product type from context",
        "depl.create_fd.not_enough_info.004": "customer says 'fixed deposit' -> fd; standard abbreviation IS the inference",
    }

    @staticmethod
    def _stated(value, text):
        """Did the customer say this enum value?

        Two normalisations, both to stop the check reporting authoring problems that are really
        matcher artefacts. Enum tokens are snake_case while people write words, so `personal_loan`
        has to match "personal loan"; and a short suffix is allowed so "emailed to me" states
        `email`. Without the first, seven correct cases looked like defects.
        """
        phrase = re.escape(str(value)).replace("_", r"[\s_-]")
        return re.search(rf"\b{phrase}(?:[a-z]{{0,3}})?\b", text, re.I) is not None

    def test_every_gold_enum_value_is_stated_or_declared_an_inference(self):
        raw, _ = tools.load_tool_definitions()
        enums = {t["name"]: {k: v["enum"] for k, v in (t["parameters"].get("properties") or {}).items()
                             if "enum" in v}
                 for t in raw["tools"]}
        offenders, checked = [], 0
        for path, case in _cases():
            said = " ".join(t["content"] for t in case["user_turns"])
            said += " " + " ".join((m.get("content") or "")
                                   for m in (case["setup"].get("prior_messages") or []))
            for call in case["gold"].get("tool_calls") or []:
                for field in enums.get(call["tool"], {}):
                    value = (call.get("arguments") or {}).get(field)
                    if value is None:
                        continue
                    checked += 1
                    if self._stated(value, said):
                        continue
                    if case["case_id"] in self.INFERENCE_BY_DESIGN:
                        continue
                    offenders.append(
                        f"{path.relative_to(CASE_BANK)}: {call['tool']}.{field}={value!r} is never "
                        f"said by the customer and the case is not declared an inference test")
        self.assertGreater(checked, 20, "the enum scan examined almost nothing")
        self.assertEqual([], offenders, "\n  " + "\n  ".join(offenders))


class NotesReachTheModel(unittest.TestCase):
    """Every `notes` entry in the contract must appear in the model-facing description.

    `assert_schema_parity()` diffs `parameters` ONLY — descriptions are hand-maintained and
    deliberately excluded from it, because they are the point of the model-facing file rather
    than a derived copy. That leaves exactly one gap, and it is the dangerous one: a note added
    to `tool_definitions.json` but never folded into `new_tool_definitions_v1.json` is a rule the
    model is graded against and was never shown. Cases legitimately gate on notes now that
    prerequisites are disclosed, so the gap is live rather than theoretical.

    The fold is hand-formatted — a note carrying semicolons is rendered as `--` sub-bullets across
    several lines — so this compares on alphanumerics only rather than as a literal substring.
    """

    @staticmethod
    def _norm(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())

    def test_every_contract_note_is_disclosed_to_the_candidate(self):
        raw, _ = tools.load_tool_definitions()
        model = tools.load_model_schemas()
        missing, checked = [], 0
        for t in raw["tools"]:
            entry = model.get(t["name"]) or {}
            desc = self._norm((entry.get("function") or entry).get("description", ""))
            for note in t.get("notes") or []:
                checked += 1
                if self._norm(note) not in desc:
                    missing.append(f"{t['name']}: {note[:80]}")
        self.assertGreater(checked, 20, "the notes scan examined almost nothing")
        self.assertEqual([], missing, "\n  " + "\n  ".join(missing))


class AdvisoryMetricIdsAreKnown(unittest.TestCase):
    """Every `Q_*` id in `judge_advisory` must be one CODES.md actually defines.

    `judge._advisory_metric_ids()` filters on `startswith("Q_")` and nothing else, so a typo is
    passed straight to the judge as a metric name to score. There is no catalogue of metric
    definitions anywhere in the harness — the judge infers meaning from the id alone — so a
    misspelled id is scored blind rather than rejected.

    Seven cases carried `Q_grounding` for `Q_grounded`, which no check caught.
    The harm was smaller than it first appeared — `quality_score` averages `sub_scores.values()`,
    so it is name-agnostic — but the ids are a contract, and a phantom id would split any future
    per-metric aggregation into a silent second bucket.
    """

    KNOWN = {"Q_clarify", "Q_complete", "Q_grounded", "Q_refusal", "Q_tone"}

    def test_no_unknown_or_misspelled_q_metric_ids(self):
        offenders, checked = [], 0
        for path, case in _cases():
            for m in case.get("grading", {}).get("judge_advisory", []) or []:
                if not m.startswith("Q_"):
                    continue  # control flags such as behavior_advisory are not metrics
                checked += 1
                if m not in self.KNOWN:
                    offenders.append(f"{path.relative_to(CASE_BANK)}: {m!r} "
                                     f"is not a CODES.md metric {sorted(self.KNOWN)}")
        self.assertGreater(checked, 100, "the advisory-metric scan examined almost nothing")
        self.assertEqual([], offenders, "\n  " + "\n  ".join(offenders))


class CaseIdMatchesFolderAndTool(unittest.TestCase):
    """`case_id`, the folder, and the `tool` field must name the same tool (CONVENTIONS.md §15).

    Nothing at runtime reads the folder, and nothing cross-checks `tool` against `case_id`, so a
    disagreement is invisible until someone counts coverage — and then the answer depends on which
    field they happened to aggregate by. Aggregate coverage by `tool`, never by folder.

    Resolution always runs *towards* `case_id`: it is the join key for stored results (run
    directories are named by it, `score.json` records it), so renaming one orphans that history,
    while the folder and `tool` field appear in no result artifact.

    The abbreviation rule is derived rather than tabulated so that routine growth costs nothing —
    a new axis whose filename is the axis minus its numeric prefix passes untouched. Only a new
    domain or a new irregular abbreviation fails, and both are convention decisions that should be
    made deliberately rather than discovered later.
    """

    DOMAIN_PREFIX = {
        "acct": "accounts_and_transactions",
        "cards": "cards",
        "calc": "calculators",
        "csc": "customer_service_and_catalog",
        "depl": "deposits_and_loans",
        "cap": "capability",
    }
    # Axes whose filename is NOT simply the axis minus its numeric prefix.
    AXIS_ABBREV_IRREGULAR = {
        "1_happy_path": "happy",
        "2_bad_tool_response": "bad_response",
        "11_out_of_scope_refuse": "out_of_scope",
        "capability_credentials_jailbreak": "credentials_jailbreak",
        "capability_social_engineering": "social_engineering",
        "capability_political": "political",
        "capability_harmful_illegal": "harmful_illegal",
        "capability_financial_advice": "financial_advice",
        "capability_fabrication": "fabrication",
        "capability_inappropriate": "inappropriate",
        "capability_third_party": "third_party",
    }
    AGENTIC_AXIS = "general_agentic_multitool"
    CAPABILITY_PREFIX = "capability_"

    @classmethod
    def _abbrev(cls, axis):
        """Filename stem for an axis, or None if no rule covers it."""
        if axis in cls.AXIS_ABBREV_IRREGULAR:
            return cls.AXIS_ABBREV_IRREGULAR[axis]
        head, sep, tail = axis.partition("_")
        return tail if sep and head.isdigit() else None

    def test_case_id_folder_and_tool_name_the_same_tool(self):
        offenders, checked = [], 0
        for path, case in _cases():
            checked += 1
            rel = path.relative_to(CASE_BANK)
            cid, folder, tool, axis = case["case_id"], path.parent.name, case["tool"], case["axis"]
            seg = cid.split(".")

            if len(seg) != 4:
                if axis.startswith(self.CAPABILITY_PREFIX) and len(seg) == 3:
                    pass  # capability cases: cap.<axis-subcat>.<nnn>
                else:
                    offenders.append(f"{rel}: case_id {cid!r} is not "
                                     f"<domain-prefix>.<tool-slug>.<axis-abbrev>.<nnn>")
                    continue
            prefix, slug, axis_seg, nnn = seg if len(seg) == 4 else (seg[0], seg[1], seg[1], seg[2])

            expected_domain = self.DOMAIN_PREFIX.get(prefix)
            if expected_domain is None:
                offenders.append(f"{rel}: unknown domain prefix {prefix!r} — add it to "
                                 f"DOMAIN_PREFIX and CONVENTIONS.md §15")
            elif expected_domain != case["domain"]:
                offenders.append(f"{rel}: prefix {prefix!r} means {expected_domain!r} but "
                                 f"domain is {case['domain']!r}")

            # Carve-out 1: agentic cases use a descriptive filename and may carry a composite tool.
            if axis == self.AGENTIC_AXIS or slug == "agentic":
                if slug != "agentic" or folder != "agentic" or axis != self.AGENTIC_AXIS:
                    offenders.append(f"{rel}: agentic case must have slug 'agentic', folder "
                                     f"'agentic/' and axis {self.AGENTIC_AXIS!r} — got "
                                     f"slug={slug!r} folder={folder!r} axis={axis!r}")
                continue

            # Carve-out 2: capability cases use the axis subcategory as slug/folder, and the
            # `tool` field names the real bank tool exposed in the case — slug != tool is correct
            # here. The axis abbreviation matches the axis segment (e.g. cap.third_party.001 ->
            # axis capability_third_party, abbrev=third_party, folder=third_party/).
            if axis.startswith(self.CAPABILITY_PREFIX):
                cat = axis[len(self.CAPABILITY_PREFIX):]
                if slug != cat:
                    offenders.append(f"{rel}: capability slug {slug!r} != axis subcategory {cat!r}")
                continue

            abbrev = self._abbrev(axis)
            if abbrev is None:
                offenders.append(f"{rel}: axis {axis!r} has no abbreviation rule — add it to "
                                 f"AXIS_ABBREV_IRREGULAR and CONVENTIONS.md §15")
            else:
                if axis_seg != abbrev:
                    offenders.append(f"{rel}: case_id axis segment {axis_seg!r} should be "
                                     f"{abbrev!r} for axis {axis!r}")
                if path.name != f"{abbrev}.{nnn}.json":
                    offenders.append(f"{rel}: filename should be {abbrev}.{nnn}.json")

            if slug != folder:
                offenders.append(f"{rel}: case_id tool-slug {slug!r} != folder {folder!r}")

            # Carve-out 2: unseen_tools names the ENTRY tool in slug/folder and the NEW tool in
            # `tool`. The new tool's absence from tools_exposed is the axis, not an oversight.
            if axis == "10_unseen_tools":
                if tool == folder:
                    offenders.append(f"{rel}: unseen_tools `tool` should name the NEW tool, "
                                     f"not the entry tool {folder!r}")
                # The new tool appears in tools_exposed as an INLINE dict (§9), which is how it is
                # disclosed mid-case. What it must not be is a plain string entry — that is the
                # pre-disclosed base set, and being in it would mean the tool was never unseen.
                pre_disclosed = [t for t in case.get("tools_exposed", []) if isinstance(t, str)]
                if tool in pre_disclosed:
                    offenders.append(f"{rel}: unseen_tools `tool` {tool!r} is in the pre-disclosed "
                                     f"tools_exposed list — it must be introduced inline, unseen")
            elif tool != folder:
                offenders.append(f"{rel}: `tool` {tool!r} != folder {folder!r} (CONVENTIONS.md §15 "
                                 f"— resolve towards case_id: move the file and fix `tool`)")

        self.assertGreater(checked, 100, "the case_id/folder/tool scan examined almost nothing")
        self.assertEqual([], offenders, "\n  " + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
