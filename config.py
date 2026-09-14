"""
config.py — credentials and models for the scan.

Environment variables first, then a local file, so the same code runs on a
laptop and on a host with no home directory to read from.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import models as _models

_ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "serpapi": "SERPAPI_KEY",
}

_CRED_FILES = {s: Path.home() / f".{s}" / "credentials.json" for s in _ENV_KEYS}

GEMINI_MODEL = _models.consumer_model("gemini")


def _load_key(service: str) -> str | None:
    env = os.environ.get(_ENV_KEYS.get(service, ""))
    if env and env.strip():
        return env.strip()
    path = _CRED_FILES.get(service)
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text())["api_key"]
    except Exception:
        return None


def openai_key() -> str | None:
    return _load_key("openai")


def perplexity_key() -> str | None:
    return _load_key("perplexity")


def gemini_key() -> str | None:
    return _load_key("gemini")


def serpapi_key() -> str | None:
    return _load_key("serpapi")
