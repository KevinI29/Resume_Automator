import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from typing import Generator

import config
from models import Application, Job


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY,
                linkedin_job_id TEXT UNIQUE,
                title TEXT,
                company TEXT,
                location TEXT,
                description TEXT,
                url TEXT,
                is_easy_apply BOOLEAN,
                fit_score INTEGER,
                fit_reason TEXT,
                status TEXT DEFAULT 'new',
                resume_path TEXT,
                created_at TIMESTAMP,
                applied_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY,
                job_id INTEGER REFERENCES jobs(id),
                resume_path TEXT,
                method TEXT,
                status TEXT,
                submitted_at TIMESTAMP
            );
        """)
        # Migrate existing databases that predate the resume_path column
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN resume_path TEXT")
        except Exception:
            pass


def insert_job(job: Job) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO jobs
                (linkedin_job_id, title, company, location, description, url,
                 is_easy_apply, fit_score, fit_reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.linkedin_job_id, job.title, job.company, job.location,
                job.description, job.url, job.is_easy_apply, job.fit_score,
                job.fit_reason, job.status, job.created_at or datetime.utcnow(),
            ),
        )


def get_jobs_by_status(status: str) -> list[Job]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    return [Job(**dict(row)) for row in rows]


def get_all_jobs() -> list[Job]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC"
        ).fetchall()
    return [Job(**dict(row)) for row in rows]


def update_job_status(job_id: int, status: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = ? WHERE id = ?", (status, job_id)
        )


def insert_application(application: Application) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO applications (job_id, resume_path, method, status, submitted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                application.job_id, application.resume_path, application.method,
                application.status, application.submitted_at or datetime.utcnow(),
            ),
        )


def get_unscored_jobs() -> list[Job]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE fit_score IS NULL AND status = 'new' ORDER BY created_at DESC"
        ).fetchall()
    return [Job(**dict(row)) for row in rows]


def update_job_score(job_id: int, score: int, reason: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE jobs SET fit_score = ?, fit_reason = ? WHERE id = ?",
            (score, reason, job_id),
        )


def get_jobs_above_threshold(min_score: int = config.MIN_FIT_SCORE) -> list[Job]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE fit_score >= ? ORDER BY fit_score DESC",
            (min_score,),
        ).fetchall()
    return [Job(**dict(row)) for row in rows]


def update_job_resume_path(job_id: int, resume_path: str | None) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE jobs SET resume_path = ? WHERE id = ?", (resume_path, job_id)
        )


def get_approved_jobs_without_resume() -> list[Job]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = 'approved' AND resume_path IS NULL"
        ).fetchall()
    return [Job(**dict(row)) for row in rows]


def get_job_by_id(job_id: int) -> Job | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return Job(**dict(row)) if row else None


def get_stats() -> dict:
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        new = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'new'").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'approved'").fetchone()[0]
        applied = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'applied'").fetchone()[0]
        skipped = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'skipped'").fetchone()[0]
        above = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE fit_score >= ?", (config.MIN_FIT_SCORE,)
        ).fetchone()[0]
    return {
        "total": total,
        "new": new,
        "approved": approved,
        "applied": applied,
        "skipped": skipped,
        "above_threshold": above,
    }


def reset_stuck_tailoring_jobs() -> None:
    """Reset approved jobs whose resume_path is NULL — stuck from a previous failed tailoring run."""
    with _conn() as conn:
        conn.execute("""
            UPDATE jobs
            SET status = 'approved', resume_path = NULL
            WHERE status = 'approved' AND resume_path IS NULL
        """)


def get_approved_jobs_with_resume() -> list[Job]:
    """Jobs ready to apply — approved with a real PDF resume (not null, not 'failed')."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = 'approved' "
            "AND resume_path IS NOT NULL AND resume_path != 'failed'"
        ).fetchall()
    return [Job(**dict(row)) for row in rows]


def update_job_applied(job_id: int) -> None:
    """Mark a job as applied and record the timestamp."""
    with _conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'applied', applied_at = ? WHERE id = ?",
            (datetime.utcnow(), job_id),
        )


def get_daily_application_count() -> int:
    """Count of jobs applied today — used to enforce the daily cap."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'applied' AND date(applied_at) = date('now')"
        ).fetchone()
    return row[0]
