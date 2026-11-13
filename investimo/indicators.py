"""Pure indicator math. No I/O, no LLM — these numbers must be exactly right."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from .config import MA, Config


def ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder-smoothed average true range."""
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False).mean()


def moving_average(close: pd.Series, spec: MA) -> pd.Series:
    if spec.kind == "ema":
        return ema(close, spec.period)
    if spec.kind == "sma":
        return sma(close, spec.period)
    raise ValueError(f"Unknown MA kind: {spec.kind!r}")


@dataclass(frozen=True)
class MaReading:
    name: str
    value: float
    distance: float          # close - MA (signed; positive = price above)
    distance_atr: float      # |distance| / ATR
    above: bool
    slope_pct: float         # MA change over cfg.slope_bars bars, as % of the MA


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    as_of: date
    close: float
    atr: float
    volume: float
    avg_volume: float        # mean of the cfg.volume_window bars before the latest
    mas: dict[str, MaReading]
    stacked: bool            # shorter MAs above longer ones (e.g. 21 > 50 > 200)

    @property
    def volume_ratio(self) -> float:
        return self.volume / self.avg_volume if self.avg_volume else 1.0


def compute(symbol: str, df: pd.DataFrame, cfg: Config) -> Snapshot | None:
    """Indicator snapshot for the most recent bar, or None if history is short."""
    if len(df) < cfg.min_bars:
        return None

    close = df["close"]
    atr_series = atr(df, cfg.atr_period)
    last_atr = float(atr_series.iloc[-1])
    if not last_atr or pd.isna(last_atr):
        return None

    last_close = float(close.iloc[-1])
    readings: dict[str, MaReading] = {}
    for spec in cfg.mas:
        series = moving_average(close, spec)
        value = float(series.iloc[-1])
        if pd.isna(value):
            continue
        prior = float(series.iloc[-1 - cfg.slope_bars])
        distance = last_close - value
        readings[spec.name] = MaReading(
            name=spec.name,
            value=value,
            distance=distance,
            distance_atr=abs(distance) / last_atr,
            above=distance >= 0,
            slope_pct=((value - prior) / value * 100) if value else 0.0,
        )

    by_period = sorted((m for m in cfg.mas if m.name in readings), key=lambda m: m.period)
    ordered = [readings[m.name].value for m in by_period]
    stacked = len(ordered) > 1 and all(a > b for a, b in zip(ordered, ordered[1:]))

    return Snapshot(
        symbol=symbol,
        as_of=df.index[-1],
        close=last_close,
        atr=last_atr,
        volume=float(df["volume"].iloc[-1]),
        avg_volume=float(df["volume"].iloc[-1 - cfg.volume_window : -1].mean()),
        mas=readings,
        stacked=stacked,
    )
