import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

import config
import db
from models import MasterResume

logger = logging.getLogger("dashboard")

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

_running_applies: dict[int, int] = {}  # job_id → subprocess PID

_validation_state: dict = {
    "running": False,
    "last_run": None,       # ISO timestamp string, or None
    "log_file": None,       # path to the most recent log file
}

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Jobs API ──────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
async def get_jobs(
    status: str = Query(default="", description="Filter by status. Empty = all statuses."),
    sort: str = Query(default="score_desc", description="score_desc | score_asc | date_desc | date_asc | company_asc"),
    search: str = Query(default="", description="Text search across title, company, description"),
    easy_apply: bool = Query(default=False, description="If true, only return is_easy_apply=True jobs"),
    min_score: int = Query(default=0, description="Only return jobs with fit_score >= this value. 0 = no filter."),
):
    sort_map = {
        "score_desc":  ("score",   "desc"),
        "score_asc":   ("score",   "asc"),
        "date_desc":   ("date",    "desc"),
        "date_asc":    ("date",    "asc"),
        "company_asc": ("company", "asc"),
    }
    sort_by, order = sort_map.get(sort, ("score", "desc"))

    jobs = db.get_all_jobs_sorted(
        sort_by=sort_by,
        order=order,
        search=search,
        status=status,
    )

    if easy_apply:
        jobs = [j for j in jobs if j.is_easy_apply]

    if min_score > 0:
        jobs = [j for j in jobs if j.fit_score is not None and j.fit_score >= min_score]

    return [
        {**j.model_dump(), "is_likely_expired": j.is_likely_expired}
        for j in jobs
    ]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int):
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.model_dump()


@app.get("/api/stats")
async def get_stats():
    return db.get_stats()


@app.post("/api/jobs/unskip-all")
async def unskip_all_jobs():
    """Move all skipped jobs back to 'new' status."""
    count = db.unskip_all_jobs()
    return {"status": "ok", "restored": count}


@app.post("/api/jobs/purge")
async def purge_dead_jobs():
    """
    Permanently delete all jobs with status 'closed' or 'failed',
    their application records, and any generated resume files on disk.
    """
    count, resume_paths = db.purge_dead_jobs()

    files_cleaned = 0
    for path in resume_paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
                files_cleaned += 1
            if path and path.endswith("_resume.pdf"):
                json_path = path.replace("_resume.pdf", "_resume.json")
                if os.path.exists(json_path):
                    os.remove(json_path)
                    files_cleaned += 1
        except OSError as e:
            logger.warning(f"Could not delete file {path}: {e}")

    return {
        "status": "ok",
        "jobs_deleted": count,
        "files_cleaned": files_cleaned,
    }


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


@app.post("/api/jobs/{job_id}/unskip")
async def unskip_job(job_id: int):
    """Move a single skipped job back to 'new' status."""
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "skipped":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot unskip: job status is '{job.status}', expected 'skipped'"
        )
    db.unskip_job(job_id)
    updated = db.get_job_by_id(job_id)
    return {**updated.model_dump(), "is_likely_expired": updated.is_likely_expired}


@app.get("/api/jobs/{job_id}/resume")
async def download_resume(job_id: int):
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.resume_path or not os.path.exists(job.resume_path):
        raise HTTPException(status_code=404, detail="Resume not generated yet")
    filename = f"{job.company}_{job.title}_resume.pdf".replace(" ", "_")
    return FileResponse(job.resume_path, media_type="application/pdf", filename=filename)


# ── Apply API ─────────────────────────────────────────────────────────────────

@app.post("/api/jobs/{job_id}/apply")
async def apply_job(job_id: int):
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "approved" or not job.resume_path or job.resume_path == "failed":
        raise HTTPException(
            status_code=400,
            detail="Job not ready — must be approved with a resume",
        )
    log_dir = _PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"apply_{job_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            [sys.executable, str(_PROJECT_ROOT / "applicant.py"), "--single", str(job_id)],
            cwd=str(_PROJECT_ROOT),
            stdout=lf,
            stderr=lf,
        )
    _running_applies[job_id] = proc.pid
    return {"status": "applying", "pid": proc.pid}


@app.get("/api/jobs/{job_id}/apply-status")
async def apply_status(job_id: int):
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    pid = _running_applies.get(job_id)
    is_running = False
    if pid:
        try:
            os.kill(pid, 0)  # signal 0 = existence check only
            is_running = True
        except (OSError, ProcessLookupError):
            is_running = False
        if not is_running:
            _running_applies.pop(job_id, None)

    return {
        "status": job.status,
        "applied": job.status == "applied",
        "running": is_running,
    }


@app.post("/api/apply-all")
async def apply_all():
    jobs = db.get_approved_jobs_with_resume()
    if not jobs:
        return {"status": "no_jobs", "count": 0}
    log_dir = _PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"apply_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            [sys.executable, str(_PROJECT_ROOT / "applicant.py")],
            cwd=str(_PROJECT_ROOT),
            stdout=lf,
            stderr=lf,
        )
    for j in jobs:
        _running_applies[j.id] = proc.pid
    return {"status": "started", "count": len(jobs)}


