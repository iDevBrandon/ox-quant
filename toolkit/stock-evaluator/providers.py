"""
Price-bar providers for the signals evaluator.

Routing (decided per ticker):
    US ticker  (no '.' suffix)   -> Alpaca daily bars (IEX feed, free)
    foreign    (has '.' suffix)  -> yfinance
    Alpaca miss / no keys / err  -> yfinance fallback

Why: Alpaca is a licensed, contracted market-data API (no Yahoo ToS exposure, far
more reliable than yfinance which rate-limits). It only covers US listings, so the
non-US "Global 500" names (.KS/.T/.HK/.SS/.L/.PA ...) stay on yfinance for now.

Returns a pandas DataFrame with a tz-naive DatetimeIndex and a 'Close' column
(plus OHLCV when available) so the existing RSI / sparkline / score logic runs
unchanged, together with the source name ('alpaca' | 'yfinance') for provenance.

Env (optional — without Alpaca keys everything falls back to yfinance, i.e. the
old behavior is preserved exactly):
    ALPACA_API_KEY_ID
    ALPACA_API_SECRET_KEY
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import pandas as pd
import requests
import yfinance as yf

ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
# ~13 months of calendar days so RSI(14) + 90d lookback always have enough bars.
ALPACA_LOOKBACK_DAYS = 400
# 'split' = split-adjusted but NOT dividend-adjusted. This avoids fake RSI/%-move
# spikes around stock splits. (yfinance auto_adjust=False returns raw close, which
# *does* jump on splits — so this is a small correctness improvement. Switch to
# 'raw' if you want byte-for-byte parity with the old yfinance behavior.)
ALPACA_ADJUSTMENT = "split"


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


def _fetch_alpaca(ticker: str) -> Optional[pd.DataFrame]:
    key, secret = _alpaca_keys()
    if not (key and secret):
        return None
    symbol = to_alpaca_symbol(ticker)
    start = (datetime.now(timezone.utc) - timedelta(days=ALPACA_LOOKBACK_DAYS)).date().isoformat()
    headers = {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "accept": "application/json",
    }
    base_params = {
        "timeframe": "1Day",
        "start": start,
        "adjustment": ALPACA_ADJUSTMENT,
        "feed": "iex",     # free tier
        "limit": 10000,
    }
    url = ALPACA_BARS_URL.format(symbol=symbol)
    bars: list[dict] = []
    page_token: Optional[str] = None
    for _ in range(20):  # pagination guard (1y of daily bars fits in one page)
        params = dict(base_params)
        if page_token:
            params["page_token"] = page_token
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code != 200:
            # 401/403 bad key, 404 unknown symbol, 429 rate-limit -> let caller fall back
            return None
        body = r.json() or {}
        page = body.get("bars") or []
        bars.extend(page)
        page_token = body.get("next_page_token")
        if not page_token:
            break
    if not bars:
        return None
    df = pd.DataFrame(
        [{"Open": b["o"], "High": b["h"], "Low": b["l"], "Close": b["c"], "Volume": b["v"]} for b in bars],
        index=pd.to_datetime([b["t"] for b in bars], utc=True),
    )
    df.index = df.index.tz_localize(None)  # match yfinance's tz-naive index
    return df.sort_index()


def _fetch_yfinance(ticker: str) -> Optional[pd.DataFrame]:
    df = yf.download(ticker, period="1y", interval="1d", progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    return df


def get_bars(ticker: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """Return (df, source). df has a DatetimeIndex and a 'Close' column, or
    (None, None) on total failure. source is 'alpaca' or 'yfinance'."""
    # US -> try Alpaca first (licensed, reliable)
    if is_us_ticker(ticker) and alpaca_available():
        try:
            df = _fetch_alpaca(ticker)
            if df is not None and not df.empty:
                return df, "alpaca"
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Alpaca fetch failed for {ticker}: {e}")

    # Foreign, no Alpaca keys, or Alpaca miss -> yfinance
    try:
        df = _fetch_yfinance(ticker)
        if df is not None and not df.empty:
            return df, "yfinance"
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] yfinance fetch failed for {ticker}: {e}")

    return None, None
