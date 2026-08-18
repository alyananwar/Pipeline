"""Immutable data models shared across the pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class Stage(str, Enum):
    """Where a job currently sits in the pipeline."""

    SCRAPED = "scraped"
    RESEARCHED = "researched"
    MATCHED = "matched"
    GENERATED = "generated"
    APPLYING = "applying"
    BLOCKED = "blocked"
    SUBMITTED = "submitted"
    SKIPPED = "skipped"
    FAILED = "failed"


class Barrier(str, Enum):
    """A wall the apply engine cannot cross on its own.

    Every value here means the same thing operationally: the browser tab is
    parked and open, and a human has to finish this one step.
    """

    ACCOUNT_CREATION = "account_creation"
    EMAIL_VERIFICATION = "email_verification"
    CAPTCHA = "captcha"
    ASSESSMENT = "assessment"
    UNKNOWN_FIELD = "unknown_field"
    LOGIN_EXPIRED = "login_expired"
    AWAITING_SUBMIT = "awaiting_submit"


BARRIER_HELP = {
    Barrier.ACCOUNT_CREATION: "Site wants an account before it will take the application.",
    Barrier.EMAIL_VERIFICATION: "Check your inbox for a code, then enter it in the open tab.",
    Barrier.CAPTCHA: "Solve the challenge in the open tab.",
    Barrier.ASSESSMENT: "This posting requires a timed test or coding assessment.",
    Barrier.UNKNOWN_FIELD: "A required field could not be mapped to your profile.",
    Barrier.LOGIN_EXPIRED: "Saved browser session is no longer signed in.",
    Barrier.AWAITING_SUBMIT: "Form is filled and ready. Review it, then confirm to submit.",
}


@dataclass(frozen=True)
class Profile:
    """The user's answers, loaded once from config/profile.yml."""

    raw: Dict[str, Any]
    name: str
    email: str
    phone: Optional[str] = None
    location: Optional[str] = None
    links: Dict[str, str] = field(default_factory=dict)
    target_titles: List[str] = field(default_factory=list)
    archetypes: List[Dict[str, Any]] = field(default_factory=list)
    narrative: Dict[str, Any] = field(default_factory=dict)
    work_authorization: Optional[str] = None
    requires_sponsorship: bool = False
    min_score_to_generate: int = 70
    resume_path: Optional[str] = None


@dataclass(frozen=True)
class Job:
    """One posting, normalized across every source."""

    external_id: str            # sha1(source::company::title::location)
    source: str                 # greenhouse | lever | ashby | ...
    company: str
    title: str
    url: str                    # canonical posting URL
    apply_url: str = ""         # where the form lives, often == url
    location: Optional[str] = None
    department: Optional[str] = None
    description: Optional[str] = None
    remote: bool = False
    salary_range: Optional[str] = None
    posted_at: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None
    db_id: Optional[int] = None


@dataclass(frozen=True)
class Brief:
    """Research output for one company, reused by scoring and writing."""

    company: str
    summary: str
    what_they_do: str
    recent_signals: List[str] = field(default_factory=list)
    tech_stack: List[str] = field(default_factory=list)
    talking_points: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    db_id: Optional[int] = None


@dataclass(frozen=True)
class Match:
    """LLM fit score for one job against the profile."""

    job_external_id: str
    score: int                  # 0-100
    reasoning: str
    strengths: List[str] = field(default_factory=list)
    concerns: List[str] = field(default_factory=list)
    model_used: str = ""
    prompt_version: str = ""
    db_id: Optional[int] = None


@dataclass(frozen=True)
class Application:
    """Everything needed to fill a form, generated once per job."""

    job_external_id: str
    cover_letter: str
    resume_pdf_path: Optional[str] = None
    # question text -> drafted answer, used by the form filler
    answers: Dict[str, str] = field(default_factory=dict)
    model_used: str = ""
    prompt_version: str = ""
    db_id: Optional[int] = None


@dataclass(frozen=True)
class BlockedItem:
    """A parked job waiting on the human."""

    job_external_id: str
    company: str
    title: str
    barrier: Barrier
    detail: str
    url: str
    created_at: datetime
    db_id: Optional[int] = None

    @property
    def help_text(self) -> str:
        return BARRIER_HELP.get(self.barrier, "Needs manual attention.")
