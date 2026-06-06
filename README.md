# damia-timesheet-bot

Assisted weekly timesheet workflow for contractors using the Damia timesheet portal.

**Hard invariant:** the tool never sends an email and never clicks the final submit button. It drafts, screenshots, watches for approvals, and stages everything for a human-confirmed last step.

## Architecture

Hexagonal layout. `core/` owns domain models and port (interface) definitions; nothing in `core/` imports an adapter. Adapters live under `adapters/` and depend on `core/`.

```
damia_timesheet_bot/
  core/
    models.py        # Week, Day, Submission, ApprovalRecord, ...
    ports.py         # TimesheetDriver, EmailDriver, HolidayProvider, StateStore (Protocols)
  adapters/
    timesheet/       # Damia (Playwright over CDP) lives here
    email/           # Outlook COM lives here
    holidays/        # uk_govuk lives here
    state/           # CSV-on-disk lives here
  tui/               # Textual front end
spikes/              # one-off recon scripts, not shipped
```

Swapping out a subsystem (different timesheet portal, Gmail instead of Outlook, different country's holidays) means writing one new adapter; nothing else changes.

## Stage 0 — recon

Before any real driver work, we probe the Damia portal to learn its framework, DOM shape, and network surface. The probe is read-only.

### 1. Install dependencies

```powershell
# from the repo root
uv venv
uv sync
uv run playwright install chromium   # only needed if we ever launch our own browser; harmless to install
```

### 2. Launch Chrome with the remote debugging port

> ⚠️ **Chrome 136+ silently ignores `--remote-debugging-port` when run against the default user-data-dir.** This is a security mitigation Google added in 2025 — you MUST use a dedicated profile directory. The trade-off: you'll log in to Damia in that profile the first time, then the session cookie persists across relaunches.

Use the launch script:

```powershell
# Normal launch
.\scripts\launch-chrome.ps1

# If a stale Chrome is hanging around (or the port doesn't come up):
.\scripts\launch-chrome.ps1 -KillExisting

# Launch AND run the probe in one go (45s watch window):
.\scripts\launch-chrome.ps1 -KillExisting -Probe -WatchSeconds 90
```

The script kills any existing chrome.exe if `-KillExisting` is passed, launches with the dedicated profile at `…\damia-timesheet-bot\chrome-profile`, and polls `/json/version` until CDP is ready (or fails fast with an actionable error).

**First time only:** log in to Damia in the launched window. The login cookie persists in the dedicated profile so subsequent runs skip this step.

### 3. Run the probe

```powershell
# defaults to a 45s watch window; -w/--watch-seconds to override
uv run python -m spikes.damia_probe --watch-seconds 90
```

The probe writes `spikes/output/damia_probe_report.md` (DOM tree, framework hints, captured network traffic, a screenshot). During the watch window, click previous-week / next-week / any non-destructive navigation in the Damia tab. The probe prints a per-second countdown so you can pace yourself. We then read the report together to lock the `TimesheetDriver` port shape.

## Stage 1+

To be planned after recon. See `memory/project_damia_bot.md` for the stage roadmap and `memory/feedback_subsystem_independence.md` for the swappability rule that governs design.
