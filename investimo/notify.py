"""Telegram delivery.

One message per section, not per ticker — a heavy day would otherwise be dozens
of separate messages. Telegram attaches inline keyboards per message, so the
per-ticker Pin/GEX buttons live behind a drill-down: each section message
carries a keyboard of ticker symbols, and tapping one opens that ticker's own
message with its actions.
"""

from __future__ import annotations

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


def hit_line(hit: Hit) -> str:
    r, s = hit.reading, hit.snapshot
    side = "▲" if r.above else "▼"
    return (
        f"{hit.symbol} · {r.name} {side}{r.distance_atr:.2f} ATR · "
        f"{s.close:,.2f} vs {r.value:,.2f} · slope {r.slope_pct:+.1f}% · "
        f"vol {s.volume_ratio:.1f}x{' · stacked' if s.stacked else ''}"
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


def section_messages(sections: Sections) -> list[tuple[str, list[str]]]:
    """(text, symbols for the button grid) per non-empty section."""
    out = []
    for title, hits in (("📌 PINNED", sections.pinned), ("⭐ NEW", sections.new)):
        if not hits:
            continue
        symbols = list(dict.fromkeys(h.symbol for h in hits))  # unique, score order
        note = (
            f"\n\n(buttons for the first {MAX_BUTTONS} of {len(symbols)} tickers)"
            if len(symbols) > MAX_BUTTONS
            else ""
        )
        body = "\n".join(hit_line(h) for h in hits)
        out.append((f"— {title} —\n{body}{note}", symbols))
    return out


async def send_report(report_text: str, sections: Sections, secrets: Secrets | None = None) -> int:
    """Digest, then one message per non-empty section. Returns messages sent."""
    from telegram import Bot

    secrets = secrets or Secrets()
    missing = secrets.missing("telegram_token", "telegram_chat_id")
    if missing:
        raise RuntimeError(f"Missing {', '.join(m.upper() for m in missing)} in .env")

    chat = secrets.telegram_chat_id
    sent = 0
    async with Bot(token=secrets.telegram_token) as bot:
        for chunk in _chunks(report_text):
            await bot.send_message(chat_id=chat, text=chunk)
            sent += 1
        for text, symbols in section_messages(sections):
            for i, chunk in enumerate(_chunks(text)):
                await bot.send_message(
                    chat_id=chat,
                    text=chunk,
                    reply_markup=ticker_keyboard(symbols) if i == 0 else None,
                )
                sent += 1
    return sent
