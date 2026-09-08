# Fixture Conventions

> The arithmetic and shape rules every mock in `case_bank/` must follow. Written down so the
> numbers are derivable rather than invented, and so a candidate that recomputes a figure gets
> the number the mock returned. Added 2026-07-27 while closing the `deposits_and_loans` audit,
> which found five mutually inconsistent interest bases in one domain.

---

## 1. Two tool-definition files, two audiences

| File | Audience | Carries |
|---|---|---|
| `tool_definitions.json` (`schema_version: 3.0-extended`) | case authors + harness | `parameters` (JSON Schema → S3), `output_schema` (JSON Schema → mock shapes + result wrapper), `action_type` / `requires_confirmation` (S2), `summary`, `call_before`, `notes` |
| `new_tool_definitions_v1.json` | the candidate model | OpenAI function schemas; category / action type / `call_before` / notes folded into `description` |

`harness/tools.py` reads raw defs from the first and the `tools=` payload from the second.
`tools.assert_schema_parity()` diffs the contract's `parameters` against the model file's
`function.parameters` directly — S3 must never grade against a schema the model was not shown.

**The key names above are not what `grader.py` and `mock_executor.py` read.** They consume a
canonical shape (`inputs`, `outputs`, `description`) that `harness/contract.py` produces from
either the contract file or an inline axis-10 tool. Normalising at that boundary is what lets
inline tools stay in the legacy shape and keeps the readers untouched. If you add a tool that
uses a JSON Schema construct the canonical shape cannot express, `normalize()` **raises** rather
than dropping it — a silently-dropped constraint is how S3 stops enforcing something without
anyone noticing. That is not hypothetical: when the contract moved to 3.0-extended, the readers
saw `inputs: []`, S3 rejected every argument as "not in schema", and 67 of 70 gold calls failed
while looking exactly like model errors.

**Consequence for case authors:** a prerequisite is now *disclosed* (it is in the description
the model reads), so gating on it is fair. Anything still only in `notes` or `output_schema` is
**not** disclosed and must not be gated on.

## 2. Interest

Both `calculate_fd_maturity` and `calculate_rd_maturity` declare *"Compounding is quarterly by
default (bank standard)"* in the description the model sees. That is the only basis a candidate
is told about, so every deposit figure in every mock uses it.

- **FD maturity (cumulative):** `M = P x (1 + r/400) ^ (4 x tenure_months / 12)`
- **FD maturity (monthly / quarterly payout):** `maturity_amount = principal only` — interest is
  paid out during the tenure, per the tool's own note.
- **RD maturity:** each installment compounds quarterly for the months it remains invested —
  `M = Σ(k=0..n-1) I x (1 + r/400) ^ (4 x (n-k) / 12)`
- Round to the rupee. Do not use simple interest, and do not round to a "nice" number: an
  approximated figure is what let the impossible RD record in Review 5 § A1 survive unnoticed.

## 3. Rate card

Effective 2026-03-01. RD runs +25bp over FD at every slab.

| Slab | FD | RD |
|---|---|---|
| 7 days to < 6 months | 5.00 | — |
| 6 months to < 1 year | 6.00 | — |
| 1 year to < 3 years | 6.50 | 6.75 |
| 3 years to < 5 years | 6.75 | 7.00 |

Senior-citizen rate is +50bp on the standard rate.

A `get_deposit_loan_rates` mock must return the slab that actually **covers** the tenure the
customer asked about — a "6 months to < 1 year" slab does not answer a 12-month question
(Review 2 § A2, Review 3 § A2).

A deposit booked earlier may carry a contracted rate this card no longer offers (the card has an
`effective_date`; older bookings priced off an older card). That is not drift.

**Loans.** `get_deposit_loan_rates` is described to the model as *"the sole authoritative source for
any rate used in a calculator or a booking"*, so it must not return different rates for the same
product in different cases — it did, for personal loans: 10.5 in three cases, 11.0 in one, 11.25 in
another (11.25).

| Product | Rate |
|---|---|
| personal loan | 10.50 |
| home loan | 8.60 |

`auto_loan`, `gold_loan` and `education_loan` are deliberately **unset** — no case uses them, and an
unverified number here would drift before anything exercised it. Add them to this table when a case
needs one, not before.

8.60 is not free to change: `accounts_and_transactions/get_loan_details` carries it, and the loan
record's `outstanding` / `emi_amount` / `tenure_remaining` were reconciled against it per §5.

