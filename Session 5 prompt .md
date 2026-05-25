# Session 5 — Dashboard (FastAPI + Frontend)

> Paste this immediately after PROJECT_BRIEF.md in Claude Code.

---

## Session 5 Goal
Build the dashboard — a local web interface where I review scored jobs,
approve or skip them, trigger resume tailoring, and download generated PDFs.
This is the human-in-the-loop layer that sits between scoring and applying.

By the end of this session:
- FastAPI backend serving job data from SQLite
- Clean, functional frontend (single HTML file with vanilla JS)
- Can view all jobs scored 6+ with their fit scores and reasons
- Can approve or skip individual jobs
- Can trigger resume tailoring for approved jobs
- Can download generated PDFs
- Runs locally with: `python dashboard/app.py`

---

## Dashboard Layout

### Left sidebar
- App title: "Job Auto"
- Stats: Total jobs / Approved / Applied / Skipped
- Filter buttons: All | New | Approved | Applied | Skipped
- Run pipeline button (triggers scrape → score)

### Main content area — Job cards
Each job card shows:
- Fit score badge (color coded: green 8-10, yellow 6-7)
- Job title + Company name (bold)
- Location + Easy Apply badge (if applicable)
- First 150 chars of fit_reason
- Date scraped
- Action buttons:
  - "Approve" (green) — sets status to approved, triggers tailoring
  - "Skip" (red) — sets status to skipped
  - "View Job" (grey) — opens LinkedIn URL in new tab
  - "Download Resume" (blue) — only shows if resume_path exists

### Job detail modal (click on card)
Shows full job description, full fit reason, and tailoring status.

---

## Task List for This Session

### 1. dashboard/app.py — FastAPI backend

```python
from fastapi import FastAPI, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn

app = FastAPI(title="Job Auto Dashboard")
```

**Endpoints to build:**

`GET /` — serves the dashboard HTML page

`GET /api/jobs` — returns all jobs as JSON
- Query params: `?status=new` (optional filter)
- Returns list of job objects with all fields
- Ordered by fit_score DESC

`GET /api/stats` — returns dashboard stats
```json
{
  "total": 91,
  "new": 25,
  "approved": 3,
  "applied": 1,
  "skipped": 10,
  "above_threshold": 25
}
```

`POST /api/jobs/{job_id}/approve` — approves a job
- Sets job status to 'approved'
- Triggers resume tailoring as a background task
- Returns updated job object

`POST /api/jobs/{job_id}/skip` — skips a job
- Sets job status to 'skipped'
- Returns updated job object

`POST /api/jobs/{job_id}/unapprove` — resets approved back to new
- Useful if you accidentally approved wrong job
- Returns updated job object

`GET /api/jobs/{job_id}/resume` — downloads the PDF
- Returns FileResponse with the PDF
- Returns 404 if resume not generated yet

`POST /api/pipeline/run` — triggers full pipeline
- Runs scraper + scorer as background task
- Returns immediately with: {"status": "started"}
- (Don't await — just fire and forget)

`GET /api/pipeline/status` — returns pipeline status
```json
{
  "running": false,
  "last_run": "2026-05-17 23:42:55",
  "last_run_new_jobs": 50,
  "last_run_scored": 91
}
```

Store pipeline status in a simple module-level dict — no need for a database.

### 2. Background task for tailoring
When a job is approved, trigger tailoring in the background:
```python
async def tailor_job_background(job_id: int):
    job = db.get_job_by_id(job_id)
    tailor = ResumeTailor()
    renderer = ResumeRenderer()
    json_path = await tailor.tailor_and_save(job)
    import json
    resume_data = json.load(open(json_path))
    pdf_path = renderer.render(resume_data, job.linkedin_job_id)
    db.update_job_resume_path(job.id, pdf_path)
    db.update_job_status(job.id, 'approved')
```

### 3. Update db.py
Add:
```python
def get_job_by_id(job_id: int) -> Job | None:
    """Fetch a single job by its DB id"""

def get_stats() -> dict:
    """Returns counts by status for the dashboard stats"""
```

### 4. dashboard/templates/index.html — The frontend

Build a single-page dashboard in vanilla HTML + CSS + JavaScript.
No frameworks, no build step — must work by opening the file directly.

**Design direction: Clean dark utility dashboard**
- Dark background: #0f1117
- Card background: #1a1d27
- Accent: #6366f1 (indigo)
- Success: #22c55e (green)
- Warning: #eab308 (yellow)
- Danger: #ef4444 (red)
- Font: 'IBM Plex Mono' from Google Fonts for headers,
         'Inter' for body text (import both via @import)
- Score badges: pill shaped, color coded by score range
- Cards: subtle border, hover lifts with box-shadow transition
- Approve button turns green with checkmark on success
- Skip button turns red with X on success
- Smooth fade-in animation for cards on page load

**JavaScript functionality:**
- On load: fetch /api/stats and /api/jobs?status=new
- Filter buttons update the displayed jobs without page reload
- Approve/Skip buttons call their respective API endpoints
- After approve: card updates to show "Tailoring..." then
  polls /api/jobs/{id} every 3s until resume_path is set,
  then shows Download Resume button
- Run Pipeline button shows a loading spinner while running,
  polls /api/pipeline/status every 5s until done
- Job cards are clickable — shows a modal with full description
- All API calls have error handling with a toast notification

**Score badge colors:**
- 9-10: bright green (#22c55e) with white text
- 7-8: indigo (#6366f1) with white text
- 6: yellow (#eab308) with dark text

**Easy Apply badge:**
- Small green pill "Easy Apply" shown on cards where is_easy_apply=True

### 5. Run configuration
At the bottom of dashboard/app.py:
```python
if __name__ == "__main__":
    import db
    db.init_db()
    uvicorn.run(
        "dashboard.app:app",
        host="127.0.0.1",
        port=8000,
        reload=True
    )
```

Add to run.py:
```
python run.py dashboard   # starts the dashboard server
```

---

## CORS and Static Files
Since the HTML is served by FastAPI itself (not opened as a file),
no CORS issues. Serve the HTML template directly:

```python
from fastapi.templating import Jinja2Templates
templates = Jinja2Templates(directory="dashboard/templates")

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
```

---

## Testing This Session

**Step 1 — Start the dashboard:**
```bash
python dashboard/app.py
```
Open http://127.0.0.1:8000 in browser.

**Step 2 — Verify these work:**
- [ ] Jobs load and display with correct scores
- [ ] Filter buttons (All/New/Approved/Skipped) work
- [ ] Stats bar shows correct counts
- [ ] Approve button triggers tailoring (check logs)
- [ ] Download Resume button appears after tailoring completes
- [ ] Skip button updates card status
- [ ] View Job opens correct LinkedIn URL
- [ ] Job card click shows modal with full description

**Step 3 — Approve one job end to end:**
- Click Approve on any job scoring 7+
- Wait for "Tailoring..." to resolve
- Click Download Resume
- Verify PDF opens correctly

---

## After This Session
- Mark Session 5 complete in PROJECT_BRIEF.md
- Note any UI issues to polish later
- Session 6 builds Easy Apply automation (Playwright)
- That's the final module — after Session 6 the full pipeline is complete