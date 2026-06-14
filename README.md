# damia-timesheet-bot

Assisted weekly timesheet workflow for contractors using the Damia timesheet portal.

**Hard invariants (never violated):** the tool **never sends an email** and **never clicks Submit**. It fills drafts, screenshots, drafts the approval email, watches for the reply, renders the approval proof, and attaches it — every outward step that commits anything is left to you.

## Architecture

Hexagonal. `core/` owns domain models + port (interface) definitions and is adapter-agnostic; adapters under `adapters/` depend on `core/`. Swapping a subsystem (different portal, Gmail instead of Outlook, another country's holidays) means writing one new adapter.

```
damia_timesheet_bot/
  core/      models, ports, config, paths, weekplan, decide (circuit-breaker),
             classify (reply heuristic), tracking, hydrate
  adapters/
    timesheet/   Damia driver (Playwright over CDP)
    email/       Outlook COM (draft + watch + proof render)
    holidays/    gov.uk bank holidays
    leave/       config.yml leave ledger
    state/       CSV portal cache + JSON submission overlay
    render.py    HTML -> PNG (headless Chromium) for the proof
  tui/         Textual front end (reads view.json)
spikes/        recon + a tracked selector health-check (run if the portal looks reskinned)
```

The **portal is the source of truth**; `cache/` is disposable and rebuilt by `hydrate`. `config.yml` is the one precious file you edit. `submissions.json` (email-side state, the tracking-ids we issued) lives at the data root, outside `cache/`. `proofs/` holds the request screenshots and approval-proof PNGs (precious — the agency pays against them).

---

## One-time setup (per machine, e.g. your work PC)

### 1. Install

```powershell
# from the repo root
uv venv
uv sync
uv run playwright install chromium   # needed for rendering the approval-proof PNG
```

### 2. Launch Chrome with the remote-debugging port

> ⚠️ **Chrome 136+ silently ignores `--remote-debugging-port` with the default user-data-dir.** Use the dedicated profile the script creates; you log in to Damia once and the cookie persists.

```powershell
.\scripts\launch-chrome.ps1            # normal launch
.\scripts\launch-chrome.ps1 -KillExisting   # if a stale Chrome is holding the port
```

**First time:** log in to Damia in the launched window and open your timesheet tab.

### 3. Classic Outlook (for the email side)

The email subsystem talks to **classic Outlook via COM** (new Outlook has no COM). Open classic Outlook signed in to the mailbox you send from — on the work PC that's your work Exchange account.

### 4. Create + edit your config

```powershell
uv run damia-bot init        # creates %LOCALAPPDATA%\damia-timesheet-bot\ + a config.yml template
```

(Any command scaffolds the config on first run, but `init` does just that and prints the path.) Then edit it:

```yaml
name: Stephen James
day_rate: 500
currency: GBP
week_start: sunday

# Approval emails go here — set your REAL manager(s) on the work PC.
approver_emails:
  - first.manager@yourcompany.com
  - second.manager@yourcompany.com

# Personal days off (bank holidays are detected automatically from gov.uk — list only leave).
# Single day:   - {date: 2026-08-25, type: annual}
# Range (incl): - {start: 2026-12-24, end: 2026-12-31, type: annual}
leave: []

# How a reply counts as approval. Conservative: a reply is APPROVED only if (after stripping
# quoted history) it is <= max_words AND contains a keyword. Anything longer -> manual.
approval:
  keywords: [approved, approve, approval, ok, okay, yes, agreed, confirmed, fine]
  max_words: 2
```

---

## The weekly loop

Run from the repo root with Chrome (CDP) + classic Outlook open. `--week` accepts any date in the target week and **defaults to the previous (just-completed) week** — the one you've just worked and are submitting — so a bare command does the right thing on Monday. Pass `--week <date>` for any other week. (Damia's "current period" is the week *about to start*; you submit the one that just ended.)

