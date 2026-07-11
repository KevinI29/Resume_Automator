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
