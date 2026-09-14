"""
scan_report.py — analysis behind the free scan.

Three things the first version lacked, in the order they change a decision:

  COMPARISON. "Village Dental 4x" is information. "You 0 of 6, Village Dental
  4 of 6" is a reason to act. Same denominator, same method, no invented score
  - the comparison is honest and it is the part that lands.

  EVIDENCE. Saying "an entity problem" is a diagnosis nobody can act on. Saying
  "they hold 1,943 reviews and you hold 140" is the same diagnosis with the
  cause attached, and the prospect can verify it in ten seconds.

  A RECORD. A scan that leaves no trace is a page view. Captured leads are
  stored locally so a scan becomes a follow-up.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "prospecting"))

import config                                     # noqa: E402
from prospecting import market_scan as ms         # noqa: E402

DB = Path(__file__).parent / "data" / "scan_leads.db"


# ── comparison ────────────────────────────────────────────────────────────────

def comparative(res: dict, top: int = 5) -> list[dict]:
    """You and your rivals on the same denominator.

    Every row is `hits of slots` over the identical question-and-engine grid,
    so the comparison needs no weighting and no score. A rival named by both
    engines on all three questions reads 6 of 6; you read whatever you read.
    """
    slots = res["slots"]
    counts: dict[str, int] = {}
    for row in res["rows"]:
        for _e, d in row["engines"].items():
            for n in d.get("named", []):
                if ms._match(res["business"], n):
                    continue
                # Collapse near-duplicate spellings the models emit
                # ("Russo Dentistry" / "Russo General & Cosmetic Dentistry").
                key = next((k for k in counts if ms._match(k, n)), n)
                counts[key] = counts.get(key, 0) + 1

    rows = [{"name": res["business"], "hits": res["hits"], "slots": slots, "you": True}]
    rows += [{"name": n, "hits": c, "slots": slots, "you": False}
             for n, c in sorted(counts.items(), key=lambda kv: -kv[1])[:top]]
    return rows


# ── evidence: what the winners have that you do not ───────────────────────────

_STOP = re.compile(r"\b(the|of|and|inc|llc|pa|pllc|dds|dmd|md|dr|drs)\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", _STOP.sub("", (s or "").lower())).split()


def _same(a: str, b: str) -> bool:
    """A Maps listing and an AI answer rarely spell a business identically.
    Two shared significant words is a match in practice - one is not, or every
    'Raleigh X' matches every other."""
    x, y = set(_norm(a)), set(_norm(b))
    if not x or not y:
        return False
    return len(x & y) >= 2 or x <= y or y <= x


def gbp_lookup(name: str, city: str, state: str) -> dict | None:
    """Rating and reviews for ONE named business, via a targeted Maps search.

    Two traps here, both found the hard way. A generic roster query returns
    Maps' local pack, which is not the set the models name - matching against
    it hit 1 of 6. And a search for a specific business returns SerpAPI's
    `place_results` (a single object), not `local_results` (a list), so code
    written for rosters silently finds nothing.

    One SerpAPI credit per call, which is why this runs only after the gate.
    """
    key = config.serpapi_key()
    if not key:
        return None
    try:
        import requests
        from urllib.parse import urlencode
        params = {"engine": "google_maps", "type": "search",
                  "q": f"{name} {city} {state}", "google_domain": "google.com",
                  "hl": "en", "api_key": key}
        d = requests.get("https://serpapi.com/search.json?" + urlencode(params),
                         timeout=45).json()
    except Exception:
        return None

    def pack(b: dict) -> dict:
        return {"rating": b.get("rating"), "reviews": b.get("reviews"),
                "listing": b.get("title") or b.get("name"),
                "website": b.get("website")}

    place = d.get("place_results")
    if place and place.get("title") and _same(name, place["title"]):
        return pack(place)
    for b in (d.get("local_results") or [])[:5]:
        if b.get("title") and _same(name, b["title"]):
            return pack(b)
    return None


def gbp_evidence(names: list[str], city: str, state: str, cap: int = 3,
                 on_step=None) -> dict:
    """Look up at most `cap` businesses - you and the strongest rivals.

    Capped deliberately. The sharpest line needs two numbers, not six, and
    every extra name is a SerpAPI credit spent on a marginal comparison.

    on_step(fraction, label) narrates each lookup, because this runs while
    someone is staring at a screen having just handed over their email.
    """
    out: dict[str, dict] = {}
    take = names[:cap]
    for i, n in enumerate(take):
        if on_step:
            on_step(i / max(len(take), 1), f"Checking Google reviews for {n}...")
        v = gbp_lookup(n, city, state)
        if v:
            out[n] = v
    if on_step:
        on_step(1.0, "Comparing review volume...")
    return out


def review_gap(comp: list[dict], gbp: dict) -> dict | None:
    """The single sharpest evidence line, or None when the data will not carry it."""
    you = next((r for r in comp if r["you"]), None)
    if not you:
        return None
    mine = gbp.get(you["name"], {}).get("reviews")
    rivals = [(r["name"], gbp[r["name"]]["reviews"])
              for r in comp if not r["you"]
              and gbp.get(r["name"], {}).get("reviews")]
    if not rivals:
        return None
    top_name, top_reviews = max(rivals, key=lambda t: t[1])
    avg = round(sum(v for _n, v in rivals) / len(rivals))
    return {"you": mine, "top_name": top_name, "top_reviews": top_reviews,
            "rival_avg": avg, "n": len(rivals)}


# ── leads ─────────────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS leads (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    email      TEXT NOT NULL,
    business   TEXT,
    website    TEXT,
    city       TEXT,
    state      TEXT,
    vertical   TEXT,
    hits       INTEGER,
    slots      INTEGER,
    rivals     TEXT,
    cause      TEXT
);
"""


