"""One-page resume PDF built from profile.yml.

For people who already have a resume, `preferences.baseline_resume` points at
their own PDF and this module is never called. For everyone else, this produces
something ATS-parseable: real text, single column, no tables, no graphics.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from pipeline.models import Profile

logger = logging.getLogger(__name__)

# Tuned so a normal amount of content lands on exactly one page. If a resume
# overflows, the fix is cutting content, not shrinking type below 9pt.
BODY_SIZE = 9.5
MIN_BODY_SIZE = 9.0
NAME_SIZE = 19
SECTION_SIZE = 10.5
MARGIN = 0.5 * inch

# One tightening pass before giving up. Enough to absorb a resume that runs a few
# lines long, not enough to pretend a three-page career fits on one page.
RETRY_SCALE = 0.92


class ResumeOverflow(RuntimeError):
    """The resume did not fit on one page even after tightening."""

    def __init__(self, path: Path, pages: int) -> None:
        super().__init__(
            "Resume came out {0} pages. Trim the longest sections in profile.yml "
            "(fewer bullets, or drop the oldest entry) and run generate again.".format(pages)
        )
        self.path = path
        self.pages = pages


def _styles(scale: float = 1.0) -> Dict[str, ParagraphStyle]:
    """Body styles. `scale` tightens type and spacing for the second-pass retry.

    Never called with a scale that drops body text below MIN_BODY_SIZE. Below
    that a resume stops being comfortably readable, and the honest fix is cutting
    content rather than hiding it in 8pt type.
    """
    body_size = max(MIN_BODY_SIZE, BODY_SIZE * scale)
    section_size = max(MIN_BODY_SIZE + 0.5, SECTION_SIZE * scale)
    name_size = max(15.0, NAME_SIZE * scale)
    base = ParagraphStyle(
        "body",
        fontName="Helvetica",
        fontSize=body_size,
        leading=body_size * (1.28 if scale >= 1.0 else 1.18),
        alignment=TA_LEFT,
        spaceAfter=1.5 * scale,
    )
    return {
        "body": base,
        "name": ParagraphStyle(
            "name", parent=base, fontName="Helvetica-Bold", fontSize=name_size,
            leading=name_size * 1.1, spaceAfter=3 * scale,
        ),
        "contact": ParagraphStyle(
            "contact", parent=base, fontSize=body_size - 0.5, textColor="#333333",
            spaceAfter=8 * scale,
        ),
        "section": ParagraphStyle(
            "section", parent=base, fontName="Helvetica-Bold", fontSize=section_size,
            spaceBefore=8 * scale, spaceAfter=2 * scale,
        ),
        "role": ParagraphStyle("role", parent=base, fontName="Helvetica-Bold", spaceAfter=0.5),
        "bullet": ParagraphStyle(
            "bullet", parent=base, leftIndent=10, bulletIndent=2, spaceAfter=1 * scale,
        ),
    }


def _escape(text: Any) -> str:
    """Escape the three characters reportlab's mini-markup cares about."""
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _contact_line(profile: Profile) -> str:
    bits: List[str] = [profile.email]
    if profile.phone:
        bits.append(profile.phone)
    if profile.location:
        bits.append(profile.location)
    for key in ("linkedin", "github", "portfolio_url"):
        value = profile.links.get(key)
        if value:
            bits.append(value.replace("https://", "").replace("http://", ""))
    return "  |  ".join(_escape(b) for b in bits if b)


