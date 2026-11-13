"""Alpaca daily bars.

Deliberately hits the REST endpoint with httpx instead of using `alpaca-py`:
the SDK is reported to drop the `adjustment` parameter
(https://github.com/alpacahq/alpaca-py/issues/207) and we depend on
adjustment=split — split-adjusted but NOT dividend-adjusted — so the moving
averages line up with broker charts. Verify with `check_split_adjustment()`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from .config import Secrets

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
MARKET_TZ = ZoneInfo("America/New_York")

# Free-tier SIP data is available once it is at least 15 minutes old.
SIP_DELAY = timedelta(minutes=20)

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def is_symbol(text: str) -> bool:
    """Plausible US equity ticker (AAPL, BRK.B). Guards the DB, file names, and
    Telegram callback data against arbitrary input."""
    return bool(_SYMBOL.match(text))


def market_today() -> date:
    """Today's date in New York, regardless of where this machine is."""
    return datetime.now(MARKET_TZ).date()


class MarketDataError(RuntimeError):
    pass


def _headers(secrets: Secrets) -> dict[str, str]:
    missing = secrets.missing("alpaca_key", "alpaca_secret")
    if missing:
        raise MarketDataError(
            f"Missing {', '.join(m.upper() for m in missing)} in .env — "
            "get free keys at alpaca.markets"
        )
    return {
        "APCA-API-KEY-ID": secrets.alpaca_key,
        "APCA-API-SECRET-KEY": secrets.alpaca_secret,
    }


def fetch_bars(
    symbols: list[str],
    *,
    start: date,
    end: date | None = None,
    secrets: Secrets | None = None,
    timeout: float = 60.0,
) -> dict[str, pd.DataFrame]:
    """Daily OHLCV per symbol, oldest first, indexed by date.

    One batched request (plus pagination) for the whole symbol list.
    """
    if not symbols:
        return {}
    headers = _headers(secrets or Secrets())

    cutoff = datetime.now(timezone.utc) - SIP_DELAY
    end_dt = datetime.combine(end, datetime.max.time(), timezone.utc) if end else cutoff
    end_dt = min(end_dt, cutoff)

    params = {
        "symbols": ",".join(sorted(symbols)),
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": end_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "adjustment": "split",  # never "all": dividend adjustment skews the 200 SMA
        "feed": "sip",
        "limit": 10000,
    }

    collected: dict[str, list[dict]] = {}
    with httpx.Client(timeout=timeout) as client:
        while True:
            try:
                resp = client.get(BARS_URL, params=params, headers=headers)
            except httpx.ConnectError as exc:
                raise MarketDataError(
                    f"Cannot reach {BARS_URL}: {exc}\n"
                    "If this is a DNS failure, some resolvers (including 8.8.8.8 and "
                    "many ISP defaults) return NXDOMAIN for data.alpaca.markets. "
                    "Cloudflare (1.1.1.1) resolves it — set DNS to 1.1.1.1 in "
                    "System Settings > Network > Details > DNS."
                ) from exc
            if resp.status_code == 401:
                raise MarketDataError(
                    "Alpaca rejected the credentials (401). Check ALPACA_KEY/ALPACA_SECRET."
                )
            if resp.status_code == 429:
                raise MarketDataError("Alpaca rate limit (429). Retry shortly.")
            if resp.status_code >= 400:
                # 403 is usually a data-subscription limit, 422 a bad parameter.
                raise MarketDataError(f"Alpaca HTTP {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()

            for sym, bars in (payload.get("bars") or {}).items():
                collected.setdefault(sym, []).extend(bars)

            token = payload.get("next_page_token")
            if not token:
                break
            params["page_token"] = token

    return {sym: _to_frame(bars) for sym, bars in collected.items() if bars}


def _to_frame(bars: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(bars)
    df["date"] = pd.to_datetime(df["t"], utc=True).dt.date
    return (
        df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        .loc[:, ["date", "open", "high", "low", "close", "volume"]]
        .drop_duplicates(subset="date")
        .sort_values("date")
        .set_index("date")
    )


def check_split_adjustment(secrets: Secrets | None = None) -> tuple[bool, str]:
    """NVDA split 10:1 on 2024-06-07.

    With adjustment=split the pre-split closes read ~$120. If they read ~$1200
    the parameter is being ignored and every moving average downstream is wrong.
    """
    bars = fetch_bars(
        ["NVDA"], start=date(2024, 5, 20), end=date(2024, 6, 20), secrets=secrets
    )
    df = bars.get("NVDA")
    if df is None or df.empty:
        return False, "No NVDA bars returned."

    pre = df[df.index < date(2024, 6, 7)]["close"]
    if pre.empty:
        return False, "No pre-split NVDA bars in the window."

    avg = float(pre.mean())
    if 80 <= avg <= 200:
        return True, f"OK — pre-split NVDA closes average ${avg:,.2f} (split-adjusted)."
    if avg > 800:
        return False, (
            f"FAIL — pre-split NVDA closes average ${avg:,.2f}, i.e. raw/unadjusted. "
            "The `adjustment` parameter is being ignored; moving averages will not "
            "match broker charts."
        )
    return False, f"UNEXPECTED — pre-split NVDA closes average ${avg:,.2f}."