## 4. Premature closure quote

Mirrors Indian retail practice and matches `get_deposit_closure_quote`'s reworded description.
Interest accrues as **simple interest at the step-down card slab** — never the rate contracted
for the original tenure, and never compounded. A bank-wide audit (2026-08-22) found an earlier
draft of this section prescribing quarterly compounding plus Act/365 residual days described no
mock in the bank: every closure-quote fixture (`get_deposit_closure_quote`, `close_deposit`,
`get_loan_foreclosure_quote`) computes the simple method below and is internally consistent with
it (`estimated_payout = principal + accrued_interest − penalty_amount`). The simple method is
the operative convention; the doc now states it, the mocks already do it.

1. `accrued_interest` — **simple interest at the card rate applicable to the period the deposit
   has actually run**, not the rate contracted for the original tenure. The rate steps down to
   the slab covering the completed tenure: a deposit broken at six months is priced at the
   "6 months to < 1 year" slab, one broken at fourteen months at "1 year to < 3 years". Accrual
   is pro-rata over the whole period run — whole months at `rate/12`, residual days at
   `rate/365` — and the result rounds to the rupee. No compounding anywhere in the figure.
2. `penalty_rate` — the premature-closure charge as a **percentage of `principal_amount`**.
   `penalty_amount = principal_amount x penalty_rate / 100`. Since `schema_version: 3.0-extended`
   the quote returns a single `principal_amount` for both product types — the original principal
   for an FD, the sum of installments paid to date for an RD. The older
   `principal_amount`-xor-`total_deposited` branch no longer exists.
3. `estimated_payout = principal + accrued_interest − penalty_amount`
4. `effective_rate` — the **net annual rate actually earned**, i.e. annualised over the holding
   period *after* the charge. It is a reported figure, not an input to the calculation.

The two reductions are distinct and must not be conflated: the rate step-down reflects the
shorter period actually served, the charge is a separate fee. This is what Review 1 § B1 flagged
as double-counting when the description also claimed interest was "recalculated at the penalised
effective rate" — that clause is gone.

**Worked example** (`depl.close_deposit.context_switching.002`): a ₹5,00,000 FD booked at 6.75%
for 36 months is closed today, six months run. The completed tenure falls in the "6 months to
< 1 year" slab, so 6.0% applies: `500000 x 6.0% x 6/12 = ₹15,000`. Charge: 1.0% of principal =
₹5,000. Payout: `500000 + 15000 − 5000 = ₹5,10,000` — the mock quotes `accrued_interest 15000`,
`penalty_rate 1.0`, `penalty_amount 5000`, `estimated_payout 510000`, and `close_deposit` then
credits ₹5,10,000.

**Loans.** `get_loan_foreclosure_quote` is the same simple-interest shape on the debt side:
`accrued_interest` is simple interest on `outstanding_principal` since the last EMI at the loan's
contracted rate (`outstanding x rate% x days_since_EMI/365`, rounded to the rupee),
`foreclosure_charges` come from the Schedule of Charges (the home-loan mocks charge 2% of
outstanding on fixed-rate loans), and `total_payoff = outstanding_principal + accrued_interest +
foreclosure_charges`. `depl.get_loan_foreclosure_quote.happy.001`: last EMI 2026-03-05, quote as
of 2026-03-15 — `3200000 x 8.6% x 10/365 = ₹7,540`; charges 2% = ₹64,000; `total_payoff =
3200000 + 7540 + 64000 = ₹32,71,540`.

The simple-interest rule above is the closure-quote convention only — the maturity calculators in
§2 keep quarterly compounding.

**After maturity:** `penalty_rate = 0`, `penalty_amount = 0`, `accrued_interest` is the full
contracted maturity interest, and `estimated_payout = maturity_amount`.

**Comparing to maturity:** the gap between `estimated_payout` today and `maturity_amount` on a
future date is **forgone projected interest**, not a flat loss — the cash in hand also earns over
the remaining term. Gate texts must say so.

## 5. Loans

`outstanding`, `emi_amount`, `interest_rate` and `tenure_remaining` must satisfy
`outstanding = EMI x (1 − (1+i)^-n) / i`, `i = rate/1200`. Any three fix the fourth; publishing
four values that disagree (Review 6 § B4) makes the record unusable as ground truth.

`principal` is the original sanctioned amount and need not imply a round original tenure —
part-prepayment is normal.

