"""Job sources.

Every source here is a public JSON endpoint that needs no key, no login, and no
browser. That is deliberate: a fresh install should find real jobs before the
user has configured anything beyond their profile.

Adding a source means writing one `_fetch_*` function that returns List[Job] and
registering it in SOURCES.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

import httpx

from pipeline.models import Job

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0
USER_AGENT = "Pipeline/0.1 (+https://github.com/alyananwar/Pipeline)"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")


class SourceError(RuntimeError):
    """A source could not be reached or returned something unusable.

    `is_missing` separates "you typed the board slug wrong" from "the network is
    having a bad day". They need opposite reactions from the user, so the two
    must never be reported with the same words.
    """

    def __init__(self, message: str, is_missing: bool = False) -> None:
        super().__init__(message)
        self.is_missing = is_missing


class SourceResult(NamedTuple):
    """What one board returned: the jobs kept, and how many were looked at."""

    jobs: List["Job"]
    seen: int


class BoardReport(NamedTuple):
    """Per-board outcome, so the CLI can explain a small result set."""

    source: str
    board: str
    matched: int
    seen: int
    error: Optional[str] = None
    missing: bool = False


def make_id(source: str, company: str, title: str, location: str) -> str:
    """Stable id so re-scraping the same posting does not create a duplicate."""
    key = "{0}::{1}::{2}::{3}".format(
        source.lower(), company.lower().strip(), title.lower().strip(), (location or "").lower().strip()
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def strip_html(raw: Optional[str]) -> str:
    """Turn an ATS description blob into readable plain text."""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = _WS_RE.sub("\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Seniority buckets, lowest to highest. A posting and a target must land in the
# same bucket to match, with one exception noted in _LEVEL_COMPATIBLE.
#
# This exists because keyword overlap alone is useless on real boards: a profile
# targeting "Software Engineer Intern" shares two of three tokens with "Staff
# Software Engineer, Inference", so a fuzzy match hands a student 77 senior roles
# they cannot get and bills them for an LLM call on every one.
LEVEL_INTERN = 0
LEVEL_ENTRY = 1
LEVEL_MID = 2      # no level word in the title
LEVEL_SENIOR = 3
LEVEL_STAFF = 4
LEVEL_LEAD = 5

_LEVEL_WORDS = (
    (LEVEL_INTERN, ("intern", "interns", "internship", "internships", "co-op", "coop", "trainee")),
    (LEVEL_ENTRY, ("junior", "jr", "entry", "grad", "graduate", "associate", "apprentice", "early-career")),
    (LEVEL_LEAD, ("manager", "director", "head", "vp", "chief", "president")),
    (LEVEL_STAFF, ("staff", "principal", "distinguished", "fellow", "architect")),
    (LEVEL_SENIOR, ("senior", "sr", "snr", "lead", "ii", "iii", "iv")),
)

# Entry level and unmarked titles are the same job market in practice: a new grad
# targeting "Software Engineer" should still see "Junior Software Engineer".
_LEVEL_COMPATIBLE = {LEVEL_ENTRY, LEVEL_MID}


def _tokens(text: str) -> List[str]:
    """Lowercase word tokens, keeping the characters that matter in job titles."""
    return [t for t in re.split(r"[^a-z0-9+#]+", (text or "").lower()) if t]


def detect_level(title: str) -> int:
    """The seniority a job title advertises.

    Returns LEVEL_MID when the title carries no level word, which is how the vast
    majority of mid-level postings are written. Highest match wins so that
    "Staff + Sr. Software Engineer" reads as staff rather than senior.
    """
    # Hyphenated levels ("co-op", "early-career") tokenize into halves, so match
    # against a squashed copy of the title as well as the plain tokens.
    words = set(_tokens(title)) | set(_tokens((title or "").replace("-", "")))
    best = LEVEL_MID
    found = False
    for level, needles in _LEVEL_WORDS:
        for needle in needles:
            if needle.replace("-", "") in words:
                if not found or level > best:
                    best = level
                    found = True
                break
    return best if found else LEVEL_MID


def levels_compatible(target_level: int, posting_level: int) -> bool:
    """Whether a posting's level is one the target is actually shopping for."""
    if target_level == posting_level:
        return True
    return target_level in _LEVEL_COMPATIBLE and posting_level in _LEVEL_COMPATIBLE


