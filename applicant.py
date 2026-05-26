"""
applicant.py — LinkedIn Easy Apply automation via Playwright
Runs HEADED (never headless). Uses a persistent browser profile so
cookies persist across runs. Max 10-15 applications per day, enforced here.

Usage:
  python applicant.py                    # apply to all approved jobs with resumes
  python applicant.py --single <job_id>  # apply to one specific job
"""
import asyncio
import random
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright
from rich.console import Console

import config
import db
from models import Job
from utils import setup_logger

console = Console()


class LinkedInApplicant:
    BROWSER_DATA_DIR = "browser_data/linkedin_profile"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    def __init__(self) -> None:
        self.logger = setup_logger("applicant")
        self.playwright = None
        self.context: BrowserContext | None = None
        Path("logs/screenshots").mkdir(parents=True, exist_ok=True)

    # ── Browser setup ──────────────────────────────────────────────────────────

    async def setup_browser(self) -> None:
        Path(self.BROWSER_DATA_DIR).mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.BROWSER_DATA_DIR,
            headless=False,
            viewport={"width": 1280, "height": 800},
            user_agent=self.USER_AGENT,
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.logger.info("Browser started (headed Chromium, persistent profile)")

    # ── Human-like primitives ──────────────────────────────────────────────────

    async def _human_delay(self, min_s: float = 2.0, max_s: float = 5.0) -> None:
        delay = random.uniform(min_s, max_s)
        self.logger.debug(f"Delay {delay:.1f}s")
        await asyncio.sleep(delay)

    async def _human_type(self, element, text: str) -> None:
        """Type text character by character with random per-key delays."""
        for char in text:
            await element.type(char, delay=random.randint(50, 150))

    async def _human_click(self, element) -> None:
        """Click with random pixel offset from center — avoids bot-pattern exact-center clicks."""
        box = await element.bounding_box()
        if box:
            offset_x = box["width"] / 2 + random.uniform(-10, 10)
            offset_y = box["height"] / 2 + random.uniform(-5, 5)
            offset_x = max(2, min(offset_x, box["width"] - 2))
            offset_y = max(2, min(offset_y, box["height"] - 2))
            await element.click(position={"x": offset_x, "y": offset_y})
        else:
            await element.click()
        await asyncio.sleep(random.uniform(1.0, 2.0))

    async def _scroll_job_page(self, page: Page) -> None:
        """Scroll slowly through the job description to simulate reading (5-10s total)."""
        scrolls = random.randint(4, 7)
        for _ in range(scrolls):
            await page.mouse.wheel(0, random.randint(250, 350))
            await asyncio.sleep(random.uniform(1.0, 2.5))
        await asyncio.sleep(random.uniform(1.0, 2.0))
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(1.0)

    # ── Safety checks ──────────────────────────────────────────────────────────

    async def _check_captcha(self, page: Page) -> bool:
        for sel in ['iframe[src*="captcha"]', 'div.captcha', '#captcha', '[data-testid="captcha"]']:
            if await page.query_selector(sel):
                return True
        return False

    async def _take_screenshot(self, page: Page, name: str) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"logs/screenshots/{name}_{ts}.png"
        await page.screenshot(path=path)
        self.logger.info(f"Screenshot saved: {path}")

    # ── Navigation ─────────────────────────────────────────────────────────────

    async def navigate_to_job(self, page: Page, url: str) -> bool:
        """Navigate to the job URL and simulate reading it. Returns False if job is closed."""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self._human_delay(2.0, 4.0)

            # Check if we need to log in
            if await page.query_selector('input[name="session_key"]'):
                self.logger.warning("Not logged in — please log in in the browser window")
                input("Press Enter after logging in...")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await self._human_delay(2.0, 4.0)

            if await self._check_captcha(page):
                await self._take_screenshot(page, "captcha")
                raise RuntimeError("CAPTCHA detected — stopping automation")

            content = await page.content()
            if any(phrase in content for phrase in [
                "No longer accepting applications",
                "This job is no longer accepting",
                "no longer available",
            ]):
                self.logger.info(f"Job is closed: {url}")
                return False

            await self._scroll_job_page(page)
            return True

        except RuntimeError:
            raise
        except Exception as exc:
            self.logger.error(f"Navigation failed: {exc}")
            return False

    # ── Easy Apply modal ───────────────────────────────────────────────────────

    async def click_easy_apply(self, page: Page) -> bool:
        """Find and click the Easy Apply button, wait for modal to open."""
        await self._human_delay(2.0, 4.0)
        selectors = [
            'button.jobs-apply-button',
            'button[aria-label*="Easy Apply"]',
            'button[aria-label*="easy apply"]',
            '.jobs-s-apply button',
            '[data-control-name="jobdetails_topcard_inapply"]',
        ]
        for sel in selectors:
            btn = await page.query_selector(sel)
            if btn:
                await self._human_click(btn)
                try:
                    await page.wait_for_selector(
                        '.jobs-easy-apply-modal, [data-test-modal], [role="dialog"]',
                        timeout=8000,
                    )
                    self.logger.info("Easy Apply modal opened")
                    return True
                except Exception:
                    pass  # try next selector
        self.logger.warning("Easy Apply button not found")
        return False

    async def upload_resume(self, page: Page, pdf_path: str) -> bool:
        """Upload the tailored PDF resume inside the Easy Apply modal."""
        try:
            await self._human_delay(1.0, 2.0)
            file_input = await page.query_selector('input[type="file"]')
            if not file_input:
                self.logger.warning("No file input found — skipping resume upload")
                return False
            await file_input.set_input_files(pdf_path)
            await asyncio.sleep(random.uniform(2.0, 3.5))
            self.logger.info(f"Resume uploaded: {pdf_path}")
            return True
        except Exception as exc:
            self.logger.error(f"Resume upload failed: {exc}")
            return False

    async def fill_form_fields(self, page: Page) -> bool:
        """Fill common form fields conservatively. Skips anything unfamiliar."""
        try:
            # Work authorization — look for "yes" radio options
            radios = await page.query_selector_all('input[type="radio"]')
            for radio in radios:
                label = await radio.evaluate(
                    'el => {'
                    '  const lbl = el.closest("label") || document.querySelector(`label[for="${el.id}"]`);'
                    '  return lbl ? lbl.textContent : el.getAttribute("aria-label") || "";'
                    '}'
                )
                if any(w in label.lower() for w in ["yes", "authorized", "citizen", "legally eligible"]):
                    is_checked = await radio.is_checked()
                    if not is_checked:
                        await self._human_click(radio)
                    break

            # Years of experience — numeric inputs labeled "experience"
            exp_inputs = await page.query_selector_all(
                'input[id*="experience"], input[name*="experience"]'
            )
            for inp in exp_inputs:
                val = await inp.input_value()
                if not val:
                    await inp.fill(str(config.EXPERIENCE_YEARS))
                    await asyncio.sleep(0.3)

            # Selects near "experience" labels
            exp_selects = await page.query_selector_all(
                'select[id*="experience"], select[name*="experience"]'
            )
            for sel in exp_selects:
                await sel.select_option(index=1)

            return True
        except Exception as exc:
            self.logger.warning(f"Form fill warning (non-fatal): {exc}")
            return True  # non-fatal — keep going

    async def handle_form_steps(self, page: Page) -> bool:
        """
        State machine for the multi-step Easy Apply form.
        Loops through Next → Review → Submit, max 5 steps.
        Returns True if application was submitted.
        """
        for step in range(5):
            await self._human_delay(3.0, 6.0)

            if await self._check_captcha(page):
                await self._take_screenshot(page, "captcha_in_form")
                raise RuntimeError("CAPTCHA detected during form — stopping")

            # Submit button — we're done
            submit_btn = await page.query_selector(
                'button[aria-label*="Submit application"], '
                'button[aria-label="Submit application"]'
            )
            if submit_btn:
                await self._human_delay(1.0, 2.0)
                await self._human_click(submit_btn)
                await asyncio.sleep(2.0)
                self.logger.info(f"Application submitted (step {step + 1})")
                return True

            # Review button
            review_btn = await page.query_selector(
                'button[aria-label*="Review"], button[aria-label="Review your application"]'
            )
            if review_btn:
                await self._human_click(review_btn)
                continue

            # Next button — fill current page first
            next_btn = await page.query_selector(
                'button[aria-label*="Continue to next step"], '
                'button[aria-label*="Next"]'
            )
            if next_btn:
                await self.fill_form_fields(page)
                await self._human_delay(1.0, 2.0)
                await self._human_click(next_btn)
                continue

            self.logger.error(f"No Next/Review/Submit button found on step {step + 1}")
            return False

        self.logger.error("Exceeded 5 form steps — something is wrong")
        return False

    # ── Single job application ─────────────────────────────────────────────────

    async def apply_to_job(self, job: Job) -> bool:
        """Full apply flow for one job. Returns True on success."""
        if db.get_daily_application_count() >= config.MAX_APPLICATIONS_PER_DAY:
            self.logger.warning(
                f"Daily cap ({config.MAX_APPLICATIONS_PER_DAY}) reached — skipping {job.title}"
            )
            return False

        page = await self.context.new_page()
        try:
            self.logger.info(f"Applying: {job.title} @ {job.company} (ID {job.id})")

            if not await self.navigate_to_job(page, job.url):
                db.update_job_status(job.id, "closed")
                return False

            if not await self.click_easy_apply(page):
                await self._take_screenshot(page, f"no_easyapply_{job.id}")
                return False

            # Upload resume if available
            if job.resume_path and job.resume_path != "failed":
                await self.upload_resume(page, job.resume_path)

            if not await self.handle_form_steps(page):
                await self._take_screenshot(page, f"form_fail_{job.id}")
                db.update_job_status(job.id, "failed")
                return False

            db.update_job_applied(job.id)
            self.logger.info(f"✓ Applied: {job.title} @ {job.company}")
            return True

        except RuntimeError as exc:
            # CAPTCHA — caller decides whether to stop the batch
            self.logger.error(str(exc))
            raise
        except Exception as exc:
            self.logger.error(f"Apply failed for job {job.id}: {exc}")
            await self._take_screenshot(page, f"error_{job.id}")
            db.update_job_status(job.id, "failed")
            return False
        finally:
            await page.close()

    # ── Batch application ──────────────────────────────────────────────────────

    async def apply_batch(self, jobs: list[Job]) -> dict:
        """Apply to a list of jobs sequentially with human-like gaps between them."""
        results = {"applied": 0, "failed": 0, "skipped": 0}
        consecutive_failures = 0

        for i, job in enumerate(jobs):
            if db.get_daily_application_count() >= config.MAX_APPLICATIONS_PER_DAY:
                self.logger.warning("Daily cap reached — stopping batch")
                results["skipped"] += len(jobs) - i
                break

            if consecutive_failures >= 3:
                self.logger.error("3 consecutive failures — stopping batch")
                results["skipped"] += len(jobs) - i
                break

            try:
                success = await self.apply_to_job(job)
                if success:
                    results["applied"] += 1
                    consecutive_failures = 0
                else:
                    results["failed"] += 1
                    consecutive_failures += 1
            except RuntimeError:
                # CAPTCHA — abort everything
                self.logger.error("CAPTCHA encountered — aborting batch")
                results["skipped"] += len(jobs) - i - 1
                break
            except Exception as exc:
                self.logger.error(f"Unexpected batch error: {exc}")
                results["failed"] += 1
                consecutive_failures += 1

            # Human-like gap between applications — 60-120 seconds
            if i < len(jobs) - 1:
                gap = random.uniform(60, 120)
                self.logger.info(f"Waiting {gap:.0f}s before next application…")
                await asyncio.sleep(gap)

        self.logger.info(f"Batch complete — {results}")
        return results

    # ── Teardown ───────────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        self.logger.info("Browser closed")


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    async def main() -> None:
        applicant = LinkedInApplicant()

        if "--single" in sys.argv:
            job_id = int(sys.argv[sys.argv.index("--single") + 1])
            job = db.get_job_by_id(job_id)
            if not job:
                console.print(f"[red]Job {job_id} not found[/red]")
                return
            if not job.resume_path or job.resume_path == "failed":
                console.print(f"[red]Job {job_id} has no ready resume[/red]")
                return
            console.print(f"[bold]Applying to:[/bold] {job.title} @ {job.company}")
            await applicant.setup_browser()
            success = await applicant.apply_to_job(job)
            console.print("[green]✓ Applied![/green]" if success else "[red]✗ Failed[/red]")
        else:
            jobs = db.get_approved_jobs_with_resume()
            if not jobs:
                console.print("[yellow]No approved jobs with resumes ready.[/yellow]")
                return
            console.print(f"[bold]Found {len(jobs)} job(s) ready to apply[/bold]")
            await applicant.setup_browser()
            results = await applicant.apply_batch(jobs)
            console.print(f"[green]Applied:  {results['applied']}[/green]")
            console.print(f"[red]Failed:   {results['failed']}[/red]")
            console.print(f"[yellow]Skipped:  {results['skipped']}[/yellow]")

        await applicant.close()

    asyncio.run(main())
