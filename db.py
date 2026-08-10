import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Generator

import config
from models import Application, Job, PendingAnswer, QABankEntry, SavedSearch

logger = logging.getLogger("db")


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
            -- jobs.status: new / approved / applied / skipped / failed / closed /
            -- pending_questions / expired
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

            CREATE TABLE IF NOT EXISTS qa_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_norm TEXT UNIQUE NOT NULL,
                question_raw TEXT NOT NULL,
                field_type TEXT NOT NULL,
                options_json TEXT,           -- JSON array string, or NULL
                answer TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',  -- 'config' | 'ai' | 'manual'
                confidence REAL,
                use_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pending_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER REFERENCES jobs(id),
                question_raw TEXT NOT NULL,
                question_norm TEXT NOT NULL,
                field_type TEXT NOT NULL,
                options_json TEXT,           -- JSON array string, or NULL
                ai_suggested_answer TEXT,
                ai_confidence REAL,
                resolved BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                titles_json TEXT NOT NULL,        -- JSON array of title strings
                geo_id TEXT NOT NULL,             -- LinkedIn opaque GeoID
                geo_label TEXT NOT NULL,          -- friendly city name for display
                remote_only INTEGER NOT NULL DEFAULT 0,
                easy_apply_only INTEGER NOT NULL DEFAULT 1,
                max_results INTEGER NOT NULL DEFAULT 25,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    are likely expired based on JOB_EXPIRY_HOURS, and (Session 11) 'aging'
    (approaching auto-purge) / 'purgeable_old' (already past MAX_JOB_AGE_DAYS)
    counts. Used by the dashboard GET /api/stats endpoint.
    """
    from config import JOB_EXPIRY_HOURS

    protected = config.AGE_PURGE_PROTECTED_STATUSES
    placeholders = ",".join("?" * len(protected))

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

        cursor.execute(
            f"""
            SELECT COUNT(*) FROM jobs
            WHERE created_at < datetime('now', ?)
            AND created_at >= datetime('now', ?)
            AND status NOT IN ({placeholders})
            """,
            (f"-{config.JOB_AGE_WARNING_DAYS} days",
             f"-{config.MAX_JOB_AGE_DAYS} days", *protected),
        )
        aging = cursor.fetchone()[0]

        cursor.execute(
            f"""
            SELECT COUNT(*) FROM jobs
            WHERE created_at < datetime('now', ?)
            AND status NOT IN ({placeholders})
            """,
            (f"-{config.MAX_JOB_AGE_DAYS} days", *protected),
        )
        purgeable_old = cursor.fetchone()[0]

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
        "pending_questions": get_pending_question_count(),
        "aging": aging,
        "purgeable_old": purgeable_old,
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


def get_purgeable_old_jobs() -> list[Job]:
    """
    Returns jobs older than MAX_JOB_AGE_DAYS, excluding statuses in
    AGE_PURGE_PROTECTED_STATUSES ('applied', 'pending_questions' by default —
    they carry a submitted-application record or accumulated Q&A work that
    purging would silently destroy). Ordered by created_at ASC.
    Backs the dashboard's "Purge Old Jobs" count and run.py maintain.
    """
    protected = config.AGE_PURGE_PROTECTED_STATUSES
    placeholders = ",".join("?" * len(protected))
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT * FROM jobs
            WHERE created_at < datetime('now', ?)
            AND status NOT IN ({placeholders})
            ORDER BY created_at ASC
            """,
            (f"-{config.MAX_JOB_AGE_DAYS} days", *protected),
        )
        rows = cursor.fetchall()
        return [Job(**dict(row)) for row in rows]


