"""LLM fit scoring: how well does this posting match this person."""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from pipeline.llm import LLMClient, LLMError
from pipeline.models import Brief, Job, Match, Profile

logger = logging.getLogger(__name__)

PROMPT_VERSION = "match-v1"
MAX_DESCRIPTION_CHARS = 6000

SYSTEM = """You score how well a job posting fits a specific candidate.

Return ONLY a JSON object:
  score      - integer 0-100
  reasoning  - two or three sentences explaining the score
  strengths  - array of concrete overlaps between candidate and posting
  concerns   - array of real gaps or mismatches

Scoring guide:
  85-100  Strong fit. Candidate meets the core requirements and the level is right.
  70-84   Good fit. Worth applying. Minor gaps.
  50-69   Stretch. Some overlap but a real gap in level, domain, or requirements.
  0-49    Poor fit. Wrong level, wrong field, or requirements the candidate cannot meet.

Be honest and be strict about level. A student or intern candidate applying to a
role that asks for 5+ years, a Senior/Staff/Principal title, or management scope
should score below 50 no matter how well the domain matches. Under-scoring a bad
fit costs the candidate nothing. Over-scoring wastes their time and their name.

If the posting requires work authorization or sponsorship the candidate does not
have, say so in concerns and cap the score at 40."""


def _profile_block(profile: Profile) -> str:
    narrative = profile.narrative or {}
    parts = [
        "Name: {0}".format(profile.name),
        "Location: {0}".format(profile.location or "not specified"),
        "Target roles: {0}".format(", ".join(profile.target_titles)),
    ]
    if narrative.get("headline"):
        parts.append("Headline: {0}".format(narrative["headline"]))
    if narrative.get("exit_story"):
        parts.append("Background: {0}".format(narrative["exit_story"]))
    for key, label in (("superpowers", "Strengths"), ("proof_points", "Projects")):
        items = narrative.get(key) or []
        if not items:
            continue
        rendered = []
        for item in items:
            if isinstance(item, dict):
                rendered.append(
                    "{0}: {1}".format(item.get("name", ""), item.get("hero_metric", "")).strip(": ")
                )
            else:
                rendered.append(str(item))
        parts.append("{0}:\n  - {1}".format(label, "\n  - ".join(rendered)))
    if profile.work_authorization:
        parts.append("Work authorization: {0}".format(profile.work_authorization))
    if profile.requires_sponsorship:
        parts.append("Requires visa sponsorship: yes")
    return "\n".join(parts)


def _job_block(job: Job, brief: Optional[Brief]) -> str:
    parts = [
        "Company: {0}".format(job.company),
        "Title: {0}".format(job.title),
        "Location: {0}{1}".format(job.location or "not specified", " (remote)" if job.remote else ""),
    ]
    if job.department:
        parts.append("Department: {0}".format(job.department))
    if job.salary_range:
        parts.append("Compensation: {0}".format(job.salary_range))
    if brief and brief.summary:
        parts.append("Company context: {0}".format(brief.summary))
    parts.append("\nDescription:\n{0}".format((job.description or "")[:MAX_DESCRIPTION_CHARS]))
    return "\n".join(parts)


def score_job(
    job: Job,
    profile: Profile,
    llm: LLMClient,
    brief: Optional[Brief] = None,
) -> Match:
    """Score one job. Never raises: a scoring failure becomes a zero with a reason."""
    user = "CANDIDATE:\n{0}\n\n---\n\nPOSTING:\n{1}".format(
        _profile_block(profile), _job_block(job, brief)
    )

    try:
        data = llm.complete_json(SYSTEM, user, max_tokens=1200)
    except LLMError as exc:
        logger.warning("Scoring failed for %s at %s: %s", job.title, job.company, exc)
        return Match(
            job_external_id=job.external_id,
            score=0,
            reasoning="Scoring failed: {0}".format(exc),
            model_used=llm.config.model,
            prompt_version=PROMPT_VERSION,
        )

    return Match(
        job_external_id=job.external_id,
        score=_clamp_score(data.get("score")),
        reasoning=str(data.get("reasoning") or "").strip(),
        strengths=_string_list(data.get("strengths")),
        concerns=_string_list(data.get("concerns")),
        model_used=llm.config.model,
        prompt_version=PROMPT_VERSION,
    )


def _clamp_score(value: Any) -> int:
    """Coerce whatever the model returned into 0-100.

    Small local models return "85", 85.0, or "85/100". All of those should work
    rather than crashing a long scan.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, min(100, int(value)))
    text = str(value or "").strip()
    digits = ""
    for ch in text:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if not digits:
        return 0
    return max(0, min(100, int(digits)))


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]