def _conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB))
    c.executescript(DDL)
    return c


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")


def valid_email(e: str) -> bool:
    return bool(EMAIL_RE.match((e or "").strip()))


def save_lead(email: str, form: dict, res: dict, cause: str) -> int:
    c = _conn()
    rivals = [r["name"] for r in comparative(res) if not r["you"]]
    cur = c.execute(
        """INSERT INTO leads (created_at, email, business, website, city, state,
                              vertical, hits, slots, rivals, cause)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), email.strip().lower(),
         form.get("business"), form.get("website"), form.get("city"), form.get("state"),
         form.get("vertical"), res["hits"], res["slots"], json.dumps(rivals), cause))
    c.commit()
    n = cur.lastrowid
    c.close()
    return n


def deliver_lead(email: str, form: dict, res: dict, cause: str,
                 comp: list[dict] | None = None) -> tuple[bool, str]:
    """Send the lead somewhere durable, immediately.

    The SQLite row is a local record, and on an ephemeral host that record dies
    at the next restart - so it is a log, not a destination. This POSTs the lead
    the moment it is captured, which is the only part that survives.

    Destination is a secret, not a constant: LEAD_WEBHOOK_URL. Sherpa's n8n
    endpoint works, but routing jameschase.co leads through an employer's
    infrastructure is a choice worth making deliberately rather than inheriting
    from a hardcoded default.
    """
    url = os.environ.get("LEAD_WEBHOOK_URL", "").strip()
    if not url:
        return False, "no LEAD_WEBHOOK_URL configured"

    comp = comp or comparative(res)
    rivals = [f"{r['name']} ({r['hits']}/{r['slots']})" for r in comp if not r["you"]]
    body = (
        f"<h3>AI visibility scan — new lead</h3>"
        f"<p><b>{email}</b><br>{form.get('business','')} — "
        f"{form.get('city','')}, {form.get('state','')} — {form.get('vertical','')}<br>"
        f"{form.get('website','') or 'no website given'}</p>"
        f"<p><b>Named in {res['hits']} of {res['slots']}</b> answer slots.<br>"
        f"Cause: {cause}</p>"
        f"<p>Named ahead of them:<br>" + "<br>".join(rivals) + "</p>"
    )
    payload = {"to": os.environ.get("LEAD_EMAIL_TO", "hello@jameschase.co"),
               "subject": f"[scan] {form.get('business','?')} — "
                          f"{res['hits']}/{res['slots']} — {email}",
               "html": body}
    try:
        import requests
        r = requests.post(url, json=payload, timeout=15)
        return (200 <= r.status_code < 300), f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:90]


WRAP = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        'line-height:1.55;color:#202124;max-width:640px">')


def report_html(form: dict, res: dict, comp: list[dict], gbp: dict,
                cause: str, for_owner: bool = False) -> str:
    """The scan as an email. One body, sent to the prospect and to James, so a
    call starts from the same artifact rather than two recollections of it."""
    rows = ""
    for r in comp:
        bg = ' style="background:#e6f4ea"' if r["you"] else ""
        label = r["name"] + (" (you)" if r["you"] else "")
        rev = gbp.get(r["name"], {}).get("reviews")
        rows += (f'<tr{bg}><td style="border:1px solid #dadce0">{label}</td>'
                 f'<td align="right" style="border:1px solid #dadce0">{r["hits"]} of {r["slots"]}</td>'
                 f'<td align="right" style="border:1px solid #dadce0">'
                 f'{f"{rev:,}" if rev else "&mdash;"}</td></tr>')

    gap = review_gap(comp, gbp)
    evidence = ""
    if gap and gap.get("you") is not None:
        evidence = (f'<p><b>Why they win.</b> {gap["top_name"]} holds '
                    f'{gap["top_reviews"]:,} Google reviews. You hold {gap["you"]:,}. '
                    f'The engines read review volume as evidence a business is real and '
                    f'preferred, and that gap is doing more work than any page on your '
                    f'site.</p>')
    elif gap:
        evidence = (f'<p><b>Why they win.</b> {gap["top_name"]} holds '
                    f'{gap["top_reviews"]:,} Google reviews. I could not match your '
                    f'business on Google Maps at all, which is worth checking on its own '
                    f'&mdash; if Maps cannot resolve you cleanly, neither can the models.</p>')

    who = (f'<p style="background:#f1f3f4;padding:10px 12px;border-radius:4px">'
           f'<b>Lead:</b> {form.get("email","")}<br>{form.get("business","")} &middot; '
           f'{form.get("city","")}, {form.get("state","")} &middot; '
           f'{form.get("vertical","")}<br>{form.get("website") or "no website given"}</p>'
           ) if for_owner else ""

    opener = ("Here is the scan." if for_owner else
              f'Here is the scan for {form.get("business","your business")}.')

    return (WRAP + who +
            f"<p>{opener}</p>"
            f'<p>I asked ChatGPT and Perplexity the questions a buyer asks right before '
            f'they choose, across {res["slots"]} question-and-engine slots. '
            f'<b>{form.get("business","You")} was named in {res["hits"]} of them.</b></p>'
            f'<table cellpadding="8" cellspacing="0" '
            f'style="border-collapse:collapse;font-size:13px;margin:0 0 22px">'
            f'<tr style="background:#f1f3f4">'
            f'<th align="left" style="border:1px solid #dadce0">Business</th>'
            f'<th align="right" style="border:1px solid #dadce0">Named in</th>'
            f'<th align="right" style="border:1px solid #dadce0">Google reviews</th></tr>'
            f"{rows}</table>"
            f'<p>Everyone in that table faced the same questions on the same engines, so '
            f'the comparison needs no score and nothing is weighted.</p>'
            f"{evidence}"
            f'<p><b>Why you are absent.</b> {cause}</p>'
            f'<p><b>What this did not cover.</b> The rest of the question set, Gemini as a '
            f'third engine, every cause ranked by what moves first, and what to fix in week '
            f'one, week two, week three. That is the twenty-minute call.</p>'
            f'<p><a href="https://cal.com/james-chase-topd7c/strategy-call" '
            f'style="background:#15181C;color:#fff;padding:12px 22px;border-radius:6px;'
            f'text-decoration:none;display:inline-block">Book 20 minutes</a></p>'
            f'<p style="color:#5f6368;font-size:12.5px">James Chase &middot; '
            f'jameschase.co &middot; hello@jameschase.co<br>'
            f'Run {datetime.now(timezone.utc).strftime("%d %b %Y")}. Live API calls, '
            f'not cached &mdash; ask an engine yourself and you should see the same '
            f'shape.</p></div>')


# Three ways to send, tried in order. Whichever is configured wins, so the
# destination is a secret rather than a code change.
#
#   RESEND_API_KEY   transactional, from hello@jameschase.co with real SPF and
#                    DKIM. Best deliverability, and these emails go to people
#                    who have never heard from the domain before.
#   LEAD_WEBHOOK_URL any endpoint taking {to, subject, html}. A Google Apps
#                    Script web app (see tools/gmail_relay.gs) fits here and
#                    needs no third-party account.
#   nothing          the app says so on screen rather than dropping silently.

FROM_ADDR = "James Chase <hello@jameschase.co>"


def _send_resend(to: str, subject: str, html: str) -> tuple[bool, str]:
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        return False, "no key"
    try:
        import requests
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"from": os.environ.get("LEAD_FROM", FROM_ADDR),
                  "to": [to], "subject": subject, "html": html},
            timeout=20)
        if 200 <= r.status_code < 300:
            return True, "resend"
        return False, f"resend HTTP {r.status_code}: {r.text[:80]}"
    except Exception as e:
        return False, f"resend {type(e).__name__}"[:60]


def _send_webhook(to: str, subject: str, html: str) -> tuple[bool, str]:
    url = os.environ.get("LEAD_WEBHOOK_URL", "").strip()
    if not url:
        return False, "no webhook"
    try:
        import requests
        r = requests.post(url, json={"to": to, "subject": subject, "html": html}, timeout=20)
        if not (200 <= r.status_code < 300):
            return False, f"webhook HTTP {r.status_code}"
        # A Google Apps Script web app answers 200 even when it refuses the
        # request - a bad token, a missing field. The verdict is in the body,
        # so a status-only check reports success on a message never sent.
        try:
            data = r.json()
        except Exception:
            return True, "webhook"
        if isinstance(data, dict) and data.get("ok") is False:
            return False, f"webhook rejected: {str(data.get('error'))[:60]}"
        return True, "webhook"
    except Exception as e:
        return False, f"webhook {type(e).__name__}"[:60]


def _post(url_unused: str, to: str, subject: str, html: str) -> tuple[bool, str]:
    """Send by whichever transport is configured. `url_unused` is kept so the
    call sites do not change; the transport decides for itself."""
    for send in (_send_resend, _send_webhook):
        ok, detail = send(to, subject, html)
        if ok:
            return True, detail
        if detail not in ("no key", "no webhook"):
            return False, detail          # configured but failing - say which
    return False, "no sender configured"


def sender_configured() -> str | None:
    if os.environ.get("RESEND_API_KEY", "").strip():
        return "Resend"
    if os.environ.get("LEAD_WEBHOOK_URL", "").strip():
        return "webhook"
    return None


def send_report(email: str, form: dict, res: dict, comp: list[dict], gbp: dict,
                cause: str) -> dict:
    """Send the report to the prospect and a copy to James. Reports each leg
    separately - the prospect's copy failing matters more than James's."""
    if not sender_configured():
        return {"prospect": (False, "no sender configured"),
                "owner": (False, "no sender configured")}
    url = ""
    owner_to = os.environ.get("LEAD_EMAIL_TO", "hello@jameschase.co")
    f2 = dict(form, email=email)
    biz = form.get("business", "your business")
    return {
        "prospect": _post(url, email,
                          f"Your AI visibility scan - {biz}",
                          report_html(f2, res, comp, gbp, cause, for_owner=False)),
        "owner": _post(url, owner_to,
                       f"[scan] {biz} - {res['hits']}/{res['slots']} - {email}",
                       report_html(f2, res, comp, gbp, cause, for_owner=True)),
    }


def lead_count() -> int:
    try:
        c = _conn()
        n = c.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        c.close()
        return n
    except Exception:
        return 0


def export_leads(path: str = "data/scan_leads.csv") -> str:
    import csv
    c = _conn()
    rows = c.execute("SELECT * FROM leads ORDER BY created_at DESC").fetchall()
    cols = [d[0] for d in c.execute("SELECT * FROM leads LIMIT 0").description]
    c.close()
    p = Path(__file__).parent / path
    p.parent.mkdir(parents=True, exist_ok=True)
    with io.open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    return str(p)


if __name__ == "__main__":
    print(f"  leads captured: {lead_count()}")
    if lead_count():
        print(f"  exported: {export_leads()}")
