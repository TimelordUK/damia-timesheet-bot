"""Read-only recon of the Damia Attachments tab, so we can design the attachment puller
(gap #2: pull the user's uploaded approval screenshot off each Approved week).

Navigates to an Approved week, discovers the per-week timesheet id, clicks the Attachments
tab (a __doPostBack div), and dumps everything attachment-related: the tab buttons, the
panel container, and every <a href> / <img src> / file-row inside it. Never mutates.

Usage:
    uv run python -m spikes.attachments_probe                 # default week 24/05/26
    uv run python -m spikes.attachments_probe --week 17/05/26
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from damia_timesheet_bot.adapters.timesheet.damia_playwright import DamiaTimesheetDriver

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", default="24/05/26", help="Sunday of an Approved week (dd/mm/yy).")
    args = parser.parse_args()
    target = datetime.strptime(args.week, "%d/%m/%y").date()

    with DamiaTimesheetDriver().attached() as drv:
        print(f">>> Navigating to {target} ...")
        drv.navigate_to_week(target)
        start, end = drv.current_week_range()
        tid = drv.timesheet_id
        print(f"    week {start} → {end}, status={drv.status_word()}, timesheet_id={tid}")

        # 1) Discover all tab buttons (Timesheet / Notes / Attachments / History).
        tabs = drv.page.evaluate(
            """() => Array.from(document.querySelectorAll("[id^='btnShow'][id*='Panel_']"))
                 .map(el => ({ id: el.id, text: (el.innerText||'').trim(), onclick: el.getAttribute('onclick') }))"""
        )
        print("\n--- tab buttons ---")
        print(json.dumps(tabs, indent=2))

        # 2) Click the Attachments tab via DOM click (fires onclick regardless of visibility).
        att_tab = next((t for t in tabs if "attach" in (t["text"] + t["id"]).lower()), None)
        if not att_tab:
            print("\n[!] No Attachments tab button found.")
            return 1
        print(f"\n>>> Clicking Attachments tab: #{att_tab['id']}")
        drv.page.locator(f"#{att_tab['id']}").evaluate("el => el.click()")
        drv.page.wait_for_timeout(1500)

        # 3) Dump everything attachment-related now that the panel is shown.
        dump = drv.page.evaluate(
            """() => {
              const interesting = el => {
                const id = (el.id||'').toLowerCase();
                const cls = (el.className && typeof el.className==='string' ? el.className : '').toLowerCase();
                return id.includes('attach') || cls.includes('attach');
              };
              const containers = Array.from(document.querySelectorAll('*')).filter(interesting)
                .map(el => ({ tag: el.tagName.toLowerCase(), id: el.id||'', classes: (typeof el.className==='string'?el.className:''), }));

              // Links and images anywhere in an attachment-ish container.
              const roots = Array.from(document.querySelectorAll('*')).filter(interesting);
              const links = [], images = [];
              for (const r of roots) {
                for (const a of r.querySelectorAll('a[href]')) {
                  links.push({ href: a.getAttribute('href'), text: (a.innerText||'').trim().slice(0,80),
                               onclick: a.getAttribute('onclick'), download: a.getAttribute('download') });
                }
                for (const img of r.querySelectorAll('img[src]')) {
                  images.push({ src: (img.getAttribute('src')||'').slice(0,200), alt: img.getAttribute('alt')||'' });
                }
              }
              // Dedupe links by href.
              const seen = new Set(), ulinks = [];
              for (const l of links) { if (!seen.has(l.href)) { seen.add(l.href); ulinks.push(l); } }
              return { containers, links: ulinks, images };
            }"""
        )
        print("\n--- attachment-ish containers ---")
        print(json.dumps(dump["containers"], indent=2))
        print("\n--- links inside them ---")
        print(json.dumps(dump["links"], indent=2))
        print("\n--- images inside them ---")
        print(json.dumps(dump["images"], indent=2))

        # 4) Outer HTML of the first attachment container, for selector design.
        if dump["containers"]:
            cid = next((c["id"] for c in dump["containers"] if c["id"]), None)
            if cid:
                html = drv.page.locator(f"#{cid}").first.evaluate("e => e.outerHTML")
                print(f"\n--- outerHTML of #{cid} (truncated 3000) ---")
                print(html[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
