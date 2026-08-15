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
import os
import sys
import time
from datetime import datetime, timezone
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
# These contain player props, game lines, and other high-volume sports contracts.
SKIP_CATEGORIES: set[str] = {"Sports"}


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
        "--max-pages", type=int, metavar="N",
        help="Stop fetching after N pages (200 markets/page) — useful for quick scans",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()

    skip = SKIP_CATEGORIES if not args.include_sports else set()
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


if __name__ == "__main__":
    main()
