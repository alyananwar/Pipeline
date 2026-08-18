"""The apply engine: open the posting, fill the form, stop at any wall.

Two things shape this module.

First, Scrapling runs `page_action` inside its own try/except and logs whatever
it catches, so a raise in there is swallowed. Every outcome therefore travels
back out through `self._outcome` rather than through an exception.

Second, the browser tab only exists for as long as `page_action` is running.
Parking a tab for the human to finish means blocking inside `page_action`, which
is exactly what _wait_for_human does.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from pipeline import barriers as barrier_rules
from pipeline.generate import answer_question
from pipeline.llm import LLMClient
from pipeline.models import Application, Barrier, Brief, Job, Profile
from pipeline.tracker import Tracker

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_DIR = Path("data/browser-profile")
PAGE_TIMEOUT_MS = 45000

# Ordered candidate selectors per logical field. First hit wins.
FIELD_SELECTORS: Dict[str, Tuple[str, ...]] = {
    "first_name": ("input#first_name", "input[name*='first_name' i]", "input[name*='firstname' i]", "input[autocomplete='given-name']"),
    "last_name": ("input#last_name", "input[name*='last_name' i]", "input[name*='lastname' i]", "input[autocomplete='family-name']"),
    "full_name": ("input#name", "input[name='name']", "input[autocomplete='name']"),
    "email": ("input[type='email']", "input#email", "input[name*='email' i]"),
    "phone": ("input[type='tel']", "input#phone", "input[name*='phone' i]"),
    "location": ("input[name*='location' i]", "input[name*='city' i]", "input#location"),
    "linkedin": ("input[name*='linkedin' i]", "input[name*='urls[LinkedIn]' i]"),
    "github": ("input[name*='github' i]",),
    "portfolio": ("input[name*='website' i]", "input[name*='portfolio' i]"),
}

COVER_LETTER_SELECTORS = (
    "textarea[name*='cover' i]",
    "textarea#cover_letter_text",
    "textarea[aria-label*='cover' i]",
    "textarea",
)

RESUME_SELECTORS = (
    "input[type='file'][name*='resume' i]",
    "input[type='file'][name*='cv' i]",
    "input[type='file']",
)

SUBMIT_SELECTORS = (
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
    "button:has-text('Apply')",
)


@dataclass
class Outcome:
    """What happened to one application attempt."""

    status: str                      # submitted | blocked | ready | failed
    barrier: Optional[Barrier] = None
    detail: str = ""
    url: str = ""
    filled: int = 0


class ApplyEngine:
    """Drives a real browser through application forms."""

    def __init__(
        self,
        profile: Profile,
        llm: LLMClient,
        tracker: Tracker,
        submit_mode: str = "confirm",
        headless: bool = False,
        user_data_dir: Path = DEFAULT_PROFILE_DIR,
        real_chrome: bool = True,
    ) -> None:
        if submit_mode not in ("confirm", "auto", "never"):
            raise ValueError("submit_mode must be confirm, auto, or never.")
        self.profile = profile
        self.llm = llm
        self.tracker = tracker
        self.submit_mode = submit_mode
        self.headless = headless
        self.real_chrome = real_chrome
        self.user_data_dir = user_data_dir
        self._session: Optional[Any] = None
        self._outcome = Outcome(status="failed", detail="never ran")

    # -- lifecycle ------------------------------------------------------------

    def __enter__(self) -> "ApplyEngine":
        try:
            from scrapling.fetchers import StealthySession
        except ImportError as exc:
            raise RuntimeError(
                "Applying needs a browser. Install it with:\n"
                '  pip install "scrapling[fetchers]"\n'
                "  scrapling install"
            ) from exc

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._session = StealthySession(
            headless=self.headless,
            real_chrome=self.real_chrome,
            user_data_dir=str(self.user_data_dir),
            solve_cloudflare=True,
            network_idle=True,
            timeout=PAGE_TIMEOUT_MS,
        )
        self._session.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._session is not None:
            self._session.__exit__(*exc_info)
            self._session = None

    # -- public ---------------------------------------------------------------

    def apply(self, job: Job, application: Application, brief: Optional[Brief] = None) -> Outcome:
        """Attempt one application. Never raises: every failure is an Outcome."""
        if self._session is None:
            return Outcome(status="failed", detail="Browser session is not open.")

        target = job.apply_url or job.url
        if not target:
            return Outcome(status="failed", detail="Posting has no apply URL.")

        self._outcome = Outcome(status="failed", detail="page_action did not complete", url=target)

        def drive(page: Any) -> None:
            self._outcome = self._drive(page, job, application, brief)

        try:
            self._session.fetch(target, page_action=drive)
        except Exception as exc:  # browser and network errors of many shapes
            logger.warning("Apply failed for %s: %s", job.title, exc)
            return Outcome(status="failed", detail=str(exc)[:200], url=target)

        return self._outcome

    # -- the actual drive -----------------------------------------------------

    def _drive(
        self,
        page: Any,
        job: Job,
        application: Application,
        brief: Optional[Brief],
    ) -> Outcome:
        url = self._safe(lambda: page.url, default=job.apply_url) or job.apply_url

        # A wall before we touch anything: login gate, captcha, employer account.
        text = self._page_text(page)
        early = barrier_rules.detect(text, url)
        if early is not None:
            return self._park(page, job, early[0], early[1], url)

        filled = self._fill_known_fields(page, application)
        filled += self._attach_resume(page, application)
        filled += self._fill_cover_letter(page, application)

        unresolved = self._fill_unknown_fields(page, job, application, brief)

        text = self._page_text(page)
        wall = barrier_rules.detect(text, url, filled_all_required=not unresolved)
        if wall is not None:
            detail = wall[1]
            if unresolved:
                detail = "{0} Unanswered: {1}".format(detail, ", ".join(unresolved[:3]))
            return self._park(page, job, wall[0], detail, url)

        if self.submit_mode == "never":
            return Outcome(status="ready", detail="Filled. Submit disabled.", url=url, filled=filled)

        if self.submit_mode == "confirm":
            return self._park(
                page, job, Barrier.AWAITING_SUBMIT,
                "Form filled with {0} fields. Review it, then confirm.".format(filled), url,
                allow_submit=True, filled=filled,
            )

        return self._submit(page, url, filled)

    # -- filling --------------------------------------------------------------

    def _fill_known_fields(self, page: Any, application: Application) -> int:
        answers = dict(application.answers)
        name_parts = (answers.get("full_name") or self.profile.name).split()
        values = {
            "first_name": name_parts[0] if name_parts else "",
            "last_name": " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
            "full_name": answers.get("full_name") or self.profile.name,
            "email": answers.get("email") or self.profile.email,
            "phone": answers.get("phone", ""),
            "location": answers.get("location", ""),
            "linkedin": answers.get("linkedin", ""),
            "github": answers.get("github", ""),
            "portfolio": answers.get("portfolio", ""),
        }

        filled = 0
        for field, selectors in FIELD_SELECTORS.items():
            value = values.get(field, "")
            if not value:
                continue
            if self._fill_first(page, selectors, value):
                filled += 1
        return filled

    def _fill_first(self, page: Any, selectors: Tuple[str, ...], value: str) -> bool:
        for selector in selectors:
            def attempt() -> bool:
                locator = page.locator(selector).first
                if locator.count() == 0 or not locator.is_visible(timeout=1500):
                    return False
                locator.fill(value, timeout=5000)
                return True

            if self._safe(attempt, default=False):
                return True
        return False

    def _attach_resume(self, page: Any, application: Application) -> int:
        path = application.resume_pdf_path
        if not path or not Path(path).is_file():
            logger.info("No resume file to attach.")
            return 0
        for selector in RESUME_SELECTORS:
            def attempt() -> bool:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    return False
                locator.set_input_files(path, timeout=10000)
                return True

            if self._safe(attempt, default=False):
                return 1
        return 0

    def _fill_cover_letter(self, page: Any, application: Application) -> int:
        if not application.cover_letter:
            return 0
        return 1 if self._fill_first(page, COVER_LETTER_SELECTORS, application.cover_letter) else 0

    def _fill_unknown_fields(
        self,
        page: Any,
        job: Job,
        application: Application,
        brief: Optional[Brief],
    ) -> List[str]:
        """Answer required questions we did not anticipate.

        Returns the questions that could not be answered honestly. Those become
        a barrier rather than a guess, because a made-up answer on an
        application is worse than an application that waits ten seconds for you.
        """
        questions = self._collect_unanswered(page)
        unresolved: List[str] = []

        for label, selector in questions:
            cached = application.answers.get(label)
            answer = cached or answer_question(label, job, self.profile, self.llm, brief)
            if not answer:
                unresolved.append(label)
                continue
            def attempt() -> bool:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    return False
                locator.fill(answer, timeout=5000)
                return True

            if not self._safe(attempt, default=False):
                unresolved.append(label)

        return unresolved

    def _collect_unanswered(self, page: Any, limit: int = 8) -> List[Tuple[str, str]]:
        """Find required, still-empty text inputs and read their labels."""
        script = """
        () => {
          const out = [];
          const nodes = document.querySelectorAll(
            'textarea, input[type=text], input:not([type])'
          );
          for (const el of nodes) {
            if (el.value && el.value.trim()) continue;
            const required = el.required || el.getAttribute('aria-required') === 'true';
            if (!required) continue;
            let label = '';
            if (el.id) {
              const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
              if (l) label = l.innerText;
            }
            if (!label && el.closest('label')) label = el.closest('label').innerText;
            if (!label) label = el.getAttribute('aria-label') || el.placeholder || el.name || '';
            label = (label || '').replace(/\\s+/g, ' ').trim().slice(0, 200);
            if (!label) continue;
            let sel = '';
            if (el.id) sel = '#' + CSS.escape(el.id);
            else if (el.name) sel = el.tagName.toLowerCase() + '[name="' + el.name + '"]';
            else continue;
            out.push([label, sel]);
          }
          return out.slice(0, 20);
        }
        """
        found = self._safe(lambda: page.evaluate(script), default=[]) or []
        pairs: List[Tuple[str, str]] = []
        for entry in found[:limit]:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                pairs.append((str(entry[0]), str(entry[1])))
        return pairs

    # -- barriers and submission ---------------------------------------------

    def _park(
        self,
        page: Any,
        job: Job,
        barrier: Barrier,
        detail: str,
        url: str,
        allow_submit: bool = False,
        filled: int = 0,
    ) -> Outcome:
        """Hand control to the human with the tab still open."""
        from datetime import datetime

        from pipeline.models import BlockedItem

        self.tracker.block(
            BlockedItem(
                job_external_id=job.external_id,
                company=job.company,
                title=job.title,
                barrier=barrier,
                detail=detail,
                url=url,
                created_at=datetime.now(),
            )
        )

        choice = self._wait_for_human(job, barrier, detail, allow_submit)

        if choice == "submit" and allow_submit:
            self.tracker.unblock(job.external_id)
            return self._submit(page, url, filled)
        if choice == "done":
            self.tracker.unblock(job.external_id)
            return Outcome(status="submitted", detail="Completed by hand.", url=url, filled=filled)
        return Outcome(status="blocked", barrier=barrier, detail=detail, url=url, filled=filled)

    def _wait_for_human(
        self, job: Job, barrier: Barrier, detail: str, allow_submit: bool
    ) -> str:
        """Block while the tab stays open. Returns submit, done, or skip."""
        if not sys.stdin.isatty():
            # Unattended run: record the barrier and move on rather than hang.
            return "skip"

        prompt = (
            "\n  {0} · {1}\n  {2}\n  {3}\n  > ".format(
                job.company,
                barrier.value.replace("_", " "),
                detail,
                "[Enter] submit   [s] skip   [d] I finished it myself"
                if allow_submit
                else "[Enter] skip   [d] I finished it myself",
            )
        )
        try:
            reply = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "skip"

        if reply == "d":
            return "done"
        if allow_submit and reply == "":
            return "submit"
        return "skip"

    def _submit(self, page: Any, url: str, filled: int) -> Outcome:
        for selector in SUBMIT_SELECTORS:
            def attempt() -> bool:
                locator = page.locator(selector).first
                if locator.count() == 0 or not locator.is_visible(timeout=1500):
                    return False
                locator.click(timeout=10000)
                return True

            if self._safe(attempt, default=False):
                self._safe(lambda: page.wait_for_timeout(3000))
                return Outcome(status="submitted", detail="Submitted.", url=url, filled=filled)

        return Outcome(
            status="blocked",
            barrier=Barrier.UNKNOWN_FIELD,
            detail="Could not find a submit button.",
            url=url,
            filled=filled,
        )

    # -- helpers --------------------------------------------------------------

    def _page_text(self, page: Any) -> str:
        """Visible text plus enough markup that widget class names show up."""
        body = self._safe(lambda: page.inner_text("body"), default="") or ""
        html = self._safe(lambda: page.content(), default="") or ""
        return "{0}\n{1}".format(body, html[:60000])

    @staticmethod
    def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
        """Run a Playwright call, swallowing the many ways a page can disagree."""
        try:
            return fn()
        except Exception as exc:
            logger.debug("Browser call failed: %s", exc)
            return default
