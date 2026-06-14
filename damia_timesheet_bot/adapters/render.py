"""Render an HTML document (with local image files) to a single full-page PNG.

Used to turn an approval reply email into the agency-evidence proof image. Launches a headless
Chromium via Playwright (already a dependency). The HTML and its images are written into one
directory and loaded over file:// so relative <img src> resolve.
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright


def render_html_dir_to_png(html_dir: Path, index_name: str, out_path: Path,
                           *, width: int = 900) -> Path:
    """Render html_dir/index_name (which may reference sibling image files) to out_path."""
    html_dir = Path(html_dir)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    index = (html_dir / index_name).resolve()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 1400},
                                    device_scale_factor=2)
            page.goto(index.as_uri(), wait_until="networkidle")
            page.screenshot(path=str(out_path), full_page=True)
        finally:
            browser.close()
    return out_path
