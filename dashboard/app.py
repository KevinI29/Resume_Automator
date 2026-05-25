import asyncio
import json
import os
from datetime import datetime
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

import config
import db

app = FastAPI(title="Job Auto Dashboard")
templates = Jinja2Templates(directory="dashboard/templates")


@app.on_event("startup")
async def startup():
    db.reset_stuck_tailoring_jobs()

_pipeline_status: dict = {
    "running": False,
    "last_run": None,
    "last_run_new_jobs": 0,
    "last_run_scored": 0,
}


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Jobs API ──────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
async def get_jobs(status: Optional[str] = None):
    if status == "new":
        # "New" means actionable — all jobs above threshold regardless of status
        jobs = db.get_jobs_above_threshold()
    elif status:
        jobs = db.get_jobs_by_status(status)
    else:
        # "All" means truly all — no score filter
        jobs = db.get_all_jobs()
    jobs.sort(key=lambda j: j.fit_score or 0, reverse=True)
    return [j.model_dump() for j in jobs]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int):
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.model_dump()


@app.get("/api/stats")
async def get_stats():
    return db.get_stats()


@app.post("/api/jobs/{job_id}/approve")
async def approve_job(job_id: int, background_tasks: BackgroundTasks):
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    db.update_job_status(job_id, "approved")
    db.update_job_resume_path(job_id, None)  # clear any prior failed sentinel
    background_tasks.add_task(_tailor_background, job_id)
    return db.get_job_by_id(job_id).model_dump()


@app.post("/api/jobs/{job_id}/skip")
async def skip_job(job_id: int):
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    db.update_job_status(job_id, "skipped")
    return db.get_job_by_id(job_id).model_dump()


@app.post("/api/jobs/{job_id}/unapprove")
async def unapprove_job(job_id: int):
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    db.update_job_status(job_id, "new")
    db.update_job_resume_path(job_id, None)
    return db.get_job_by_id(job_id).model_dump()


@app.get("/api/jobs/{job_id}/resume")
async def download_resume(job_id: int):
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.resume_path or not os.path.exists(job.resume_path):
        raise HTTPException(status_code=404, detail="Resume not generated yet")
    filename = f"{job.company}_{job.title}_resume.pdf".replace(" ", "_")
    return FileResponse(job.resume_path, media_type="application/pdf", filename=filename)


# ── Pipeline API ──────────────────────────────────────────────────────────────

@app.post("/api/pipeline/run")
async def run_pipeline(background_tasks: BackgroundTasks):
    if _pipeline_status["running"]:
        return {"status": "already_running"}
    background_tasks.add_task(_pipeline_background)
    return {"status": "started"}


@app.get("/api/pipeline/status")
async def pipeline_status():
    return _pipeline_status


# ── Background tasks ──────────────────────────────────────────────────────────

async def _tailor_background(job_id: int) -> None:
    import logging
    log = logging.getLogger("dashboard.tailor")
    try:
        from tailor import ResumeTailor
        from renderer import ResumeRenderer

        job = db.get_job_by_id(job_id)
        if not job:
            return
        tailor = ResumeTailor()
        renderer = ResumeRenderer()
        json_path = await tailor.tailor_and_save(job)
        with open(json_path, encoding="utf-8") as f:
            resume_data = json.load(f)
        pdf_path = renderer.render(resume_data, job.linkedin_job_id)
        db.update_job_resume_path(job_id, pdf_path)
        log.info(f"Tailoring complete for job {job_id}: {pdf_path}")
    except Exception as exc:
        log.error(f"Tailoring failed for job {job_id}: {exc}", exc_info=True)
        db.update_job_status(job_id, "approved")
        db.update_job_resume_path(job_id, "failed")


async def _pipeline_background() -> None:
    import logging
    log = logging.getLogger("dashboard.pipeline")
    _pipeline_status["running"] = True
    _pipeline_status["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        from scraper import LinkedInScraper
        from scorer import JobScorer

        scraper = LinkedInScraper()
        new_jobs = await scraper.scrape()
        _pipeline_status["last_run_new_jobs"] = len(new_jobs)

        scorer = JobScorer()
        scored = await scorer.score_all_unscored()
        _pipeline_status["last_run_scored"] = scored
        log.info(f"Pipeline done — {len(new_jobs)} new, {scored} scored")
    except Exception as exc:
        log.error(f"Pipeline failed: {exc}")
    finally:
        _pipeline_status["running"] = False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    db.init_db()
    uvicorn.run("dashboard.app:app", host="127.0.0.1", port=8000, reload=True)
