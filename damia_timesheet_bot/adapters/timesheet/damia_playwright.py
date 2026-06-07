"""DamiaTimesheetDriver — DOM-driven Playwright adapter for damia.timesheetportal.com.

Attaches to an already-running Chrome via CDP (the user's session stays theirs; we never
touch credentials). All mutating operations are partial-postback driven via the page's
own __doPostBack JS. NEVER clicks Submit. NEVER clicks Cancel timesheet.
"""
from __future__ import annotations

import json as _json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterator

import requests
from playwright.sync_api import Browser, Page, sync_playwright

from ...core.models import Day, DayKind, Week

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DAMIA_URL_FRAGMENT = "damia.timesheetportal.com"

# Selectors that are NOT parameterized by the per-week timesheet ID.
SEL_DATE_CAPTION     = "#MainContent_lblDateCaption"
SEL_BTN_PREV_WEEK    = "#MainContent_btnPrevWeek"
SEL_BTN_NEXT_WEEK    = "#MainContent_btnNextWeek"
SEL_BTN_CURRENT_WEEK = "#MainContent_btnCurrentWeek"
SEL_STATUS_SPAN      = "[class*='timesheetStatus']"
# Affirm/decline in a jQuery UI confirm dialog. Text varies; class is the stable signal.
SEL_CONFIRM_AFFIRM   = ".ui-dialog-buttonpane button.is-success"
SEL_CONFIRM_DECLINE  = ".ui-dialog-buttonpane button.is-danger"
# Any button in a currently-visible jQuery UI dialog. Used to close the "not a valid
# period for this job" alert Damia raises when you try to step before the contract start.
SEL_DIALOG_ANY_BUTTON = ".ui-dialog:visible .ui-dialog-buttonpane button"

# Selectors that ARE parameterized by the per-week Damia timesheet ID (an integer that
# Damia embeds in every form-element id for a given timesheet). The driver discovers
# this id dynamically after each navigation.
FMT_ENTRIES_WRAPPER   = "#tsEntriesWrapper_{tid}"
FMT_BTN_AUTOFILL      = "#MainContent_tblSubmit_{tid}_pnlHeader_pnlFooterButtons_btnPopulateTimesheet"
FMT_BTN_CANCEL        = "#MainContent_tblSubmit_{tid}_pnlHeader_pnlFooterButtons_btnCancel"
FMT_BTN_DOWNLOAD      = "#MainContent_tblSubmit_{tid}_pnlHeader_pnlFooterButtons_btnDownload"
FMT_BTN_SAVE_DRAFT    = "#MainContent_tblSubmit_{tid}_pnlApproverFooter_btnSaveTimesheet"
FMT_BTN_SUBMIT        = "#MainContent_tblSubmit_{tid}_pnlApproverFooter_btnSubmit"  # never wired
FMT_WEEK_TOTAL        = "#lblTotalDaysForWeek_{tid}"
FMT_DAY_SELECT        = "#ctlEnd_{tid}_{day}_0"
FMT_DAILY_TOTAL       = "#pnlDailyDaysDetail_{tid}_{day}"
FMT_BTN_ATTACH_TAB    = "#btnShowAttachmentsPanel_{tid}"  # onclick=showAttachmentPanel(tid)
FMT_ATTACH_WRAPPER    = "#tsAttachmentWrapper_{tid}"

DAMIA_VALUES = ("1.00", "0.75", "0.50", "0.25", "0.00")
STATUS_CLASS_RE  = re.compile(r"timesheetStatus(\w+)")
DATE_CAPTION_RE  = re.compile(r"(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})")
TIMESHEET_ID_RE  = re.compile(r"tsEntriesWrapper_(\d+)")


def units_to_damia_value(units: float) -> str:
    """Snap a float in [0, 1] to the nearest Damia option string."""
    if units < 0 or units > 1:
        raise ValueError(f"units must be in [0,1]; got {units}")
    snapped = round(units * 4) / 4
    return f"{snapped:.2f}"


