"""
validator.py — LinkedIn job page classification via Playwright
Runs HEADED (never headless). Shares the persistent browser profile with
applicant.py. Visits each job's LinkedIn URL and classifies it as one of:
easy_apply, manual, closed, applied, or unknown.

Usage:
  python validator.py                  # batch-validate unvalidated jobs
  python validator.py --single <job_id>  # validate one job
"""
import asyncio
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext, Page

import config
import db
from utils import setup_logger


class JobValidator:

    BROWSER_DATA_DIR = "browser_data/linkedin_profile"  # shared with applicant.py

    def __init__(self):
        self.logger = setup_logger("validator")
        self.playwright = None
        self.context: BrowserContext | None = None

    # ── Browser lifecycle ───────────────────────────────────────────────────

    async def setup_browser(self) -> None:
        self.playwright = await async_playwright().start()

        profile_path = Path(self.BROWSER_DATA_DIR).resolve()
        profile_path.mkdir(parents=True, exist_ok=True)

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        self.logger.info(f"Browser launched. Profile: {profile_path}")

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        self.logger.info("Browser closed")

    # ── Human-like helpers ──────────────────────────────────────────────────

    async def _human_delay(self, min_s: float = 4.0, max_s: float = 9.0) -> None:
        delay = random.uniform(min_s, max_s)
        self.logger.debug(f"Delay: {delay:.1f}s")
        await asyncio.sleep(delay)

    async def _scroll_page(self, page: Page) -> None:
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 300)")
            await asyncio.sleep(random.uniform(0.8, 1.8))
        await asyncio.sleep(random.uniform(0.5, 1.0))
        await page.evaluate("window.scrollTo(0, 0)")

    # ── Safety checks ────────────────────────────────────────────────────────

    async def _is_logged_out(self, page: Page) -> bool:
        try:
            if "/login" in page.url or "/authwall" in page.url:
                return True
            login_input = await page.query_selector('input[name="session_key"]')
            if login_input:
                return True
        except Exception:
            pass
        return False

    async def _captcha_present(self, page: Page) -> bool:
        try:
            captcha_selectors = [
                'iframe[src*="captcha"]',
                'iframe[src*="recaptcha"]',
                'div[class*="captcha"]',
                '#captcha',
            ]
            for sel in captcha_selectors:
                if await page.query_selector(sel):
                    return True
            body = await page.inner_text("body")
            if "security verification" in body.lower():
                return True
        except Exception:
            pass
        return False

    # ── URL guard ────────────────────────────────────────────────────────────

    def _validate_url(self, job) -> bool:
        if not job.url or not isinstance(job.url, str):
            self.logger.warning(
                f"Job {job.id} ({job.title}) has no URL — skipping"
            )
            return False
        if "linkedin.com" not in job.url:
            self.logger.warning(
                f"Job {job.id} URL does not look like LinkedIn: {job.url} — skipping"
            )
            return False
        return True

    # ── Page state detection ────────────────────────────────────────────────

    async def _detect_page_state(self, page: Page) -> str:
        """
        Detect which of the 6 states the job page is in.
        Order is mandatory — see the state table in the session brief.
        Returns one of: 'closed', 'applied', 'easy_apply', 'manual', 'unknown'
        """
        try:
            body_text = await page.inner_text("body")
            body_lower = body_text.lower()
        except Exception as e:
            self.logger.warning(f"Could not read page body: {e}")
            return "unknown"

        # State 1 — Removed / invalid
        removed_phrases = [
            "unable to load the page",
            "job posting has been removed",
            "not be valid",
        ]
        if any(phrase in body_lower for phrase in removed_phrases):
            return "closed"

        normalized_url = page.url.rstrip("/")
        if normalized_url in [
            "https://www.linkedin.com/jobs",
            "https://www.linkedin.com/jobs/search",
        ]:
            return "closed"

        # State 2 — Already applied (MUST be before expired check)
        if "application submitted" in body_lower:
            return "applied"
        try:
            applied_el = await page.query_selector(
                '[aria-label*="Applied"], .artdeco-inline-feedback'
            )
            if applied_el:
                el_text = (await applied_el.inner_text()).lower()
                if "applied" in el_text:
                    return "applied"
        except Exception:
            pass

        # State 3 — Expired
        if "no longer accepting applications" in body_lower:
            return "closed"

        # State 4 — Easy Apply button visible
        easy_apply_selectors = [
            'button.jobs-apply-button[aria-label*="Easy Apply"]',
            'button[aria-label*="Easy Apply"]',
            'button:has-text("Easy Apply")',
        ]
        for sel in easy_apply_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    return "easy_apply"
            except Exception:
                continue

        # State 5 — External apply only
        external_selectors = [
            'a[href*="externalApply"]',
            'button.jobs-apply-button',  # generic Apply (no "Easy Apply")
        ]
        for sel in external_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    return "manual"
            except Exception:
                continue

        if "responses managed off linkedin" in body_lower:
            return "manual"

        # State 6 — Unknown
        return "unknown"

    # ── Single-job validation ────────────────────────────────────────────────

    async def check_job(self, job) -> str:
        """
        Navigate to a job's LinkedIn URL, detect its state, and update the DB.
        Returns the detected state string.
        Raises RuntimeError if session expired or CAPTCHA detected — caller
        must stop the batch.
        """
        if not self.context:
            raise RuntimeError("Browser not set up. Call setup_browser() first.")

        if not self._validate_url(job):
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            db.update_job_field(job.id, "validated_at", now_str)
            return "unknown"

        page = await self.context.new_page()

        try:
            self.logger.info(f"Checking: [{job.id}] {job.title} @ {job.company}")
            self.logger.info(f"  URL: {job.url}")

            await page.goto(
                job.url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            await asyncio.sleep(random.uniform(2.0, 3.5))

            if await self._is_logged_out(page):
                self.logger.warning("Not logged in — please log in in the browser window")
                # H3 fix: input() raises EOFError with no stdin TTY (subprocess
                # context) — guard it and fail loudly instead.
                if sys.stdin.isatty():
                    input("Press Enter after logging in...")
                else:
                    self.logger.error(
                        "LinkedIn session not logged in and running in subprocess context. "
                        "Run directly from a terminal: python validator.py --single <job_id>"
                    )
                    raise RuntimeError(
                        "Not logged in and no TTY available — cannot pause for manual login"
                    )
                await page.goto(job.url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(random.uniform(2.0, 3.5))
                if await self._is_logged_out(page):
                    raise RuntimeError(
                        "Still not logged in after manual login attempt. "
                        "Open the browser manually, log in, and retry."
                    )

            if await self._captcha_present(page):
                raise RuntimeError(
                    "CAPTCHA detected. Stop all automation and wait at least "
                    "30 minutes before retrying."
                )

            await self._scroll_page(page)

            state = await self._detect_page_state(page)
            self.logger.info(f"  → State: {state}")

            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            if state == "closed":
                db.update_job_field(job.id, "status", "closed")

            elif state == "applied":
                if job.status != "applied":
                    db.update_job_field(job.id, "status", "applied")

            elif state == "easy_apply":
                db.update_job_field(job.id, "is_easy_apply", True)
                # Do NOT change status — job stays 'new' for human review

            elif state == "manual":
                db.update_job_field(job.id, "status", "manual")
                db.update_job_field(job.id, "is_easy_apply", False)

            elif state == "unknown":
                self.logger.warning(
                    f"  Could not classify job {job.id}. "
                    f"Manual check recommended: {job.url}"
                )

            db.update_job_field(job.id, "validated_at", now_str)

            return state

        except RuntimeError:
            raise

        except Exception as e:
            self.logger.error(
                f"  Unexpected error on job {job.id}: {e}", exc_info=True
            )
            try:
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                db.update_job_field(job.id, "validated_at", now_str)
            except Exception:
                pass
            return "unknown"

        finally:
            await page.close()

    # ── Batch validation ─────────────────────────────────────────────────────

    async def validate_batch(self, jobs: list, max_jobs: int = 30) -> dict:
        """
        Validate a list of jobs sequentially.
        Stops immediately on session expiry or CAPTCHA.
        Returns a results summary dict.
        """
        results = {
            "easy_apply": 0,
            "manual": 0,
            "closed": 0,
            "applied": 0,
            "unknown": 0,
            "stopped_early": False,
            "stop_reason": None,
        }

        jobs_to_check = jobs[:max_jobs]
        self.logger.info(
            f"Starting batch: {len(jobs_to_check)} jobs (cap: {max_jobs})"
        )

        for i, job in enumerate(jobs_to_check, start=1):
            self.logger.info(f"[{i}/{len(jobs_to_check)}]")

            try:
                state = await self.check_job(job)
                results[state] = results.get(state, 0) + 1
            except RuntimeError as e:
                results["stopped_early"] = True
                results["stop_reason"] = str(e)
                self.logger.error(f"Batch stopped: {e}")
                break

            if i < len(jobs_to_check):
                await self._human_delay(min_s=4.0, max_s=9.0)

        self.logger.info(f"Batch complete: {results}")
        return results


def get_jobs_to_validate(min_score: int | None = None) -> list:
    """
    Returns jobs that need validation:
    - status = 'new'
    - fit_score >= min_score
    - validated_at IS NULL (never validated before)
    Uses config.MIN_FIT_SCORE if min_score is not provided.
    """
    import sqlite3 as _sqlite3

    if min_score is None:
        min_score = config.MIN_FIT_SCORE

    from models import Job as _Job

    with db.get_connection() as conn:
        conn.row_factory = _sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'new'
            AND fit_score >= ?
            AND (validated_at IS NULL OR validated_at = '')
            ORDER BY fit_score DESC
            """,
            (min_score,),
        )
        rows = cursor.fetchall()
        return [_Job(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    # `sys` is imported at module level now (needed by check_job()'s
    # stdin/TTY check) — no longer imported locally here.
    from rich.console import Console
    from rich.table import Table
    from rich import box

    console = Console()
    args = sys.argv[1:]

    # --single <job_id> mode
    if "--single" in args:
        try:
            job_id = int(args[args.index("--single") + 1])
        except (ValueError, IndexError):
            console.print("[red]Usage: python validator.py --single <job_id>[/red]")
            return

        job = db.get_job_by_id(job_id)
        if not job:
            console.print(f"[red]Job ID {job_id} not found in database.[/red]")
            return

        console.print(f"[bold]Validating:[/bold] {job.title} @ {job.company}")
        console.print(f"[dim]URL: {job.url}[/dim]")

        console.print(
            "\n[yellow]NOTE:[/yellow] If this is your first time running the validator, "
            "the browser may open to the LinkedIn login page.\n"
            "Log in manually in the browser window, then validation will continue automatically."
        )

        validator = JobValidator()
        await validator.setup_browser()
        try:
            state = await validator.check_job(job)
        except RuntimeError as e:
            console.print(f"[bold red]Stopped:[/bold red] {e}")
            await validator.close()
            return
        finally:
            await validator.close()

        color = {
            "easy_apply": "green",
            "manual": "yellow",
            "closed": "red",
            "applied": "blue",
            "unknown": "dim",
        }.get(state, "white")
        console.print(f"\nResult: [{color}]{state}[/{color}]")

        updated = db.get_job_by_id(job_id)
        if updated:
            console.print(
                f"[dim]DB updated — status: {updated.status} | "
                f"is_easy_apply: {updated.is_easy_apply} | "
                f"validated_at: {updated.validated_at}[/dim]"
            )
        return

    # ---- Batch mode (default) ----
    jobs = get_jobs_to_validate()

    if not jobs:
        console.print(
            "[yellow]No unvalidated jobs above the scoring threshold.[/yellow]\n"
            "[dim]Either all eligible jobs are already validated, or no jobs "
            f"score >= {config.MIN_FIT_SCORE}. Run the scraper and scorer first.[/dim]"
        )
        return

    console.print(
        f"[bold green]Starting validation:[/bold green] "
        f"{len(jobs)} unvalidated jobs (fit_score >= {config.MIN_FIT_SCORE})\n"
        "[dim]The browser will open shortly. "
        "If LinkedIn shows a login page, log in manually — "
        "your session will be saved for future runs.\n"
        "Press Ctrl+C at any time to stop safely.[/dim]\n"
    )

    validator = JobValidator()
    await validator.setup_browser()
    try:
        results = await validator.validate_batch(jobs)
    finally:
        await validator.close()

    table = Table(title="Validation Results", box=box.ROUNDED)
    table.add_column("State", style="bold")
    table.add_column("Count", justify="right")

    table.add_row("Easy Apply ✓", f"[green]{results['easy_apply']}[/green]")
    table.add_row("Manual / External", f"[yellow]{results['manual']}[/yellow]")
    table.add_row("Closed / Expired", f"[red]{results['closed']}[/red]")
    table.add_row("Already Applied", f"[blue]{results['applied']}[/blue]")
    table.add_row("Unknown", f"[dim]{results['unknown']}[/dim]")
    console.print(table)

    if results["stopped_early"]:
        console.print(
            f"\n[bold red]⚠ Stopped early:[/bold red] {results['stop_reason']}\n"
            "[dim]Wait before retrying. If CAPTCHA: wait at least 30 minutes "
            "and visit LinkedIn manually to confirm your account is not flagged.[/dim]"
        )
    else:
        total = sum(
            v for k, v in results.items()
            if k not in ("stopped_early", "stop_reason")
        )
        console.print(
            f"\n[bold green]Done.[/bold green] {total} jobs classified. "
            f"{results['easy_apply']} Easy Apply jobs ready for your approval."
        )


if __name__ == "__main__":
    asyncio.run(_main())
