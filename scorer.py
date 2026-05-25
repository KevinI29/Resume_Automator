import asyncio
import json
import random
from datetime import datetime

import anthropic

import config
import db
from models import Job, MasterResume
from utils import setup_logger


class JobScorer:
    MODEL = "claude-haiku-4-5"
    MAX_TOKENS = 400
    HAIKU_INPUT_COST = 0.0000008   # $ per token
    HAIKU_OUTPUT_COST = 0.000004   # $ per token

    def __init__(self) -> None:
        self.logger = setup_logger("scorer")
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self.resume = self._load_resume()
        self.profile_summary = self._build_profile_summary()
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def _load_resume(self) -> MasterResume:
        import json as _json
        with open(config.RESUME_MASTER_PATH, encoding="utf-8") as f:
            return MasterResume(**_json.load(f))

    def _build_profile_summary(self) -> str:
        r = self.resume
        all_skills = (
            r.skills.languages
            + r.skills.frameworks
            + r.skills.tools
            + r.skills.other
        )
        skills_str = ", ".join(all_skills[:20])  # cap to keep tokens low

        # Calculate approximate years of experience from earliest start date
        years = "< 1"
        if r.experience:
            try:
                earliest = r.experience[-1].start  # e.g. "Aug 2025"
                start_year = int(earliest.split()[-1])
                start_month_str = earliest.split()[0]
                months = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
                          "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
                start_month = months.get(start_month_str, 1)
                now = datetime.utcnow()
                total_months = (now.year - start_year) * 12 + (now.month - start_month)
                yrs = total_months / 12
                years = f"{yrs:.1f}" if yrs >= 1 else "< 1"
            except (ValueError, IndexError):
                pass

        return (
            f"Name: {r.personal.name}\n"
            f"Summary: {r.summary}\n"
            f"Skills: {skills_str}\n"
            f"Years of experience: {years}"
        )

    def _build_scoring_prompt(self, job: Job) -> str:
        description = (job.description or "")[:3000]
        return f"""You are a job fit evaluator. Score how well this candidate matches this job.

CANDIDATE PROFILE:
{self.profile_summary}

JOB TO EVALUATE:
Title: {job.title}
Company: {job.company}
Location: {job.location}
Description: {description}

SCORING RULES:
- Score 1-10 where 10 = perfect match, 1 = completely wrong fit
- Score 8-10: Meets 90%+ of requirements, strong keyword overlap
- Score 6-7: Meets 70%+ of requirements, some gaps but hirable
- Score 4-5: Meets 50%, significant gaps
- Score 1-3: Wrong domain, too senior/junior, or missing critical requirements

DEALBREAKER RULES (auto score 2 or below):
- Requires 5+ more years of experience than candidate has
- Completely different tech stack with no overlap
- Requires specific domain expertise candidate doesn't have

Respond ONLY with valid JSON, no other text:
{{
  "score": <integer 1-10>,
  "reason": "<2-3 sentences explaining the score, mention specific matches and gaps>",
  "dealbreakers": ["<list any dealbreakers found, empty array if none>"]
}}"""

    async def score_job(self, job: Job) -> tuple[int, str]:
        if not job.description:
            return 0, "no description available"

        prompt = self._build_scoring_prompt(job)
        try:
            response = self.client.messages.create(
                model=self.MODEL,
                max_tokens=self.MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            self.total_input_tokens += response.usage.input_tokens
            self.total_output_tokens += response.usage.output_tokens

            raw = response.content[0].text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            data = json.loads(raw)
            score = int(data["score"])
            reason = data.get("reason", "")
            return score, reason

        except json.JSONDecodeError:
            self.logger.error(f"JSON parse error for job {job.id}. Raw: {raw[:200]}")
            return 0, "parse error"
        except anthropic.AuthenticationError:
            self.logger.error("Invalid Anthropic API key — check ANTHROPIC_API_KEY in .env")
            raise
        except anthropic.RateLimitError:
            self.logger.warning("Rate limited by Anthropic. Waiting 30s then retrying...")
            await asyncio.sleep(30)
            try:
                response = self.client.messages.create(
                    model=self.MODEL,
                    max_tokens=self.MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = response.content[0].text.strip()
                data = json.loads(raw)
                return int(data["score"]), data.get("reason", "")
            except Exception as exc:
                self.logger.error(f"Retry failed for job {job.id}: {exc}")
                return 0, "scoring failed after retry"
        except Exception as exc:
            self.logger.error(f"Unexpected error scoring job {job.id}: {exc}")
            return 0, "scoring failed"

    async def score_all_unscored(self) -> int:
        jobs = db.get_unscored_jobs()
        if not jobs:
            self.logger.info("All jobs already scored.")
            return 0

        self.logger.info(f"Scoring {len(jobs)} unscored jobs...")
        scored = 0

        for job in jobs:
            score, reason = await self.score_job(job)
            db.update_job_score(job.id, score, reason)
            scored += 1
            self.logger.info(
                f"Scored: {job.title} at {job.company} → {score}/10 | "
                f"{reason[:100]}"
            )
            delay = random.uniform(config.MIN_DELAY_SECONDS, config.MAX_DELAY_SECONDS)
            await asyncio.sleep(delay)

        cost = (
            self.total_input_tokens * self.HAIKU_INPUT_COST
            + self.total_output_tokens * self.HAIKU_OUTPUT_COST
        )
        self.logger.info(
            f"Done. Tokens used — input: {self.total_input_tokens}, "
            f"output: {self.total_output_tokens}. "
            f"Estimated cost: ${cost:.4f}"
        )
        return scored

    async def score_single(self, job_id: int) -> tuple[int, str]:
        jobs = db.get_all_jobs()
        job = next((j for j in jobs if j.id == job_id), None)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        score, reason = await self.score_job(job)
        db.update_job_score(job_id, score, reason)
        self.logger.info(f"Re-scored job {job_id}: {score}/10 | {reason[:100]}")
        return score, reason


if __name__ == "__main__":
    from rich import box
    from rich.console import Console
    from rich.table import Table
    from config import MIN_FIT_SCORE

    async def main() -> None:
        scorer = JobScorer()
        console = Console()

        console.print("[bold green]Starting job scorer...[/bold green]")
        count = await scorer.score_all_unscored()
        console.print(f"[bold]Scored {count} jobs.[/bold]")

        jobs = db.get_jobs_above_threshold()
        if not jobs:
            console.print("[yellow]No jobs above score threshold yet.[/yellow]")
            return

        table = Table(
            title=f"Jobs above threshold (score >= {MIN_FIT_SCORE})",
            box=box.ROUNDED,
        )
        table.add_column("Score", style="bold green", width=6)
        table.add_column("Title", style="cyan")
        table.add_column("Company", style="magenta")
        table.add_column("Easy Apply", width=10)
        table.add_column("Reason")

        for job in jobs:
            reason = job.fit_reason or ""
            table.add_row(
                f"{job.fit_score}/10",
                job.title,
                job.company,
                "✓" if job.is_easy_apply else "✗",
                reason[:80] + "..." if len(reason) > 80 else reason,
            )

        console.print(table)

    asyncio.run(main())
