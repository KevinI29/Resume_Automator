import asyncio
import random
import urllib.parse
from datetime import datetime

import httpx

import config
import db
from models import Job, SavedSearch
from utils import clean_text, setup_logger


class LinkedInScraper:
    _SEARCH_URL = "https://www.linkedin.com/voyager/api/voyagerJobsDashJobCards"
    _DECORATION_ID = "com.linkedin.voyager.dash.deco.jobs.search.JobSearchCardsCollection-220"
    _JOB_URL = "https://www.linkedin.com/voyager/api/jobs/jobPostings/{job_id}"

    def __init__(self) -> None:
        self.logger = setup_logger("scraper")
        self.client = httpx.AsyncClient(
            headers=self._build_headers(),
            timeout=15.0,
            follow_redirects=True,
        )

    def _build_headers(self) -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) "
                "Gecko/20100101 Firefox/150.0"
            ),
            "Accept": "application/vnd.linkedin.normalized+json+2.1",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Referer": "https://www.linkedin.com/jobs/search/",
            "x-li-lang": "en_US",
            "x-li-track": (
                '{"clientVersion":"1.13.44203","mpVersion":"1.13.44203",'
                '"osName":"web","timezoneOffset":5.5,"timezone":"Asia/Kolkata",'
                '"deviceFormFactor":"DESKTOP","mpName":"voyager-web",'
                '"displayDensity":1.25,"displayWidth":1920,"displayHeight":1080}'
            ),
            "x-li-page-instance": "urn:li:page:d_flagship3_search_srp_jobs;scraper",
            "x-li-pem-metadata": (
                "Voyager - Careers - Jobs Search=jobs-search-results,"
                "Voyager - Careers - Critical - careers-api=jobs-search-results"
            ),
            "x-li-deco-include-micro-schema": "true",
            "x-restli-protocol-version": "2.0.0",
            "csrf-token": config.LINKEDIN_CSRF_TOKEN,
            "Cookie": f'li_at={config.LINKEDIN_COOKIE}; JSESSIONID="{config.LINKEDIN_CSRF_TOKEN}"',
        }

    async def _random_delay(self) -> None:
        delay = random.uniform(config.MIN_DELAY_SECONDS, config.MAX_DELAY_SECONDS)
        self.logger.debug(f"Sleeping {delay:.1f}s")
        await asyncio.sleep(delay)

    async def _get(self, url: str, params: dict | None = None) -> dict | None:
        safe_url = url.split("?")[0]
        self.logger.info(f"GET {url}")
        try:
            response = await self.client.get(url, params=params)

            if response.status_code in (401, 403):
                self.logger.error(
                    "Cookie expired or invalid — refresh LINKEDIN_COOKIE and "
                    "LINKEDIN_CSRF_TOKEN in .env"
                )
                raise PermissionError("LinkedIn authentication failed")

            if response.status_code == 429:
                self.logger.warning("Rate limited (429). Waiting 60s then retrying once...")
                await asyncio.sleep(60)
                response = await self.client.get(url, params=params)
                if response.status_code == 429:
                    self.logger.error("Still rate limited after retry. Stopping scraper.")
                    raise RuntimeError("LinkedIn rate limit exceeded")

            if not response.is_success:
                self.logger.error(
                    f"HTTP {response.status_code} for {safe_url} — "
                    f"body: {response.text[:500]}"
                )
                return None

            return response.json()

        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            self.logger.error(f"Connection error for {safe_url}: {exc}")
            return None
        except httpx.HTTPStatusError as exc:
            self.logger.error(
                f"HTTP {exc.response.status_code} for {safe_url} — "
                f"body: {exc.response.text[:500]}"
            )
            return None
        except ValueError as exc:
            self.logger.error(f"JSON parse error for {safe_url}: {exc}")
            return None

    def _extract_job_id(self, urn: str) -> str:
        # "urn:li:fsd_jobPosting:3912345678" → "3912345678"
        return urn.rsplit(":", 1)[-1]

    def _build_job_url(self, job_id: str) -> str:
        return f"https://www.linkedin.com/jobs/view/{job_id}"

    def _is_easy_apply(self, card: dict) -> bool:
        if card.get("easyApplyUrl"):
            return True
        if card.get("easyApply"):
            return True
        apply_method = card.get("applyMethod", {})
        if isinstance(apply_method, dict):
            if "easyapply" in apply_method.get("$type", "").lower():
                return True
        return False

    async def search_jobs(
        self,
        keywords: str,
        location: str,
        easy_apply_only: bool,
        max_results: int,
        geo_id: str | None = None,
    ) -> list[dict]:
        results: list[dict] = []
        start = 0
        page_size = 25

        if geo_id is None:
            geo_id = config.LINKEDIN_GEO_ID

        self.logger.info(
            f"Searching: '{keywords}' in '{location or 'any'}' "
            f"(easy_apply={easy_apply_only}, max={max_results})"
        )

        while len(results) < max_results:
            # Build Rest.li query — parentheses/colons/commas must stay literal,
            # only spaces get percent-encoded. Using httpx params= would over-encode.
            kw_encoded = urllib.parse.quote(keywords, safe="")
            # Order matches browser: origin → keywords → locationUnion → selectedFilters
            query_parts = [
                "origin:JOB_SEARCH_PAGE_SEARCH_BUTTON",
                f"keywords:{kw_encoded}",
            ]
            if geo_id:
                query_parts.append(f"locationUnion:(geoId:{geo_id})")
                filters = "distance:List(25)"
                if easy_apply_only:
                    filters += ",easyApply:List(true)"
                query_parts.append(f"selectedFilters:({filters})")
            query_parts.append("spellCorrectionEnabled:true")

            query_value = f"({','.join(query_parts)})"
            url = (
                f"{self._SEARCH_URL}"
                f"?decorationId={urllib.parse.quote(self._DECORATION_ID, safe='.-')}"
                f"&count={page_size}"
                f"&q=jobSearch"
                f"&query={query_value}"
                f"&start={start}"
            )

            data = await self._get(url)
            if not data:
                break

            # elements = pagination references (count only — not the job data)
            elements: list = (
                data.get("elements")
                or data.get("data", {}).get("elements")
                or []
            )
            if not elements:
                self.logger.info(f"No results for '{keywords}' (start={start})")
                break

            # LinkedIn normalized JSON: actual job data lives in the top-level
            # "included" array; filter for objects that have jobPostingTitle
            job_cards = [
                item for item in data.get("included", [])
                if item.get("jobPostingTitle")
            ]
            results.extend(job_cards)
            self.logger.info(
                f"Page: {len(elements)} refs → {len(job_cards)} job cards "
                f"(running total: {len(results)})"
            )

            if len(elements) < page_size:
                break

            start += page_size
            await self._random_delay()

        return results[:max_results]
    
    

    async def get_job_description(self, job_id: str) -> str:
        url = self._JOB_URL.format(job_id=job_id)
        data = await self._get(url)
        if not data:
            return ""
        try:
            text = data.get("data", {}).get("description", {}).get("text", "")
            return clean_text(text)
        except (AttributeError, TypeError):
            return ""

    async def scrape(
        self,
        search: SavedSearch | None = None,
        max_results_override: int | None = None,
    ) -> list[Job]:
        """
        When `search` is None, behavior is unchanged from Session <12:
        config.TARGET_TITLES / config.LINKEDIN_GEO_ID / easy_apply_only=True,
        capped at config.MAX_JOBS_PER_SCRAPE (or max_results_override if given).
        When `search` is provided, its titles/geo_id/easy_apply_only are used
        instead, capped at max_results_override if given, else search.max_results.
        """
        db.init_db()
        seen_ids = {j.linkedin_job_id for j in db.get_all_jobs()}
        new_jobs: list[Job] = []

        if search is not None:
            titles = search.titles
            geo_id = search.geo_id
            geo_label = search.geo_label
            easy_apply_only = search.easy_apply_only
            cap = max_results_override if max_results_override is not None else search.max_results
        else:
            titles = config.TARGET_TITLES
            geo_id = config.LINKEDIN_GEO_ID
            geo_label = config.TARGET_LOCATION
            easy_apply_only = True
            cap = max_results_override if max_results_override is not None else config.MAX_JOBS_PER_SCRAPE

        self.logger.info(
            f"Scrape started — {len(seen_ids)} jobs already in DB. "
            f"Cap: {cap} new jobs."
        )

        for title in titles:
            if len(new_jobs) >= cap:
                self.logger.info("Reached scrape cap. Stopping.")
                break

            remaining = cap - len(new_jobs)
            raw_cards = await self.search_jobs(
                keywords=title,
                location=geo_label,
                easy_apply_only=easy_apply_only,
                max_results=remaining,
                geo_id=geo_id,
            )

            for card in raw_cards:
                if len(new_jobs) >= cap:
                    break

                # cards come directly from "included" — jobPostingUrn is the clean ID field
                urn: str = card.get("jobPostingUrn", "")
                if not urn:
                    continue

                job_id = self._extract_job_id(urn)

                if job_id in seen_ids:
                    self.logger.debug(f"Already in DB: {job_id}")
                    continue

                await self._random_delay()

                description = await self.get_job_description(job_id)

                job = Job(
                    linkedin_job_id=job_id,
                    title=card.get("jobPostingTitle", "Unknown"),
                    company=card.get("primaryDescription", {}).get("text", "Unknown"),
                    location=card.get("secondaryDescription", {}).get("text", "Unknown"),
                    description=description,
                    url=self._build_job_url(job_id),
                    is_easy_apply=True,
                    created_at=datetime.utcnow(),
                )

                db.insert_job(job)
                seen_ids.add(job_id)
                new_jobs.append(job)
                self.logger.info(f"Saved: {job.title} @ {job.company} [{job_id}]")

        self.logger.info(f"Scrape complete — {len(new_jobs)} new jobs saved.")
        if search is not None and search.id is not None:
            db.update_search_last_run(search.id)
        return new_jobs