# ── Validation API ───────────────────────────────────────────────────────────

@app.post("/api/validate")
async def trigger_validation():
    """
    Spawn validator.py as a subprocess to validate all unvalidated jobs.
    Returns immediately — the browser runs independently.
    Use GET /api/validate/status to check if it's still running.

    NOTE: Playwright cannot run inside FastAPI's asyncio loop on Windows.
    Subprocess is the only correct pattern here.
    """
    if _validation_state.get("running"):
        return {"status": "already_running", "log_file": _validation_state.get("log_file")}

    log_dir = _PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"validate_{timestamp}.log"

    with open(log_file, "w") as lf:
        subprocess.Popen(
            [sys.executable, str(_PROJECT_ROOT / "validator.py")],
            cwd=str(_PROJECT_ROOT),
            stdout=lf,
            stderr=lf,
        )

    _validation_state["running"] = True
    _validation_state["last_run"] = datetime.now().isoformat()
    _validation_state["log_file"] = str(log_file)

    return {
        "status": "started",
        "log_file": str(log_file),
    }


@app.post("/api/jobs/{job_id}/validate")
async def validate_single_job(job_id: int):
    """
    Validate a single job by spawning validator.py --single <job_id>.
    Returns immediately. Poll the job via GET /api/jobs/{id} to see
    when validated_at and is_easy_apply have been updated.
    """
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    log_dir = _PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"validate_{job_id}_{timestamp}.log"

    with open(log_file, "w") as lf:
        subprocess.Popen(
            [
                sys.executable,
                str(_PROJECT_ROOT / "validator.py"),
                "--single",
                str(job_id),
            ],
            cwd=str(_PROJECT_ROOT),
            stdout=lf,
            stderr=lf,
        )

    return {
        "status": "started",
        "job_id": job_id,
        "log_file": str(log_file),
    }


@app.get("/api/validate/status")
async def get_validation_status():
    """
    Returns the current validation subprocess state.
    'running' resets to True when triggered and stays True until
    the dashboard is restarted (known limitation — acceptable for
    personal use). Check the log_file for live output.
    """
    return {
        "running": _validation_state.get("running", False),
        "last_run": _validation_state.get("last_run"),
        "log_file": _validation_state.get("log_file"),
    }


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


# ── Resume API ────────────────────────────────────────────────────────────────

@app.get("/api/resume")
async def get_resume():
    """Return the current master.json as a JSON response."""
    master_path = _PROJECT_ROOT / config.RESUME_MASTER_PATH
    try:
        with open(master_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="master.json not found")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"master.json is malformed JSON: {e}")


@app.put("/api/resume")
async def update_resume(resume_data: MasterResume):
    """
    Validate and overwrite master.json. Pydantic validation runs automatically
    before this function body executes — invalid payloads return 422.
    Creates a timestamped backup and prunes old ones.
    """
    backup_dir = _PROJECT_ROOT / config.RESUME_BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)

    master_path = _PROJECT_ROOT / config.RESUME_MASTER_PATH

    if master_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"master_{timestamp}.json"
        shutil.copy2(master_path, backup_path)
    else:
        backup_path = None

    backups = sorted(
        backup_dir.glob("master_*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    while len(backups) > config.MAX_RESUME_BACKUPS:
        oldest = backups.pop(0)
        oldest.unlink()
        logger.info(f"Pruned old resume backup: {oldest.name}")

    with open(master_path, "w", encoding="utf-8") as f:
        json.dump(resume_data.model_dump(), f, indent=2, ensure_ascii=False)

    return {
        "status": "ok",
        "backup": str(backup_path) if backup_path else None,
    }


@app.post("/api/resume/preview")
async def preview_resume():
    """
    Renders the current master.json to a temporary PDF and returns it inline.
    Uses job_id="preview" so the same temp file is always overwritten.
    """
    from renderer import ResumeRenderer

    master_path = _PROJECT_ROOT / config.RESUME_MASTER_PATH
    try:
        with open(master_path, "r", encoding="utf-8") as f:
            resume_data = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read master.json: {e}")

    try:
        renderer = ResumeRenderer()
        pdf_path = renderer.render(resume_data, job_id="preview")
        return FileResponse(
            path=pdf_path,
            media_type="application/pdf",
            filename="resume_preview.pdf",
            headers={"Content-Disposition": "inline; filename=resume_preview.pdf"},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF rendering failed: {e}")


@app.get("/resume", response_class=HTMLResponse)
async def resume_editor(request: Request):
    return templates.TemplateResponse("resume.html", {"request": request})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    assert (_PROJECT_ROOT / "applicant.py").exists(), \
        f"applicant.py not found in _PROJECT_ROOT={_PROJECT_ROOT}"
    db.init_db()
    uvicorn.run("dashboard.app:app", host="127.0.0.1", port=8000, reload=True)