def _parse_damia_date(s: str) -> date:
    return datetime.strptime(s, "%d/%m/%y").date()


def _parse_units_from_cell(cell: dict) -> float:
    if cell.get("value"):
        try:
            return float(cell["value"])
        except (TypeError, ValueError):
            pass
    text = (cell.get("text") or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


@dataclass
class DamiaTimesheetDriver:
    """Adapter implementing the TimesheetDriver port for Damia.

    Lifecycle:
        with DamiaTimesheetDriver().attached() as drv:
            drv.read_week()
            drv.fill_week(week)
    """
    cdp_url: str = DEFAULT_CDP_URL
    _pw: object | None = field(default=None, repr=False)
    _browser: Browser | None = field(default=None, repr=False)
    _page: Page | None = field(default=None, repr=False)
    timesheet_id: int | None = field(default=None)

    # ---- lifecycle --------------------------------------------------------

    def attach(self) -> DamiaTimesheetDriver:
        try:
            requests.get(f"{self.cdp_url}/json/version", timeout=2).raise_for_status()
        except Exception as e:
            raise RuntimeError(
                f"Could not reach CDP at {self.cdp_url}/json/version: {e}\n"
                "Launch Chrome with scripts/launch-chrome.ps1 first."
            ) from e

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception:
            self._pw.stop()
            self._pw = None
            raise

        self._page = self._find_damia_page()
        self._page.on("dialog", lambda d: None)
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=10000)
            self._page.wait_for_selector("[id^='tsEntriesWrapper_']", timeout=10000)
        except Exception:
            pass

        self._refresh_timesheet_id()
        return self

    def detach(self) -> None:
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = None
        self._browser = None
        self._page = None
        self.timesheet_id = None

    @contextmanager
    def attached(self) -> Iterator[DamiaTimesheetDriver]:
        self.attach()
        try:
            yield self
        finally:
            self.detach()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Driver not attached. Call .attach() or use .attached().")
        return self._page

    def _find_damia_page(self) -> Page:
        assert self._browser is not None
        for ctx in self._browser.contexts:
            for p in ctx.pages:
                if DAMIA_URL_FRAGMENT in p.url:
                    return p
        raise RuntimeError(
            f"No tab found with URL containing {DAMIA_URL_FRAGMENT!r}. "
            "Open the Damia timesheet tab in the CDP-launched Chrome and retry."
        )

    # ---- timesheet-id discovery -------------------------------------------

    def _refresh_timesheet_id(self) -> int:
        """Find the Damia timesheet id embedded in form-element IDs on the current page.
        Called after attach and after every navigation since each week has its own id."""
        tid = self.page.evaluate(
            """() => {
              const candidates = document.querySelectorAll('[id^="tsEntriesWrapper_"]');
              for (const el of candidates) {
                const m = el.id.match(/^tsEntriesWrapper_(\\d+)$/);
                if (m) return parseInt(m[1], 10);
              }
              return null;
            }"""
        )
        if tid is None:
            raise RuntimeError(
                "Could not discover Damia timesheet id from the page. "
                "Expected an element matching [id^='tsEntriesWrapper_']."
            )
        self.timesheet_id = int(tid)
        return self.timesheet_id

    def _sel(self, fmt: str, **kwargs) -> str:
        if self.timesheet_id is None:
            self._refresh_timesheet_id()
        return fmt.format(tid=self.timesheet_id, **kwargs)

    # ---- read -------------------------------------------------------------

    def current_week_range(self) -> tuple[date, date]:
        caption = self.page.locator(SEL_DATE_CAPTION).inner_text(timeout=5000).strip()
        m = DATE_CAPTION_RE.search(caption)
        if not m:
            raise RuntimeError(f"Could not parse date caption: {caption!r}")
        return _parse_damia_date(m.group(1)), _parse_damia_date(m.group(2))

    def status_word(self) -> str:
        cls = self.page.locator(SEL_STATUS_SPAN).first.evaluate("e => e.className", timeout=5000)
        m = STATUS_CLASS_RE.search(cls)
        return m.group(1) if m else "Unknown"

    def is_editable(self) -> bool:
        """True if the current week is safely editable. Damia doesn't always set <select>.disabled
        on Approved/Submitted sheets (enforcement is server-side), so we go by the status word:
        only 'Draft' (and 'Rejected', which Damia re-opens for editing) is safely mutable."""
        return self.status_word().lower() in ("draft", "rejected")

    def read_week(self) -> Week:
        """Return a Week reflecting the current page state. Works on both editable and
        approved/submitted weeks — the <select>s exist in both modes (disabled when not
        editable). Falls back to per-day totals if the selects are absent.

        Each day's `is_damia_holiday` is True when Damia paints the day-header div with
        a yellow background (rgb(255, 255, 0)) — its built-in bank-holiday signal."""
        start, _end = self.current_week_range()
        tid = self.timesheet_id
        cells = self.page.evaluate(
            """(tid) => {
              if (tid == null) return { error: 'no_timesheet_id' };
              // Find the day-header divs for this timesheet. They live in .tsHeaderDatesPanel
              // inside the timesheet wrapper.
              const wrapper = document.querySelector(`#MainContent_tblSubmit_${tid}`);
              const headerDivs = wrapper
                ? Array.from(wrapper.querySelectorAll('.tsHeaderDatesPanel > div'))
                : [];

              const isYellow = (rgb) => {
                const m = rgb.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                if (!m) return false;
                const r = +m[1], g = +m[2], b = +m[3];
                return r > 230 && g > 230 && b < 80;  // tight match for Damia's yellow
              };

              const out = [];
              for (let i = 0; i < 7; i++) {
                const sel = document.getElementById(`ctlEnd_${tid}_${i}_0`);
                if (!sel) { return { error: `no_select_for_day_${i}` }; }
                const td = sel.closest('td');
                const headerDiv = headerDivs[i] || null;
                const headerBg = headerDiv
                  ? window.getComputedStyle(headerDiv).backgroundColor : '';
                out.push({
                  source: 'select',
                  editable: !sel.disabled,
                  value: sel.value,
                  text: (td?.innerText || '').trim(),
                  classes: td ? Array.from(td.classList) : [],
                  header_bg: headerBg,
                  is_damia_holiday: isYellow(headerBg),
                });
              }
              return out;
            }""",
            tid,
        )

        # If the primary path failed, fall back to per-day totals before raising.
        if isinstance(cells, dict) and "error" in cells:
            fallback = self.page.evaluate(
                """(tid) => {
                  const out = [];
                  for (let i = 0; i < 7; i++) {
                    const td = document.querySelector(`#pnlDailyDaysDetail_${tid}_${i}`);
                    if (!td) return { error: `no_total_for_day_${i}` };
                    out.push({
                      source: 'pnlDailyDaysDetail',
                      editable: false,
                      value: null,
                      text: (td.innerText || '').trim(),
                      classes: Array.from(td.classList),
                    });
                  }
                  return out;
                }""",
                tid,
            )
            if isinstance(fallback, dict) and "error" in fallback:
                raise RuntimeError(
                    f"read_week failed both primary ({cells['error']}) and fallback "
                    f"({fallback['error']}) paths for timesheet_id={tid}."
                )
            cells = fallback

        if not isinstance(cells, list) or len(cells) != 7:
            raise RuntimeError(f"Expected 7 day cells; got {cells!r}")

        days: list[Day] = []
        for i, cell in enumerate(cells):
            units = _parse_units_from_cell(cell)
            is_holiday = bool(cell.get("is_damia_holiday", False))
            if units > 0:
                kind = DayKind.WORKED
            elif is_holiday:
                kind = DayKind.BANK_HOLIDAY
            else:
                kind = DayKind.NOT_WORKED
            days.append(Day(
                date=start + timedelta(days=i),
                kind=kind,
                units=units,
                damia_classes=tuple(cell["classes"]),
                is_damia_holiday=is_holiday,
            ))
        return Week(start=start, days=days)

    # ---- navigate ---------------------------------------------------------

    def _click_postback_div(self, selector: str, timeout: int = 5000) -> None:
        """Click an ASP.NET __doPostBack div. These nav controls are <div onclick=...>
        and Damia hides some of them depending on state (e.g. the current-week button is
        hidden when you're already on the current week). A normal Playwright click waits
        for visibility and times out on those; falling back to the element's own DOM
        click() fires the onclick/__doPostBack regardless of visibility."""
        loc = self.page.locator(selector)
        try:
            loc.click(timeout=timeout)
        except Exception:
            loc.evaluate("el => el.click()")

    def navigate_to_current_week(self) -> None:
        self._click_postback_div(SEL_BTN_CURRENT_WEEK)
        self._wait_for_postback()
        self._refresh_timesheet_id()

    def navigate_to_week(self, week_start: date, max_steps: int = 60) -> None:
        """Walk the week-prev / week-next buttons until we land on the requested week.
        Re-discovers the timesheet id after each step (it changes per week)."""
        for _ in range(max_steps):
            cur_start, _ = self.current_week_range()
            if cur_start == week_start:
                self._refresh_timesheet_id()
                return
            if week_start < cur_start:
                self.page.locator(SEL_BTN_PREV_WEEK).click(timeout=5000)
            else:
                self.page.locator(SEL_BTN_NEXT_WEEK).click(timeout=5000)
            self._dismiss_data_changed_prompt_if_present()
            self._wait_for_postback()
        raise RuntimeError(
            f"navigate_to_week: still not at {week_start} after {max_steps} steps "
            f"(currently {self.current_week_range()[0]})."
        )

    def _dismiss_data_changed_prompt_if_present(self) -> None:
        try:
            self.page.locator(SEL_CONFIRM_AFFIRM).click(timeout=500)
        except Exception:
            pass

    def _dismiss_period_dialog_if_present(self) -> None:
        """Close any visible jQuery UI dialog by clicking its (single) button. Damia raises
        a 'not a valid period for this job' alert when you step before the contract start;
        dismissing it leaves the page on the unchanged week."""
        try:
            btns = self.page.locator(SEL_DIALOG_ANY_BUTTON)
            if btns.count() > 0:
                btns.first.click(timeout=1000)
        except Exception:
            pass

    def step_to_prev_week(self) -> bool:
        """Click the previous-week button once. Returns True if the week actually moved,
        False if Damia refused — it shows a modal ('not a valid period for this job') and
        stays put when you try to step before the contract start. On a refusal we dismiss
        the dialog and leave the page on the same (earliest) week. The unchanged-range
        check is the authoritative stop signal for the back-walk, independent of the
        dialog's exact markup."""
        before, _ = self.current_week_range()
        self.page.locator(SEL_BTN_PREV_WEEK).click(timeout=5000)
        self._wait_for_postback()
        after, _ = self.current_week_range()
        if after == before:
            self._dismiss_period_dialog_if_present()
            return False
        self._refresh_timesheet_id()
        return True

    # ---- mutate -----------------------------------------------------------

    def set_day(self, day_index: int, units: float) -> None:
        if day_index not in range(7):
            raise ValueError(f"day_index must be 0..6 (Sun..Sat); got {day_index}")
        if not self.is_editable():
            raise RuntimeError(
                "Cannot set day: current week is read-only (likely Submitted or Approved). "
                "Navigate to an editable week first."
            )
        damia_value = units_to_damia_value(units)
        self.page.locator(self._sel(FMT_DAY_SELECT, day=day_index)).select_option(
            value=damia_value, timeout=5000,
        )
        self._wait_for_postback()

    def fill_week(self, week: Week) -> None:
        if len(week.days) != 7:
            raise ValueError(f"Week must have 7 days; got {len(week.days)}")
        for i, day in enumerate(week.days):
            self.set_day(i, day.units)

    def autofill_week(self) -> None:
        """Click 'Autofill timesheet' and confirm. Damia fills Mon-Fri = 1.00, Sun/Sat = 0.00.
        Destructive: overwrites any existing entries for this week."""
        self.page.locator(self._sel(FMT_BTN_AUTOFILL)).click(timeout=5000)
        self.page.locator(SEL_CONFIRM_AFFIRM).click(timeout=5000)
        self._wait_for_postback()

    def save_draft(self) -> None:
        self.page.locator(self._sel(FMT_BTN_SAVE_DRAFT)).click(timeout=5000)
        self._wait_for_postback()
        # Saving for the first time may assign a new timesheet id — re-discover.
        self._refresh_timesheet_id()

    # ---- screenshot -------------------------------------------------------

    def screenshot_week(self, bring_to_front: bool = True) -> bytes:
        if bring_to_front:
            try:
                self.page.bring_to_front()
                self.page.wait_for_timeout(300)
            except Exception:
                pass
        try:
            return self.page.screenshot(full_page=True, timeout=10000)
        except Exception:
            return self.page.screenshot(full_page=False, timeout=5000)

    # ---- introspection ----------------------------------------------------

    def has_download_button(self) -> bool:
        return self.page.locator(self._sel(FMT_BTN_DOWNLOAD)).count() > 0

    def find_yellow_elements_debug(self) -> list[dict]:
        """Scan the entire page for elements with a yellow-ish computed background color.
        Returns each one's tag/id/classes/bounding-box/text preview so we can identify what
        carries Damia's bank-holiday highlight."""
        return self.page.evaluate(
            """() => {
              const out = [];
              for (const node of document.querySelectorAll('*')) {
                const cs = window.getComputedStyle(node);
                const bg = cs.backgroundColor;
                const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);
                if (!m) continue;
                const r = +m[1], g = +m[2], b = +m[3];
                const alpha = m[4] !== undefined ? parseFloat(m[4]) : 1;
                if (alpha === 0) continue;
                // Yellow-ish: red & green high, blue lower, red/green similar (warm tone).
                if (r > 180 && g > 150 && b < 200 && Math.abs(r - g) < 80) {
                  const rect = node.getBoundingClientRect();
                  if (rect.width === 0 || rect.height === 0) continue;
                  out.push({
                    tag: node.tagName.toLowerCase(),
                    id: node.id || '',
                    classes: Array.from(node.classList).slice(0, 6),
                    bg: bg,
                    rect: {
                      x: Math.round(rect.x), y: Math.round(rect.y),
                      w: Math.round(rect.width), h: Math.round(rect.height),
                    },
                    text_preview: (node.textContent || '').trim().slice(0, 80),
                    parent_id: node.parentElement?.id || '',
                    parent_classes: node.parentElement
                      ? Array.from(node.parentElement.classList).slice(0, 4) : [],
                  });
                }
              }
              return out;
            }"""
        )

    def read_day_cells_debug(self) -> list[dict]:
        """Return rich per-day cell info for diagnostics: inline style, computed background,
        parent <tr> classes, inner descendant classes. Used to hunt down Damia's
        bank-holiday visual signal (yellow background)."""
        tid = self.timesheet_id
        return self.page.evaluate(
            """(tid) => {
              const out = [];
              for (let i = 0; i < 7; i++) {
                const sel = document.getElementById(`ctlEnd_${tid}_${i}_0`);
                if (!sel) { out.push({ day: i, error: 'no_select' }); continue; }
                const td = sel.closest('td');
                const tr = td?.parentElement;
                const computed = td ? window.getComputedStyle(td) : null;
                const innerEls = td ? Array.from(td.querySelectorAll('div, span')) : [];
                const innerClasses = Array.from(new Set(innerEls.flatMap(el => Array.from(el.classList))));
                out.push({
                  day: i,
                  td_classes: td ? Array.from(td.classList) : [],
                  td_inline_style: td?.getAttribute('style') || '',
                  td_computed_bg: computed ? computed.backgroundColor : '',
                  td_computed_bg_image: computed ? computed.backgroundImage : '',
                  tr_classes: tr ? Array.from(tr.classList) : [],
                  inner_classes: innerClasses,
                });
              }
              return out;
            }""",
            tid,
        )

    def open_attachments_tab(self) -> None:
        """Show the Attachments panel. The tab is a div whose onclick calls
        showAttachmentPanel(tid); fire it via DOM click() (it may be hidden)."""
        self.page.locator(self._sel(FMT_BTN_ATTACH_TAB)).evaluate("el => el.click()")
        self.page.wait_for_timeout(800)

    def attachment_urls(self) -> list[str]:
        """Distinct signed-CDN URLs for files attached to the current week. Damia renders
        each attachment as an inline <img src> pointing at
        download-lb-*.timesheetportal.com/.../private/...?key=<JWT> (signed, expiring).
        We filter on '/private/' + 'key=' so UI icons don't get picked up."""
        tid = self.timesheet_id
        return self.page.evaluate(
            """(tid) => {
              const wrap = document.querySelector(`#tsAttachmentWrapper_${tid}`);
              if (!wrap) return [];
              const seen = new Set(), out = [];
              for (const img of wrap.querySelectorAll('img[src]')) {
                const s = img.getAttribute('src') || '';
                if (s.includes('/private/') && s.includes('key=') && !seen.has(s)) {
                  seen.add(s); out.push(s);
                }
              }
              return out;
            }""",
            tid,
        )

    @staticmethod
    def _sniff_ext(body: bytes) -> str:
        """Pick a file extension from the actual bytes — Damia serves JPEGs from URLs ending
        '..png' with an unreliable content-type, so we trust the magic number, not the header."""
        if body[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if body[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if body[:4] == b"%PDF":
            return ".pdf"
        if body[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"
        return ".bin"

    def pull_attachments(self, save_dir) -> list:
        """Download every file attached to the CURRENT week into save_dir, fetching through
        the browser context (so the signed URL's session/cookies apply). Returns the saved
        Paths. Safe on weeks with no attachments (returns []). Switches to the Attachments
        tab as a side effect — fine for hydration since we read the week before calling this.

        Clears any existing attachment_* files in save_dir first so re-hydration is a clean
        rebuild, not an accumulation."""
        from pathlib import Path
        save_dir = Path(save_dir)
        self.open_attachments_tab()
        urls = self.attachment_urls()
        if not urls:
            return []
        save_dir.mkdir(parents=True, exist_ok=True)
        for stale in save_dir.glob("attachment_*"):
            try:
                stale.unlink()
            except Exception:
                pass
        saved: list = []
        for i, url in enumerate(urls):
            try:
                resp = self.page.request.get(url, timeout=30000)
                if not resp.ok:
                    continue
                body = resp.body()
                p = save_dir / f"attachment_{i + 1}{self._sniff_ext(body)}"
                p.write_bytes(body)
                saved.append(p)
            except Exception:
                continue
        return saved

    def download_week_pdf(self, save_to) -> object:
        """Click the Download button and capture the PDF that Damia produces. Refuses on
        Drafts that don't have a download button. Returns the saved Path."""
        from pathlib import Path
        save_to = Path(save_to)
        if not self.has_download_button():
            raise RuntimeError(
                "No download button on this week — likely a Draft that hasn't been saved/submitted."
            )
        # bring_to_front so any browser native download UI doesn't surprise the user.
        try:
            self.page.bring_to_front()
        except Exception:
            pass
        with self.page.expect_download(timeout=30000) as dl_info:
            self.page.locator(self._sel(FMT_BTN_DOWNLOAD)).click(timeout=5000)
        download = dl_info.value
        save_to.parent.mkdir(parents=True, exist_ok=True)
        download.save_as(str(save_to))
        return save_to

    # ---- internals --------------------------------------------------------

    def _wait_for_postback(self) -> None:
        self.page.wait_for_timeout(1500)


__all__ = [
    "DamiaTimesheetDriver",
    "units_to_damia_value",
    "DEFAULT_CDP_URL",
]
