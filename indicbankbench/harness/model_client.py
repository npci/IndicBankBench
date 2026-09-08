"""OpenAI-compatible model client configured by models.yaml."""
import json
import logging
import os
import re
from pathlib import Path

import yaml
from openai import OpenAI

from . import envtools

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = Path(__file__).resolve().parents[1]
MODELS_CONFIG_PATH = BENCH_ROOT / "config" / "models.yaml"
DOTENV_PATH = REPO_ROOT / ".env"

envtools.load_dotenv(DOTENV_PATH)

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-(.*?))?\}")


def _expand_env(value):
    if not isinstance(value, str):
        return value

    def repl(m):
        var_name, _, default = m.groups()
        return os.environ.get(var_name, default if default is not None else "")

    return _ENV_VAR_RE.sub(repl, value)


def _expand_env_num(value, default):
    """Resolve a numeric configuration value or default."""
    if value is None:
        return default
    expanded = _expand_env(value) if isinstance(value, str) else value
    if expanded in (None, ""):
        return default
    return expanded


class ModelClient:
    def __init__(self, config_path=None):
        path = Path(config_path) if config_path else MODELS_CONFIG_PATH
        with open(path) as f:
            raw = yaml.safe_load(f)
        self._profiles = raw["models"]
        self._clients = {}

    def _profile(self, name):
        if name not in self._profiles:
            raise KeyError(f"No model profile '{name}' in {MODELS_CONFIG_PATH}")
        p = self._profiles[name]
        is_judge = name.startswith("judge")
        return {
            "provider": p["provider"],
            "model": _expand_env(p["model"]),
            "api_key_env": p["api_key_env"],
            "base_url": _expand_env(p.get("base_url") or "") or None,
            "temperature": 0.0 if is_judge else p.get("temperature", 0.0),
            "max_tokens": p.get("max_tokens", 1024),
            "top_p": 1.0 if is_judge else p.get("top_p", 1.0),
            "seed": 42 if is_judge else p.get("seed"),
            "request_timeout": float(_expand_env_num(p.get("request_timeout"), 120)),
            "max_retries": int(_expand_env_num(p.get("max_retries"), 2)),
        }

    def _client_for(self, profile_name):
        if profile_name in self._clients:
            return self._clients[profile_name]
        profile = self._profile(profile_name)
        if profile["provider"] != "openai":
            raise NotImplementedError(f"provider '{profile['provider']}' not supported yet")
        api_key = os.environ.get(profile["api_key_env"]) or "EMPTY"
        if api_key == "EMPTY":
            logging.warning(
                "Environment variable %r is not set; using placeholder 'EMPTY'. "
                "This is fine for local vLLM but will fail against authenticated endpoints.",
                profile["api_key_env"],
            )
        client = OpenAI(
            api_key=api_key,
            base_url=profile["base_url"],
            timeout=profile["request_timeout"],
            max_retries=profile["max_retries"],
        )
        self._clients[profile_name] = (client, profile)
        return self._clients[profile_name]

    def chat(self, profile_name, messages, tools=None):
        """Send a chat completion and normalize the response."""
        client, profile = self._client_for(profile_name)
        kwargs = dict(
            model=profile["model"],
            messages=messages,
            temperature=profile["temperature"],
            max_tokens=profile["max_tokens"],
            top_p=profile["top_p"],
        )
        if profile["seed"] is not None:
            kwargs["seed"] = profile["seed"]
        if tools:
            kwargs["tools"] = tools
        extra_raw = os.environ.get("CANDIDATE_EXTRA_BODY")
        if extra_raw:
            try:
                kwargs["extra_body"] = json.loads(extra_raw)
            except json.JSONDecodeError:
                pass
        response = client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        tool_calls = None
        if msg.tool_calls:
            tool_calls = []
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": args},
                    }
                )
        # Preserve reasoning for later model turns.
        result = {"role": "assistant", "content": msg.content, "tool_calls": tool_calls}
        reasoning = getattr(msg, "reasoning", None)
        if reasoning:
            result["reasoning"] = reasoning
        reasoning_details = getattr(msg, "reasoning_details", None)
        if reasoning_details:
            result["reasoning_details"] = reasoning_details
        return result

    def raw_text(self, profile_name, messages):
        """Return a plain-text completion."""
        result = self.chat(profile_name, messages, tools=None)
        return result["content"] or ""