def title_matches(title: str, wanted: Sequence[str], threshold: float = 0.5) -> bool:
    """Fuzzy keyword match against the user's target titles, gated on seniority.

    Two gates, both of which a posting has to pass.

    Seniority first. Without it an intern search returns mostly Staff and
    Principal roles, because "Software Engineer Intern" shares two of three words
    with "Staff Software Engineer, Inference". On a real three-board scan that
    was 77 senior roles out of 107 matches, every one of them billed to the LLM
    scorer to be told no.

    Then keyword overlap on what is left once the level words are removed: at
    least `threshold` of the target's words, and never fewer than two distinct
    words. The floor matters because half of two words is one word, which would
    let "Software Engineer" accept every "AI Engineer" on the board. Words match
    on shared stems so "engineer" still finds "engineering".

    The LLM scorer is the real filter on everything else. This just keeps the
    scoring bill proportional to the number of plausible jobs.
    """
    if not wanted:
        return True

    posting_level = detect_level(title)
    haystack = set(_tokens(title))

    for want in wanted:
        if not want:
            continue
        if not levels_compatible(detect_level(str(want)), posting_level):
            continue
        # Level words carry the seniority signal, not the role signal, so they
        # must not also count toward keyword overlap. Otherwise "intern" alone
        # would push a half-related title over the threshold.
        tokens = [t for t in _tokens(str(want)) if len(t) > 2 and detect_level(t) == LEVEL_MID]
        if not tokens:
            continue
        hits = sum(1 for token in tokens if _token_hit(token, haystack))
        # Two distinct words minimum, because a fraction alone lets a two-word
        # target match on one word: "Software Engineer" would otherwise accept
        # every "AI Engineer" and "Quality Engineer" on the board.
        needed = max(2, round(len(tokens) * threshold)) if len(tokens) >= 2 else 1
        if hits >= needed:
            return True
    return False


def _token_hit(token: str, haystack: Sequence[str]) -> bool:
    """Whether a target word appears in a posting title, allowing shared stems.

    "engineer" has to match "engineering" or half of the real postings on a board
    fall out. Prefix matching is capped at four characters so short tokens do not
    start matching everything.
    """
    for word in haystack:
        if word == token:
            return True
        if len(token) >= 4 and len(word) >= 4 and (word.startswith(token) or token.startswith(word)):
            return True
    return False


