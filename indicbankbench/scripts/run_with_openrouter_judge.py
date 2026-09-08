#!/usr/bin/env python3
"""Run a normal local eval with the JUDGE served by OpenRouter, pinned to one provider at one
quantization.

OpenRouter load-balances a model across providers serving different quantizations; pinning the
provider (via `provider.quantizations` + `allow_fallbacks: false`) keeps every verdict graded by
the same model. Only the judge is affected — the local candidate is untouched.

The pin is applied by intercepting `ModelClient.raw_text()`, the single method
`judge.call_judge()` uses to reach the network, so no harness change is needed.

USAGE
    export OPENROUTER_API_KEY=sk-or-...

    # 1. See which providers serve the model, and at what quantization
    python indicbankbench/scripts/run_with_openrouter_judge.py --list-providers

    # 2. Run, with the judge pinned (all other args pass through to `harness.cli run`)
    python indicbankbench/scripts/run_with_openrouter_judge.py --provider Novita \
        --run-id e4b_glm_20260816 --passes 1
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

BENCH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(BENCH.parent))

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model}/endpoints"

JUDGE_MODEL = "z-ai/glm-5.2"

# Accepted by OpenRouter: int4, int8, fp4, fp6, fp8, fp16, bf16, fp32, unknown.
#
# Pinned to a SINGLE value, not a "8-bit or better" list. Allowing fp8+bf16+fp16 would raise
# average precision but let precision vary between calls, and for grading, CONSISTENCY beats
# peak accuracy: a judge that scores case 40 at bf16 and case 41 at fp8 makes those two
# verdicts incomparable, which is worse than scoring both slightly less well. Widen this only
# if you also pin one provider, so precision is still fixed for the run.
QUANTIZATIONS = ["fp8"]

# The judge grades; it does not converse. These are deliberately independent of models.yaml's
# judge_default, which points at a self-hosted vLLM deployment this replaces.
TEMPERATURE = 0.0
TOP_P = 1.0
MAX_TOKENS = 8000        # reasoning models spend most of this thinking before emitting JSON
REASONING_EFFORT = "medium"
JUDGE_PROFILES = {"judge_default"}   # profiles routed to OpenRouter; all others pass through


def _get(url, api_key, timeout=60):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def list_providers(model, api_key):
    """Print every endpoint serving `model`, with its quantization — the input to --provider.

    Queried live rather than hardcoded: providers add and drop models continuously, and a
    stale table here would send the run at a provider that no longer serves fp8.
    """
    data = _get(ENDPOINTS_URL.format(model=model), api_key).get("data") or {}
    endpoints = data.get("endpoints") or []
    if not endpoints:
        print(f"No endpoints returned for {model!r} — check the model slug.", file=sys.stderr)
        return 1
    print(f"\n{model}\n")
    print(f"  {'provider':28} {'quant':10} {'context':>9}   eligible")
    print(f"  {'-'*28} {'-'*10} {'-'*9}   {'-'*8}")
    eligible = []
    for e in sorted(endpoints, key=lambda x: (x.get("quantization") or "", x.get("provider_name") or "")):
        name = e.get("provider_name") or e.get("name") or "?"
        quant = e.get("quantization") or "unknown"
        # A provider can list several endpoints for one model (different context windows), so
        # dedupe — otherwise the suggested --provider list repeats names.
        ok = quant in QUANTIZATIONS
        if ok and name not in eligible:
            eligible.append(name)
        print(f"  {name:28} {quant:10} {str(e.get('context_length') or '?'):>9}   {'YES' if ok else ''}")
    print(f"\nEligible at {QUANTIZATIONS}: {eligible or 'NONE'}")
    if not eligible:
        print("  No provider serves this model at the pinned quantization. Either widen\n"
              "  QUANTIZATIONS (and read the comment above it first) or pick another judge.")
    else:
        print(f"\n  python {Path(__file__).name} --provider {eligible[0]} --run-id <id>")
    return 0


class OpenRouterJudge:
    """Judge transport for one pinned provider at one quantization.

    Records the serving provider and quantization on every call. Recorded, not assumed: a pin
    that silently falls back is invisible in the verdicts themselves, so the evidence has to be
    collected per call or the guarantee is only a comment.
    """

    def __init__(self, model, provider, api_key, seed=None):
        self.model, self.provider, self.api_key, self.seed = model, provider, api_key, seed
        self.calls = []
        self.throttled = 0

    def raw_text(self, profile_name, messages):
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": TEMPERATURE,
            "top_p": TOP_P,
            "max_tokens": MAX_TOKENS,
            "reasoning": {"effort": REASONING_EFFORT},
            "provider": {
                "order": [self.provider],
                "quantizations": QUANTIZATIONS,
                # Without this, a busy pinned provider is silently replaced by whoever is free
                # — including a 4-bit endpoint. Failing the call is the correct outcome: a
                # missing verdict is visible, a differently-quantized one is not.
                "allow_fallbacks": False,
            },
        }
        if self.seed is not None:
            body["seed"] = self.seed
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        t0 = time.time()
        # 429 is provider capacity, not a judge or schema defect. Backing off keeps a
        # rate-limited run from looking like a broken judge.
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    payload = json.load(r)
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:400]
                if e.code == 429 and attempt < 5:
                    self.throttled += 1
                    time.sleep(min(60, 4 * (2 ** attempt)))
                    continue
                self.calls.append({"error": f"HTTP {e.code}", "detail": detail})
                # 404 here is almost never a bad model slug — the pinned provider simply serves
                # nothing matching QUANTIZATIONS (verified: pinning an fp4-only provider while
                # demanding fp8 returns exactly this). OpenRouter's own message says only "No
                # endpoints found", which sends you looking at the model name instead.
                hint = ""
                if e.code == 404:
                    hint = (f"\n  Likely cause: provider {self.provider!r} serves no "
                            f"{'/'.join(QUANTIZATIONS)} endpoint for {self.model!r}. "
                            f"Re-run with --list-providers to pick one that does.")
                raise RuntimeError(f"OpenRouter HTTP {e.code}: {detail}{hint}") from e
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                if attempt < 5:
                    self.throttled += 1
                    time.sleep(min(60, 4 * (2 ** attempt)))
                    continue
                self.calls.append({"error": type(e).__name__, "detail": str(e)[:200]})
                raise
        usage = payload.get("usage") or {}
        self.calls.append({
            "latency_s": round(time.time() - t0, 2),
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        })
        choices = payload.get("choices") or []
        return (choices[0].get("message", {}).get("content") if choices else "") or ""


class JudgeRoutingClient:
    """Wraps the real ModelClient; sends only judge `raw_text` calls to OpenRouter.

    `cli._lazy_client()` hands ONE client to both the candidate and the judge — the candidate
    via `chat()`, the judge via `raw_text()`. Intercepting `raw_text` for judge profiles and
    delegating everything else leaves local vLLM inference for the model under test untouched.
    `__getattr__` forwards any method added to ModelClient later, so this does not silently
    become a stale copy of its interface.
    """

    def __init__(self, real_client, judge_transport):
        self._real = real_client
        self._judge = judge_transport

    def raw_text(self, profile_name, messages):
        if profile_name in JUDGE_PROFILES:
            return self._judge.raw_text(profile_name, messages)
        return self._real.raw_text(profile_name, messages)

    def __getattr__(self, name):
        return getattr(self._real, name)


def install(provider, api_key, model, seed=None):
    """Patch cli's client getter so the judge routes through OpenRouter. Returns the transport."""
    from indicbankbench.harness import cli
    from indicbankbench.harness.model_client import ModelClient

    transport = OpenRouterJudge(model, provider, api_key, seed=seed)
    holder = {}

    def getter():
        if "client" not in holder:
            holder["client"] = JudgeRoutingClient(ModelClient(), transport)
        return holder["client"]

    cli._lazy_client = lambda: getter

    # run.json exists to record "what was actually sent" (cli._run_config's own words), but
    # it reads judge_default straight from models.yaml — which this script has just bypassed.
    # Unpatched it names the self-hosted vLLM judge that never ran, and every verdict in the
    # run is misattributed.
    #
    # This also repairs the resume cache. _case_fingerprint() hashes judge_model, so with the
    # profile value ("/model") reported for every judge, switching judges left the fingerprint
    # unchanged and a resumed run would silently reuse verdicts from a DIFFERENT judge.
    # Reporting the real judge makes that invalidate correctly.
    _orig_run_config = cli._run_config

    def _run_config():
        cfg = _orig_run_config()
        cfg["judge_model"] = f"openrouter:{model}"
        cfg["judge_base_url"] = OPENROUTER_URL
        cfg["judge_provider"] = provider
        cfg["judge_quantizations"] = list(QUANTIZATIONS)
        cfg["judge_reasoning_effort"] = REASONING_EFFORT
        return cfg

    cli._run_config = _run_config
    return transport


