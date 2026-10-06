"""Quality / Value / Momentum / Growth fundamentals fetcher.

Companion to main.py: main.py refreshes daily technicals (price/RSI/trend)
into the `signals` table. This script refreshes fundamentals (which only
change quarterly/annually) into a separate `fundamentals` table on its own,
slower cadence -- see .github/workflows/fundamentals.yml.

All ratios are stored as decimal fractions (e.g. 0.15 == 15%), matching
yfinance's own convention for fields like `returnOnEquity`.
"""

import os
import time
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from supabase import create_client

from main import QQQ, VOO, read_global500_tickers

load_dotenv()

FINANCE_SUPABASE_URL = os.getenv("FINANCE_SUPABASE_URL")
FINANCE_SUPABASE_SECRET_KEY = os.getenv("FINANCE_SUPABASE_SECRET_KEY")
supabase = None

if FINANCE_SUPABASE_URL and FINANCE_SUPABASE_SECRET_KEY:
    supabase = create_client(FINANCE_SUPABASE_URL, FINANCE_SUPABASE_SECRET_KEY)
    print("[INFO] Supabase client initialized")
else:
    print("[INFO] Supabase not configured, running in local mode")

# 12-1 month momentum: trailing ~12mo return excluding the most recent
# month, so a short-term pullback/spike doesn't swamp the longer trend.
MOMENTUM_SKIP_TRADING_DAYS = 22
MIN_TRADING_DAYS_FOR_MOMENTUM = MOMENTUM_SKIP_TRADING_DAYS + 20

# Same pruning rationale as main.py's prune_stale_signals, but fundamentals
# refresh weekly (not daily), so the cutoff is longer.
STALE_DAYS = 30

# Small pause between tickers -- this script makes ~4x the yfinance calls
# per ticker that main.py does (info + financials + balance_sheet + price
# history), so it's more exposed to rate limiting across a full universe run.
REQUEST_PAUSE_SECONDS = 0.25


def _latest(df: pd.DataFrame, labels: list[str]) -> float | None:
    """Most recent (first column) value for the first matching row label.

    yfinance row labels shift slightly by ticker/filing (e.g. "EBIT" vs
    "Operating Income"), so callers pass a few candidates in priority order.
    """
    if df is None or df.empty:
        return None
    for label in labels:
        if label in df.index:
            series = df.loc[label].dropna()
            if not series.empty:
                return float(series.iloc[0])
    return None


def calculate_price_momentum(ticker: str) -> float | None:
    try:
        df = yf.download(ticker, period="1y", interval="1d", progress=False, auto_adjust=False)
        if df.empty:
            return None
        closes = df["Close"].squeeze().dropna()
        if len(closes) < MIN_TRADING_DAYS_FOR_MOMENTUM:
            return None
        recent = closes.iloc[-MOMENTUM_SKIP_TRADING_DAYS]
        oldest = closes.iloc[0]
        if oldest == 0:
            return None
        return float(recent / oldest - 1)
    except Exception as e:
        print(f"[WARN] momentum failed for {ticker}: {e}")
        return None


def calculate_roic(financials: pd.DataFrame, balance_sheet: pd.DataFrame) -> float | None:
    """ROIC = NOPAT / Invested Capital, NOPAT = EBIT * (1 - effective tax rate)."""
    ebit = _latest(financials, ["EBIT", "Operating Income"])
    pretax_income = _latest(financials, ["Pretax Income"])
    tax_provision = _latest(financials, ["Tax Provision"])
    equity = _latest(balance_sheet, ["Stockholders Equity", "Total Equity Gross Minority Interest"])
    if ebit is None or equity is None:
        return None

    total_debt = _latest(balance_sheet, ["Total Debt"])
    if total_debt is None:
        long_term_debt = _latest(balance_sheet, ["Long Term Debt"]) or 0
        current_debt = _latest(
            balance_sheet, ["Current Debt", "Current Debt And Capital Lease Obligation"]
        ) or 0
        total_debt = long_term_debt + current_debt
    cash = _latest(
        balance_sheet, ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]
    ) or 0

    tax_rate = 0.21  # fallback: rough long-run US effective corporate rate
    if pretax_income and tax_provision is not None and pretax_income != 0:
        tax_rate = max(0.0, min(1.0, tax_provision / pretax_income))

    nopat = ebit * (1 - tax_rate)
    invested_capital = total_debt + equity - cash
    if invested_capital <= 0:
        return None
    return float(nopat / invested_capital)


