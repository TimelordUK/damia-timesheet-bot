"""Render an HTML document (with local image files) to a single full-page PNG.

Used to turn an approval reply email into the agency-evidence proof image. Prefers the
already-running Chrome over CDP (so no separate Chromium download is needed — important
behind a corporate proxy that blocks the Playwright browser download); falls back to a
headless Chromium launch if CDP isn't reachable. The HTML + its images live in one directory
loaded over file:// so relative <img src> resolve.
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright


def render_html_dir_to_png(html_dir: Path, index_name: str, out_path: Path,
                           *, width: int = 900, cdp_url: str | None = None) -> Path:
    """Render html_dir/index_name (which may reference sibling image files) to out_path.

    If cdp_url is given, render in a new tab of that already-running Chrome (no download);
    on any failure there, fall back to launching headless Chromium."""
    html_dir = Path(html_dir)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    index_uri = (html_dir / index_name).resolve().as_uri()

    with sync_playwright() as pw:
        if cdp_url:
            try:
                browser = pw.chromium.connect_over_cdp(cdp_url)
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.new_page()
                try:
                    page.set_viewport_size({"width": width, "height": 1400})
                    page.goto(index_uri, wait_until="networkidle")
                    page.screenshot(path=str(out_path), full_page=True)
                finally:
                    page.close()  # close only our tab; never the user's browser
                return out_path
            except Exception:
                pass  # CDP not reachable / blocked — fall back to a headless launch

        browser = pw.chromium.launch()  # requires `playwright install chromium`
        try:
            page = browser.new_page(viewport={"width": width, "height": 1400},
                                    device_scale_factor=2)
            page.goto(index_uri, wait_until="networkidle")
            page.screenshot(path=str(out_path), full_page=True)
        finally:
            browser.close()
    return out_path
