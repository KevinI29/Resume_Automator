"""
run.py — Full pipeline runner
Usage:
  python run.py scrape       # scrape new jobs only
  python run.py score        # score unscored jobs only
  python run.py tailor       # tailor + render resumes for approved jobs
  python run.py validate     # validate unvalidated jobs above threshold
  python run.py apply        # apply to all approved jobs with resumes
  python run.py maintain     # purge_old → validate → purge_dead
  python run.py pipeline     # maintain → scrape → score → tailor (normal daily run)
  python run.py dashboard    # start local review dashboard on :8000
"""
import asyncio
import json
import os
import random
import sys

from rich.console import Console

import config

console = Console()


async def run_maintain() -> None:
    """
    Canonical maintenance order (Session 11): purge_old → validate → purge_dead.
    Purge-old runs first so the validator doesn't spend Playwright time (and
    LinkedIn traffic) on jobs that are about to be deleted anyway. A failure
    or abort (CAPTCHA / logged-out) in the validate phase is logged and does
    NOT prevent purge_dead from running — the two purges are independent.
    """
    import db

    console.rule("[bold blue]Maintain: Purge Old[/bold blue]")
    old_result = db.purge_old_jobs()
    console.print(
        f"[green]✓ Removed {old_result['jobs_purged']} old jobs, "
        f"cleaned {old_result['files_deleted']} files[/green]"
    )

    console.rule("[bold blue]Maintain: Validate[/bold blue]")
    try:
        from validator import JobValidator, get_jobs_to_validate

        jobs = get_jobs_to_validate()
        if not jobs:
            console.print("[dim]No unvalidated jobs above threshold — skipping.[/dim]")
        else:
            validator = JobValidator()
            await validator.setup_browser()
            try:
                await validator.validate_batch(jobs)
            finally:
                await validator.close()
    except Exception as exc:
        console.print(f"[red]Validation step failed or aborted — continuing: {exc}[/red]")

    console.rule("[bold blue]Maintain: Purge Dead[/bold blue]")
    dead_count, dead_paths = db.purge_dead_jobs()
    files_cleaned = 0
    for path in dead_paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
                files_cleaned += 1
        except OSError:
            pass
    console.print(
        f"[green]✓ Removed {dead_count} closed/failed jobs, "
        f"cleaned {files_cleaned} files[/green]"
    )


async def run_pipeline() -> None:
    from scraper import LinkedInScraper
    from scorer import JobScorer
    import db

    console.rule("[bold magenta]Phase 1: Maintenance[/bold magenta]")
    await run_maintain()

    console.rule("[bold blue]Step 1: Scraping LinkedIn[/bold blue]")
    scraper = LinkedInScraper()
    enabled_searches = db.get_enabled_searches()
    if not enabled_searches:
        # Backwards-compatible fallback: no saved searches configured yet
        new_jobs = await scraper.scrape()
        console.print(f"[green]✓ Found {len(new_jobs)} new jobs[/green]")
    else:
        remaining_budget = config.MAX_JOBS_PER_SCRAPE
        total_new = 0
        for i, search in enumerate(enabled_searches):
            if remaining_budget <= 0:
                console.print(
                    f"[yellow]Global cap MAX_JOBS_PER_SCRAPE hit; "
                    f"skipping {len(enabled_searches) - i} remaining searches[/yellow]"
                )
                break
            cap = min(search.max_results, remaining_budget)
            console.rule(f"[bold]Search: {search.name} ({search.geo_label}) — cap {cap}[/bold]")
            new_jobs = await scraper.scrape(search=search, max_results_override=cap)
            remaining_budget -= len(new_jobs)
            total_new += len(new_jobs)
            if i < len(enabled_searches) - 1 and remaining_budget > 0:
                delay = random.uniform(*config.INTER_SEARCH_DELAY_SECONDS)
                console.print(f"[dim]Inter-search delay: {delay:.1f}s[/dim]")
                await asyncio.sleep(delay)
        console.print(
            f"[green]✓ Found {total_new} new jobs across {len(enabled_searches)} searches[/green]"
        )

    console.rule("[bold blue]Step 2: Scoring Jobs[/bold blue]")
    scorer = JobScorer()
    count = await scorer.score_all_unscored()
    console.print(f"[green]✓ Scored {count} jobs[/green]")

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
            with open(json_path, encoding="utf-8") as f:
                resume_data = json.load(f)
            pdf_path = renderer.render(resume_data, job.linkedin_job_id)
            db.update_job_resume_path(job.id, pdf_path)
            console.print(f"[green]✓[/green] {job.title} @ {job.company} → {pdf_path}")

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
    elif command == "tailor":
        import db, json
        from tailor import ResumeTailor
        from renderer import ResumeRenderer
        async def _tailor() -> None:
            tailor = ResumeTailor()
            renderer = ResumeRenderer()
            jobs = db.get_approved_jobs_without_resume()
            if not jobs:
                console.print("[yellow]No approved jobs without a resume.[/yellow]")
                return
            for job in jobs:
                json_path = await tailor.tailor_and_save(job)
                with open(json_path, encoding="utf-8") as f:
                    resume_data = json.load(f)
                pdf_path = renderer.render(resume_data, job.linkedin_job_id)
                db.update_job_resume_path(job.id, pdf_path)
                console.print(f"[green]✓[/green] {job.title} @ {job.company} → {pdf_path}")
        asyncio.run(_tailor())
    elif command == "apply":
        from applicant import LinkedInApplicant
        import db as _db
        async def _apply() -> None:
            _db.init_db()  # QAResolver reads qa_bank at construction time
            jobs = _db.get_approved_jobs_with_resume()
            if not jobs:
                console.print("[yellow]No approved jobs with resumes ready.[/yellow]")
                return
            console.print(f"[bold]Found {len(jobs)} job(s) ready to apply[/bold]")
            applicant = LinkedInApplicant()
            await applicant.setup_browser()
            results = await applicant.apply_batch(jobs)
            await applicant.close()
            console.print(f"[green]Applied: {results['applied']}[/green]")
            console.print(f"[yellow]Pending questions: {results['pending_questions']}[/yellow]")
            console.print(f"[red]Failed: {results['failed']}[/red]")
            console.print(f"[dim]Skipped: {results['skipped']}[/dim]")
        asyncio.run(_apply())
    elif command == "validate":
        from validator import _main as _validator_main
        asyncio.run(_validator_main())
    elif command == "maintain":
        import db as _db
        _db.init_db()
        asyncio.run(run_maintain())
    elif command == "pipeline":
        import db as _db
        _db.init_db()
        asyncio.run(run_pipeline())
    elif command == "dashboard":
        import uvicorn
        import db as _db
        _db.init_db()
        uvicorn.run("dashboard.app:app", host="127.0.0.1", port=8000, reload=True)
    else:
        console.print(f"[red]Unknown command: {command}[/red]")
        console.print("Usage: python run.py [scrape|score|tailor|validate|apply|maintain|pipeline|dashboard]")
