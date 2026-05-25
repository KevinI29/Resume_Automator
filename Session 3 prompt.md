# Session 3 — Job Scorer (Haiku AI Scoring)

> Paste this immediately after PROJECT_BRIEF.md in Claude Code.

---

## Session 3 Goal
Build `scorer.py` — uses Claude Haiku to score each job in the database against
my resume and preferences. By the end of this session:
- Reads all unscored jobs (fit_score IS NULL) from the database
- Sends each job's description + my master resume to Haiku
- Gets back a 1-10 fit score + a short reason
- Updates the database with the score
- Has a CLI entry point to run manually
- Cost per run stays under $0.05

---

## How the Scoring Works

For each unscored job, we send Haiku a prompt containing:
1. My resume summary + skills (NOT the full resume — keep tokens low)
2. The job title + company + full description
3. Ask for a JSON response with score and reason

Haiku responds with structured JSON:
```json
{
  "score": 7,
  "reason": "Strong match on Python and FastAPI. Role requires AWS experience which candidate has. Missing: Kubernetes (listed as preferred not required).",
  "dealbreakers": []
}
```

We parse this, update the job in the database, and move on.

---

## Task List for This Session

### 1. scorer.py — Main scorer class

Build a `JobScorer` class with these methods:

**`__init__(self)`**
- Initialize Anthropic client: `anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)`
- Load master resume from `resume/master.json`
- Extract a compact "profile summary" string from the resume for use in prompts
  (summary + skills only — not full experience, to save tokens)
- Set up logger

**`_build_profile_summary(self) -> str`**
Builds a compact string from master.json for use in every prompt:
```
Name: [name]
Summary: [summary field]
Skills: Python, FastAPI, React, PostgreSQL, Docker, AWS (from skills section)
Years of experience: [calculate from earliest job start date]
```
Keep this under 200 tokens. Do not include full bullet points.

**`_build_scoring_prompt(self, job: Job) -> str`**
Build the prompt sent to Haiku for each job. Structure:
```
You are a job fit evaluator. Score how well this candidate matches this job.

CANDIDATE PROFILE:
{profile_summary}

JOB TO EVALUATE:
Title: {job.title}
Company: {job.company}
Location: {job.location}
Description: {job.description[:3000]}  # cap at 3000 chars to control tokens

SCORING RULES:
- Score 1-10 where 10 = perfect match, 1 = completely wrong fit
- Score 8-10: Meets 90%+ of requirements, strong keyword overlap
- Score 6-7: Meets 70%+ of requirements, some gaps but hirable
- Score 4-5: Meets 50%, significant gaps
- Score 1-3: Wrong domain, too senior/junior, or missing critical requirements

DEALBREAKER RULES (auto score 2 or below):
- Requires 5+ more years of experience than candidate has
- Completely different tech stack with no overlap
- Requires specific domain expertise candidate doesn't have (e.g. ML when candidate is backend)

Respond ONLY with valid JSON, no other text:
{
  "score": <integer 1-10>,
  "reason": "<2-3 sentences explaining the score, mention specific matches and gaps>",
  "dealbreakers": ["<list any dealbreakers found, empty array if none>"]
}
```

**`async score_job(self, job: Job) -> tuple[int, str]`**
- Calls Haiku with the scoring prompt
- Model to use: `"claude-haiku-4-5"`
- max_tokens: 200 (score responses are short)
- Parses the JSON response
- Returns (score, reason) tuple
- On any parse error: returns (0, "scoring failed") — never crash the whole run

**`async score_all_unscored(self) -> int`**
- The main entry point
- Fetches all jobs with fit_score IS NULL from db
- For each job:
  - Calls score_job()
  - Updates db with score + reason
  - Adds random delay between calls (use random_delay from utils.py)
  - Logs the result: "Scored: [title] at [company] → [score]/10"
- Returns count of jobs scored
- If 0 unscored jobs found, logs "All jobs already scored" and returns 0

**`async score_single(self, job_id: int) -> tuple[int, str]`**
- Scores a single job by ID (useful for re-scoring or testing)
- Fetches job from db, calls score_job(), updates db
- Returns (score, reason)

### 2. Update db.py
Add these new database helper functions:

```python
def get_unscored_jobs() -> list[Job]:
    """Returns jobs where fit_score IS NULL and status = 'new'"""

def update_job_score(job_id: int, score: int, reason: str):
    """Updates fit_score and fit_reason for a job"""

def get_jobs_above_threshold(min_score: int = MIN_FIT_SCORE) -> list[Job]:
    """Returns all jobs scoring at or above the threshold, ordered by score desc"""
```

