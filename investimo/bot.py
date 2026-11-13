"""Telegram polling service: inline buttons and slash commands."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from . import gex, main, notify, store
from .config import ConfigError, Secrets, load_config
from .market_data import is_symbol

log = logging.getLogger("investimo.bot")

HELP = """\
/scan [YYYY-MM-DD] — run the scan now (does not advance New/Repeat state)
/status — health of the scheduled job
/list — active universe
/add TICKER… — add to the universe
/remove TICKER… — remove (also unpins)
/pins — pinned tickers
/pin TICKER… — pin (adds to the universe if needed) · /unpin TICKER…
/gex TICKER — gamma exposure chart
/config — current MAs, thresholds and weights
/help — this list"""


async def _reply(update: Update, text: str) -> None:
    # effective_message: the owner filter also matches edited messages, where
    # update.message is None.
    await update.effective_message.reply_text(text)


async def _symbols(
    update: Update, context: ContextTypes.DEFAULT_TYPE, usage: str
) -> list[str] | None:
    """Tickers from the command args ("NVDA AMD", "nvda,amd", "$NVDA").
    Replies and returns None if there are none or any are malformed."""
    raw = " ".join(context.args or [])
    tokens = [t.upper().lstrip("$") for t in re.split(r"[\s,]+", raw) if t]
    bad = [t for t in tokens if not is_symbol(t)]
    if not tokens or bad:
        prefix = f"Not a ticker: {', '.join(bad)}\n" if bad else ""
        await _reply(update, f"{prefix}Usage: {usage}")
        return None
    return list(dict.fromkeys(tokens))


# ---------------------------------------------------------------- commands

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, f"Investimo ready.\nThis chat's id is {update.effective_chat.id}\n\n{HELP}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, HELP)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        on = date.fromisoformat(context.args[0]) if context.args else None
    except ValueError:
        await _reply(update, "Usage: /scan [YYYY-MM-DD]")
        return

    await _reply(update, f"⏳ Scanning {len(store.universe())} tickers…")
    try:
        # main.scan() never writes state, so an ad-hoc scan can't consume a
        # NEW status before the scheduled report has shown it.
        result = await asyncio.to_thread(main.scan, on)
    except Exception as exc:  # noqa: BLE001
        await _reply(update, f"Scan failed: {exc}")
        return

    await notify.send_report(result.report, result.sections)
    if result.skipped:
        await _reply(update, f"No data: {', '.join(result.skipped)}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, await asyncio.to_thread(main.status_text))


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbols = store.universe()
    if not symbols:
        await _reply(update, "Universe is empty. Use /add TICKER.")
        return
    pinned = store.pins()
    marked = [f"📌{s}" if s in pinned else s for s in symbols]
    await _reply(update, f"{len(symbols)} tickers:\n" + " ".join(marked))


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not (symbols := await _symbols(update, context, "/add NVDA AMD")):
        return
    added = store.add_tickers(symbols)
    present = [s for s in symbols if s not in added]
    parts = []
    if added:
        parts.append(f"Added: {', '.join(added)}")
    if present:
        parts.append(f"Already present: {', '.join(present)}")
    await _reply(update, "\n".join(parts))


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not (symbols := await _symbols(update, context, "/remove NVDA AMD")):
        return
    removed = store.remove_tickers(symbols)
    missing = [s for s in symbols if s not in removed]
    parts = []
    if removed:
        parts.append(f"Removed: {', '.join(removed)}")
    if missing:
        parts.append(f"Not in universe: {', '.join(missing)}")
    await _reply(update, "\n".join(parts))


async def cmd_pins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pinned = sorted(store.pins())
    await _reply(update, "📌 " + ", ".join(pinned) if pinned else "Nothing pinned.")


async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not (symbols := await _symbols(update, context, "/pin NVDA")):
        return
    universe = set(store.universe())
    for s in symbols:
        store.pin(s)
    added = [s for s in symbols if s not in universe]
    note = f"\n(added to the universe: {', '.join(added)})" if added else ""
    await _reply(update, f"📌 Pinned {', '.join(symbols)}{note}")


async def cmd_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not (symbols := await _symbols(update, context, "/unpin NVDA")):
        return
    done = [s for s in symbols if store.unpin(s)]
    await _reply(update, f"📍 Unpinned {', '.join(done)}" if done else "Those weren't pinned.")


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    mas = ", ".join(f"{m.name}({m.kind}{m.period})" for m in cfg.mas)
    weights = ", ".join(f"{k}={v}" for k, v in cfg.weights.model_dump().items())
    await _reply(
        update,
        f"k = {cfg.k} ATR\nrearm_margin = {cfg.rearm_margin} "
        f"(re-arms past {cfg.rearm_k:.2f} ATR)\n"
        f"ATR period = {cfg.atr_period} · slope over {cfg.slope_bars} bars · "
        f"volume vs {cfg.volume_window}-bar avg\nMAs: {mas}\nWeights: {weights}\n"
        f"Model: {cfg.llm.model}",
    )


async def cmd_gex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not (symbols := await _symbols(update, context, "/gex NVDA")):
        return
    await _send_gex(update.effective_chat.id, symbols[0], context)


# ---------------------------------------------------------------- callbacks

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, symbol = (query.data or "").partition(":")
    if str(query.message.chat_id) != context.bot_data["owner_chat"] or not is_symbol(symbol):
        await query.answer()
        return

    if action == "sym":
        # Drill-down from a section message into one ticker's actions.
        await query.answer()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=symbol,
            reply_markup=notify.action_keyboard(symbol, symbol in store.pins()),
            reply_to_message_id=query.message.message_id,
        )
    elif action == "pin":
        store.pin(symbol)
        await query.answer(f"📌 {symbol} pinned")
        await _swap_pin_button(query, symbol, pinned=True)
    elif action == "unpin":
        store.unpin(symbol)
        await query.answer(f"📍 {symbol} unpinned")
        await _swap_pin_button(query, symbol, pinned=False)
    elif action == "gex":
        await query.answer("Fetching GEX…")
        await _send_gex(query.message.chat_id, symbol, context, reply_to=query.message.message_id)
    else:
        await query.answer()


async def _swap_pin_button(query, symbol: str, pinned: bool) -> None:
    try:
        await query.edit_message_reply_markup(reply_markup=notify.action_keyboard(symbol, pinned))
    except Exception:  # noqa: BLE001 - message may be too old to edit
        pass


async def _send_gex(chat_id: int, symbol: str, context, reply_to: int | None = None) -> None:
    try:
        image = await gex.get(symbol, load_config())
    except Exception as exc:  # noqa: BLE001
        await context.bot.send_message(chat_id=chat_id, text=f"GEX failed for {symbol}: {exc}")
        return
    await context.bot.send_photo(
        chat_id=chat_id, photo=image, caption=f"📊 {symbol}", reply_to_message_id=reply_to
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Handler failed", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=f"⚠️ Error: {context.error}"
            )
        except Exception:  # noqa: BLE001 - nothing more to do if Telegram itself fails
            pass


# ---------------------------------------------------------------- entrypoint

def build_app() -> Application:
    secrets = Secrets()
    if not secrets.telegram_token:
        raise SystemExit("TELEGRAM_TOKEN is not set in .env")
    try:
        load_config()  # fail at startup, not on the first tap
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None

    # Everything except /start answers only the configured chat: the bot edits
    # the watchlist and launches a browser, so strangers must not reach it.
    # /start stays open because it is how you learn the chat id to configure.
    if secrets.telegram_chat_id:
        owner = filters.Chat(chat_id=int(secrets.telegram_chat_id))
    else:
        log.warning("TELEGRAM_CHAT_ID unset: only /start will respond. Send it to get the id.")
        owner = filters.Chat(chat_id=[])

    # concurrent_updates: a 30 s GEX capture or /scan shouldn't freeze every
    # other button while it runs.
    app = Application.builder().token(secrets.telegram_token).concurrent_updates(True).build()
    app.bot_data["owner_chat"] = secrets.telegram_chat_id  # callbacks take no filters
    app.add_handler(CommandHandler("start", cmd_start))
    for name, handler in [
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
        app.add_handler(CommandHandler(name, handler, filters=owner))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    return app


def cli() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per poll otherwise
    build_app().run_polling(
        allowed_updates=["message", "callback_query"], drop_pending_updates=True
    )


if __name__ == "__main__":
    cli()
