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
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

import httpx

from pipeline.models import Job

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0
USER_AGENT = "Pipeline/0.1 (+https://github.com/alyananwar/Pipeline)"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")


class SourceError(RuntimeError):
    """A source could not be reached or returned something unusable."""


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


def title_matches(title: str, wanted: Sequence[str], threshold: float = 0.5) -> bool:
    """Fuzzy keyword match against the user's target titles.

    A title matches when it shares at least `threshold` of the distinctive
    tokens from any one target. Single-token overlap is too loose ("Security"
    alone pulls in every security-adjacent role at a big company) and requiring
    every token is too strict ("Cybersecurity Intern" would miss "Security
    Analyst Intern"). Half the tokens lands in between.

    The LLM scorer is still the real filter. This just keeps the scoring bill
    proportional to the number of plausible jobs.
    """
    if not wanted:
        return True
    haystack = set(t for t in re.split(r"[^a-z0-9+#]+", title.lower()) if t)
    for want in wanted:
        tokens = [t for t in re.split(r"[^a-z0-9+#]+", want.lower()) if len(t) > 2]
        if not tokens:
            continue
        hits = sum(1 for token in tokens if token in haystack)
        if hits / len(tokens) >= threshold:
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
        raise SourceError("No board found at {0} (HTTP 404).".format(url))
    if response.status_code != 200:
        raise SourceError("{0} returned HTTP {1}.".format(url, response.status_code))

    try:
        return response.json()
    except ValueError as exc:
        raise SourceError("{0} did not return JSON.".format(url)) from exc


# -- greenhouse ---------------------------------------------------------------

def fetch_greenhouse(board: str, titles: Sequence[str]) -> List[Job]:
    payload = _get_json(
        "https://boards-api.greenhouse.io/v1/boards/{0}/jobs".format(board),
        params={"content": "true"},
    )
    jobs: List[Job] = []
    for item in payload.get("jobs", []):
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
    return jobs


# -- lever --------------------------------------------------------------------

def fetch_lever(board: str, titles: Sequence[str]) -> List[Job]:
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
    return jobs


# -- ashby --------------------------------------------------------------------

def fetch_ashby(board: str, titles: Sequence[str]) -> List[Job]:
    payload = _get_json("https://api.ashbyhq.com/posting-api/job-board/{0}".format(board))
    jobs: List[Job] = []
    for item in payload.get("jobs", []):
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
    return jobs


SOURCES: Dict[str, Callable[[str, Sequence[str]], List[Job]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_all(
    boards: Iterable[Dict[str, str]],
    titles: Sequence[str],
    on_progress: Optional[Callable[[str, str, int], None]] = None,
) -> List[Job]:
    """Run every configured board, skipping the ones that fail.

    One dead board must never kill a whole scan, so failures are logged and the
    run continues.
    """
    collected: List[Job] = []
    for entry in boards:
        source = (entry.get("source") or "").strip().lower()
        board = (entry.get("board") or "").strip()
        if source not in SOURCES:
            logger.warning("Unknown source '%s', skipping.", source)
            continue
        if not board:
            logger.warning("Board entry for '%s' has no slug, skipping.", source)
            continue
        try:
            found = SOURCES[source](board, titles)
        except SourceError as exc:
            logger.warning("%s/%s failed: %s", source, board, exc)
            if on_progress:
                on_progress(source, board, -1)
            continue
        collected.extend(found)
        if on_progress:
            on_progress(source, board, len(found))
    return collected