| # | Command | What it does |
|---|---------|--------------|
| 1 | `uv run damia-bot hydrate` | Walk the portal back to the contract start; rebuild `cache/` (CSV + PDFs + approval JPEGs) + `view.json`. **Run this first on a new machine.** |
| 2 | `uv run damia-bot plan --week <date>` | Read-only preview: the planned week (leave + bank holidays) and the circuit-breaker decision. No portal/Outlook writes. |
| 3 | `uv run damia-bot draft --week <date>` | Fill the week (Save draft), screenshot the **whole portal page**, and create the **Outlook draft** with the screenshot embedded + a `[TS:…]` subject. Records the submission. **Never sends.** |
| — | *You* review the draft in Outlook and **send it**. | |
| 4 | `uv run damia-bot watch` | For each in-flight week, find the reply by `[TS:…]`, classify it. Clean "Approved" → render the **proof PNG** + mark APPROVED. A query → `needs_attention`. |
| 5 | `uv run damia-bot attach-proof --week <date>` | Upload the approval proof to the Damia **Attachments** panel and click **Save draft**. **Never Submits.** |
| — | *You* do the final **Submit** to the agency. | |

Helpers: `damia-bot view` (print `view.json`), `damia-bot tui` (Textual dashboard), `damia-bot fill-draft` (fill + Save draft only, no email).

Most write commands take `--dry-run` (decide/preview, change nothing) — use it first when in doubt.

### Your work-PC run, step by step

1. **Init + set the approver:** `uv run damia-bot init`, then edit `config.yml` → `name` + `approver_emails` (your actual manager).
2. **Hydrate:** `uv run damia-bot hydrate` — builds the cache from the portal.
3. **Draft:** `uv run damia-bot draft --week <date>` → open Outlook Drafts, check it, **send**.
4. **After your manager replies:** `uv run damia-bot watch` → generates the proof and flips the week to APPROVED. (Check the proof under `…\damia-timesheet-bot\proofs\`.)
5. **Attach:** `uv run damia-bot attach-proof --week <date>` → proof attached + draft saved.
6. **Submit** the timesheet to the agency yourself.

### Re-doing or removing a draft

- Regenerate a week already in flight: `damia-bot draft --week <date> --force` (deletes the stale Outlook draft and re-creates it).
- To fully remove a test draft, delete it in Outlook Drafts. To reset a week's bot state, delete its entry from `submissions.json`.

---

## Corporate network (SSL interception)

The **only** outbound internet call the tool makes is fetching UK bank holidays from `gov.uk` (everything else goes through your already-authenticated browser or local Outlook COM). Behind a corporate proxy that intercepts TLS with a private root CA, that call fails — so the tool **falls back to a bundled bank-holidays snapshot** (it prints a warning) and drafting still works. For live gov.uk data, point requests at your corporate root CA before running:

```powershell
$env:REQUESTS_CA_BUNDLE = "C:\path\to\corporate-root-ca.pem"
```

## If the portal looks reskinned

Damia changes its markup periodically (it did on 2026-06-13). If a command fails on a selector, run the **health-check** first — it reports which of the driver's selectors still resolve, so the repair target is obvious:

```powershell
uv run python -m spikes.selector_health
```

All selectors are centralised as `SEL_*` / `FMT_*` constants at the top of `adapters/timesheet/damia_playwright.py` — that's the repair surface.

---

## Roadmap

- **Orchestrator / `--bot` mode:** a single command you spin up (e.g. Monday morning) that runs the whole loop unattended — hydrate, draft (pause for your send), poll `watch` until approved, attach the proof — stopping at the two human gates (sending the email, the final Submit) and flagging anything off the standard path for manual handling.
- **Delta hydration:** re-sweep status every run but reuse cached artifacts for settled weeks (`--full` to force a full rebuild).
- **Outlook-calendar leave** adapter (swap-in for the config ledger).
