"""On-demand GEX screenshots. Never runs during the scheduled scan.

Screenshots a gamma-exposure chart with Playwright and sends it as a photo —
no model, no interpretation. You read the chart yourself.

Sources are configured in config.yaml and tried in order.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .config import GEX_DIR, Config, GexSource
from .market_data import is_symbol, market_today

CACHE_DAYS = 14  # images older than this are deleted on the next capture


class GexCaptureFailed(RuntimeError):
    pass


async def _shoot(page, source: GexSource, timeout: int) -> bytes:
    """Screenshot the matching chart element on an already-loaded page."""
    selector = source.selector.strip()
    if not selector:
        return await page.screenshot(type="png", full_page=False)

    await page.wait_for_selector(selector, timeout=timeout)

    wanted = source.contains.strip()
    if not wanted:
        return await page.locator(selector).first.screenshot(type="png")

    # Several charts can match the selector; pick the one whose SVG text
    # contains `wanted`. Ids here are generated per page load, so matching on
    # content is the only stable handle.
    count = await page.locator(selector).count()
    for i in range(count):
        element = page.locator(selector).nth(i)
        text = await element.evaluate("e => e.textContent || ''")
        if wanted.lower() in text.lower():
            return await element.screenshot(type="png")

    raise GexCaptureFailed(
        f"No element matching {selector!r} contained {wanted!r} "
        f"({count} candidate(s) on the page — the site's markup may have changed)."
    )


async def capture(symbol: str, cfg: Config) -> bytes:
    """Try each configured source in order; return the first chart obtained."""
    if not cfg.gex.sources:
        raise GexCaptureFailed("No gex.sources configured in config.yaml.")

    timeout = cfg.gex.timeout_ms
    errors: list[str] = []

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            for source in cfg.gex.sources:
                url = source.url.format(symbol=symbol.upper())
                page = await browser.new_page(viewport={"width": 1440, "height": 1400})
                try:
                    response = await page.goto(
                        url, timeout=timeout, wait_until="domcontentloaded"
                    )
                    if response and response.status >= 400:
                        raise GexCaptureFailed(f"HTTP {response.status}")
                    await page.wait_for_timeout(source.settle_ms)
                    return await _shoot(page, source, timeout)
                except Exception as exc:  # noqa: BLE001 - fall through to next source
                    host = url.split("/")[2]
                    errors.append(f"{host}: {type(exc).__name__} {str(exc)[:120]}")
                finally:
                    await page.close()
        finally:
            await browser.close()

    raise GexCaptureFailed("All GEX sources failed —\n" + "\n".join(errors))


async def get(symbol: str, cfg: Config) -> bytes:
    """Chart image, cached per symbol per market day so a second tap skips the browser."""
    symbol = symbol.upper()
    if not is_symbol(symbol):
        raise GexCaptureFailed(f"{symbol!r} is not a valid ticker.")
    today = market_today()
    path = GEX_DIR / f"{symbol}_{today:%Y%m%d}.png"
    if path.exists():
        return path.read_bytes()

    image = await capture(symbol, cfg)
    GEX_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image)
    _prune(today - timedelta(days=CACHE_DAYS))
    return image


def _prune(before: date) -> None:
    for old in GEX_DIR.glob("*_????????.png"):
        try:
            if datetime.strptime(old.stem[-8:], "%Y%m%d").date() < before:
                old.unlink()
        except ValueError:
            pass