def purge_old_jobs() -> dict:
    """
    Deletes jobs older than MAX_JOB_AGE_DAYS (excluding AGE_PURGE_PROTECTED_STATUSES),
    plus their application records, in a single transaction — mirrors the
    purge_dead_jobs() pattern (select first, delete in the same transaction,
    single commit). After the commit, deletes each purged job's tailored
    resume PDF from disk:
      - None / empty / the 'failed' sentinel are skipped (not real paths)
      - paths that resolve outside config.OUTPUT_RESUME_DIR are skipped and
        logged at WARNING (defense against a malformed resume_path deleting
        an arbitrary file) — never raises
      - a missing file is logged at DEBUG — never raises
      - any other OSError is logged at WARNING — never raises
    Returns {"jobs_purged": N, "files_deleted": M}.
    """
    protected = config.AGE_PURGE_PROTECTED_STATUSES
    placeholders = ",".join("?" * len(protected))
    age_param = f"-{config.MAX_JOB_AGE_DAYS} days"

    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            f"""
            SELECT id, resume_path FROM jobs
            WHERE created_at < datetime('now', ?)
            AND status NOT IN ({placeholders})
            """,
            (age_param, *protected),
        )
        rows = cursor.fetchall()
        job_ids = [row[0] for row in rows]
        resume_paths = [row[1] for row in rows if row[1] and row[1] != "failed"]

        if job_ids:
            id_placeholders = ",".join("?" * len(job_ids))
            cursor.execute(
                f"DELETE FROM applications WHERE job_id IN ({id_placeholders})",
                job_ids,
            )
            cursor.execute(
                f"DELETE FROM jobs WHERE id IN ({id_placeholders})",
                job_ids,
            )

        conn.commit()

    files_deleted = 0
    output_dir = Path(config.OUTPUT_RESUME_DIR).resolve()

    for path in resume_paths:
        try:
            resolved = Path(path).resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(output_dir):
            logger.warning(
                "purge_old_jobs: refusing to delete path outside OUTPUT_RESUME_DIR: %s",
                path,
            )
            continue
        if not resolved.exists():
            logger.debug("purge_old_jobs: resume file already gone: %s", path)
            continue
        try:
            resolved.unlink()
            files_deleted += 1
        except OSError as e:
            logger.warning("purge_old_jobs: could not delete file %s: %s", path, e)

    return {"jobs_purged": len(job_ids), "files_deleted": files_deleted}


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


# ── Adaptive Q&A resolution (Session 10) ─────────────────────────────────────

def normalize_question(text: str) -> str:
    """
    Canonical normalization for qa_bank dedup key.
    Lowercase -> strip punctuation -> collapse whitespace -> strip ends.
    qa_resolver.py MUST import and call this function directly.
    Never re-implement it — any divergence silently breaks tier-1 matching.
    """
    text = text.lower()
    text = re.sub(r'[^\w\s]', '', text)   # remove punctuation
    text = re.sub(r'\s+', ' ', text)       # collapse whitespace
    return text.strip()


def get_qa_exact(question_norm: str, field_type: str) -> QABankEntry | None:
    """
    Returns the QABankEntry matching both question_norm and field_type,
    or None. Used for tier-1 resolution.
    """
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM qa_bank WHERE question_norm = ? AND field_type = ?",
            (question_norm, field_type),
        )
        row = cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        d['options'] = json.loads(d.pop('options_json')) if d.get('options_json') else None
        return QABankEntry(**d)


def get_qa_bank_all() -> list[QABankEntry]:
    """
    Full qa_bank table, ordered by use_count DESC.
    Used for in-memory fuzzy matching (loaded once per applicant.py run)
    and the dashboard's Manage Saved Answers view.
    """
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM qa_bank ORDER BY use_count DESC")
        rows = cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d['options'] = json.loads(d.pop('options_json')) if d.get('options_json') else None
            result.append(QABankEntry(**d))
        return result


def upsert_qa_answer(
    question_norm: str, question_raw: str, field_type: str,
    options_json: str | None, answer: str, source: str,
    confidence: float | None,
) -> None:
    """
    INSERT a new qa_bank row, or UPDATE the existing row if question_norm
    already exists (UNIQUE constraint). On UPDATE: replaces answer, source,
    confidence, and sets updated_at = now(). Does NOT reset use_count.
    options_json must already be a JSON string (or None) — caller does json.dumps().
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO qa_bank
                (question_norm, question_raw, field_type, options_json,
                 answer, source, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(question_norm) DO UPDATE SET
                question_raw = excluded.question_raw,
                field_type   = excluded.field_type,
                options_json = excluded.options_json,
                answer       = excluded.answer,
                source       = excluded.source,
                confidence   = excluded.confidence,
                updated_at   = CURRENT_TIMESTAMP
            """,
            (question_norm, question_raw, field_type, options_json,
             answer, source, confidence),
        )
        conn.commit()


