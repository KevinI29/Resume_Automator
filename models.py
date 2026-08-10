from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional
from pydantic import BaseModel, model_validator, field_validator


class Job(BaseModel):
    id: Optional[int] = None
    linkedin_job_id: str
    title: str
    company: str
    location: str
    description: str
    url: str
    is_easy_apply: bool
    fit_score: Optional[int] = None
    fit_reason: Optional[str] = None
    status: str = "new"
    resume_path: Optional[str] = None
    created_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None
    validated_at: Optional[datetime] = None

    def is_actionable(self) -> bool:
        return self.status == "approved"

    @property
    def is_likely_expired(self) -> bool:
        """
        Returns True if this job is 'new' and older than JOB_EXPIRY_HOURS.
        The .replace(tzinfo=None) guards against naive vs aware datetime
        comparison errors — SQLite stores timestamps as naive strings.
        """
        if self.status != "new" or not self.created_at:
            return False
        try:
            from config import JOB_EXPIRY_HOURS
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            age = now - self.created_at.replace(tzinfo=None)
            return age > timedelta(hours=JOB_EXPIRY_HOURS)
        except Exception:
            return False

    @property
    def is_aging(self) -> bool:
        """
        True if this job is approaching auto-purge (past JOB_AGE_WARNING_DAYS
        but not yet past MAX_JOB_AGE_DAYS) and is in a purgeable (non-protected)
        status. Independent from is_likely_expired — a job can be both, either,
        or neither; a stale post can still be a perfectly valid application.
        """
        if not self.created_at:
            return False
        try:
            import config
            if self.status in config.AGE_PURGE_PROTECTED_STATUSES:
                return False
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            age = now - self.created_at.replace(tzinfo=None)
            warning = timedelta(days=config.JOB_AGE_WARNING_DAYS)
            purge = timedelta(days=config.MAX_JOB_AGE_DAYS)
            return warning <= age < purge
        except Exception:
            return False


class Application(BaseModel):
    id: Optional[int] = None
    job_id: int
    resume_path: str
    method: str          # "easy_apply" | "manual"
    status: str          # "submitted" | "failed" | "pending_manual"
    submitted_at: Optional[datetime] = None


class ResumeSection(BaseModel):
    name: str
    content: str | list | dict


class PersonalInfo(BaseModel):
    name: str
    email: str
    phone: str
    location: str
    linkedin: str
    github: str


class ExperienceEntry(BaseModel):
    company: str
    title: str
    start: str
    end: str
    location: str
    bullets: list[str]

    @field_validator("bullets")
    @classmethod
    def bullets_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("Each experience entry must have at least one bullet")
        return v


# Backward-compat alias
Experience = ExperienceEntry


class EducationEntry(BaseModel):
    institution: str
    degree: str
    year: str
    gpa: Optional[str] = ""


Education = EducationEntry


class Skills(BaseModel):
    languages: list[str] = []
    frameworks: list[str] = []
    tools: list[str] = []
    other: list[str] = []


class ProjectEntry(BaseModel):
    name: str
    description: str
    tech: list[str]
    url: Optional[str] = ""


Project = ProjectEntry


class MasterResume(BaseModel):
    personal: PersonalInfo
    summary: str
    experience: list[ExperienceEntry]
    education: list[EducationEntry]
    skills: Skills
    projects: list[ProjectEntry] = []
    achievements: list[str] = []

    @model_validator(mode="after")
    def validate_structure(self) -> "MasterResume":
        if not self.personal.name.strip():
            raise ValueError("personal.name cannot be empty")
        if not self.summary.strip():
            raise ValueError("summary cannot be empty")
        if not self.experience:
            raise ValueError("Resume must have at least one experience entry")
        if (
            not self.skills.languages
            and not self.skills.frameworks
            and not self.skills.tools
        ):
            raise ValueError("Skills section cannot be entirely empty")
        return self


class QABankEntry(BaseModel):
    id: int
    question_norm: str
    question_raw: str
    field_type: str
    options: Optional[list[str]] = None   # deserialized from options_json by db helpers
    answer: str
    source: str                           # 'config' | 'ai' | 'manual'
    confidence: Optional[float] = None
    use_count: int = 0
    created_at: datetime
    updated_at: datetime


class PendingAnswer(BaseModel):
    id: int
    job_id: int
    question_raw: str
    question_norm: str
    field_type: str
    options: Optional[list[str]] = None   # deserialized from options_json by db helpers
    ai_suggested_answer: Optional[str] = None
    ai_confidence: Optional[float] = None
    resolved: bool = False
    created_at: datetime


class SavedSearch(BaseModel):
    """A saved LinkedIn job search config. Titles are stored JSON-serialized
    in the DB but exposed as a proper list on the model."""
    id: int | None = None
    name: str
    titles: list[str]
    geo_id: str
    geo_label: str
    remote_only: bool = False
    easy_apply_only: bool = True
    max_results: int = 25
    enabled: bool = True
    last_run_at: datetime | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _validate(self):
        if not self.name.strip():
            raise ValueError("name cannot be empty")
        if not self.titles or all(not t.strip() for t in self.titles):
            raise ValueError("at least one title required")
        if not self.geo_id.strip():
            raise ValueError("geo_id cannot be empty")
        if self.max_results < 1 or self.max_results > 100:
            raise ValueError("max_results must be between 1 and 100")
        return self