def _get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    try:
        response = httpx.get(
            url,
            params=params,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
    except httpx.RequestError as exc:
        raise SourceError("Could not reach {0}: {1}".format(url, exc)) from exc

    if response.status_code == 404:
        raise SourceError("No board found at {0} (HTTP 404).".format(url), is_missing=True)
    if response.status_code != 200:
        raise SourceError("{0} returned HTTP {1}.".format(url, response.status_code))

    try:
        return response.json()
    except ValueError as exc:
        raise SourceError("{0} did not return JSON.".format(url)) from exc


# -- greenhouse ---------------------------------------------------------------

def fetch_greenhouse(board: str, titles: Sequence[str]) -> SourceResult:
    payload = _get_json(
        "https://boards-api.greenhouse.io/v1/boards/{0}/jobs".format(board),
        params={"content": "true"},
    )
    listings = payload.get("jobs", [])
    jobs: List[Job] = []
    for item in listings:
        title = (item.get("title") or "").strip()
        if not title or not title_matches(title, titles):
            continue
        company = (item.get("company_name") or board).strip()
        location = ((item.get("location") or {}).get("name") or "").strip()
        departments = item.get("departments") or []
        url = item.get("absolute_url") or ""
        jobs.append(
            Job(
                external_id=make_id("greenhouse", company, title, location),
                source="greenhouse",
                company=company,
                title=title,
                url=url,
                apply_url=url,
                location=location or None,
                department=(departments[0].get("name") if departments else None),
                description=strip_html(item.get("content")),
                remote="remote" in location.lower(),
                posted_at=item.get("first_published") or item.get("updated_at"),
                raw={"id": item.get("id")},
            )
        )
    return SourceResult(jobs, len(listings))


# -- lever --------------------------------------------------------------------

def fetch_lever(board: str, titles: Sequence[str]) -> SourceResult:
    payload = _get_json(
        "https://api.lever.co/v0/postings/{0}".format(board), params={"mode": "json"}
    )
    if not isinstance(payload, list):
        raise SourceError("Lever board '{0}' returned an unexpected shape.".format(board))

    jobs: List[Job] = []
    for item in payload:
        title = (item.get("text") or "").strip()
        if not title or not title_matches(title, titles):
            continue
        categories = item.get("categories") or {}
        location = (categories.get("location") or "").strip()
        description = item.get("descriptionPlain") or ""
        extra = item.get("additionalPlain") or ""
        jobs.append(
            Job(
                external_id=make_id("lever", board, title, location),
                source="lever",
                company=board,
                title=title,
                url=item.get("hostedUrl") or "",
                apply_url=item.get("applyUrl") or item.get("hostedUrl") or "",
                location=location or None,
                department=(categories.get("department") or categories.get("team") or None),
                description="\n\n".join(p for p in (description, extra) if p).strip(),
                remote=(item.get("workplaceType") or "").lower() == "remote",
                posted_at=str(item.get("createdAt") or "") or None,
                raw={"id": item.get("id")},
            )
        )
    return SourceResult(jobs, len(payload))


# -- ashby --------------------------------------------------------------------

def fetch_ashby(board: str, titles: Sequence[str]) -> SourceResult:
    payload = _get_json("https://api.ashbyhq.com/posting-api/job-board/{0}".format(board))
    listings = payload.get("jobs", [])
    jobs: List[Job] = []
    for item in listings:
        if item.get("isListed") is False:
            continue
        title = (item.get("title") or "").strip()
        if not title or not title_matches(title, titles):
            continue
        location = (item.get("location") or "").strip()
        jobs.append(
            Job(
                external_id=make_id("ashby", board, title, location),
                source="ashby",
                company=board,
                title=title,
                url=item.get("jobUrl") or "",
                apply_url=item.get("applyUrl") or item.get("jobUrl") or "",
                location=location or None,
                department=item.get("department") or item.get("team"),
                description=item.get("descriptionPlain") or "",
                remote=bool(item.get("isRemote")),
                posted_at=item.get("publishedAt"),
                raw={"id": item.get("id"), "employmentType": item.get("employmentType")},
            )
        )
    return SourceResult(jobs, len(listings))


SOURCES: Dict[str, Callable[[str, Sequence[str]], SourceResult]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_all(
    boards: Iterable[Dict[str, str]],
    titles: Sequence[str],
    on_progress: Optional[Callable[[BoardReport], None]] = None,
) -> Tuple[List[Job], List[BoardReport]]:
    """Run every configured board, skipping the ones that fail.

    One dead board must never kill a whole scan, so failures are recorded and the
    run continues. Returns the jobs plus a per-board report, because "1 job
    found" is alarming on its own and reassuring next to "scanned 210 postings,
    filtered 209 on role and seniority".
    """
    collected: List[Job] = []
    reports: List[BoardReport] = []

    def record(report: BoardReport) -> None:
        reports.append(report)
        if on_progress:
            on_progress(report)

    for entry in boards:
        source = (entry.get("source") or "").strip().lower()
        board = (entry.get("board") or "").strip()
        if source not in SOURCES:
            record(BoardReport(source or "?", board or "?", 0, 0,
                               "unknown source, expected one of: {0}".format(
                                   ", ".join(sorted(SOURCES)))))
            continue
        if not board:
            record(BoardReport(source, "?", 0, 0, "no board slug set"))
            continue
        try:
            result = SOURCES[source](board, titles)
        except SourceError as exc:
            logger.warning("%s/%s failed: %s", source, board, exc)
            record(BoardReport(source, board, 0, 0, str(exc), missing=exc.is_missing))
            continue
        collected.extend(result.jobs)
        record(BoardReport(source, board, len(result.jobs), result.seen))

    return collected, reports