def increment_qa_use_count(qa_id: int) -> None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE qa_bank SET use_count = use_count + 1 WHERE id = ?",
            (qa_id,)
        )
        conn.commit()


def update_qa_answer(qa_id: int, answer: str) -> None:
    """Manual edit from the Manage Saved Answers dashboard modal."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE qa_bank SET answer = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (answer, qa_id)
        )
        conn.commit()


def delete_qa_answer(qa_id: int) -> None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM qa_bank WHERE id = ?", (qa_id,))
        conn.commit()


def save_pending_questions(job_id: int, questions: list[dict]) -> None:
    """
    Bulk-inserts pending_answers rows for one job and sets
    jobs.status = 'pending_questions' in the same transaction.
    questions: list of dicts with keys:
        label (str), field_type (str), options (list[str] | None),
        ai_suggested_answer (str | None), ai_confidence (float | None)
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        for q in questions:
            cursor.execute(
                """
                INSERT INTO pending_answers
                    (job_id, question_raw, question_norm, field_type,
                     options_json, ai_suggested_answer, ai_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    q['label'],
                    normalize_question(q['label']),
                    q['field_type'],
                    json.dumps(q['options']) if q.get('options') else None,
                    q.get('ai_suggested_answer'),
                    q.get('ai_confidence'),
                ),
            )
        cursor.execute(
            "UPDATE jobs SET status = 'pending_questions' WHERE id = ?",
            (job_id,)
        )
        conn.commit()   # single commit — both writes succeed or neither does


def get_pending_questions_for_job(job_id: int) -> list[PendingAnswer]:
    """Returns unresolved pending_answers rows for one job."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM pending_answers WHERE job_id = ? AND resolved = 0",
            (job_id,)
        )
        rows = cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d['options'] = json.loads(d.pop('options_json')) if d.get('options_json') else None
            result.append(PendingAnswer(**d))
        return result


def get_jobs_pending_questions() -> list[Job]:
    """All jobs with status = 'pending_questions'."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM jobs WHERE status = 'pending_questions' ORDER BY created_at DESC"
        )
        rows = cursor.fetchall()
        return [Job(**dict(row)) for row in rows]


def resolve_pending_questions(job_id: int, resolutions: list[dict]) -> None:
    """
    resolutions: [{"pending_id": int, "answer": str, "save_to_bank": bool}, ...]

    Atomically (single transaction):
    1. For each resolution: set pending_answers.resolved = 1, store the answer.
    2. Where save_to_bank is True: upsert into qa_bank with source='manual'.
    3. Set jobs.status = 'approved' — NOT 'new'. resume_path is untouched.
       get_approved_jobs_with_resume() will pick this job up on the next Apply run.

    If any statement fails, conn.commit() is never reached and the DB stays unchanged.
    """
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        for res in resolutions:
            pending_id = res['pending_id']
            answer     = res['answer']
            save       = res.get('save_to_bank', True)

            # Mark resolved and store the chosen answer
            cursor.execute(
                """UPDATE pending_answers
                   SET resolved = 1, ai_suggested_answer = ?
                   WHERE id = ?""",
                (answer, pending_id),
            )

            if save:
                # Fetch the row to get question_norm, field_type, options_json
                cursor.execute(
                    "SELECT * FROM pending_answers WHERE id = ?",
                    (pending_id,)
                )
                row = cursor.fetchone()
                if row:
                    cursor.execute(
                        """
                        INSERT INTO qa_bank
                            (question_norm, question_raw, field_type,
                             options_json, answer, source, confidence)
                        VALUES (?, ?, ?, ?, ?, 'manual', NULL)
                        ON CONFLICT(question_norm) DO UPDATE SET
                            answer     = excluded.answer,
                            source     = 'manual',
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (row['question_norm'], row['question_raw'],
                         row['field_type'], row['options_json'], answer),
                    )

        # Set job back to approved — resume_path unchanged, next Apply run picks it up
        cursor.execute(
            "UPDATE jobs SET status = 'approved' WHERE id = ?",
            (job_id,)
        )
        conn.commit()  # all-or-nothing


