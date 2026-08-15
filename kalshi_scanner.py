#!/usr/bin/env python3
"""
Kalshi Tight Market Scanner

Surfaces markets with tight spreads in hard-to-model event categories:
political personalities, executive/CEO moves, celebrity events,
geopolitical developments, legal rulings, and company-specific events.

Usage:
    python kalshi_scanner.py [options]
    python kalshi_scanner.py --help
"""

import argparse
import base64
import csv
import html
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    from rich.text import Text
    RICH = True
except ImportError:
    RICH = False

# ── API ──────────────────────────────────────────────────────────────────────

API_BASE = "https://external-api.kalshi.com/trade-api/v2"
PAGE_LIMIT = 200
REQUEST_DELAY = 0.15  # seconds between pages

KALSHI_KEY_ID = "55e7d77e-2965-49ca-af94-9835343e83ca"


def _load_private_key():
    raw = os.environ.get("KALSHI_PRIVATE_KEY", "")
    if not raw:
        return None
    pem = raw.encode()
    # GitHub secrets collapse newlines; restore them if needed
    if b"\\n" in pem:
        pem = pem.replace(b"\\n", b"\n")
    try:
        return serialization.load_pem_private_key(pem, password=None)
    except Exception as e:
        print(f"Failed to load private key: {e}", file=sys.stderr)
        return None


def _auth_headers(method: str, path: str) -> dict:
    key = _load_private_key()
    if key is None:
        return {}
    ts_ms = str(int(time.time() * 1000))
    msg = (ts_ms + method.upper() + path).encode()
    sig = key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }

# ── Category definitions ─────────────────────────────────────────────────────
# Matched against lowercased "title + subtitle" of each market.
# Add or remove keywords freely to tune signal quality.

INTERESTING_CATEGORIES: dict[str, list[str]] = {
    "political_personality": [
        "trump", "biden", "harris", "pelosi", "desantis", "obama", "newsom",
        "mcconnell", "rfk", "vivek", "pence", "gaetz", "bannon", "schumer",
        "ocasio-cortez", " aoc ", "rubio", "ted cruz", "rand paul", "tulsi",
        "marjorie", "mtg", "mike johnson", "hakeem jeffries",
    ],
    "executive_ceo": [
        "ceo", "cfo", "chief executive", "chief financial", "resigns",
        "resignation", "departure", "steps down", "ousted", "fired",
        "appointed ceo", "new ceo", "successor", "board chair",
    ],
    "celebrity": [
        "oscar", "grammy", "emmy", "golden globe", "taylor swift", "beyoncé",
        "beyonce", "kanye", "kardashian", "award show", "box office",
        "album sales", "movie gross", "elon musk",
    ],
    "geopolitical": [
        "ukraine", "russia", "china", "taiwan", "nato", "sanction",
        "invasion", "ceasefire", "nuclear", "iran", "north korea", "israel",
        "gaza", "middle east", "coup", "conflict", "regime", "war ends",
        "peace deal", "annexation",
    ],
    "legal_court": [
        "supreme court", "court ruling", "verdict", "trial", "lawsuit",
        "indicted", "convicted", "sentenced", "appeals court", "doj",
        "sec charges", "criminal charges", "plea deal", "acquitted",
        "extradited", "pardon",
    ],
    "company_event": [
        "merger", "acquisition", "ipo", "bankruptcy", "layoffs",
        "spin-off", "buyout", "takeover", "product recall", "sec fine",
        "antitrust", "class action", "whistleblower",
    ],
}

# Markets where established models and large-scale public data exist.
# These are flagged (not removed) so you can filter them out with --exclude-modelable.
MODELABLE_KEYWORDS: list[str] = [
    "nba", "nfl", "mlb", "nhl", "ncaa", "super bowl", "world series",
    "champions league", "premier league", "federal reserve", "fed rate",
    "fomc", " cpi ", "gdp ", "unemployment rate", "nonfarm payroll",
    "jobs report", "pce ", "core inflation",
]

MODELABLE_CATEGORIES: set[str] = {"Sports"}

# ── Data fetching ─────────────────────────────────────────────────────────────

# As of the API's dollar-denominated schema, per-market category/subtitle were
# removed from GET /markets. Category now lives on the parent event, so we
# fetch GET /events?with_nested_markets=true and flatten each event's markets,
# stamping the event's category/title/sub_title onto every child market.

# Kalshi category strings to skip entirely during fetch (saves time + memory).
# Sports: player props, game lines, and other high-volume sports contracts.
# Crypto: rolling 15-minute/hourly price-direction markets, extremely high churn.
# Climate and Weather: established forecasting models exist, low signal here.
# Mentions: "will X say Y" word-count markets, low signal.
SKIP_CATEGORIES: set[str] = {"Sports", "Crypto", "Climate and Weather", "Mentions"}


