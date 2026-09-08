"""Tests for tool contracts."""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from indicbankbench.harness import judge, paths, tools  # noqa: E402
from indicbankbench.harness.grader import _validate_call_schema  # noqa: E402

CASE_BANK = paths.case_bank_path()

# Skip data-dependent tests on a bare harness checkout.
needs_cases = unittest.skipIf(
    CASE_BANK is None,
    f"case bank not present; fetch the dataset and set ${paths.DATA_ENV}")


def _cases():
    """Yield every active case, failing if none are found."""
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


@needs_cases
class SchemaParity(unittest.TestCase):

    def test_every_exposed_tool_reaches_the_model_with_its_full_description(self):
        model_schemas = tools.load_model_schemas()
        missing = set()
        for _path, case in _cases():
            for entry in case["tools_exposed"]:
                if isinstance(entry, str) and entry not in model_schemas:
                    missing.add(entry)
        self.assertEqual(set(), missing)


class Disclosure(unittest.TestCase):

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


@needs_cases
class GoldCallRoundTrip(unittest.TestCase):

    def test_every_gold_call_passes_s3(self):
        _, defs = tools.load_tool_definitions()
        model = tools.load_model_schemas()
        failures = []
        for path, case in _cases():
            try:
                raw, _ = tools.resolve_tools_exposed(case["tools_exposed"], defs, model)
            except KeyError:
                continue
            for call in case["gold"].get("tool_calls", []):
                errs = _validate_call_schema(call["tool"], call.get("arguments", {}), raw.get(call["tool"]))
                if errs:
                    failures.append(f"{path.relative_to(CASE_BANK)}: {'; '.join(errs)}")
        self.assertEqual(
            [], failures,
            f"{len(failures)} gold calls rejected by S3:\n  " + "\n  ".join(failures[:15]),
        )


@needs_cases
class CaseBankPreflight(unittest.TestCase):

    def test_every_exposed_tool_name_resolves(self):
        _, defs = tools.load_tool_definitions()
        offenders = {}
        for path, case in _cases():
            for entry in case["tools_exposed"]:
                if isinstance(entry, str) and entry not in defs:
                    offenders.setdefault(entry, []).append(str(path.relative_to(CASE_BANK)))
        summary = [f"{tool} — {len(files)} cases (e.g. {files[0]})" for tool, files in sorted(offenders.items())]
        self.assertEqual([], summary, "unresolvable tool names:\n  " + "\n  ".join(summary))


@needs_cases
class MockWrapperResolution(unittest.TestCase):

    def test_every_record_set_mock_wraps_under_a_declared_array(self):
        _, defs = tools.load_tool_definitions()
        offenders = {}
        for path, case in _cases():
            for name, mock in (case.get("mock") or {}).items():
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


@needs_cases
class MockHygiene(unittest.TestCase):

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
        case = self._case(["behavior_advisory", "Q_refusal"])
        self.assertIn("behavior_advisory", case["grading"]["judge_advisory"])


@needs_cases
class TransactionRecordShape(unittest.TestCase):

    FIELDS = ["date", "amount", "type", "channel", "reference_id", "transaction_note",
              "status", "running_balance"]
    CHANNELS = {"upi", "neft", "imps", "rtgs", "atm", "pos", "cheque", "internal"}
    STATUSES = {"success", "pending", "failed", "reversed"}

    def _fixtures(self):
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


@needs_cases
class MockOutputShape(unittest.TestCase):

    def _mocks(self):
        _raw, canonical = tools.load_tool_definitions()
        found = 0
        for path, case in _cases():
            inline = {t["name"] for t in case.get("tools_exposed") or [] if isinstance(t, dict)}
            for name, mock in (case.get("mock") or {}).items():
                if name in inline:
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


@needs_cases
class InvariantScope(unittest.TestCase):

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


