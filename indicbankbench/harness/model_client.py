"""OpenAI-compatible model client configured by models.yaml."""
import json
import logging
import os
import re
import time
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


class ProviderError(RuntimeError):
    """The endpoint answered with an error instead of a completion."""


# Error text sent as the reply, such as "HTTP 503: Service temporarily unavailable.".
_ERROR_TEXT_RE = re.compile(
    r"^\s*(?:HTTP(?:/\d(?:\.\d)?)?\s*[45]\d\d\b"
    r"|[45]\d\d\s+(?:Bad Gateway|Service Unavailable|Gateway Time-?out|Internal Server Error"
    r"|Too Many Requests)\b)",
    re.I,
)
_ERROR_TEXT_MAX_CHARS = 300


def _has_reasoning(message):
    for name in ("reasoning", "reasoning_content", "reasoning_details"):
        value = getattr(message, name, None)
        if isinstance(value, (str, list)) and value:
            return True
    return False


def _is_error_text(choice):
    """Whether the reply is a provider's error message."""
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str) or len(content) > _ERROR_TEXT_MAX_CHARS:
        return False
    if getattr(message, "tool_calls", None) or _has_reasoning(message):
        return False
    return bool(_ERROR_TEXT_RE.match(content))


def _provider_error(response):
    """Why this response is a provider error, or None."""
    error = getattr(response, "error", None)
    choices = getattr(response, "choices", None)
    if not choices:
        return f"provider returned an error: {error}" if error else "provider returned no choices"
    if getattr(choices[0], "finish_reason", None) == "error":
        return "provider finished the completion with finish_reason='error'"
    if error:
        message = getattr(choices[0], "message", None)
        if not (getattr(message, "content", None) or getattr(message, "tool_calls", None)):
            return f"provider returned an error: {error}"
        logging.warning("provider attached an error to a finished reply; the reply is kept: %s", error)
    if _is_error_text(response.choices[0]):
        return f"provider returned error text as the reply: {response.choices[0].message.content[:80]!r}"
    return None


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


def extra_body(profile_name):
    """Request options for a call: CANDIDATE_EXTRA_BODY or JUDGE_EXTRA_BODY."""
    if profile_name.startswith("candidate"):
        env_name = "CANDIDATE_EXTRA_BODY"
    elif profile_name.startswith(("judge", "report")):
        env_name = "JUDGE_EXTRA_BODY"
    else:
        return {}
    raw = os.environ.get(env_name)
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        logging.warning("%s is not valid JSON; ignored", env_name)
        return {}
    if not isinstance(body, dict):
        logging.warning("%s is not a JSON object; ignored", env_name)
        return {}
    return body


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
                "API key is not configured; using placeholder 'EMPTY'. "
                "This is fine for local vLLM but will fail against authenticated endpoints.",
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
        options = extra_body(profile_name)
        if options:
            kwargs["extra_body"] = options

        # Retry replies that carry an error. An empty reply is graded, not retried.
        attempts = int(profile.get("max_retries", 2)) + 1
        for attempt in range(attempts):
            response = client.chat.completions.create(**kwargs)
            problem = _provider_error(response)
            if problem is None:
                break
            logging.warning("%s (attempt %d/%d)", problem, attempt + 1, attempts)
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 30))
        else:
            raise ProviderError(f"{problem} after {attempts} attempt(s)")

        msg = response.choices[0].message
        tool_calls = None
        if msg.tool_calls:
            tool_calls = []
            for tc in msg.tool_calls:
                entry = {"id": tc.id, "type": "function", "function": {"name": tc.function.name}}
                raw_args = tc.function.arguments
                try:
                    args = json.loads(raw_args) if raw_args else {}
                    if args is None:
                        args = {}
                    if not isinstance(args, dict):
                        raise ValueError("arguments are not a JSON object")
                except ValueError:
                    # S3 fails the call; the raw text is kept in the transcript.
                    args = {}
                    entry["arguments_raw"] = raw_args
                entry["function"]["arguments"] = args
                tool_calls.append(entry)
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