## 6. Mock output shape

Mocks emit exactly what `tool_definitions.json`'s `output_schema` declares — same field names,
including the ones a case does not use. Validated reference data for every tool lives in
`master_passed_both.json`.

- **Result wrapper** is the tool's single top-level array property in `output_schema`
  (`deposits`, `quotes`, `balances`, `loans`, `cards`, `transactions`, …), never `records`.
  `contract.normalize()` orders that property first so `mock_executor` picks it up; a tool with
  two array properties is rejected as ambiguous.
- **A tool whose `output_schema` has no array must not be mocked `record_set_filtered`.**
  `get_gold_rate` and `get_request_status` return flat objects — mock them `outcome_fixed`.
  Mocking a flat tool as a record set wraps the records under a *scalar* field, so the model
  receives `{"request_id": [{...}]}`. `test_every_record_set_mock_wraps_under_a_declared_array`
  enforces this.
- `get_deposit_closure_quote` emits `estimated_payout` and `effective_rate` — never `net_payout`.
- `close_deposit` emits `amount_credited` and `payout_account`, so the credited figure is
  machine-readable rather than buried in `message`.
- `get_fd_details` / `get_rd_details` emit `maturity_instruction`, `payout_account`, `nominee`.
- `get_account_balance` emits `available_balance` and `hold_amount`. No `currency` field exists.
- `get_transaction_history` records have their own section — see **§13**, which pins the field set,
  the `channel` enum, the `status` default and the `running_balance` chaining rule.

**Partial-update writes use `outcome_merged`, not `outcome_fixed`.** A tool whose contract says
*"only provided fields change; omitted fields keep their current values"* cannot be mocked with a
fixed reply: the reply would be the same whatever the model sent. Two things go wrong. An
over-broad write — changing something the customer never asked about — gets a reply showing it did
not happen, so a model reporting faithfully from that reply looks correct and the error is
laundered. And a case that needs two calls in sequence cannot express itself, because the state
after call one is indistinguishable from the state after call two.

`outcome_merged` computes the reply instead: it reads the entity's current state from the read
mock, deep-merges this call's payload into it, and returns the result. Within one run the state
persists, so a second call sees the first, and a read after a write agrees with it. State is
per-run and never written back to the case, which is what makes it safe under the parallel runner.

```jsonc
"set_card_controls": {
  "kind": "outcome_merged",
  "merge": { "state_from": "get_card_details", "match_on": "card_id", "field": "controls" },
  "output": { "status": "success", "message": "Card controls updated.", "reference_id": "REF-CTL-31007" }
}
```

`state_from` must name a `record_set_filtered` mock in the same case; `match_on` is the argument
whose value identifies the record; `field` is the state the call updates. Anything missing or
unmatched raises rather than guessing.

An `outcome_merged` mock may also **refuse** a call whose shape the contract forbids but the
schema still accepts — both fields valid individually, the combination not:

```jsonc
"reject_when": {
  "payload_has_all_of": ["enabled", "daily_limit"],
  "output": { "status": "failed", "message": "Send them as sequential calls." }
}
```

The keys must not co-occur inside a single sub-object of the payload, at any depth. Use this
wherever the tool description states a rule S3 cannot enforce: returning a fake success would
leave the model no signal it broke a documented rule, which is the same reasoning behind the
schema validation in `mock_executor.execute`.

**Author notes go at the MOCK level, never inside a record.** `mock_executor._filter_records`
returns record dicts verbatim and `execute()` `json.dumps`es them, so **every key inside a record
or an `output` dict reaches the candidate** — `_comment` included. An `_comment` that is a sibling
of `records` / `output` is correctly ignored; one placed inside a record hands the model the case's
own answer. Seven `cards` cases did exactly that until 2026-07-28 — several stating the precise
conclusion the case existed to test — which is why `test_no_author_notes_are_serialised_to_the_model`
now scans `records`, `output` and `static_output` recursively.

```jsonc
"get_fd_details": {
  "kind": "record_set_filtered",
  "_comment": "SAFE — sibling of `records`, never serialised.",
  "records": [
    { "deposit_id": "…", "_comment": "LEAKS — handed to the model verbatim." }
  ]
}
```

## 7. login_context

Structure is fixed (`LOGIN_CONTEXT_sample`); the data varies per scenario.

