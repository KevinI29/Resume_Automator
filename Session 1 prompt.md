# Session 1 — Project Scaffold, Config, Database, Master Resume

> Paste this immediately after PROJECT_BRIEF.md in Claude Code.

---

## Session 1 Goal
Set up the complete project foundation. By the end of this session we should have:
- All folders and empty module files created
- `requirements.txt` with all dependencies
- `.env` template (no real secrets committed)
- `config.py` — all user preferences in one place
- `db.py` — SQLite setup + all helpers (create tables, insert, query, update)
- `models.py` — Pydantic models for Job and Application
- `resume/master.json` — my structured resume in the schema we defined
- `.gitignore` set up correctly (secrets, outputs, logs all ignored)

## Task List for This Session

### 1. Scaffold the project
Create the full folder structure from the project brief. Create empty `__init__` or placeholder files where needed so the structure is clear.

### 2. requirements.txt
Generate `requirements.txt` with pinned versions for:
httpx, playwright, anthropic, weasyprint, fastapi, uvicorn, jinja2, 
python-dotenv, schedule, pydantic, aiofiles, rich (for nice terminal output)

### 3. .env template
Create `.env.example` (committed to git) with these keys and comments explaining each:
```
ANTHROPIC_API_KEY=           # Get from console.anthropic.com
LINKEDIN_COOKIE=             # li_at cookie value from your browser
LINKEDIN_CSRF_TOKEN=         # JSESSIONID cookie value
```
Create `.env` as a copy of `.env.example` (but .gitignored).

### 4. config.py
A single config file that loads from `.env` and defines all user preferences:
```python
# Job search preferences
TARGET_TITLES = ["Software Engineer", "Backend Engineer", "Full Stack Engineer"]
TARGET_LOCATION = ""          # e.g. "Bengaluru" or "Remote"
EXPERIENCE_YEARS = 0          # My years of experience (for filtering)
MIN_FIT_SCORE = 6             # Only apply to jobs scoring 6+
REMOTE_ONLY = False           # Set True to filter remote-only jobs

# Safety limits (do not change these without thinking)
MAX_JOBS_PER_SCRAPE = 50
MAX_APPLICATIONS_PER_DAY = 15
MIN_DELAY_SECONDS = 3
MAX_DELAY_SECONDS = 8

# Paths
DB_PATH = "job_auto.db"
RESUME_MASTER_PATH = "resume/master.json"
RESUME_TEMPLATE_PATH = "resume/template.html"
OUTPUT_RESUME_DIR = "output/resumes"
LOG_DIR = "logs"
```

### 5. models.py
Pydantic v2 models for:
- `Job` — mirrors the jobs table, includes a method `is_actionable()` that returns True if status is 'approved'
- `Application` — mirrors the applications table
- `ResumeSection` — represents one section of the resume JSON
- `MasterResume` — the full resume structure

### 6. db.py
SQLite database module with:
- `init_db()` — creates tables if they don't exist (run on startup)
- `insert_job(job: Job)` — inserts, ignores if linkedin_job_id already exists
- `get_jobs_by_status(status: str) -> list[Job]`
- `update_job_status(job_id: int, status: str)`
- `insert_application(application: Application)`
- `get_daily_application_count() -> int` — count of applications submitted today (for the daily cap check)
- `get_all_jobs() -> list[Job]` — for the dashboard

Use context managers for connections. Never leave connections open.

### 7. resume/master.json
Create the master resume JSON schema with placeholder data. The structure should be:
```json
{
  "personal": {
    "name": "Your Name",
    "email": "you@email.com",
    "phone": "+91-XXXXXXXXXX",
    "location": "Bengaluru, India",
    "linkedin": "linkedin.com/in/yourprofile",
    "github": "github.com/yourusername"
  },
  "summary": "One paragraph professional summary...",
  "experience": [
    {
      "company": "Company Name",
      "title": "Job Title",
      "start": "Jan 2022",
      "end": "Present",
      "location": "Bengaluru, India",
      "bullets": [
        "Bullet point 1 — quantified achievement",
        "Bullet point 2 — quantified achievement"
      ]
    }
  ],
  "education": [
    {
      "institution": "University Name",
      "degree": "B.Tech Computer Science",
      "year": "2021",
      "gpa": ""
    }
  ],
  "skills": {
    "languages": ["Python", "TypeScript", "Go"],
    "frameworks": ["FastAPI", "React", "Node.js"],
    "tools": ["PostgreSQL", "Redis", "Docker", "AWS"],
    "other": []
  },
  "projects": [
    {
      "name": "Project Name",
      "description": "What it does and the impact",
      "tech": ["Python", "FastAPI"],
      "url": ""
    }
  ]
}
```

### 8. .gitignore
Include: `.env`, `output/`, `logs/`, `*.db`, `__pycache__/`, `.venv/`, `*.pyc`, `job_auto.db`

### 9. README.md
Short README with:
- What the project is (one paragraph)
- Setup instructions (clone, create venv, pip install, copy .env.example, fill in values)
- How to run each module

---

## After This Session
Update `PROJECT_BRIEF.md` → mark Session 1 complete, add any new decisions made.

The next session (Session 2) will build `scraper.py` — the LinkedIn voyager API integration.