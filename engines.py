"""
engines.py — the three engine callers, with nothing else attached.

Lifted out of monitor/prompt_tester.py, which imports db and clients at module
level. The scan path never calls anything that needs either, but a module-level
import runs regardless - so importing prompt_tester dragged the whole client
registry along with it. That made the scan impossible to deploy anywhere the
client list should not go.

These functions touch no local state. Same code, same behaviour, no registry.
"""

from __future__ import annotations

import json
import logging
import re
import time

import requests

from config import openai_key, perplexity_key, gemini_key
import models

log = logging.getLogger(__name__)

RATE_LIMITS = {
    "chatgpt":    3.5,   # gpt-5-search-api has a low RPM cap; pace + retry (below) avoid drops
    "perplexity": 1.5,
    "gemini":     4.5,   # google-search grounding; stay under free-tier RPM
}

MODELS = {e: models.consumer_model(e) for e in ("chatgpt", "perplexity", "gemini")}

TOKEN_COSTS = {
    "chatgpt":    {"input": 1.25, "output": 10.00},
    "perplexity": {"input": 3.00, "output": 15.00},
    "gemini":     {"input": 0.30, "output": 2.50},
}

SYSTEM_PROMPT = (
    "You are a helpful research assistant. Answer the user's question thoroughly "
    "and naturally, the way you would for a real person. When specific businesses, "
    "brands, products, or services are genuinely the best answer, name them."
)

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

_PERMANENT_429 = ("insufficient_quota", "credit_balance_exhausted", "billing_not_active")


def _query_chatgpt(prompt_text: str, location: dict | None = None) -> dict:
    """
    Query ChatGPT with live web search (gpt-5-search-api) — mirrors what a real
    ChatGPT user with browsing sees, not the model's training memory. Search
    models don't accept temperature/max_tokens, so we omit them. Citations come
    back as message.annotations (url_citation), plus any inline URLs.
    """
    key = openai_key()
    if not key:
        return {"error": "no_key"}

    body = {
        "model": MODELS["chatgpt"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt_text},
        ],
    }
    if location:
        body["web_search_options"] = {"user_location": {"type": "approximate", "approximate": location}}
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    resp.raise_for_status()
    data    = resp.json()
    message = data["choices"][0]["message"]
    text    = message.get("content") or ""
    sources = [a["url_citation"]["url"]
               for a in (message.get("annotations") or [])
               if a.get("type") == "url_citation" and a.get("url_citation", {}).get("url")]
    sources += re.findall(r'https?://[^\s\)\"\']+', text)
    usage   = data.get("usage", {})
    return {
        "text":            text,
        "sources":         list(dict.fromkeys(sources)),
        "tokens_input":    usage.get("prompt_tokens", 0),
        "tokens_output":   usage.get("completion_tokens", 0),
    }


def _query_perplexity(prompt_text: str, location: dict | None = None) -> dict:
    """Query Perplexity sonar-pro. Returns response, citations, and token usage."""
    key = perplexity_key()
    if not key:
        return {"error": "no_key"}

    body = {
        "model": MODELS["perplexity"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt_text},
        ],
        "max_tokens": 800,
        "temperature": 0.3,
        "return_citations": True,
    }
    if location:
        body["web_search_options"] = {"user_location": location}
    resp = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    data    = resp.json()
    text    = data["choices"][0]["message"]["content"]
    sources = data.get("citations", [])
    usage   = data.get("usage", {})
    return {
        "text":          text,
        "sources":       sources,
        "tokens_input":  usage.get("prompt_tokens", 0),
        "tokens_output": usage.get("completion_tokens", 0),
    }


def _query_gemini(prompt_text: str, location: dict | None = None) -> dict:
    """
    Query Google Gemini with Search grounding. Gemini's grounding has no
    user_location parameter, so it relies on geo written into the prompt text
    (our prompts carry the city); `location` is accepted for a uniform dispatch
    signature but not used here.
    """
    key = gemini_key()
    if not key:
        return {"error": "no_key"}

    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODELS['gemini']}:generateContent",
        params={"key": key},
        json={
            "contents": [{"parts": [{"text": f"{SYSTEM_PROMPT}\n\n{prompt_text}"}]}],
            # Enable Google Search grounding — mirrors the real consumer Gemini answer.
            "tools": [{"google_search": {}}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    text    = ""
    sources = []
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        grounding = data["candidates"][0].get("groundingMetadata", {})
        chunks    = grounding.get("groundingChunks", [])
        sources   = [c["web"]["uri"] for c in chunks if c.get("web", {}).get("uri")]
    except (KeyError, IndexError):
        pass

    meta = data.get("usageMetadata", {})
    return {
        "text":          text,
        "sources":       sources,
        "tokens_input":  meta.get("promptTokenCount", 0),
        "tokens_output": meta.get("candidatesTokenCount", 0),
    }


ENGINE_FNS = {
    "chatgpt":    _query_chatgpt,
    "perplexity": _query_perplexity,
    "gemini":     _query_gemini,
}