def fetch_all_markets(verbose: bool = True, skip_categories: set[str] = SKIP_CATEGORIES, max_pages: Optional[int] = None) -> list[dict]:
    """Paginate through all open Kalshi events, flatten nested markets, and return raw API records."""
    markets: list[dict] = []
    cursor: Optional[str] = None
    page = 0
    skipped = 0

    while True:
        params: dict = {"limit": PAGE_LIMIT, "status": "open", "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor

        try:
            path = "/trade-api/v2/events"  # path used for RSA signature, must match URL path
            headers = _auth_headers("GET", path)
            resp = requests.get(f"{API_BASE}/events", params=params, headers=headers, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"API error: {e}", file=sys.stderr)
            break

        data = resp.json()
        events = data.get("events", [])

        kept: list[dict] = []
        for event in events:
            category = event.get("category", "")
            nested = event.get("markets", [])
            if skip_categories and category in skip_categories:
                skipped += len(nested)
                continue
            for m in nested:
                m = dict(m)
                m["category"] = category
                m["_event_title"] = event.get("title", "")
                m["_event_sub_title"] = event.get("sub_title", "")
                kept.append(m)

        markets.extend(kept)
        page += 1

        if verbose:
            print(
                f"  Page {page:>3}: {len(kept):>3} kept  (total kept: {len(markets):,}  skipped: {skipped:,})",
                file=sys.stderr,
            )

        cursor = data.get("cursor")
        if not cursor or not events:
            break
        if max_pages and page >= max_pages:
            print(f"  Stopped at page limit ({max_pages})", file=sys.stderr)
            break

        time.sleep(REQUEST_DELAY)

    return markets


# ── Metric computation ────────────────────────────────────────────────────────


def _dollars_to_cents(value) -> Optional[float]:
    """Convert a Kalshi '*_dollars' price string (e.g. '0.1400') to cents."""
    if value is None:
        return None
    try:
        return round(float(value) * 100, 2)
    except (TypeError, ValueError):
        return None


def compute_metrics(raw: dict) -> Optional[dict]:
    """
    Derive trading and classification metrics from a raw Kalshi market record.
    Returns None for markets without valid two-sided quotes.
    """
    if raw.get("status") != "active":
        return None

    yes_bid = _dollars_to_cents(raw.get("yes_bid_dollars"))
    yes_ask = _dollars_to_cents(raw.get("yes_ask_dollars"))

    # Require a valid, non-crossed two-sided market
    if yes_bid is None or yes_ask is None:
        return None
    if yes_bid <= 0 or yes_ask <= 0 or yes_ask < yes_bid:
        return None

    spread = round(yes_ask - yes_bid, 2)
    midpoint = (yes_bid + yes_ask) / 2
    # Relative spread normalizes for price level.
    # A 2¢ spread on a 5¢ market (40%) differs enormously from 2¢ on a 50¢ market (4%).
    relative_spread = round(spread / midpoint, 4) if midpoint > 0 else None

    # Days to expiration
    close_str = raw.get("close_time", "")
    try:
        close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        days_to_exp = (close_dt - datetime.now(timezone.utc)).days
        close_date = close_dt.strftime("%Y-%m-%d")
    except Exception:
        days_to_exp = None
        close_date = ""

    # When the market opened for trading (used to sort newest-listed-first).
    open_str = raw.get("open_time", "")
    try:
        open_dt = datetime.fromisoformat(open_str.replace("Z", "+00:00"))
        open_date = open_dt.strftime("%Y-%m-%d")
        open_timestamp = open_dt.timestamp()
    except Exception:
        open_date = ""
        open_timestamp = 0

    # The market's own title is already fully specified (e.g. "Will X win the
    # election?"), unlike the old schema where subtitle carried the specific
    # instance. Fold in the parent event's title/sub_title and the per-side
    # sub-titles for keyword matching, since a candidate's name may only
    # appear there.
    title = raw.get("title", "") or raw.get("_event_title", "")
    subtitle = raw.get("subtitle") or raw.get("yes_sub_title") or ""
    display_title = title
    title_blob = " ".join(
        str(x) for x in (
            title,
            subtitle,
            raw.get("no_sub_title", ""),
            raw.get("_event_title", ""),
            raw.get("_event_sub_title", ""),
        ) if x
    ).lower()

    # Categorize by keyword matching
    flags: list[str] = [
        cat
        for cat, keywords in INTERESTING_CATEGORIES.items()
        if any(kw in title_blob for kw in keywords)
    ]

    # Detect modelable (sports / macro) markets
    kalshi_category = raw.get("category", "")
    is_modelable = kalshi_category in MODELABLE_CATEGORIES or any(
        kw in title_blob for kw in MODELABLE_KEYWORDS
    )

    event_ticker = raw.get("event_ticker", "")
    ticker = raw.get("ticker", "")
    url = f"https://kalshi.com/markets/{event_ticker}/{ticker}"

    volume = int(round(float(raw.get("volume_fp", 0) or 0)))
    volume_24h = int(round(float(raw.get("volume_24h_fp", 0) or 0)))
    open_interest = int(round(float(raw.get("open_interest_fp", 0) or 0)))

    return {
        "ticker": ticker,
        "title": display_title[:90],
        "category": kalshi_category,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "spread": spread,
        "midpoint": round(midpoint, 1),
        "relative_spread": relative_spread,
        "volume": volume,
        "volume_24h": volume_24h,
        "open_interest": open_interest,
        "days_to_exp": days_to_exp,
        "close_date": close_date,
        "open_date": open_date,
        "open_timestamp": open_timestamp,
        "flags": flags,
        "is_modelable": is_modelable,
        "url": url,
    }


# ── Filtering ─────────────────────────────────────────────────────────────────


def apply_filters(
    markets: list[dict],
    max_spread: Optional[int],
    max_relative_spread: Optional[float],
    min_volume: Optional[int],
    min_open_interest: Optional[int],
    max_days: Optional[int],
    min_days: Optional[int],
    min_price: Optional[float],
    max_price: Optional[float],
    category: Optional[str],
    interesting_only: bool,
    exclude_modelable: bool,
) -> list[dict]:
    out = []
    for m in markets:
        if max_spread is not None and m["spread"] > max_spread:
            continue
        if max_relative_spread is not None and (
            m["relative_spread"] is None or m["relative_spread"] > max_relative_spread
        ):
            continue
        if min_volume is not None and m["volume"] < min_volume:
            continue
        if min_open_interest is not None and m["open_interest"] < min_open_interest:
            continue
        if max_days is not None and m["days_to_exp"] is not None and m["days_to_exp"] > max_days:
            continue
        if min_days is not None and m["days_to_exp"] is not None and m["days_to_exp"] < min_days:
            continue
        if min_price is not None and m["midpoint"] < min_price:
            continue
        if max_price is not None and m["midpoint"] > max_price:
            continue
        if category and category.lower() not in (m["category"] or "").lower():
            continue
        if interesting_only and not m["flags"]:
            continue
        if exclude_modelable and m["is_modelable"]:
            continue
        out.append(m)
    return out


# ── Display ───────────────────────────────────────────────────────────────────


def _fmt_cents(value: float) -> str:
    """Render a cents value without a noisy '.0' for whole numbers (sub-cent granularity exists)."""
    if value == int(value):
        return f"{int(value)}¢"
    return f"{value:.1f}¢"


def _spread_color(spread: float) -> str:
    if spread <= 1:
        return "bright_green"
    if spread <= 3:
        return "green"
    if spread <= 5:
        return "yellow"
    return "white"


def display_rich_table(markets: list[dict], title: str, max_rows: int) -> None:
    console = Console()
    table = Table(
        title=title,
        box=box.ROUNDED,
        show_lines=False,
        header_style="bold cyan",
        title_style="bold white",
        padding=(0, 1),
    )

    table.add_column("Spr", style="bold", justify="right", width=5)
    table.add_column("Rel%", justify="right", width=6)
    table.add_column("Mid", justify="right", width=5)
    table.add_column("Bid", justify="right", width=5)
    table.add_column("Ask", justify="right", width=5)
    table.add_column("Vol", justify="right", width=9)
    table.add_column("OI", justify="right", width=9)
    table.add_column("Exp", width=10)
    table.add_column("Flags", width=22)
    table.add_column("Title")

    shown = markets[:max_rows]
    for m in shown:
        color = _spread_color(m["spread"])
        rel = f"{m['relative_spread']*100:.1f}%" if m["relative_spread"] is not None else "—"
        flags_str = ", ".join(f.replace("_", " ") for f in m["flags"]) if m["flags"] else ""
        modelable_suffix = " [dim](~)[/dim]" if m["is_modelable"] else ""
        days = m["days_to_exp"]
        exp_str = m["close_date"] if days is None else f"{m['close_date']} ({days}d)"

        table.add_row(
            Text(_fmt_cents(m['spread']), style=color),
            rel,
            _fmt_cents(m['midpoint']),
            _fmt_cents(m['yes_bid']),
            _fmt_cents(m['yes_ask']),
            f"{m['volume']:,}",
            f"{m['open_interest']:,}",
            exp_str,
            flags_str,
            m["title"] + modelable_suffix,
        )

    console.print()
    console.print(table)
    console.print(
        f"  [dim]Showing {len(shown):,} of {len(markets):,} markets. "
        f"(~) = likely modelable market.[/dim]"
    )


def display_plain_table(markets: list[dict], title: str, max_rows: int) -> None:
    print(f"\n{'─'*10} {title} {'─'*10}")
    header = f"{'SPR':>4} {'MID':>5} {'BID':>4} {'ASK':>4} {'VOL':>9} {'OI':>9} {'EXP':>10}  TITLE"
    print(header)
    print("─" * 130)
    for m in markets[:max_rows]:
        flags = "|".join(m["flags"]) if m["flags"] else ""
        modelable = "(~)" if m["is_modelable"] else "   "
        print(
            f"{_fmt_cents(m['spread']):>4} {_fmt_cents(m['midpoint']):>5} "
            f"{_fmt_cents(m['yes_bid']):>4} {_fmt_cents(m['yes_ask']):>4}"
            f" {m['volume']:>9,} {m['open_interest']:>9,} {m['close_date']:>10}"
            f"  {modelable} {m['title'][:60]:<60}  {flags}"
        )
    print(f"\nShowing {min(len(markets), max_rows):,} of {len(markets):,} markets.")


def display(markets: list[dict], title: str, max_rows: int) -> None:
    if RICH:
        display_rich_table(markets, title, max_rows)
    else:
        display_plain_table(markets, title, max_rows)


# ── CSV export ────────────────────────────────────────────────────────────────


def export_csv(markets: list[dict], path: str) -> None:
    if not markets:
        print("Nothing to export.")
        return
    fields = [k for k in markets[0] if k != "flags"] + ["flags"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for m in markets:
            row = {**m, "flags": "|".join(m["flags"])}
            writer.writerow(row)
    print(f"\nExported {len(markets):,} markets → {path}")


# ── HTML dashboard export ────────────────────────────────────────────────────

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"

_DASHBOARD_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
__FONT_CSS__

:root {
  --bg: #f2f4f6;
  --surface: #ffffff;
  --surface-2: #e7eaef;
  --text: #14213d;
  --text-muted: #5b6478;
  --border: #d8dce3;
  --accent: #a8701f;
  --accent-ink: #ffffff;
  --tight: #1e8e5a;
  --mid: #a8701f;
  --wide: #b5442e;
  --row-hover: #eef1f5;
  --font-display: 'Big Shoulders Display', 'Arial Narrow', sans-serif;
  --font-body: 'IBM Plex Sans', -apple-system, sans-serif;
  --font-mono: 'IBM Plex Mono', ui-monospace, 'SF Mono', Menlo, monospace;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0b1220;
    --surface: #121b2e;
    --surface-2: #1a2540;
    --text: #e9ecf1;
    --text-muted: #8d96ac;
    --border: #263252;
    --accent: #e8a33d;
    --accent-ink: #1a1204;
    --tight: #4ade80;
    --mid: #e8a33d;
    --wide: #ff6b57;
    --row-hover: #17213a;
  }
}

:root[data-theme="dark"] {
  --bg: #0b1220;
  --surface: #121b2e;
  --surface-2: #1a2540;
  --text: #e9ecf1;
  --text-muted: #8d96ac;
  --border: #263252;
  --accent: #e8a33d;
  --accent-ink: #1a1204;
  --tight: #4ade80;
  --mid: #e8a33d;
  --wide: #ff6b57;
  --row-hover: #17213a;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-body);
  font-size: 14px;
  line-height: 1.45;
}

.masthead {
  padding: 28px clamp(16px, 4vw, 40px) 20px;
  border-bottom: 1px solid var(--border);
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px 32px;
}

.wordmark {
  font-family: var(--font-display);
  font-weight: 800;
  font-size: clamp(28px, 4vw, 40px);
  letter-spacing: 0.01em;
  text-transform: uppercase;
  margin: 0;
  text-wrap: balance;
}

.wordmark span {
  color: var(--accent);
}

.meta {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-muted);
  text-align: right;
}

