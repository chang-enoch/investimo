"""Settings from .env plus config.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "investimo.db"
GEX_DIR = DATA / "gex"


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    alpaca_key: str = ""
    alpaca_secret: str = ""
    anthropic_api_key: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""

    def missing(self, *names: str) -> list[str]:
        return [n for n in names if not getattr(self, n)]


@dataclass(frozen=True)
class MA:
    name: str
    kind: str  # "ema" | "sma"
    period: int


@dataclass(frozen=True)
class Config:
    lookback_days: int
    atr_period: int
    k: float
    rearm_margin: float
    mas: tuple[MA, ...]
    weights: dict[str, float]
    llm: dict
    gex: dict

    @property
    def rearm_k(self) -> float:
        return self.k * self.rearm_margin


def load_config(path: Path | None = None) -> Config:
    raw = yaml.safe_load((path or ROOT / "config.yaml").read_text())
    scan = raw["scan"]
    return Config(
        lookback_days=scan["lookback_days"],
        atr_period=scan["atr_period"],
        k=float(scan["k"]),
        rearm_margin=float(scan["rearm_margin"]),
        mas=tuple(MA(m["name"], m["kind"], m["period"]) for m in scan["mas"]),
        weights=raw["scoring"],
        llm=raw["llm"],
        gex=raw.get("gex", {}),
    )


def load_seed_universe() -> list[str]:
    """Symbols from data/seed_universe.yaml -> `universe:`."""
    path = DATA / "seed_universe.yaml"
    if not path.exists():
        return []
    return list(yaml.safe_load(path.read_text()).get("universe") or [])
