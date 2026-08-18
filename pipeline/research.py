"""Company and person research, synthesized into a reusable Brief.

Tiering matters here. The zero-key tier runs on public JSON and plain page
fetches and is on by default. The browser tier needs Scrapling's browsers and is
opt-in. The search tier needs an API key. Nothing in the zero-key tier touches a
login or risks anyone's account.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote_plus, urlparse

import httpx

from pipeline.llm import LLMClient, LLMError
from pipeline.models import Brief, Job
from pipeline.settings import Settings
from pipeline.sources import strip_html

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20.0
USER_AGENT = "Pipeline/0.1 (+https://github.com/alyananwar/Pipeline)"
MAX_SNIPPET_CHARS = 3000
PROMPT_VERSION = "research-v1"

HN_SEARCH = "https://hn.algolia.com/api/v1/search"


def _get(url: str, **kwargs: Any) -> Optional[httpx.Response]:
    """Fetch a URL, returning None instead of raising. Research is best-effort."""
    try:
        response = httpx.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            **kwargs,
        )
    except httpx.RequestError as exc:
        logger.debug("Research fetch failed for %s: %s", url, exc)
        return None
    if response.status_code != 200:
        logger.debug("Research fetch got HTTP %d for %s", response.status_code, url)
        return None
    return response


# -- zero-key gatherers -------------------------------------------------------

def gather_hacker_news(company: str, limit: int = 5) -> List[Dict[str, str]]:
    """Recent Hacker News stories mentioning the company.

    Good signal for funding rounds, launches, outages, and how engineers
    actually talk about the place.
    """
    response = _get(HN_SEARCH, params={"query": company, "tags": "story", "hitsPerPage": limit})
    if response is None:
        return []
    try:
        hits = response.json().get("hits", [])
    except ValueError:
        return []

    out: List[Dict[str, str]] = []
    for hit in hits:
        title = (hit.get("title") or "").strip()
        if not title:
            continue
        out.append(
            {
                "title": title,
                "url": hit.get("url") or "https://news.ycombinator.com/item?id={0}".format(hit.get("objectID")),
                "points": str(hit.get("points") or 0),
                "date": (hit.get("created_at") or "")[:10],
            }
        )
    return out


def gather_company_page(url: str) -> str:
    """Plain-text content of a company page, capped so it does not eat the prompt."""
    response = _get(url)
    if response is None:
        return ""
    text = strip_html(response.text)
    return text[:MAX_SNIPPET_CHARS]


def guess_company_site(job: Job) -> Optional[str]:
    """Best guess at the company's own domain from the posting URL.

    ATS-hosted postings do not carry the real domain, so this only fires when
    the posting lives on the company's own site.
    """
    if not job.url:
        return None
    host = urlparse(job.url).netloc.lower()
    ats_hosts = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "icims.com")
    if any(h in host for h in ats_hosts):
        return None
    return "{0}://{1}".format(urlparse(job.url).scheme or "https", host)


# -- key-gated search ---------------------------------------------------------

def web_search(query: str, settings: Settings, limit: int = 5) -> List[Dict[str, str]]:
    """General web search, only when the user supplied a key.

    Left out of the default path on purpose: the free scrapable search engines
    return challenge pages more often than results, which makes for a tool that
    looks broken on first run.
    """
    if not settings.search_api_key:
        return []

    if settings.search_provider == "brave":
        response = _get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": limit},
            headers={
                "User-Agent": USER_AGENT,
                "X-Subscription-Token": settings.search_api_key,
                "Accept": "application/json",
            },
        )
        if response is None:
            return []
        try:
            results = response.json().get("web", {}).get("results", [])
        except ValueError:
            return []
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
            for r in results[:limit]
        ]

    if settings.search_provider == "serper":
        try:
            response = httpx.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": settings.search_api_key, "Content-Type": "application/json"},
                json={"q": query, "num": limit},
                timeout=REQUEST_TIMEOUT,
            )
            results = response.json().get("organic", []) if response.status_code == 200 else []
        except (httpx.RequestError, ValueError):
            return []
        return [
            {"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", "")}
            for r in results[:limit]
        ]

    return []


# -- browser tier (opt-in) ----------------------------------------------------

def gather_with_browser(url: str) -> str:
    """Fetch a JS-heavy or bot-protected page via Scrapling.

    Imported lazily so the zero-key install never needs Scrapling's browsers.
    """
    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError:
        logger.info("Browser research requested but Scrapling is not installed.")
        return ""

    try:
        page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=45000)
    except Exception as exc:  # Scrapling raises a range of browser errors
        logger.debug("Stealth fetch failed for %s: %s", url, exc)
        return ""

    body = getattr(page, "html_content", None) or getattr(page, "body", "") or ""
    return strip_html(body)[:MAX_SNIPPET_CHARS]


# -- synthesis ----------------------------------------------------------------

RESEARCH_SYSTEM = """You are a research analyst preparing a candidate for a job application.