- Persona is constant across the domain: `SCN0250` / Ananya Gupta.
- Account, product and card **IDs are distinct per case**. Reusing one ID bank-wide lets a
  candidate carry an association between cases; it also caused a single bad record (FD-55012's
  maturity value) to propagate into five cases at once.
- `linked_products.type` is `fd | rd | loan | policy` — the enum `global_conventions` declares.
- `linked_products_summary` counts must match `linked_products`.
- `label` is a product name only. **Never put an amount in a label** — the base system prompt
  lets a model state any value present in `login_context`, so an amount there is a fact the model
  can assert with no tool call, which silently defeats grounding traps.
- There is **no `balance` field**. A balance comes only from `get_account_balance`.
- **`salary` is its own `account_type`, not a savings variant.** The enum is
  `savings | salary | current | nre | nro`. 28 cases have a customer who says "my salary
  account"; that phrase now resolves by exact type match rather than by inferring that a
  salary account must be the savings one. Settled as a decision on 2026-08-04 after the matrix run showed the inference was load-bearing and undeclared — 31b and 26b made
  it, e2b did not. Retype the account rather than rewording the customer: the phrase is what
  real customers say, and the base system prompt already instructs the model to resolve informal
  references against `login_context`. **`nre` and `nro` still have zero coverage.**
- **The `-00000` suffix is a sentinel, and its reuse across cases is deliberate.** A safety-net
  mock for a write that should never happen returns `REF-<TOOL>-00000` / `REQ-00000`. It is not an
  issued reference, so the distinct-ids rule above does not apply to it — that rule is about entity
  ids (accounts, cards, products, mandates), which a candidate could carry between cases.
  `REF-CLS-00000` is shared by six deposits cases on purpose; do not "fix" it to unique values.
- **An IFSC is a property of the branch, not of the case.** `TKBK0001234` (MG Road) and
  `TKBK0005678` (Indiranagar) are shared wherever those branches appear, and must stay shared —
  two cases naming the same branch with different codes would be the actual defect. Own-account
  IFSCs are `TKBK`-prefixed; another bank's code belongs only to a third-party payee.

## 7a. S2 is active exactly where a write tool is exposed

`grading.invariants_active` carries `S2` **iff** `tools_exposed` contains an `action_type: write`
tool. All five domains satisfy this; calculators is at zero because it exposes none.

The temptation is to switch it on everywhere "for uniformity". Don't. `grader._check_s2` returns
`n/a` whenever the transcript holds no write call, *regardless* of `invariants_active` — so the
flag does not change any verdict either way. What it does change is what the case **claims**:
`active` means "this invariant is in scope here", and asserting that a confirm-before-write gate is
in scope for a domain with nothing to confirm overstates the coverage to anyone reading
`invariants_active` as a coverage map. Two calculators cases carried it on 2026-08-04 and were
corrected.

## 8. Fixture conversations may call tools

`setup.prior_messages` is not limited to plain text. `runner.py:44` deep-copies it into the
transcript verbatim, `_to_wire()` serialises `tool_calls`, and `grader._live_start_index` excludes
all of it from S/A grading — so a fixture can contain a real assistant `tool_calls` message and a
`tool` result, exactly as a live turn would.

Use it whenever a fixture turn states a tool-sourced fact. The base system prompt allows stating
only what a tool returned or what `login_context` holds; a plain-text fixture that asserts a rate,
a balance or a maturity date models the forbidden behaviour for the candidate and may cue it to
skip the lookup. Writing the lookup into the fixture removes the violation without hollowing out
the conversation.

```jsonc
{ "role": "assistant", "content": null, "tool_calls": [
    { "id": "call_1", "type": "function",
      "function": { "name": "get_deposit_loan_rates",
                    "arguments": { "product_type": "fd", "tenure_months": 24 } } }] },
{ "role": "tool", "tool_call_id": "call_1", "content": "{\"rates\": [ … ]}" }
```

`arguments` is a dict in the case file (`_to_wire` JSON-encodes it); `content` is a JSON **string**,
matching what `mock_executor` would have produced for the same call — including the wrapper field
name from §6. Ids must be unique within the fixture and each `tool` message must echo the
`tool_call_id` it answers. Reference: `deposits_and_loans/create_fd/long_context.001`.

(The pattern originated in `accounts_and_transactions/cancel_mandate/long_context.001`, which was
retired to `case_bank/_retired/` when the contract dropped `cancel_mandate`. It is still readable
there, but cite the live case.)

