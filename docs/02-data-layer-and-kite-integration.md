# 02 — Data Layer & Kite Integration

## 1. Kite Connect Market Data Specifications

Zerodha's Kite Connect API provides equity market data under strict technical and licensing boundaries:
* **Market Data Subscription:** Kite Connect API with historical data costs ₹500/month. The free Personal API has order placement and positions only, with **no market data**.
* **Rate Limits:** The historical data endpoint (`/instruments/historical/...`) has a hard limit of **3 requests per second**. Exceeding this returns HTTP 429 (`Too Many Requests`) and risks IP throttling.
* **Token Lifetime:** Access tokens expire daily at approximately 06:00 IST. Token generation requires an interactive browser login with user 2FA.

---

## 2. Authentication & Token Exchange Flow

Automating Zerodha 2FA via headless browser/TOTP scraping violates Zerodha's terms of service and breaks whenever UI elements or captcha rules change. Artha Quant adopts an **interactive local redirect server** approach:

```
[Developer runs CLI] ──► Opens Browser to Kite Login URL
                               │
                               ▼
                   [User logs in & passes 2FA]
                               │
                               ▼
       Kite redirects to: http://127.0.0.1:8080/?request_token=XYZ
                               │
                               ▼
        [Local Python HTTP Server intercepts request_token]
                               │
                               ▼
   kite.generate_session(request_token, api_secret) ──► Returns access_token
                               │
                               ▼
       Saves to encrypted / protected session file: kite_session.json
```

### Session File Specification (`kite_session.json`)
```json
{
  "user_id": "AB1234",
  "api_key": "your_api_key",
  "access_token": "abcdef123456...",
  "login_time": "2026-11-07T08:30:00+05:30",
  "token_expiry_date": "2026-11-08"
}
```

### Token Expiration Validation Logic
Before any scheduled scan or data job executes:
1. Load `kite_session.json`.
2. Verify `token_expiry_date == today`.
3. Perform a lightweight ping: `kite.profile()`.
4. If invalid or HTTP 403 returned, immediately dispatch an urgent Telegram notification and fail safely.

---

## 3. Rate-Limiting & Ingestion Pipeline

To guarantee complete compliance with the 3 req/sec limit:

```python
import time
from functools import wraps

def rate_limited(min_interval: float = 0.35):
    """Decorator ensuring a minimum interval between calls."""
    last_called = [0.0]
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            elapsed = time.time() - last_called[0]
            left_to_wait = min_interval - elapsed
            if left_to_wait > 0:
                time.sleep(left_to_wait)
            ret = func(*args, **kwargs)
            last_called[0] = time.time()
            return ret
        return wrapper
    return decorator
```

### Date Chunking Strategy
Kite Connect limits the span of historical bars requested in a single call. 
* For `day` interval: Request up to **2,000 trading days (~5–7 years)** in a single batch, or chunk by 365-day segments.
* For `minute` intervals (future): Chunk into 60-day blocks maximum.

---

## 4. Corporate Actions & Data Adjustments

A single unadjusted 1:1 bonus or 2:1 stock split corrupts a backtest by manifesting as an artificial 50% overnight crash, triggering false signals and invalidating risk statistics.

### Audit Protocol Before Using Kite Data
1. **Benchmark Test on Known Corporate Actions:**
   * Run a data fetch on known historical split/bonus events (e.g., Reliance Industries 1:1 bonus in Oct 2024, Tata Motors split, IRCTC split).
   * Check whether historical prices before the ex-date are adjusted downward or whether raw unadjusted exchange prices are returned.
2. **Adjustment Policy:**
   * If Kite returns **adjusted** series: Document and proceed with periodic audit against NSE official bhavcopy.
   * If Kite returns **unadjusted** series: Implement a corporate actions lookup table (`data/cleaners/corporate_actions.json`) that applies split/bonus ratios retrospectively to prior bars.

---

## 5. Automated Data Quality & Cleansing Checks

Every retrieved bar series passes through the validation pipeline (`data/cleaners/validator.py`):

| Rule | Validation Check | Action on Failure |
|---|---|---|
| **Bar Integrity** | `High >= Open`, `High >= Close`, `Low <= Open`, `Low <= Close` | Raise `DataCorruptionError`, quarantine symbol. |
| **Zero Volume** | `Volume == 0` on an active market trading day | Flag as non-trading/illiquid bar; do not trigger signals. |
| **Outlier Jump** | `abs(Close - Open) > 3.5 * ATR(14)` or `(High - Low) > 4 * ATR(14)` | Mark for manual inspection (possible unadjusted split or flash crash). |
| **Trading Calendar Gap** | Missing trading days compared against NSE holiday calendar | Attempt refetch; log warning if gap persists. |
| **Stale Bar Check** | `bar_timestamp.date() != today` (when running at 16:00 IST) | Abort scan for symbol; alert operator that bar is stale. |

---

## 6. Parquet Storage Architecture

Data is stored locally using **Apache Parquet via `pyarrow`**:
* **Directory structure:**
  ```
  data/store/
  ├── 1D/
  │   ├── RELIANCE.parquet
  │   ├── TCS.parquet
  │   ├── HDFCBANK.parquet
  │   └── INFY.parquet
  ```

### Atomic Append & Deduplication Routine
When fetching new bars at 16:00 IST:
1. Load existing `data/store/1D/{symbol}.parquet` into a DataFrame (if file exists).
2. Query Kite API only for dates `[last_saved_date + 1 day, today]`.
3. If new bars exist:
   * Concat existing DataFrame + new DataFrame.
   * Deduplicate by index: `df = df[~df.index.duplicated(keep='last')].sort_index()`.
   * Write to a temporary file: `data/store/1D/{symbol}.parquet.tmp`.
   * Atomically rename `tmp` to `{symbol}.parquet` (guarantees zero corrupted files on crash).
