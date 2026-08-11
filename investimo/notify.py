"""Telegram delivery: the digest, then one message per ticker carrying buttons."""

from __future__ import annotations

import asyncio
from datetime import date

from .config import Secrets
from .screener import Hit, Sections

MAX_LEN = 4000  # Telegram caps at 4096


def _keyboard(symbol: str, pinned: bool):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    pin_btn = (
        InlineKeyboardButton("📍 Unpin", callback_data=f"unpin:{symbol}")
        if pinned
        else InlineKeyboardButton("📌 Pin", callback_data=f"pin:{symbol}")
    )
    return InlineKeyboardMarkup(
        [[pin_btn, InlineKeyboardButton("📊 GEX", callback_data=f"gex:{symbol}")]]
    )


def _line(hit: Hit, compact: bool = False) -> str:
    r, s = hit.reading, hit.snapshot
    side = "above" if r.above else "below"
    if compact:
        return f"{hit.symbol} · {r.name} · {r.distance_atr:.2f} ATR {side}"
    return (
        f"{hit.symbol} · {r.name}\n"
        f"{s.close:,.2f} vs {r.value:,.2f} ({r.distance_atr:.2f} ATR {side})\n"
        f"slope {r.slope_pct:+.2f}% · vol {s.volume_ratio:.1f}x"
        f"{' · stacked' if s.stacked else ''}"
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


async def send_report(
    report_text: str,
    sections: Sections,
    pinned: set[str],
    run_date: date,
    secrets: Secrets | None = None,
) -> int:
    """Send the digest plus per-ticker button rows. Returns messages sent."""
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

    groups = (
        ("📌 PINNED", sections.pinned, False),
        ("⭐ NEW", sections.new, False),
        ("🔁 REPEAT", sections.repeat, True),
    )
    for title, hits, compact in groups:
        if not hits:
            continue
        await bot.send_message(chat_id=chat, text=f"— {title} —")
        sent += 1
        for hit in hits:
            await bot.send_message(
                chat_id=chat,
                text=_line(hit, compact),
                reply_markup=_keyboard(hit.symbol, hit.symbol in pinned),
            )
            sent += 1
            await asyncio.sleep(0.05)  # stay clear of Telegram's rate limit

    return sent


async def send_text(text: str, secrets: Secrets | None = None) -> None:
    from telegram import Bot

    secrets = secrets or Secrets()
    bot = Bot(token=secrets.telegram_token)
    for chunk in _chunks(text):
        await bot.send_message(chat_id=secrets.telegram_chat_id, text=chunk)
