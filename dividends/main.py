"""
Dividend history crawler — yfinance edition.

Pulls the FULL dividend history from Yahoo Finance (deep, unlike its income
statements) for each ticker in global500.csv and upserts:
  * every payment into `dividend_history` — raw amount + split-adjusted amount,
    inferred payout frequency, and a special-dividend flag
  * a per-ticker summary into `dividends` — frequency, TTM, forward yield,
    3y/5y CAGR, growth streak, and yearly totals

Free, no API key, no daily cap. Yahoo can throttle a single IP if hammered, so
we sleep between tickers and spread the universe across parallel shards.

RESUME-SKIP: each run skips tickers already in dividend_history (via the
dividend_history_tickers view) plus known-empty symbols (.div_empty.txt).
SHARDING: DIV_SHARD / DIV_SHARDS split the universe into disjoint strides.

Env (.env locally / GitHub Actions secrets):
  FINANCE_SUPABASE_URL   Supabase project URL
  FINANCE_SUPABASE_SECRET_KEY     Supabase SECRET key (sb_secret_…) — needed for writes
  DIV_LIMIT              tickers to process this run (default 500)
  DIV_YEARS              keep only the last N years of payments (default 0 = all)
  DIV_SLEEP              seconds between tickers (default 1.0)
  DIV_CSV               universe csv (default global500.csv)
  DIV_SHARD/DIV_SHARDS   stride shard index / count (default 0 / 1)
  DIV_TICKERS            explicit tickers to (re)fetch — bypasses CSV + resume
  DIV_SKIP_FILE          local file of no-dividend symbols (default .div_empty.txt)

  pip install -r requirements.txt   # yfinance, pandas, supabase, python-dotenv
  python main.py                    # run from the stoxx/ root
"""
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.getenv("FINANCE_SUPABASE_URL")
SUPABASE_KEY = os.getenv("FINANCE_SUPABASE_SECRET_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit(
        "Missing Supabase creds. Set FINANCE_SUPABASE_URL and the SECRET key "
        "FINANCE_SUPABASE_SECRET_KEY (sb_secret_…) in .env."
    )
DIV_LIMIT = int(os.getenv("DIV_LIMIT", "500"))
DIV_YEARS = int(os.getenv("DIV_YEARS", "0"))     # 0 = keep full history
DIV_SLEEP = float(os.getenv("DIV_SLEEP", "1.0"))
DIV_CSV = os.getenv("DIV_CSV", "global500.csv")
DIV_SHARD = int(os.getenv("DIV_SHARD", "0"))
DIV_SHARDS = int(os.getenv("DIV_SHARDS", "1"))
DIV_UNIVERSE_LIMIT = 500  # only the first N tickers of global500.csv (0 = all); bump this to expand
DIV_TICKERS = os.getenv("DIV_TICKERS", "").strip()
DIV_REFETCH = os.getenv("DIV_REFETCH", "").strip().lower() not in ("", "0", "false", "no")
DIV_SKIP_FILE = os.getenv(
    "DIV_SKIP_FILE", os.path.join(os.path.dirname(__file__), ".div_empty.txt")
)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---- dividend math (from the tested dividend_history.py) ----
def _naive(idx):
    return idx.tz_localize(None) if getattr(idx, "tz", None) is not None else idx


def days_to_frequency(median_days: float) -> str:
    if median_days <= 45:
        return "monthly"
    if median_days <= 135:
        return "quarterly"
    if median_days <= 270:
        return "semiannual"
    return "annual"


def payments_per_year(freq: str) -> int:
    return {"monthly": 12, "quarterly": 4, "semiannual": 2, "annual": 1}.get(freq, 4)


NASDAQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def _parse_mdy(v):
    """Nasdaq 'MM/DD/YYYY' -> date, or None."""
    s = str(v or "").strip()
    if not s or s.upper() == "N/A":
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        return None


def fetch_nasdaq_schedule(ticker: str):
    """Recent dividend rows from Nasdaq with real ex/pay/record/declaration dates.

    yfinance only exposes ex-dates; Nasdaq carries the payable date per payment,
    which is what the projected-dividend calendar needs. Returns a list of
    {ex, pay, record, decl} (dates; pay/record/decl may be None) or [] on any
    failure — the caller then just leaves pay_date null for that ticker.
    """
    url = f"https://api.nasdaq.com/api/quote/{ticker}/dividends?assetclass=stocks"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=NASDAQ_HEADERS, timeout=10)
            if r.status_code == 200:
                rows = (
                    (((r.json() or {}).get("data") or {}).get("dividends") or {}).get(
                        "rows"
                    )
                    or []
                )
                out = []
                for row in rows:
                    ex = _parse_mdy(row.get("exOrEffDate"))
                    if not ex:
                        continue
                    out.append(
                        {
                            "ex": ex,
                            "pay": _parse_mdy(row.get("paymentDate")),
                            "record": _parse_mdy(row.get("recordDate")),
                            "decl": _parse_mdy(row.get("declarationDate")),
                        }
                    )
                if out:
                    return out
        except Exception:
            pass
        time.sleep(0.8 * (attempt + 1))
    return []


