#!/usr/bin/env python3
"""Run a normal eval with the CANDIDATE served by OpenRouter, pinned to one provider at one
quantization.

Sibling to run_with_openrouter_judge.py; same rationale and "fail loudly on fallback" contract,
applied to the candidate side instead of the judge. Composes with the judge script: this script
always routes the judge through OpenRouter too, reusing its OpenRouterJudge transport.

The candidate client is substituted for the one `cli._lazy_client()` hands out, reproducing
`ModelClient.chat`'s normalized return shape (tool_calls[i].function.arguments as a Python dict).

USAGE
    export OPENROUTER_API_KEY=sk-or-...

    # 1. See which providers serve the model, and at what quantization
    python indicbankbench/scripts/run_candidate_with_openrouter.py --list-providers --model google/gemma-4-26b-a4b-it

    # 2. Run, with BOTH candidate and judge pinned (all other args pass through to `harness.cli run`)
    python indicbankbench/scripts/run_candidate_with_openrouter.py \
        --model google/gemma-4-26b-a4b-it --candidate-provider NextBit \
        --judge-provider Novita \
        --run-id my_run --passes 3 \
        --candidate-label gemma-26b
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

SCRIPTS_DIR = Path(__file__).resolve().parent
BENCH = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))  # for `import run_with_openrouter_judge` below
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(BENCH.parent))

import run_with_openrouter_judge as judge_mod  # noqa: E402  (sys.path set up above)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model}/endpoints"

# Reasoning presets per model family. Each is the exact OpenRouter `reasoning` object to send
# in the request body, chosen to get the best out of that family. Determined from the live
# /api/v1/models `reasoning` metadata (default_enabled / supported_efforts / mandatory) plus a
# smoke probe against the pinned provider. Hardcoded to always use the best reasoning preset.
#
#   google/gemma-4-31b-it  -> Gemini 2.5-style (thinkingBudget); no supported_efforts exposed,
#                             so toggle it on and let the provider size the budget. Verified
#                             live on Venice: `{"enabled": true}` returns reasoning tokens.
#   qwen / deepseek / muse / mistral / nemotron-3-super -> expose supported_efforts; pick the
#                             highest listed (xhigh/max/high/medium).
#   nvidia/nemotron-3.5-lightning -> reasoning {"mandatory": false} with no efforts = no support;
#                             deliberately absent from this table, so nothing is sent.
REASONING_PRESETS = {
    "google/":                    {"enabled": True},
    "qwen/qwen3":                 {"effort": "xhigh"},
    "deepseek/":                  {"effort": "max"},
    "meta/muse":                  {"effort": "xhigh"},
    "mistralai/":                 {"effort": "high"},
    "nvidia/nemotron-3-super":    {"effort": "medium"},
    "z-ai/":                      {"effort": "low"},
}


def reasoning_for(model, effort=None):
    """Return the reasoning body for a model, or None if its family doesn't support it.
    `effort` overrides the preset, for models that expose a supported_efforts list."""
    for prefix, preset in REASONING_PRESETS.items():
        if model.startswith(prefix):
            body = dict(preset)
            if effort and "effort" in body:
                body["effort"] = effort
            return body
    return {"effort": effort} if effort else None

# Candidate temperature/max_tokens/top_p still come from config/models.yaml's candidate_default
# profile (read once at startup, below) -- only the TRANSPORT (which endpoint, which provider,
# whether tool schemas reach it) is replaced. Keeping the sampling config in one place (the yaml)
# means a temperature sweep still only requires editing models.yaml, not this script.


def list_providers(model, api_key, quantizations):
    """Same listing as judge_mod.list_providers, but eligibility is checked against
    `quantizations` (the CANDIDATE's pin) rather than judge_mod's hardcoded fp8 -- the judge and
    candidate are pinned to different quantizations in general (e.g. the judge to fp8, the
    26b candidate to bf16, since that is what NextBit actually serves for it)."""
    data = judge_mod._get(judge_mod.ENDPOINTS_URL.format(model=model), api_key).get("data") or {}
    endpoints = data.get("endpoints") or []
    if not endpoints:
        print(f"No endpoints returned for {model!r} — check the model slug.", file=sys.stderr)
        return 1
    print(f"\n{model}  (pinning quantizations={quantizations})\n")
    print(f"  {'provider':28} {'quant':10} {'context':>9}   eligible")
    print(f"  {'-'*28} {'-'*10} {'-'*9}   {'-'*8}")
    eligible = []
    for e in sorted(endpoints, key=lambda x: (x.get("quantization") or "", x.get("provider_name") or "")):
        name = e.get("provider_name") or e.get("name") or "?"
        quant = e.get("quantization") or "unknown"
        ok = quant in quantizations
        if ok and name not in eligible:
            eligible.append(name)
        print(f"  {name:28} {quant:10} {str(e.get('context_length') or '?'):>9}   {'YES' if ok else ''}")
    print(f"\nEligible at {quantizations}: {eligible or 'NONE'}")
    if eligible:
        print(f"\n  python {Path(__file__).name} --model {model} --candidate-provider {eligible[0]} "
              f"--candidate-quantizations {','.join(quantizations)} --run-id <id>")
    return 0


class OpenRouterCandidate:
    """Candidate transport for one pinned provider at one quantization. Mirrors
    run_with_openrouter_judge.OpenRouterJudge's retry/backoff/pin-recording contract, but speaks
    chat() (with tool schemas), not raw_text() -- the candidate calls tools, the judge doesn't.
    """

    def __init__(self, model, providers, quantizations, api_key, temperature, max_tokens, top_p, seed=None, effort=None):
        self.model, self.providers, self.quantizations = model, providers, quantizations
        self._api_key = api_key  # stored with underscore to signal internal use only
        self.temperature, self.max_tokens, self.top_p, self.seed = temperature, max_tokens, top_p, seed
        self.effort = effort
        self.calls = []
        self.throttled = 0

    def chat(self, profile_name, messages, tools=None):
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "provider": {
                "order": list(self.providers),
                "quantizations": self.quantizations,
                # Same reasoning as the judge script: a busy pinned provider must fail the call,
                # not silently hand it to a provider outside the list (possibly a different
                # quantization of the candidate's weights, which would make that one case's
                # grading incomparable to every other case in the same run).
                "allow_fallbacks": False,
            },
        }
        if self.seed is not None:
            body["seed"] = self.seed
        reasoning = reasoning_for(self.model, self.effort)
        if reasoning:
            body["reasoning"] = reasoning
        if tools:
            body["tools"] = tools

        req = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
        )
        t0 = time.time()
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    payload = json.load(r)
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:400]
                if e.code == 429 and attempt < 5:
                    self.throttled += 1
                    time.sleep(min(60, 4 * (2 ** attempt)))
                    continue
                self.calls.append({"error": f"HTTP {e.code}", "detail": detail})
                hint = ""
                if e.code == 404:
                    hint = (f"\n  Likely cause: providers {self.providers} serve no "
                            f"{'/'.join(self.quantizations)} endpoint for {self.model!r}. "
                            f"Re-run with --list-providers to pick ones that do.")
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
        })
        choices = payload.get("choices") or []
        if not choices:
            return {"role": "assistant", "content": "", "tool_calls": None}
        msg = choices[0].get("message", {}) or {}

        tool_calls = None
        raw_tool_calls = msg.get("tool_calls")
        if raw_tool_calls:
            tool_calls = []
            for tc in raw_tool_calls:
                fn = tc.get("function", {}) or {}
                raw_args = fn.get("arguments")
                # Normalize to a dict here, matching ModelClient.chat()'s own json.loads --
                # runner.py's internal transcript convention expects a dict (see runner.py's
                # module docstring); it is re-serialized to a string only when sent back over
                # the wire (runner._to_wire).
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({
                    "id": tc.get("id"),
                    "type": "function",
                    "function": {"name": fn.get("name"), "arguments": args},
                })
        result = {"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls}
        # Preserve reasoning so runner.py can echo it back across multi-turn tool calls -- the
        # model needs its thinking thread to continue after a tool result. Mirrors
        # model_client.ModelClient.chat()'s own reasoning/reasoning_details extraction; the
        # judge never sees these fields (judge.py's _render_transcript reads content + tool_calls).
        if msg.get("reasoning"):
            result["reasoning"] = msg["reasoning"]
        if msg.get("reasoning_details"):
            result["reasoning_details"] = msg["reasoning_details"]
        return result


class FullOpenRouterRoutingClient:
    """Wraps the real ModelClient; routes candidate chat() calls to OpenRouterCandidate and
    judge raw_text() calls to OpenRouterJudge. Both concerns live in one object because
    cli._lazy_client() hands out exactly one client for both roles (see judge_mod's own
    JudgeRoutingClient docstring)."""

    def __init__(self, real_client, candidate_transport, judge_transport, candidate_profiles):
        self._real = real_client
        self._candidate = candidate_transport
        self._judge = judge_transport
        self._candidate_profiles = candidate_profiles

    def chat(self, profile_name, messages, tools=None):
        if profile_name in self._candidate_profiles:
            return self._candidate.chat(profile_name, messages, tools=tools)
        return self._real.chat(profile_name, messages, tools=tools)

    def raw_text(self, profile_name, messages):
        if profile_name in judge_mod.JUDGE_PROFILES:
            return self._judge.raw_text(profile_name, messages)
        return self._real.raw_text(profile_name, messages)

    def __getattr__(self, name):
        return getattr(self._real, name)


CANDIDATE_PROFILES = {"candidate_default"}


def install(candidate_model, candidate_providers, candidate_quantizations, candidate_effort,
            judge_model, judge_provider, judge_quantizations, api_key):
    from indicbankbench.harness import cli
    from indicbankbench.harness.model_client import ModelClient

    # Sampling params still come from config/models.yaml -- read once via a real client so a
    # temperature/max_tokens change there is still a one-file edit, not a flag on this script.
    probe = ModelClient()
    cand_profile = probe._profile("candidate_default")  # noqa: SLF001 - see run_with_openrouter_judge's own use of this
    judge_profile = probe._profile("judge_default")  # noqa: SLF001

    candidate_transport = OpenRouterCandidate(
        candidate_model, candidate_providers, candidate_quantizations, api_key,
        temperature=cand_profile["temperature"], max_tokens=cand_profile["max_tokens"],
        top_p=cand_profile["top_p"], seed=cand_profile.get("seed"), effort=candidate_effort,
    )
    judge_transport = judge_mod.OpenRouterJudge(judge_model, judge_provider, api_key, seed=judge_profile.get("seed"))

    holder = {}

    def getter():
        if "client" not in holder:
            holder["client"] = FullOpenRouterRoutingClient(
                ModelClient(), candidate_transport, judge_transport, CANDIDATE_PROFILES
            )
        return holder["client"]

    cli._lazy_client = lambda: getter

    _orig_run_config = cli._run_config

    def _run_config():
        cfg = _orig_run_config()
        cfg["candidate_model"] = candidate_model
        cfg["candidate_base_url"] = OPENROUTER_URL
        cfg["candidate_provider"] = list(candidate_providers)
        cfg["candidate_quantizations"] = list(candidate_quantizations)
        cfg["candidate_reasoning"] = reasoning_for(candidate_model, candidate_effort)
        cfg["judge_model"] = f"openrouter:{judge_model}"
        cfg["judge_base_url"] = judge_mod.OPENROUTER_URL
        cfg["judge_provider"] = judge_provider
        cfg["judge_quantizations"] = list(judge_mod.QUANTIZATIONS)
        cfg["judge_reasoning_effort"] = judge_mod.REASONING_EFFORT
        return cfg

    cli._run_config = _run_config
    return candidate_transport, judge_transport


def report(candidate_transport, candidate_providers, judge_transport, judge_provider):
    rc = 0
    ok = [c for c in candidate_transport.calls if "error" not in c]
    print(f"\n--- candidate routing ({candidate_transport.model} @ "
          f"{candidate_transport.quantizations[0]}) ---")
    if not ok:
        print("  No successful candidate calls recorded.", file=sys.stderr)
        rc = 1
    else:
        served = Counter(c.get("provider") or "?" for c in ok)
        prompt = sum(c.get("prompt_tokens") or 0 for c in ok)
        comp = sum(c.get("completion_tokens") or 0 for c in ok)
        print(f"  calls          {len(ok)}   (429 backoffs: {candidate_transport.throttled})")
        print(f"  served by      {dict(served)}")
        print(f"  prompt tokens  {prompt:,}   output tokens {comp:,}")
        strays = {p: n for p, n in served.items() if p not in candidate_providers}
        if strays:
            print(f"\n  PROVIDER DRIFT (candidate) — {strays} served calls outside the pinned list\n"
                  f"  {candidate_providers}. Treat this run as mixed and re-run before quoting\n"
                  f"  its numbers.", file=sys.stderr)
            rc = 1
        else:
            print(f"  all {len(ok)} calls served by the pinned list.")

    judge_rc = judge_mod.report(judge_transport, judge_provider)
    return rc or judge_rc


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-providers", action="store_true",
                    help="list endpoints and their quantizations for --model, then exit")
    ap.add_argument("--model", required=True, help="candidate model slug, e.g. google/gemma-4-26b-a4b-it")
    ap.add_argument("--candidate-provider", default=None,
                    help="provider name(s) to pin for the candidate, comma-separated for failover (e.g. BaseTen,Novita)")
    ap.add_argument("--candidate-quantizations", default="bf16",
                    help="comma-separated quantizations to pin for the candidate (default: bf16)")
    ap.add_argument("--candidate-reasoning-effort", default=None,
                    help="override the candidate reasoning effort (e.g. low / high / max)")
    ap.add_argument("--judge-model", default=judge_mod.JUDGE_MODEL)
    ap.add_argument("--judge-provider", default="Novita", help="exact provider name to pin for the judge")
    args, passthrough = ap.parse_known_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY is not set.", file=sys.stderr)
        return 2

    if args.list_providers:
        return list_providers(args.model, api_key, args.candidate_quantizations.split(","))
    if not args.candidate_provider:
        print("--candidate-provider is required (run --list-providers first).", file=sys.stderr)
        return 2

    candidate_providers = [p.strip() for p in args.candidate_provider.split(",") if p.strip()]
    candidate_transport, judge_transport = install(
        args.model, candidate_providers, args.candidate_quantizations.split(","),
        args.candidate_reasoning_effort,
        args.judge_model, args.judge_provider, judge_mod.QUANTIZATIONS, api_key,
    )

    from indicbankbench.harness import cli
    sys.argv = ["cli", "run"] + passthrough
    try:
        cli.main()
    finally:
        rc = report(candidate_transport, candidate_providers, judge_transport, args.judge_provider)
    return rc


if __name__ == "__main__":
    sys.exit(main())
