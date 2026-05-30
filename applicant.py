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

    async def _find_input_by_label(self, page: Page, *label_keywords) -> any:
        """Find an input field by searching for label text above it."""
        labels = await page.query_selector_all(
            '.jobs-easy-apply-modal label, '
            '[role="dialog"] label, '
            '.jobs-easy-apply-modal .fb-form-element, '
            '[role="dialog"] .fb-form-element'
        )
        for label in labels:
            label_text = (await label.inner_text()).lower()
            if any(kw.lower() in label_text for kw in label_keywords):
                for_attr = await label.get_attribute('for')
                if for_attr:
                    inp = await page.query_selector(f'#{for_attr}')
                    if inp:
                        return inp
                parent = await label.evaluate_handle(
                    'el => el.closest(".fb-form-element, .jobs-easy-apply-form-element")'
                )
                if parent:
                    inp = await parent.query_selector('input, select, textarea')
                    if inp:
                        return inp
        return None

    async def _select_option_by_label(self, page: Page, label_keywords: list,
                                       preferred_values: list) -> bool:
        """Find a select dropdown by label and pick the best matching option."""
        select = await self._find_input_by_label(page, *label_keywords)
        if not select:
            return False
        tag = await select.evaluate('el => el.tagName.toLowerCase()')
        if tag != 'select':
            return False
        options = await select.query_selector_all('option')
        option_texts = []
        for opt in options:
            txt = (await opt.inner_text()).strip().lower()
            val = await opt.get_attribute('value') or ''
            option_texts.append((txt, val, opt))
        for preferred in preferred_values:
            for txt, val, opt in option_texts:
                if preferred.lower() in txt or preferred.lower() == val.lower():
                    await select.select_option(value=val if val else txt)
                    self.logger.info(f"Selected dropdown option: {txt}")
                    return True
        for txt, val, opt in option_texts:
            if txt and txt != 'select an option' and val:
                await select.select_option(value=val)
                self.logger.info(f"Selected first available option: {txt}")
                return True
        return False

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

    async def navigate_to_job(self, page: Page, url: str) -> str | None:
        """Navigate to the job URL. Returns 'open', 'closed', 'applied', or None on error."""
        try:
            await page.goto(url, wait_until="load", timeout=30000)
            await self._human_delay(4.0, 6.0)

            # Check if we need to log in
            if await page.query_selector('input[name="session_key"]'):
                self.logger.warning("Not logged in — please log in in the browser window")
                input("Press Enter after logging in...")
                await page.goto(url, wait_until="load", timeout=30000)
                await self._human_delay(4.0, 6.0)

            if await self._check_captcha(page):
                await self._take_screenshot(page, "captcha")
                raise RuntimeError("CAPTCHA detected — stopping automation")

            page_lower = (await page.inner_text("body")).lower()

            # State 1: Job removed / invalid link
            if ("unable to load the page" in page_lower
                    or "job posting has been removed" in page_lower
                    or "not be valid" in page_lower):
                self.logger.info(f"Job removed/invalid: {url}")
                return "closed"

            # State 2: Already applied — check BEFORE expired (page can show both)
            if "application submitted" in page_lower:
                self.logger.info(f"Already applied to this job: {url}")
                return "applied"

            # State 3: No longer accepting
            if "no longer accepting" in page_lower or "no longer available" in page_lower:
                self.logger.info(f"Job closed: {url}")
                return "closed"

            await self._scroll_job_page(page)
            return "open"

        except RuntimeError:
            raise
        except Exception as exc:
            self.logger.error(f"Navigation failed: {exc}")
            return None

    # ── Easy Apply modal ───────────────────────────────────────────────────────

    async def click_easy_apply(self, page: Page) -> bool:
        """Find and click the Easy Apply button, wait for modal to open."""
        await self._human_delay(2.0, 3.0)

        # Wait for page to be interactive
        await self._human_delay(2.0, 4.0)

        # Try text-based selectors first — most resilient to LinkedIn UI changes
        text_selectors = [
            'button:has-text("Easy Apply")',
            '[aria-label*="Easy Apply"]',
            '[aria-label*="easy apply"]',
        ]

        # Then fall back to class-based selectors
        class_selectors = [
            'button.jobs-apply-button',
            '.jobs-s-apply button',
            '[data-control-name="jobdetails_topcard_inapply"]',
        ]

        all_selectors = text_selectors + class_selectors

        for sel in all_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    self.logger.info(f"Found Easy Apply button with selector: {sel}")
                    await btn.scroll_into_view_if_needed()
                    await self._human_delay(0.5, 1.5)
                    await self._human_click(btn)
                    modal_sel = (
                        'div.jobs-easy-apply-modal, '
                        'h2#jobs-apply-header, '
                        'div[data-test-modal-id="easy-apply-modal"]'
                    )
                    try:
                        await page.wait_for_selector(modal_sel, timeout=8000)
                        self.logger.info("Easy Apply modal opened")
                        return True
                    except Exception:
                        self.logger.info(f"Modal not found — retrying click with {sel}")
                        await page.evaluate("window.scrollTo(0, 0)")
                        await self._human_delay(1.5, 2.5)
                        btn2 = await page.query_selector(sel)
                        if btn2:
                            await btn2.scroll_into_view_if_needed()
                            await self._human_click(btn2)
                            try:
                                await page.wait_for_selector(modal_sel, timeout=8000)
                                self.logger.info("Easy Apply modal opened (retry)")
                                return True
                            except Exception:
                                pass
                        self.logger.debug(f"Modal did not appear after retry with {sel}")
                        continue
            except Exception as e:
                self.logger.debug(f"Selector {sel} error: {e}")
                continue

        # Last resort — dump all buttons on page to logs for debugging
        buttons = await page.query_selector_all('button')
        button_texts = []
        for b in buttons[:20]:
            txt = await b.inner_text()
            aria = await b.get_attribute('aria-label') or ''
            button_texts.append(f"text='{txt.strip()}' aria='{aria}'")
        self.logger.warning(f"Easy Apply button not found. Page buttons: {button_texts}")

        # Check if this is an external-apply job (valid but not Easy Apply)
        try:
            external_btn = await page.query_selector(
                "button.jobs-apply-button:not([aria-label*='Easy Apply']), "
                "a.jobs-apply-button, "
                "button[aria-label='Apply'], "
                "a[aria-label='Apply']"
            )
            page_body = (await page.inner_text("body")).lower()
            if external_btn or "managed off linkedin" in page_body:
                self.logger.info("External apply job detected — marking as manual")
                return "external"
        except Exception:
            pass

        await self._take_screenshot(page, "no_easyapply")
        return False

    async def upload_resume(self, page: Page, pdf_path: str) -> bool:
        """Upload the tailored PDF resume inside the Easy Apply modal."""
        try:
            await self._human_delay(1.0, 2.0)
            file_input = await page.query_selector('input[type="file"]')
            if not file_input:
                await page.evaluate("""
                    document.querySelectorAll('input[type="file"]').forEach(el => {
                        el.style.display = 'block';
                        el.style.opacity = '1';
                    });
                """)
                file_input = await page.query_selector('input[type="file"]')

            if file_input:
                await file_input.set_input_files(pdf_path)
                self.logger.info(f"Resume uploaded: {pdf_path}")
                await self._human_delay(2.0, 3.0)
                return True
            else:
                self.logger.warning("No file input found — skipping resume upload")
                return True  # Don't fail — LinkedIn may use previously uploaded resume
        except Exception as exc:
            self.logger.error(f"Resume upload failed: {exc}")
            return False

    async def fill_form_fields(self, page: Page) -> bool:
        """Fill common form fields conservatively. Skips anything unfamiliar."""
        await self._human_delay(1.0, 2.0)

        # --- Phone number ---
        phone_field = await page.query_selector(
            'input[id*="phoneNumber"], '
            'input[name*="phoneNumber"], '
            'input[aria-label*="Mobile phone"], '
            'input[aria-label*="Phone number"], '
            'input[aria-label*="phone"]'
        )
        if phone_field:
            await phone_field.scroll_into_view_if_needed()
            await self._human_click(phone_field)
            await phone_field.click(click_count=3)
            await phone_field.press("Backspace")
            await self._human_type(phone_field, "9884561353")
            await self._human_delay(0.5, 1.0)

        # --- Location (city) ---
        location_field = await page.query_selector(
            'input[aria-label*="ity"], '
            'input[aria-label*="ocation"], '
            'input[id*="location"], '
            'input[id*="city"]'
        )
        if location_field:
            val = await location_field.input_value()
            if not val.strip():
                await location_field.scroll_into_view_if_needed()
                await location_field.click(click_count=3)
                await location_field.press("Backspace")
                await self._human_type(location_field, "Bengaluru")
                await self._human_delay(2.0, 3.0)
                try:
                    await page.wait_for_selector(
                        '[role="option"], '
                        '.basic-typeahead__selectable, '
                        'li[data-value], '
                        '.search-typeahead-v2__hit',
                        timeout=5000
                    )
                    first_option = await page.query_selector(
                        '[role="option"], '
                        '.basic-typeahead__selectable, '
                        'li[data-value], '
                        '.search-typeahead-v2__hit'
                    )
                    if first_option:
                        await self._human_click(first_option)
                        self.logger.info("Selected location from dropdown")
                        await self._human_delay(0.5, 1.0)
                    else:
                        await location_field.press("Enter")
                except Exception:
                    await location_field.press("Enter")
                    self.logger.warning("Location dropdown did not appear — used typed value")

        # --- Current CTC ---
        current_ctc = await self._find_input_by_label(
            page, "current ctc", "current salary", "current compensation"
        )
        if current_ctc:
            val = await current_ctc.input_value()
            if not val.strip():
                await current_ctc.scroll_into_view_if_needed()
                await current_ctc.click(click_count=3)
                await self._human_type(current_ctc, str(config.CURRENT_CTC))
                self.logger.info("Filled current CTC")

        # --- Expected CTC ---
        expected_ctc = await self._find_input_by_label(
            page, "expected ctc", "expected salary", "expected compensation",
            "desired salary"
        )
        if expected_ctc:
            val = await expected_ctc.input_value()
            if not val.strip():
                await expected_ctc.scroll_into_view_if_needed()
                await expected_ctc.click(click_count=3)
                await self._human_type(expected_ctc, str(config.EXPECTED_CTC))
                self.logger.info("Filled expected CTC")

        # --- Notice period ---
        notice = await self._find_input_by_label(
            page, "notice period", "notice", "joining"
        )
        if notice:
            val = await notice.input_value()
            if not val.strip():
                await notice.scroll_into_view_if_needed()
                await notice.click(click_count=3)
                await self._human_type(notice, str(config.NOTICE_PERIOD_DAYS))
                self.logger.info("Filled notice period")

        # --- Years of experience dropdown ---
        await self._select_option_by_label(
            page,
            ["years of professional experience", "years of experience", "total years"],
            ["0", "less than 1", "0-1", "fresher", "1"]
        )

        # --- Months of experience dropdown ---
        await self._select_option_by_label(
            page,
            ["months of experience", "additional months", "total months"],
            ["0", "less than 6", "0-6", "none"]
        )

        # --- Work authorization dropdown ---
        await self._select_option_by_label(
            page,
            ["authorized", "eligible to work", "work authorization", "legally authorized"],
            ["yes", "true", "1", "authorized"]
        )

        # --- Gender dropdown ---
        await self._select_option_by_label(
            page,
            ["gender"],
            ["male", "prefer not", "not to say"]
        )

        # --- Fieldset/legend-based radio questions (LinkedIn screening questions) ---
        # LinkedIn puts "Are you currently a student?" etc. in <fieldset><legend> — not <label>
        try:
            fieldsets = await page.query_selector_all(
                '.jobs-easy-apply-modal fieldset, [role="dialog"] fieldset'
            )
            for fieldset in fieldsets:
                legend = await fieldset.query_selector('legend')
                legend_text = (await legend.inner_text()).lower().strip() if legend else ''

                radios = await fieldset.query_selector_all('input[type="radio"]')
                if not radios:
                    continue

                any_checked = False
                for r in radios:
                    if await r.is_checked():
                        any_checked = True
                        break
                if any_checked:
                    continue

                if not legend_text:
                    self.logger.info("Field group has no label — applying type-based fallback")

                await self._human_click(radios[0])
                self.logger.info(
                    f"Selected first radio for: '{legend_text or 'unlabeled fieldset'}'"
                )
                await self._human_delay(0.3, 0.7)
        except Exception as exc:
            self.logger.warning(f"Fieldset radio handling warning: {exc}")

        # --- Work authorization radios (aria-label based fallback) ---
        try:
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
        except Exception as exc:
            self.logger.warning(f"Radio fill warning (non-fatal): {exc}")

        # --- Years of experience ---
        try:
            exp_inputs = await page.query_selector_all(
                'input[id*="experience"], input[name*="experience"]'
            )
            for inp in exp_inputs:
                val = await inp.input_value()
                if not val:
                    await inp.fill(str(config.EXPERIENCE_YEARS))
                    await asyncio.sleep(0.3)

            exp_selects = await page.query_selector_all(
                'select[id*="experience"], select[name*="experience"]'
            )
            for sel in exp_selects:
                await sel.select_option(index=1)
        except Exception as exc:
            self.logger.warning(f"Experience fill warning (non-fatal): {exc}")

        # --- Log any remaining empty required text/number inputs (excludes radios/checkboxes) ---
        required_inputs = await page.query_selector_all(
            'input[required]:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]), '
            'input[aria-required="true"]:not([type="hidden"]):not([type="radio"]):not([type="checkbox"])'
        )
        for inp in required_inputs:
            val = await inp.input_value()
            if not val.strip():
                label = await inp.get_attribute('aria-label') or ''
                placeholder = await inp.get_attribute('placeholder') or ''
                self.logger.warning(
                    f"Unfilled required field: label='{label}' placeholder='{placeholder}'"
                )

        return True

    async def handle_form_steps(self, page: Page) -> bool:
        """
        State machine for the multi-step Easy Apply form.
        Loops through Next → Review → Submit, max 8 steps.
        Only increments step counter when form content actually changes.
        Returns True if application was submitted.
        """
        max_steps = 8
        step = 0
        review_retries = 0

        while step < max_steps:
            await self._human_delay(2.0, 4.0)

            if await self._check_captcha(page):
                await self._take_screenshot(page, "captcha_in_form")
                raise RuntimeError("CAPTCHA detected during form — stopping")

            # Fill fields on the current page
            await self.fill_form_fields(page)
            await self._human_delay(1.0, 2.0)

            # Submit button — we're done
            submit_btn = await page.query_selector(
                'button[aria-label*="Submit application"], '
                'button:has-text("Submit application")'
            )
            if submit_btn:
                self.logger.info("Found Submit button — submitting application")
                await self._human_click(submit_btn)
                await self._human_delay(2.0, 3.0)
                return True

            # Review button
            review_btn = await page.query_selector(
                'button[aria-label*="Review"], '
                'button:has-text("Review")'
            )
            if review_btn:
                if review_retries > 2:
                    self.logger.warning(
                        "Required fields could not be filled — proceeding anyway"
                    )
                    # Fall through to Next button check below
                else:
                    try:
                        current_content = await page.inner_text(
                            '.jobs-easy-apply-modal, [role="dialog"]'
                        )
                    except Exception:
                        current_content = ""

                    self.logger.info(f"Clicking Review button (attempt {review_retries + 1})")
                    await self._human_click(review_btn)
                    await self._human_delay(2.0, 3.0)

                    try:
                        new_content = await page.inner_text(
                            '.jobs-easy-apply-modal, [role="dialog"]'
                        )
                    except Exception:
                        new_content = ""

                    if new_content and new_content == current_content:
                        review_retries += 1
                        self.logger.warning(
                            f"Review did not advance form (attempt {review_retries})"
                        )
                        await self.fill_form_fields(page)
                        continue
                    else:
                        review_retries = 0
                        step += 1
                        continue

            # Next button
            next_btn = await page.query_selector(
                'button[aria-label*="Continue"], '
                'button:has-text("Next")'
            )
            if next_btn:
                # Check for validation errors before advancing
                errors = await page.query_selector_all(
                    '[data-test="inline-error"], '
                    '.fb-form-element__error-text, '
                    'p.artdeco-inline-feedback--error, '
                    '[aria-live="assertive"]'
                )
                if errors:
                    error_texts = []
                    for e in errors:
                        txt = await e.inner_text()
                        if txt.strip():
                            error_texts.append(txt.strip())
                    if error_texts:
                        self.logger.warning(f"Validation errors on form: {error_texts}")
                        await self.fill_form_fields(page)
                        await self._human_delay(1.0, 2.0)

                # Capture content before clicking to detect if we advanced
                try:
                    current_content = await page.inner_text(
                        '.jobs-easy-apply-modal, [role="dialog"]'
                    )
                except Exception:
                    current_content = ""

                self.logger.info(f"Clicking Next (step {step + 1})")
                await self._human_click(next_btn)
                await self._human_delay(2.0, 3.0)

                try:
                    new_content = await page.inner_text(
                        '.jobs-easy-apply-modal, [role="dialog"]'
                    )
                except Exception:
                    new_content = ""

                if new_content and new_content == current_content:
                    self.logger.warning("Form did not advance — likely a validation error")
                    await self._take_screenshot(page, f"stuck_form_step{step}")
                    return False

                step += 1
                continue

            self.logger.error(f"No Next/Review/Submit button found on step {step + 1}")
            await self._take_screenshot(page, f"no_button_step{step}")
            return False

        self.logger.error(f"Exceeded {max_steps} form steps")
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

            nav_result = await self.navigate_to_job(page, job.url)
            if nav_result != "open":
                db.update_job_status(job.id, nav_result or "closed")
                return False

            # Dismiss LinkedIn messaging overlay if open
            try:
                for close_sel in [
                    "button[aria-label='Dismiss']",
                    ".msg-overlay-bubble-header__control--close-btn",
                ]:
                    close_btn = page.locator(close_sel)
                    if await close_btn.count() > 0:
                        await close_btn.first.click()
                        await self._human_delay(1.0, 2.0)
                        self.logger.info("Dismissed messaging overlay")
                        break
            except Exception:
                pass

            easy_apply_result = await self.click_easy_apply(page)
            if easy_apply_result == "external":
                db.update_job_status(job.id, "manual")
                self.logger.info(f"Marked as manual apply: {job.title} @ {job.company}")
                return False
            elif not easy_apply_result:
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