def calculate_gross_profitability(financials: pd.DataFrame, balance_sheet: pd.DataFrame) -> float | None:
    """Novy-Marx gross profitability = (Revenue - COGS) / Total Assets."""
    revenue = _latest(financials, ["Total Revenue"])
    cost_of_revenue = _latest(financials, ["Cost Of Revenue", "Reconciled Cost Of Revenue"])
    total_assets = _latest(balance_sheet, ["Total Assets"])
    if revenue is None or cost_of_revenue is None or not total_assets:
        return None
    return float((revenue - cost_of_revenue) / total_assets)


def calculate_revenue_cagr(financials: pd.DataFrame) -> float | None:
    if financials is None or financials.empty or "Total Revenue" not in financials.index:
        return None
    series = financials.loc["Total Revenue"].dropna()
    if len(series) < 2:
        return None
    newest, oldest = series.iloc[0], series.iloc[-1]
    years = len(series) - 1
    if oldest <= 0 or newest <= 0:
        return None
    return float((newest / oldest) ** (1 / years) - 1)


def evaluate_fundamentals(ticker: str) -> dict | None:
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        financials = t.financials
        balance_sheet = t.balance_sheet
    except Exception as e:
        print(f"[WARN] Could not fetch fundamentals for {ticker}: {e}")
        return None

    market_cap = info.get("marketCap")
    free_cash_flow = info.get("freeCashflow")
    fcf_yield = float(free_cash_flow / market_cap) if free_cash_flow and market_cap else None

    return {
        "ticker": ticker,
        "pe": info.get("trailingPE"),
        "pb": info.get("priceToBook"),
        "roe": info.get("returnOnEquity"),
        "roic": calculate_roic(financials, balance_sheet),
        "gross_profitability": calculate_gross_profitability(financials, balance_sheet),
        "fcf_yield": fcf_yield,
        "price_momentum": calculate_price_momentum(ticker),
        "revenue_cagr": calculate_revenue_cagr(financials),
        "eps_growth": info.get("earningsGrowth"),
        "updated_at": datetime.now().isoformat(),
    }


def prune_stale_fundamentals(days: int = STALE_DAYS) -> None:
    if not supabase:
        return
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    try:
        res = supabase.table("fundamentals").delete().lt("updated_at", cutoff).execute()
        removed = len(res.data) if res.data else 0
        if removed:
            print(f"[INFO] Pruned {removed} stale fundamentals row(s) untouched for {days}+ days")
    except Exception as e:
        print(f"[ERROR] Pruning stale fundamentals failed: {e}")


if __name__ == "__main__":
    # Same universe as main.py's default run -- keep these in sync manually
    # (there are only two callers, so a shared constant isn't worth the
    # cross-module coupling yet).
    ENABLED_INDEXES = ["Global 500", "QQQ", "VOO"]
    STATIC_INDEX_TICKERS = {"QQQ": QQQ, "VOO": VOO}

    tickers: set[str] = set()
    for name in ENABLED_INDEXES:
        tickers.update(read_global500_tickers() if name == "Global 500" else STATIC_INDEX_TICKERS.get(name, []))

    print(f"\nEvaluating fundamentals for {len(tickers)} unique tickers\n")

    rows = []
    for ticker in sorted(tickers):
        result = evaluate_fundamentals(ticker)
        if result:
            rows.append(result)
            print(
                f"[INFO] {ticker}: P/E={result['pe']} P/B={result['pb']} ROE={result['roe']} "
                f"ROIC={result['roic']} GP={result['gross_profitability']} FCFY={result['fcf_yield']} "
                f"Mom={result['price_momentum']} RevCAGR={result['revenue_cagr']} EPSg={result['eps_growth']}"
            )
        time.sleep(REQUEST_PAUSE_SECONDS)

    if supabase and rows:
        print(f"\nSaving {len(rows)} fundamentals records...")
        try:
            supabase.table("fundamentals").upsert(rows, on_conflict="ticker").execute()
            print(f"[INFO] Upserted {len(rows)} tickers total")
        except Exception as e:
            print(f"[ERROR] Batch upsert failed: {e}")
    elif rows:
        print(f"\n[INFO] Supabase not configured. {len(rows)} records not saved.")

    prune_stale_fundamentals()
