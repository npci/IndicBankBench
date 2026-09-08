# IndicBankBench — A Benchmark for Evaluating the Safety and Reliability of Language Models in Indian Retail Banking

[Dataset](https://huggingface.co/datasets/NPCI/IndicBankBench) · [Documentation](docs/ARCHITECTURE.md) · [Run guide](HOW_TO_RUN.md)

IndicBankBench is a benchmark for evaluating safe and reliable language models in Indian retail
banking. Across 799 synthetic multi-turn cases, it measures whether a model can ground its
decisions in the available customer context, use bank tools correctly, and handle incomplete,
risky, and out-of-scope requests.

## At a glance

| Grounded | Safe | Reliable |
| --- | --- | --- |
| Uses the customer and tool context provided | Takes only permitted actions and protects sensitive details | Resolves multi-turn requests with the right answer, clarification, or refusal |

## Quickstart

```bash
# Clone and install
git clone https://github.com/npci/IndicBankBench.git
cd IndicBankBench
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

# Copy the configuration template
cp .env.example .env

# Download the case data
hf download NPCI/IndicBankBench --repo-type dataset --local-dir ./data
export INDICBANKBENCH_DATA=./data

# Run an evaluation and compare two runs
python -m harness.cli run --run-id my_model_v1
python -m harness.cli compare my_model_v1 other_model_v1
```

Set the candidate and judge endpoints in `.env`. Full setup and flag reference:
[`HOW_TO_RUN.md`](HOW_TO_RUN.md).

## What it tests

- **Response behavior** — each case expects an *answer*, *clarification* for a genuinely missing
  detail, or *refusal* of an out-of-scope or nonexistent request. When clarification is required,
  the case continues after the customer provides the missing detail and evaluates whether the
  assistant completes the request.
- **Twenty primary axes** across five operational banking domains and one capability/refusal
  domain: 12 task and behavioral axes, including multi-tool workflows, and 8 capability/refusal
  axes. Coverage includes anomalous tool results, ambiguous intent, missing or conflicting
  information, long context, unseen tools, and out-of-scope requests.
- **Active safety requirements**: grounded identifiers and values, confirmation before writes,
  privacy protection, confirmed outcomes, and safe presentation of invalid values.

## How a case passes

```
S — Safety ──→ A — Actions ──→ R — Response ──→ PASS / FAIL
                                      │
                                      └── Q — Quality (reported separately; not a gate)
```

| Phase | It checks | Decided by | Effect on verdict |
|-------|----------|------------|--------|
| **S — Safety** | Grounded identifiers, confirmation before writes, and safe handling | deterministic code; resolver for ambiguous S2 confirmation | required for a pass |
| **A — Actions** | Correct tools, arguments, and order | deterministic code | required for a pass |
| **R — Response** | Whether answering, clarifying, or refusing was appropriate | LLM judge | required for a pass |
| **Q — Quality** | Grounding, completeness, and tone | LLM judge | reported separately; not a gate |

**Pass** iff every S, A, and R gate holds. Q never turns a failed case into a pass. The judge
never decides safety or tool correctness, so a lenient judge can't loosen those gates.
Details: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (model), [`docs/METRICS.md`](docs/METRICS.md) (exact formulas),
[`docs/CODES.md`](docs/CODES.md) (per-code lookup).

## Development

```bash
python -m unittest discover -s indicbankbench/tests     # no deps beyond stdlib
python -m harness.cli smoke <case.json> --stub <script>   # scripted candidate, no-LLM judge
```

To propose a new or corrected case, open a GitHub Issue as described in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for how to add cases and open pull requests. By participating you agree to
follow the [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). Report security issues per [`SECURITY.md`](SECURITY.md).

## License

Code is licensed under the MIT License — see [`LICENSE`](LICENSE). The separately downloaded case
data is licensed under [CC BY 4.0](https://huggingface.co/datasets/NPCI/IndicBankBench/blob/main/DATA_LICENSE.md).

## Disclaimer

IndicBankBench is synthetic, research-only data provided "as is." It contains no real customer
information and is not for live banking, regulatory, or financial decisions. See
[`DISCLAIMER.md`](DISCLAIMER.md) for details.
