"""Detecting the walls an automated application cannot cross.

Kept as pure functions over page text so the logic is testable without a
browser. The apply engine supplies the strings; everything here is decidable
from them.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence, Tuple

from pipeline.models import Barrier

# Ordered by severity. The first match wins, so a page showing both a captcha
# and a signup form reports the captcha, which is the thing actually blocking.
_SIGNALS: Sequence[Tuple[Barrier, str, Sequence[str]]] = (
    (
        Barrier.CAPTCHA,
        "Bot check on the page.",
        (
            "recaptcha", "g-recaptcha", "hcaptcha", "cf-turnstile",
            "verify you are human", "are you a robot",
        ),
    ),
    (
        Barrier.EMAIL_VERIFICATION,
        "Site sent a verification code.",
        (
            "verify your email", "verification code", "confirm your email",
            "we sent a code", "check your inbox", "enter the code",
            "one-time passcode", "one time password",
        ),
    ),
    (
        Barrier.ASSESSMENT,
        "Posting requires a test or assessment.",
        (
            "coding assessment", "take-home assignment", "hackerrank",
            "codility", "karat", "timed assessment", "online assessment",
            "complete the assessment",
        ),
    ),
    (
        Barrier.LOGIN_EXPIRED,
        "Saved session is signed out.",
        ("your session has expired", "please sign in again", "session timed out"),
    ),
    (
        Barrier.ACCOUNT_CREATION,
        "Site wants an account before accepting the application.",
        (
            "create an account", "create your account", "sign up to apply",
            "register to apply", "create a profile to apply",
            "you must be signed in", "sign in to apply", "log in to apply",
        ),
    ),
)

# Workday and iCIMS effectively always require an account per employer.
_ACCOUNT_REQUIRED_HOSTS = ("myworkdayjobs.com", "icims.com", "taleo.net", "successfactors.com")


def detect(page_text: str, url: str = "", filled_all_required: bool = True) -> Optional[Tuple[Barrier, str]]:
    """Return the barrier blocking this page, or None if the path is clear.

    `page_text` should be the visible text plus enough of the markup that
    widget class names (recaptcha, cf-turnstile) are present.
    """
    haystack = (page_text or "").lower()

    for barrier, detail, needles in _SIGNALS:
        for needle in needles:
            if needle in haystack:
                return barrier, detail

    host = _host(url)
    if any(h in host for h in _ACCOUNT_REQUIRED_HOSTS):
        # Only a barrier if we are not already through the account gate, which
        # shows up as the application form itself being present.
        if not _looks_like_application_form(haystack):
            return Barrier.ACCOUNT_CREATION, "{0} requires an employer account.".format(host)

    if not filled_all_required:
        return Barrier.UNKNOWN_FIELD, "A required field could not be filled from your profile."

    return None


def _host(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url or "", re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _looks_like_application_form(haystack: str) -> bool:
    """Rough check that we are actually on a form and not a gate page."""
    hits = sum(
        1
        for marker in ("resume", "cover letter", "first name", "last name", "phone", "upload")
        if marker in haystack
    )
    return hits >= 3


def detect_ats(url: str) -> str:
    """Which applicant tracking system is this, from the URL alone."""
    host = _host(url)
    for name, needles in (
        ("greenhouse", ("greenhouse.io",)),
        ("lever", ("lever.co",)),
        ("ashby", ("ashbyhq.com",)),
        ("workday", ("myworkdayjobs.com", "workday.com")),
        ("icims", ("icims.com",)),
        ("taleo", ("taleo.net",)),
        ("smartrecruiters", ("smartrecruiters.com",)),
        ("workable", ("workable.com",)),
    ):
        if any(n in host for n in needles):
            return name
    return "unknown"


# How much of the flow each ATS can realistically be automated through. Used to
# set expectations in the tracker rather than to skip anything.
AUTOMATION_TIER = {
    "greenhouse": "full",
    "lever": "full",
    "ashby": "full",
    "workable": "full",
    "smartrecruiters": "partial",
    "workday": "partial",
    "icims": "partial",
    "taleo": "partial",
    "unknown": "best-effort",
}
