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
from pydantic import ValidationError

import config
import db
from models import MasterResume, SavedSearch

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
    return templates.TemplateResponse("index.html", {"request": request, "config": config})


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
        {**j.model_dump(), "is_likely_expired": j.is_likely_expired, "is_aging": j.is_aging}
        for j in jobs
    ]


@app.post("/api/jobs/purge-old")
async def purge_old_jobs_endpoint():
    """
    Delete jobs older than MAX_JOB_AGE_DAYS (excluding AGE_PURGE_PROTECTED_STATUSES)
    and clean up their tailored resume PDFs.
    Registered here — BEFORE /api/jobs/{job_id} — so "purge-old" is never
    matched as a job_id path parameter.
    """
    try:
        result = db.purge_old_jobs()
        logger.info(
            "Age-purge: removed %d jobs, %d resume files",
            result["jobs_purged"], result["files_deleted"]
        )
        return result
    except Exception as e:
        logger.exception("Age-purge failed")
        raise HTTPException(status_code=500, detail=str(e))


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
    return {**updated.model_dump(), "is_likely_expired": updated.is_likely_expired, "is_aging": updated.is_aging}


@app.get("/api/jobs/{job_id}/resume")
async def download_resume(job_id: int):
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.resume_path or not os.path.exists(job.resume_path):
        raise HTTPException(status_code=404, detail="Resume not generated yet")
    filename = f"{job.company}_{job.title}_resume.pdf".replace(" ", "_")
    return FileResponse(job.resume_path, media_type="application/pdf", filename=filename)


# ── Q&A resolver API ─────────────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}/questions")
async def get_job_questions(job_id: int):
    """Return unresolved pending questions for a job."""
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    questions = db.get_pending_questions_for_job(job_id)
    return [q.model_dump() for q in questions]


@app.post("/api/jobs/{job_id}/answers")
async def submit_job_answers(job_id: int, body: dict):
    """
    body: {"resolutions": [{"pending_id": int, "answer": str, "save_to_bank": bool}]}
    Resolves pending questions and sets job status back to 'approved'.
    """
    job = db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "pending_questions":
        raise HTTPException(
            status_code=400,
            detail=f"Job status is '{job.status}', expected 'pending_questions'"
        )
    resolutions = body.get("resolutions", [])
    if not resolutions:
        raise HTTPException(status_code=400, detail="No resolutions provided")
    db.resolve_pending_questions(job_id, resolutions)
    updated = db.get_job_by_id(job_id)
    return {**updated.model_dump(), "is_likely_expired": updated.is_likely_expired, "is_aging": updated.is_aging}


@app.get("/api/qa-bank")
async def get_qa_bank():
    """Return all qa_bank entries ordered by use_count desc."""
    entries = db.get_qa_bank_all()
    return [e.model_dump() for e in entries]


@app.put("/api/qa-bank/{qa_id}")
async def update_qa_bank_entry(qa_id: int, body: dict):
    """body: {"answer": str} — edit a saved answer."""
    answer = body.get("answer", "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer cannot be empty")
    db.update_qa_answer(qa_id, answer)
    return {"status": "ok", "qa_id": qa_id, "answer": answer}


@app.delete("/api/qa-bank/{qa_id}")
async def delete_qa_bank_entry(qa_id: int):
    """Delete a qa_bank entry. It will fall through to AI again next time."""
    db.delete_qa_answer(qa_id)
    return {"status": "ok", "qa_id": qa_id}


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


# ── Searches API (Session 12) ────────────────────────────────────────────────
# Route ordering is critical: fixed-path routes (discover, geos) MUST be
# registered before the parameterized /api/searches/{search_id} routes below,
# or FastAPI will try to parse "discover"/"geos" as an int search_id and 422 —
# the exact foot-gun Session 11 had to watch for with /api/jobs/purge-old.

