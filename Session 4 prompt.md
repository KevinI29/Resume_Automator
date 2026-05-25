# Session 4 — Resume Tailor + PDF Renderer

> Paste this immediately after PROJECT_BRIEF.md in Claude Code.

---

## Session 4 Goal
Build `tailor.py` and `renderer.py` — takes an approved job from the database,
rewrites the resume JSON to match the job description using Claude, then renders
it to a professional PDF. By the end of this session:
- `tailor.py` reads master.json + job description → outputs tailored resume JSON
- `renderer.py` takes tailored JSON → renders to PDF via HTML template + WeasyPrint
- Each job gets its own tailored PDF saved to output/resumes/
- The tailored JSON is saved alongside the PDF for debugging
- A CLI entry point to test on a single job by ID
- Full round-trip test: job in DB → tailored PDF on disk

---

## How Tailoring Works

The master resume (resume/master.json) is the source of truth — we never modify it.
For each approved job we:
1. Send the job description + master resume to Claude
2. Claude rewrites ONLY the summary and experience bullets — nothing else
3. We get back a "tailored resume JSON" (same schema as master, different content)
4. We render the tailored JSON to PDF using an HTML template

Critical rule: Claude must NEVER invent experience. It only reframes existing
experience to emphasize relevance to the JD. The prompt enforces this hard.

---

## Task List for This Session

### 1. tailor.py — Resume tailoring via Claude

Build a `ResumeTailor` class with these methods:

**`__init__(self)`**
- Initialize Anthropic client
- Load master resume from config.RESUME_MASTER_PATH
- Set up logger

**`_build_tailoring_prompt(self, job: Job, master: dict) -> str`**
Build the prompt sent to Claude for tailoring:
```
You are an expert resume writer. Your job is to tailor a resume for a specific
job posting. You must follow these rules strictly:

RULES:
1. NEVER invent, fabricate, or add experience that doesn't exist in the original
2. Only reframe and emphasize existing experience to match the job requirements
3. Use keywords from the job description naturally — do not keyword-stuff
4. Keep bullet points achievement-focused and quantified where possible
5. The summary should be 2-3 sentences maximum
6. Return ONLY valid JSON — no markdown, no explanation, no preamble

JOB DETAILS:
Title: {job.title}
Company: {job.company}
Description: {job.description[:4000]}

ORIGINAL RESUME:
{json.dumps(master, indent=2)}

TASK:
Return a modified version of the resume JSON where you have:
1. Rewritten the "summary" field to target this specific role and company
2. Rewritten up to 3 experience bullet points per job to emphasize relevance
3. Reordered the "skills" lists to put the most relevant skills first
4. Left ALL other fields (personal info, education, job titles, dates) EXACTLY
   as they are in the original

Return the complete resume JSON with your modifications applied.
```

**`async tailor_resume(self, job: Job) -> dict`**
- Calls Claude with the tailoring prompt
- Model: use `"claude-haiku-4-5"` for speed and cost
- max_tokens: 4000 (resume JSON can be long)
- Strips any markdown fences from response before parsing
- Validates the returned JSON has the same top-level keys as master resume
- If validation fails: logs warning and returns the master resume unchanged
  (better to submit an unmodified resume than crash)
- Returns the tailored resume dict

**`async tailor_and_save(self, job: Job) -> str`**
- The main entry point for each job
- Calls tailor_resume()
- Saves the tailored JSON to: `output/resumes/{job_id}_resume.json`
- Returns the path to the saved JSON
- Logs: "Tailored resume for {title} at {company}"

### 2. renderer.py — HTML/CSS → PDF

Build a `ResumeRenderer` class:

**`__init__(self)`**
- Load the HTML template from config.RESUME_TEMPLATE_PATH
- Set up logger
- Create output directory if it doesn't exist

**`render(self, resume_data: dict, job_id: str) -> str`**
- Takes tailored resume dict + job_id
- Renders the HTML template with the resume data using Jinja2
- Converts rendered HTML to PDF using WeasyPrint
- Saves to: `output/resumes/{job_id}_resume.pdf`
- Returns the path to the PDF
- Logs: "Rendered PDF: {path}"

**`render_from_file(self, json_path: str) -> str`**
- Convenience method: loads JSON from path, renders to PDF
- Useful for re-rendering without re-tailoring

### 3. resume/template.html — The PDF template

Create a clean, professional single-page resume template. Requirements:
- Clean modern design — not over-designed, ATS-friendly
- Single column layout (better for ATS parsers)
- Sections: Personal Info/Header, Summary, Experience, Skills, Education, Projects
- Use CSS variables for colors so it's easy to customize
- Must render well in WeasyPrint (avoid complex CSS like flexbox gaps, use
  padding/margin instead; avoid CSS Grid — WeasyPrint support is limited)
- Font: use a web-safe font (Arial or Helvetica) or embed a Google Font via
  @import in the CSS
- Page size: A4
- Margins: 15mm on all sides
- Font size: 10-11pt for body, 14-16pt for name, 11-12pt for section headers

Template variables (Jinja2 syntax):
```html
{{ resume.personal.name }}
{{ resume.personal.email }}
{{ resume.personal.phone }}
{{ resume.personal.location }}
{{ resume.personal.linkedin }}
{{ resume.personal.github }}
{{ resume.summary }}
{% for job in resume.experience %}
  {{ job.title }}, {{ job.company }}
  {{ job.start }} – {{ job.end }}
  {% for bullet in job.bullets %}{{ bullet }}{% endfor %}
{% endfor %}
{{ resume.skills.languages | join(', ') }}
{{ resume.skills.frameworks | join(', ') }}
{{ resume.skills.tools | join(', ') }}
{% for edu in resume.education %}
  {{ edu.degree }}, {{ edu.institution }}, {{ edu.year }}
{% endfor %}
{% for project in resume.projects %}
  {{ project.name }}: {{ project.description }}
  Tech: {{ project.tech | join(', ') }}
{% endfor %}
```

