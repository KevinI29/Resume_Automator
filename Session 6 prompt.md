# Session 6 — Easy Apply Automation (Playwright)

> Paste this immediately after PROJECT_BRIEF.md in Claude Code.

---

## Session 6 Goal
Build `applicant.py` — the final module. Uses Playwright to automate LinkedIn
Easy Apply submissions for approved jobs that have tailored resumes ready.

By the end of this session:
- Opens a real browser, navigates to LinkedIn, loads a job page
- Fills the Easy Apply form (upload resume, answer basic questions)
- Submits the application
- Updates the database status to 'applied'
- Dashboard shows "Applied" status
- Full pipeline is complete: scrape → score → approve → tailor → apply

---

## CRITICAL SAFETY RULES — READ BEFORE CODING

These are non-negotiable. Breaking them risks a permanent LinkedIn ban.

1. **HEADED BROWSER ONLY** — never run headless. LinkedIn detects headless.
2. **Persistent browser context** — reuse the same browser profile across runs
   so cookies/localStorage persist (looks like a returning user, not a bot)
3. **MAX 10-15 applications per day** — hard cap, enforced in code
4. **Human-like delays everywhere**:
   - 2-5 seconds before each click
   - 1-3 seconds between typing characters (not instant fill)
   - 3-8 seconds between form pages
   - Random variation on everything — never exact same delay
5. **Load the full job page first** — scroll through the description, wait
   5-10 seconds, THEN click Easy Apply. LinkedIn tracks if you apply
   without reading.
6. **Random mouse movement** — don't click exact center of buttons. Add
   random pixel offset (±5-15px).
7. **Stop on ANY unexpected state** — if a CAPTCHA appears, if the form
   has unexpected fields, if anything looks wrong: stop, log it, move
   to the next job. Never force through.

---

## How LinkedIn Easy Apply Works

Easy Apply is a 1-3 step modal form overlaid on the job page:

**Step 1 — Contact Info**
- Pre-filled: name, email, phone (from LinkedIn profile)
- Resume upload button (we upload the tailored PDF)
- Sometimes a headline or summary field

**Step 2 — Additional Questions (optional)**
- Common: years of experience, work authorization, salary expectations
- Sometimes: custom text fields from the employer
- Radio buttons, dropdowns, or text inputs

**Step 3 — Review & Submit**
- Shows a review of your application
- "Submit application" button

The number of steps varies per job. Some are 1-step, some are 3-step.
There is a "Next" button between steps and a "Submit application" button
on the final step.

---

## Task List for This Session

### 1. applicant.py — Main automation class

Build a `LinkedInApplicant` class:

**`__init__(self)`**
- Set up logger
- Define the persistent browser context path:
  `browser_data/linkedin_profile/`
- Load config values

**`async setup_browser(self)`**
- Launch Playwright Chromium in HEADED mode
- Use persistent context: `browser.launch_persistent_context()`
  with user_data_dir for cookie persistence
- Set viewport: 1280x800 (realistic laptop size)
- Set user agent to match a real Chrome browser
- Set locale to 'en-US'
- Disable webdriver detection flag:
  ```python
  args=['--disable-blink-features=AutomationControlled']
  ```

**`async _human_delay(self, min_s=2.0, max_s=5.0)`**
- Random async sleep with jitter
- Log the delay at debug level

**`async _human_type(self, element, text)`**
- Types text character by character with random delays (50-150ms per char)
- Simulates human typing speed

**`async _human_click(self, element)`**
- Gets element bounding box
- Adds random offset (±5-15px from center)
- Clicks at the offset position
- Waits 1-2 seconds after click

**`async _scroll_job_page(self, page)`**
- Scrolls down the job description slowly (300px at a time)
- Random delay between each scroll (1-3 seconds)
- Scrolls back to top after reading
- Total time spent: 5-10 seconds (simulates reading the JD)

**`async navigate_to_job(self, page, url: str) -> bool`**
- Navigates to the job URL
- Waits for page load
- Checks for "No longer accepting applications" text — return False if found
- Calls _scroll_job_page() to simulate reading
- Returns True if job is open and ready

**`async click_easy_apply(self, page) -> bool`**
- Finds the "Easy Apply" button on the job page
- Clicks it with human-like delay
- Waits for the modal to appear
- Returns True if modal opened, False if button not found