if __name__ == "__main__":
    import sys

    from rich.console import Console
    from rich.table import Table

    async def main() -> None:
        scraper = LinkedInScraper()
        console = Console()

        search = None
        if "--search-id" in sys.argv:
            idx = sys.argv.index("--search-id")
            try:
                search_id = int(sys.argv[idx + 1])
            except (IndexError, ValueError):
                console.print("[red]Usage: python scraper.py --search-id <id>[/red]")
                return
            search = db.get_search_by_id(search_id)
            if not search:
                console.print(f"[red]Search ID {search_id} not found.[/red]")
                return
            console.print(f"[bold green]Starting LinkedIn scraper — search: {search.name}[/bold green]")
        else:
            console.print("[bold green]Starting LinkedIn scraper...[/bold green]")

        jobs = await scraper.scrape(search=search)

        table = Table(title=f"Found {len(jobs)} new jobs")
        table.add_column("Title", style="cyan")
        table.add_column("Company", style="magenta")
        table.add_column("Location")
        table.add_column("Easy Apply", style="green")
        table.add_column("Score")

        for job in jobs:
            table.add_row(
                job.title,
                job.company,
                job.location,
                "✓" if job.is_easy_apply else "✗",
                str(job.fit_score or "—"),
            )

        console.print(table)
        console.print("[bold]Saved to database.[/bold]")

    asyncio.run(main())