@needs_cases
class InlineToolDisclosure(unittest.TestCase):

    def _inline_tools(self):
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
        _, real = tools.load_tool_definitions()
        for rel, _case, tool in self._inline_tools():
            with self.subTest(case=str(rel), tool=tool["name"]):
                self.assertNotIn(tool["name"], real)


@needs_cases
class DisclosedPrerequisiteIsReachable(unittest.TestCase):

    # Approved cases without a transaction to locate.
    NO_TRANSACTION_TO_LOCATE = {
        "csc.get_insurance_details.confusing_intent.001",
        "csc.raise_request.confusing_intent.001",
        "csc.search_knowledge_base.bad_response.001",
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
                    # Declared prerequisites may be alternatives.
                    if any(alt in allowed for alt in prereqs if alt != pre):
                        continue
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


@needs_cases
class EnumValueIsObtainable(unittest.TestCase):
    INFERENCE_BY_DESIGN = {
        "cards.block_card.confusing_intent.001",
        "cards.toggle_card_freeze.happy.001",
        "acct.stop_cheque_payment.confusing_intent.001",
        "csc.agentic.dispute_unauthorized.001",
        "csc.raise_request.not_enough_info.001",
        "csc.get_insurance_details.confusing_intent.001",
        "csc.raise_request.happy.001",
        "depl.create_fd.confusing_intent.001",
        "depl.update_deposit_renewal.happy.001",
        "acct.get_transaction_history.confusing_intent.001",
        "calc.calculate_fd_maturity.happy.001",
        "calc.calculate_rd_maturity.confusing_intent.001",
        "calc.calculate_rd_maturity.happy.001",
        "calc.get_deposit_loan_rates.happy.001",
        "depl.create_fd.context_switching.001",
        "depl.get_gold_rate.confusing_intent.001",
        "calc.get_deposit_loan_rates.confusing_intent.001",
        "csc.get_all_requests.context_switching.001",
        "depl.agentic.book_fd.002",
        "depl.agentic.book_rd.001",
        "csc.get_products_and_offers.irrelevant_rag.002",
        "depl.create_fd.not_enough_info.004",
    }

    @staticmethod
    def _stated(value, text):
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


@needs_cases
class AdvisoryMetricIdsAreKnown(unittest.TestCase):

    KNOWN = {"Q_clarify", "Q_complete", "Q_grounded", "Q_refusal", "Q_tone"}

    def test_no_unknown_or_misspelled_q_metric_ids(self):
        offenders, checked = [], 0
        for path, case in _cases():
            for m in case.get("grading", {}).get("judge_advisory", []) or []:
                if not m.startswith("Q_"):
                    continue
                checked += 1
                if m not in self.KNOWN:
                    offenders.append(f"{path.relative_to(CASE_BANK)}: {m!r} "
                                     f"is not a CODES.md metric {sorted(self.KNOWN)}")
        self.assertGreater(checked, 100, "the advisory-metric scan examined almost nothing")
        self.assertEqual([], offenders, "\n  " + "\n  ".join(offenders))


@needs_cases
class CaseIdMatchesFolderAndTool(unittest.TestCase):

    DOMAIN_PREFIX = {
        "acct": "accounts_and_transactions",
        "cards": "cards",
        "calc": "calculators",
        "csc": "customer_service_and_catalog",
        "depl": "deposits_and_loans",
        "cap": "capability",
    }
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
                    pass
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

            if axis == self.AGENTIC_AXIS or slug == "agentic":
                if slug != "agentic" or folder != "agentic" or axis != self.AGENTIC_AXIS:
                    offenders.append(f"{rel}: agentic case must have slug 'agentic', folder "
                                     f"'agentic/' and axis {self.AGENTIC_AXIS!r} — got "
                                     f"slug={slug!r} folder={folder!r} axis={axis!r}")
                continue

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

            if axis == "10_unseen_tools":
                if tool == folder:
                    offenders.append(f"{rel}: unseen_tools `tool` should name the NEW tool, "
                                     f"not the entry tool {folder!r}")
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
