"""Tests for tool contracts."""
import collections
import json
from datetime import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from indicbankbench.harness import paths, tools  # noqa: E402
from indicbankbench.harness.grader import _validate_call_schema  # noqa: E402

CASE_BANK = paths.case_bank_path()

def setUpModule():
    if CASE_BANK is None:
        raise RuntimeError(paths.FETCH_HINT)


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


class CaseBankPreflight(unittest.TestCase):

    def test_every_case_exposes_unique_tool_names(self):
        duplicates = []
        for path, case in _cases():
            names = [entry if isinstance(entry, str) else entry["name"]
                     for entry in case["tools_exposed"]]
            repeated = sorted(name for name, count in collections.Counter(names).items() if count > 1)
            if repeated:
                duplicates.append(f"{path.relative_to(CASE_BANK)}: {', '.join(repeated)}")
        self.assertEqual([], duplicates, "duplicate exposed tool names:\n  " + "\n  ".join(duplicates))

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


class TransactionRecordShape(unittest.TestCase):

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


    def test_transaction_values_follow_declared_enums(self):
        raw, _ = tools.load_tool_definitions()
        tool = next(t for t in raw["tools"] if t["name"] == "get_transaction_history")
        fields = tool["output_schema"]["properties"]["transactions"]["items"]["properties"]
        for rel, records in self._fixtures():
            for i, record in enumerate(records):
                with self.subTest(case=str(rel), row=i):
                    for name, spec in fields.items():
                        if "enum" in spec and name in record:
                            self.assertIn(record[name], spec["enum"])

    def test_transaction_timestamps_are_newest_first(self):
        for rel, records in self._fixtures():
            with self.subTest(case=str(rel)):
                dates = []
                for record in records:
                    value = record["date"]
                    self.assertIn("T", value)
                    dates.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
                self.assertEqual(sorted(dates, reverse=True), dates)

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


class MockOutputShape(unittest.TestCase):

    def _mocks(self):
        raw, canonical = tools.load_tool_definitions()
        schemas = {t["name"]: t["output_schema"] for t in raw["tools"]}
        found = 0
        for path, case in _cases():
            inline = {t["name"] for t in case.get("tools_exposed") or [] if isinstance(t, dict)}
            for name, mock in (case.get("mock") or {}).items():
                if name in inline:
                    continue
                self.assertIn(name, canonical,
                              f"{path.name} mocks '{name}', which is not a real tool")
                found += 1
                yield path.relative_to(CASE_BANK), name, mock, canonical[name], schemas[name]
        if not found:
            raise AssertionError("_mocks() yielded nothing — both shape tests would pass vacuously")

    @staticmethod
    def _wrapper(tool):
        outs = {o["name"]: o for o in tool.get("outputs") or []}
        arrays = [n for n, o in outs.items() if str(o.get("type", "")).startswith("array")]
        return outs, (arrays[0] if arrays else None)

    def test_no_record_emits_an_undeclared_field(self):
        checked = 0
        for rel, name, mock, tool, schema in self._mocks():
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
            required = schema["properties"][wrapper]["items"].get("required", [])
            for i, record in enumerate(mock["records"]):
                self.assertTrue(set(required) <= set(record), f"{rel}: missing required record fields")
                extra = [k for k in record if k not in item and not k.startswith("_")]
                with self.subTest(case=str(rel), tool=name, row=i):
                    self.assertEqual([], extra, f"{name} does not declare {extra}")
                checked += 1
        self.assertGreater(checked, 0, "no record outputs checked")

    def test_no_outcome_emits_an_undeclared_field(self):
        for rel, name, mock, tool, schema in self._mocks():
            if mock.get("output") is None:
                continue
            outs, _ = self._wrapper(tool)
            extra = [k for k in mock["output"] if k not in outs and not k.startswith("_")]
            with self.subTest(case=str(rel), tool=name):
                self.assertEqual([], extra, f"{name} does not declare {extra}")


class InvariantScope(unittest.TestCase):

    def test_s2_tracks_catalog_write_exposure(self):
        raw, _ = tools.load_tool_definitions()
        writes = {t["name"] for t in raw["tools"] if t.get("action_type") == "write"}
        for path, case in _cases():
            exposed = {t["name"] if isinstance(t, dict) else t
                       for t in case.get("tools_exposed") or []}
            with self.subTest(case=str(path.relative_to(CASE_BANK))):
                self.assertEqual(
                    bool(exposed & writes),
                    "S2" in case["grading"]["invariants_active"],
                    "S2 must be active iff the case exposes a catalog write tool — on everywhere else it "
                    "overstates coverage, off where a write exists it disables a real gate")


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
        self.assertGreater(checked, 0, "no applicable prerequisites checked")
        self.assertEqual([], offenders, "\n  " + "\n  ".join(offenders))


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
                                     f"is not a RESULTS.md metric {sorted(self.KNOWN)}")
        self.assertGreater(checked, 0, "no advisory metrics checked")
        self.assertEqual([], offenders, "\n  " + "\n  ".join(offenders))


class UnseenToolDisclosure(unittest.TestCase):
    def test_unseen_tools_are_not_pre_disclosed(self):
        for path, case in _cases():
            if case["axis"] == "10_unseen_tools":
                with self.subTest(case=str(path.relative_to(CASE_BANK))):
                    disclosed = {t for t in case["tools_exposed"] if isinstance(t, str)}
                    self.assertNotIn(case["tool"], disclosed)
