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
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from rich.console import Console

import config
import db
from models import Job
from qa_resolver import QAResolver
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
        self.qa_resolver = QAResolver()
        self._pending_fields: list[dict] = []
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

    async def _page_confirms_closed(self, page: Page, page_lower: str | None = None) -> bool:
        """
        H2 fix (Remediation Session 2): the single canonical answer to "is this
        job confirmed closed by LinkedIn itself". Returns True ONLY when page
        content (or a redirect to the generic jobs page) explicitly confirms
        the job is gone. Never call this on a page that failed to load — only
        after a successful page.goto().

        Pass a pre-lowercased page_lower to skip the internal inner_text()
        fetch (navigate_to_job() already has one computed); omit it to have
        this method fetch its own — used when calling with just a bare page.

        Phrase list is the union of what applicant.py's own inline checks and
        validator.py's independently-calibrated _detect_page_state() already
        use in production — kept broad deliberately; narrowing either set
        risks missing a real closed-job signal.
        """
        try:
            if page_lower is None:
                page_lower = (await page.inner_text("body")).lower()

            closed_phrases = (
                "unable to load the page",
                "job posting has been removed",
                "not be valid",
                "no longer accepting",
                "no longer available",
            )
            if any(phrase in page_lower for phrase in closed_phrases):
                return True

            # Redirect to the generic jobs/search page = job gone.
            if page.url.rstrip("/") in (
                "https://www.linkedin.com/jobs",
                "https://www.linkedin.com/jobs/search",
            ):
                return True
        except Exception:
            pass
        return False

    async def _take_screenshot(self, page: Page, name: str) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"logs/screenshots/{name}_{ts}.png"
        await page.screenshot(path=path)
        self.logger.info(f"Screenshot saved: {path}")

    async def _dismiss_modal(self, page: Page) -> None:
        """
        Closes the Easy Apply modal by clicking the X/Dismiss button.
        Tries known selectors in order; logs a warning if none found.
        """
        selectors = [
            'button[aria-label="Dismiss"]',
            'button.artdeco-modal__dismiss',
            'button[data-test-modal-close-btn]',
        ]
        for sel in selectors:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await self._human_click(btn)
                    await asyncio.sleep(1.0)
                    self.logger.info("Modal dismissed")
                    return
            except Exception:
                continue
        self.logger.warning("Could not find modal dismiss button — modal may still be open")

    # ── Navigation ─────────────────────────────────────────────────────────────

    async def navigate_to_job(self, page: Page, url: str) -> str:
        """Navigate to the job URL. Returns 'open', 'closed', 'applied', or 'failed'.

        H2 fix (Remediation Session 2): every error path returns 'failed', never
        None — a navigation/network error is not the same claim as LinkedIn
        confirming the job is gone (see _page_confirms_closed()), and the two
        must never be conflated (a 'closed' status makes the job eligible for
        PDF-deleting purge; 'failed' is recoverable and retried).
        """
        try:
            await page.goto(url, wait_until="load", timeout=30000)
            await self._human_delay(4.0, 6.0)

            # Check if we need to log in
            if await page.query_selector('input[name="session_key"]'):
                self.logger.warning("Not logged in — please log in in the browser window")
                # H3 fix: input() raises EOFError with no stdin TTY (subprocess
                # context) — guard it and fail loudly instead.
                if sys.stdin.isatty():
                    input("Press Enter after logging in...")
                else:
                    self.logger.error(
                        "LinkedIn session not logged in and running in subprocess context "
                        "(stdin is not a TTY). Manual login is not possible here. "
                        "Run directly from a terminal to log in first: "
                        "python applicant.py --single <job_id>"
                    )
                    raise RuntimeError(
                        "Not logged in and no TTY available — cannot pause for manual login"
                    )
                await page.goto(url, wait_until="load", timeout=30000)
                await self._human_delay(4.0, 6.0)

            if await self._check_captcha(page):
                await self._take_screenshot(page, "captcha")
                raise RuntimeError("CAPTCHA detected — stopping automation")

            page_lower = (await page.inner_text("body")).lower()

            # Already applied — check BEFORE closed (page can show both)
            if "application submitted" in page_lower:
                self.logger.info(f"Already applied to this job: {url}")
                return "applied"

            if await self._page_confirms_closed(page, page_lower):
                self.logger.info(f"Job confirmed closed by page content: {url}")
                return "closed"

            await self._scroll_job_page(page)
            return "open"

        except RuntimeError:
            raise
        except PlaywrightTimeoutError as exc:
            self.logger.warning(f"Navigation timeout for {url}: {exc} — network issue, not closed")
            return "failed"
        except Exception as exc:
            self.logger.error(f"Navigation failed: {exc}")
            return "failed"

    # ── Easy Apply modal ───────────────────────────────────────────────────────

    # Signals that the Easy Apply modal is open. `dialog[data-testid="dialog"]`
    # is the confirmed current markup (captured from a live Playwright trace on
    # 2026-07-26 — LinkedIn now renders Easy Apply as a native <dialog> element,
    # not the old div.jobs-easy-apply-modal). Older selectors are kept as
    # fallback in case LinkedIn A/B tests a different variant.
    MODAL_SELECTORS = (
        'dialog[data-testid="dialog"], '
        'dialog[aria-labelledby="dialog-header"], '
        'div.jobs-easy-apply-modal, '
        'div[data-test-modal-id="easy-apply-modal"], '
        'h2#jobs-apply-header, '
        'div.artdeco-modal[role="dialog"], '
        'div[aria-labelledby="jobs-apply-header"], '
        'div[role="dialog"] button[aria-label="Continue to next step"], '
        'div[role="dialog"] button[aria-label="Submit application"], '
        'div[role="dialog"] button[aria-label="Review your application"]'
    )

    # Container selector used to read the modal's text content, to detect
    # whether clicking Next/Review actually advanced the form.
    MODAL_CONTENT_SELECTOR = (
        'dialog[data-testid="dialog"], '
        'dialog[aria-labelledby="dialog-header"], '
        '.jobs-easy-apply-modal, '
        '[role="dialog"]'
    )

    # Fieldsets (radio/checkbox groups) must be scoped to the same modal
    # container as MODAL_CONTENT_SELECTOR — previously scoped only to
    # `.jobs-easy-apply-modal fieldset, [role="dialog"] fieldset`, which
    # silently matched zero fieldsets against the native <dialog> markup
    # (Session 10.1: this is why a required radio group was never even
    # logged, let alone filled).
    FIELDSET_SCOPE_SELECTOR = (
        'dialog[data-testid="dialog"] fieldset, '
        'dialog[aria-labelledby="dialog-header"] fieldset, '
        '.jobs-easy-apply-modal fieldset, '
        '[role="dialog"] fieldset'
    )

    async def _fieldset_is_required(self, fieldset) -> bool:
        """
        True if a radio/checkbox fieldset is required. LinkedIn marks this
        inconsistently, so every reliable signal is OR'd together:
        asterisk in the legend, aria-required or data-test-form-element-required
        on the fieldset, a required child input, or a hidden 'Required' span.
        """
        try:
            legend = await fieldset.query_selector('legend')
            legend_text = (await legend.inner_text()).strip() if legend else ''
            if '*' in legend_text:
                return True

            aria_required = await fieldset.get_attribute('aria-required')
            if aria_required and aria_required.lower() == 'true':
                return True

            data_required = await fieldset.get_attribute('data-test-form-element-required')
            if data_required and data_required.lower() == 'true':
                return True

            if await fieldset.query_selector('input[required]'):
                return True

            if await fieldset.query_selector('span:text-is("Required")'):
                return True
        except Exception:
            pass
        return False

    async def _modal_is_open(self, page: Page) -> bool:
        """True if an Easy Apply modal is currently open on the page."""
        try:
            el = await page.query_selector(self.MODAL_SELECTORS)
            return el is not None
        except Exception:
            return False

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

        modal_sel = self.MODAL_SELECTORS

        for sel in all_selectors:
            try:
                # If a previous click already opened the modal, don't click again —
                # a second click lands on the Apply button behind the overlay.
                if await self._modal_is_open(page):
                    self.logger.info("Easy Apply modal already open")
                    return True

                btn = await page.query_selector(sel)
                if btn:
                    self.logger.info(f"Found Easy Apply button with selector: {sel}")
                    await btn.scroll_into_view_if_needed()
                    await self._human_delay(0.5, 1.5)
                    await self._human_click(btn)
                    try:
                        await page.wait_for_selector(modal_sel, timeout=8000)
                        self.logger.info("Easy Apply modal opened")
                        return True
                    except Exception:
                        self.logger.info(f"Modal not found — retrying click with {sel}")
                        await page.evaluate("window.scrollTo(0, 0)")
                        await self._human_delay(1.5, 2.5)
                        if await self._modal_is_open(page):
                            self.logger.info("Easy Apply modal opened (detected after retry wait)")
                            return True
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

        # Final safety net: the button may have opened the modal even though
        # our post-click waits missed it (the exact failure in the 2026-07-26 run).
        if await self._modal_is_open(page):
            self.logger.info("Easy Apply modal open (detected on final check)")
            return True

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

    async def upload_resume(self, page: Page, pdf_path: str) -> bool | None:
        """
        Upload the tailored PDF resume inside the Easy Apply modal, if a file
        input is present on the current step.

        H1 fix (Remediation Session 2) — tristate contract:
          True  — file input found AND upload confirmed
          False — file input found BUT upload could not be confirmed (genuine failure)
          None  — no file input on this step (nothing to do here — not an error)
        Callers must fail the job on False, continue normally on None, and
        continue to Submit/Next on True. The old behavior returned True on a
        miss, which made the caller falsely believe the upload had succeeded.
        """
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

            if not file_input:
                self.logger.info("No file input on this step — skipping resume upload")
                return None

            await file_input.set_input_files(pdf_path)
            await self._human_delay(2.0, 3.0)

            # Confirm the upload actually took. VERIFIED against a live
            # single-job test (job 567, screenshot
            # upload_fail_step2_20260823_174003.png): LinkedIn's real Resume
            # step is a resume PICKER, not a blank upload target — once any
            # resume has ever been uploaded, it shows a library of resume
            # cards and re-renders that section after a successful upload.
            # That re-render can leave the original <input type="file">
            # element's own value empty/stale even though the upload
            # genuinely succeeded — checking input_value() alone produced a
            # false negative there (job 567 failed with a confirmed-good
            # upload sitting selected in the modal). The uploaded filename
            # appearing as visible text in the modal (every resume card shows
            # its filename) is the reliable signal for that flow.
            # input_value() is kept as a fallback second signal for a
            # simpler first-ever-upload variant that may not use the picker
            # UI (still unverified — no live test has hit that path yet).
            filename = Path(pdf_path).name
            try:
                modal_text = await page.inner_text(self.MODAL_CONTENT_SELECTOR)
            except Exception:
                modal_text = ""

            if filename in modal_text:
                self.logger.info(f"Resume upload confirmed (filename visible in modal): {pdf_path}")
                return True

            input_value = await file_input.input_value()
            if input_value.strip():
                self.logger.info(f"Resume upload confirmed (input value set): {pdf_path}")
                return True

            self.logger.warning(f"Resume uploaded but not confirmed by either signal: {pdf_path}")
            return False
        except Exception as exc:
            self.logger.error(f"Resume upload failed: {exc}")
            return False

    # ── Q&A resolver fill primitives ────────────────────────────────────────
    # These determine HOW a field gets filled once qa_resolver has decided
    # WHAT to fill it with. Frozen shape — resolver logic lives in qa_resolver.py.

    async def _get_field_label(self, page: Page, field) -> str:
        """Best-effort label lookup: aria-label, then <label for=id>, then placeholder."""
        aria = await field.get_attribute('aria-label')
        if aria and aria.strip():
            return aria.strip()
        field_id = await field.get_attribute('id')
        if field_id:
            label_el = await page.query_selector(f'label[for="{field_id}"]')
            if label_el:
                text = (await label_el.inner_text()).strip()
                if text:
                    return text
        placeholder = await field.get_attribute('placeholder')
        if placeholder and placeholder.strip():
            return placeholder.strip()
        return "Unlabeled field"

    async def _fill_text_input(self, field, value: str) -> None:
        """Clears (select-all + Backspace) and types a resolved answer into a text/number input."""
        await field.scroll_into_view_if_needed()
        await self._human_click(field)
        await field.click(click_count=3)
        await field.press("Backspace")
        await self._human_type(field, value)
        await self._human_delay(0.5, 1.0)

    async def _get_select_options(self, select) -> list[str]:
        options = await select.query_selector_all('option')
        texts = []
        for opt in options:
            txt = (await opt.inner_text()).strip()
            if txt and txt.lower() not in ('select an option', ''):
                texts.append(txt)
        return texts

    async def _fill_select(self, select, answer: str) -> bool:
        """Selects the option whose text best matches `answer` (case-insensitive substring)."""
        options = await select.query_selector_all('option')
        answer_lower = answer.lower()
        for opt in options:
            txt = (await opt.inner_text()).strip()
            val = await opt.get_attribute('value') or ''
            txt_lower = txt.lower()
            if txt_lower and (txt_lower == answer_lower or answer_lower in txt_lower or txt_lower in answer_lower):
                await select.select_option(value=val if val else txt)
                self.logger.info(f"Selected dropdown option: {txt}")
                return True
        return False

    async def _get_radio_option_label(self, radio) -> str:
        return await radio.evaluate(
            'el => {'
            '  const lbl = el.closest("label") || document.querySelector(`label[for="${el.id}"]`);'
            '  return (lbl ? lbl.textContent : (el.getAttribute("aria-label") || "")).trim();'
            '}'
        )

    async def _click_matching_radio(self, radios, labels: list[str], answer: str) -> bool:
        answer_lower = answer.lower()
        for radio, lbl in zip(radios, labels):
            lbl_lower = lbl.lower()
            if lbl_lower and (lbl_lower == answer_lower or answer_lower in lbl_lower or lbl_lower in answer_lower):
                await self._human_click(radio)
                return True
        return False

    async def fill_form_fields(self, page: Page) -> None:
        """
        Fill form fields on the current step.
        Required fields — input[required]/[aria-required], select[required]/
        [aria-required], and legend-bearing radio fieldsets — are resolved via
        the four-tier QAResolver cascade (see qa_resolver.py). Any required
        field the resolver can't confidently answer is appended to
        self._pending_fields instead of being left blank; the caller
        (handle_form_steps / apply_to_job) checks that list and parks the job
        as pending_questions rather than looping to MAX_STEPS.
        Optional fields that can't be resolved are left blank — unchanged
        from before this session.
        """
        await self._human_delay(1.0, 2.0)

        # --- Location (city) --- unrelated to Q&A resolution, unchanged
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
                await self._human_type(location_field, config.JOB_SEARCH_LOCATION)
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

        # --- Months of experience dropdown --- unrelated to Q&A resolution, unchanged
        await self._select_option_by_label(
            page,
            ["months of experience", "additional months", "total months"],
            ["0", "less than 6", "0-6", "none"]
        )

        # --- Gender dropdown --- unrelated to Q&A resolution, unchanged
        await self._select_option_by_label(
            page,
            ["gender"],
            ["male", "prefer not", "not to say"]
        )

        # --- Required text/number inputs -> four-tier resolver ---
        required_inputs = await page.query_selector_all(
            'input[required]:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]), '
            'input[aria-required="true"]:not([type="hidden"]):not([type="radio"]):not([type="checkbox"])'
        )
        for inp in required_inputs:
            val = await inp.input_value()
            if val.strip():
                continue
            label = await self._get_field_label(page, inp)
            field_type = await inp.get_attribute('type') or 'text'
            resolved = await self.qa_resolver.resolve_field(label, field_type, None)
            if resolved.resolved:
                await self._fill_text_input(inp, resolved.answer)
                self.logger.info(f"Filled required field '{label}' ({resolved.source})")
            else:
                self._pending_fields.append({
                    "label": label, "field_type": field_type, "options": None,
                    "ai_suggested_answer": resolved.ai_suggestion,
                    "ai_confidence": resolved.ai_confidence,
                })

        # --- Required select dropdowns -> four-tier resolver ---
        required_selects = await page.query_selector_all(
            'select[required], select[aria-required="true"]'
        )
        for sel in required_selects:
            current_value = await sel.evaluate('el => el.value')
            if current_value:
                continue
            label = await self._get_field_label(page, sel)
            options = await self._get_select_options(sel)
            resolved = await self.qa_resolver.resolve_field(label, "select", options)
            filled = False
            if resolved.resolved:
                filled = await self._fill_select(sel, resolved.answer)
                if filled:
                    self.logger.info(f"Filled required dropdown '{label}' ({resolved.source})")
            if not filled:
                self._pending_fields.append({
                    "label": label, "field_type": "select", "options": options,
                    "ai_suggested_answer": resolved.ai_suggestion,
                    "ai_confidence": resolved.ai_confidence,
                })

        # --- Radio/checkbox fieldsets (LinkedIn screening questions) -> four-tier resolver ---
        # LinkedIn puts "Are you currently a student?" etc. in <fieldset><legend> — not
        # <label>. Session 10.1: a fieldset is only in scope for the resolver if
        # _fieldset_is_required() finds a real required signal (asterisk, aria-required,
        # data-test-form-element-required, a required child input, or a hidden
        # "Required" span) — non-required groups are left untouched, same as any
        # other optional field.
        try:
            fieldsets = await page.query_selector_all(self.FIELDSET_SCOPE_SELECTOR)
            for fieldset in fieldsets:
                radios = await fieldset.query_selector_all('input[type="radio"]')
                checkboxes = [] if radios else await fieldset.query_selector_all('input[type="checkbox"]')
                inputs = radios or checkboxes
                if not inputs:
                    continue
                field_type = "radio" if radios else "checkbox"

                any_checked = False
                for inp in inputs:
                    if await inp.is_checked():
                        any_checked = True
                        break
                if any_checked:
                    continue

                if not await self._fieldset_is_required(fieldset):
                    continue

                legend = await fieldset.query_selector('legend')
                legend_text = (await legend.inner_text()).strip() if legend else ''

                if not legend_text:
                    # No question text to resolve against — preserve the old
                    # best-effort fallback rather than leaving it blank.
                    self.logger.info("Field group has no label — applying type-based fallback")
                    await self._human_click(inputs[0])
                    await self._human_delay(0.3, 0.7)
                    continue

                option_labels = [await self._get_radio_option_label(inp) for inp in inputs]
                resolved = await self.qa_resolver.resolve_field(legend_text, field_type, option_labels)
                filled = False
                if resolved.resolved:
                    filled = await self._click_matching_radio(inputs, option_labels, resolved.answer)
                    if filled:
                        self.logger.info(
                            f"Selected {field_type} '{legend_text}' -> '{resolved.answer}' ({resolved.source})"
                        )
                if not filled:
                    self._pending_fields.append({
                        "label": legend_text, "field_type": field_type, "options": option_labels,
                        "ai_suggested_answer": resolved.ai_suggestion,
                        "ai_confidence": resolved.ai_confidence,
                    })
                await self._human_delay(0.3, 0.7)
        except Exception as exc:
            self.logger.warning(f"Fieldset radio handling warning: {exc}")

    async def _get_current_step(self, page: Page) -> int | None:
        """
        Returns the current step number from the Easy Apply modal's step
        indicator ("Step X of Y"), or None if no indicator is found/parseable.

        UNVERIFIED against a live LinkedIn modal — the selectors below are
        best-effort guesses (Remediation Session 2 had no live session to
        confirm them against). Safe to ship regardless: every caller falls
        back to the pre-existing text-diff detection whenever this returns
        None, so a wrong/no-match selector just means the old behavior
        applies — never a new failure mode. Flagged for the live single-job
        test to confirm or correct.
        """
        try:
            selectors = [
                'h3.t-16',                              # "Step 1 of 3"
                '[aria-label*="Step"]',                  # aria-label="Step 2 of 3"
                '.artdeco-modal__header h2',
                'span:has-text("Step")',
            ]
            for sel in selectors:
                el = await page.query_selector(sel)
                if el:
                    text = await el.inner_text()
                    match = re.search(r'Step\s+(\d+)\s+of\s+\d+', text, re.IGNORECASE)
                    if match:
                        return int(match.group(1))
        except Exception:
            pass
        return None

    async def handle_form_steps(self, page: Page, pdf_path: str | None = None) -> str:
        """
        State machine for the multi-step Easy Apply form.
        Loops through Next → Review → Submit, max 8 steps.
        Only increments step counter when form content actually changes.
        Returns "submitted", "failed", or "pending" — "pending" means
        fill_form_fields() queued one or more required fields it couldn't
        resolve (see self._pending_fields); the caller parks the job as
        pending_questions instead of retrying to MAX_STEPS.

        H1 fix (Remediation Session 2): attempts resume upload on every step
        (not just the first, via a standalone pre-loop call as before) — the
        file input may not appear until step 2+. No-ops when pdf_path is
        falsy/"failed", matching the old guard that lived in apply_to_job().
        """
        max_steps = 8
        step = 0
        review_retries = 0

        while step < max_steps:
            await self._human_delay(2.0, 4.0)

            if await self._check_captcha(page):
                await self._take_screenshot(page, "captcha_in_form")
                raise RuntimeError("CAPTCHA detected during form — stopping")

            # Attempt resume upload on every step (H1 fix). upload_resume()
            # returns True (uploaded+confirmed), None (no file input on this
            # step — fine), or False (file input present but upload could
            # not be confirmed — a genuine failure).
            if pdf_path and pdf_path != "failed":
                upload_result = await self.upload_resume(page, pdf_path)
                if upload_result is False:
                    self.logger.error(f"Resume upload failed on step {step + 1}")
                    await self._take_screenshot(page, f"upload_fail_step{step + 1}")
                    return "failed"

            # Fill fields on the current page
            await self.fill_form_fields(page)
            if self._pending_fields:
                return "pending"
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
                return "submitted"

            # Review button
            review_btn = await page.query_selector(
                'button[aria-label*="Review"], '
                'button:has-text("Review")'
            )
            if review_btn:
                if review_retries >= 2:
                    # Task 8 (LOW fix): the Review step is a hard boundary —
                    # only Submit exists here, never Next. The old code fell
                    # through to look for Next, which could never succeed;
                    # fail directly instead of that misleading detour.
                    self.logger.error(
                        f"Review did not advance the form after {review_retries} attempts "
                        f"(step {step + 1}). Not looking for Next — it cannot exist on a "
                        f"Review step. Treating as a genuine failure."
                    )
                    await self._take_screenshot(page, f"review_fail_step{step}")
                    return "failed"

                # §2a fix: step-indicator is the primary advance signal —
                # immune to inline validation-error text changes that used to
                # cause a false "advanced" read from the text diff alone.
                step_before = await self._get_current_step(page)
                try:
                    current_content = await page.inner_text(
                        self.MODAL_CONTENT_SELECTOR
                    )
                except Exception:
                    current_content = ""

                self.logger.info(f"Clicking Review button (attempt {review_retries + 1})")
                await self._human_click(review_btn)
                await self._human_delay(2.0, 3.0)

                if step_before is not None:
                    step_after = None
                    for _ in range(6):
                        step_after = await self._get_current_step(page)
                        if step_after != step_before:
                            break
                        await asyncio.sleep(0.5)
                    advanced = step_after is not None and step_after != step_before
                    if advanced:
                        self.logger.info(f"Form advanced: step {step_before} → {step_after}")
                else:
                    # Step indicator not found — fall back to the pre-existing
                    # text-diff detection (unchanged behavior for modals where
                    # the step number isn't parseable).
                    try:
                        new_content = await page.inner_text(
                            self.MODAL_CONTENT_SELECTOR
                        )
                    except Exception:
                        new_content = ""
                    advanced = not (new_content and new_content == current_content)

                if not advanced:
                    review_retries += 1
                    self.logger.warning(
                        f"Review did not advance form (attempt {review_retries})"
                    )
                    await self.fill_form_fields(page)
                    if self._pending_fields:
                        return "pending"
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
                        if self._pending_fields:
                            return "pending"
                        await self._human_delay(1.0, 2.0)

                # Capture step/content before clicking to detect if we advanced.
                # §2a fix: step-indicator is primary (immune to inline
                # validation-error text changes); text-diff is the fallback
                # when no indicator is found.
                step_before = await self._get_current_step(page)
                try:
                    current_content = await page.inner_text(
                        self.MODAL_CONTENT_SELECTOR
                    )
                except Exception:
                    current_content = ""

                self.logger.info(f"Clicking Next (step {step + 1})")
                await self._human_click(next_btn)
                await self._human_delay(2.0, 3.0)

                step_after = None
                if step_before is not None:
                    for _ in range(6):
                        step_after = await self._get_current_step(page)
                        if step_after != step_before:
                            break
                        await asyncio.sleep(0.5)
                    advanced = step_after is not None and step_after != step_before
                    if advanced:
                        self.logger.info(f"Form advanced: step {step_before} → {step_after}")
                else:
                    try:
                        new_content = await page.inner_text(
                            self.MODAL_CONTENT_SELECTOR
                        )
                    except Exception:
                        new_content = ""
                    advanced = not (new_content and new_content == current_content)

                if not advanced:
                    self.logger.warning(
                        f"Form did not advance after clicking Next "
                        f"(step {step_before} → {step_after}) — likely a validation error"
                    )
                    await self._take_screenshot(page, f"stuck_form_step{step}")
                    return "failed"

                step += 1
                continue

            self.logger.error(f"No Next/Review/Submit button found on step {step + 1}")
            await self._take_screenshot(page, f"no_button_step{step}")
            return "failed"

        self.logger.error(f"Exceeded {max_steps} form steps")
        return "failed"

    # ── Single job application ─────────────────────────────────────────────────

    async def apply_to_job(self, job: Job) -> str:
        """
        Full apply flow for one job. Returns "applied", "pending_questions",
        "failed", or "closed".
        """
        self._pending_fields = []  # reset per-job accumulator

        if db.get_daily_application_count() >= config.MAX_APPLICATIONS_PER_DAY:
            self.logger.warning(
                f"Daily cap ({config.MAX_APPLICATIONS_PER_DAY}) reached — skipping {job.title}"
            )
            return "failed"

        page = await self.context.new_page()
        try:
            self.logger.info(f"Applying: {job.title} @ {job.company} (ID {job.id})")

            nav_result = await self.navigate_to_job(page, job.url)
            if nav_result != "open":
                # H2 fix: navigate_to_job() now always returns a real string
                # ("open"/"closed"/"applied"/"failed") — never None — so the
                # old status-write, which silently defaulted to closed
                # whenever navigation returned nothing, is gone. A "failed"
                # write here is recoverable; "closed" is not (it makes the
                # job eligible for PDF-deleting purge).
                db.update_job_status(job.id, nav_result)
                if nav_result == "closed":
                    return "closed"
                if nav_result == "applied":
                    return "applied"
                return "failed"

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
                # H5 fix: external-apply is an intentional skip, not a
                # failure — it must not trip the consecutive-failure circuit
                # breaker. Status is still "manual" so the dashboard shows it
                # correctly; is_easy_apply is corrected to False since we just
                # observed ground truth live.
                db.update_job_status(job.id, "manual")
                db.update_job_field(job.id, "is_easy_apply", False)
                self.logger.info(
                    f"Job {job.id} is external-apply only — cannot auto-submit. "
                    f"Marked manual: {job.title} @ {job.company}"
                )
                return "skipped"
            elif not easy_apply_result:
                await self._take_screenshot(page, f"no_easyapply_{job.id}")
                return "failed"

            # H1 fix: resume upload is no longer a single pre-loop attempt —
            # handle_form_steps() now retries it on every step, since the
            # file input may not appear until step 2+. The resume_path guard
            # (skip entirely if there's no valid resume) moved into the
            # pdf_path argument itself.
            step_outcome = await self.handle_form_steps(page, job.resume_path)

            if step_outcome == "pending":
                self.logger.warning(
                    f"Job {job.id}: {len(self._pending_fields)} required field(s) unresolved — "
                    f"parking as pending_questions"
                )
                await self._take_screenshot(page, f"pending_{job.id}")
                await self._dismiss_modal(page)
                db.save_pending_questions(job.id, self._pending_fields)
                return "pending_questions"

            if step_outcome != "submitted":
                await self._take_screenshot(page, f"form_fail_{job.id}")
                db.update_job_status(job.id, "failed")
                return "failed"

            db.update_job_applied(job.id)
            self.logger.info(f"✓ Applied: {job.title} @ {job.company}")
            return "applied"

        except RuntimeError as exc:
            # CAPTCHA — caller decides whether to stop the batch
            self.logger.error(str(exc))
            raise
        except Exception as exc:
            self.logger.error(f"Apply failed for job {job.id}: {exc}")
            await self._take_screenshot(page, f"error_{job.id}")
            db.update_job_status(job.id, "failed")
            return "failed"
        finally:
            await page.close()

    # ── Batch application ──────────────────────────────────────────────────────

    async def apply_batch(self, jobs: list[Job]) -> dict:
        """Apply to a list of jobs sequentially with human-like gaps between them."""
        results = {"applied": 0, "pending_questions": 0, "failed": 0, "skipped": 0, "closed": 0}
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
                outcome = await self.apply_to_job(job)
                results[outcome] = results.get(outcome, 0) + 1
                if outcome == "failed":
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0   # reset on non-failure outcomes
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

        self.logger.info(
            f"Batch complete — applied:{results['applied']} "
            f"pending:{results['pending_questions']} "
            f"failed:{results['failed']} skipped:{results['skipped']}"
        )
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
    # `sys` is imported at module level now (needed by navigate_to_job()'s
    # stdin/TTY check) — no longer imported locally here.

    async def main() -> None:
        # QAResolver (constructed in LinkedInApplicant.__init__) reads the
        # qa_bank table — make sure it exists even when this script is run
        # standalone, without the dashboard having initialized the DB first.
        db.init_db()
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
            outcome = await applicant.apply_to_job(job)
            outcome_colors = {
                "applied":           "[green]Applied![/green]",
                "pending_questions": "[yellow]Pending — questions need your answers in the dashboard[/yellow]",
                "failed":            "[red]Failed — check logs[/red]",
                "closed":            "[dim]Closed — job no longer accepting applications[/dim]",
            }
            console.print(outcome_colors.get(outcome, f"[white]Unknown outcome: {outcome}[/white]"))
        else:
            jobs = db.get_approved_jobs_with_resume()
            if not jobs:
                console.print("[yellow]No approved jobs with resumes ready.[/yellow]")
                return
            console.print(f"[bold]Found {len(jobs)} job(s) ready to apply[/bold]")
            await applicant.setup_browser()
            results = await applicant.apply_batch(jobs)
            console.print(f"[green]Applied: {results['applied']}[/green]")
            console.print(f"[yellow]Pending questions: {results['pending_questions']}[/yellow]")
            console.print(f"[red]Failed: {results['failed']}[/red]")
            console.print(f"[dim]Skipped: {results['skipped']}[/dim]")
            console.print(f"[dim]Closed (job no longer open): {results.get('closed', 0)}[/dim]")

        await applicant.close()

    asyncio.run(main())
