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


@contextmanager
def get_connection():
    conn = sqlite3.connect(config.DB_PATH)
    try:
        yield conn
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
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN validated_at TIMESTAMP")
        except Exception:
            pass  # column already exists in this database


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
    """
    Returns job counts grouped by status, plus a count of 'new' jobs that
    are likely expired based on JOB_EXPIRY_HOURS.
    Used by the dashboard GET /api/stats endpoint.
    """
    from config import JOB_EXPIRY_HOURS

    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")
        rows = cursor.fetchall()
        counts = {row[0]: row[1] for row in rows}

        cursor.execute(
            """
            SELECT COUNT(*) FROM jobs
            WHERE status = 'new'
            AND created_at < datetime('now', ?)
            """,
            (f"-{JOB_EXPIRY_HOURS} hours",),
        )
        likely_expired = cursor.fetchone()[0]

    return {
        "total": sum(counts.values()),
        "new": counts.get("new", 0),
        "approved": counts.get("approved", 0),
        "applied": counts.get("applied", 0),
        "skipped": counts.get("skipped", 0),
        "manual": counts.get("manual", 0),
        "closed": counts.get("closed", 0),
        "failed": counts.get("failed", 0),
        "likely_expired": likely_expired,
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


def get_all_jobs_sorted(
    sort_by: str = "score",
    order: str = "desc",
    search: str = "",
    status: str = "",
) -> list[Job]:
    """
    Returns jobs with optional sorting, full-text search, and status filter.
    All parameters are optional. Default behavior matches get_all_jobs() sorted by score desc.
    Used exclusively by the dashboard GET /api/jobs endpoint.
    """
    sort_columns = {
        "score": "fit_score",
        "date": "created_at",
        "company": "company",
    }
    col = sort_columns.get(sort_by, "fit_score")
    direction = "DESC" if order == "desc" else "ASC"

    conditions = []
    params: list = []

    if status:
        conditions.append("status = ?")
        params.append(status)

    if search:
        conditions.append(
            "(title LIKE ? OR company LIKE ? OR description LIKE ?)"
        )
        term = f"%{search}%"
        params.extend([term, term, term])

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT * FROM jobs {where_clause} ORDER BY {col} {direction}"

    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [Job(**dict(row)) for row in rows]


def get_likely_expired_jobs(hours: int | None = None) -> list[Job]:
    """
    Returns jobs with status='new' that are older than `hours` hours.
    Used for the dashboard's passive expiry warning badges.
    If hours is None, reads JOB_EXPIRY_HOURS from config.
    """
    if hours is None:
        from config import JOB_EXPIRY_HOURS
        hours = JOB_EXPIRY_HOURS

    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'new'
            AND created_at < datetime('now', ?)
            ORDER BY created_at ASC
            """,
            (f"-{hours} hours",),
        )
        rows = cursor.fetchall()
        return [Job(**dict(row)) for row in rows]


def purge_dead_jobs() -> tuple[int, list[str]]:
    """
    Deletes all jobs with status 'closed' or 'failed', plus their associated
    application records. Returns the count of deleted jobs and a list of
    resume file paths so the caller can clean up files on disk.

    Does NOT delete: new, approved, applied, skipped, manual, expired jobs.
    The caller is responsible for any on-disk file deletion.
    """
    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT resume_path FROM jobs
            WHERE status IN ('closed', 'failed')
            AND resume_path IS NOT NULL
            AND resume_path != ''
            AND resume_path != 'failed'
            """
        )
        paths = [row[0] for row in cursor.fetchall()]

        cursor.execute(
            """
            DELETE FROM applications
            WHERE job_id IN (
                SELECT id FROM jobs WHERE status IN ('closed', 'failed')
            )
            """
        )

        cursor.execute("DELETE FROM jobs WHERE status IN ('closed', 'failed')")
        count = cursor.rowcount
        conn.commit()
        return count, paths


def update_job_field(job_id: int, field: str, value) -> None:
    """
    Updates a single field on a job row.
    Only allows whitelisted field names to prevent SQL injection through
    the column name (which cannot be parameterized with `?`).
    Used by validator.py to update is_easy_apply without touching status.
    """
    _ALLOWED_FIELDS = {
        "is_easy_apply",
        "status",
        "fit_score",
        "resume_path",
        "validated_at",
        "expires_at",
    }
    if field not in _ALLOWED_FIELDS:
        raise ValueError(
            f"update_job_field: '{field}' is not a writable field. "
            f"Allowed: {sorted(_ALLOWED_FIELDS)}"
        )
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE jobs SET {field} = ? WHERE id = ?",
            (value, job_id),
        )
        conn.commit()


def unskip_job(job_id: int) -> bool:
    """
    Moves a single skipped job back to 'new' status.
    Returns True if the job was found and updated, False if not found
    or if it was not in 'skipped' status.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE jobs SET status = 'new' WHERE id = ? AND status = 'skipped'",
            (job_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def unskip_all_jobs() -> int:
    """
    Moves ALL jobs with status='skipped' back to status='new'.
    Returns the count of jobs restored.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE jobs SET status = 'new' WHERE status = 'skipped'")
        conn.commit()
        return cursor.rowcount
