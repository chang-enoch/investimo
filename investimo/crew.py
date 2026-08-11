"""Agent layer.

The numbers are already final by the time anything here runs — sectioning,
ordering, distances and scores are all computed in screener.py. These agents
only turn that payload into prose. If ANTHROPIC_API_KEY is unset, every entry
point falls back to a deterministic plain-text rendering so the pipeline still
works end to end.
"""

from __future__ import annotations

import json
from datetime import date

from .config import Config, Secrets
from .screener import Hit, Sections


def _llm(cfg: Config, secrets: Secrets):
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=cfg.llm["model"],
        max_tokens=cfg.llm.get("max_tokens", 16000),
        api_key=secrets.anthropic_api_key,
    )


def _tools():
    """LangChain tools the Analyst can call for context we didn't pre-compute."""
    from langchain_core.tools import tool

    from . import store

    @tool
    def get_flag_history(symbol: str) -> str:
        """Prior moving-average approaches for a ticker: which MAs it is currently
        near, its state (NEW/REPEAT/ARMED), and when the current approach began."""
        rows = store.flag_history(symbol)
        return json.dumps(rows) if rows else f"No prior flag history for {symbol}."

    @tool
    def get_pinned() -> str:
        """Tickers the user has pinned for ongoing attention."""
        return ", ".join(sorted(store.pins())) or "none"

    return [get_flag_history, get_pinned]


def _hit_payload(hit: Hit) -> dict:
    s, r = hit.snapshot, hit.reading
    return {
        "symbol": hit.symbol,
        "ma": r.name,
        "state": hit.state,
        "close": round(s.close, 2),
        "ma_value": round(r.value, 2),
        "distance_atr": round(r.distance_atr, 2),
        "price_is": "above" if r.above else "below",
        "ma_slope_pct_5d": round(r.slope_pct, 2),
        "stacked_21_50_200": s.stacked,
        "volume_vs_20d": round(s.volume_ratio, 2),
        "first_entered": hit.first_entered,
        "score": round(hit.score, 3),
    }


def build_payload(sections: Sections, run_date: date) -> dict:
    return {
        "run_date": run_date.isoformat(),
        "pinned": [_hit_payload(h) for h in sections.pinned],
        "new": [_hit_payload(h) for h in sections.new],
        "repeat": [_hit_payload(h) for h in sections.repeat],
    }


# ---------------------------------------------------------------- daily report

ANALYST_TASK = """\
Below is today's completed moving-average scan. Every number is final — do not
recompute, re-rank, or second-guess any of it.

{payload}

Write one line per entry in the `pinned` and `new` groups: what the setup is and
what would confirm or invalidate it. Reference the actual numbers given. Use
get_flag_history when knowing how a name has behaved on previous approaches
changes the read.

For `repeat`, write nothing per-name — these are already-seen setups.

Rules:
- One sentence per name, two at most. No preamble, no headers, no bullets.
- Format each line exactly as: SYMBOL (ma) — your note
- Say plainly when a setup looks weak. Do not manufacture conviction.
- Never state a price, distance, or direction that is not in the data above."""

EDITOR_TASK = """\
Assemble the final Telegram report from the analyst's notes and this data:

{payload}

Structure, exactly:
📌 PINNED
⭐ NEW
🔁 REPEAT — one compact line per name: SYMBOL ma ±N.NN ATR

Keep the ordering exactly as given; it is score-ranked. Omit a section that is
empty. Open with a single line stating the date and the count of new setups.
Plain text, no markdown formatting characters. Be terse — this is read on a
phone."""


def write_report(sections: Sections, run_date: date, cfg: Config, secrets: Secrets) -> str:
    payload = build_payload(sections, run_date)

    if not secrets.anthropic_api_key:
        return plain_report(sections, run_date)

    try:
        from crewai import Agent, Crew, Process, Task

        llm = _llm(cfg, secrets)

        analyst = Agent(
            role="Technical Analyst",
            goal="Explain what each flagged moving-average approach means for a swing trader.",
            backstory=(
                "You read price action around moving averages for a discretionary swing "
                "trader. You are concise and you never overstate a setup."
            ),
            llm=llm,
            tools=_tools(),
            verbose=False,
            allow_delegation=False,
        )
        editor = Agent(
            role="Report Editor",
            goal="Assemble a scannable end-of-day Telegram briefing.",
            backstory="You format trading notes for fast reading on a phone.",
            llm=llm,
            verbose=False,
            allow_delegation=False,
        )

        blob = json.dumps(payload, indent=2)
        crew = Crew(
            agents=[analyst, editor],
            tasks=[
                Task(
                    description=ANALYST_TASK.format(payload=blob),
                    expected_output="One line per pinned and new entry.",
                    agent=analyst,
                ),
                Task(
                    description=EDITOR_TASK.format(payload=blob),
                    expected_output="The finished plain-text report.",
                    agent=editor,
                ),
            ],
            process=Process.sequential,
            verbose=False,
        )
        return str(crew.kickoff()).strip()
    except Exception as exc:  # noqa: BLE001 - never lose the report over the prose layer
        return f"{plain_report(sections, run_date)}\n\n(writeup unavailable: {exc})"


def plain_report(sections: Sections, run_date: date) -> str:
    """Deterministic fallback. Same information, no LLM."""
    lines = [f"Investimo — {run_date:%a %d %b %Y}", f"{len(sections.new)} new setup(s)"]

    def block(title: str, hits: list[Hit], compact: bool = False) -> None:
        if not hits:
            return
        lines.append("")
        lines.append(title)
        for h in hits:
            r, s = h.reading, h.snapshot
            side = "above" if r.above else "below"
            if compact:
                lines.append(f"{h.symbol} {r.name} {r.distance_atr:.2f} ATR {side}")
            else:
                lines.append(
                    f"{h.symbol} ({r.name}) — {s.close:,.2f} vs {r.value:,.2f}, "
                    f"{r.distance_atr:.2f} ATR {side}, slope {r.slope_pct:+.2f}%, "
                    f"vol {s.volume_ratio:.1f}x"
                )

    block("📌 PINNED", sections.pinned)
    block("⭐ NEW", sections.new)
    block("🔁 REPEAT", sections.repeat, compact=True)

    if sections.total == 0:
        lines.append("")
        lines.append("Nothing within range today.")
    return "\n".join(lines)


# ------------------------------------------------------------------ gex reader

GEX_PROMPT = """\
This is a gamma exposure (GEX) chart for {symbol}. Report, if visible:
- call wall (largest positive GEX strike)
- put wall (largest negative GEX strike)
- gamma flip / zero-gamma level
- whether net GEX is positive or negative, and what that implies for whether
  dealer hedging is likely to dampen or amplify moves

Four lines maximum, plain text. If a level is not legible, say so rather than
guessing — a wrong level is worse than a missing one."""


def read_gex_chart(symbol: str, image_bytes: bytes, cfg: Config, secrets: Secrets) -> str:
    """Vision read of a GEX screenshot."""
    if not secrets.anthropic_api_key:
        return "(no ANTHROPIC_API_KEY — chart only, no written read)"

    import base64

    from langchain_core.messages import HumanMessage

    b64 = base64.standard_b64encode(image_bytes).decode()
    try:
        resp = _llm(cfg, secrets).invoke(
            [
                HumanMessage(
                    content=[
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                        {"type": "text", "text": GEX_PROMPT.format(symbol=symbol)},
                    ]
                )
            ]
        )
        return str(resp.content).strip()
    except Exception as exc:  # noqa: BLE001
        return f"(GEX read failed: {exc})"