@app.post("/api/searches/discover")
async def discover_search(body: dict, background_tasks: BackgroundTasks):
    """
    One-off search — params come from the request body. Nothing is persisted
    to the searches table; results land in `jobs` normally. Runs as a
    background task; the frontend polls GET /api/stats to see new jobs land.
    """
    geo_label = body.get("geo_label", "")
    geo_id = config.LINKEDIN_GEO_IDS.get(geo_label)
    if geo_id is None:
        raise HTTPException(status_code=400, detail=f"Unknown geo_label: {geo_label}")

    try:
        search = SavedSearch(
            id=None,
            name="Discover",
            titles=body.get("titles", []),
            geo_id=geo_id,
            geo_label=geo_label,
            remote_only=body.get("remote_only", False),
            easy_apply_only=body.get("easy_apply_only", True),
            max_results=body.get("max_results", 25),
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    background_tasks.add_task(_discover_background, search)
    return {"status": "started"}


@app.get("/api/searches/geos")
async def get_search_geos():
    """Sorted city labels for the frontend's city dropdowns."""
    return sorted(config.LINKEDIN_GEO_IDS.keys())


@app.get("/api/searches")
async def get_searches():
    return [s.model_dump() for s in db.get_all_searches()]


@app.post("/api/searches")
async def create_search(body: dict):
    geo_label = body.get("geo_label", "")
    geo_id = config.LINKEDIN_GEO_IDS.get(geo_label)
    if geo_id is None:
        raise HTTPException(status_code=400, detail=f"Unknown geo_label: {geo_label}")

    try:
        search = SavedSearch(
            name=body.get("name", ""),
            titles=body.get("titles", []),
            geo_id=geo_id,
            geo_label=geo_label,
            remote_only=body.get("remote_only", False),
            easy_apply_only=body.get("easy_apply_only", True),
            max_results=body.get("max_results", 25),
            enabled=body.get("enabled", True),
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    new_id = db.insert_search(
        name=search.name, titles=search.titles, geo_id=search.geo_id,
        geo_label=search.geo_label, remote_only=search.remote_only,
        easy_apply_only=search.easy_apply_only, max_results=search.max_results,
        enabled=search.enabled,
    )
    return db.get_search_by_id(new_id).model_dump()


@app.get("/api/searches/{search_id}")
async def get_search(search_id: int):
    search = db.get_search_by_id(search_id)
    if not search:
        raise HTTPException(status_code=404, detail="Search not found")
    return search.model_dump()


@app.put("/api/searches/{search_id}")
async def update_search_endpoint(search_id: int, body: dict):
    existing = db.get_search_by_id(search_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Search not found")

    geo_id = existing.geo_id
    geo_label = existing.geo_label
    if "geo_label" in body:
        geo_label = body["geo_label"]
        looked_up = config.LINKEDIN_GEO_IDS.get(geo_label)
        if looked_up is None:
            raise HTTPException(status_code=400, detail=f"Unknown geo_label: {geo_label}")
        geo_id = looked_up

    merged = {
        "name": body.get("name", existing.name),
        "titles": body.get("titles", existing.titles),
        "geo_id": geo_id,
        "geo_label": geo_label,
        "remote_only": body.get("remote_only", existing.remote_only),
        "easy_apply_only": body.get("easy_apply_only", existing.easy_apply_only),
        "max_results": body.get("max_results", existing.max_results),
        "enabled": body.get("enabled", existing.enabled),
    }
    try:
        SavedSearch(id=search_id, **merged)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    db.update_search(search_id, **merged)
    return db.get_search_by_id(search_id).model_dump()


@app.delete("/api/searches/{search_id}")
async def delete_search_endpoint(search_id: int):
    existing = db.get_search_by_id(search_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Search not found")
    db.delete_search(search_id)
    return {"deleted": True}


@app.post("/api/searches/{search_id}/toggle")
async def toggle_search_endpoint(search_id: int):
    existing = db.get_search_by_id(search_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Search not found")
    new_value = db.toggle_search(search_id)
    return {"enabled": new_value}


@app.post("/api/searches/{search_id}/run")
async def run_search_endpoint(search_id: int, background_tasks: BackgroundTasks):
    """Runs just this one search as a background task, using its own
    max_results directly — no global budget, since this is user-initiated."""
    search = db.get_search_by_id(search_id)
    if not search:
        raise HTTPException(status_code=404, detail="Search not found")
    background_tasks.add_task(_run_search_background, search)
    return {"status": "started"}


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


async def _discover_background(search: SavedSearch) -> None:
    """Runs an ad-hoc (unsaved) search. Results land in `jobs` normally."""
    import logging
    log = logging.getLogger("dashboard.discover")
    try:
        from scraper import LinkedInScraper
        scraper = LinkedInScraper()
        new_jobs = await scraper.scrape(search=search)
        log.info(f"Discover complete — {len(new_jobs)} new jobs")
    except Exception as exc:
        log.error(f"Discover failed: {exc}", exc_info=True)


async def _run_search_background(search: SavedSearch) -> None:
    import logging
    log = logging.getLogger("dashboard.search_run")
    try:
        from scraper import LinkedInScraper
        scraper = LinkedInScraper()
        new_jobs = await scraper.scrape(search=search)
        log.info(f"Search '{search.name}' complete — {len(new_jobs)} new jobs")
    except Exception as exc:
        log.error(f"Search '{search.name}' failed: {exc}", exc_info=True)


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


@app.get("/searches", response_class=HTMLResponse)
async def searches_page(request: Request):
    return templates.TemplateResponse(
        "searches.html", {"request": request, "config": config}
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    assert (_PROJECT_ROOT / "applicant.py").exists(), \
        f"applicant.py not found in _PROJECT_ROOT={_PROJECT_ROOT}"
    db.init_db()
    uvicorn.run("dashboard.app:app", host="127.0.0.1", port=8000, reload=True)
