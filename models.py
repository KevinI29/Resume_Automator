from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel


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

    def is_actionable(self) -> bool:
        return self.status == "approved"


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


class Experience(BaseModel):
    company: str
    title: str
    start: str
    end: str
    location: str
    bullets: list[str]


class Education(BaseModel):
    institution: str
    degree: str
    year: str
    gpa: str = ""


class Skills(BaseModel):
    languages: list[str]
    frameworks: list[str]
    tools: list[str]
    other: list[str] = []


class Project(BaseModel):
    name: str
    description: str
    tech: list[str]
    url: str = ""


class MasterResume(BaseModel):
    personal: PersonalInfo
    summary: str
    experience: list[Experience]
    education: list[Education]
    skills: Skills
    projects: list[Project]
    achievements: list[str] = []