def _section(story: List[Any], styles: Dict[str, ParagraphStyle], title: str) -> None:
    story.append(Paragraph(title.upper(), styles["section"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color="#888888", spaceAfter=3))


def _entries(
    story: List[Any],
    styles: Dict[str, ParagraphStyle],
    items: Sequence[Dict[str, Any]],
    title_keys: Sequence[str],
    meta_keys: Sequence[str],
) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        heading = ", ".join(
            _escape(item[k]) for k in title_keys if item.get(k)
        )
        meta = "  |  ".join(_escape(item[k]) for k in meta_keys if item.get(k))
        if heading and meta:
            story.append(Paragraph("{0}<br/><font size='8.5' color='#444444'>{1}</font>".format(heading, meta), styles["role"]))
        elif heading:
            story.append(Paragraph(heading, styles["role"]))

        for bullet in item.get("bullets") or []:
            story.append(Paragraph(_escape(bullet), styles["bullet"], bulletText="•"))
        story.append(Spacer(1, 3))


def render_resume(
    profile: Profile,
    output_path: Path,
    highlight: Optional[Sequence[str]] = None,
    strict: bool = False,
) -> Path:
    """Write a one-page resume PDF and return its path.

    `highlight` reorders skills so the ones a specific posting asked for appear
    first. Nothing is invented: only skills already in the profile are used.

    One page is a promise the README makes, so it is checked rather than assumed.
    A resume that runs long gets one tightening pass; if it still spills, that is
    a content problem the candidate has to decide about. With `strict` the
    overflow raises, otherwise it is logged and the two-page file is returned so
    an unattended run still has something to attach.
    """
    _build_once(profile, output_path, highlight, scale=1.0)
    pages = page_count(output_path)
    if pages <= 1:
        return output_path

    logger.info("Resume ran to %d pages, retrying tighter.", pages)
    _build_once(profile, output_path, highlight, scale=RETRY_SCALE)
    pages = page_count(output_path)
    if pages <= 1:
        return output_path

    if strict:
        raise ResumeOverflow(output_path, pages)
    logger.warning(
        "Resume for %s is %d pages. Trim profile.yml to get it onto one.",
        profile.name, pages,
    )
    return output_path


def _build_once(
    profile: Profile,
    output_path: Path,
    highlight: Optional[Sequence[str]],
    scale: float,
) -> Path:
    raw = profile.raw
    styles = _styles(scale)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title="{0} Resume".format(profile.name),
        author=profile.name,
    )

    story: List[Any] = [
        Paragraph(_escape(profile.name), styles["name"]),
        Paragraph(_contact_line(profile), styles["contact"]),
    ]

    summary = (profile.narrative or {}).get("headline")
    if summary:
        _section(story, styles, "Summary")
        story.append(Paragraph(_escape(summary), styles["body"]))

    experience = raw.get("experience") or []
    if experience:
        _section(story, styles, "Experience")
        _entries(story, styles, experience, ("title", "company"), ("location", "dates"))

    projects = raw.get("projects") or []
    if projects:
        _section(story, styles, "Projects")
        _entries(story, styles, projects, ("name",), ("tech", "link"))

    skills = raw.get("skills") or {}
    if skills:
        _section(story, styles, "Skills")
        for group, values in skills.items():
            if not values:
                continue
            ordered = _prioritize(list(values), highlight)
            story.append(
                Paragraph(
                    "<b>{0}:</b> {1}".format(
                        _escape(str(group).replace("_", " ").title()),
                        _escape(", ".join(str(v) for v in ordered)),
                    ),
                    styles["body"],
                )
            )

    education = raw.get("education") or []
    if education:
        _section(story, styles, "Education")
        _entries(story, styles, education, ("degree", "school"), ("location", "dates"))

    doc.build(story)
    logger.info("Wrote resume to %s", output_path)
    return output_path


def _prioritize(values: List[Any], highlight: Optional[Sequence[str]]) -> List[Any]:
    """Move skills the posting asked for to the front. Never adds new ones."""
    if not highlight:
        return values
    wanted = {h.lower().strip() for h in highlight}
    front = [v for v in values if str(v).lower().strip() in wanted]
    back = [v for v in values if str(v).lower().strip() not in wanted]
    return front + back


def page_count(pdf_path: Path) -> int:
    """Count pages without a PDF library, by counting page objects in the raw file."""
    try:
        blob = pdf_path.read_bytes()
    except OSError:
        return 0
    return blob.count(b"/Type /Page") - blob.count(b"/Type /Pages")
