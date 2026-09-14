"""
market_scan.py — "who does the AI name?" for a niche + city.

The prospecting engine. Instead of picking a business and auditing it, we audit
the MARKET: ask the AI engines (web-grounded, location-injected) a hyper-local
niche query and capture the specific businesses they name. The businesses that
show up are the AI-visible winners; everyone else in the local roster is invisible
— and each invisible business is a warm prospect that arrives with its own finding
("ChatGPT names X and Y for this, not you").

Runs on the funded search engines (OpenAI / Gemini / Perplexity) — no Anthropic.

    python market_scan.py --niche "veneers without insurance" --city Raleigh --state NC
    python market_scan.py --niche "emergency plumber" --city Raleigh --state NC --lsa plumber
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# allow standalone CLI use from this subdir — put the aeo-engine root on the path
# so the lazy `monitor` / `ppc_audit` imports resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_ENGINES = ("chatgpt", "perplexity", "gemini")

# name-normalization: drop legal suffixes / honorifics so the same practice matches
# across an AI answer and a directory roster. Keep industry words for distinctiveness.
_STOP = {"the", "llc", "pllc", "pa", "inc", "co", "dds", "dmd", "md", "dr", "drs", "and"}


def scan_prompt(niche: str, city: str, state: str) -> str:
    """An extraction-optimized query: engineered so the answer is a clean name list."""
    return (
        f'A person located in {city}, {state} searches for: "{niche}".\n'
        "Based on current local web results, which specific local businesses, practices, "
        "or providers come up as the top options for this search?\n"
        "Return ONLY a numbered list of the business names, most prominent first — "
        "name only, no descriptions, no addresses. If you cannot name specific local "
        "businesses, reply with the single word: NONE."
    )


# ── Parsing an answer into business names ──────────────────────────────────────

def _clean_line(line: str) -> str:
    s = line.strip()
    s = re.sub(r"^#{1,6}\s*", "", s)                               # markdown heading
    if s.count("|") >= 1:                                          # table row / pipe noise
        cells = [c.strip() for c in s.split("|") if c.strip()]
        s = cells[0] if cells else ""
    s = re.sub(r"^\s*(?:\d{1,2}[\.\)]|[-*•·])\s*", "", s)          # list marker
    s = s.replace("**", "").replace("__", "").replace("`", "").strip()
    s = re.split(r"\s+[-–—:]\s+|\s+\(", s)[0].strip()              # cut trailing description
    return s.strip(" .,:;-–—*\"'")


_SKIP_PREFIX = ("here", "based", "the following", "these", "top ", "note", "disclaimer",
                "i ", "as an", "unfortunately", "however", "for ", "in ", "when ", "if ",
                "several", "many", "some ", "there ", "keep in", "please")


# Service categories the engines list as headings or descriptors. On their own
# they are never a business name; inside one ("Raleigh Cosmetic Dentistry") the
# capital-letter test below lets them through.
_CATEGORY_ONLY = re.compile(
    r"^(?:best|top|good|affordable|cheap)?\s*"
    r"(?:general|family|cosmetic|pediatric|emergency|restorative|sedation|implant|"
    r"orthodont\w*|endodont\w*|periodont\w*|bariatric|fertility|dermatolog\w*)?"
    r"[\s/&,-]*"
    r"(?:dentistry|dentists?|practices?|clinics?|surgeons?|doctors?|providers?|"
    r"specialists?|options?|services?|care|treatment)\s*$", re.IGNORECASE)


def _looks_like_name(s: str) -> bool:
    if not s or not re.search(r"[A-Za-z]", s):
        return False
    # A business name is capitalised. Category phrases the models emit as
    # descriptors ("cosmetic dentistry", "best for insurance/network") are not.
    if not re.search(r"[A-Z]", s):
        return False
    if _CATEGORY_ONLY.match(s.strip()):
        return False
    low = s.lower()
    if low in ("none", "n/a", "na"):
        return False
    words = s.split()
    if len(words) > 8:                       # a sentence, not a name
        return False
    if low.startswith(_SKIP_PREFIX):
        return False
    if s.endswith(".") and len(words) > 5:
        return False
    return True


_SECTION_HEADING = re.compile(
    r"\b(?:practices|specialists|options|dentists|clinics|providers|surgeons|"
    r"doctors|considerations|notes|summary|picks|choices|recommendations|"
    r"takeaways|overview|others?|alternatives)\b", re.IGNORECASE)


def extract_names(text: str, cap: int = 12) -> list[str]:
    """Best-effort business-name extraction from an AI answer (engineered to be a list)."""
    if not text or text.strip().upper() == "NONE":
        return []
    out, seen = [], set()
    for line in text.splitlines():
        # A markdown heading naming a category ("## Specialists & Niche Practices")
        # reads like a business name once the hashes are stripped. A heading
        # naming one business ("### Village Dental") does not, so only headings
        # carrying a generic plural are dropped.
        if line.lstrip().startswith("#") and _SECTION_HEADING.search(line):
            continue
        name = _clean_line(line)
        if not _looks_like_name(name):
            continue
        k = _norm(name)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(name)
        if len(out) >= cap:
            break
    return out


# ── Name matching (AI answer <-> roster) ───────────────────────────────────────

def _norm(name: str) -> str:
    toks = [t for t in re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split() if t not in _STOP]
    return " ".join(toks)


def _match(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    ta, tb = set(na.split()), set(nb.split())
    inter = ta & tb
    return bool(inter) and len(inter) / len(ta | tb) >= 0.6


# ── The scan ───────────────────────────────────────────────────────────────────

def _default_query_fn(engine: str, prompt: str, location: dict) -> dict:
    # engines, not prompt_tester: the latter imports db and clients at module
    # level, so touching it drags the whole client registry into any process
    # that runs a scan - including one deployed where that registry must not go.
    from engines import ENGINE_FNS
    return ENGINE_FNS[engine](prompt, location)


def _location(city: str, state: str) -> dict:
    from engines import US_STATES
    return {"country": "US", "city": city, "region": US_STATES.get(state, state)}


def scan_niche(niche: str, city: str, state: str,
               engines=DEFAULT_ENGINES, query_fn=None) -> dict:
    """
    Query each engine for the niche (location-injected) and aggregate the businesses
    named. `query_fn(engine, prompt, location) -> {"text": ...} | {"error": ...}` is
    injectable for testing. Returns per-engine names + a cross-engine consensus
    (named by more engines = more AI-visible).
    """
    qf = query_fn or _default_query_fn
    prompt = scan_prompt(niche, city, state)
    loc = _location(city, state)

    per_engine: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for e in engines:
        try:
            r = qf(e, prompt, loc)
        except Exception as ex:                      # network / API failure on one engine
            errors[e] = f"{type(ex).__name__}: {ex}"[:80]
            continue
        if not r or "error" in r:
            errors[e] = (r or {}).get("error", "no_data")
            continue
        per_engine[e] = extract_names(r.get("text", ""))

    canon: dict[str, dict] = {}
    for e, names in per_engine.items():
        for nm in names:
            k = _norm(nm)
            if not k:
                continue
            c = canon.setdefault(k, {"name": nm, "count": 0, "engines": []})
            if e not in c["engines"]:
                c["count"] += 1
                c["engines"].append(e)
    consensus = sorted(canon.values(), key=lambda c: (-c["count"], c["name"].lower()))
    return {
        "niche": niche, "city": city, "state": state,
        "per_engine": per_engine, "errors": errors,
        "consensus": [(c["name"], c["count"]) for c in consensus],
        "all_named": [c["name"] for c in consensus],
    }


def scan_market(market: str, queries: list[str], city: str, state: str,
                query_fn=None) -> dict:
    """
    Run a SPREAD of queries for one market (decision + consideration) and rank
    businesses by share-of-voice — the fraction of (query x engine) slots that named
    them. Named across many queries/engines = genuinely owns the market; named in a
    single narrow query = weak signal. This is how you see the whole playing field
    instead of one query's snapshot.
    """
    per_query = [scan_niche(q, city, state, query_fn=query_fn) for q in queries]
    total_slots = sum(len(r["per_engine"]) for r in per_query)  # answering (query,engine) pairs

    canon: dict[str, dict] = {}
    for qi, res in enumerate(per_query):
        for engine, names in res["per_engine"].items():
            for nm in names:
                k = _norm(nm)
                if not k:
                    continue
                c = canon.setdefault(k, {"name": nm, "mentions": 0,
                                         "queries": set(), "engines": set()})
                c["mentions"] += 1
                c["queries"].add(qi)
                c["engines"].add(engine)
    ranking = []
    for c in canon.values():
        ranking.append({"name": c["name"], "mentions": c["mentions"],
                        "n_queries": len(c["queries"]), "n_engines": len(c["engines"]),
                        "share_of_voice": round(c["mentions"] / total_slots, 3) if total_slots else 0.0})
    ranking.sort(key=lambda r: (-r["mentions"], -r["n_queries"], r["name"].lower()))
    errors = {f"q{qi}:{e}": v for qi, res in enumerate(per_query) for e, v in res["errors"].items()}
    return {"market": market, "city": city, "state": state, "queries": queries,
            "n_queries": len(queries), "total_slots": total_slots,
            "ranking": ranking, "per_query": per_query, "errors": errors}


def gap_report(ai_named: list[str], roster: list[str]) -> dict:
    """
    Diff the AI-named winners against a local roster (e.g. Google Screened/LSA).
      invisible  = in the market, NOT named by the AI  -> warmest prospects
      ai_visible = named by the AI AND in the roster
      ai_only    = named by the AI but not in the roster (strong incumbents)
    """
    invisible = [r for r in roster if not any(_match(r, a) for a in ai_named)]
    ai_visible = [r for r in roster if any(_match(r, a) for a in ai_named)]
    ai_only = [a for a in ai_named if not any(_match(a, r) for r in roster)]
    return {"invisible": invisible, "ai_visible": ai_visible, "ai_only": ai_only}


def print_scan(res: dict) -> None:
    print(f"\n{'='*64}\nMarket scan — \"{res['niche']}\"  ·  {res['city']}, {res['state']}\n{'='*64}")
    print("  Who the AI names (consensus across engines):")
    if not res["consensus"]:
        print("    (no specific businesses named)")
    for i, (name, cnt) in enumerate(res["consensus"], 1):
        print(f"    {i:2d}. {name:42s} named by {cnt}/{len(res['per_engine'])} engines")
    for e, names in res["per_engine"].items():
        print(f"\n  {e}: {', '.join(names) or '(none)'}")
    if res["errors"]:
        print(f"\n  errors: {res['errors']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scan who the AI names for a niche + city")
    ap.add_argument("--niche", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--engines", default=",".join(DEFAULT_ENGINES))
    ap.add_argument("--lsa", help="LSA category to pull the local roster for a gap report (e.g. 'dentist')")
    args = ap.parse_args()

    res = scan_niche(args.niche, args.city, args.state, engines=tuple(args.engines.split(",")))
    print_scan(res)

    if args.lsa:
        import ppc_audit
        from urllib.parse import quote
        url = f"https://www.google.com/localservices/prolist?q={quote(f'{args.lsa} {args.city} {args.state}')}&hl=en&gl=us"
        roster = ppc_audit.lsa_roster(url)
        gap = gap_report(res["all_named"], roster)
        print(f"\n  Local roster (LSA '{args.lsa}'): {len(roster)} providers")
        print(f"  INVISIBLE in AI (prospects): {', '.join(gap['invisible']) or '(none)'}")
        print(f"  AI-visible + in roster:      {', '.join(gap['ai_visible']) or '(none)'}")