.stats {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  padding: 18px clamp(16px, 4vw, 40px);
}

.stat {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 10px 16px;
  min-width: 108px;
}

.stat .n {
  font-family: var(--font-mono);
  font-weight: 600;
  font-size: 20px;
  font-variant-numeric: tabular-nums;
  display: block;
}

.stat .l {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
}

.filters {
  padding: 0 clamp(16px, 4vw, 40px) 12px;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-muted);
}

.filters b { color: var(--text); font-weight: 500; }

.filter-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 0 clamp(16px, 4vw, 40px) 20px;
}

.filter-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
}

.filter-group .group-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  min-width: 60px;
  flex-shrink: 0;
}

.filter-group label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  cursor: pointer;
  user-select: none;
  font-size: 13px;
}

.filter-group input[type="checkbox"] {
  appearance: none;
  width: 15px;
  height: 15px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--surface);
  cursor: pointer;
  position: relative;
  flex-shrink: 0;
}

.filter-group input[type="checkbox"]:checked {
  background: var(--accent);
  border-color: var(--accent);
}

.filter-group input[type="checkbox"]:checked::after {
  content: "";
  position: absolute;
  left: 4px;
  top: 1px;
  width: 4px;
  height: 8px;
  border: solid var(--accent-ink);
  border-width: 0 2px 2px 0;
  transform: rotate(45deg);
}

