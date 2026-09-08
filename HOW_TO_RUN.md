# Running the eval

Two commands. `run` evaluates one model; `compare` puts finished runs side by side.

```bash
python -m harness.cli run --run-id my_model_v1
python -m harness.cli compare my_model_v1 other_model_v1
```

For *what* is being measured, see `indicbankbench/ARCHITECTURE.md`. Full flag reference is in
`--help` on either command.

---

## 1. Setup

You need **two OpenAI-compatible endpoints** — a *candidate* (the model under test) and a
*judge* (grades transcripts). The two things you change often live in the project-root `.env`:

```bash
CANDIDATE_BASE_URL=http://localhost:8000/v1
CANDIDATE_MODEL=your-model-id    # the id the endpoint serves — see /v1/models
JUDGE_BASE_URL=http://localhost:8005/v1
JUDGE_MODEL=/model
```

Everything else — temperature, `max_tokens`, timeouts, API-key env names — lives in
`indicbankbench/config/models.yaml`, because you rarely change it. **Temperature is deliberately not a CLI
flag**: set it there, and every run records the value it used in `run.json`.

Three requirements that will otherwise cost you a run:

1. **Serve the candidate with tool-calling enabled**, or every case 400s:
   `vllm serve <model> --enable-auto-tool-choice --tool-call-parser <parser>`
   (the parser must match the model's template — e.g. `pythonic` for Gemma).
2. **Use a judge from a different model family** than the candidate, to avoid self-leniency.
3. **The candidate's reasoning must not leak into `content`** — enable its reasoning parser.
   Leaked `<|channel>` tags corrupt both multi-turn context and the judge's input.

Before a full run, confirm both endpoints are up (`curl <base_url>/v1/models`) and that the
candidate's reply `content` is clean (no `<|channel>` tags).

---

## 2. Run it

```bash
python -m harness.cli run --run-id my_model_v1
```

That runs **the whole case bank three times** and writes a report. Useful variations:

```bash
--passes 1                        # one pass: plain pass rate, no consistency signal
--cases indicbankbench/case_bank/cards # scope to a domain (or a single case file) while debugging
--concurrency 6                   # cases in parallel; all models share one judge, so keep it modest
--fresh                           # ignore cached results and redo everything
```

**Scoring is strict.** With `--passes 3` a case counts only if it passes **all three** times.
What each number in the report means — and why strict matters — is in `indicbankbench/METRICS.md`.

**Runs resume automatically.** If a run dies halfway (endpoint fell over, you hit Ctrl-C),
re-run the same `--run-id` and it fills in only what's missing. Cached results are reused
**only** when the case file and the model/temperature are unchanged, so a stale result can
never be silently mixed into a fresh run.

> **Keep a comparison on one deployment.** Never mix results taken before and after a
> redeploy — a redeploy can silently change the served model itself. After any deployment
> change, re-run everything.

---

## 3. Reading the output

```
results/<run-id>/
  run.json      # model, endpoint, temperature, passes, case-bank hash, timestamp
  cases/pass1/<case_id>/{transcript.json, score.json}
  summary.json  # machine-readable aggregates
  REPORT.md     # the tables you actually read
```

What each number in `REPORT.md` means is defined in `indicbankbench/METRICS.md`; the failure codes
(`A1`, `R`, …) are decoded in `indicbankbench/CODES.md`. When a verdict looks wrong, read `transcript.json`
first — it's the ground truth, and it's how you tell a real model failure from a case that
gates on something it shouldn't.

---

## 4. Comparing models

Run each model (pointing `CANDIDATE_*` at its endpoint), then:

```bash
python -m harness.cli compare my_model_v1 other_model_v1 --out COMPARISON.md
```

Each run carries its own configuration, so `compare` **warns loudly** if you're mixing
temperatures, pass counts, or case-bank versions. Heed those warnings — a table built from
mixed configurations looks perfectly clean and means nothing.

---

## 5. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `400 ... "auto" tool choice requires --enable-auto-tool-choice` | Candidate served without tool-calling — relaunch with `--enable-auto-tool-choice --tool-call-parser <parser>`. |
| `!! INCOMPLETE RUN` at the end | Cases errored (usually an endpoint died mid-run). Fix the endpoint and re-run the same `--run-id` — it fills only the gaps. |
| `judge failed to produce valid output` | Judge returned empty/non-JSON — often a reasoning judge spending its whole budget thinking. Raise `judge_default.max_tokens` in `indicbankbench/config/models.yaml`; often transient. |
| Results look inverted — weak models beating strong ones | Suspect the candidate's **reasoning parser is off**: `<|channel>` tags leak into `content` and hit reasoning-heavy models hardest. Run the preflight in §1. |
| Scores shift a few cases between identical re-runs | Expected. Temp 0.0 pins token choices but **not** vLLM batch nondeterminism or the judge's sampling. Noise floor ≈ **±3 cases** per full run — don't read smaller deltas as signal. |
| A case ERRORs with `max_tool_iters (N) exceeded` | The model degenerated into a tool-call loop — a real failure mode. It writes no score and correctly counts as a non-pass. |
| Small model "fails everything" on write cases | Usually `A1`, not bad answers: it over-clarifies and never acts. Check where failures land in `REPORT.md`. |

---

## 6. Running via OpenRouter

If you don't serve the candidate or judge locally, the two scripts under `indicbankbench/scripts/` run
either side through OpenRouter, pinned to one provider/quantization so every verdict is graded by
the same model:

```bash
export OPENROUTER_API_KEY=sk-or-...

# Judge on OpenRouter, candidate local:
python indicbankbench/scripts/run_with_openrouter_judge.py \
  --provider <provider> --run-id my_run --passes 1

# Both candidate and judge on OpenRouter (all other args pass through to `harness.cli run`):
python indicbankbench/scripts/run_candidate_with_openrouter.py \
  --model <model-slug> --candidate-provider <provider> --candidate-quantizations bf16 \
  --judge-provider <provider> \
  --run-id my_run --passes 3 --candidate-label <label> --concurrency 6
```

Run each with `--list-providers` first to see which providers serve the model and at what
quantization. Always pin both sides — `allow_fallbacks: false` makes a mixed run fail loudly
rather than reporting numbers graded on a mixture of weights.

---

## Developing the harness itself

Neither of these touches a model, so both are free to run:

```bash
python -m unittest discover -s indicbankbench/tests        # unit tests (~0.3s, no deps beyond stdlib)
python -m harness.cli smoke <case.json> --stub <script>   # scripted candidate + no-LLM judge
```

The tests cover only the invariants that fail *silently* — a crashed case still counting as a
non-pass, the resume fingerprint invalidating on config change, and `compare`'s mismatch
warnings firing. Plain arithmetic is deliberately untested; a break there is obvious on sight.
