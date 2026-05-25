"""
run.py — Full pipeline runner
Usage:
  python run.py scrape       # scrape new jobs only
  python run.py score        # score unscored jobs only
  python run.py tailor       # tailor + render resumes for approved jobs
  python run.py pipeline     # scrape → score (normal daily run)
  python run.py dashboard    # start local review dashboard on :8000
"""
import asyncio
import json
import sys

from rich.console import Console

console = Console()


async def run_pipeline() -> None:
    from scraper import LinkedInScraper
    from scorer import JobScorer
    import db

    console.rule("[bold blue]Step 1: Scraping LinkedIn[/bold blue]")
    scraper = LinkedInScraper()
    new_jobs = await scraper.scrape()
    console.print(f"[green]✓ Found {len(new_jobs)} new jobs[/green]")

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
    elif command == "pipeline":
        asyncio.run(run_pipeline())
    elif command == "dashboard":
        import uvicorn
        import db as _db
        _db.init_db()
        uvicorn.run("dashboard.app:app", host="127.0.0.1", port=8000, reload=True)
    else:
        console.print(f"[red]Unknown command: {command}[/red]")
        console.print("Usage: python run.py [scrape|score|tailor|pipeline]")
