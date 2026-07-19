# Job Application Automation — Project Brief
> Paste this at the start of EVERY Claude Code session to restore full context.

---

## What This Is
A personal, local Python tool that automates my LinkedIn job application process.
It is NOT a startup product. It runs on my machine only. Built for stealth — must not get my LinkedIn account banned.

## The 3 Core Modules
1. **Scraper** — Hits LinkedIn's internal voyager API using my session cookie to pull job listings
2. **Scorer + Tailor** — Uses Claude AI (Haiku for scoring, Sonnet/Haiku for tailoring) to filter and rewrite my resume per job
3. **Applicant** — Uses Playwright to auto-submit LinkedIn Easy Apply jobs; flags external applications for manual review

## Golden Rules (never break these)
- Max 50-80 job scrapes per session, max 10-15 Easy Apply submissions per day
- All requests have randomized delays of 3-8 seconds minimum
- Playwright runs HEADED (not headless) — real browser fingerprint
- Never use proxies or VPNs
- Always human-in-the-loop: I approve jobs in the dashboard before anything is submitted
- Log every action to SQLite

## Tech Stack
```
Python 3.11+
httpx          — LinkedIn API calls (async)
playwright     — Easy Apply browser automation
anthropic      — Claude API (scoring + tailoring)
wkhtmltopdf    — HTML/CSS resume → PDF
fastapi        — Local dashboard
sqlite3        — Job tracking database
jinja2         — Resume PDF template
schedule       — Timed scraping runs
python-dotenv  — Config / secrets management
```

## Project Folder Structure
```
job-auto/
├── config.py           # LinkedIn cookie, preferences, thresholds
├── scraper.py          # LinkedIn job discovery
├── scorer.py           # Haiku fit scoring
├── tailor.py           # Resume tailoring via Claude
├── renderer.py         # JSON resume → PDF
├── applicant.py        # Easy Apply automation
├── dashboard/
│   ├── app.py          # FastAPI app
│   └── templates/      # HTML templates
├── db.py               # SQLite helpers
├── models.py           # Dataclasses / Pydantic models
├── resume/
│   ├── master.json     # My structured resume (source of truth)
│   └── template.html   # PDF resume template
├── output/
│   └── resumes/        # Generated tailored PDFs (gitignored)
├── logs/               # All run logs (gitignored)
├── .env                # Secrets: LinkedIn cookie, Anthropic API key
├── requirements.txt
└── README.md
```

## Database Schema (SQLite — db.py)
```sql
-- Jobs discovered by the scraper
jobs (
  id INTEGER PRIMARY KEY,
  linkedin_job_id TEXT UNIQUE,
  title TEXT,
  company TEXT,
  location TEXT,
  description TEXT,
  url TEXT,
  is_easy_apply BOOLEAN,
  fit_score INTEGER,          -- 1-10, from Haiku
  fit_reason TEXT,
  status TEXT DEFAULT 'new',  -- new / approved / applied / skipped / failed
  created_at TIMESTAMP,
  applied_at TIMESTAMP
)

-- Application records
applications (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  resume_path TEXT,
  method TEXT,                -- easy_apply / manual
  status TEXT,                -- submitted / failed / pending_manual
  submitted_at TIMESTAMP
)
```

## Data Flow (end to end)
```
Scraper → pulls 50 jobs from LinkedIn voyager API
       ↓
Filter → remove wrong city / wrong seniority (config-based rules)
       ↓
Scorer → Haiku scores each job 1-10 for fit
       ↓
Dashboard → I review and approve jobs scoring 6+
       ↓
Tailor → Claude rewrites my resume JSON for this specific JD
       ↓
Renderer → Tailored JSON → PDF via HTML template + WeasyPrint
       ↓
Applicant → Easy Apply: Playwright submits | External: flags as manual
       ↓
Logger → Everything written to SQLite + log files
```

## Key Design Decisions (already made, don't revisit)
- Resume stored as structured JSON, not a Word doc or PDF — enables programmatic tailoring
- Scraping and applying are separate sessions run at different times of day
- Human approval required before any application is submitted
- Haiku for high-volume AI calls (scoring), Sonnet for quality calls (tailoring)
- WeasyPrint for PDF — resume designed in HTML/CSS for easy customization

## Current Build Status
> Update this section at the end of each session

- [x] Session 1 — Project scaffold, config, database, master resume JSON
- [x] Session 2 — Scraper (LinkedIn voyager API)
- [x] Session 3 — Scorer (Haiku integration)
- [x] Session 4 — Resume tailor + PDF renderer
- [x] Session 5 — Dashboard (FastAPI)
- [x] Session 6 — Easy Apply automation (Playwright)
- [x] Session 7A — Data layer hardening, backend endpoints, bug fixes
  - Apply button subprocess fix: script path made absolute; `DB_PATH` now absolute in `config.py`
  - `_is_easy_apply()` fallback was already `return False` (confirmed correct)
  - `JOB_EXPIRY_HOURS = 48` added to `config.py`
  - `validated_at` column added to jobs table via `init_db()` migration
  - New db helpers: `get_all_jobs_sorted`, `get_likely_expired_jobs`, `purge_dead_jobs`, `update_job_field`, `unskip_job`, `unskip_all_jobs`; `get_stats()` now includes `likely_expired`
  - `MasterResume` Pydantic v2 model with `@model_validator` structural validation; `Job.is_likely_expired` property added
  - New endpoints: `GET /api/resume`, `PUT /api/resume` (backup+validate), `POST /api/resume/preview`, `POST /api/jobs/unskip-all`, `POST /api/jobs/{id}/unskip`, `POST /api/jobs/purge`, `GET /resume` stub
  - `GET /api/jobs` now accepts `sort`, `search`, `easy_apply`, `status` query params
