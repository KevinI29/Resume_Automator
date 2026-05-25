from dotenv import load_dotenv
import os

load_dotenv()

# Secrets
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
LINKEDIN_COOKIE: str = os.environ["LINKEDIN_COOKIE"]
LINKEDIN_CSRF_TOKEN: str = os.environ["LINKEDIN_CSRF_TOKEN"]

# Job search preferences
TARGET_TITLES: list[str] = ["Software Engineer", "Backend Engineer", "Full Stack Engineer"]
TARGET_LOCATION: str = ""       # human-readable label (used in logs only)
LINKEDIN_GEO_ID: str = "105214831"  # LinkedIn geoId — 102713980=India, 105214831=Bangalore
EXPERIENCE_YEARS: int = 0       # My years of experience (for filtering)
MIN_FIT_SCORE: int = 6          # Only apply to jobs scoring 6+
REMOTE_ONLY: bool = False       # Set True to filter remote-only jobs

# Safety limits — do not change these without thinking
MAX_JOBS_PER_SCRAPE: int = 50
MAX_APPLICATIONS_PER_DAY: int = 15
MIN_DELAY_SECONDS: int = 3
MAX_DELAY_SECONDS: int = 8

# Paths
DB_PATH: str = "job_auto.db"
RESUME_MASTER_PATH: str = "resume/master.json"
RESUME_TEMPLATE_PATH: str = "resume/template.html"
OUTPUT_RESUME_DIR: str = "output/resumes"
LOG_DIR: str = "logs"

# pdfkit — path to wkhtmltopdf binary (download from https://wkhtmltopdf.org/downloads.html)
WKHTMLTOPDF_PATH: str = os.getenv(
    "WKHTMLTOPDF_PATH",
    r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe",
)
