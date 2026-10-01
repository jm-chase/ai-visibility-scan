"""Which pages decide a market.

Every engine already returns the URLs it leaned on. The scan threw them away and
kept only the names, which answers "am I named" and not the question that
follows it: *where would I have to appear for that to change*.

A market is not decided by the whole internet. It is decided by the handful of
pages the engines quote when asked who to use - a roundup, a review platform, a
forum thread, two trade titles. Aggregating the sources turns "build authority",
which has no edge and cannot be sold, into a finite list of pages with an owner
and a deadline.

Classification is deliberately coarse. The type only has to be right enough to
say who does the work: a review platform is an operations job, a roundup is a
pitch, a forum is a participation problem.
"""

from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlparse

# Redirect wrappers and grounding proxies the engines emit. Not real sources.
NOISE = (
    "vertexaisearch.cloud.google.com", "googleusercontent.com", "google.com/url",
    "bing.com/ck", "duckduckgo.com/l", "news.google.com",
)

REVIEW = (
    "yelp.", "trustpilot.", "bbb.org", "google.com/maps", "tripadvisor.",
    "angi.com", "thumbtack.", "birdeye.", "healthgrades.", "zocdoc.", "vitals.com",
    "ratemds.", "glassdoor.", "g2.com", "capterra.",
)
FORUM = (
    "reddit.com", "quora.com", "stackexchange.", "stackoverflow.", "nextdoor.",
    "forums.", "forum.", "community.", "discourse.",
)
VIDEO = ("youtube.com", "youtu.be", "vimeo.com", "tiktok.com")
DIRECTORY = (
    "yellowpages.", "manta.com", "chamberofcommerce.", "expertise.com",
    "threebestrated.", "three-best-rated.", "citysearch.", "mapquest.",
    "crunchbase.", "clutch.co", "directory",
)
ENCYCLOPEDIC = ("wikipedia.org", "wikidata.org", "britannica.com")

# Vertical aggregators: sites that exist to rank the businesses in one category.
# They were the blind spot in the first version, classified as editorial because
# they are not obviously directories - and in two of six markets measured they
# were the single biggest source of citations (Caring.com and A Place for Mom
# took 41 of 105 citations in senior care). Getting listed and ranking inside
# one of these is a different job from earning a mention, so it gets its own type.
AGGREGATOR = (
    "caring.com", "aplaceformom.com", "seniorly.com", "seniorliving.org",
    "realself.com", "medicalspalocator.com", "fresha.com", "booksy.com", "vagaro.com",
    "chrono24.com", "1stdibs.com", "bobswatches.com",
    "zillow.com", "redfin.com", "realtor.com", "bankrate.com", "nerdwallet.com",
    "lendingtree.com", "avvo.com", "findlaw.com", "lawyers.com", "justia.com",
    "porch.com", "houzz.com", "homeadvisor.com", "wedding wire", "theknot.com",
)


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def classify(url: str) -> str:
    """Coarse source type. The type decides who does the placement work."""
    u = url.lower()
    for group, label in (
        (ENCYCLOPEDIC, "encyclopedic"),
        (AGGREGATOR, "aggregator"),
        (REVIEW, "review platform"),
        (FORUM, "forum"),
        (VIDEO, "video"),
        (DIRECTORY, "directory"),
    ):
        if any(h in u for h in group):
            return label
    # A "best/top/vs" page on an ordinary domain is an editorial roundup, which
    # is the most pitchable target type there is.
    if any(w in u for w in ("/best-", "best-", "/top-", "top-", "-vs-", "review")):
        return "roundup"
    return "editorial"


def harvest(rows: list[dict], own_domain: str = "") -> dict:
    """Aggregate the sources behind a scan into ranked placement targets.

    `rows` is the scan's per-query structure: each engine entry may carry a
    `sources` list and a `you` flag.

    Sources behind answers where the business was NOT named rank first: those
    are what the engine surfaced instead, which is the gap to close.
    """
    own = (own_domain or "").lower().replace("www.", "")
    by_domain: dict[str, dict] = defaultdict(
        lambda: {"urls": set(), "engines": set(), "queries": set(),
                 "absent_hits": 0, "total_hits": 0})

    for row in rows:
        q = row.get("query", "")
        for engine, data in (row.get("engines") or {}).items():
            if "error" in data:
                continue
            named_here = bool(data.get("you"))
            for url in data.get("sources") or []:
                d = _domain(url)
                if not d or any(n in url.lower() for n in NOISE):
                    continue
                if own and own in d:
                    continue
                e = by_domain[d]
                e["urls"].add(url)
                e["engines"].add(engine)
                e["queries"].add(q)
                e["total_hits"] += 1
                if not named_here:
                    e["absent_hits"] += 1

    targets = []
    for domain, e in by_domain.items():
        targets.append({
            "domain": domain,
            "type": classify(sorted(e["urls"])[0]),
            "cited": e["total_hits"],
            "cited_where_absent": e["absent_hits"],
            "engines": sorted(e["engines"]),
            "queries": sorted(e["queries"]),
            "urls": sorted(e["urls"])[:5],
        })

    # Cited often, and especially where the business was missing, comes first.
    targets.sort(key=lambda t: (-t["cited_where_absent"], -t["cited"], t["domain"]))

    by_type: dict[str, int] = defaultdict(int)
    for t in targets:
        by_type[t["type"]] += 1

    return {
        "targets": targets,
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "domains": len(targets),
        "citations": sum(t["cited"] for t in targets),
    }


def print_targets(res: dict, limit: int = 20) -> None:
    """The list as an operator reads it: where to get placed, most urgent first."""
    print(f"\n{res['domains']} domains cited across {res['citations']} citations")
    print(f"{'domain':34s}{'type':17s}{'cited':>7s}{'absent':>8s}  engines")
    for t in res["targets"][:limit]:
        print(f"{t['domain'][:32]:34s}{t['type']:17s}"
              f"{t['cited']:>7d}{t['cited_where_absent']:>8d}  {', '.join(t['engines'])}")