.filter-group input[type="checkbox"]:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

#showingCount {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-muted);
}

.table-wrap {
  overflow-x: auto;
  padding: 0 clamp(16px, 4vw, 40px) 40px;
}

table {
  width: 100%;
  border-collapse: collapse;
  min-width: 1080px;
  background: var(--surface);
}

thead th {
  position: sticky;
  top: 0;
  background: var(--surface-2);
  border-bottom: 1px solid var(--border);
  text-align: left;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  padding: 10px 12px;
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}

thead th:hover { color: var(--text); }

thead th .arrow { opacity: 0; margin-left: 4px; font-size: 9px; }
thead th.sorted .arrow { opacity: 1; color: var(--accent); }

th.num, td.num { text-align: right; }

tbody td {
  padding: 9px 12px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
  white-space: nowrap;
}

tbody tr:hover { background: var(--row-hover); }

td.market {
  white-space: normal;
  min-width: 260px;
  padding: 0;
}

td.market a {
  display: block;
  padding: 9px 12px;
  color: var(--text);
  text-decoration: none;
  font-weight: 500;
  cursor: pointer;
}

td.market a:hover { color: var(--accent); }
td.market a:hover .title { text-decoration: underline; }

td.market a:focus-visible, thead th:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.ticker {
  display: block;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  font-weight: 400;
  margin-top: 2px;
}

