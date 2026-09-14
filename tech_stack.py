"""
Tech-stack audit — detects a site's marketing/analytics stack from its HTML.

Second audit in the stacked wedge. Presence of tags is high-inference: GTM/GA4
=> they measure and very likely run Google Ads; a Meta Pixel => they run (or can
run) Meta ads; no pixel at all => a tracking/attribution gap. Feeds the
recommendation engine (which service to pitch) and is measurable over time.

    python tech_stack.py https://thedentalmarket.com

Note: reads the initial HTML. Analytics/pixel snippets almost always live in
<head> and are present even on JS-rendered sites; deeper app content is not.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import urlparse

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"}

# marker_name -> (regex, extract-id-group or None)
SIGNATURES = {
    # Analytics / tag management
    "google_tag_manager": (r"googletagmanager\.com/gtm\.js|GTM-[A-Z0-9]{4,}", r"GTM-[A-Z0-9]{4,}"),
    "ga4":                (r"gtag/js\?id=G-[A-Z0-9]+|['\"]G-[A-Z0-9]{6,}['\"]", r"G-[A-Z0-9]{6,}"),
    "universal_analytics":(r"UA-\d{4,}-\d+", r"UA-\d{4,}-\d+"),
    # Google Ads
    "google_ads":         (r"AW-\d{6,}|googleadservices\.com|googleads\.g\.doubleclick", r"AW-\d{6,}"),
    # Social pixels
    "meta_pixel":         (r"connect\.facebook\.net/.+/fbevents\.js|fbq\(", None),
    "tiktok_pixel":       (r"analytics\.tiktok\.com|ttq\.load", None),
    "linkedin_insight":   (r"snap\.licdn\.com|_linkedin_partner_id", None),
    "bing_uet":           (r"bat\.bing\.com|uetq", None),
    "pinterest_tag":      (r"pintrk\(", None),
    # Chat / CRM / booking
    "gohighlevel":        (r"leadconnectorhq|gohighlevel|msgsndr", None),
    "hubspot":            (r"js\.hs-scripts\.com|hubspot", None),
    # Reviews
    "reviews_widget":     (r"birdeye|podium|nicejob|grade\.us", None),
    # Schema (AEO-relevant)
    "localbusiness_schema": (r'"@type"\s*:\s*"(LocalBusiness|Dentist|MedicalBusiness|HealthAndBeautyBusiness|MedicalClinic)"', None),
    "faq_schema":         (r'"@type"\s*:\s*"FAQPage"', None),
}

# CMS / platform detection
PLATFORMS = {
    "WordPress": r"wp-content|wp-includes",
    "Wix":       r"wix\.com|_wixCssData",
    "Squarespace": r"squarespace\.com|static1\.squarespace",
    "Shopify":   r"cdn\.shopify\.com|Shopify\.",
    "Webflow":   r"webflow\.com|w-mod-",
    "GoHighLevel": r"leadconnectorhq|msgsndr",
    "Duda":      r"dudaone|_dm_",
}


# AI crawlers worth checking. Blocking any of these removes the site from the
# corpus that engine reads when it answers — the SEO agency's robots.txt is a
# common and entirely silent cause of AEO invisibility.
AI_CRAWLERS = {
    "GPTBot":          "OpenAI — training + ChatGPT browsing",
    "OAI-SearchBot":   "OpenAI — ChatGPT search index",
    "ChatGPT-User":    "OpenAI — user-initiated fetches",
    "ClaudeBot":       "Anthropic — training + Claude browsing",
    "PerplexityBot":   "Perplexity — index",
    "Perplexity-User": "Perplexity — user-initiated fetches",
    "Google-Extended": "Google — Gemini grounding + training",
    "CCBot":           "Common Crawl — feeds many open models",
}


# Paths every CMS blocks by default. A Disallow here says nothing about AEO, so
# they are ignored — otherwise every WordPress site reports a false positive.
BENIGN_DISALLOW = re.compile(
    r"^/(wp-admin|wp-includes|wp-json|wp-content/(plugins|cache|uploads/wpo)|admin|administrator"
    r"|cgi-bin|xmlrpc\.php|trackback|feed|comments|search|\?|\*|.*\.(php|json|xml)\$?)",
    re.IGNORECASE)


def _parse_robots(text: str) -> dict[str, list[tuple[str, str]]]:
    """user-agent (lowercased) -> [(directive, path)]. Consecutive UA lines share a block."""
    groups: dict[str, list[tuple[str, str]]] = {}
    agents: list[str] = []
    collecting = True
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if not collecting:
                agents, collecting = [], True
            agents.append(value.lower())
            groups.setdefault(value.lower(), [])
        elif field in ("allow", "disallow") and agents:
            collecting = False
            for a in agents:
                groups[a].append((field, value))
    return groups


def check_ai_crawlers(url: str) -> dict:
    """Fetch robots.txt and report which AI crawlers are shut out.

    A named group wins over `*` (RFC 9309). We only assert a full block on an
    explicit `Disallow: /` with no `Allow: /` alongside it; anything narrower is
    reported as partial and left for a human, rather than guessing at path rules.
    """
    if not url.startswith("http"):
        url = "https://" + url
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    out: dict = {"robots_url": f"{base}/robots.txt", "status": None,
                 "blocked": [], "partial": [], "allowed": [], "via_wildcard": []}
    try:
        r = requests.get(out["robots_url"], headers=UA, timeout=15)
    except requests.RequestException as e:
        out["status"] = f"unreachable ({type(e).__name__})"
        return out
    if r.status_code == 404:
        out["status"] = "no robots.txt — nothing blocked"
        out["allowed"] = list(AI_CRAWLERS)
        return out
    if r.status_code != 200:
        out["status"] = f"HTTP {r.status_code}"
        return out

    out["status"] = "ok"
    groups = _parse_robots(r.text)
    for bot in AI_CRAWLERS:
        rules = groups.get(bot.lower())
        matched_wildcard = rules is None
        if matched_wildcard:
            rules = groups.get("*", [])
        full = any(d == "disallow" and p == "/" for d, p in rules)
        allow_root = any(d == "allow" and p == "/" for d, p in rules)
        narrower = any(d == "disallow" and p not in ("", "/")
                       and not BENIGN_DISALLOW.match(p) for d, p in rules)
        if full and not allow_root:
            out["blocked"].append(bot)
            if matched_wildcard:
                out["via_wildcard"].append(bot)
        elif narrower:
            out["partial"].append(bot)
        else:
            out["allowed"].append(bot)
    return out


def _fetch(url: str) -> str:
    if not url.startswith("http"):
        url = "https://" + url
    r = requests.get(url, headers=UA, timeout=25)
    r.raise_for_status()
    return r.text


def audit(url: str) -> dict:
    html = _fetch(url)
    found: dict[str, dict] = {}
    for name, (pattern, id_pat) in SIGNATURES.items():
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            ident = None
            if id_pat:
                idm = re.search(id_pat, html)
                ident = idm.group() if idm else None
            found[name] = {"present": True, "id": ident}

    platform = next((name for name, pat in PLATFORMS.items()
                     if re.search(pat, html, re.IGNORECASE)), None)

    crawlers = check_ai_crawlers(url)

    # High-inference read for the recommendation engine
    has = lambda k: k in found
    inferences = []
    if has("google_tag_manager") or has("ga4"):
        inferences.append("Runs Google analytics/tagging — very likely running or ready for Google Ads.")
    if has("google_ads"):
        inferences.append("Active Google Ads conversion tag detected — already spending on paid search.")
    if not (has("google_ads")) and (has("ga4") or has("google_tag_manager")):
        inferences.append("Measures traffic but no Google Ads conversion tag — PPC opportunity or untracked spend.")
    if has("meta_pixel"):
        inferences.append("Meta Pixel present — running or set up for Meta ads.")
    else:
        inferences.append("No Meta Pixel — Meta/Instagram ads gap (no audience building or retargeting).")
    if not has("localbusiness_schema"):
        inferences.append("No LocalBusiness schema — weak entity signal for AI/local search (AEO fix).")
    if not (has("ga4") or has("universal_analytics") or has("google_tag_manager")):
        inferences.append("No analytics detected at all — flying blind; foundational gap.")
    if crawlers["blocked"]:
        who = ", ".join(crawlers["blocked"])
        route = " via a blanket Disallow, likely unintended" if crawlers["via_wildcard"] else ""
        inferences.append(
            f"robots.txt BLOCKS {who}{route} — these engines cannot read the site at all, "
            "so no amount of content or schema work will surface it. Fix this first.")
    elif crawlers["partial"]:
        inferences.append(
            f"robots.txt partially restricts {', '.join(crawlers['partial'])} — worth a human read "
            "to confirm service pages aren't caught.")

    return {
        "url": url,
        "domain": urlparse(url if "://" in url else "https://" + url).netloc.replace("www.", ""),
        "platform": platform,
        "detected": found,
        "ai_crawlers": crawlers,
        "inferences": inferences,
    }


def print_summary(url: str) -> dict:
    a = audit(url)
    print(f"\n{'='*60}\nTech-Stack Audit — {a['domain']}\n{'='*60}")
    print(f"  Platform: {a['platform'] or 'unknown'}\n")
    print("  Detected:")
    for name, info in a["detected"].items():
        ids = f"  [{info['id']}]" if info.get("id") else ""
        print(f"    + {name}{ids}")
    if not a["detected"]:
        print("    (none)")

    c = a["ai_crawlers"]
    print(f"\n  AI crawlers ({c['status']}):")
    if c["blocked"]:
        via = "  (via a blanket rule)" if c["via_wildcard"] else ""
        print(f"    BLOCKED   {', '.join(c['blocked'])}{via}")
    if c["partial"]:
        print(f"    partial   {', '.join(c['partial'])}")
    if c["allowed"]:
        print(f"    allowed   {len(c['allowed'])}/{len(AI_CRAWLERS)}")

    print("\n  Inferences (for the recommendation engine):")
    for inf in a["inferences"]:
        print(f"    - {inf}")
    return a


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Audit a site's marketing/analytics tech stack")
    ap.add_argument("url")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a summary")
    args = ap.parse_args()
    if args.json:
        print(json.dumps(audit(args.url), indent=2))
    else:
        print_summary(args.url)
