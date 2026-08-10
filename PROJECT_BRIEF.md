# Job Application Automation — Project Brief
> Paste this at the start of EVERY Claude Code session to restore full context.

---

## What This Is
A personal, local Python tool that automates my LinkedIn job application process.
It is NOT a startup product. It runs on my machine only. Built for stealth — must not get my LinkedIn account banned.

## The 3 Core Modules
1. **Scraper** — Hits LinkedIn's internal voyager API using my session cookie to pull job listings. Driven by dynamic saved searches (multiple titles × city × filters, managed on the `/searches` dashboard page) as of Session 12, with the original single `config.py`-defined search still working as the fallback when no saved searches are enabled
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
- [x] Session 10 — Adaptive Q&A resolution + pending questions workflow
  - `qa_bank` (dedup key `question_norm`, UNIQUE) and `pending_answers` tables added via `init_db()`; `jobs.status` comment documents the new `pending_questions` value. `Job.status` in `models.py` is a plain `str`, not `Literal`, so no enum update was needed there.
  - `db.py`: `normalize_question()` (canonical, lowercase/strip-punctuation/collapse-whitespace) plus `get_qa_exact`, `get_qa_bank_all`, `upsert_qa_answer`, `increment_qa_use_count`, `update_qa_answer`, `delete_qa_answer`, `save_pending_questions`, `get_pending_questions_for_job`, `get_jobs_pending_questions`, `resolve_pending_questions` (atomic — single `conn.commit()` across `pending_answers` + `qa_bank` + `jobs.status='approved'`), `get_pending_question_count`. `get_stats()` now includes `pending_questions`.
  - `config.py`: `QA_AI_CONFIDENCE_THRESHOLD = 0.75`, `QA_FUZZY_MATCH_THRESHOLD = 0.85` (both tunable after a real batch run).
  - `qa_resolver.py` (new): `QAResolver` four-tier cascade — bank exact → bank fuzzy (`difflib`, filtered by `field_type`) → config-keyword rules → Haiku (`claude-haiku-4-5`) with confidence gating. `_legacy_config_match()` ports the CTC/notice/experience/phone/work-auth matching that used to live inline in `fill_form_fields()`; keyword lists were widened beyond the session spec's example set to cover every phrasing the *actual* old code matched (`desired salary`, `joining`, `total years`, `eligible to work`, `legally authorized`, `citizen`) — a literal relocation, not the abridged template.
  - `applicant.py`: `QAResolver` instantiated once per `LinkedInApplicant` run. `fill_form_fields()` rewritten — location/months-of-experience/gender fields are untouched; the CTC/notice/experience/phone/work-auth blocks were deleted and replaced by three generic required-field loops (text/number inputs, select dropdowns, legend-bearing radio fieldsets) that all route through `qa_resolver.resolve_field()`. New fill primitives: `_get_field_label`, `_fill_text_input`, `_get_select_options`, `_fill_select`, `_get_radio_option_label`, `_click_matching_radio`, `_dismiss_modal`. Unlabeled radio fieldsets keep the old best-effort "click first radio" fallback (can't resolve a question with no text). `handle_form_steps()` now returns `"submitted"/"failed"/"pending"`; `apply_to_job()` returns `"applied"/"pending_questions"/"failed"/"closed"`. `apply_batch()`'s consecutive-failure breaker now only increments on `"failed"`.
  - Two side-fixes surfaced by the bool→str migration: (1) neither `applicant.py`'s own CLI entry point nor `run.py apply` ever called `db.init_db()` before constructing `LinkedInApplicant()` — harmless before this session since `jobs`/`applications` always pre-existed via the dashboard, but `QAResolver.__init__` now queries `qa_bank` at construction time, so both entry points call `db.init_db()` defensively. (2) `apply_to_job()` now correctly returns `"applied"` when `navigate_to_job()` finds the job was already applied — previously this case silently counted as a failure.
  - `dashboard/app.py`: 5 new endpoints — `GET/POST /api/jobs/{id}/questions|answers`, `GET /api/qa-bank`, `PUT/DELETE /api/qa-bank/{id}`.
  - `dashboard/templates/index.html`: "⏳ Needs Answers" sidebar filter (folded into the existing generic status-count map rather than a special-cased line), matching empty-state message, `pending_questions` card action button (new `.btn-info` class, `var(--info)` blue), slide-out **Answer Questions** panel with AI-suggestion confidence labels and a "remember this answer" checkbox, and a **Manage Saved Answers** modal (double-click-to-edit a saved answer, source badges, delete). Used `var(--accent)` for the "config" source badge — the design system has no separate indigo token.
  - `tests/test_10.py`: 18 unit tests (normalize, all 4 tiers, AI fence-stripping/parse-error handling, circuit-breaker logic), all mocked, **all passing**.
  - Automated verification completed: `python -m unittest tests.test_10 -v` (18/18 pass), schema migration check, db helper smoke test (inserted-then-cleaned-up test row — the smoke test's hardcoded `'1200000'` doesn't match the real configured `CURRENT_CTC`, so it was deleted from the live `qa_bank` table afterward rather than left in place), `QAResolver` import/normalize-delegation check, `py_compile` on every changed file, and a dashboard-app import + route-registration check.
  - **Not done — needs the user, live LinkedIn, and real Anthropic spend**, same as Session 8's Approve/Validate/Apply All: Testing Steps 5–8 (regression-check a real known-good job application, trigger the pending flow against a real Easy Apply form, the dashboard round-trip in a live browser, and the tier-1 learning verification on a second run). These were code-reviewed against the spec's contracts but not live-exercised.
- [x] Session 10.1 — Hotfix: required radio/checkbox fieldset detection + Review-retry counter investigation
  - Root cause was one level deeper than the hotfix prompt assumed: LinkedIn migrated Easy Apply to a native `<dialog data-testid="dialog" aria-labelledby="dialog-header">` element (discovered via a live Playwright trace while fixing `click_easy_apply()`'s modal-detection earlier in the same working session — a fix that landed in `applicant.py` but was never recorded in this brief until now). Session 10's fieldset scan was still hardcoded to `.jobs-easy-apply-modal fieldset, [role="dialog"] fieldset`, which matches neither the native `<dialog>` nor its `role`-less attributes — so `fill_form_fields()` found **zero** fieldsets on every real run. A required radio group was silently invisible: never logged as filled, never logged as unresolved. This exactly matches the Navi failure log this hotfix was written to fix.
  - `applicant.py`: new `MODAL_SELECTORS`/`MODAL_CONTENT_SELECTOR` constants scoped to the real `dialog[data-testid="dialog"]` / `dialog[aria-labelledby="dialog-header"]` container (old selectors kept as fallback); `click_easy_apply()` now checks `_modal_is_open()` before re-clicking instead of blindly retrying into a click blocked by the open dialog. New `FIELDSET_SCOPE_SELECTOR` mirrors the same fix for fieldset scanning. New `_fieldset_is_required()` ORs five signals: asterisk in legend text, `aria-required="true"`, `data-test-form-element-required="true"`, any child `input[required]`, or a hidden `span:text-is("Required")`. The old "every legend-bearing fieldset is required" blanket assumption is gone — non-required groups are now left untouched, same as any other optional field.
  - Generalized beyond radios per the hotfix spec's explicit instruction: the fieldset loop now also matches checkbox-only fieldsets (`field_type="checkbox"` routed through the same `qa_resolver.resolve_field()` cascade — `qa_resolver.py` itself untouched, per the spec's constraint). `_click_matching_radio()` reused as-is for checkboxes since its logic was already input-type-agnostic.
  - **Retry counter (Task 3 of the hotfix spec): investigated, not changed.** Read `handle_form_steps()` end-to-end — `review_retries` is already correctly scoped outside the `while` loop, and there is only one "Clicking Review button (attempt N)" log call, not two. Neither of the spec's two hypothesized causes (mis-scoped counter, duplicate hardcoded log calls) exists in the current code. The actual symptom — a duplicated "(attempt 1)" on the very first Review click, confirmed in `logs/applicant_2026-07-26.log` at both 23:50 and 23:56 — traces to a false-positive "form advanced" read from the whole-modal `page.inner_text()` diff, not the counter/log wiring itself. Left as-is per the spec's own constraint ("must not change... when the batch gives up"), since a real fix means changing the advance-detection heuristic, not the counter. Flagged as a separate, still-open follow-up for a future session.
  - **Not live-tested against a real LinkedIn Easy Apply form.** No unit-test scaffolding exists for the extractor, so Testing Step 1 was skipped per the spec's own instruction. Static verification only: `py_compile`/import clean. Steps 2–5 (regression job, Navi-style required-radio reproduction, retry-counter log inspection, dashboard round-trip for a pending-questions job) still need a live run against the real site — the Navi job (ID 390) is the natural candidate to retry first.
- [x] Session 11 — Age-based purge + canonical maintenance order
  - `config.py`: 3 new constants — `MAX_JOB_AGE_DAYS=10`, `JOB_AGE_WARNING_DAYS=8`, `AGE_PURGE_PROTECTED_STATUSES=("applied", "pending_questions")`.
  - `db.py`: `get_purgeable_old_jobs()` (age + protected-status filter via bound `datetime('now', ?)` params, never string-formatted) and `purge_old_jobs()` (select→delete→single-commit, mirroring `purge_dead_jobs()` exactly, then post-commit PDF cleanup: skips `None`/empty/the `'failed'` sentinel, skips any path resolving outside `OUTPUT_RESUME_DIR` via `Path.resolve()`+`is_relative_to()` — logged at WARNING, never raises — missing files logged at DEBUG). `get_stats()` gained `"aging"` and `"purgeable_old"` keys additively; all pre-existing keys verified still present. `db.py` got its first-ever logger (`logging.getLogger("db")`, no handler attached) rather than `utils.setup_logger()` — deliberately the lighter convention (matches `dashboard/app.py`'s own choice) because `db.py` is imported by every test file, and `setup_logger()` would create a real log file on disk as an import side effect for the whole suite.
  - `models.py`: `Job.is_aging` property, mirroring `is_likely_expired`'s tz-safety pattern (UTC-naive `datetime.now()` vs `.replace(tzinfo=None)` on `created_at`) rather than the spec's literal naive-local-time sample code, since `created_at` is stored as UTC (`datetime.utcnow()` in `insert_job()`) and the naive-local version would drift with the machine's timezone offset.
  - `dashboard/app.py`: new `POST /api/jobs/purge-old`, registered immediately after `GET /api/jobs` and before `GET /api/jobs/{job_id}` (confirmed via route introspection, not just by inspection). `/` route now passes `config` into the template context. **Beyond the literal file-touch note:** `is_aging` had to be manually threaded alongside `is_likely_expired` into `GET /api/jobs`, `unskip`, and `answers` responses — it's a plain `@property`, not a `computed_field`, so `model_dump()` never serializes it; without this the aging tooltip (explicitly requested in the same session) would have had no data to render.
  - `dashboard/templates/index.html`: new sidebar "Purge Old Jobs (N)" button (`data-max-age` from `config.MAX_JOB_AGE_DAYS`, hidden at 0, same confirm-dialog/toast pattern as "Purge Expired"), and a small `⌛` tooltip icon (distinct from the `⏳` already used for "Needs Answers") next to the existing `⚠` expiry icon on aging cards — both icons can appear together, neither is deduplicated, no new visible badge.
  - `run.py`: new `maintain` command (`purge_old → validate → purge_dead`, matching the spec's calls into `get_jobs_to_validate()` + `JobValidator().validate_batch()` directly rather than the CLI's argv-parsing `_main()` wrapper or a nonexistent `validate_all()`; a validator exception is caught and logged without blocking `purge_dead`). **Deviation from spec's assumption:** `pipeline` was already `scrape → score → tailor` (3 phases, not the 2-phase `scrape → score` the prompt described) — `maintain` was prepended as a new first phase with the existing three left untouched. Also replicated the existing `/api/jobs/purge`-endpoint's file-cleanup (resume PDF + sibling `_resume.json`) into `maintain`'s purge_dead phase, since `db.purge_dead_jobs()` itself only deletes DB rows — without this, unattended `maintain`/`pipeline` runs (the exact use case Session 14 is being set up for) would leak PDFs on every run with no cleanup path. Added a defensive `db.init_db()` before the `maintain`/`pipeline` CLI dispatch, same fix Session 10 already applied to `apply`, since `purge_old_jobs()` now queries tables immediately.
  - `tests/test_11.py`: 12 tests, all passing (`python -m unittest tests.test_11 -v`). The 7 tests verifying real SQL filtering (protected-status exclusion, age boundaries) and filesystem safety (inside/outside `OUTPUT_RESUME_DIR`, missing file) use an isolated temp SQLite file + temp resume dir (via `config.DB_PATH`/`OUTPUT_RESUME_DIR` swapped in `setUp`/restored in `tearDown`) rather than `MagicMock` — a mocked cursor replays canned data and can't actually evaluate a `WHERE ... NOT IN (...)` clause, so it can't prove the query is correct, only that some Python code called `execute()`. The one atomicity test (mid-transaction failure → no partial commit, no file deletions) correctly uses `MagicMock` instead, since that's a pure control-flow/exception-propagation question. `Job.is_aging`'s boundary tests (day 7/8/9/10/11) construct real `Job` objects directly with patched `config` constants — no DB involved. Full existing suite (`test_7a` + `test_7b` + `test_10`, 52 tests) reverified with zero regressions; all changed files clean under `py_compile`; route ordering for `/api/jobs/purge-old` confirmed via live introspection of `app.routes`.
  - **Not done — needs the user's explicit go-ahead, not just code review:** the spec's Testing Sequence Steps 3/4/5/6/7/8 all either mutate the real `job_auto.db` or would spin up a real dashboard/Playwright session. A read-only `db.get_stats()` smoke test against the real DB (Step 2) showed **`purgeable_old: 265`** — i.e., 265 real accumulated job rows already qualify for auto-purge under `MAX_JOB_AGE_DAYS=10` today. Running the real purge (dashboard button, `python run.py maintain`/`pipeline`, or the spec's own manual Step 3/4 snippets) will permanently delete them; I deliberately did not run any of that myself. Flag this to the user before anyone runs `maintain`/`pipeline`/the dashboard button for the first time — the age threshold may need tuning, or they may want to review what's in that bucket first.
- [x] Session 12 — Dynamic saved searches (multi-role + multi-location)
  - `config.py`: `LINKEDIN_GEO_IDS` dict — only **2 verified cities so far** (`Bengaluru`, `Anywhere in India`, both carried over from the pre-existing single-city constants). Per the spec's own explicit instruction, no other cities (Chennai/Pune/Hyderabad/Mumbai/Delhi NCR) were added — those GeoIDs must come from the user verifying each one against a real LinkedIn search URL; a guessed GeoID silently scrapes the wrong region. `INTER_SEARCH_DELAY_SECONDS=(15,45)` added alongside. `TARGET_TITLES`/`LINKEDIN_GEO_ID`/`MAX_JOBS_PER_SCRAPE` all left untouched as the backwards-compat fallback.
  - `db.py`: `searches` table migration (`titles_json` TEXT, boolean columns as INTEGER 0/1 matching `jobs.is_easy_apply`'s existing convention) plus 9 helpers (`get_all_searches`, `get_enabled_searches`, `get_search_by_id`, `insert_search`, `update_search` (whitelisted partial update, silently ignores unknown fields), `delete_search`, `toggle_search`, `update_search_last_run`) and a single `_row_to_saved_search()` deserialization point (JSON-decodes titles, int→bool conversion) that every read path routes through.
  - `models.py`: `SavedSearch` Pydantic model with the spec's exact validator (non-empty name/titles/geo_id, `max_results` in [1,100]).
  - `scraper.py`: **Found and fixed a real latent gap while threading this through** — `search_jobs()` already accepted a `location` parameter, but it was only ever used for the log line; the actual city filter was hardcoded to `config.LINKEDIN_GEO_ID` regardless of what was passed in. Added a real `geo_id` parameter (defaulting to `config.LINKEDIN_GEO_ID`) so per-search cities actually take effect — without this fix, every saved search would have silently scraped Bengaluru regardless of its configured `geo_label`. `scrape()` gained `search: SavedSearch | None = None` and `max_results_override: int | None = None`; `search=None` reproduces prior behavior exactly (same titles/geo/cap/`easy_apply_only=True`). Deliberately left the `is_easy_apply=True` hardcoded in the inserted `Job(...)` untouched even for `easy_apply_only=False` searches, per the spec's explicit "do not touch `_is_easy_apply()`/stealth logic" — the real correction already happens later via `validator.py`'s live page inspection, same as it does today for every scraped job regardless of search config. CLI gained `--search-id N`.
  - `run.py`: `pipeline`'s scrape step now checks `db.get_enabled_searches()` first; empty → unchanged config-fallback behavior; non-empty → iterates with a `remaining_budget` counter (`min(search.max_results, remaining_budget)` per search), logs and breaks when the global `MAX_JOBS_PER_SCRAPE` cap is hit, and inserts a randomized 15–45s delay between searches (skipped after the last one). `maintain`/`validate`/`score`/`apply`/`dashboard` commands and Session 11's age-purge behavior are untouched.
  - `dashboard/app.py`: 8 new endpoints, registered in the exact order the spec required (`discover` → `geos` → collection → parameterized) — confirmed via live `app.routes` introspection, not just visual inspection, since Session 11's own retro noted this exact ordering mistake as a recurring risk. `POST/PUT /api/searches[/{id}]` reject an unrecognized `geo_label` with 400 rather than trusting a client-supplied GeoID. `/searches` page route added alongside `/resume`, passing `config` into the template context (unused by the template itself today, kept only for consistency with the pattern Session 11 established).
  - `dashboard/templates/searches.html`: new page — a Discover form (one-off, unsaved) plus a Saved Searches list with an edit modal. The titles chip input reuses `resume.html`'s skill-tag functions (`addSkillTag`/`handleSkillInput`/`collectSkillTags`) copied verbatim rather than reimplemented, per the spec's explicit instruction. `resume.html` was **deliberately left untouched** — read in full before starting, and it turns out it has no Jobs/Resume/Searches nav at all (just a plain "← Back to Jobs" link), so the spec's conditional ("add the link if resume.html has the same nav") resolves to no-op anyway; it's also on the explicit may-not-touch list.
  - `dashboard/templates/index.html`: one nav-link addition (`🔍 Searches` → `/searches`), nothing else touched.
  - `tests/test_12.py`: 26 tests, all passing. Same methodology split as Session 11: real isolated temp-SQLite for anything needing genuine SQL/JSON-round-trip correctness (schema, CRUD, `titles_json` serialization, boolean conversion — a `MagicMock` cursor can't evaluate a real query or prove real JSON got written), FastAPI `TestClient` + that same temp DB for endpoint tests (with `_discover_background`/`_run_search_background` mocked out so a test run never fires a real LinkedIn request), and `MagicMock`/`AsyncMock` for `run.py pipeline`'s budget-enforcement/inter-search-delay control flow (with `run.run_maintain`, `scorer.JobScorer`, `tailor.ResumeTailor`, and `renderer.ResumeRenderer` all mocked out too, since `run_pipeline()` runs those unconditionally after the scrape step and they'd otherwise make real Anthropic calls or touch real resume files). Full regression suite (`test_7a`+`test_7b`+`test_10`+`test_11`+`test_12` = 90 tests) reverified with zero regressions.
  - **Two real bugs found and fixed during verification, not just theoretical risks:** (1) a Jinja2 `TemplateSyntaxError` → 500 on `/searches` — an HTML *comment* in `searches.html` documenting the "no `{{ }}` inside `<script>`" rule itself literally contained `{{ }}`, and Jinja2 parses its delimiters regardless of HTML comment context, so the rule-reminder comment broke the exact rule it was warning about. Reworded to avoid the literal braces. (2) The four new `run.py pipeline` tests initially crashed on a pre-existing (not new) Windows console limitation — `rich`'s `Console` hits a legacy-console `UnicodeEncodeError` on `✓` when stdout isn't a real interactive terminal, as under this test runner; worked around by mocking `run.console` for those tests only, not by changing any real CLI output.
  - **Verified live, not just via automated tests:** started the real dashboard and drove `/searches` with Playwright end-to-end — nav link from `/`, modal open, chip add (Enter)/remove (×), city dropdown populated from `/api/searches/geos`, Save creating a real card, the Enabled toggle, Edit prefill, Cancel-without-prompt-when-clean, and Delete-with-confirm all passed against the real local DB with **zero console errors**. Left no test data behind (deleted the Playwright-created rows after) and stopped the dashboard process cleanly afterward (confirmed port 8000 fully released) rather than leaving it stuck, unlike the port issue flagged in Session 9's notes. **"Run Now" and "Discover" were not live-clicked** — both trigger a real LinkedIn scrape via `LinkedInScraper`, so that path is verified only by code review + the mocked test suite, matching how Sessions 8/10/11 already handle anything with real external side effects.
- [ ] Opus Review — Auth/session safety review before going live; full dashboard feature set (Sessions 5–12) is otherwise complete