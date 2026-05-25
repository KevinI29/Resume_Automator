# Session 2 — LinkedIn Job Scraper

> Paste this immediately after PROJECT_BRIEF.md in Claude Code.

---

## Session 2 Goal
Build `scraper.py` — a safe, rate-limited LinkedIn job scraper that hits LinkedIn's
internal voyager API using the session cookie. By the end of this session:
- Can search LinkedIn jobs by title, location, and filters
- Returns structured Job objects saved to SQLite
- Respects all anti-bot safety rules (delays, caps, headers)
- Has a CLI entry point to run manually and see results in terminal
- Fully testable without triggering any application logic

---

## How LinkedIn's Voyager API Works
LinkedIn's frontend talks to an internal REST API at:
```
https://www.linkedin.com/voyager/api/jobs/search
```
This is NOT a public API but it's what the browser uses. We call it with our
session cookie to authenticate — exactly like the browser would.

Key headers required for every request:
```python
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/vnd.linkedin.normalized+json+2.1",
    "Accept-Language": "en-US,en;q=0.9",
    "x-li-lang": "en_US",
    "x-restli-protocol-version": "2.0.0",
    "csrf-token": LINKEDIN_CSRF_TOKEN,   # from config
    "Cookie": f"li_at={LINKEDIN_COOKIE}; JSESSIONID={LINKEDIN_CSRF_TOKEN}",
}
```

Job search endpoint:
```
GET https://www.linkedin.com/voyager/api/jobs/search
    ?keywords=Software Engineer
    &location=Bengaluru
    &start=0          # pagination offset
    &count=25         # results per page (max 25)
    &f_TPR=r86400     # posted in last 24 hours (r604800 = last week)
    &f_AL=true        # Easy Apply only (remove this param for all jobs)
    &sortBy=DD        # sort by date
```

Response is JSON — jobs are nested inside:
`data.elements[].jobCardUnion.jobPostingCard`

Each job card has:
- `jobPostingUrn` — unique ID (extract the number from the end)
- `jobPostingTitle` — job title
- `primaryDescription.text` — company name
- `secondaryDescription.text` — location
- `easyApplyUrl` — present only if Easy Apply

To get the full job description, make a second call:
```
GET https://www.linkedin.com/voyager/api/jobs/jobPostings/{job_id}
```
The description is at `data.description.text`

---

## Task List for This Session

### 1. scraper.py — Main scraper class

Build a `LinkedInScraper` class with these methods:

**`__init__(self)`**
- Load cookie and CSRF token from config
- Set up httpx AsyncClient with correct headers
- Set up logger

**`async search_jobs(keywords: str, location: str, easy_apply_only: bool, max_results: int) -> list[dict]`**
- Paginates through results (25 per page) until max_results reached
- Adds randomized delay between pages (use config MIN/MAX_DELAY_SECONDS)
- Returns raw job card dicts

**`async get_job_description(job_id: str) -> str`**
- Fetches full job description for a single job ID
- Returns the description text
- Handles 404 gracefully (return empty string)

**`async scrape() -> list[Job]`**
- The main entry point
- Calls search_jobs() using TARGET_TITLES and TARGET_LOCATION from config
- For each result calls get_job_description()
- Adds randomized delay between each description fetch (critical — this is the most suspicious pattern)
- Converts raw dicts → Job model objects
- Filters out jobs already in the database (check by linkedin_job_id)
- Calls db.insert_job() for each new job
- Returns list of new Job objects
- Hard stops if MAX_JOBS_PER_SCRAPE is reached

**`_extract_job_id(urn: str) -> str`**
- Extracts numeric ID from LinkedIn URN string
- e.g. `"urn:li:fsd_jobPosting:3912345678"` → `"3912345678"`

**`_build_job_url(job_id: str) -> str`**
- Returns `https://www.linkedin.com/jobs/view/{job_id}`

### 2. Safe request helper
Add a private `async _get(url, params)` method that:
- Wraps every httpx call in try/except
- Logs the URL being called (without cookie values)
- Checks response status — if 401/403, logs "Cookie expired — refresh your LinkedIn cookie" and raises an exception
- If 429 (rate limited), waits 60 seconds and retries once
- Returns parsed JSON or None on failure

### 3. Human-like delay helper
```python
import random, asyncio

async def _random_delay(self):
    delay = random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
    await asyncio.sleep(delay)
```
Call this between EVERY request — both search pages and description fetches.

### 4. CLI entry point
At the bottom of scraper.py, add:
```python
if __name__ == "__main__":
    import asyncio
    from rich.console import Console
    from rich.table import Table

    async def main():
        scraper = LinkedInScraper()
        console = Console()
        console.print("[bold green]Starting LinkedIn scraper...[/bold green]")
        jobs = await scraper.scrape()
        
        table = Table(title=f"Found {len(jobs)} new jobs")
        table.add_column("Title", style="cyan")
        table.add_column("Company", style="magenta")
        table.add_column("Location")
        table.add_column("Easy Apply", style="green")
        table.add_column("Score")
        
        for job in jobs:
            table.add_row(
                job.title,
                job.company,
                job.location,
                "✓" if job.is_easy_apply else "✗",
                str(job.fit_score or "—")
            )
        
        console.print(table)
        console.print(f"[bold]Saved to database.[/bold]")

    asyncio.run(main())
```

### 5. Logging
Every scraper run should log to `logs/scraper_YYYY-MM-DD.log`:
- Start time, search params used
- Number of results found per search
- Each job ID fetched + whether it was new or already in DB
- Total new jobs saved
- Any errors (with full traceback)

Use Python's built-in `logging` module. Create a `setup_logger()` helper in a
new `utils.py` file that both scraper.py and future modules can import.

### 6. utils.py
Create this utility module with:
- `setup_logger(name: str) -> logging.Logger` — configures file + console logging
- `random_delay(min_s: float, max_s: float)` — async sleep with random duration
- `clean_text(text: str) -> str` — strips excessive whitespace from scraped text

---

## Error Handling Requirements
Handle these specific failure cases gracefully:
- **401/403** — Cookie expired. Print clear message, stop scraper, do not crash.
- **429** — Rate limited. Wait 60s, retry once. If still 429, stop and log.
- **Connection error** — Log and skip that job, continue with rest.
- **JSON parse error** — Log the raw response, skip that job.
- **Empty results** — Log "No jobs found for query X" and return empty list.

---

## What NOT to build in this session
- No AI scoring yet (that's Session 3)
- No resume tailoring
- No dashboard
- No Playwright / Easy Apply
- fit_score can be None/null for all jobs saved in this session

---

## Testing the Scraper
After building, test by running:
```bash
cd job-auto
python scraper.py
```
Expected output: a rich table of jobs found, saved to the SQLite database.
Check the database with:
```bash
python -c "import db; jobs = db.get_jobs_by_status('new'); [print(j.title, j.company) for j in jobs]"
```

---

## After This Session
- Mark Session 2 complete in PROJECT_BRIEF.md
- Note how many jobs were returned in a test run
- Note any LinkedIn API quirks discovered
- Session 3 will add Haiku scoring on top of the jobs already in the database
