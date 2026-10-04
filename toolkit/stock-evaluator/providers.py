"""
Price-bar providers for the signals evaluator (batched Alpaca edition).

Routing (per ticker):
    US ticker  (no '.' suffix)   -> Alpaca daily bars (IEX feed, free)
    foreign    (has '.' suffix)  -> yfinance
    Alpaca miss / no keys / err  -> yfinance fallback

Alpaca is pulled in BULK up front via prefetch_us(): one multi-symbol request per
~100 tickers instead of one request per ticker. ~600 US names => ~6 calls, which
never approaches the free tier's 200 req/min limit (the per-ticker version did,
so ~100 US names were 429'd and fell back to yfinance). Foreign names stay on
yfinance (Alpaca is US only).

Returns a pandas DataFrame with a tz-naive DatetimeIndex and a 'Close' column
(plus OHLCV) so the existing RSI / sparkline / score logic runs unchanged, along
with the source name ('alpaca' | 'yfinance').

Env (optional — without keys everything falls back to yfinance, old behavior):
    ALPACA_API_KEY_ID
    ALPACA_API_SECRET_KEY
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import yfinance as yf

ALPACA_MULTI_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
ALPACA_LOOKBACK_DAYS = 400          # ~13 months: enough for RSI(14) + 90d lookback
ALPACA_ADJUSTMENT = "split"         # split-adjusted, dividend-unadjusted
ALPACA_CHUNK = 100                  # symbols per request (well under URL/limit caps)
ALPACA_PAGE_LIMIT = 10000           # max bars per page (Alpaca cap)

# Module-level cache: original ticker -> daily-bar DataFrame, filled by prefetch_us().
_ALPACA_CACHE: Dict[str, pd.DataFrame] = {}


def _alpaca_keys() -> Tuple[str, str]:
    return (
        os.getenv("ALPACA_API_KEY_ID", "").strip(),
        os.getenv("ALPACA_API_SECRET_KEY", "").strip(),
    )


def alpaca_available() -> bool:
    k, s = _alpaca_keys()
    return bool(k and s)


def is_us_ticker(ticker: str) -> bool:
    """US listings have no exchange suffix. Foreign (Yahoo form) carry a '.' suffix
    (.KS .T .HK .SS .L .PA ...). US class shares use a dash (BRK-B) — still US."""
    return "." not in ticker


def to_alpaca_symbol(ticker: str) -> str:
    """Yahoo form -> Alpaca form. US class shares: 'BRK-B' -> 'BRK.B'."""
    return ticker.replace("-", ".")


def _bars_to_df(arr: List[dict]) -> Optional[pd.DataFrame]:
    if not arr:
        return None
    df = pd.DataFrame(
        [{"Open": b["o"], "High": b["h"], "Low": b["l"], "Close": b["c"], "Volume": b["v"]} for b in arr],
        index=pd.to_datetime([b["t"] for b in arr], utc=True),
    )
    df.index = df.index.tz_localize(None)
    return df.sort_index()


def prefetch_us(tickers: List[str]) -> None:
    """Bulk-load US tickers' daily bars from Alpaca into _ALPACA_CACHE.

    One request per ALPACA_CHUNK symbols (paginated). No-op without keys. A chunk
    that errors (e.g. transient) just leaves its symbols uncached -> they fall back
    to yfinance in get_bars().
    """
    if not alpaca_available():
        print("[INFO] Alpaca keys absent — all tickers via yfinance.")
        return

    # alpaca symbol -> original ticker (so cache is keyed by what evaluate_stock passes)
    sym_to_orig: Dict[str, str] = {}
    for t in tickers:
        if is_us_ticker(t):
            sym_to_orig[to_alpaca_symbol(t)] = t
    symbols = list(sym_to_orig.keys())
    if not symbols:
        return

    key, secret = _alpaca_keys()
    headers = {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "accept": "application/json",
    }
    start = (datetime.now(timezone.utc) - timedelta(days=ALPACA_LOOKBACK_DAYS)).date().isoformat()

    raw: Dict[str, List[dict]] = {}
    chunks = 0
    for i in range(0, len(symbols), ALPACA_CHUNK):
        batch = symbols[i:i + ALPACA_CHUNK]
        page_token: Optional[str] = None
        while True:
            params = {
                "symbols": ",".join(batch),
                "timeframe": "1Day",
                "start": start,
                "adjustment": ALPACA_ADJUSTMENT,
                "feed": "iex",
                "limit": ALPACA_PAGE_LIMIT,
            }
            if page_token:
                params["page_token"] = page_token
            try:
                r = requests.get(ALPACA_MULTI_BARS_URL, headers=headers, params=params, timeout=30)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] Alpaca batch request failed ({len(batch)} syms): {e}")
                break
            if r.status_code != 200:
                print(f"[WARN] Alpaca batch HTTP {r.status_code} for {len(batch)} syms — those fall back to yfinance")
                break
            body = r.json() or {}
            bars_obj = body.get("bars") or {}       # multi-symbol: {symbol: [bars]}
            for sym, arr in bars_obj.items():
                raw.setdefault(sym, []).extend(arr or [])
            page_token = body.get("next_page_token")
            if not page_token:
                break
        chunks += 1

    for sym, arr in raw.items():
        df = _bars_to_df(arr)
        if df is not None and not df.empty:
            _ALPACA_CACHE[sym_to_orig.get(sym, sym)] = df

    print(f"[INFO] Alpaca prefetch: {len(_ALPACA_CACHE)}/{len(symbols)} US tickers cached in {chunks} chunk(s)")


def _fetch_yfinance(ticker: str) -> Optional[pd.DataFrame]:
    df = yf.download(ticker, period="1y", interval="1d", progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    return df


def get_bars(ticker: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """Return (df, source). df has a DatetimeIndex and a 'Close' column, or
    (None, None). source is 'alpaca' or 'yfinance'. Call prefetch_us() first so
    US tickers resolve from the Alpaca cache; anything not cached (foreign, no
    keys, IEX gap, failed chunk) falls back to yfinance."""
    if is_us_ticker(ticker):
        df = _ALPACA_CACHE.get(ticker)
        if df is not None and not df.empty:
            return df, "alpaca"

    try:
        df = _fetch_yfinance(ticker)
        if df is not None and not df.empty:
            return df, "yfinance"
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] yfinance fetch failed for {ticker}: {e}")

    return None, None
