"""
Model registry — the single source of truth for *which* model represents each
AI answer engine, plus the internal Claude ops models.

Why this exists
---------------
AEO measures whether a brand shows up when a real person asks ChatGPT / Gemini /
Perplexity a question. That measurement is only credible if we query the model a
real person actually hits. Model IDs churn fast (ChatGPT moved gpt-4o -> gpt-5.x
in under a year), so hardcoding them anywhere means the engine silently drifts to
testing a model nobody uses.

Two mechanisms keep this current:
  1. Prefer the provider's own *auto-updating alias* where one exists
     (`gemini-flash-latest`, OpenAI `*-chat-latest`). These track the consumer
     product without a code change.
  2. `check_freshness()` queries each provider's live /models endpoint, applies
     the selection rule, and flags when a newer model exists than the one pinned
     here — or when REVIEW_BY has passed. Run it from the CLI (`python models.py`)
     or the monthly servicing cycle.

`grounded=True` means "answer with live web search enabled" — this mirrors what a
real consumer sees (ChatGPT with browsing, Gemini with Google Search grounding).
Ungrounded model-memory answers are NOT what a local-business buyer experiences,
so grounding is the default for citation testing.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

# When the pinned choices below should next be eyeballed against reality.
REVIEW_BY = date(2026, 10, 1)
REVIEWED = date(2026, 7, 22)

HOME = Path.home()


# ── Answer-engine registry ────────────────────────────────────────────────────
# `model`      : what we query today (prefer an auto-updating alias).
# `grounded`   : query with live web search on (mirrors the real consumer answer).
# `match`      : substring every candidate for this engine's consumer model shares
#                — used by check_freshness() to spot newer models on the live list.
# `prefer`     : ranked hints; the first live model containing one of these wins
#                when auto-discovering. Earlier = stronger preference.
ENGINES: dict[str, dict] = {
    "chatgpt": {
        "label":    "ChatGPT (OpenAI)",
        "model":    "gpt-5-search-api",       # web-grounded search model (consumer ChatGPT hits web search)
        "baseline": "gpt-5.3-chat-latest",    # ungrounded consumer-chat alias, for memory-only comparison
        "grounded": True,
        "match":    "gpt-",
        "prefer":   ["-search-api", "-chat-latest", "gpt-5"],
        "share":    0.77,   # ~77% of AI-search traffic — weight it heaviest
    },
    "gemini": {
        "label":    "Gemini (Google)",
        "model":    "gemini-flash-latest",    # auto-updating alias -> current consumer Flash
        "baseline": "gemini-flash-latest",
        "grounded": True,                     # enable google_search grounding in the request
        "match":    "gemini-",
        "prefer":   ["-flash-latest", "-pro-latest", "gemini-3"],
        "share":    0.15,
    },
    "perplexity": {
        "label":    "Perplexity",
        "model":    "sonar-pro",              # inherently web-grounded
        "baseline": "sonar-pro",
        "grounded": True,
        "match":    "sonar",
        "prefer":   ["sonar-pro", "sonar"],
        "share":    0.08,
    },
}

# ── Internal Claude ops models (prompt mining, content generation) ─────────────
# Not an engine we test — this is our own tooling. Keep on current Claude IDs.
CLAUDE_STRONG = "claude-sonnet-5"        # prompt/content generation (balanced)
CLAUDE_FAST   = "claude-haiku-4-5"       # cheap classification / short tasks


def consumer_model(engine: str, grounded: bool | None = None) -> str:
    """The model ID to query for `engine`. `grounded=False` returns the memory-only baseline."""
    cfg = ENGINES[engine]
    if grounded is False:
        return cfg["baseline"]
    return cfg["model"]


def is_grounded(engine: str) -> bool:
    return ENGINES[engine].get("grounded", False)


# ── Freshness check (the self-updating half) ──────────────────────────────────

def _load_key(svc: str) -> str | None:
    p = HOME / svc / "credentials.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text()).get("api_key")
    except Exception:
        return None


def _openai_models() -> list[str]:
    key = _load_key(".openai")
    if not key:
        return []
    req = urllib.request.Request(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return [m["id"] for m in json.loads(r.read()).get("data", [])]


def _gemini_models() -> list[str]:
    key = _load_key(".gemini")
    if not key:
        return []
    url = "https://generativelanguage.googleapis.com/v1beta/models?" + urllib.parse.urlencode({"key": key})
    with urllib.request.urlopen(url, timeout=30) as r:
        out = []
        for m in json.loads(r.read()).get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                out.append(m.get("name", "").replace("models/", ""))
        return out


_LIVE_LISTERS = {"chatgpt": _openai_models, "gemini": _gemini_models}


def _newest_matching(cfg: dict, live: list[str]) -> str | None:
    """Pick the live model that best fits this engine's consumer profile."""
    cands = [m for m in live if cfg["match"] in m]
    if not cands:
        return None
    for hint in cfg["prefer"]:
        hits = sorted((m for m in cands if hint in m), reverse=True)
        if hits:
            return hits[0]
    return sorted(cands, reverse=True)[0]


def check_freshness() -> list[dict]:
    """
    Compare each pinned model against the live provider list. Returns one row per
    engine with {engine, pinned, suggested, stale}. `stale` is True when the live
    list offers a model our `prefer` rules would rank above the pinned one.
    """
    rows = []
    for engine, cfg in ENGINES.items():
        lister = _LIVE_LISTERS.get(engine)
        live = []
        if lister:
            try:
                live = lister()
            except Exception as e:
                rows.append({"engine": engine, "pinned": cfg["model"],
                             "suggested": f"(live check failed: {e})", "stale": False})
                continue
        suggested = _newest_matching(cfg, live) if live else None
        # Not stale when: pinned is an auto-updating alias (…-latest, provider moves it
        # for us), or `suggested` is just a dated snapshot of the pinned model
        # (one is a prefix of the other, e.g. gpt-5-search-api vs …-2025-10-14).
        pinned = cfg["model"]
        same_family = bool(suggested and (suggested.startswith(pinned) or pinned.startswith(suggested)))
        stale = bool(suggested and suggested != pinned and "latest" not in pinned and not same_family)
        rows.append({"engine": engine, "pinned": pinned,
                     "suggested": suggested or "(no live list / key)", "stale": stale})
    return rows


if __name__ == "__main__":
    print(f"Model registry — reviewed {REVIEWED}, review by {REVIEW_BY} "
          f"({'STALE — past review date' if date.today() > REVIEW_BY else 'ok'})\n")
    print(f"  Claude ops: strong={CLAUDE_STRONG}  fast={CLAUDE_FAST}\n")
    print("  Answer engines (live check):")
    for row in check_freshness():
        flag = "  <-- NEWER AVAILABLE, review" if row["stale"] else ""
        print(f"    {row['engine']:11s} pinned={row['pinned']:22s} "
              f"live_suggests={row['suggested']}{flag}")