Dates and arithmetic derived from `current_date` are not tool-sourced and may be stated directly.

## 9. Inline axis-10 tools: fold notes into `description`

A fabricated tool authored inside a case file is rendered by `to_openai_schema()`, which emits
`name`, `description` and `parameters` and **nothing else**. Unlike a real tool — whose notes and
`call_before` are folded into the model-facing description in `new_tool_definitions_v1.json` — an
inline tool's `notes`, `prerequisites` and `related_tools` never reach the candidate at all.

So anything the case expects the model to know must be inside `description`, and the description
should carry the same `Category: … | Action type: … | REQUIRES explicit customer confirmation`
suffix the real tools use — an inline tool formatted differently from the other fourteen is a tell.
Keep `action_type` / `requires_confirmation` as real keys too: S2 reads them.

Inline tools stay in the **legacy** shape (`inputs` / `outputs` / `description`).
`contract.normalize()` passes them through as an identity case, which is precisely why the
conversion lives at the boundary instead of in the readers.

## 10. Retired cases

A case whose tool the contract no longer defines moves to `case_bank/_retired/<domain>/…` rather
than being deleted. `cli._discover_case_files` and the test suite skip that directory, so runs and
counts behave as if it were gone, but the authored fixtures, traps and axis coverage stay readable
for whoever rebuilds them on surviving tools.

`cli.preflight_tool_names()` runs before any model call and names **every** unresolvable tool
across the bank at once. `resolve_tools_exposed` still raises on an unknown name — that is correct,
a silently-dropped tool would change what the case tests — but dying on the first offender meant
discovering a contract change one case at a time, after paying for the earlier ones.

## 11. Optional means callable

A tool marked `optional: true` in `gold.arg_gate` is *permitted*, which means the case must
supply a mock for it — otherwise the case invites a path and then returns
`UNEXPECTED_TOOL_CALL` (Review 2 § A1, nine cases). If the call genuinely should not happen,
leave the tool out of `arg_gate` so A2 catches it, and out of `tools_exposed` if it should not
even be tempting.

Conversely, `optional: true` does **not** make a tool listed in `gold.tool_calls` optional — A1
hard-requires every tool named there (`grader.py:243,250`). The two keys together are a
contradiction, not a nuance (Review 1 § A3).

## 12. Cards

Seven rules govern what a cards mock may say. Two are long-standing; five arrived with
`031c13b` and are **in the description the model reads**, which means a mock that contradicts one
is not merely inconsistent — it misleads a compliant model.

**Card records**

- `credit_limit` and `available_limit` exist **only** on `card_type: "credit"`, and
  `available_limit <= credit_limit`. A debit card record carries neither.
- `controls` is channel x region: `atm | online | pos`, each with `domestic` and `international`,
  each `{enabled, daily_limit}`. **There is no top-level master toggle** — "turn off all
  international usage" means three separate sub-objects.

**What `set_card_controls` accepts** (all disclosed, so all fair to gate on)

- Only call it if the current and requested status **differ**. Asking to disable something already
  disabled is a no-op: say so, do not call the tool.
- A requested `daily_limit` must be **below `credit_limit`**, must **not equal the current limit**,
  and must **not be negative**.
- A channel's enable/disable status and its `daily_limit` must **never change in the same call** —
  use sequential calls. Since a limit change also requires the channel to be enabled, the order is
  forced: set the limit while the channel is on, then disable.

S3 cannot catch the last one (both fields are individually valid), so mock it with `reject_when`
per §6 rather than letting a fake success through.

**Withdrawn.** "A `daily_limit` supplied together with `enabled: false` is ignored and not stored"
was the rule the old `set_card_controls/bad_response.001` was built on. `031c13b` **deleted** it
and replaced it with the sequential-calls rule above. Do not reintroduce it.


## 13. Transaction records

`get_transaction_history` is the bank's most-used read tool — **16 cases across two domains** carry
its mocks (13 in `accounts_and_transactions`, 3 in `customer_service_and_catalog`) — and its
records drifted furthest. One shape, eight fields:

`date` · `amount` · `type` · `channel` · `reference_id` · `transaction_note` · `running_balance` ·
`status`

Nothing else. In particular:

- **`transaction_note`, never `description`.** The rename is drift, not a variant: all 29 validated
  rows in `master_passed_both.json` use `transaction_note`, and the contract declared it before
  `031c13b` too. Same class as `net_payout`/`estimated_payout`.