You will be given raw, unverified material about a company: news headlines, page
text, and a job description. Some of it may be irrelevant or about a different
company with a similar name. Ignore anything that does not clearly refer to the
company named in the request.

Return ONLY a JSON object with these keys:
  summary        - two sentences on what the company is and where it stands
  what_they_do   - one paragraph a candidate could paraphrase in an interview
  recent_signals - array of short strings: funding, launches, growth, notable news
  tech_stack     - array of technologies you have direct evidence for
  talking_points - array of specific things a candidate could reference to show
                   genuine interest, each tied to real evidence in the material

Rules:
- Never invent facts. If the material does not support a field, return an empty
  array or an empty string.
- talking_points must be specific enough that a generic applicant could not have
  written them without doing this research.
- No marketing language."""


def build_brief(
    job: Job,
    llm: LLMClient,
    settings: Settings,
    use_browser: bool = False,
) -> Brief:
    """Gather everything available about the company, then synthesize one Brief."""
    company = job.company
    material: List[str] = []
    sources: List[str] = []

    if job.description:
        material.append("JOB DESCRIPTION:\n{0}".format(job.description[:MAX_SNIPPET_CHARS]))
        sources.append(job.url)

    stories = gather_hacker_news(company)
    if stories:
        lines = ["{0} ({1}, {2} points)".format(s["title"], s["date"], s["points"]) for s in stories]
        material.append("HACKER NEWS MENTIONS:\n" + "\n".join(lines))
        sources.append(HN_SEARCH)

    site = guess_company_site(job)
    if site:
        page = gather_company_page(site)
        if page:
            material.append("COMPANY SITE:\n{0}".format(page))
            sources.append(site)

    for result in web_search("{0} company engineering culture funding".format(company), settings):
        material.append("WEB: {0}\n{1}\n{2}".format(result["title"], result["url"], result["snippet"]))
        sources.append(result["url"])

    if use_browser:
        linkedin = "https://www.linkedin.com/company/{0}/".format(
            re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
        )
        page = gather_with_browser(linkedin)
        if page:
            material.append("LINKEDIN COMPANY PAGE:\n{0}".format(page))
            sources.append(linkedin)

    if not material:
        return Brief(
            company=company,
            summary="No public material found.",
            what_they_do="",
            sources=[],
        )

    user = "Company: {0}\nRole being applied for: {1}\n\n{2}".format(
        company, job.title, "\n\n---\n\n".join(material)
    )

    try:
        data = llm.complete_json(RESEARCH_SYSTEM, user, max_tokens=1500)
    except LLMError as exc:
        logger.warning("Research synthesis failed for %s: %s", company, exc)
        return Brief(company=company, summary="Research unavailable.", what_they_do="", sources=sources)

    return Brief(
        company=company,
        summary=str(data.get("summary") or "").strip(),
        what_they_do=str(data.get("what_they_do") or "").strip(),
        recent_signals=_string_list(data.get("recent_signals")),
        tech_stack=_string_list(data.get("tech_stack")),
        talking_points=_string_list(data.get("talking_points")),
        sources=sources,
    )


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def brief_to_markdown(brief: Brief) -> str:
    """Human-readable brief, written next to the tracker for reading over coffee."""
    lines = ["# {0}".format(brief.company), "", brief.summary, ""]
    if brief.what_they_do:
        lines += ["## What they do", "", brief.what_they_do, ""]
    for heading, items in (
        ("Recent signals", brief.recent_signals),
        ("Tech stack", brief.tech_stack),
        ("Talking points", brief.talking_points),
    ):
        if items:
            lines += ["## {0}".format(heading), ""] + ["- {0}".format(i) for i in items] + [""]
    if brief.sources:
        lines += ["## Sources", ""] + ["- {0}".format(s) for s in brief.sources] + [""]
    return "\n".join(lines)
