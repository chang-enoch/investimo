"""Telegram delivery.

One message per section, not per ticker — a 58-hit day would otherwise be 60
separate messages. Telegram attaches inline keyboards per message, so the
per-ticker Pin/GEX buttons live behind a drill-down: each section message
carries a keyboard of ticker symbols, and tapping one opens that ticker's own
message with its actions.
"""

from __future__ import annotations

from datetime import date

from .config import Secrets
from .screener import Hit, Sections

MAX_LEN = 4000       # Telegram caps at 4096
MAX_BUTTONS = 36     # keyboard rows get unwieldy past this
BUTTONS_PER_ROW = 3


def ticker_keyboard(symbols: list[str]):
    """Grid of symbol buttons that drill into a per-ticker action message."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows, row = [], []
    for sym in symbols[:MAX_BUTTONS]:
        row.append(InlineKeyboardButton(sym, callback_data=f"sym:{sym}"))
        if len(row) == BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows) if rows else None


def action_keyboard(symbol: str, pinned: bool):
    """Pin/Unpin + GEX for a single ticker."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    pin_btn = (
        InlineKeyboardButton("📍 Unpin", callback_data=f"unpin:{symbol}")
        if pinned
        else InlineKeyboardButton("📌 Pin", callback_data=f"pin:{symbol}")
    )
    return InlineKeyboardMarkup(
        [[pin_btn, InlineKeyboardButton("📊 GEX", callback_data=f"gex:{symbol}")]]
    )


def hit_line(hit: Hit, compact: bool = False) -> str:
    r, s = hit.reading, hit.snapshot
    side = "▲" if r.above else "▼"
    if compact:
        return f"{hit.symbol} {r.name} {side}{r.distance_atr:.2f}"
    return (
        f"{hit.symbol} · {r.name} {side}{r.distance_atr:.2f} ATR · "
        f"{s.close:,.2f} vs {r.value:,.2f} · slope {r.slope_pct:+.1f}%"
        f"{' · stacked' if s.stacked else ''}"
    )


def detail_text(hit: Hit) -> str:
    r, s = hit.reading, hit.snapshot
    return (
        f"{hit.symbol} · {r.name}\n"
        f"close {s.close:,.2f} vs {r.value:,.2f}\n"
        f"{r.distance_atr:.2f} ATR {'above' if r.above else 'below'}\n"
        f"slope {r.slope_pct:+.2f}% · vol {s.volume_ratio:.1f}x"
        f"{' · 21>50>200' if s.stacked else ''}\n"
        f"state {hit.state}"
    )


def _chunks(text: str, size: int = MAX_LEN):
    while text:
        if len(text) <= size:
            yield text
            return
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > 0 else size
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def _dedup(hits: list[Hit]) -> list[str]:
    """Unique symbols, preserving score order."""
    seen, out = set(), []
    for h in hits:
        if h.symbol not in seen:
            seen.add(h.symbol)
            out.append(h.symbol)
    return out


async def send_report(
    report_text: str,
    sections: Sections,
    pinned: set[str],
    run_date: date,
    secrets: Secrets | None = None,
) -> int:
    """Digest, then one message per non-empty section. Returns messages sent."""
    from telegram import Bot

    secrets = secrets or Secrets()
    missing = secrets.missing("telegram_token", "telegram_chat_id")
    if missing:
        raise RuntimeError(f"Missing {', '.join(m.upper() for m in missing)} in .env")

    bot = Bot(token=secrets.telegram_token)
    chat = secrets.telegram_chat_id
    sent = 0

    for chunk in _chunks(report_text):
        await bot.send_message(chat_id=chat, text=chunk)
        sent += 1

    for title, hits, compact in (
        ("📌 PINNED", sections.pinned, False),
        ("⭐ NEW", sections.new, False),
        ("🔁 REPEAT", sections.repeat, True),
    ):
        if not hits:
            continue
        body = "\n".join(hit_line(h, compact) for h in hits)
        symbols = _dedup(hits)
        note = (
            f"\n\n(buttons for the first {MAX_BUTTONS} of {len(symbols)} tickers)"
            if len(symbols) > MAX_BUTTONS
            else ""
        )
        for i, chunk in enumerate(_chunks(f"— {title} —\n{body}{note}")):
            await bot.send_message(
                chat_id=chat,
                text=chunk,
                reply_markup=ticker_keyboard(symbols) if i == 0 else None,
            )
            sent += 1

    return sent


async def send_text(text: str, secrets: Secrets | None = None) -> None:
    from telegram import Bot

    secrets = secrets or Secrets()
    bot = Bot(token=secrets.telegram_token)
    for chunk in _chunks(text):
        await bot.send_message(chat_id=secrets.telegram_chat_id, text=chunk)