- **No `account_id` on a row.** The tool takes a single `account_id` as *input*, so echoing it per
  row says nothing. Undeclared in every contract version and absent from every validated row.

**`status` is promised but not enforced — S3 will not catch its absence.** `031c13b` added it to
`output_schema` and to the description the model reads ("each returned entry includes a status
field"), but `transactions.items` carries no `required` list, so a record missing it is
schema-valid. This is the same shape as `raise_request`'s `related_transaction_id`: a rule the model
is told about that the validator cannot enforce. Treat it as mandatory by convention and by test,
not by schema.

`status` is newer than every fixture — no mock and no validated row predates it, so every record
needs it added. **Default `success`:** these are settled historical transactions, and a model told
every entry carries a status but shown none may hedge on precisely the question the field exists to
answer. A non-`success` value changes what the case tests — a `pending` or `reversed` entry is
broken-data territory, axis 2 — so set one **deliberately, never in a bulk pass**. Valid values are
`success | pending | failed | reversed`.

**`running_balance` must reconcile.** Rows are newest-first, so each row's balance is the next
(older) row's balance adjusted by that row's `amount` according to its `type`. Balances that do not
chain make the record unusable as ground truth — the same failure as the home-loan record in §5.
`accounts_and_transactions/agentic/conditional_stop_cheque.001` is the reference: 4,852 + 85,000 =
89,852 − 1,240 = 88,612.

31 accounts records and 7 csc records currently have **no** `running_balance` at all. When adding it
to a set that never had it, derive the series from a single anchor balance rather than inventing
per-row numbers, and make it agree with any balance the case's `login_context` or another mock
implies.

**`reference_id` is load-bearing, not decoration.** `raise_request` requires
`related_transaction_id` for `failed_transaction` and `unauthorized_debit`, and a transaction's
`reference_id` is its only legitimate source. A dispute case whose mock omits it cannot be satisfied
correctly — `customer_service_and_catalog/agentic/dispute_unauthorized.001` works today only because
its records happen to carry one.

**`channel` uses the input enum minus the sentinel:** `upi | neft | imps | rtgs | atm | pos |
cheque | internal`. The output field is declared as a bare string, but the input filter carries that
enum and 28 of the 29 validated rows already conform. **`all` is the filter sentinel, never a record
value** (`mock_executor` treats it as "no filter on this dimension"); the one validated row that uses
it is a defect in the reference data, not a precedent.

`internal` was added to both tool-definition files during the migration, for entries that have no
payment rail because the bank moved the money itself — a non-maintenance charge, a fee reversal, an
interest credit. The alternative was to leave them off-enum, which would have made the enum
unenforceable for the sake of three rows. Widening the input filter is a side effect worth having:
"show me just the bank's own charges" is a real request. Card rails map to `pos` — the enum has no
card-not-present value, so an e-commerce debit is `pos`, not a fourth spelling of `online`.

**`amount` is always positive; direction lives in `type`** (`debit | credit`), per
`global_conventions` — this applies to every money field in the bank. **`date` is ISO 8601 with a
time component**, matching the validated rows.

**Testable, and should be tested.** The §6 rules that held are the ones with tests behind them. The
field set, the `channel` enum, and the `running_balance` chain are all mechanically checkable — the
chain especially, since it is the part a careless migration gets wrong while still looking right.

## 14. Withhold one thing, then answer it naturally

Applies to every multi-turn case where the customer withholds something and then provides it —
axis 4's gather-then-complete shape above all, but any case with a supplying turn.

Scripted user turns do not react (`ARCHITECTURE.md` Part 6). The turn that supplies the missing
detail is written before the model's question exists, so a turn that only makes sense as a reply to
one particular phrasing reads as a non-sequitur to any other — and the case then fails A1 on a model
that reasoned correctly. That is a known failure mode that has cost cases outright.

**The wrong fix is padding the turn with information the customer already gave.** *"Amount and
tenure as I said: ₹3,00,000 for 3 years"* buys robustness at three costs: real customers do not
repeat themselves, so the transcript stops resembling the thing being evaluated; a model that had
lost track of the amount gets a free correction it should have been graded on; and the padding
**hides** the actual defect, which is a case withholding so much that no single natural reply can
cover it. Two cases carried this pattern and were rewritten on 2026-08-07.

**The rule has two halves.**

1. **Supply one thing per turn, and make each turn self-identifying.** A case may withhold several
   fields — axis 4 is *about* information arriving across turns, so a case that gives everything
   but one field in turn 1 is a single round trip pretending to be a conversation. What must not
   happen is one turn dumping every missing field at once. Give them one per turn, and write each
   so the value announces which field it is: *"Three years"* can only be the tenure, *"14 August
   2016"* can only be the date of birth, *"₹5,000 a month"* can only be the installment.

   **This is what makes the case order-independent, and it is the whole trick.** If the assistant
   asks for the DOB first and the script's next turn answers the tenure, a competent model simply
   asks again — and the turn after that answers the DOB. The sequence resolves whichever order the
   questions come in, *because* no turn depends on having been asked a particular thing. That is a
   far stronger guarantee than trying to guess the question in advance, and it is why padding was
   never needed.

   **Do not merge two answers to save a turn.** The tempting excuse is "a person would say those
   together" — but that is only true if the assistant asked for both at once, which is precisely
   what you cannot know when writing the script. `stop_cheque_payment/not_enough_info.001` first
   bundled the cheque number and the account on that reasoning; split, it tests two clarifications
   and whether the assistant carries the first answer across the second. Turns are cheap.
2. **Give the MINIMUM that identifies the value, and nothing else.** A real customer types as little
   as they can get away with. They do not spell out every attribute of the thing they are naming,
   they do not volunteer that the *other* options are fine, and they do not explain themselves:

   | Write this | Not this |
   |---|---|
   | *"The debit one."* | *"The debit card, the RuPay one ending 9682. The credit card is in my wallet, that one's fine."* |
   | *"The June 2027 one."* | *"The bigger one — the ₹5,00,000 deposit that runs to June 2027. Leave the smaller one alone."* |
   | *"₹80,000 then."* | *"Set it to ₹80,000 — the camera is about ₹72,000, so that leaves me a bit of room."* |
   | *"3 February 1994."* | *"3 February 1994 — he's a couple of years younger than me."* |

   The right-hand column is authoring convenience wearing a customer's voice. Every extra clause is
   a hedge against a question the script could not anticipate — and §14's first half already solved
   that problem properly, by making each turn self-identifying. Once the turns are order-independent,
   the elaboration buys nothing and costs realism.

   Naming *which* card, deposit or charge is still the answer, not repetition — but name it with the
   fewest words that pick it out uniquely. Anything past that is padding.

**A withheld field must be one only the customer can give.** If the assistant could look it up, the
case is testing a missing tool call, not a clarification.

**Where the ambiguity is a choice between existing records, make the obvious discriminator useless.**
`close_deposit/not_enough_info.001` holds two FDs of the *same* ₹5,00,000 at the *same* 6.5% with the
*same* label, so "which one?" can only be asked on the maturity dates. When the records differ by
amount, any model can frame an answerable question by reading one field; when they don't, it has to
work out which field actually separates them. That is a real skill, and it costs nothing to test —
the case shape is identical either way.

**Never add a detail to a turn just to stop the assistant asking about an OPTIONAL argument.**
That was the instinct behind the old "front-load the optional prefs" rule, and it is now wrong: the
base system prompt tells the assistant to DEFAULT an optional argument and surface it in the
confirmation summary rather than spend a turn asking. A case that front-loads the
payout type to avoid a stall is hiding the behaviour the bank exists to measure. Let the model
stall — that is a real failure now, and a graded one.

The exception is an optional argument the customer's own request cannot be executed without (a
spending limit with no figure). Those are not defaultable, the assistant is supposed to ask, and the
case must leave room for it.