def report(transport, provider):
    """Summarise routing and cost; fail the run if the pin did not hold on every call."""
    ok = [c for c in transport.calls if "error" not in c]
    if not ok:
        print("\nNo successful judge calls recorded.", file=sys.stderr)
        return 1
    served = Counter(c.get("provider") or "?" for c in ok)
    comp = sum(c.get("completion_tokens") or 0 for c in ok)
    reas = sum(c.get("reasoning_tokens") or 0 for c in ok)
    prompt = sum(c.get("prompt_tokens") or 0 for c in ok)
    print(f"\n--- judge routing ({transport.model} @ {QUANTIZATIONS[0]}) ---")
    print(f"  calls          {len(ok)}   (429 backoffs: {transport.throttled})")
    print(f"  served by      {dict(served)}")
    print(f"  prompt tokens  {prompt:,}")
    print(f"  output tokens  {comp:,}  (of which reasoning: {reas:,})")

    strays = {p: n for p, n in served.items() if p != provider}
    if strays:
        print(f"\n  PIN BROKEN — {strays} served calls despite allow_fallbacks=false.\n"
              f"  These verdicts were graded by a different deployment, possibly at a different\n"
              f"  quantization. Treat this run as mixed and re-run before quoting its numbers.",
              file=sys.stderr)
        return 1
    print(f"  pin held on all {len(ok)} calls.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-providers", action="store_true",
                    help="list endpoints and their quantizations, then exit")
    ap.add_argument("--provider", help="exact provider name to pin (see --list-providers)")
    ap.add_argument("--model", default=JUDGE_MODEL, help=f"judge model slug (default {JUDGE_MODEL})")
    args, passthrough = ap.parse_known_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY is not set.", file=sys.stderr)
        return 2

    if args.list_providers:
        return list_providers(args.model, api_key)
    if not args.provider:
        print("--provider is required (run --list-providers first).", file=sys.stderr)
        return 2

    from indicbankbench.harness.model_client import ModelClient as _MC
    judge_seed = _MC()._profile("judge_default").get("seed")
    transport = install(args.provider, api_key, args.model, seed=judge_seed)

    from indicbankbench.harness import cli
    sys.argv = ["cli", "run"] + passthrough
    try:
        cli.main()
    finally:
        # Reported even on failure: a partial run's routing evidence is still worth having,
        # and a crash midway is exactly when you want to know whether the pin was holding.
        rc = report(transport, args.provider)
    return rc


if __name__ == "__main__":
    sys.exit(main())
