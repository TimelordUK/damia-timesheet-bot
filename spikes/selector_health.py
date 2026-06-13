"""Damia selector health-check — run this FIRST whenever the portal looks reskinned.

Read-only. Attaches to the running CDP Chrome and reports, for the current week and one
established (older) week, whether each selector the driver depends on still resolves. Damia
has reskinned before (see memory project-damia-reskin-2026-06); the breakages were always in
one of three buckets, so this groups them that way:

  FRAGILE   — status representation, footer-button id paths, id-discovery semantics
  STABLE-ish — nav buttons, date caption, day selects, attachments, header dates

When something reports n=0 here, that's the repair target. NO mutation, NO submit.
"""
from damia_timesheet_bot.adapters.timesheet import damia_playwright as D
from damia_timesheet_bot.adapters.timesheet.damia_playwright import DamiaTimesheetDriver


def _probe(page, tid: int, editable: bool) -> None:
    # expect: True = always; "edit" = only on editable weeks; "settled" = only on read-only.
    checks = {
        # --- STABLE-ish ---
        "date_caption":   (D.SEL_DATE_CAPTION, True),
        "btn_prev":       (D.SEL_BTN_PREV_WEEK, True),
        "btn_next":       (D.SEL_BTN_NEXT_WEEK, True),
        "btn_current":    (D.SEL_BTN_CURRENT_WEEK, True),
        "day_select_0":   (D.FMT_DAY_SELECT.format(tid=tid, day=0), True),
        "daily_total_1":  (D.FMT_DAILY_TOTAL.format(tid=tid, day=1), True),
        "week_total":     (D.FMT_WEEK_TOTAL.format(tid=tid), True),
        "attach_tab":     (D.FMT_BTN_ATTACH_TAB.format(tid=tid), True),
        "attach_wrapper": (D.FMT_ATTACH_WRAPPER.format(tid=tid), True),
        # --- FRAGILE (reskin-sensitive) ---
        "status_tag":     (D.SEL_STATUS_TAG, True),
        "btn_autofill":   (D.FMT_BTN_AUTOFILL.format(tid=tid), "edit"),
        "btn_save_draft": (D.FMT_BTN_SAVE_DRAFT.format(tid=tid), "edit"),
        "btn_submit":     (D.FMT_BTN_SUBMIT.format(tid=tid), "edit"),
        "btn_download":   (D.FMT_BTN_DOWNLOAD.format(tid=tid), "settled"),
    }
    res = page.evaluate(
        """(sels) => {
          const out = {};
          for (const [k, s] of Object.entries(sels)) {
            const els = document.querySelectorAll(s);
            out[k] = {n: els.length,
                      text: els[0] ? (els[0].textContent || '').trim().slice(0, 24) : null};
          }
          return out;
        }""",
        {k: s for k, (s, _) in checks.items()},
    )
    for k, (_sel, expect) in checks.items():
        v = res[k]
        expected = expect is True or (expect == "edit" and editable) or \
            (expect == "settled" and not editable)
        if v["n"]:
            flag = "OK"
        elif not expected:
            flag = "- "  # legitimately absent in this state
        else:
            flag = "??"  # MISSING where expected — likely a reskin breakage
        txt = f"  text={v['text']!r}" if v["text"] else ""
        print(f"  {flag} {k:16} n={v['n']}{txt}")


def main() -> None:
    with DamiaTimesheetDriver().attached() as drv:
        page = drv.page
        drv.navigate_to_current_week()
        cap = drv.current_week_range()
        print(f"=== CURRENT week {cap[0]}..{cap[1]}  tid={drv.timesheet_id}  "
              f"status={drv.status_word()!r} ===")
        _probe(page, drv.timesheet_id, drv.is_editable())

        for _ in range(2):
            if not drv.step_to_prev_week():
                break
        cap = drv.current_week_range()
        print(f"\n=== ESTABLISHED week {cap[0]}..{cap[1]}  tid={drv.timesheet_id}  "
              f"status={drv.status_word()!r} ===")
        _probe(page, drv.timesheet_id, drv.is_editable())
        print("\n('??' = MISSING where expected = the repair target. '-' = legit absent.)")


if __name__ == "__main__":
    main()
