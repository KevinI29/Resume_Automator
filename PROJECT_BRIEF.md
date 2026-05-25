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
weasyprint     — HTML/CSS resume → PDF
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
- [ ] Session 5 — Dashboard (FastAPI)
- [ ] Session 6 — Easy Apply automation (Playwright)
- [ ] Opus Review — Auth/session safety review before going live