.num, .mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }

.pill {
  display: inline-block;
  font-family: var(--font-mono);
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 3px;
  font-size: 12px;
}

.pill.tight { color: var(--tight); background: color-mix(in srgb, var(--tight) 16%, transparent); }
.pill.mid { color: var(--mid); background: color-mix(in srgb, var(--mid) 16%, transparent); }
.pill.wide { color: var(--wide); background: color-mix(in srgb, var(--wide) 16%, transparent); }

.cat {
  color: var(--text-muted);
}

footer {
  padding: 20px clamp(16px, 4vw, 40px) 40px;
  font-size: 11px;
  color: var(--text-muted);
}

@media (prefers-reduced-motion: no-preference) {
  tbody tr { transition: background-color 120ms ease; }
}
</style>
</head>
<body>

<header class="masthead">
  <h1 class="wordmark">Kalshi <span>Tight</span> Market Scanner</h1>
  <div class="meta">Generated __GENERATED_AT__ UTC<br />Sorted by listing date, newest first</div>
</header>

<div class="stats">
__STAT_TILES__
</div>

<div class="filters">__FILTER_SUMMARY__</div>

<div class="filter-panel">
  <div class="filter-group" id="categoryFilters">
    <span class="group-label">Category</span>
__CATEGORY_CHECKBOXES__
  </div>
  <div class="filter-group" id="spreadFilters">
    <span class="group-label">Spread</span>
    <label><input type="checkbox" class="spread-toggle" value="tight" checked /> ≤0.5¢</label>
    <label><input type="checkbox" class="spread-toggle" value="low" checked /> 0.5¢–2¢</label>
    <label><input type="checkbox" class="spread-toggle" value="mid" checked /> 2¢–3¢</label>
    <label><input type="checkbox" class="spread-toggle" value="wide" checked /> 3¢+</label>
  </div>
  <div class="filter-group" id="volumeFilters">
    <span class="group-label">Volume</span>
    <label><input type="checkbox" class="volume-toggle" value="v1" checked /> 5K–25K</label>
    <label><input type="checkbox" class="volume-toggle" value="v2" checked /> 25K–100K</label>
    <label><input type="checkbox" class="volume-toggle" value="v3" checked /> 100K–500K</label>
    <label><input type="checkbox" class="volume-toggle" value="v4" checked /> 500K+</label>
  </div>
  <div class="filter-group">
    <span id="showingCount"></span>
  </div>
</div>

<div class="table-wrap">
<table id="dash">
  <thead>
    <tr>
      <th data-type="num" data-key="open_ts">Listed<span class="arrow">▼</span></th>
      <th data-type="text" data-key="market">Market<span class="arrow">▼</span></th>
      <th data-type="text" data-key="category">Category<span class="arrow">▼</span></th>
      <th class="num" data-type="num" data-key="spread">Spread<span class="arrow">▼</span></th>
      <th class="num" data-type="num" data-key="rel">Rel %<span class="arrow">▼</span></th>
      <th class="num" data-type="num" data-key="bid">Bid<span class="arrow">▼</span></th>
      <th class="num" data-type="num" data-key="ask">Ask<span class="arrow">▼</span></th>
      <th class="num" data-type="num" data-key="volume">Volume<span class="arrow">▼</span></th>
      <th class="num" data-type="num" data-key="oi">Open Int.<span class="arrow">▼</span></th>
      <th data-type="num" data-key="closes">Closes<span class="arrow">▼</span></th>
    </tr>
  </thead>
  <tbody>
