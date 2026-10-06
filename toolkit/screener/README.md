# Stock Evaluator

A Python-based stock evaluation engine that turns a simple **ticker list** into clear **trend, score, and buy/sell signals** using technical indicators like **RSI** and price trends.

This project is designed as a foundation for a larger investing toolkit under **Oxinion Finance**, with future integration into **Oxinion SDK notifications**.

---

## Philosophy

The goal is not to predict prices, but to **evaluate opportunity**.

> Cheap stocks should look weak, not strong.  
> Strong scores should come from weakness, not hype.

This evaluator follows a simple rule:

- **Low RSI → Downtrend**
- **Low RSI + Oversold Conditions → High Score**
- **High Score → Buy Signal**

---

## Core Pipeline

Ticker List
↓
Price Data (Historical)
↓
Indicator Calculation (RSI, Trend)
↓
Score Calculation
↓
Signal Generation (BUY / HOLD / SELL) with oxinion-sdk/notification integration (future)

---

## Features

### 1. Price Data Ingestion

- Fetch historical price data for a list of tickers
- Designed for weekly or daily resolution
- Easily extendable to multiple data providers

### 2. RSI Calculation

- Standard 14-period RSI
- Used to identify oversold and overbought conditions

#### RSI Interpretation

- RSI < 30 → Oversold
- RSI 30–50 → Weak / Neutral
- RSI > 70 → Overbought

---

### 3. Trend Detection

Trend is determined using recent price direction:

- Price below recent average → **Downtrend**
- Price above recent average → **Uptrend**

This mirrors logic similar to Google Sheets `SPARKLINE + GOOGLEFINANCE` trend visuals.

---

### 4. Scoring Logic

Scores represent **opportunity**, not momentum.

Example logic:

- Lower RSI → Higher score
- Downtrend + Oversold → Score boost
- Overbought → Score penalty

Higher score = more attractive entry.

---

### 5. Signal Generation

Signals are derived from score + trend + RSI:

| Condition                        | Signal       |
| -------------------------------- | ------------ |
| Low RSI + Downtrend + High Score | BUY          |
| Neutral RSI                      | HOLD         |
| High RSI + Uptrend               | SELL / AVOID |

---

## Example Output

```json
{
  "ticker": "COST",
  "rsi": 28.4,
  "trend": "DOWN",
  "score": 82,
  "signal": "BUY"
}
Project Structure (Planned)
css
Copy code
screener/
├── data/
│   └── price_loader.py
├── indicators/
│   ├── rsi.py
│   └── trend.py
├── scoring/
│   └── score_engine.py
├── signals/
│   └── signal_engine.py
├── main.py
└── README.md
Future Roadmap
🔔 Oxinion SDK Notification Pipeline (Planned)
In the future, this evaluator will connect to Oxinion SDK to:

Trigger notifications when a stock turns BUY

Send alerts via Oxinion SDK workflows (email, push, webhook)

Plug directly into geo-first automation pipelines

Example:
“RSI crossed below 30 → BUY signal → Oxinion SDK sends notification”

```

## Score Logic

The **score** represents how attractive a stock is for buying.  
It combines **RSI (Relative Strength Index)** and **Trend** to determine opportunity.

- **Higher score → more attractive for BUY**
- **Low RSI + Downtrend → score boost (oversold condition)**
- **High RSI + Uptrend → score penalty (overheated condition)**

### Python Implementation

```python
def calculate_score(rsi, trend):
    """
    Calculate a stock score based on RSI and Trend.

    Parameters:
        rsi (float): Current RSI value
        trend (str): 'UP', 'DOWN', or 'FLAT'

    Returns:
        int: Score between 0 and 100
    """

    score = 50  # Base score

    # RSI-based adjustment
    if rsi < 25:
        score += 35
    elif rsi < 30:
        score += 25
    elif rsi < 40:
        score += 10
    elif rsi > 70:
        score -= 35
    elif rsi > 60:
        score -= 20

    # Trend + RSI interaction
    if trend == "DOWN" and rsi < 35:
        score += 20   # Oversold + downtrend → attractive
    elif trend == "UP" and rsi > 60:
        score -= 20   # Expensive + uptrend → risky

    # Ensure score is between 0 and 100
    score = max(0, min(100, score))

    return score
Score Interpretation
RSI Trend Score Effect Meaning
<25 DOWN +35 +20 Very oversold → strong BUY
25–30 DOWN +25 +20 Oversold → good BUY
30–40 DOWN +10 +20 Slightly cheap → possible BUY
60–70 UP -20 Expensive → caution / HOLD
>70 UP -35 -20 Overheated → SELL

Score combines cheapness (RSI) and trend context to generate actionable signals.


```

## How to insert into Supabase

1. Set up a table in Supabase to store stock evaluations:

   ```sql
   create table signals (
   id uuid primary key default gen_random_uuid(),
   index_name text,
   ticker text,
   price numeric,
   rsi numeric,
   trend text,
   score int,
   signal text,
   created_at timestamptz default now(),
   unique (index_name, ticker)
   );
   ```

2. Set up environment variables and install dependencies:

## Setup

### Prerequisites

- Python 3.7+
- pip or conda for package management

### Installation

1. Clone this repository:

   ```bash
   git clone <your-repo-url>
   cd screener
   ```

2. Create and activate a virtual environment:

   ```bash
   # Create virtual environment
   python -m venv venv

   # Activate it (macOS/Linux)
   source venv/bin/activate

   # Activate it (Windows)
   venv\Scripts\activate
   ```

3. Install required packages:

   ```bash
   pip install yfinance pandas ta

   # Optional: For Supabase integration
   pip install supabase python-dotenv

   # update requirements.txt
   pip freeze > requirements.txt
   ```

4. Run the script

   python main.py

5. **Optional Supabase Setup**:

   Create a `.env` file in the project root with your Supabase credentials:

   **Note**: The application works perfectly without Supabase. If you don't set up these environment variables, the app will run normally and just skip the database integration.

### Usage

Run the stock evaluator:

```bash
python main.py
```

**With Supabase**: Results will be automatically saved to your Supabase `signals` table
**Without Supabase**: Results will only be displayed in the console

### Environment Variables

If either environment variable is missing, the application will run in local-only mode.

## Database Schema

If you want to use Supabase integration, create this table in your Supabase project:

```sql
create table signals (
  id uuid not null default gen_random_uuid (),
  index_names text null,
  ticker text null,
  price numeric null,
  rsi numeric null,
  trend text null,
  score integer null,
  signal text null,
  created_at timestamp with time zone null default now(),
  constraint signals_pkey primary key (id),
  constraint signals_ticker_key unique (ticker)
) TABLESPACE pg_default;
```

1. The application will automatically handle data insertion when environment variables are configured:
