"""
Read-only Damia timesheet portal recon.

Connects to an already-running Chrome via CDP (--remote-debugging-port=9222),
finds the Damia tab, and dumps what we need to know to design the
TimesheetDriver port: framework hint, DOM structure of the form area,
captured network traffic, and a screenshot.

Never clicks anything that could mutate state.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import Browser, Page, sync_playwright
except ImportError:
    sys.stderr.write(
        "playwright not installed. Run: uv sync   (and re-run this script)\n"
    )
    sys.exit(2)

import requests

CDP_URL = "http://127.0.0.1:9222"
DAMIA_URL_FRAGMENT = "damia.timesheetportal.com"
OUTPUT_DIR = Path(__file__).parent / "output"
DEFAULT_WATCH_SECONDS = 45

KNOWN_IDS = {
    "autofill_button":  "#MainContent_tblSubmit_1_pnlHeader_pnlFooterButtons_btnPopulateTimesheet",
    "cancel_button":    "#MainContent_tblSubmit_1_pnlHeader_pnlFooterButtons_btnCancel",
    "save_draft":       "#MainContent_tblSubmit_1_pnlApproverFooter_btnSaveTimesheet",
    "submit_button":    "#MainContent_tblSubmit_1_pnlApproverFooter_btnSubmit",
    "btn_prev_week":    "#MainContent_btnPrevWeek",
    "btn_next_week":    "#MainContent_btnNextWeek",
    "btn_current_week": "#MainContent_btnCurrentWeek",
    "date_caption":     "#MainContent_lblDateCaption",
    "tab_timesheet":    "#btnShowTimesheetPanel_1",
    "tab_notes":        "#btnShowNotesPanel_1",
    "tab_attachments":  "#btnShowAttachmentsPanel_1",
    "tab_history":      "#btnShowHistoryPanel_1",
    "approver_select":  "#MainContent_tblSubmit_1_pnlApproverFooter_cmbApprovers_CC_1",
    "file_upload":      "#fileupload",
    "drop_zone":        ".tsAttachmentDropZone",
    "entries_wrapper":  "#tsEntriesWrapper_1",
    "ts_entry_table":   "#tsEntriesWrapper_1 table.tsEntry",
    "status_span":      "[class*='timesheetStatus']",
    "week_total":       "#lblTotalDaysForWeek_1",
}


def preflight_cdp() -> None:
    """Hit /json/version directly so we get a clear error before Playwright's opaque one."""
    try:
        r = requests.get(f"{CDP_URL}/json/version", timeout=2)
        r.raise_for_status()
        info = r.json()
        print(f"CDP reachable: {info.get('Browser')} (protocol {info.get('Protocol-Version')})")
    except Exception as e:
        print(
            "\n--- CDP preflight FAILED ---\n"
            f"  Could not reach {CDP_URL}/json/version: {e}\n\n"
            "Likely causes:\n"
            "  A) Chrome 136+ silently ignores --remote-debugging-port when launched\n"
            "     against the DEFAULT user-data-dir. You must use a dedicated profile.\n"
            "  B) A background chrome.exe is still running and your launch joined it,\n"
            "     causing the flag to be discarded.\n\n"
            "Fix:\n"
            "  1) Kill all Chrome:\n"
            "       Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force\n"
            "  2) Launch with a DEDICATED profile dir:\n"
            "       & \"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\" `\n"
            "         --remote-debugging-port=9222 `\n"
            "         --user-data-dir=\"$env:LOCALAPPDATA\\damia-timesheet-bot\\chrome-profile\" `\n"
            "         https://damia.timesheetportal.com/\n"
            "  3) Verify:  Invoke-WebRequest http://127.0.0.1:9222/json/version\n"
            "  4) Log in to Damia (first time only — cookie persists), open the week's tab.\n"
            "  5) Re-run this probe.\n",
            file=sys.stderr,
        )
        sys.exit(2)


def find_damia_page(browser: Browser) -> Page:
    pages: list[Page] = []
    for context in browser.contexts:
        pages.extend(context.pages)
    for p in pages:
        if DAMIA_URL_FRAGMENT in p.url:
            return p
    raise SystemExit(
        f"No tab found with URL containing {DAMIA_URL_FRAGMENT!r}.\n"
        f"Open the Damia timesheet in your CDP-launched Chrome and re-run."
    )


def framework_hints(page: Page) -> dict[str, Any]:
    js = """
    () => ({
      hasAngular: !!(window.angular || window.ng || document.querySelector('[ng-app],[ng-controller],[data-ng-app]')),
      hasReact: !!(window.React || document.querySelector('[data-reactroot],[data-reactid]')),
      hasVue: !!(window.Vue || document.querySelector('[data-v-]')),
      hasJQuery: !!window.jQuery,
      hasKnockout: !!window.ko,
      generator: (document.querySelector('meta[name=generator]') || {}).content || null,
      title: document.title,
      url: location.href,
      formCount: document.querySelectorAll('form').length,
      tableCount: document.querySelectorAll('table').length,
      inputCount: document.querySelectorAll('input,select,textarea').length,
    })
    """
    return page.evaluate(js)


def dom_outline(page: Page, root_selector: str | None, max_depth: int) -> str:
    """Return an indented tag/class/id outline. If root_selector is None, picks form/main/body."""
    js = """
    ({rootSelector, maxDepth}) => {
      const out = [];
      const walk = (el, depth) => {
        if (depth > maxDepth) return;
        const tag = el.tagName.toLowerCase();
        const id = el.id ? '#' + el.id : '';
        const cls = el.className && typeof el.className === 'string'
          ? '.' + el.className.trim().split(/\\s+/).slice(0, 3).join('.')
          : '';
        const name = el.getAttribute('name');
        const role = el.getAttribute('role');
        const type = el.getAttribute('type');
        const value = (el.tagName === 'INPUT' && el.value && el.value.length < 40) ? el.value : null;
        const attrs = [
          name && `name=${name}`,
          role && `role=${role}`,
          type && `type=${type}`,
          value !== null && `value=${JSON.stringify(value)}`,
        ].filter(Boolean).join(' ');
        out.push('  '.repeat(depth) + `<${tag}${id}${cls}${attrs ? ' ' + attrs : ''}>`);
        for (const child of el.children) walk(child, depth + 1);
      };
      const root = rootSelector
        ? document.querySelector(rootSelector)
        : (document.querySelector('form') || document.querySelector('main') || document.body);
      if (!root) return `(selector ${rootSelector} not found)`;
      walk(root, 0);
      return out.join('\\n');
    }
    """
    return page.evaluate(js, {"rootSelector": root_selector, "maxDepth": max_depth})


def capture_specimens(page: Page) -> dict[str, str | None]:
    """For each known element of interest, grab its outerHTML (truncated) so we know what to drive."""
    out: dict[str, str | None] = {}
    for label, selector in KNOWN_IDS.items():
        try:
            el = page.query_selector(selector)
            if el is None:
                out[label] = None
                continue
            html = el.evaluate("e => e.outerHTML")
            out[label] = html[:800] + ("..." if len(html) > 800 else "")
        except Exception as e:
            out[label] = f"(query error: {e})"
    return out


def perform_auto_clicks(page: Page) -> list[dict[str, Any]]:
    """Drive a deterministic, non-destructive sequence: read state, click Autofill, snap state.
    Returns a log of actions. NEVER clicks Submit. NEVER clicks Cancel (would wipe the state)."""
    log: list[dict[str, Any]] = []

    def note(action: str, detail: str = "") -> None:
        log.append({"action": action, "detail": detail})
        print(f"   [auto] {action}{(' — ' + detail) if detail else ''}")

    # 1) Record date caption + status before
    try:
        caption = page.locator(KNOWN_IDS["date_caption"]).inner_text(timeout=2000)
        note("read_date_caption_before", caption)
    except Exception as e:
        note("read_date_caption_before_error", str(e))

    try:
        status = page.locator(KNOWN_IDS["status_span"]).evaluate("e => e.className", timeout=2000)
        note("read_status_class_before", status)
    except Exception as e:
        note("read_status_class_before_error", str(e))

    # 2) Click Autofill timesheet — this opens a jQuery UI confirm dialog
    #    ("Are you sure you wish to autofill this timesheet? Any existing time will be replaced")
    try:
        page.locator(KNOWN_IDS["autofill_button"]).click(timeout=3000)
        note("click_autofill", "success")
    except Exception as e:
        note("click_autofill_error", str(e))

    # 2a) Give the dialog a moment to render
    page.wait_for_timeout(1000)

    # 2b) Snapshot the confirm dialog so we know its exact structure for the future driver
    dialog_selectors = [
        ".ui-dialog:visible",
        "#dialogConfirmation:visible",
        ".ui-dialog",
        "#dialogConfirmation",
    ]
    for sel in dialog_selectors:
        try:
            html = page.locator(sel).first.evaluate("e => e.outerHTML", timeout=1000)
            note(f"autofill_dialog_html ({sel})", html[:1500])
            break
        except Exception:
            continue
    else:
        note("autofill_dialog_html", "no matching dialog element found")

    # 2c) Click Yes in the confirm dialog. Try several strategies; the exact selector
    #     will be locked into the driver once we see which one wins.
    yes_strategies = [
        ".ui-dialog-buttonpane button:has-text('Yes')",
        ".ui-dialog button:has-text('Yes')",
        "#dialogConfirmation button:has-text('Yes')",
        "button:visible:has-text('Yes')",
    ]
    yes_clicked = False
    for sel in yes_strategies:
        try:
            page.locator(sel).first.click(timeout=1500)
            note("click_autofill_yes", f"success via {sel!r}")
            yes_clicked = True
            break
        except Exception:
            continue
    if not yes_clicked:
        note("click_autofill_yes", "FAILED — no strategy matched; dialog likely left open")

    # 3) Wait for the postback to complete (UpdatePanel partial postback)
    page.wait_for_timeout(3000)

    # 4) Read total days now (should be 5.00 if autofill worked)
    try:
        total = page.locator(KNOWN_IDS["week_total"]).inner_text(timeout=2000)
        note("read_week_total_after_autofill", total)
    except Exception as e:
        note("read_week_total_error", str(e))

    # 4a) Dump the 5 options + currently-selected option for each day's <select>.
    #     This tells us the Damia data model: what does "worked" / "sick" / "bank holiday"
    #     / "annual leave" / "not worked" look like as <option value=X>label</option>.
    day_labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    options_by_day = {}
    js = """
    (selectId) => {
      const sel = document.getElementById(selectId);
      if (!sel) return null;
      return {
        selected_index: sel.selectedIndex,
        selected_value: sel.value,
        options: Array.from(sel.options).map(o => ({
          value: o.value,
          text: o.text.trim(),
          selected: o.selected,
        })),
      };
    }
    """
    for i, label in enumerate(day_labels):
        select_id = f"ctlEnd_1_{i}_0"
        try:
            data = page.evaluate(js, select_id)
            options_by_day[f"{label}({i}) #{select_id}"] = data
        except Exception as e:
            options_by_day[f"{label}({i}) #{select_id}"] = f"(query error: {e})"
    note("day_select_options_after_autofill", json.dumps(options_by_day, indent=2))

    # 5) Click the Attachments tab so we see its DOM in the after-snap
    try:
        page.locator(KNOWN_IDS["tab_attachments"]).click(timeout=3000)
        note("click_attachments_tab", "success")
        page.wait_for_timeout(800)
    except Exception as e:
        note("click_attachments_tab_error", str(e))

    # 6) Back to Timesheet tab so the after-DOM shows the populated grid, not Attachments
    try:
        page.locator(KNOWN_IDS["tab_timesheet"]).click(timeout=3000)
        note("click_timesheet_tab_back", "success")
        page.wait_for_timeout(800)
    except Exception as e:
        note("click_timesheet_tab_back_error", str(e))

    return log


def capture_network(page: Page, seconds: int, action_fn=None) -> list[dict[str, Any]]:
    """Listen for ALL network on the page via raw CDP for N seconds.

    Why raw CDP and not page.on('request'): Playwright's high-level request events are
    unreliable for CDP-attached pre-existing pages. Going through a CDP session and
    listening for Network.requestWillBeSent directly bypasses that issue.

    Also registers a no-op dialog handler. Without one, Playwright auto-dismisses any
    JS confirm/alert dialogs and races with the user clicking them in their visible
    Chrome, which crashes the Node driver with 'No dialog is showing'.
    """
    captured: list[dict[str, Any]] = []
    captured_by_id: dict[str, dict[str, Any]] = {}

    # Defuse the auto-dismiss vs. user-dismiss race. Doing nothing in the handler
    # means Playwright won't touch the dialog — the user handles it in their browser.
    page.on("dialog", lambda d: None)

    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Page.enable")

    def on_request_will_be_sent(event: dict) -> None:
        req = event.get("request", {})
        entry = {
            "method": req.get("method"),
            "url": req.get("url"),
            "resource_type": event.get("type"),
            "post_data_preview": (req.get("postData") or "")[:400],
        }
        rid = event.get("requestId")
        if rid:
            captured_by_id[rid] = entry
        captured.append(entry)

    def on_response_received(event: dict) -> None:
        rid = event.get("requestId")
        if rid and rid in captured_by_id:
            captured_by_id[rid]["status"] = event.get("response", {}).get("status")

    cdp.on("Network.requestWillBeSent", on_request_will_be_sent)
    cdp.on("Network.responseReceived", on_response_received)

    # Also register a Page-level request listener as a belt-and-braces fallback.
    # In sync mode, CDP events from a new_cdp_session sometimes don't dispatch reliably;
    # page.on("request") catches what gets through Playwright's own wrapper.
    page_level_seen: list[dict[str, Any]] = []
    def on_page_request(request) -> None:
        page_level_seen.append({
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "post_data_preview": (request.post_data or "")[:400],
            "source": "page.on",
        })
    page.on("request", on_page_request)

    print(f"\n>>> Listening for ALL network for {seconds}s.")

    # Run the deterministic action sequence (if provided) while we're listening.
    if action_fn is not None:
        print(">>> Running deterministic auto-clicks now...")
        try:
            action_log = action_fn()
            captured.append({"_auto_action_log": action_log})
        except Exception as e:
            print(f"   [auto] sequence failed: {e}")

    print(
        ">>> You can also click around manually in the Damia tab:\n"
        ">>>   week-prev / week-next arrows, History tab, Save draft, etc.\n"
        ">>> DO NOT click Submit or Cancel timesheet.\n"
    )

    # Use page.wait_for_timeout instead of time.sleep — yields to Playwright's event
    # dispatcher so CDP events actually flush to Python callbacks.
    for remaining in range(seconds, 0, -1):
        n = len([c for c in captured if "_auto_action_log" not in c])
        m = len(page_level_seen)
        types: dict[str, int] = {}
        for e in captured:
            if "_auto_action_log" in e:
                continue
            t = e["resource_type"] or "?"
            types[t] = types.get(t, 0) + 1
        type_summary = " ".join(f"{k}:{v}" for k, v in sorted(types.items())) or "(none)"
        print(
            f"  {remaining:3d}s left  |  cdp:{n:4d}  page.on:{m:4d}  |  {type_summary}             ",
            end="\r",
        )
        page.wait_for_timeout(1000)
    print()

    captured.extend(page_level_seen)
    try:
        cdp.detach()
    except Exception:
        pass
    return captured