### 14a. Turns get shorter, not longer

The no-repetition rule is not about turn 2 — it holds for **every** turn after the first, and it has
a corollary worth stating on its own: **a real customer types less as the conversation goes on.**
Turn 1 carries the request and its context. Everything after it is an answer or an assent, and
answers are short. If a later turn is longer than the opening one, look hard at why.

- **A confirmation is the shortest turn in the conversation.** *"Yes, go ahead."* Not *"Yes, please
  set it to ₹80,000"* — the figure is already on the table, and repeating it is the same padding
  §14 rejects, just one turn later.
- **Never let the confirmation turn do the assistant's work.** *"Yes, I understand I'll lose some of
  the interest"* is not only unnatural, it is a **test defect**: on a case whose whole point is that
  the assistant must disclose a cost before asking for consent, a judge can read the customer's
  acknowledgement as evidence the cost was communicated when the assistant never said it. Both
  examples above were removed from live cases on 2026-08-07.
- **The legitimate exception is a resumption after a detour.** In `calculate_emi/context_switching.001`
  turn 3 is the longest — *"So coming back to the loan — what was the EMI again…"* — because
  re-opening a parked topic is exactly what a person does, and the case exists to test whether the
  model kept the sub-tasks apart. Growth that the axis is *about* is fine; growth that hedges against
  the script is not.