### 3. CLI entry point
```python
if __name__ == "__main__":
    import asyncio
    from rich.console import Console
    from rich.table import Table
    from rich import box

    async def main():
        scorer = JobScorer()
        console = Console()

        console.print("[bold green]Starting job scorer...[/bold green]")
        count = await scorer.score_all_unscored()
        console.print(f"[bold]Scored {count} jobs.[/bold]")

        # Show all scored jobs above threshold
        jobs = db.get_jobs_above_threshold()
        if not jobs:
            console.print("[yellow]No jobs above score threshold yet.[/yellow]")
            return

        table = Table(
            title=f"Jobs above threshold (score ≥ {MIN_FIT_SCORE})",
            box=box.ROUNDED
        )
        table.add_column("Score", style="bold green", width=6)
        table.add_column("Title", style="cyan")
        table.add_column("Company", style="magenta")
        table.add_column("Easy Apply", width=10)
        table.add_column("Reason")

        for job in jobs:
            table.add_row(
                f"{job.fit_score}/10",
                job.title,
                job.company,
                "✓" if job.is_easy_apply else "✗",
                job.fit_reason[:80] + "..." if job.fit_reason and len(job.fit_reason) > 80 else (job.fit_reason or "")
            )

        console.print(table)

    asyncio.run(main())
```

### 4. Add a combined runner: run.py
Create `run.py` at the project root — this becomes the single command to run
the full pipeline (scrape → score):

```python
"""
run.py — Full pipeline runner
Usage:
  python run.py scrape       # scrape new jobs only
  python run.py score        # score unscored jobs only
  python run.py pipeline     # scrape then score (the normal daily run)
"""
import asyncio
import sys
from rich.console import Console

console = Console()

async def run_pipeline():
    from scraper import LinkedInScraper
    from scorer import JobScorer
    import db

    # Step 1: Scrape
    console.rule("[bold blue]Step 1: Scraping LinkedIn[/bold blue]")
    scraper = LinkedInScraper()
    new_jobs = await scraper.scrape()
    console.print(f"[green]✓ Found {len(new_jobs)} new jobs[/green]")

    # Step 2: Score
    console.rule("[bold blue]Step 2: Scoring Jobs[/bold blue]")
    scorer = JobScorer()
    count = await scorer.score_all_unscored()
    console.print(f"[green]✓ Scored {count} jobs[/green]")

    # Summary
    console.rule("[bold blue]Summary[/bold blue]")
    good_jobs = db.get_jobs_above_threshold()
    console.print(f"[bold]{len(good_jobs)} jobs ready for your review in the dashboard.[/bold]")

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "pipeline"

    if command == "scrape":
        from scraper import LinkedInScraper
        asyncio.run(LinkedInScraper().scrape())
    elif command == "score":
        from scorer import JobScorer
        asyncio.run(JobScorer().score_all_unscored())
    elif command == "pipeline":
        asyncio.run(run_pipeline())
    else:
        console.print(f"[red]Unknown command: {command}[/red]")
        console.print("Usage: python run.py [scrape|score|pipeline]")
```

### 5. Logging
Every scorer run logs to `logs/scorer_YYYY-MM-DD.log`:
- Start time + number of unscored jobs found
- Each job scored: title, company, score, reason (first 100 chars)
- Total tokens used (from the Haiku response usage field)
- Estimated cost: `(input_tokens * 0.0000008) + (output_tokens * 0.000004)`
- Any errors with full traceback

---

## Error Handling Requirements
- **Invalid JSON from Haiku** — log the raw response, assign score=0, reason="parse error", continue
- **API rate limit** — wait 30 seconds, retry once
- **API auth error** — log "Invalid Anthropic API key", stop immediately
- **Job description empty** — skip scoring, assign score=0, reason="no description available"
- **Network error** — log and skip that job, continue with rest

---

## Token Budget (keep costs low)
- Profile summary: ~150 tokens
- Job description: capped at 3000 characters (~750 tokens)
- Prompt overhead: ~200 tokens
- Total input per call: ~1100 tokens
- Output per call: ~150 tokens
- **Cost per job: ~$0.0009 (Haiku pricing)**
- **50 jobs scored: ~$0.045 total**

---

## Testing the Scorer
After building, test with:
```bash
# Score all unscored jobs in the DB
python scorer.py

# Or run the full pipeline
python run.py pipeline
```

To verify scores were saved:
```bash
python -c "
import db
from config import MIN_FIT_SCORE
jobs = db.get_jobs_above_threshold(MIN_FIT_SCORE)
for j in jobs:
    print(f'{j.fit_score}/10 | {j.title} | {j.company}')
    print(f'  → {j.fit_reason}')
    print()
"
```

Expected output: a list of jobs with scores and reasons explaining why each
was rated that way. Tweak the scoring prompt if the scores feel off.

---

## Prompt Tuning (important step)
After the first test run, check 5-6 scored jobs manually:
- Does a score of 7 actually feel like a good match to you?
- Are dealbreakers being caught correctly?
- Is the reason text useful or vague?

If scores feel too high or too low, adjust the SCORING RULES section of the
prompt in `_build_scoring_prompt()`. This is expected — one round of tuning
is normal.

---

## After This Session
- Mark Session 3 complete in PROJECT_BRIEF.md
- Note the actual cost of the test run (check logs for token usage)
- Note any prompt tuning changes made
- Session 4 will build the resume tailor — takes the approved jobs and
  rewrites the resume JSON, then renders to PDF