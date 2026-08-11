"""On-demand GEX screenshots. Never runs during the scheduled scan.

Screenshots a gamma-exposure chart with Playwright and sends it as a photo —
no model, no interpretation. You read the chart yourself.

Sources are configured in config.yaml and tried in order.
"""

from __future__ import annotations

from datetime import date

from . import store
from .config import GEX_DIR, Config


class GexNotConfigured(RuntimeError):
    pass


class GexCaptureFailed(RuntimeError):
    pass


async def _shoot(page, source: dict, timeout: int) -> bytes:
    """Screenshot the matching chart element on an already-loaded page."""
    selector = (source.get("selector") or "").strip()
    if not selector:
        return await page.screenshot(type="png", full_page=False)

    await page.wait_for_selector(selector, timeout=timeout)

    wanted = (source.get("contains") or "").strip()
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
    sources = cfg.gex.get("sources") or []
    if not sources:
        raise GexNotConfigured("No gex.sources configured in config.yaml.")

    timeout = int(cfg.gex.get("timeout_ms", 45000))
    errors: list[str] = []

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            for source in sources:
                url = source["url"].format(symbol=symbol.upper())
                page = await browser.new_page(viewport={"width": 1440, "height": 1400})
                try:
                    response = await page.goto(
                        url, timeout=timeout, wait_until="domcontentloaded"
                    )
                    if response and response.status >= 400:
                        raise GexCaptureFailed(f"HTTP {response.status}")
                    await page.wait_for_timeout(int(source.get("settle_ms", 5000)))
                    return await _shoot(page, source, timeout)
                except Exception as exc:  # noqa: BLE001 - fall through to next source
                    host = url.split("/")[2]
                    errors.append(f"{host}: {type(exc).__name__} {str(exc)[:120]}")
                finally:
                    await page.close()
        finally:
            await browser.close()

    raise GexCaptureFailed("All GEX sources failed —\n" + "\n".join(errors))


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
