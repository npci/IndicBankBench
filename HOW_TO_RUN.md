# Run IndicBankBench

`run` evaluates one model; `compare` puts completed runs side by side.

```bash
python -m harness.cli run --run-id my_model_v1
python -m harness.cli compare my_model_v1 other_model_v1
```

For *what* is being measured, see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Full flag reference is in
`--help` on either command.

---

## 1. Setup

### 1.1 Clone and install the harness

Install the harness from a source checkout:

```bash
git clone https://github.com/npci/IndicBankBench.git
cd IndicBankBench
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

### 1.2 Configure endpoints and download the case bank

You need **two OpenAI-compatible endpoints**: a *candidate* (the model under test) and a *judge*
(grades transcripts). Set them in the project-root `.env`:

```bash
CANDIDATE_BASE_URL=http://localhost:8000/v1
CANDIDATE_MODEL=your-model-id    # the id the endpoint serves — see /v1/models
CANDIDATE_API_KEY=EMPTY
JUDGE_BASE_URL=http://localhost:8005/v1
JUDGE_MODEL=/model
JUDGE_API_KEY=EMPTY
```

Use `EMPTY` only for local unauthenticated vLLM. For hosted endpoints, use real keys; if both
roles share an account, the two values may be identical.

Set temperature, `max_tokens`, and timeouts in `indicbankbench/config/models.yaml`. Temperature is
not a CLI flag; every run records the configured value in `run.json`.

Optional `.env` settings:

- `MODEL_MAX_RETRIES` — integer retry count for both endpoints (default: `2`).
- `CANDIDATE_EXTRA_BODY` — JSON object passed to the candidate endpoint request; use only when
  required by that provider.

You also need the case bank, which ships as a dataset rather than in this repository:

```bash
hf download NPCI/IndicBankBench --repo-type dataset --local-dir ./data
export INDICBANKBENCH_DATA=./data      # or pass --cases <path> per run
```

Before running, confirm the following:

1. **Serve the candidate with tool-calling enabled**, or every case 400s:
   `vllm serve <model> --enable-auto-tool-choice --tool-call-parser <parser>`
   (the parser must match the model's template — e.g. `pythonic` for Gemma).
2. **Keep the judge configuration fixed within a comparison**, including the model and decoding settings.
3. **The candidate's reasoning must not leak into `content`** — enable its reasoning parser.
   Leaked `<|channel>` tags corrupt both multi-turn context and the judge's input.

Before a full run, confirm both endpoints are up (`curl <base_url>/v1/models`) and that the
candidate's reply `content` is clean (no `<|channel>` tags).

---

## 2. Run it

```bash
python -m harness.cli run --run-id my_model_v1
```

By default, this evaluates the complete case bank three times and writes a report. Common options:

```bash
--passes 1                        # one pass: plain pass rate, no consistency signal
--cases data/case_bank/cards           # scope to a domain (or a single case file) while debugging
--concurrency 6                   # cases in parallel; all models share one judge, so keep it modest
--fresh                           # ignore cached results and redo everything
```

With `--passes 3`, a case counts only if it passes **all three** times. See
[`docs/METRICS.md`](docs/METRICS.md) for the reported metrics and strict-pass definition.

Runs resume automatically. If a run stops before completion, re-run the same `--run-id` to fill
only the missing cases. Cached results are reused only when the case file and model/temperature
are unchanged.

---

## 3. Reading the output

```
results/<run-id>/
  run.json      # model, endpoint, temperature, passes, case-bank hash, timestamp
  cases/pass1/<case_id>/{transcript.json, score.json}
  summary.json  # machine-readable aggregates
  REPORT.md     # human-readable summary
```

[`docs/METRICS.md`](docs/METRICS.md) defines the values in `REPORT.md`, and
[`docs/CODES.md`](docs/CODES.md) defines failure codes such as `A1` and `R`. To investigate a
verdict, start with `transcript.json`; it records the model interaction used for scoring.

---

## 4. Comparing models

Run each model (pointing `CANDIDATE_*` at its endpoint), then:

```bash
python -m harness.cli compare my_model_v1 other_model_v1 --out COMPARISON.md
```

Each run records its configuration. `compare` warns when temperatures, pass counts, or
case-bank versions differ; align those settings before drawing conclusions from the comparison.
