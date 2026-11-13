"""Settings: secrets from .env, everything else from config.yaml (validated on load)."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "investimo.db"
GEX_DIR = DATA / "gex"

# An EMA is seeded from the first bar, so a short history leaves it visibly off
# from broker charts. 3x the period gets it to within rounding.
EMA_WARMUP = 3
# Extra sessions fetched beyond the strict minimum (slope window, holidays, gaps).
BAR_BUFFER = 80


class ConfigError(RuntimeError):
    pass


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    alpaca_key: str = ""
    alpaca_secret: str = ""
    anthropic_api_key: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""

    def missing(self, *names: str) -> list[str]:
        return [n for n in names if not getattr(self, n)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MA(_Frozen):
    name: str
    kind: Literal["ema", "sma"]
    period: int = Field(gt=1)

    @property
    def warmup(self) -> int:
        """Bars needed before this MA's latest value is trustworthy."""
        return self.period * (EMA_WARMUP if self.kind == "ema" else 1)


class Weights(_Frozen):
    """Relative weights for the score that orders each report section."""

    distance: float = 1.0   # closer to the MA scores higher
    slope: float = 0.5      # MA rising
    stacking: float = 0.5   # shorter MAs above longer ones
    volume: float = 0.3     # latest volume vs its average


class Llm(_Frozen):
    model: str = "claude-opus-5"
    max_tokens: int = 16000


class GexSource(_Frozen):
    url: str                # "{symbol}" is substituted
    selector: str = ""      # element to screenshot; empty = whole viewport
    contains: str = ""      # among matches, pick the one whose text contains this
    settle_ms: int = 5000   # wait after load for charts to paint


class Gex(_Frozen):
    sources: tuple[GexSource, ...] = ()
    timeout_ms: int = 45000


class Config(_Frozen):
    mas: tuple[MA, ...] = Field(min_length=1)
    k: float = Field(0.5, gt=0)
    rearm_margin: float = Field(1.5, ge=1)
    atr_period: int = Field(14, gt=1)
    slope_bars: int = Field(5, gt=0)
    volume_window: int = Field(20, gt=0)
    weights: Weights = Weights()
    llm: Llm = Llm()
    gex: Gex = Gex()

    @model_validator(mode="after")
    def _unique_ma_names(self) -> Config:
        names = [m.name for m in self.mas]
        if len(set(names)) != len(names):
            raise ValueError(f"scan.mas names must be unique, got {names}")
        return self

    @property
    def rearm_k(self) -> float:
        return self.k * self.rearm_margin

    @property
    def min_bars(self) -> int:
        """Shortest history a symbol needs to be scanned at all."""
        longest = max(max(m.warmup for m in self.mas), self.volume_window + 1)
        return longest + self.atr_period

    @property
    def lookback_days(self) -> int:
        """Calendar days of bars to request (~252 sessions per 365 days)."""
        return math.ceil((self.min_bars + BAR_BUFFER) * 365 / 252)


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "config.yaml"
    raw = yaml.safe_load(path.read_text()) or {}
    try:
        return Config(
            **raw.get("scan", {}),
            weights=raw.get("scoring", {}),
            llm=raw.get("llm", {}),
            gex=raw.get("gex", {}),
        )
    except (ValidationError, TypeError) as exc:
        raise ConfigError(f"Invalid {path.name}:\n{exc}") from None


def load_seed_universe() -> list[str]:
    """Symbols from data/seed_universe.yaml -> `universe:`."""
    path = DATA / "seed_universe.yaml"
    if not path.exists():
        return []
    return list(yaml.safe_load(path.read_text()).get("universe") or [])
