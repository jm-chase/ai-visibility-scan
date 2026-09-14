# AI visibility scan

Asks ChatGPT and Perplexity the questions a buyer types right before they
choose, and reports whether a business is named in the answer.

Live at [scan.jameschase.co](https://scan.jameschase.co).

## What it reports, and what it refuses to

**A fraction, not a score.** "Named in 0 of 6 answer slots" is a count anyone
can check by asking an engine themselves. Scores out of 100 are tuned so
everybody looks like they are losing, and they do not survive being asked how
they were calculated.

**Competitors verbatim, on the same denominator.** Every row faces the same
questions on the same engines, so the comparison needs no weighting.

**The cause, not just the absence.** A blocked AI crawler is a same-afternoon
fix. Most reports stop at "not mentioned".

**A range, never a precise revenue figure.** Stated as a multiple of one
customer at the operator's own numbers. Anyone quoting an exact figure from a
visibility scan is guessing.

## Running it

```
pip install -r requirements.txt
streamlit run scan_app.py
```

Keys come from environment variables first, then `~/.<service>/credentials.json`:

```
OPENAI_API_KEY        required
PERPLEXITY_API_KEY    required
SERPAPI_KEY           optional - Google review counts
RESEND_API_KEY        optional - emails the report
LEAD_FROM             e.g. "James Chase <hello@example.com>"
LEAD_EMAIL_TO         where the owner copy goes
```

## Spend

A public scan button is an uncapped spend surface. Two limits sit in front of
it: `$1/day` at about `$0.08` a scan, and 2 scans per browser session. The
ledger is a file, so on a host with an ephemeral filesystem the cap resets when
the container restarts — fine at this ceiling, not fine at a larger one.

## Before pushing

```
py audit.py
```

Checks for secret-shaped strings, local paths, and names from a local
`.audit-denylist` that is deliberately untracked — written into the script, it
would publish the very names it exists to keep out.
