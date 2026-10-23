"""
Daily snapshot of the most-mentioned stocks on Reddit, from ApeWisdom.

  python fetch_reddit_trending.py            # fetch and save to Supabase (public.reddit_trending)
  python fetch_reddit_trending.py --dry-run  # fetch and print, no database

Source: https://apewisdom.io/api/v1.0/filter/all-stocks (all stock subreddits, last 24h, 100 per page).
ETFs are kept but flagged with is_etf, from the name (ApeWisdom has no type field).
Re-running on the same day replaces that day's rows, so ranks never duplicate.
"""
import argparse
import html
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

API = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}"
PAGES = 1  # 100 tickers per page; the top 100 is plenty
TABLE = "reddit_trending"

# ApeWisdom names ETFs like "SPDR S&P 500 ETF Trust", "ProShares UltraPro QQQ", "United States Oil Fund".
ETF_NAME = re.compile(r"\bETF\b|\bETN\b|\bFund\b|iShares|ProShares|SPDR|Direxion", re.IGNORECASE)
# A few big ones whose names don't say so.
ETF_TICKERS = {"SPY", "QQQ", "VOO", "VTI", "IWM", "DIA", "GLD", "SLV", "TLT", "TQQQ", "SQQQ", "SOXL", "SOXS", "UVXY", "USO"}


def is_etf(ticker: str, name: str) -> bool:
    return ticker.upper() in ETF_TICKERS or bool(ETF_NAME.search(name))


def to_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def fetch(page: int) -> list[dict]:
    last = None
    for attempt in range(3):
        try:
            res = requests.get(API.format(page=page), timeout=20, headers={"User-Agent": "ox-quant/1.0 (+https://finance.oxinion.com)"})
            res.raise_for_status()
            return res.json().get("results") or []
        except Exception as e:  # network blips, 5xx, bad JSON
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"ApeWisdom page {page} failed: {last}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print instead of writing to Supabase")
    args = ap.parse_args()

    day = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    raw = [r for p in range(1, PAGES + 1) for r in fetch(p)]
    rows, seen = [], set()
    for r in raw:
        ticker = str(r.get("ticker") or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        name = html.unescape(str(r.get("name") or ticker)).strip()
        rows.append({
            "date": day,
            "rank": to_int(r.get("rank")) or len(rows) + 1,
            "ticker": ticker,
            "name": name,
            "mentions": to_int(r.get("mentions")) or 0,
            "upvotes": to_int(r.get("upvotes")) or 0,
            "rank_24h_ago": to_int(r.get("rank_24h_ago")),
            "mentions_24h_ago": to_int(r.get("mentions_24h_ago")),
            "is_etf": is_etf(ticker, name),
        })
    if not rows:
        print("❌ ApeWisdom returned no results")
        return 1

    stocks = [r for r in rows if not r["is_etf"]]
    print(f"{day}: {len(rows)} tickers ({len(rows) - len(stocks)} ETFs). Top stocks: "
          + ", ".join(f"{r['ticker']}({r['mentions']})" for r in stocks[:5]))

    if args.dry_run:
        return 0

    url, key = os.environ.get("FINANCE_SUPABASE_URL"), os.environ.get("FINANCE_SUPABASE_SECRET_KEY")
    if not url or not key:
        print("❌ Missing FINANCE_SUPABASE_URL / FINANCE_SUPABASE_SECRET_KEY")
        return 1
    from supabase import create_client

    db = create_client(url, key)
    db.table(TABLE).delete().eq("date", day).execute()  # same-day re-run replaces the snapshot
    db.table(TABLE).insert(rows).execute()
    print(f"✅ Saved {len(rows)} rows to public.{TABLE} for {day}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