def get_pending_question_count() -> int:
    """Count of jobs with status = 'pending_questions'. For the dashboard stat."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'pending_questions'"
        )
        return cursor.fetchone()[0]


# ── Saved searches (Session 12) ──────────────────────────────────────────────

def _row_to_saved_search(row: sqlite3.Row) -> SavedSearch:
    """
    Single deserialization point for `searches` rows: JSON-decodes
    titles_json into a list and converts the INTEGER 0/1 boolean columns
    back to Python bool. Every read path (get_all/get_enabled/get_by_id)
    must go through this — do not duplicate the logic inline.
    """
    d = dict(row)
    d["titles"] = json.loads(d.pop("titles_json"))
    d["remote_only"] = bool(d["remote_only"])
    d["easy_apply_only"] = bool(d["easy_apply_only"])
    d["enabled"] = bool(d["enabled"])
    return SavedSearch(**d)


def get_all_searches() -> list[SavedSearch]:
    """All saved searches, newest first."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM searches ORDER BY created_at DESC")
        rows = cursor.fetchall()
        return [_row_to_saved_search(row) for row in rows]


def get_enabled_searches() -> list[SavedSearch]:
    """Enabled saved searches, insertion order — the order run.py pipeline iterates them."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM searches WHERE enabled = 1 ORDER BY id ASC")
        rows = cursor.fetchall()
        return [_row_to_saved_search(row) for row in rows]


def get_search_by_id(search_id: int) -> SavedSearch | None:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM searches WHERE id = ?", (search_id,))
        row = cursor.fetchone()
        return _row_to_saved_search(row) if row else None


def insert_search(
    name: str,
    titles: list[str],
    geo_id: str,
    geo_label: str,
    remote_only: bool = False,
    easy_apply_only: bool = True,
    max_results: int = 25,
    enabled: bool = True,
) -> int:
    """Returns the new row id. titles is JSON-serialized before write."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO searches
                (name, titles_json, geo_id, geo_label, remote_only,
                 easy_apply_only, max_results, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, json.dumps(titles), geo_id, geo_label, remote_only,
             easy_apply_only, max_results, enabled),
        )
        conn.commit()
        return cursor.lastrowid


def update_search(search_id: int, **fields) -> None:
    """
    Partial update. Whitelist: name, titles, geo_id, geo_label, remote_only,
    easy_apply_only, max_results, enabled. `titles` (a Python list) is
    JSON-serialized into titles_json if present. Unknown fields are
    silently ignored — the endpoint layer validates the payload via
    SavedSearch(...) before calling this.
    """
    _ALLOWED_FIELDS = {
        "name", "titles", "geo_id", "geo_label", "remote_only",
        "easy_apply_only", "max_results", "enabled",
    }
    updates = {k: v for k, v in fields.items() if k in _ALLOWED_FIELDS}
    if not updates:
        return
    if "titles" in updates:
        updates["titles_json"] = json.dumps(updates.pop("titles"))

    set_clause = ", ".join(f"{col} = ?" for col in updates)
    params = list(updates.values()) + [search_id]

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE searches SET {set_clause} WHERE id = ?", params)
        conn.commit()


def delete_search(search_id: int) -> None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM searches WHERE id = ?", (search_id,))
        conn.commit()


def toggle_search(search_id: int) -> bool:
    """Flips the enabled flag atomically. Returns the new value."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE searches SET enabled = 1 - enabled WHERE id = ?", (search_id,)
        )
        conn.commit()
        cursor.execute("SELECT enabled FROM searches WHERE id = ?", (search_id,))
        row = cursor.fetchone()
        return bool(row[0]) if row else False


def update_search_last_run(search_id: int) -> None:
    """Sets last_run_at = CURRENT_TIMESTAMP. Called by the scraper after a
    successful run of that search."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE searches SET last_run_at = CURRENT_TIMESTAMP WHERE id = ?",
            (search_id,),
        )
        conn.commit()