### 4. Update db.py
Add these helpers:

```python
def update_job_resume_path(job_id: int, resume_path: str):
    """Saves the path of the generated resume PDF for a job"""

def get_approved_jobs_without_resume() -> list[Job]:
    """Returns approved jobs that don't have a resume generated yet"""
```

Also add `resume_path TEXT` column to the jobs table in `init_db()` if it
doesn't already exist. Use `ALTER TABLE` with a try/except to add it safely
to existing databases:
```python
try:
    cursor.execute("ALTER TABLE jobs ADD COLUMN resume_path TEXT")
except:
    pass  # column already exists
```

### 5. models.py update
Add `resume_path: str | None = None` field to the Job model.

### 6. pipeline.py update — add tailoring step
Update `run.py` to add a third step after scoring:

```python
async def run_pipeline():
    # Step 1: Scrape (existing)
    # Step 2: Score (existing)
    # Step 3: Tailor resumes for approved jobs
    console.rule("[bold blue]Step 3: Tailoring Resumes[/bold blue]")
    from tailor import ResumeTailor
    from renderer import ResumeRenderer

    tailor = ResumeTailor()
    renderer = ResumeRenderer()

    approved_jobs = db.get_approved_jobs_without_resume()
    if not approved_jobs:
        console.print("[yellow]No approved jobs waiting for resumes.[/yellow]")
        console.print("[dim]Tip: approve jobs in the dashboard first.[/dim]")
    else:
        for job in approved_jobs:
            json_path = await tailor.tailor_and_save(job)
            resume_data = json.load(open(json_path))
            pdf_path = renderer.render(resume_data, job.linkedin_job_id)
            db.update_job_resume_path(job.id, pdf_path)
            console.print(f"[green]✓[/green] {job.title} @ {job.company} → {pdf_path}")
```

### 7. CLI entry point in tailor.py
```python
if __name__ == "__main__":
    import asyncio, sys
    from rich.console import Console

    async def main():
        console = Console()
        if len(sys.argv) < 2:
            console.print("[red]Usage: python tailor.py <job_id>[/red]")
            console.print("Example: python tailor.py 1")
            return

        job_id = int(sys.argv[1])
        jobs = db.get_all_jobs()
        job = next((j for j in jobs if j.id == job_id), None)

        if not job:
            console.print(f"[red]Job ID {job_id} not found in database[/red]")
            return

        console.print(f"[bold]Tailoring resume for:[/bold] {job.title} @ {job.company}")
        console.print(f"[dim]Fit score: {job.fit_score}/10[/dim]")

        tailor = ResumeTailor()
        renderer = ResumeRenderer()

        json_path = await tailor.tailor_and_save(job)
        console.print(f"[green]✓ Tailored JSON saved:[/green] {json_path}")

        import json as json_lib
        resume_data = json_lib.load(open(json_path))
        pdf_path = renderer.render(resume_data, job.linkedin_job_id)
        console.print(f"[green]✓ PDF rendered:[/green] {pdf_path}")
        console.print(f"\n[bold green]Done! Open the PDF to review:[/bold green] {pdf_path}")

    asyncio.run(main())
```

---

## WeasyPrint Installation Note
WeasyPrint requires system dependencies. If installation fails, run:
```bash
# On Ubuntu/Debian
sudo apt-get install python3-cffi python3-brotli libpango-1.0-0 libpangoft2-1.0-0

# On Windows (via pip)
pip install weasyprint
# If it fails on Windows, use the alternative: pdfkit + wkhtmltopdf
```

If WeasyPrint is problematic on the current system, fall back to `pdfkit`:
```python
import pdfkit
pdfkit.from_string(html_content, output_path)
```

---

## Testing This Session

**Step 1 — Check which job IDs are in the DB:**
```bash
python -c "
import db
jobs = [j for j in db.get_all_jobs() if j.fit_score and j.fit_score >= 6]
for j in jobs[:5]:
    print(f'ID: {j.id} | {j.fit_score}/10 | {j.title} @ {j.company}')
"
```

**Step 2 — Test tailoring on one job:**
```bash
python tailor.py <job_id>
```

**Step 3 — Open the PDF and check:**
- Does it look like a professional resume?
- Is all the data from master.json present?
- Are the bullet points actually rewritten for the job?
- Does it fit on one page?

**Step 4 — Check output directory:**
```bash
ls output/resumes/
# Should see: {job_id}_resume.json and {job_id}_resume.pdf
```

---

## Quality Checklist for the PDF
Before calling this session done, verify the PDF:
- [ ] Name and contact info in the header
- [ ] Summary is 2-3 sentences and mentions the target role
- [ ] Experience bullets are rewritten (not identical to master.json)
- [ ] Skills section shows relevant skills first
- [ ] No placeholder text visible
- [ ] Fits on one page (or two at most)
- [ ] Looks professional enough to submit

---

## After This Session
- Mark Session 4 complete in PROJECT_BRIEF.md
- Note which PDF renderer worked (WeasyPrint or pdfkit)
- Note any template adjustments needed
- Session 5 builds the dashboard where you approve jobs and trigger tailoring