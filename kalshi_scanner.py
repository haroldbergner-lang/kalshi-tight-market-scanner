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
import csv
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    from rich.text import Text
    RICH = True
except ImportError:
    RICH = False

# ── API ──────────────────────────────────────────────────────────────────────

API_BASE = "https://api.kalshi.com/trade-api/v2"
PAGE_LIMIT = 200
REQUEST_DELAY = 0.15  # seconds between pages

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


def fetch_all_markets(verbose: bool = True) -> list[dict]:
    """Paginate through all open Kalshi markets and return raw API records."""
    markets: list[dict] = []
    cursor: Optional[str] = None
    page = 0

    while True:
        params: dict = {"limit": PAGE_LIMIT, "status": "open"}
        if cursor:
            params["cursor"] = cursor

        try:
            resp = requests.get(f"{API_BASE}/markets", params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"API error: {e}", file=sys.stderr)
            break

        data = resp.json()
        batch = data.get("markets", [])
        markets.extend(batch)
        page += 1

        if verbose:
            print(
                f"  Page {page:>3}: {len(batch):>3} markets  (running total: {len(markets):,})",
                file=sys.stderr,
            )

        cursor = data.get("cursor")
        if not cursor or not batch:
            break

        time.sleep(REQUEST_DELAY)

    return markets


# ── Metric computation ────────────────────────────────────────────────────────


def compute_metrics(raw: dict) -> Optional[dict]:
    """
    Derive trading and classification metrics from a raw Kalshi market record.
    Returns None for markets without valid two-sided quotes.
    """
    yes_bid = raw.get("yes_bid")
    yes_ask = raw.get("yes_ask")

    # Require a valid, non-crossed two-sided market
    if yes_bid is None or yes_ask is None:
        return None
    if yes_bid <= 0 or yes_ask <= 0 or yes_ask < yes_bid:
        return None

    spread = yes_ask - yes_bid
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

    # Build a searchable title blob
    title = raw.get("title", "")
    subtitle = raw.get("subtitle", "")
    display_title = title + (" — " + subtitle if subtitle else "")
    title_blob = (title + " " + subtitle).lower()

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

    return {
        "ticker": ticker,
        "title": display_title[:90],
        "category": kalshi_category,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "spread": spread,
        "midpoint": round(midpoint, 1),
        "relative_spread": relative_spread,
        "volume": raw.get("volume", 0) or 0,
        "volume_24h": raw.get("volume_24h", 0) or 0,
        "open_interest": raw.get("open_interest", 0) or 0,
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


def _spread_color(spread: int) -> str:
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
            Text(f"{m['spread']}¢", style=color),
            rel,
            f"{m['midpoint']}¢",
            f"{m['yes_bid']}¢",
            f"{m['yes_ask']}¢",
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
            f"{m['spread']:>3}¢ {m['midpoint']:>5} {m['yes_bid']:>4} {m['yes_ask']:>4}"
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
        "--max-spread", type=int, default=5, metavar="CENTS",
        help="Max spread in cents (default: 5)",
    )
    p.add_argument(
        "--min-volume", type=int, default=100, metavar="N",
        help="Min cumulative volume (default: 100)",
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
    return p


def main() -> None:
    args = build_parser().parse_args()

    print("Fetching Kalshi markets…", file=sys.stderr)
    raw_markets = fetch_all_markets(verbose=True)
    print(f"\nTotal raw markets fetched: {len(raw_markets):,}", file=sys.stderr)

    # Compute metrics and drop markets without valid quotes
    markets = [m for r in raw_markets if (m := compute_metrics(r)) is not None]
    print(f"Markets with valid two-sided quotes: {len(markets):,}", file=sys.stderr)

    # Primary sort: absolute spread, then relative spread
    markets.sort(key=lambda m: (m["spread"], m["relative_spread"] or 999))

    max_spread = None if args.all else args.max_spread
    filtered = apply_filters(
        markets,
        max_spread=max_spread,
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
    display(filtered, f"Kalshi Tight Market Scanner  [{spread_desc}, vol≥{args.min_volume}]", args.rows)

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
