"""On-demand GEX capture. Never runs during the scheduled scan.

Screenshots a GEX chart with Playwright, hands the image to the vision agent,
and caches both per (symbol, day) so a second tap skips the browser.

`gex.url_template` and `gex.chart_selector` in config.yaml must be set before
this works — the site is deliberately not hardcoded.
"""

from __future__ import annotations

from datetime import date

from . import store
from .config import GEX_DIR, Config, Secrets
from .crew import read_gex_chart


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


async def get(
    symbol: str, cfg: Config, secrets: Secrets, run_date: date | None = None
) -> tuple[bytes | None, str]:
    """Chart bytes plus written read, cached per day. Returns (image, summary)."""
    symbol = symbol.upper()
    run_date = run_date or date.today()

    cached = store.get_gex(symbol, run_date)
    if cached:
        path = cached.get("image_path")
        data = None
        if path:
            try:
                data = (GEX_DIR / path).read_bytes()
            except OSError:
                data = None
        return data, cached["summary"]

    image = await capture(symbol, cfg)
    summary = read_gex_chart(symbol, image, cfg, secrets)

    GEX_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{symbol}_{run_date:%Y%m%d}.png"
    (GEX_DIR / filename).write_bytes(image)
    store.put_gex(symbol, run_date, filename, summary)

    return image, summary
