"""Agent layer.

The numbers are already final by the time anything here runs — sectioning,
ordering, distances and scores are all computed in screener.py. These agents
only turn that payload into prose. Without ANTHROPIC_API_KEY, or if the agents
fail for any reason, write_report() returns the deterministic plain_report().
"""

from __future__ import annotations

import json
from datetime import date

from .config import Config, Secrets
from .screener import Hit, Sections


def _llm(cfg: Config, secrets: Secrets):
    from crewai import LLM

    return LLM(
        model=f"anthropic/{cfg.llm.model}",
        max_tokens=cfg.llm.max_tokens,
        api_key=secrets.anthropic_api_key,
    )


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
        "ma_slope_pct": round(r.slope_pct, 2),
        "mas_stacked_bullish": s.stacked,
        "volume_vs_avg": round(s.volume_ratio, 2),
        "first_entered": hit.first_entered,
        "score": round(hit.score, 3),
    }


def build_payload(sections: Sections, run_date: date) -> dict:
    return {
        "run_date": run_date.isoformat(),
        "pinned": [_hit_payload(h) for h in sections.pinned],
        "new": [_hit_payload(h) for h in sections.new],
        "still_in_band_not_reported": sections.repeat_count,
    }


# ---------------------------------------------------------------- daily report

ANALYST_TASK = """\
Below is today's completed moving-average scan. Every number is final — do not
recompute, re-rank, or second-guess any of it.

{payload}

Write one line per entry in the `pinned` and `new` groups: what the setup is and
what would confirm or invalidate it. Reference the actual numbers given.

`still_in_band_not_reported` is a count of names already flagged on an earlier
day and deliberately withheld. Never name or speculate about them.

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

Keep the ordering exactly as given; it is score-ranked. Omit a section that is
empty. Open with a single line stating the date and the count of new setups. If
`still_in_band_not_reported` is above zero, close with exactly:
(N still in band from earlier days)
Plain text, no markdown formatting characters. Be terse — this is read on a
phone."""


def write_report(sections: Sections, run_date: date, cfg: Config, secrets: Secrets) -> str:
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

        blob = json.dumps(build_payload(sections, run_date), indent=2)
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
    """Deterministic digest: date and counts. The per-name lines follow in the
    section messages (notify.section_messages), so they aren't repeated here."""
    lines = [f"Investimo — {run_date:%a %d %b %Y}", f"{len(sections.new)} new setup(s)"]
    if sections.pinned:
        lines.append(f"{len(sections.pinned)} pinned in band")
    if sections.total == 0:
        lines.append("Nothing new today.")
    if sections.repeat_count:
        lines.append(f"({sections.repeat_count} still in band from earlier days)")
    return "\n".join(lines)
