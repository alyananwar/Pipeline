"""Write the application: cover letter, screening answers, tailored resume."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from pipeline.llm import LLMClient, LLMError
from pipeline.models import Application, Brief, Job, Match, Profile
from pipeline.resume import render_resume

logger = logging.getLogger(__name__)

PROMPT_VERSION = "generate-v1"
MAX_DESCRIPTION_CHARS = 5000

# Pre-generated because almost every ATS asks some version of these. Anything
# else the form throws at us gets answered live by answer_question().
COMMON_QUESTIONS = (
    "Why do you want to work at this company?",
    "Why are you a good fit for this role?",
    "Describe a relevant project or experience.",
    "What interests you most about this position?",
)

VOICE_RULES = """Voice rules, applied to everything you write:
- Plain, direct, human. Like explaining something to a smart colleague.
- No corporate buzzwords, no "passionate about", no "I am excited to apply".
- No em-dashes. Use commas or split the sentence.
- Never invent experience, skills, numbers, or dates. Use only what the
  candidate profile actually contains. An empty claim is worse than a short answer.
- Reference something specific about the company that a generic applicant
  could not have written. If the research material does not support that, stay
  general rather than making something up."""

COVER_SYSTEM = """You write short, specific cover letters for job applications.

""" + VOICE_RULES + """

Return ONLY a JSON object:
  cover_letter - 150 to 220 words, three short paragraphs, no greeting line and
                 no signature block. Paragraph one: why this company and this
                 role specifically. Paragraph two: the single most relevant thing
                 the candidate has actually done, with its concrete result.
                 Paragraph three: one sentence closing.
  keywords     - array of skills named in the posting that the candidate
                 genuinely has, taken from their profile. Used to reorder their
                 resume. Do not include skills the profile does not list."""

ANSWERS_SYSTEM = """You draft application form answers in the candidate's voice.

""" + VOICE_RULES + """

Return ONLY a JSON object mapping each question exactly as given to a string
answer. Keep answers between 40 and 120 words unless the question clearly wants
a one-liner. Answer as the candidate, first person."""

SINGLE_ANSWER_SYSTEM = """You answer one job application question in the candidate's voice.

""" + VOICE_RULES + """

