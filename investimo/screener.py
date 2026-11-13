"""Band classification and scoring.

Per (symbol, ma_type):

    ARMED  -- enters band (|close - MA| <= k*ATR) --> NEW
    NEW    -- still in band next run --------------> REPEAT
    REPEAT -- still in band -----------------------> REPEAT
    any    -- leaves band past k*rearm_margin -----> ARMED

Only NEW is reported. REPEAT exists to stay silent: a name sitting on its MA for
a week is one event, not seven, and re-reporting it daily buries the new ones.
It is counted (`Sections.repeat_count`) but produces no Hit.

Pinned symbols are reported regardless of state, in their own section only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import Config
from .indicators import MaReading, Snapshot

ARMED, NEW, REPEAT = "ARMED", "NEW", "REPEAT"


@dataclass
class Hit:
    symbol: str
    snapshot: Snapshot
    reading: MaReading
    state: str               # NEW, or REPEAT for a pinned name
    first_entered: str | None
    score: float = 0.0


@dataclass
class Sections:
    pinned: list[Hit] = field(default_factory=list)
    new: list[Hit] = field(default_factory=list)
    repeat_count: int = 0     # unpinned names still in band, deliberately not reported
    state_updates: dict[tuple[str, str], tuple[str, str | None]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.pinned) + len(self.new)

    @property
    def new_count(self) -> int:
        """First touches, including pinned names that are NEW today."""
        return len(self.new) + sum(h.state == NEW for h in self.pinned)


def score(hit: Hit, cfg: Config) -> float:
    """Higher is more interesting. Weights live in config.yaml."""
    w = cfg.weights
    r, s = hit.reading, hit.snapshot

    proximity = max(0.0, 1.0 - r.distance_atr / cfg.k)
    slope = max(-1.0, min(1.0, r.slope_pct / 2.0))
    stacking = 1.0 if s.stacked else 0.0
    volume = max(-1.0, min(2.0, s.volume_ratio - 1.0))

    return (
        w.distance * proximity
        + w.slope * slope
        + w.stacking * stacking
        + w.volume * volume
    )


def classify(
    snapshots: dict[str, Snapshot],
    prior_state: dict[tuple[str, str], dict],
    pinned: set[str],
    cfg: Config,
    run_date: date,
) -> Sections:
    out = Sections()

    for symbol, snap in sorted(snapshots.items()):
        for ma_name, reading in snap.mas.items():
            key = (symbol, ma_name)
            prev = prior_state.get(key, {})
            prev_state = prev.get("state", ARMED)
            first_entered = prev.get("first_entered_date")

            in_band = reading.distance_atr <= cfg.k
            out_of_band = reading.distance_atr > cfg.rearm_k

            if in_band:
                if prev_state == ARMED:
                    state, first_entered = NEW, run_date.isoformat()
                else:
                    state = REPEAT
            elif out_of_band:
                state, first_entered = ARMED, None
            else:
                # Hysteresis gap: past the flag band but not far enough to
                # re-arm. Hold the prior state and stay quiet.
                state = prev_state

            out.state_updates[key] = (state, first_entered)

            if not in_band:
                continue
            is_pinned = symbol in pinned
            if state == REPEAT and not is_pinned:
                out.repeat_count += 1
                continue

            hit = Hit(symbol, snap, reading, state, first_entered)
            hit.score = score(hit, cfg)
            (out.pinned if is_pinned else out.new).append(hit)

    for bucket in (out.pinned, out.new):
        bucket.sort(key=lambda h: h.score, reverse=True)
    return out

