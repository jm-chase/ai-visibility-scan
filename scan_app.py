"""
scan_app.py — the free AI-visibility scan, as a Streamlit app.

    streamlit run scan_app.py

Design decisions, and why:

  NO SCORE. A competitor's report gives a practice with a 5.0 rating and 140
  reviews a "41/100". That number is tuned so everyone looks like they are
  losing, and it collapses the moment a sharp prospect asks how it is
  calculated. This reports a FRACTION — "named in 2 of 12 question-engine
  slots" — which is verifiable, arguable, and survives scrutiny.

  NAMED COMPETITORS, VERBATIM. The emotional payload is not a gauge, it is
  reading the model's own words listing four rivals and not you.

  THE DIAGNOSED CAUSE. Everyone else reports "not mentioned". Only this reports
  WHY — a blocked AI crawler is a same-afternoon fix and a far better opening
  than a score.

  PARTIAL REVEAL, THEN A CALL. The free scan runs a real subset and says plainly
  what is held back. For a regulated-healthcare engagement a 20-minute call is a
  normal ask; a self-serve PDF is not the constraint.

Credits are real money, so the free scan is capped and the cap is shown.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import os

import streamlit as st

# Streamlit Cloud supplies secrets through st.secrets, not the environment.
# config.py reads environment variables, so bridge them across before it is
# imported - after that point the key lookup is already resolved.
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:
    pass                      # no secrets file locally, which is fine

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "prospecting"))

import config                                    # noqa: E402
import tech_stack                                # noqa: E402
import sources                                   # noqa: E402
from prospecting import market_scan as ms        # noqa: E402


# ── Spend guard ───────────────────────────────────────────────────────────────
# A public scan button is an uncapped spend surface: one bot overnight is a
# four-figure bill. The cap is deliberately crude - it counts scans, not tokens,
# because an exact figure is not needed to stop the failure mode.
#
# The counter lives in a file. On an ephemeral host it resets when the container
# restarts, so the true worst case is one cap per restart rather than per day.
# That is acceptable for a $1 ceiling and not acceptable for a $100 one.

DAILY_CAP_USD = 1.00
COST_PER_SCAN = 0.08          # 3 queries x 2 engines, web-grounded, measured high
SESSION_MAX = 2               # scans per browser session
LEDGER = Path(__file__).parent / "data" / "scan_spend.json"


def _ledger() -> dict:
    today = date.today().isoformat()
    try:
        d = json.loads(LEDGER.read_text())
        if d.get("date") == today:
            return d
    except Exception:
        pass
    return {"date": today, "scans": 0, "spend": 0.0}


def spend_today() -> tuple[float, int]:
    d = _ledger()
    return float(d["spend"]), int(d["scans"])


def record_scan() -> None:
    d = _ledger()
    d["scans"] += 1
    d["spend"] = round(d["spend"] + COST_PER_SCAN, 4)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(d))


def budget_left() -> float:
    return round(DAILY_CAP_USD - spend_today()[0], 4)


FREE_QUERIES = 3          # per scan; engines multiply this
FREE_ENGINES = ("chatgpt", "perplexity")

# Question sets per vertical. These are DECISION-STAGE questions — the ones a
# buyer asks right before they choose — not awareness content. Paid-search
# conversion data is what tells you which phrasing belongs here.
QUERY_SETS = {
    "Bariatric surgery": [
        "best bariatric surgeon in {city}",
        "where should I get weight loss surgery near {city}",
        "top rated gastric sleeve surgeon {city} {state}",
        "bariatric surgery {city} reviews",
    ],
    "Hair restoration": [
        "best hair transplant clinic in {city}",
        "where to get a hair transplant near {city}",
        "top rated hair restoration surgeon {state}",
        "best FUE hair transplant {state}",
    ],
    "Fertility / IVF": [
        "best fertility clinic in {city}",
        "top IVF doctor near {city}",
        "best reproductive endocrinologist {city} {state}",
        "fertility clinic {city} success rates",
    ],
    "Dermatology / aesthetics": [
        "best dermatologist in {city}",
        "top rated med spa near {city}",
        "best cosmetic dermatologist {city} {state}",
    ],
    "Dental": [
        "best dentist in {city}",
        "good dentists near {city}",
        "top rated dentist in {city} {state}",
    ],
    "Franchise / multi-location": [
        "best {niche} in {city}",
        "top rated {niche} near {city}",
        "who is the best {niche} in {city} {state}",
    ],
}

# Typical customer value, used only to frame the revenue range. Shown to the
# user as an assumption they can override — never presented as a finding.
DEFAULT_VALUE = {
    "Bariatric surgery": (12000, 25000),
    "Hair restoration": (6000, 15000),
    "Fertility / IVF": (15000, 30000),
    "Dermatology / aesthetics": (1500, 6000),
    "Dental": (5000, 15000),
    "Franchise / multi-location": (500, 3000),
}


# ── Scan ──────────────────────────────────────────────────────────────────────

def run_scan(business: str, url: str, city: str, state: str,
             vertical: str, niche_word: str, on_step=None) -> dict:
    """Run the free scan, asking every engine at once.

    Sequentially this took 43 seconds for six calls. They are independent HTTP
    requests that spend nearly all of that waiting, so running them together
    costs the slowest single call - about 10 - rather than their sum. That is a
    bigger win than any amount of progress animation, because the best way to
    cover a wait is not to have one.

    on_step(fraction, label) still narrates, now driven by completions rather
    than by position in a loop.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    queries = [q.format(city=city, state=state, niche=niche_word)
               for q in QUERY_SETS[vertical]][:FREE_QUERIES]
    loc = ms._location(city, state)
    ENGINE_NAME = {"chatgpt": "ChatGPT", "perplexity": "Perplexity", "gemini": "Gemini"}

    jobs = [(q, e) for q in queries for e in FREE_ENGINES]
    total = len(jobs) + (1 if url else 0)
    done = 0

    def step(label: str) -> None:
        if on_step:
            on_step(min(done / max(total, 1), 1.0), label)

    step(f"Asking {len(FREE_ENGINES)} engines {len(queries)} questions, all at once...")

    def ask(job):
        q, e = job
        try:
            return job, ms._default_query_fn(e, q, loc)
        except Exception as ex:
            return job, {"error": type(ex).__name__}

    results: dict = {}
    # One worker per call: these are I/O-bound, so the pool is sized to the
    # work rather than to the CPU.
    with ThreadPoolExecutor(max_workers=len(jobs) or 1) as pool:
        futures = [pool.submit(ask, j) for j in jobs]
        for fut in as_completed(futures):
            job, r = fut.result()
            results[job] = r
            done += 1
            q, e = job
            named = [] if (not r or "error" in r) else ms.extract_names(r.get("text", ""))
            step(f"{ENGINE_NAME.get(e, e)} answered “{q}” "
                 f"— {len(named)} businesses named")

    slots, hits, rows = 0, 0, []
    for q in queries:
        row = {"query": q, "engines": {}}
        for engine in FREE_ENGINES:
            slots += 1
            r = results.get((q, engine))
            if not r or "error" in r:
                row["engines"][engine] = {"error": (r or {}).get("error", "no_data")}
                continue
            text = r.get("text", "")
            named = ms.extract_names(text)
            you = any(ms._match(business, n) for n in named)
            hits += 1 if you else 0
            row["engines"][engine] = {"named": named, "you": you, "excerpt": text[:400],
                                      "sources": r.get("sources", [])}
        rows.append(row)

    crawlers = {}
    if url:
        host = url.split("//")[-1].split("/")[0]
        step(f"Checking whether the AI crawlers can reach {host}")
        try:
            crawlers = tech_stack.check_ai_crawlers(url)
        except Exception as e:
            crawlers = {"status": f"unreachable ({type(e).__name__})"}
        done += 1
    step("Cross-referencing who was named against your business")

    # The sources behind those answers are the placement targets: the finite set
    # of pages that decide this market, ranked by where the business is absent.
    placements = sources.harvest(rows, own_domain=url)

    return {"business": business, "queries": queries, "rows": rows,
            "slots": slots, "hits": hits, "crawlers": crawlers,
            "placements": placements,
            "total_queries": len(QUERY_SETS[vertical])}