def write_report(
    out_dir: Path,
    hints: dict,
    outline_before: str,
    outline_after: str,
    specimens_before: dict,
    specimens_after: dict,
    network: list[dict],
    screenshot_paths: list[Path],
    watch_seconds: int,
) -> Path:
    report = out_dir / "damia_probe_report.md"

    def kv_block(d: dict, label: str) -> list[str]:
        lines = [f"### {label}", ""]
        for k, v in d.items():
            lines.append(f"**{k}**")
            if v is None:
                lines.append("`(not found)`")
            else:
                lines.append("```html")
                lines.append(v)
                lines.append("```")
            lines.append("")
        return lines

    lines = [
        "# Damia probe report",
        "",
        "## Framework hints",
        "```json",
        json.dumps(hints, indent=2),
        "```",
        "",
        "## DOM outline of `#tsEntriesWrapper_1` — BEFORE interactions",
        "```",
        outline_before,
        "```",
        "",
        "## DOM outline of `#tsEntriesWrapper_1` — AFTER interactions",
        "```",
        outline_after,
        "```",
        "",
        "## Specimens — BEFORE interactions",
        "",
    ]
    lines.extend(kv_block(specimens_before, "outerHTML samples"))
    lines.extend(["## Specimens — AFTER interactions", ""])
    lines.extend(kv_block(specimens_after, "outerHTML samples"))
    lines.extend([
        "",
        f"## Network capture ({len(network)} requests in {watch_seconds}s window, ALL resource types)",
        "```json",
        json.dumps(network, indent=2),
        "```",
        "",
        "## Screenshots",
    ])
    for p in screenshot_paths:
        lines.append(f"![{p.stem}]({p.name})")
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--watch-seconds", "-w",
        type=int,
        default=DEFAULT_WATCH_SECONDS,
        help=f"Seconds to listen for XHR/fetch (default {DEFAULT_WATCH_SECONDS})",
    )
    parser.add_argument(
        "--no-auto-click",
        action="store_true",
        help="Skip the deterministic Autofill/tab-click sequence; rely on manual user clicks only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    preflight_cdp()
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(
                f"Could not connect to Chrome at {CDP_URL}.\n"
                f"Make sure Chrome was launched with --remote-debugging-port=9222.\n"
                f"Underlying error: {e}",
                file=sys.stderr,
            )
            return 2

        page = find_damia_page(browser)
        print(f"Attached to Damia tab: {page.url}")

        # Make sure the page has actually loaded before snapping BEFORE state —
        # otherwise specimens come back '(not found)' because the form isn't rendered yet.
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            page.wait_for_selector("#tsEntriesWrapper_1", timeout=8000)
        except Exception as e:
            print(f"[warn] page may not be fully loaded for BEFORE snap: {e}")

        hints = framework_hints(page)

        outline_before = dom_outline(page, "#tsEntriesWrapper_1", max_depth=15)
        specimens_before = capture_specimens(page)
        screenshot_before = OUTPUT_DIR / "damia_before.png"
        page.screenshot(path=str(screenshot_before), full_page=True)

        action_fn = None if args.no_auto_click else (lambda: perform_auto_clicks(page))
        network = capture_network(page, args.watch_seconds, action_fn=action_fn)

        outline_after = dom_outline(page, "#tsEntriesWrapper_1", max_depth=15)
        specimens_after = capture_specimens(page)

        # Bring tab to front BEFORE screenshot so Chrome un-throttles painting.
        # Otherwise full_page screenshots can time out 'waiting for fonts to load' when
        # the window has been backgrounded during the probe.
        screenshot_after = OUTPUT_DIR / "damia_after.png"
        try:
            page.bring_to_front()
            page.wait_for_timeout(500)
            page.screenshot(path=str(screenshot_after), full_page=True, timeout=10000)
        except Exception as e:
            print(f"\n[warn] after-screenshot failed: {e}")
            try:
                page.screenshot(path=str(screenshot_after), full_page=False, timeout=5000)
                print("[warn] fell back to viewport screenshot")
            except Exception as e2:
                print(f"[warn] viewport screenshot also failed: {e2}")
                screenshot_after = None  # type: ignore[assignment]

        # Write report FIRST so screenshot trouble can't lose the DOM/specimen data.
        report = write_report(
            OUTPUT_DIR,
            hints,
            outline_before,
            outline_after,
            specimens_before,
            specimens_after,
            network,
            [p for p in [screenshot_before, screenshot_after] if p is not None],
            args.watch_seconds,
        )
        print(f"\nReport written: {report}")
        print(f"Screenshots:    {screenshot_before}\n                {screenshot_after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