**`async upload_resume(self, page, pdf_path: str) -> bool`**
- Inside the Easy Apply modal, finds the resume upload area
- If there's an existing resume shown, clicks "Replace" or the upload button
- Uploads the tailored PDF file
- Waits for upload confirmation
- Returns True on success

**`async fill_form_fields(self, page) -> bool`**
- Looks for common form fields in the modal and fills them:
  - Phone number: use from config or master resume
  - Years of experience: use from config.EXPERIENCE_YEARS
  - Work authorization: select "Yes" if the question appears
  - Salary expectations: skip or enter a reasonable default
  - Custom text fields: leave empty (better than wrong answers)
- For dropdown/select fields: try to select the most reasonable option
- Returns True if all required fields were handled

**`async handle_form_steps(self, page) -> bool`**
- This is the core state machine for the multi-step form
- Loop:
  1. Check if "Submit application" button exists → if yes, click it → done
  2. Check if "Next" button exists → fill current page → click Next
  3. Check if "Review" button exists → click Review
  4. If none found after 10 seconds → log error, return False
- Between each step: human delay (3-8 seconds)
- Max 5 steps (safety limit — if we're past 5, something went wrong)
- Returns True if application was submitted

**`async apply_to_job(self, job: Job) -> bool`**
- The full flow for one job:
  1. Check daily application cap — abort if reached
  2. Navigate to job page
  3. Check if job is still open
  4. Scroll and "read" the page
  5. Click Easy Apply
  6. Upload tailored resume
  7. Fill form fields
  8. Handle form steps until submitted
  9. Update database: status='applied', applied_at=now()
  10. Log success
- Returns True if applied successfully
- On ANY failure: log the step that failed, take a screenshot
  (save to logs/screenshots/), return False

**`async apply_batch(self, jobs: list[Job]) -> dict`**
- Applies to a list of approved jobs sequentially
- Enforces MAX_APPLICATIONS_PER_DAY cap
- Human-like delay between jobs (60-120 seconds — not rapid fire!)
- Returns summary dict: {"applied": 3, "failed": 1, "skipped": 2}
- Stops entirely if 3 consecutive failures (something is wrong)

**`async close(self)`**
- Closes the browser context
- Logs session summary

### 2. Handling unexpected situations

**CAPTCHA detected:**
- Check for CAPTCHA indicators on page (common selectors:
  iframe[src*="captcha"], div.captcha, #captcha)
- If found: log "CAPTCHA detected — stopping automation",
  take screenshot, stop ALL further applications in this session
- Do NOT attempt to solve it

**"Not accepting applications" detected:**
- Update job status to 'closed' (add this status to the system)
- Skip to next job

**Unexpected form fields:**
- If a required field has no matching handler: log it,
  take a screenshot, click "Dismiss" or X to close the modal,
  mark job as 'failed', move on

**Connection/timeout errors:**
- Wait 30 seconds, retry once
- If still failing, stop the session

### 3. Screenshot helper
Save debugging screenshots on every failure:
```python
async def _take_screenshot(self, page, name: str):
    path = f"logs/screenshots/{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    await page.screenshot(path=path)
    self.logger.info(f"Screenshot saved: {path}")
```

### 4. Update db.py
Add:
```python
def get_approved_jobs_with_resume() -> list[Job]:
    """Returns jobs with status='approved' and resume_path set (not null, not 'failed')"""

def update_job_applied(job_id: int):
    """Sets status='applied' and applied_at to current timestamp"""

def get_daily_application_count() -> int:
    """Count of jobs applied today (for daily cap enforcement)"""
    # WHERE status='applied' AND date(applied_at) = date('now')
```

### 5. Update models.py
Add to Job model: `applied_at: datetime | None = None`

### 6. Dashboard integration
In dashboard/app.py, add:

`POST /api/jobs/{job_id}/apply` — manually trigger application for one job
- Only works if job is approved and has resume_path
- Calls apply_to_job() as background task
- Returns {"status": "applying"}

`POST /api/apply-all` — apply to all approved jobs with resumes
- Triggers apply_batch() as background task
- Returns {"status": "started", "count": N}

In dashboard/templates/index.html:
- Add an "Apply" button on approved cards that have resumes
- Add an "Apply All" button in the sidebar
- Show "Applying..." state while application is in progress
- Show "Applied ✓" with green badge when done

### 7. CLI entry point
```python
if __name__ == "__main__":
    import asyncio, sys
    from rich.console import Console

    async def main():
        console = Console()
        applicant = LinkedInApplicant()

        if "--single" in sys.argv:
            # Apply to one specific job
            job_id = int(sys.argv[sys.argv.index("--single") + 1])
            job = db.get_job_by_id(job_id)
            if not job:
                console.print(f"[red]Job {job_id} not found[/red]")
                return
            await applicant.setup_browser()
            success = await applicant.apply_to_job(job)
            console.print(f"[green]Applied![/green]" if success else "[red]Failed[/red]")
        else:
            # Apply to all approved jobs with resumes
            jobs = db.get_approved_jobs_with_resume()
            console.print(f"[bold]Found {len(jobs)} jobs ready to apply[/bold]")
            await applicant.setup_browser()
            results = await applicant.apply_batch(jobs)
            console.print(f"[green]Applied: {results['applied']}[/green]")
            console.print(f"[red]Failed: {results['failed']}[/red]")
            console.print(f"[yellow]Skipped: {results['skipped']}[/yellow]")

        await applicant.close()

    asyncio.run(main())
```

### 8. Update run.py
Add a new command:
```python
elif command == "apply":
    from applicant import LinkedInApplicant
    applicant = LinkedInApplicant()
    jobs = db.get_approved_jobs_with_resume()
    asyncio.run(applicant.setup_browser())
    asyncio.run(applicant.apply_batch(jobs))
    asyncio.run(applicant.close())
elif command == "dashboard":
    # existing dashboard code
```

Full pipeline command also gets the apply step:
```
python run.py scrape      # just scrape
python run.py score       # just score
python run.py apply       # just apply
python run.py dashboard   # start dashboard
python run.py pipeline    # scrape → score (apply is manual)
```

Note: `pipeline` command does NOT include apply — applying should always be
a deliberate action, never part of an automatic pipeline.

---

## Playwright Installation
Before this session, install Playwright:
```bash
pip install playwright
playwright install chromium
```
The second command downloads the Chromium browser binary.

---

## Testing This Session

**Step 0 — Install Playwright:**
```bash
pip install playwright
playwright install chromium
```

**Step 1 — Test on ONE job first (the safest test):**
```bash
# Pick an approved job with a resume
python -c "
import db
jobs = db.get_approved_jobs_with_resume()
for j in jobs[:3]:
    print(f'ID: {j.id} | {j.title} @ {j.company} | Resume: {j.resume_path}')
"

# Apply to just that one job
python applicant.py --single <job_id>
```

Watch the browser — it should:
1. Open Chromium
2. Navigate to the LinkedIn job page
3. Scroll through the description (5-10 seconds)
4. Click Easy Apply
5. Upload your tailored resume
6. Fill any form fields
7. Submit

**Step 2 — Check the result:**
- Did it submit successfully?
- Check the dashboard — status should be "Applied"
- Check logs/screenshots/ for any failure screenshots

**Step 3 — If Step 1 worked, test batch (2-3 jobs max):**
```bash
python applicant.py
```
Watch the timing — there should be 60-120 second gaps between applications.

---

## First Run Gotcha: LinkedIn Login

The FIRST time Playwright opens LinkedIn, you won't be logged in (it's a
fresh browser profile). You have two options:

**Option A (recommended):** On the first run, the browser will open LinkedIn
and show the login page. Manually log in yourself in that browser window.
Your session will be saved in browser_data/linkedin_profile/ and persist
for future runs. The automation should detect it's not logged in and pause,
giving you time to log in.

Add this check to the start of apply_to_job():
```python
# Check if logged in
if await page.query_selector('input[name="session_key"]'):
    self.logger.warning("Not logged in — please log in manually in the browser window")
    input("Press Enter after logging in...")
```

**Option B:** Copy your li_at cookie into the persistent browser context
programmatically during setup_browser(). Less reliable.

---

## After This Session
- Mark Session 6 complete in PROJECT_BRIEF.md
- The full pipeline is now complete!
- Next step: Opus review session — security and session handling audit
  before running at scale
- Celebrate — you built a full AI-powered job application automation system