import asyncio
import json
import os
import sys

import anthropic

import config
import db
from models import Job
from utils import setup_logger


class ResumeTailor:
    MODEL = "claude-haiku-4-5"
    MAX_TOKENS = 4000

    def __init__(self) -> None:
        self.logger = setup_logger("tailor")
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        with open(config.RESUME_MASTER_PATH, encoding="utf-8") as f:
            self.master: dict = json.load(f)

    def _build_tailoring_prompt(self, job: Job, master: dict) -> str:
        description = (job.description or "")[:4000]
        return f"""You are an expert resume writer. Your job is to tailor a resume for a specific job posting. You must follow these rules strictly:

RULES:
1. NEVER invent, fabricate, or add experience that doesn't exist in the original
2. Only reframe and emphasize existing experience to match the job requirements
3. Use keywords from the job description naturally — do not keyword-stuff
4. Keep bullet points achievement-focused and quantified where possible
5. The summary should be 2-3 sentences maximum
6. Return ONLY valid JSON — no markdown, no explanation, no preamble

JOB DETAILS:
Title: {job.title}
Company: {job.company}
Description: {description}

ORIGINAL RESUME:
{json.dumps(master, indent=2)}

TASK:
Return a modified version of the resume JSON where you have:
1. Rewritten the "summary" field to target this specific role and company
2. Rewritten up to 3 experience bullet points per job to emphasize relevance
3. Reordered the "skills" lists to put the most relevant skills first
4. Left ALL other fields (personal info, education, job titles, dates) EXACTLY as they are in the original

Return the complete resume JSON with your modifications applied."""

    async def tailor_resume(self, job: Job) -> dict:
        prompt = self._build_tailoring_prompt(job, self.master)
        try:
            response = self.client.messages.create(
                model=self.MODEL,
                max_tokens=self.MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            tailored = json.loads(raw)

            if set(tailored.keys()) != set(self.master.keys()):
                self.logger.warning(
                    f"Key mismatch in tailored resume for job {job.id} — using master unchanged"
                )
                return self.master

            return tailored

        except json.JSONDecodeError as exc:
            self.logger.error(f"JSON parse error tailoring job {job.id}: {exc}")
            return self.master
        except anthropic.AuthenticationError:
            self.logger.error("Invalid Anthropic API key")
            raise
        except Exception as exc:
            self.logger.error(f"Tailoring failed for job {job.id}: {exc}")
            return self.master

    async def tailor_and_save(self, job: Job) -> str:
        tailored = await self.tailor_resume(job)
        os.makedirs(config.OUTPUT_RESUME_DIR, exist_ok=True)
        json_path = os.path.join(
            config.OUTPUT_RESUME_DIR, f"{job.linkedin_job_id}_resume.json"
        )
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(tailored, f, indent=2, ensure_ascii=False)
        self.logger.info(f"Tailored resume for {job.title} at {job.company}")
        return json_path


if __name__ == "__main__":
    from rich.console import Console
    from renderer import ResumeRenderer

    async def main() -> None:
        console = Console()
        if len(sys.argv) < 2:
            console.print("[red]Usage: python tailor.py <job_id>[/red]")
            console.print("Example: python tailor.py 1")
            return

        job_id = int(sys.argv[1])
        job = next((j for j in db.get_all_jobs() if j.id == job_id), None)
        if not job:
            console.print(f"[red]Job ID {job_id} not found in database[/red]")
            return

        console.print(f"[bold]Tailoring resume for:[/bold] {job.title} @ {job.company}")
        console.print(f"[dim]Fit score: {job.fit_score}/10[/dim]")

        tailor = ResumeTailor()
        renderer = ResumeRenderer()

        json_path = await tailor.tailor_and_save(job)
        console.print(f"[green]Tailored JSON saved:[/green] {json_path}")

        with open(json_path, encoding="utf-8") as f:
            resume_data = json.load(f)

        pdf_path = renderer.render(resume_data, job.linkedin_job_id)
        console.print(f"[green]PDF rendered:[/green] {pdf_path}")
        console.print(f"\n[bold green]Done! Open the PDF to review:[/bold green] {pdf_path}")

    asyncio.run(main())