Return ONLY a JSON object:
  answer     - the answer text, first person, sized to the question
  confident  - true if the candidate profile genuinely supports this answer,
               false if you had to stay vague because the profile lacks the
               information. Be honest: false is useful, a bluff is not."""


def _context(job: Job, profile: Profile, brief: Optional[Brief]) -> str:
    narrative = profile.narrative or {}
    lines = [
        "CANDIDATE",
        "Name: {0}".format(profile.name),
        "Headline: {0}".format(narrative.get("headline", "")),
        "Background: {0}".format(narrative.get("exit_story", "")),
    ]

    for item in narrative.get("proof_points") or []:
        if isinstance(item, dict):
            lines.append("Project - {0}: {1}".format(item.get("name", ""), item.get("hero_metric", "")))

    for section in ("experience", "projects"):
        for item in profile.raw.get(section) or []:
            if not isinstance(item, dict):
                continue
            header = item.get("title") or item.get("name") or ""
            where = item.get("company") or item.get("tech") or ""
            lines.append("{0} {1} {2}".format(section.title(), header, where).strip())
            for bullet in item.get("bullets") or []:
                lines.append("  - {0}".format(bullet))

    skills = profile.raw.get("skills") or {}
    for group, values in skills.items():
        if values:
            lines.append("Skills ({0}): {1}".format(group, ", ".join(str(v) for v in values)))

    lines += ["", "POSTING", "Company: {0}".format(job.company), "Title: {0}".format(job.title)]
    if job.location:
        lines.append("Location: {0}".format(job.location))
    lines.append("Description:\n{0}".format((job.description or "")[:MAX_DESCRIPTION_CHARS]))

    if brief and (brief.summary or brief.talking_points):
        lines += ["", "COMPANY RESEARCH", brief.summary or ""]
        if brief.what_they_do:
            lines.append(brief.what_they_do)
        for point in brief.talking_points:
            lines.append("- {0}".format(point))
        for signal in brief.recent_signals:
            lines.append("- {0}".format(signal))

    return "\n".join(l for l in lines if l is not None)


def build_application(
    job: Job,
    profile: Profile,
    llm: LLMClient,
    brief: Optional[Brief] = None,
    resume_dir: Path = Path("data/resumes"),
) -> Application:
    """Produce everything needed to fill this job's form."""
    context = _context(job, profile, brief)

    cover_letter = ""
    keywords: List[str] = []
    try:
        data = llm.complete_json(COVER_SYSTEM, context, max_tokens=1200, temperature=0.4)
        cover_letter = str(data.get("cover_letter") or "").strip()
        keywords = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()]
    except LLMError as exc:
        logger.warning("Cover letter failed for %s: %s", job.title, exc)

    answers: Dict[str, str] = {}
    try:
        question_block = "{0}\n\nQUESTIONS:\n{1}".format(
            context, "\n".join("- {0}".format(q) for q in COMMON_QUESTIONS)
        )
        raw_answers = llm.complete_json(ANSWERS_SYSTEM, question_block, max_tokens=2000, temperature=0.4)
        answers = {str(k): str(v).strip() for k, v in raw_answers.items() if str(v).strip()}
    except LLMError as exc:
        logger.warning("Answer bank failed for %s: %s", job.title, exc)

    answers.update(_structured_answers(profile))

    resume_path: Optional[str] = None
    if profile.resume_path:
        # The candidate brought their own. Never overwrite it.
        candidate_resume = Path(profile.resume_path)
        resume_path = str(candidate_resume) if candidate_resume.is_file() else None
        if resume_path is None:
            logger.warning("baseline_resume points at %s which does not exist.", profile.resume_path)
    else:
        safe = re.sub(r"[^A-Za-z0-9]+", "-", "{0}-{1}".format(job.company, job.title)).strip("-")[:80]
        try:
            resume_path = str(render_resume(profile, resume_dir / "{0}.pdf".format(safe), highlight=keywords))
        except Exception as exc:  # reportlab raises a variety of layout errors
            logger.warning("Resume render failed for %s: %s", job.title, exc)

    return Application(
        job_external_id=job.external_id,
        cover_letter=cover_letter,
        resume_pdf_path=resume_path,
        answers=answers,
        model_used=llm.config.model,
        prompt_version=PROMPT_VERSION,
    )


def _structured_answers(profile: Profile) -> Dict[str, str]:
    """Deterministic answers to the checkbox questions every ATS asks.

    These come from the profile, never the model. A hallucinated work
    authorization answer is a false statement on a legal form, so anything the
    profile does not state is left empty and escalated to the human instead.
    """
    answers = {
        "full_name": profile.name,
        "email": profile.email,
        "phone": profile.phone or "",
        "location": profile.location or "",
        "linkedin": profile.links.get("linkedin", ""),
        "github": profile.links.get("github", ""),
        "portfolio": profile.links.get("portfolio_url", ""),
    }

    # Only answer the authorization questions when the profile actually says.
    if profile.work_authorization:
        answers["work_authorization"] = profile.work_authorization
        # The standard ATS phrasing is "authorized to work WITHOUT sponsorship",
        # so someone who needs sponsorship answers No to that one.
        answers["authorized_to_work"] = "No" if profile.requires_sponsorship else "Yes"
        answers["requires_sponsorship"] = "Yes" if profile.requires_sponsorship else "No"

    return answers


def answer_question(
    question: str,
    job: Job,
    profile: Profile,
    llm: LLMClient,
    brief: Optional[Brief] = None,
) -> Optional[str]:
    """Answer a form question we did not anticipate.

    Returns None when the model says it had to bluff, which the apply engine
    turns into a barrier rather than submitting a made-up answer.
    """
    user = "{0}\n\nQUESTION TO ANSWER:\n{1}".format(_context(job, profile, brief), question)
    try:
        data = llm.complete_json(SINGLE_ANSWER_SYSTEM, user, max_tokens=800, temperature=0.4)
    except LLMError as exc:
        logger.warning("Live answer failed for %r: %s", question, exc)
        return None

    if not data.get("confident", False):
        logger.info("Model was not confident answering %r, escalating to human.", question)
        return None
    answer = str(data.get("answer") or "").strip()
    return answer or None