- [x] Session 7B — validator.py (Playwright Easy Apply classification)
  - `JobValidator` class: persistent browser profile shared with `applicant.py`, classifies job pages into closed/applied/easy_apply/manual/unknown
  - CAPTCHA and logged-out detection abort the batch safely
  - `db.update_job_field()` writes `is_easy_apply`, `status`, `validated_at` per job
  - CLI: `python validator.py` (batch) and `--single <job_id>`; wired into `run.py validate` and dashboard `/api/validate`, `/api/jobs/{id}/validate`
- [x] Session 8 — Full dashboard UI redesign
  - Full rewrite of `dashboard/templates/index.html` only — zero backend changes, all 7A/7B endpoints reused as-is
  - Three-panel fixed layout: top bar (64px), left sidebar (220px), scrollable job grid
  - Top bar: logo, clickable stat pills (Total/New/Approved/Applied/⚠ Likely Expired), debounced search with clear button, Run Pipeline (120s poll timeout), Apply All (N computed client-side from in-memory jobs cache)
  - Sidebar: status filter rows with active-state styling, sort dropdown, Easy Apply/Hide Expired toggles (hide-expired is client-side only), maintenance actions (Validate Jobs, Purge Expired, Restore Skipped — purge/restore hidden when count is 0), nav links to Jobs/Resume
  - Job cards: color-coded score badge, expiry warning icon, Easy Apply/Manual type badge, 2-line-clamped fit reason, status-specific action buttons
  - Approve → tailoring poll (3s interval, 90s timeout) with correct `resume_path` check (`!== 'failed' && !== ''`) so "Download Resume" never shows prematurely; polling auto-resumes on page refresh for any approved-but-not-ready job
  - Per-card Validate button polls up to 30s for `validated_at`
  - Right slide-out detail panel (not a modal) with full fit reason/description; updates in place when a different card is clicked while open
  - Verified in a headless Playwright smoke test: zero console errors; filters, search, sort, toggles, and detail panel all confirmed visually. Approve/Validate/Run Pipeline/Apply All were **not** live-clicked during testing (real Claude API calls / real LinkedIn Playwright session) — only code-reviewed against the endpoint contracts.
  - Note: per-card single-job "Apply" button and "Retry"/"Unapprove" actions from the old UI were intentionally dropped in this redesign (consolidated into "Apply All") per the Session 8 spec's action-button table — flag if this was unintentional.
- [x] Session 9 — Resume editor page at `/resume`
  - New `dashboard/templates/resume.html`; `dashboard/app.py`'s `/resume` route now renders it via `TemplateResponse` (only change to `app.py`) — zero other backend changes, reuses `GET/PUT /api/resume` and `POST /api/resume/preview` from 7A
  - Matches the Session 8 dark design system exactly (same `:root` tokens, fonts, toast implementation copied verbatim)
  - Editable sections: personal info, summary, experience (add/remove entries, collapse/expand, add/remove bullets, live-updating entry header), skills (4 tag-chip categories, Enter/comma to add, × to remove), education (add/remove), projects (add/remove, tech tags)
  - **Added an Achievements section beyond the session spec**: the spec's `collectFormData()`/`populateForm()` never handled `resume.achievements`, and `master.json` had 3 real achievement entries — saving as literally specced would have silently wiped them (Pydantic defaults `achievements` to `[]`). Implemented as a simple add/remove list, same pattern as bullets, no minimum-entries constraint (field is optional in `MasterResume`)
  - `saveResume()`: client-side validation mirrors `MasterResume`'s `@model_validator` (name, summary, ≥1 experience, ≥1 skill) with zero network calls when it fails; 422 responses parsed and displayed with the FastAPI `body` prefix stripped from `loc`
  - `openPreview()`: correct `fetch()` + `blob()` + `URL.createObjectURL()` implementation (not `window.open()` GET, which would 405 against the POST-only preview endpoint)
  - Unsaved-changes tracking extended beyond the spec's `input`-event-only listener: entry add/remove, bullet add/remove, achievement add/remove, and skill-tag add/remove now also set the dirty flag (none of those fire native `input` events), so the `beforeunload`/back-link guard can't be bypassed by editing via buttons alone
  - Verified live end-to-end (localhost-only, no external side effects): pre-population confirmed field-for-field against `master.json` via Playwright; client-side validation blocks empty-name save with zero network calls; skill-tag add/remove; unsaved-changes confirm dialog fires correctly; real Save round-tripped through `PUT /api/resume` with **byte-for-byte semantic JSON equality** before/after (achievements fix confirmed); Save & Preview confirmed via server log (`PUT` → `PUT` → PDF rendered → `POST /api/resume/preview` 200) — zero console errors throughout
  - Known test-environment quirk (not an app bug): a stale dashboard server process from Session 8 testing is stuck listening on port 8000 and can't be killed via Task Manager, PowerShell, or Bash on this machine (PID visible to `netstat` but invisible to every process-management tool tried) — Session 9 was verified on port 8001 instead. **A machine restart will likely be needed before `python run.py dashboard` (which hardcodes port 8000) works again** — flagging this so it isn't mistaken for a regression.
- [ ] Opus Review — Auth/session safety review before going live; full dashboard feature set (Sessions 5–9) is otherwise complete