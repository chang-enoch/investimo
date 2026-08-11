"""On-demand GEX screenshots. Never runs during the scheduled scan.

Screenshots a GEX chart with Playwright and sends it as a photo — no model,
no interpretation. You read the chart yourself.

`gex.url_template` and `gex.chart_selector` in config.yaml must be set before
this works; the site is deliberately not hardcoded.
"""

from __future__ import annotations

from datetime import date

from . import store
from .config import GEX_DIR, Config


class GexNotConfigured(RuntimeError):
    pass


async def capture(symbol: str, cfg: Config) -> bytes:
    """Screenshot the chart element for `symbol`."""
    template = (cfg.gex.get("url_template") or "").strip()
    if not template:
        raise GexNotConfigured(
            "gex.url_template is empty in config.yaml — set the GEX page URL "
            "(use {symbol} as the placeholder) and gex.chart_selector."
        )

    from playwright.async_api import async_playwright

    url = template.format(symbol=symbol.upper())
    selector = (cfg.gex.get("chart_selector") or "").strip()
    timeout = int(cfg.gex.get("timeout_ms", 20000))

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            await page.goto(url, timeout=timeout, wait_until="networkidle")
            if selector:
                element = await page.wait_for_selector(selector, timeout=timeout)
                return await element.screenshot(type="png")
            return await page.screenshot(type="png", full_page=False)
        finally:
            await browser.close()


async def get(symbol: str, cfg: Config, run_date: date | None = None) -> bytes:
    """Chart image, cached per day so a second tap skips the browser."""
    symbol = symbol.upper()
    run_date = run_date or date.today()

    cached = store.get_gex(symbol, run_date)
    if cached and cached.get("image_path"):
        try:
            return (GEX_DIR / cached["image_path"]).read_bytes()
        except OSError:
            pass  # cached file went missing; re-capture below

    image = await capture(symbol, cfg)

    GEX_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{symbol}_{run_date:%Y%m%d}.png"
    (GEX_DIR / filename).write_bytes(image)
    store.put_gex(symbol, run_date, filename, "")

    return image
