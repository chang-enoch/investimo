"""Telegram polling service: inline buttons and slash commands."""

from __future__ import annotations

import asyncio
from datetime import date

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from . import crew, gex, main as run, notify, store
from .config import Secrets, load_config

HELP = """\
/scan [YYYY-MM-DD] — run the scan now (does not advance New/Repeat state)
/status — health of the scheduled job
/list — active universe
/add TICKER… — add to the universe
/remove TICKER… — remove
/pins — pinned tickers
/pin TICKER · /unpin TICKER
/gex TICKER — gamma exposure chart + read
/config — current thresholds and weights
/help — this list"""


def _args(update: Update, context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    return [a.strip().upper().lstrip("$") for a in (context.args or []) if a.strip()]


# ---------------------------------------------------------------- commands

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Investimo ready.\nThis chat's id is {update.effective_chat.id}\n\n{HELP}"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    on = date.fromisoformat(args[0]) if args else None

    store.init()
    count = len(store.universe())
    await update.message.reply_text(f"⏳ Scanning {count} tickers…")

    try:
        # advance_state=False: an ad-hoc scan must not consume a NEW status
        # before the scheduled report has shown it.
        result = await asyncio.to_thread(
            run.scan, on, advance_state=False
        )
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"Scan failed: {exc}")
        return

    await notify.send_report(
        result.report, result.sections, store.pins(), result.run_date
    )
    if result.skipped:
        await update.message.reply_text(f"No data: {', '.join(result.skipped)}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(await asyncio.to_thread(run.status_text))


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store.init()
    symbols = store.universe()
    pinned = store.pins()
    if not symbols:
        await update.message.reply_text("Universe is empty. Use /add TICKER.")
        return
    marked = [f"📌{s}" if s in pinned else s for s in symbols]
    await update.message.reply_text(f"{len(symbols)} tickers:\n" + " ".join(marked))


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbols = _args(update, context)
    if not symbols:
        await update.message.reply_text("Usage: /add NVDA AMD")
        return
    store.init()
    added = store.add_tickers(symbols)
    skipped = sorted(set(symbols) - set(added))
    parts = []
    if added:
        parts.append(f"Added: {', '.join(added)}")
    if skipped:
        parts.append(f"Already present: {', '.join(skipped)}")
    await update.message.reply_text("\n".join(parts))


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbols = _args(update, context)
    if not symbols:
        await update.message.reply_text("Usage: /remove NVDA AMD")
        return
    store.init()
    removed = store.remove_tickers(symbols)
    missing = sorted(set(symbols) - set(removed))
    parts = []
    if removed:
        parts.append(f"Removed: {', '.join(removed)}")
    if missing:
        parts.append(f"Not in universe: {', '.join(missing)}")
    await update.message.reply_text("\n".join(parts))


async def cmd_pins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store.init()
    pinned = sorted(store.pins())
    await update.message.reply_text(
        "📌 " + ", ".join(pinned) if pinned else "Nothing pinned."
    )


async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbols = _args(update, context)
    if not symbols:
        await update.message.reply_text("Usage: /pin NVDA")
        return
    store.init()
    for s in symbols:
        store.pin(s)
    await update.message.reply_text(f"📌 Pinned {', '.join(symbols)}")


async def cmd_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbols = _args(update, context)
    if not symbols:
        await update.message.reply_text("Usage: /unpin NVDA")
        return
    store.init()
    done = [s for s in symbols if store.unpin(s)]
    await update.message.reply_text(
        f"📍 Unpinned {', '.join(done)}" if done else "Those weren't pinned."
    )


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    mas = ", ".join(f"{m.name}({m.kind}{m.period})" for m in cfg.mas)
    weights = ", ".join(f"{k}={v}" for k, v in cfg.weights.items())
    await update.message.reply_text(
        f"k = {cfg.k} ATR\nrearm_margin = {cfg.rearm_margin} "
        f"(re-arms past {cfg.rearm_k:.2f} ATR)\n"
        f"ATR period = {cfg.atr_period}\nMAs: {mas}\nWeights: {weights}\n"
        f"Model: {cfg.llm['model']}"
    )


async def cmd_gex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbols = _args(update, context)
    if not symbols:
        await update.message.reply_text("Usage: /gex NVDA")
        return
    await _send_gex(update.effective_chat.id, symbols[0], context)


# ---------------------------------------------------------------- callbacks

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, symbol = (query.data or "").partition(":")
    store.init()

    if action == "pin":
        store.pin(symbol)
        await query.answer(f"📌 {symbol} pinned")
        await _swap_pin_button(query, symbol, pinned=True)
    elif action == "unpin":
        store.unpin(symbol)
        await query.answer(f"📍 {symbol} unpinned")
        await _swap_pin_button(query, symbol, pinned=False)
    elif action == "gex":
        await query.answer("Fetching GEX…")
        await _send_gex(
            query.message.chat_id, symbol, context, reply_to=query.message.message_id
        )
    else:
        await query.answer()


async def _swap_pin_button(query, symbol: str, pinned: bool) -> None:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    pin_btn = (
        InlineKeyboardButton("📍 Unpin", callback_data=f"unpin:{symbol}")
        if pinned
        else InlineKeyboardButton("📌 Pin", callback_data=f"pin:{symbol}")
    )
    keyboard = InlineKeyboardMarkup(
        [[pin_btn, InlineKeyboardButton("📊 GEX", callback_data=f"gex:{symbol}")]]
    )
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except Exception:  # noqa: BLE001 - message may be too old to edit
        pass


async def _send_gex(chat_id: int, symbol: str, context, reply_to: int | None = None) -> None:
    cfg, secrets = load_config(), Secrets()
    store.init()
    try:
        image, summary = await gex.get(symbol, cfg, secrets)
    except gex.GexNotConfigured as exc:
        await context.bot.send_message(chat_id=chat_id, text=str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        await context.bot.send_message(chat_id=chat_id, text=f"GEX failed for {symbol}: {exc}")
        return

    if image:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=image,
            caption=f"📊 {symbol}\n{summary}"[:1024],
            reply_to_message_id=reply_to,
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id, text=f"📊 {symbol}\n{summary}", reply_to_message_id=reply_to
        )


# ---------------------------------------------------------------- entrypoint

def build_app() -> Application:
    secrets = Secrets()
    if not secrets.telegram_token:
        raise SystemExit("TELEGRAM_TOKEN is not set in .env")

    app = Application.builder().token(secrets.telegram_token).build()
    for name, handler in [
        ("start", cmd_start),
        ("help", cmd_help),
        ("scan", cmd_scan),
        ("status", cmd_status),
        ("list", cmd_list),
        ("add", cmd_add),
        ("remove", cmd_remove),
        ("pins", cmd_pins),
        ("pin", cmd_pin),
        ("unpin", cmd_unpin),
        ("config", cmd_config),
        ("gex", cmd_gex),
    ]:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(on_button))
    return app


def cli() -> None:
    store.init()
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    cli()
