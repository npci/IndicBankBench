"""Zero-dependency .env loading (no python-dotenv dependency). Loads KEY=VALUE
lines from the project-root .env into os.environ; a value already set in the real
shell always wins (existing env vars are never overridden).
"""
import os


def load_dotenv(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