def _match_nasdaq(ex_date, sched):
    """The Nasdaq entry whose ex-date is closest to ex_date (within 3 days)."""
    best, best_diff = None, 4
    for e in sched:
        diff = abs((e["ex"] - ex_date).days)
        if diff < best_diff:
            best, best_diff = e, diff
    return best


def fetch_dividends(ticker: str):
    """(rows, summary) from yfinance, or ([], {}) when there are no dividends."""
    t = yf.Ticker(ticker)
    divs = t.dividends                     # None for delisted / unknown symbols
    if divs is None or getattr(divs, "empty", True):
        return [], {}
    divs = divs.dropna()
    if divs.empty:
        return [], {}
    divs.index = _naive(divs.index)

    currency, price = "USD", None
    try:
        fi = t.fast_info
        currency = (fi.get("currency") if hasattr(fi, "get") else fi["currency"]) or "USD"
        price = float(fi.get("last_price") if hasattr(fi, "get") else fi["last_price"]) or None
    except Exception:
        pass

    df = divs.to_frame("amount")
    df["ex_date"] = df.index
    df = df.sort_values("ex_date").reset_index(drop=True)
    # yfinance dividends are already split-adjusted (back-adjusted to today's
    # share basis), so the adjusted series equals the raw yfinance amounts.
    df["adj_amount"] = df["amount"]

    gaps = df["ex_date"].diff().dropna().dt.days
    freq = days_to_frequency(float(gaps.median()) if not gaps.empty else 365.0)

    # special-dividend heuristic: far above the trailing regular median
    med = df["amount"].rolling(8, min_periods=3).median().bfill()
    df["is_special"] = df["amount"] > (2.5 * med)

    now_iso = datetime.now(timezone.utc).isoformat()
    # Nasdaq carries the payable date per payment (yfinance does not); match it to
    # each ex-date so `dividend_history` can drive a payment-month calendar.
    nasdaq_sched = fetch_nasdaq_schedule(ticker)
    rows = []
    for ex, amt, adj, sp in zip(
        df["ex_date"], df["amount"], df["adj_amount"], df["is_special"]
    ):
        ex_d = ex.date()
        m = _match_nasdaq(ex_d, nasdaq_sched) if nasdaq_sched else None
        has_pay = bool(m and m["pay"])
        rows.append({
            "ticker": ticker,
            "ex_date": ex_d.isoformat(),
            "amount": round(float(amt), 6),
            "adjusted_amount": round(float(adj), 6),
            "frequency": "special" if sp else freq,
            "is_special": bool(sp),
            "currency": currency,
            "source": "yfinance+nasdaq" if has_pay else "yfinance",
            "updated_at": now_iso,
            "pay_date": m["pay"].isoformat() if has_pay else None,
            "record_date": m["record"].isoformat() if (m and m["record"]) else None,
            "declaration_date": m["decl"].isoformat() if (m and m["decl"]) else None,
        })

    # ---- summary (regular payments only) ----
    reg = df[~df["is_special"]].copy()
    if reg.empty:
        reg = df.copy()
    now = pd.Timestamp.today()
    ttm = float(reg[reg["ex_date"] > now - pd.Timedelta(days=365)]["amount"].sum())
    last_amt = float(reg["amount"].iloc[-1])
    fwd_annual = last_amt * payments_per_year(freq)
    yld = (fwd_annual / price * 100) if price else None

    reg["year"] = reg["ex_date"].dt.year
    annual = reg.groupby("year")["adj_amount"].sum()
    complete = annual[annual.index < now.year]
    # Growth streak: year-over-year dividend sum, but only across years with the
    # ticker's normal payment count — skip ex-date-drift years (an extra/missing
    # payment) that otherwise break long streaks (e.g. KO 2001 had 5 payments).
    yr_cnt = reg.groupby("year")["amount"].count()
    modal_cnt = int(yr_cnt.mode().iloc[0]) if not yr_cnt.empty else 0
    clean_years = annual[yr_cnt == modal_cnt]
    clean_years = clean_years[clean_years.index < now.year]

    def cagr(series, years):
        # RATIO (e.g. 0.0512 = 5.12%) — matches the DividendHeatmap, which
        # thresholds at 0.15 and multiplies by 100 for display.
        if len(series) <= years or series.iloc[-1 - years] <= 0:
            return None
        start, end = float(series.iloc[-1 - years]), float(series.iloc[-1])
        return round((end / start) ** (1 / years) - 1, 4)

    streak = 0
    for i in range(len(clean_years) - 1, 0, -1):
        if clean_years.iloc[i] > clean_years.iloc[i - 1]:
            streak += 1
        else:
            break

    summary = {
        "frequency": freq,
        "last_amount": round(last_amt, 4),
        "ttm_dividend": round(ttm, 4),
        "forward_annual": round(fwd_annual, 4),
        "yield_pct": round(yld, 2) if yld else None,
        "dividend_cagr_3yr": cagr(complete, 3),
        "dividend_cagr_5yr": cagr(complete, 5),
        "consecutive_growth_years": streak,
        "total_dividends_history": {str(int(y)): round(float(v), 4) for y, v in annual.items()},
    }
    return rows, summary


