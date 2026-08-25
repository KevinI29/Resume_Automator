from dotenv import load_dotenv
import os

load_dotenv()

# Secrets
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
LINKEDIN_COOKIE: str = os.environ["LINKEDIN_COOKIE"]
LINKEDIN_CSRF_TOKEN: str = os.environ["LINKEDIN_CSRF_TOKEN"]

# Job search preferences
TARGET_TITLES: list[str] = ["Software Engineer", "Backend Engineer", "Data Engineer", "Full Stack Engineer", "Software Developer" ]
TARGET_LOCATION: str = ""       # human-readable label (used in logs only)
LINKEDIN_GEO_ID: str = "105214831"  # LinkedIn geoId — 102713980=India, 105214831=Bangalore

# Multi-city GeoIDs (Session 12) — add new cities by opening a LinkedIn job
# search for that city and copying the ?geoId=... value from the URL.
# Verified values only — do NOT invent/guess a GeoID; a wrong number
# silently scrapes the wrong region and pollutes the pipeline.
LINKEDIN_GEO_IDS: dict[str, str] = {
    "Bengaluru": "105214831",         # verified (existing LINKEDIN_GEO_ID)
    "Anywhere in India": "102713980", # verified (existing India-wide)
}

# Delay between saved searches within one pipeline run (stealth) — distinct
# from MIN/MAX_DELAY_SECONDS, which govern delays *within* a single search
INTER_SEARCH_DELAY_SECONDS: tuple[int, int] = (15, 45)
EXPERIENCE_YEARS: int = 1       # My years of experience (for filtering)
PHONE_NUMBER: str = "9884561353"  # Local digits only — country code already selected in form
CURRENT_CTC: int = 340000           # Fresher — no current CTC
EXPECTED_CTC: int = 700000     # 5 LPA in rupees
NOTICE_PERIOD_DAYS: int = 90    # Immediate joiner
MIN_FIT_SCORE: int = 6          # Only apply to jobs scoring 6+
REMOTE_ONLY: bool = False       # Set True to filter remote-only jobs

# Safety limits — do not change these without thinking
MAX_JOBS_PER_SCRAPE: int = 50
MAX_APPLICATIONS_PER_DAY: int = 15
MIN_DELAY_SECONDS: int = 3
MAX_DELAY_SECONDS: int = 8

# Paths
DB_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_auto.db")
RESUME_MASTER_PATH: str = "resume/master.json"
RESUME_TEMPLATE_PATH: str = "resume/template.html"
OUTPUT_RESUME_DIR: str = "output/resumes"
LOG_DIR: str = "logs"

# Job expiry settings
JOB_EXPIRY_HOURS = 48           # Jobs older than this get a visual warning in the dashboard

# Age-based purge (Session 11)
MAX_JOB_AGE_DAYS: int = 10          # Jobs older than this get auto-purged
JOB_AGE_WARNING_DAYS: int = 8       # Show "aging" indicator at this age
AGE_PURGE_PROTECTED_STATUSES: tuple[str, ...] = ("applied", "pending_questions")

# Resume backup settings
RESUME_BACKUP_DIR = "resume/backups"   # Where timestamped master.json backups are stored
MAX_RESUME_BACKUPS = 10               # Keep only the N most recent backups; delete older ones

# pdfkit — path to wkhtmltopdf binary (download from https://wkhtmltopdf.org/downloads.html)
WKHTMLTOPDF_PATH: str = os.getenv(
    "WKHTMLTOPDF_PATH",
    r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe",
)

# Adaptive Q&A resolution (Session 10)
QA_AI_CONFIDENCE_THRESHOLD: float = 0.75
# AI answers with confidence below this threshold are not auto-filled —
# they're stored as suggestions in pending_answers for manual review.
# Tune after first real batch run; 0.75 is a conservative starting point.

QA_FUZZY_MATCH_THRESHOLD: float = 0.85
# difflib.SequenceMatcher ratio required to trust a qa_bank fuzzy match.
# Higher = fewer false matches but more tier-4 AI calls.

# --- Remediation Session 1 additions ---

# Tailoring model (Decision 5 — Tailoring model selection)
# Haiku: fast and cheap, used in sessions 1-10 reality. claude-haiku-4-5
# Sonnet: quality calls as per original design spec. claude-sonnet-5 (latest, most capable)
# Change this value to switch without touching tailor.py.
TAILOR_MODEL: str = "claude-sonnet-5"

# Location — there are FOUR distinct location concepts in this codebase.
# Do not conflate them:
#   1. LINKEDIN_GEO_ID / LINKEDIN_GEO_IDS (above) — real geoIds the scraper's
#      voyager API uses to filter search results by city.
#   2. TARGET_LOCATION (above) — empty by default; only a log-line label for
#      the config-fallback scrape path (scraper.py). Not used functionally.
#   3. JOB_SEARCH_LOCATION (below) — the plain-text city name typed into the
#      Easy Apply modal's "current location" autocomplete field when it's
#      blank (applicant.py:532). A UI form value, unrelated to search geoIds.
#   4. master.json personal.location — the candidate's residential address as
#      printed on the resume. Never substitute for #1-#3.
# Session 2 will use JOB_SEARCH_LOCATION in applicant.py to replace the
# hardcoded "Bengaluru" at applicant.py:532. This default preserves that
# same hardcoded value — no behavior change in Session 1, just configurability.
JOB_SEARCH_LOCATION: str = "Bengaluru"