def diagnose(res: dict) -> tuple[str, str]:
    """The single most actionable cause, and its severity."""
    c = res.get("crawlers") or {}
    if c.get("blocked"):
        who = ", ".join(c["blocked"])
        route = (" through a blanket rule that almost certainly was not aimed at them"
                 if c.get("via_wildcard") else "")
        return ("blocked",
                f"Your robots.txt blocks {who}{route}. These engines cannot read your "
                f"site at all, so no amount of content or schema work will surface you. "
                f"This is a same-afternoon fix and it has to happen before anything else.")
    if c.get("partial"):
        return ("partial",
                f"Your robots.txt restricts {', '.join(c['partial'])} on some paths. "
                f"Worth a human read to confirm your service pages are not caught.")
    if res["hits"] == 0:
        return ("invisible",
                "The crawlers can reach you, so this is not an access problem. The "
                "engines have simply not built enough of a picture of you to name you "
                "— which is an entity and evidence problem, and it is fixable.")
    return ("present",
            "You appear, but not consistently. Inconsistency across engines usually "
            "means the evidence is thin rather than absent.")

# ── UI ────────────────────────────────────────────────────────────────────────
# Matches jameschase.co: JetBrains Mono, ink on paper, findings on full-bleed
# dark. A prospect arriving from a QR code should not feel handed off.
#
# Free: the fraction, and the two rivals beating you, by name.
# Gated: the rest of the field, the review evidence, the cause, the worth.
# The gate sits there because the SerpAPI lookups behind it cost a credit each
# and because a scan that captures nothing is a page view.

