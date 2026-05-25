# Job Application Automator

A personal, local Python tool that automates the LinkedIn job application process. Scrapes job listings via LinkedIn's internal API, uses Claude AI to score and tailor your resume per job, and submits Easy Apply applications via Playwright — with a human approval step before anything is submitted.

## Setup

```bash
# 1. Clone and create a virtual environment
git clone <repo>
cd job-auto
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 3. Configure secrets
copy .env.example .env        # Windows
# cp .env.example .env        # macOS/Linux
# Edit .env — fill in ANTHROPIC_API_KEY, LINKEDIN_COOKIE, LINKEDIN_CSRF_TOKEN

# 4. Initialize the database
python -c "import db; db.init_db()"
```

## How to get your LinkedIn cookies

1. Log in to LinkedIn in Chrome/Edge
2. Open DevTools (F12) → Application → Cookies → `https://www.linkedin.com`
3. Copy the value of `li_at` → paste into `LINKEDIN_COOKIE`
4. Copy the value of `JSESSIONID` → paste into `LINKEDIN_CSRF_TOKEN`

## Running each module

```bash
# Scrape jobs (Session 2)
python scraper.py

# Score scraped jobs (Session 3)
python scorer.py

# Launch the approval dashboard (Session 5)
uvicorn dashboard.app:app --reload

# Apply to approved jobs (Session 6)
python applicant.py
```

## Safety limits

- Max 50 jobs scraped per session
- Max 15 Easy Apply submissions per day
- All requests use 3–8 second randomized delays
- Browser runs headed (visible) — not headless
- Human approval required before any application is submitted