__TABLE_ROWS__
  </tbody>
</table>
</div>

<footer>kalshi_scanner.py · __ROW_COUNT__ markets · data from Kalshi public API · click any column to re-sort</footer>

<script>
(function () {
  var table = document.getElementById('dash');
  var tbody = table.tBodies[0];
  var ths = table.querySelectorAll('thead th');
  var state = { key: 'open_ts', dir: -1 };

  function applySort(key, dir, type) {
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function (a, b) {
      var av = a.getAttribute('data-' + key);
      var bv = b.getAttribute('data-' + key);
      if (type === 'num') {
        av = parseFloat(av); bv = parseFloat(bv);
        return (av - bv) * dir;
      }
      return av.localeCompare(bv) * dir;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
  }

  ths.forEach(function (th) {
    th.addEventListener('click', function () {
      var key = th.getAttribute('data-key');
      var type = th.getAttribute('data-type');
      var dir = (state.key === key) ? -state.dir : (type === 'num' ? -1 : 1);
      state = { key: key, dir: dir };
      ths.forEach(function (t) {
        t.classList.remove('sorted');
        t.querySelector('.arrow').textContent = '▼';
      });
      th.classList.add('sorted');
      th.querySelector('.arrow').textContent = dir === 1 ? '▲' : '▼';
      applySort(key, dir, type);
    });
  });

  applySort('open_ts', -1, 'num');
  ths[0].classList.add('sorted');

  var showingCount = document.getElementById('showingCount');
  var totalRows = tbody.rows.length;
  var categoryBoxes = Array.prototype.slice.call(document.querySelectorAll('#categoryFilters input[type=checkbox]'));
  var spreadBoxes = Array.prototype.slice.call(document.querySelectorAll('.spread-toggle'));
  var volumeBoxes = Array.prototype.slice.call(document.querySelectorAll('.volume-toggle'));

  function checkedValues(boxes) {
    var set = {};
    boxes.forEach(function (b) { if (b.checked) set[b.value] = true; });
    return set;
  }

  function applyToggles() {
    var cats = checkedValues(categoryBoxes);
    var spreads = checkedValues(spreadBoxes);
    var volumes = checkedValues(volumeBoxes);
    var visible = 0;
    Array.prototype.forEach.call(tbody.rows, function (r) {
      var show = cats[r.getAttribute('data-category')]
        && spreads[r.getAttribute('data-spread-bucket')]
        && volumes[r.getAttribute('data-volume-bucket')];
      r.style.display = show ? '' : 'none';
      if (show) visible++;
    });
    showingCount.textContent = 'Showing ' + visible.toLocaleString() + ' of ' + totalRows.toLocaleString();
  }

  categoryBoxes.concat(spreadBoxes, volumeBoxes).forEach(function (b) {
    b.addEventListener('change', applyToggles);
  });
  applyToggles();
})();
</script>
</body>
</html>
"""


def _spread_pill_class(spread: float) -> str:
    if spread <= 1:
        return "tight"
    if spread <= 5:
        return "mid"
    return "wide"


def _spread_bucket(spread: float) -> str:
    if spread <= 0.5:
        return "tight"
    if spread <= 2:
        return "low"
    if spread <= 3:
        return "mid"
    return "wide"


def _volume_bucket(volume: int) -> str:
    if volume < 25_000:
        return "v1"
    if volume < 100_000:
        return "v2"
    if volume < 500_000:
        return "v3"
    return "v4"


def export_html(markets: list[dict], path: str, filter_summary: str) -> None:
    if not markets:
        print("Nothing to export.")
        return

    try:
        font_css = (_ASSETS_DIR / "fonts.css").read_text(encoding="utf-8")
    except OSError:
        font_css = ""

    # Newest-listed-first by default; client-side JS lets the viewer re-sort.
    rows_sorted = sorted(markets, key=lambda m: m.get("open_timestamp", 0), reverse=True)

    row_html = []
    for m in rows_sorted:
        pill = _spread_pill_class(m["spread"])
        rel = f"{m['relative_spread']*100:.1f}%" if m["relative_spread"] is not None else "—"
        exp_str = f"{m['close_date']} ({m['days_to_exp']}d)" if m["days_to_exp"] is not None else m["close_date"]
        open_str = m["open_date"] or "—"
        title_esc = html.escape(m["title"])
        ticker_esc = html.escape(m["ticker"])
        url_esc = html.escape(m["url"], quote=True)
        category_key = html.escape((m["category"] or "—").lower())

        row_html.append(
            "    <tr "
            f'data-open_ts="{m.get("open_timestamp", 0)}" '
            f'data-market="{html.escape(m["title"].lower())}" '
            f'data-category="{category_key}" '
            f'data-spread="{m["spread"]}" '
            f'data-spread-bucket="{_spread_bucket(m["spread"])}" '
            f'data-volume-bucket="{_volume_bucket(m["volume"])}" '
            f'data-rel="{m["relative_spread"] if m["relative_spread"] is not None else -1}" '
            f'data-bid="{m["yes_bid"]}" '
            f'data-ask="{m["yes_ask"]}" '
            f'data-volume="{m["volume"]}" '
            f'data-oi="{m["open_interest"]}" '
            f'data-closes="{m["days_to_exp"] if m["days_to_exp"] is not None else 999999}"'
            ">\n"
            f'      <td class="mono">{open_str}</td>\n'
            f'      <td class="market"><a href="{url_esc}" target="_blank" rel="noopener">'
            f'<span class="title">{title_esc}</span>'
            f'<span class="ticker">{ticker_esc}</span></a></td>\n'
            f'      <td class="cat">{html.escape(m["category"] or "—")}</td>\n'
            f'      <td class="num"><span class="pill {pill}">{_fmt_cents(m["spread"])}</span></td>\n'
            f'      <td class="num mono">{rel}</td>\n'
            f'      <td class="num mono">{_fmt_cents(m["yes_bid"])}</td>\n'
            f'      <td class="num mono">{_fmt_cents(m["yes_ask"])}</td>\n'
            f'      <td class="num mono">{m["volume"]:,}</td>\n'
            f'      <td class="num mono">{m["open_interest"]:,}</td>\n'
            f'      <td class="mono">{exp_str}</td>\n'
            "    </tr>"
        )

    vols = [m["volume"] for m in markets]
    stat_tiles = "".join(
        f'  <div class="stat"><span class="n">{v}</span><span class="l">{l}</span></div>\n'
        for v, l in [
            (f"{len(markets):,}", "Markets"),
            (f"{len(set(m['category'] for m in markets)):,}", "Categories"),
            (f"{min((m['spread'] for m in markets), default=0):g}¢–{max((m['spread'] for m in markets), default=0):g}¢", "Spread range"),
            (f"{(sum(vols)//len(vols)):,}" if vols else "0", "Avg volume"),
        ]
    )

    category_checkboxes = "".join(
        f'    <label><input type="checkbox" value="{html.escape(cat.lower())}" checked /> {html.escape(cat)}</label>\n'
        for cat in sorted(set(m["category"] or "—" for m in markets))
    )

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    out = (
        _DASHBOARD_TEMPLATE
        .replace("__TITLE__", "Kalshi Tight Market Scanner")
        .replace("__FONT_CSS__", font_css)
        .replace("__GENERATED_AT__", generated_at)
        .replace("__STAT_TILES__", stat_tiles)
        .replace("__CATEGORY_CHECKBOXES__", category_checkboxes)
        .replace("__FILTER_SUMMARY__", html.escape(filter_summary))
        .replace("__TABLE_ROWS__", "\n".join(row_html))
        .replace("__ROW_COUNT__", f"{len(markets):,}")
    )

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out)
    print(f"\nExported {len(markets):,} markets → {path} (dashboard)")


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--max-spread", type=int, default=1, metavar="CENTS",
        help="Max spread in cents (default: 1)",
    )
    p.add_argument(
        "--min-volume", type=int, default=5000, metavar="N",
        help="Min cumulative volume in contracts traded (default: 5000)",
    )
    p.add_argument(
        "--max-relative-spread", type=float, metavar="PCT",
        help="Max spread as a percentage of midpoint price, e.g. 10 for 10%% "
             "(filters out near-zero-price markets where a tiny absolute spread is huge relatively)",
    )
    p.add_argument(
        "--min-oi", type=int, default=0, metavar="N",
        help="Min open interest / contracts outstanding (default: 0)",
    )
    p.add_argument(
        "--max-days", type=int, metavar="N",
        help="Max days until market closes",
    )
    p.add_argument(
        "--min-days", type=int, metavar="N",
        help="Min days until market closes",
    )
    p.add_argument(
        "--min-price", type=float, metavar="CENTS",
        help="Min midpoint price in cents (excludes near-zero/already-resolved markets)",
    )
    p.add_argument(
        "--max-price", type=float, metavar="CENTS",
        help="Max midpoint price in cents (excludes near-certain/already-resolved markets, e.g. 98 drops 99-100c)",
    )
    p.add_argument(
        "--category", type=str, metavar="NAME",
        help="Filter by Kalshi category string (substring match)",
    )
    p.add_argument(
        "--interesting-only", action="store_true",
        help="Show only markets matched to an interesting-category flag",
    )
    p.add_argument(
        "--exclude-modelable", action="store_true",
        help="Exclude sports and macro-economic markets",
    )
    p.add_argument(
        "--no-interesting-view", action="store_true",
        help="Skip the second 'flagged markets' table",
    )
    p.add_argument(
        "--export", metavar="FILE.csv",
        help="Export filtered results to CSV",
    )
    p.add_argument(
        "--export-html", metavar="FILE.html",
        help="Export filtered results to a sortable, hyperlinked HTML dashboard",
    )
    p.add_argument(
        "--rows", type=int, default=60, metavar="N",
        help="Max rows to display per table (default: 60)",
    )
    p.add_argument(
        "--all", action="store_true",
        help="Disable the default spread filter (show all valid markets)",
    )
    p.add_argument(
        "--include-sports", action="store_true",
        help="Include Sports category markets (excluded by default — very large volume)",
    )
    p.add_argument(
        "--include-crypto", action="store_true",
        help="Include Crypto category markets (excluded by default — rolling 15-min/hourly churn)",
    )
    p.add_argument(
        "--include-weather", action="store_true",
        help="Include Climate and Weather category markets (excluded by default)",
    )
    p.add_argument(
        "--include-mentions", action="store_true",
        help="Include Mentions category markets (excluded by default — low signal)",
    )
    p.add_argument(
        "--max-pages", type=int, metavar="N",
        help="Stop fetching after N pages (200 markets/page) — useful for quick scans",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()

    skip = set(SKIP_CATEGORIES)
    if args.include_sports:
        skip.discard("Sports")
    if args.include_crypto:
        skip.discard("Crypto")
    if args.include_weather:
        skip.discard("Climate and Weather")
    if args.include_mentions:
        skip.discard("Mentions")
    print("Fetching Kalshi markets…", file=sys.stderr)
    raw_markets = fetch_all_markets(verbose=True, skip_categories=skip, max_pages=args.max_pages)
    print(f"\nTotal kept: {len(raw_markets):,}", file=sys.stderr)

    # Compute metrics and drop markets without valid quotes
    markets = [m for r in raw_markets if (m := compute_metrics(r)) is not None]
    print(f"Markets with valid two-sided quotes: {len(markets):,}", file=sys.stderr)

    # Primary sort: absolute spread, then relative spread
    markets.sort(key=lambda m: (m["spread"], m["relative_spread"] or 999))

    max_spread = None if args.all else args.max_spread
    max_relative_spread = (
        args.max_relative_spread / 100 if args.max_relative_spread is not None else None
    )
    filtered = apply_filters(
        markets,
        max_spread=max_spread,
        max_relative_spread=max_relative_spread,
        min_volume=args.min_volume,
        min_open_interest=args.min_oi,
        max_days=args.max_days,
        min_days=args.min_days,
        min_price=args.min_price,
        max_price=args.max_price,
        category=args.category,
        interesting_only=args.interesting_only,
        exclude_modelable=args.exclude_modelable,
    )
    print(f"After filters: {len(filtered):,} markets\n", file=sys.stderr)

    # ── View 1: all filtered markets ─────────────────────────────────────────
    spread_desc = f"≤{max_spread}¢" if max_spread is not None else "all spreads"
    rel_desc = f", rel≤{args.max_relative_spread:g}%" if args.max_relative_spread is not None else ""
    display(filtered, f"Kalshi Tight Market Scanner  [{spread_desc}{rel_desc}, vol≥{args.min_volume}]", args.rows)

    # ── View 2: interesting / hard-to-model markets ───────────────────────────
    if not args.no_interesting_view:
        interesting = [m for m in filtered if m["flags"]]
        if interesting:
            display(
                interesting,
                "⚡  Hard-to-Model Markets  (political · exec · celebrity · geo · legal · company)",
                args.rows,
            )
        else:
            if RICH:
                Console().print("\n[dim]No flagged interesting markets matched current filters.[/dim]")
            else:
                print("\nNo flagged interesting markets matched current filters.")

    if args.export:
        export_csv(filtered, args.export)

    if args.export_html:
        filter_bits = [f"max spread {spread_desc}"]
        if args.max_relative_spread is not None:
            filter_bits.append(f"max relative spread {args.max_relative_spread:g}%")
        filter_bits.append(f"min volume {args.min_volume:,}")
        if args.min_oi:
            filter_bits.append(f"min open interest {args.min_oi:,}")
        if args.max_days is not None:
            filter_bits.append(f"closes within {args.max_days}d")
        if args.min_days is not None:
            filter_bits.append(f"closes after {args.min_days}d")
        if args.min_price is not None:
            filter_bits.append(f"price ≥ {args.min_price:g}¢")
        if args.max_price is not None:
            filter_bits.append(f"price ≤ {args.max_price:g}¢")
        if args.category:
            filter_bits.append(f"category ~ {args.category!r}")
        if args.interesting_only:
            filter_bits.append("flagged-interesting only")
        if args.exclude_modelable:
            filter_bits.append("excluding modelable")
        export_html(filtered, args.export_html, " · ".join(filter_bits))


if __name__ == "__main__":
    main()