import streamlit.components.v1 as components                     # noqa: E402
import scan_report as R                                          # noqa: E402

st.set_page_config(page_title="Does the AI name you? — James Chase",
                   page_icon="◐", layout="centered")

BRAND = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap');
  html, body, [class*="css"], .stApp, button, input, select, textarea {
    font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Consolas, monospace !important;
  }
  .stApp { background: #FDFDFD; }
  #MainMenu, footer, header, [data-testid="stToolbar"] { visibility: hidden; }
  .block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 820px; }

  .jc-eyebrow { font-size: 11px; letter-spacing: .16em; text-transform: uppercase;
                color: #6B7280; margin-bottom: 10px; }
  .jc-h1 { font-size: 34px; line-height: 1.15; letter-spacing: -.035em; font-weight: 700;
           color: #15181C; margin: 0 0 14px; }
  .jc-sub { font-size: 15px; line-height: 1.62; color: #5A6169; margin: 0 0 24px; }
  .jc-lbl { font-size: 10.5px; letter-spacing: .15em; text-transform: uppercase;
            color: #6B7280; margin: 26px 0 10px; }

  /* Full-bleed dark band, the way the site carries its claims. */
  .jc-band { background: #15181C; color: #F2F3F4; padding: 34px 38px;
             margin: 14px -38px 22px; border-radius: 2px; }
  @media (max-width: 860px) { .jc-band { margin-left: -1rem; margin-right: -1rem;
                                         padding: 26px 20px; } }
  .jc-band .jc-lbl { color: #8B949E; margin-top: 0; }
  .jc-frac { font-size: 58px; line-height: 1; font-weight: 700; color: #A8F0C6;
             letter-spacing: -.04em; }
  .jc-frac span { font-size: 22px; color: #8B949E; font-weight: 400; letter-spacing: 0; }
  .jc-note { font-size: 13.5px; line-height: 1.6; color: #9AA3AB; margin-top: 14px; }

  /* Comparison rows - same denominator, so no scale is invented. */
  .jc-row { display: grid; grid-template-columns: 1fr 78px; gap: 14px; align-items: center;
            margin-bottom: 9px; }
  .jc-name { font-size: 13.5px; color: #E6E9EC; overflow: hidden; text-overflow: ellipsis;
             white-space: nowrap; }
  .jc-name.you { color: #A8F0C6; font-weight: 700; }
  .jc-track { grid-column: 1 / -1; height: 8px; background: #2A2F35; border-radius: 4px;
              overflow: hidden; margin-top: -4px; margin-bottom: 4px; }
  .jc-fill { height: 100%; background: #6B7683; }
  .jc-fill.you { background: #A8F0C6; }
  .jc-val { font-size: 12.5px; color: #9AA3AB; text-align: right; white-space: nowrap; }

  .jc-why { border-left: 3px solid #1f6b47; padding: 4px 0 4px 18px; margin: 0 0 20px;
            font-size: 15px; line-height: 1.62; color: #2A2F35; }
  .jc-why.bad { border-left-color: #C4442E; }
  .jc-ev { background: #F2F3F4; border: 1px solid #E3E5E8; border-radius: 8px;
           padding: 18px 20px; margin-bottom: 20px; font-size: 14.5px; line-height: 1.6;
           color: #2A2F35; }
  .jc-ev b { color: #15181C; }

  .jc-locked { background: #F2F3F4; border: 1px dashed #C6CBD1; border-radius: 8px;
               padding: 22px 24px; margin: 6px 0 14px; }
  .jc-locked h4 { margin: 0 0 10px; font-size: 16px; color: #15181C; }
  .jc-locked ul { margin: 0; padding-left: 18px; font-size: 14px; line-height: 1.8;
                  color: #5A6169; }

  .stButton > button, .stFormSubmitButton > button {
    background: #15181C !important; color: #FDFDFD !important; border: 0 !important;
    border-radius: 6px !important; font-weight: 600 !important; min-height: 48px;
  }
  .stLinkButton > a {
    background: #A8F0C6 !important; color: #15181C !important; border: 0 !important;
    border-radius: 6px !important; font-weight: 700 !important; min-height: 48px;
  }
  @media (max-width: 640px) {
    .jc-h1 { font-size: 25px; } .jc-frac { font-size: 42px; }
    .block-container { padding-left: 1rem; padding-right: 1rem; }
  }
</style>
"""
st.markdown(BRAND, unsafe_allow_html=True)

CAL_LINK = "james-chase-topd7c/strategy-call"
CAL = f"https://cal.com/{CAL_LINK}"

st.markdown('<div class="jc-eyebrow">James Chase &middot; AI visibility</div>'
            '<div class="jc-h1">Does the AI name you?</div>'
            '<div class="jc-sub">A free scan of what ChatGPT and Perplexity say when someone '
            'asks for the best in your market. No score and no gauge &mdash; a fraction, the '
            'competitors named ahead of you, and why.</div>', unsafe_allow_html=True)

_keys = {"OpenAI": config.openai_key(), "Perplexity": config.perplexity_key()}
_missing = [n for n, v in _keys.items() if not v]
if _missing:
    st.error(f"**Scan unavailable — missing {', '.join(_missing)} API key(s).**")
    st.markdown(
        "Hosted on Streamlit Cloud: **Manage app &rarr; Settings &rarr; Secrets**, then add\n\n"
        "```toml\nOPENAI_API_KEY = \"sk-...\"\nPERPLEXITY_API_KEY = \"pplx-...\"\n```\n\n"
        "It reboots on its own. Locally: `~/.openai/credentials.json`.")
    try:
        st.caption(f"Secrets visible to the app: {sorted(st.secrets.keys()) or 'none'}")
    except Exception:
        st.caption("No secrets store is configured for this app.")
    st.stop()


def bars(rows, limit=None):
    """Comparison rows. Same denominator for everyone, so no scale is invented."""
    out = []
    for r in (rows[:limit] if limit else rows):
        pct = (r["hits"] / r["slots"] * 100) if r["slots"] else 0
        you = " you" if r["you"] else ""
        out.append(
            f'<div class="jc-row"><div class="jc-name{you}">{r["name"]}</div>'
            f'<div class="jc-val">{r["hits"]} of {r["slots"]}</div></div>'
            f'<div class="jc-track"><div class="jc-fill{you}" style="width:{pct:.0f}%"></div></div>')
    return "".join(out)


# ── form ──────────────────────────────────────────────────────────────────────
with st.form("scan"):
    c1, c2 = st.columns(2)
    business = c1.text_input("Business name", placeholder="Raleigh Bariatric Center")
    url = c2.text_input("Website", placeholder="https://example.com")
    c3, c4, c5 = st.columns([2, 1, 2])
    city = c3.text_input("City", value="Raleigh")
    state = c4.text_input("State", value="NC")
    vertical = c5.selectbox("Vertical", list(QUERY_SETS))
    niche_word = ""
    if vertical == "Franchise / multi-location":
        niche_word = st.text_input("Category word", placeholder="orthodontist, gym, med spa")
    lo, hi = DEFAULT_VALUE[vertical]
    v1, v2 = st.columns(2)
    val_lo = v1.number_input("A customer is worth, low ($)", value=lo, step=500)
    val_hi = v2.number_input("A customer is worth, high ($)", value=hi, step=500)
    go = st.form_submit_button("Run the free scan", type="primary")

st.caption(f"{FREE_QUERIES} questions across {len(FREE_ENGINES)} engines, asked live. "
           f"Takes about 15 seconds &mdash; the engines are being queried in real "
           f"time, not read from a cache.")

if go:
    if not business.strip():
        st.warning("Business name is required - it is what we look for in the answers.")
        st.stop()
    if budget_left() < COST_PER_SCAN:
        st.warning("**The free scan has hit today's limit.** It runs on live API calls "
                   "and the daily budget is spent.")
        st.link_button("Book a 20-minute review", CAL, type="primary")
        st.stop()
    if st.session_state.get("scans_run", 0) >= SESSION_MAX:
        st.info(f"That is {SESSION_MAX} scans this session, which is the limit.")
        st.link_button("Book a 20-minute review", CAL)
        st.stop()

    bar = st.progress(0.0, text="Starting the scan...")
    res = run_scan(business.strip(), url.strip(), city.strip(), state.strip(),
                   vertical, niche_word.strip(),
                   on_step=lambda f, l: bar.progress(f, text=l))
    bar.empty()
    record_scan()
    st.session_state["scans_run"] = st.session_state.get("scans_run", 0) + 1
    st.session_state["res"] = res
    st.session_state["form"] = {"business": business, "website": url, "city": city,
                                "state": state, "vertical": vertical,
                                "val_lo": val_lo, "val_hi": val_hi}
    st.session_state["unlocked"] = False

# ── result ────────────────────────────────────────────────────────────────────
res = st.session_state.get("res")
if res:
    form = st.session_state["form"]
    comp = R.comparative(res)
    kind, why = diagnose(res)
    ahead = [r for r in comp if not r["you"] and r["hits"] > res["hits"]]

    st.markdown(
        f'<div class="jc-band"><div class="jc-lbl">Named in</div>'
        f'<div class="jc-frac">{res["hits"]} <span>of {res["slots"]} answer slots</span></div>'
        f'<div class="jc-note">{FREE_QUERIES} questions &times; {len(FREE_ENGINES)} engines. '
        f'Not a score &mdash; a count you can check. '
        f'{len(ahead)} competitor{"s" if len(ahead) != 1 else ""} placed ahead of you.'
        f'</div>'
        f'<div class="jc-lbl" style="margin-top:26px">Same questions, same engines</div>'
        f'{bars(comp, 3 if not st.session_state.get("unlocked") else None)}'
        f'</div>', unsafe_allow_html=True)

    if not st.session_state.get("unlocked"):
        remaining = max(len(comp) - 3, 0)
        st.markdown(
            f'<div class="jc-locked"><h4>Still held back</h4><ul>'
            f'<li>{remaining} more competitor{"s" if remaining != 1 else ""} the engines named</li>'
            f'<li>Their Google review counts against yours &mdash; usually the reason they win</li>'
            f'<li>Why you are absent, specifically</li>'
            f'<li>What the gap is worth at your own customer value</li>'
            f'<li>The answers verbatim</li>'
            f'</ul></div>', unsafe_allow_html=True)
        with st.form("unlock"):
            email = st.text_input("Work email", placeholder="you@practice.com",
                                  label_visibility="collapsed")
            ok = st.form_submit_button("Show me the rest", type="primary")
        if ok:
            if not R.valid_email(email):
                st.warning("That does not look like an email address.")
            else:
                R.save_lead(email, form, res, kind)
                st.session_state["email"] = email.strip()
                st.session_state["unlocked"] = True
                st.rerun()
        st.caption("One email, no list. I send you the full report and nothing else.")

    else:
        names = [r["name"] for r in comp]
        if "gbp" not in st.session_state or not st.session_state.get("report_sent"):
            bar2 = st.progress(0.0, text="Looking up Google review counts...")
            if "gbp" not in st.session_state:
                st.session_state["gbp"] = R.gbp_evidence(
                    names, form["city"], form["state"],
                    on_step=lambda f, l: bar2.progress(f * 0.8, text=l))
            bar2.progress(0.85, text="Building your report...")
            if not st.session_state.get("report_sent"):
                st.session_state["report_sent"] = R.send_report(
                    st.session_state.get("email", ""), form, res,
                    comp, st.session_state["gbp"], kind)
            bar2.progress(1.0, text="Sent.")
            bar2.empty()
        gbp = st.session_state["gbp"]
        _sent = st.session_state.get("report_sent") or {}
        _p_ok = (_sent.get("prospect") or (False, ""))[0]
        _o_ok = (_sent.get("owner") or (False, ""))[0]
        if _p_ok:
            st.success(f"Report emailed to **{st.session_state.get('email','')}**. "
                       "It has everything below, so you can read it later or forward it.")
        else:
            st.warning(
                f"**Could not email the report** "
                f"({(_sent.get('prospect') or (0, 'not configured'))[1]}). Everything is "
                "below. Set `LEAD_WEBHOOK_URL` in secrets so this stops failing silently.")
        if not _o_ok:
            st.caption("Owner copy not delivered either - the lead is only in the local "
                       "database, which an ephemeral host wipes on restart.")

        gap = R.review_gap(comp, gbp)
        if gap and gap.get("you") is not None:
            st.markdown('<div class="jc-lbl">Why they win</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="jc-ev"><b>{gap["top_name"]}</b> holds '
                f'<b>{gap["top_reviews"]:,} Google reviews</b>. You hold '
                f'<b>{gap["you"]:,}</b>. The engines read review volume and directory '
                f'presence as evidence a business is real and preferred &mdash; that gap '
                f'is doing more work than any page on your site.</div>',
                unsafe_allow_html=True)
        elif gap:
            st.markdown('<div class="jc-lbl">Why they win</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="jc-ev"><b>{gap["top_name"]}</b> holds '
                f'<b>{gap["top_reviews"]:,} Google reviews</b>. I could not match your '
                f'business on Google Maps, which is itself worth checking &mdash; if Maps '
                f'cannot find you cleanly, neither can the models.</div>',
                unsafe_allow_html=True)

        st.markdown('<div class="jc-lbl">Why you are absent</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="jc-why{" bad" if kind == "blocked" else ""}">{why}</div>',
                    unsafe_allow_html=True)

        if res["hits"] < res["slots"]:
            share = (res["slots"] - res["hits"]) / res["slots"]
            st.markdown('<div class="jc-lbl">What the gap is worth</div>',
                        unsafe_allow_html=True)
            st.markdown(
                f'<div class="jc-ev">One customer a month from questions you are absent '
                f'from is <b>${form["val_lo"]*12:,.0f}&ndash;${form["val_hi"]*12:,.0f} a '
                f'year</b> at your own customer value. You are missing from '
                f'<b>{share:.0%}</b> of the slots checked.<br><span style="color:#6B7280">'
                f'Stated as a multiple of one customer on purpose. Anyone quoting a precise '
                f'revenue figure off a visibility scan is guessing.</span></div>',
                unsafe_allow_html=True)

        with st.expander("Read what the engines actually said"):
            for row in res["rows"]:
                st.markdown(f'**"{row["query"]}"**')
                for e, d in row["engines"].items():
                    if "error" in d:
                        st.caption(f"{e}: {d['error']}")
                        continue
                    st.caption(f"{e} — {'names you' if d['you'] else 'does not name you'}")
                    st.text(d["excerpt"].strip()[:380] + "...")
                st.divider()

        st.markdown(
            '<div class="jc-band"><div class="jc-lbl">What the free scan still does not cover</div>'
            f'<div class="jc-note" style="color:#D6DADE;font-size:14.5px">'
            f'The other {max(res["total_queries"] - FREE_QUERIES, 0)}+ questions tracked, '
            f'Gemini as a third engine, every cause ranked by what moves first, your full '
            f'Google Business Profile against each rival, and what to fix in week one, '
            f'week two, week three.</div></div>', unsafe_allow_html=True)
        st.markdown('<div class="jc-lbl">Pick a time</div>', unsafe_allow_html=True)
        st.caption("I run the full scan before we speak, so the call starts with your "
                   "results rather than a pitch.")
        # Inline rather than a link: sending someone to another domain to book,
        # right after showing them they are invisible, loses most of them.
        components.html(f"""
<div style="width:100%;height:620px;overflow:auto" id="cal-inline"></div>
<script type="text/javascript">
(function (C, A, L) {{ let p = function (a, ar) {{ a.q.push(ar); }}; let d = C.document;
C.Cal = C.Cal || function () {{ let cal = C.Cal; let ar = arguments;
if (!cal.loaded) {{ cal.ns = {{}}; cal.q = cal.q || [];
d.head.appendChild(d.createElement("script")).src = A; cal.loaded = true; }}
if (ar[0] === L) {{ const api = function () {{ p(api, arguments); }};
const namespace = ar[1]; api.q = api.q || [];
if(typeof namespace === "string"){{cal.ns[namespace] = cal.ns[namespace] || api;
p(cal.ns[namespace], ar);p(cal, ["initNamespace", namespace]);}} else p(cal, ar); return;}}
p(cal, ar); }}; }})(window, "https://app.cal.com/embed/embed.js", "init");
Cal("init", "strategy-call", {{origin:"https://app.cal.com"}});
Cal.ns["strategy-call"]("inline", {{
  elementOrSelector:"#cal-inline",
  config: {{"layout":"month_view","useSlotsViewOnSmallScreen":"true"}},
  calLink: "{CAL_LINK}",
}});
Cal.ns["strategy-call"]("ui", {{"hideEventTypeDetails":false,"layout":"month_view"}});
</script>""", height=640, scrolling=False)
        st.caption(f"Or open it in a new tab: [{CAL}]({CAL})")