def fetch_done_tickers() -> set:
    res = supabase.table("dividend_history_tickers").select("ticker").execute()
    return {r["ticker"] for r in (res.data or [])}


def load_skip_empty() -> set:
    try:
        with open(DIV_SKIP_FILE) as f:
            return {ln.strip() for ln in f if ln.strip()}
    except FileNotFoundError:
        return set()


def mark_empty(ticker: str) -> None:
    with open(DIV_SKIP_FILE, "a") as f:
        f.write(ticker + "\n")


def main():
    if DIV_TICKERS:
        names = [t.strip().upper() for t in DIV_TICKERS.split(",") if t.strip()]
        batch = pd.DataFrame({"Ticker": names, "Name": names, "country": None})
        print(f"Explicit tickers (bypassing resume-skip): {', '.join(names)}\n")
    else:
        # keep_default_na=False: treat ticker strings like "NA" (Nano Labs),
        # "NULL", "None" as real tickers instead of float NaN (which crashes
        # .upper()/yfinance). dtype=str keeps numeric-looking tickers as text.
        df = pd.read_csv(
            DIV_CSV, dtype={"Symbol": str}, keep_default_na=False
        ).rename(columns={"Symbol": "Ticker"})
        cols = [c for c in ("Ticker", "Name", "country") if c in df.columns]
        df = df[cols]
        df["Ticker"] = df["Ticker"].str.strip()
        df = df[df["Ticker"] != ""]          # drop genuinely-empty ticker cells
        if "country" not in df.columns:
            df["country"] = None
        if DIV_UNIVERSE_LIMIT > 0:                # only the first N tickers of the universe
            df = df.head(DIV_UNIVERSE_LIMIT)
        if DIV_SHARDS > 1:                        # disjoint stride for this shard
            df = df.iloc[DIV_SHARD::DIV_SHARDS]
        skip_empty = load_skip_empty()
        if DIV_REFETCH:
            # Backfill mode: re-process already-seeded tickers (e.g. to fill in
            # pay_date on existing rows). Only genuinely no-dividend symbols are
            # still skipped.
            done = set()
            pending = df[~df["Ticker"].isin(skip_empty)].reset_index(drop=True)
        else:
            done = fetch_done_tickers()
            pending = df[~df["Ticker"].isin(done | skip_empty)].reset_index(drop=True)
        batch = pending.head(DIV_LIMIT)

        print(
            f"Shard {DIV_SHARD}/{DIV_SHARDS} · this shard's rows {len(df)} · "
            f"already seeded {len(done)} · pending here {len(pending)}"
            + (" · REFETCH" if DIV_REFETCH else "")
        )
        if batch.empty:
            print("✅ Nothing left for this shard — its partition is fully seeded.")
            return
        print(f"Processing {len(batch)} tickers via yfinance, sleep {DIV_SLEEP}s\n")

    cutoff = None
    if DIV_YEARS > 0:
        y = datetime.now(timezone.utc)
        cutoff = f"{y.year - DIV_YEARS:04d}-{y.month:02d}-{y.day:02d}"

    ok = empty = failed = 0
    rows_iter = list(batch.itertuples(index=False))
    for i, r in enumerate(rows_iter):
        ticker = r.Ticker
        name = getattr(r, "Name", ticker)
        country = getattr(r, "country", None)
        print(f"[{i + 1}/{len(rows_iter)}] {ticker} ({name})")
        try:
            rows, summary = fetch_dividends(ticker)
        except Exception as e:
            print(f"  ❌ fetch failed: {e}")
            failed += 1
            time.sleep(DIV_SLEEP)
            continue

        if not rows:
            print("  ℹ️ no dividends")
            mark_empty(ticker)
            empty += 1
            time.sleep(DIV_SLEEP)
            continue

        if cutoff:
            rows = [x for x in rows if x["ex_date"] >= cutoff]

        try:
            if rows:
                supabase.table("dividend_history").upsert(
                    rows, on_conflict="ticker,ex_date"
                ).execute()
            summary.update({
                "ticker": ticker, "name": name, "country": country,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            supabase.table("dividends").upsert(summary, on_conflict="ticker").execute()
            n_special = sum(1 for x in rows if x["is_special"])
            print(f"  ✅ {len(rows)} payments · {summary['frequency']}"
                  + (f" · {n_special} special" if n_special else ""))
            ok += 1
        except Exception as e:
            print(f"  ❌ upsert failed: {e}")
            failed += 1

        time.sleep(DIV_SLEEP)

    print(f"\n✅ {ok}   ℹ️ {empty} no-div   ❌ {failed} failed")


if __name__ == "__main__":
    main()