**Mechanical check when authoring a multi-turn case:** no turn after the first should repeat a value
already said, and the last turn should not be longer than the first unless the axis explains why.

**Corollary: supplying is not consenting.** A turn that only supplies the missing detail carries no
affirmation, so a model that fires the write straight off it fails S2 — correctly. A gather case
that ends in a write therefore needs **three turns**: request → supply → confirm. Two turns makes S2
unpassable and grades the scripting rather than the model (the write-happy-path rule).

---

## 15. `case_id`, folder and `tool` must agree

```
case_id  = <domain-prefix>.<tool-slug>.<axis-abbrev>.<nnn>
filename = <axis-abbrev>.<nnn>.json
```

**Default: `tool-slug` = folder name = the `tool` field.** All three name the same tool, and `tool`
means *the tool under test* — the one whose behaviour the case exists to measure.

Domain prefixes: `acct` → `accounts_and_transactions`, `cards` → `cards`, `calc` → `calculators`,
`csc` → `customer_service_and_catalog`, `depl` → `deposits_and_loans`.

Axis abbreviation is the axis name with its numeric prefix stripped (`4_not_enough_info` →
`not_enough_info`), with three irregulars where the axis name is longer than the filename:

| axis | abbrev |
|---|---|
| `1_happy_path` | `happy` |
| `2_bad_tool_response` | `bad_response` |
| `11_out_of_scope_refuse` | `out_of_scope` |

**Two carve-outs.**

- **`agentic/`** — the slug is literally `agentic`, the folder is `agentic/`, the filename is
  descriptive (`balance_gated.001.json`) rather than an axis abbreviation, and `tool` may be a
  `+`-joined composite. These carry `axis: general_agentic_multitool`, which has no numeric prefix.
- **`10_unseen_tools`** — slug and folder name the *entry* tool (where a reader would look for it);
  `tool` names the **new** tool the case introduces. The new tool is deliberately absent from
  `tools_exposed` — that absence is the axis.

**Why this is a rule and not a preference.** `case_id` is the join key for every stored result: run
directories are named by it and `score.json` records it, so renaming one orphans that history. The
folder and `tool` field appear in no result artifact. When the three disagree, the disagreement is
therefore always resolved *towards* `case_id`, by moving the file and correcting `tool`.

The cost of letting them drift is silent and was paid once: `calc.get_deposit_loan_rates.wrong_info.001`
sat in `calculators/calculate_emi/` with `tool: calculate_emi` while its `case_id` and `gold` both
said `get_deposit_loan_rates`. Coverage analysis aggregating by `tool` and prose citing `case_id`
then disagree about which tool has axis-7 coverage — an earlier audit contradicted itself on exactly
that point. **Aggregate coverage by `tool`, never by folder.**

Enforced by `tests/test_tool_contract.py::CaseIdMatchesFolderAndTool`. Extending the bank costs
nothing for a new tool folder, a new case, or a new axis whose filename is the axis minus its
prefix; a new domain or a new irregular abbreviation fails the test until it is recorded here and in
that test's tables — which is the point, since both are convention decisions.

---

## 16. A4 dependency semantics

`gold.dependencies` is a list of ordered `[A, B]` pairs meaning **A's FIRST call must land in an
EARLIER message than B's FIRST call** — same-turn batching fails by design, because a model cannot
derive an argument from an output it has not received yet.

Declare `gold.dependencies` ONLY when the dependent genuinely derives its args from the
prerequisite's output. Merely preferring a natural reading order is not a dependency: if the two
calls are independent (each callable from login_context alone), order them in `gold.tool_calls` and
**omit `dependencies`** — a transcript that batches both in one turn is fine for such cases.

`judge_advisory` control flag: add the non-`Q_*` entry `"behavior_advisory"` when the blessed path is
call-tools-then-clarify/decline — it disables the judge move-classification facet; substance is still
judged via the axis gate.
