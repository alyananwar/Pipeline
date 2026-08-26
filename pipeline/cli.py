"""Pipeline command line interface."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path
from typing import List, NoReturn, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from pipeline.apply import ApplyEngine
from pipeline.generate import build_application
from pipeline.llm import LLMClient
from pipeline.matcher import score_job
from pipeline.models import Job, Stage
from pipeline.research import brief_to_markdown, build_brief
from pipeline.resume import page_count
from pipeline.settings import (
    ConfigError,
    Settings,
    load_profile,
    load_settings,
    profile_titles,
)
from pipeline.sources import fetch_all
from pipeline.storage import Storage
from pipeline.tracker import Tracker, print_blocked

console = Console()
logger = logging.getLogger(__name__)

# Shipped inside the package so `pipeline init` works after a pip install,
# where the repo's config/ directory does not exist.
PACKAGED_PROFILE = Path(__file__).parent / "templates" / "profile.example.yml"
REPO_PROFILE = Path("config/profile.example.yml")
PROFILE_PATH = Path("config/profile.yml")
ENV_PATH = Path(".env")

ENV_TEMPLATE = """# Which model Pipeline talks to. Everything stays between you and this provider.
# anthropic | openai | openrouter | groq | ollama
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-5
LLM_API_KEY=

# Local and free: no key needed, nothing leaves your machine.
# LLM_PROVIDER=ollama
# LLM_MODEL=llama3.1

# Optional. Adds general web search to the research stage.
# BRAVE_API_KEY=
# SERPER_API_KEY=

# Optional. Turn on browser-based sources. Needs: scrapling install
# PIPELINE_BROWSER=1
"""


# -- helpers ------------------------------------------------------------------

def _bootstrap(args: argparse.Namespace, needs_llm: bool = True):
    """Load settings, profile, and storage, or exit with something actionable.

    Order matters. A missing profile is checked before the LLM config, because
    the overwhelmingly common cause of both is running from the wrong folder,
    and "LLM_MODEL is not set" sends that user hunting in completely the wrong
    place.
    """
    try:
        settings = load_settings(Path(args.profile) if args.profile else None)
    except ConfigError as exc:
        _fail(str(exc))

    if not settings.profile_path.is_file():
        _fail(
            "No profile at {0}\n\n"
            "You are in: {1}\n"
            "Pipeline reads its config from the folder you run it in.\n"
            "  - First time here?  Run: pipeline init\n"
            "  - Set it up already? cd to that folder and run the command again.".format(
                settings.profile_path, Path.cwd()
            )
        )

    try:
        profile = load_profile(settings.profile_path)
    except ConfigError as exc:
        _fail(str(exc))

    if needs_llm and settings.llm is None:
        _fail(
            "{0}\n\nEdit .env in {1} and set it, then run this again.\n"
            "No key handy? Set LLM_PROVIDER=ollama and LLM_MODEL=llama3.1 to run "
            "a free local model instead.".format(settings.llm_error, Path.cwd())
        )

    return settings, profile, Storage(settings.db_path)


def _fail(message: str) -> "NoReturn":
    """Print a configuration problem exactly as written and stop."""
    console.print("[red]Configuration problem[/red]")
    # markup off: these messages carry paths and package extras like
    # scrapling[fetchers], which Rich would otherwise read as markup and delete.
    console.print(message, markup=False, highlight=False)
    raise SystemExit(1)


def _tracker(args: argparse.Namespace) -> Tracker:
    return Tracker(console=console, quiet=getattr(args, "quiet", False))


# -- commands -----------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    """Create config/profile.yml and .env, then say what to do next."""
    console.print(Panel("[bold]Pipeline setup[/bold]", border_style="blue"))

    template = REPO_PROFILE if REPO_PROFILE.exists() else PACKAGED_PROFILE
    if PROFILE_PATH.exists():
        console.print("[yellow]config/profile.yml already exists, leaving it alone.[/yellow]")
    elif template.exists():
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(template, PROFILE_PATH)
        console.print("[green]Created[/green] config/profile.yml")
    else:
        console.print("[red]Profile template is missing from the install.[/red]")
        return 1

    if ENV_PATH.exists():
        console.print("[yellow].env already exists, leaving it alone.[/yellow]")
    else:
        ENV_PATH.write_text(ENV_TEMPLATE, encoding="utf-8")
        console.print("[green]Created[/green] .env")

    console.print()
    console.print("[bold]Next, in this order:[/bold]")
    console.print("  1. Edit [cyan]config/profile.yml[/cyan]: your details, target roles, and boards to scan.")
    console.print("  2. Run [cyan]pipeline scrape[/cyan] to check it finds real jobs. No key needed for this.")
    console.print("  3. Edit [cyan].env[/cyan] to pick an AI model, then run [cyan]pipeline run[/cyan].")
    console.print("     Free and local: set LLM_PROVIDER=ollama and LLM_MODEL=llama3.1")
    console.print()
    console.print("Run Pipeline from this folder every time. It reads its config from here.")
    console.print()
    console.print("Applying in a browser is optional and needs two more commands:",
                  style="dim", markup=False, highlight=False)
    console.print('  pip install "scrapling[fetchers]" && scrapling install',
                  style="dim", markup=False, highlight=False)
    return 0


def cmd_scrape(args: argparse.Namespace) -> int:
    settings, profile, storage = _bootstrap(args, needs_llm=False)
    boards = profile.raw.get("sources") or []
    if not boards:
        console.print("[red]No sources in profile.yml.[/red] Add at least one board under `sources:`.")
        return 1

    titles = profile_titles(profile)
    reports = []
    with storage, _tracker(args) as tracker:
        tracker.activity("scanning {0} boards".format(len(boards)))

        def progress(report) -> None:
            if report.error:
                tracker.activity("{0}/{1} skipped, {2}".format(report.source, report.board, report.error))
            else:
                tracker.activity(
                    "{0}/{1} → {2} of {3} match".format(
                        report.source, report.board, report.matched, report.seen
                    )
                )

        jobs, reports = fetch_all(boards, titles, on_progress=progress)
        new = 0
        for job in jobs:
            if storage.upsert_job(job):
                new += 1
                tracker.track(job.external_id, job.company, job.title, job.source)
                tracker.update(job.external_id, "scraped")
        tracker.activity("done")

    _report_boards(reports, titles, len(jobs), new)
    return 0


def _report_boards(reports, titles, matched: int, new: int) -> None:
    """Explain the scan, especially when it found little or nothing."""
    seen = sum(r.seen for r in reports)
    ok = [r for r in reports if not r.error]
    bad = [r for r in reports if r.error]

    console.print()
    if ok:
        console.print(
            "Scanned [bold]{0}[/bold] postings across {1} board{2}. "
            "[bold]{3}[/bold] matched your target roles, [green]{4} new[/green].".format(
                seen, len(ok), "" if len(ok) == 1 else "s", matched, new
            )
        )

    for r in bad:
        hint = (
            "check the slug in your careers URL"
            if r.missing
            else "network or the board is down, try again"
        )
        console.print(
            "[yellow]skipped[/yellow] {0}/{1}: {2} [dim]({3})[/dim]".format(
                r.source, r.board, r.error, hint
            ),
            markup=True, highlight=False,
        )

    if ok and matched == 0:
        console.print()
        console.print("[yellow]Nothing matched.[/yellow] Pipeline filters on role AND seniority.")
        console.print("  You asked for: [cyan]{0}[/cyan]".format(", ".join(titles) or "nothing"))
        console.print("  Try: add more titles under [cyan]target_roles.primary[/cyan], or add boards under [cyan]sources[/cyan].")
        console.print("  [dim]An intern search only returns intern postings, never Senior or Staff ones.[/dim]")



def cmd_research(args: argparse.Namespace) -> int:
    settings, profile, storage = _bootstrap(args)
    out_dir = Path("data/research")
    out_dir.mkdir(parents=True, exist_ok=True)

    with storage, LLMClient(settings.require_llm()) as llm, _tracker(args) as tracker:
        jobs = storage.jobs_at_stage(Stage.SCRAPED, limit=args.limit)
        if not jobs:
            console.print("Nothing to research. Run [cyan]pipeline scrape[/cyan] first.")
            return 0

        seen = set()
        for job in jobs:
            tracker.track(job.external_id, job.company, job.title, job.source)
            if job.company in seen or storage.get_brief(job.company):
                storage.set_stage(job.external_id, Stage.RESEARCHED)
                tracker.update(job.external_id, "researched", "cached")
                continue

            tracker.activity("researching {0}".format(job.company))
            tracker.update(job.external_id, "researched", "gathering")
            brief = build_brief(job, llm, settings, use_browser=settings.browser_enabled)
            storage.save_brief(brief)
            (out_dir / "{0}.md".format(_slug(job.company))).write_text(
                brief_to_markdown(brief), encoding="utf-8"
            )
            storage.set_stage(job.external_id, Stage.RESEARCHED)
            tracker.update(job.external_id, "researched", "{0} talking points".format(len(brief.talking_points)))
            seen.add(job.company)

    console.print("Briefs written to [cyan]data/research/[/cyan].")
    return 0


def cmd_match(args: argparse.Namespace) -> int:
    settings, profile, storage = _bootstrap(args)
    with storage, LLMClient(settings.require_llm()) as llm, _tracker(args) as tracker:
        # Researched jobs first, then anything that skipped research. --limit is
        # a total across both, not a cap per stage.
        jobs = storage.jobs_at_stage(Stage.RESEARCHED, limit=args.limit)
        if args.limit is None or len(jobs) < args.limit:
            remaining = None if args.limit is None else args.limit - len(jobs)
            jobs += storage.jobs_at_stage(Stage.SCRAPED, limit=remaining)
        if not jobs:
            console.print("Nothing to score. Run [cyan]pipeline scrape[/cyan] first.")
            return 0

        for job in jobs:
            tracker.track(job.external_id, job.company, job.title, job.source)
            tracker.activity("scoring {0} at {1}".format(job.title, job.company))
            brief = storage.get_brief(job.company)
            match = score_job(job, profile, llm, brief)
            storage.save_match(match)
            storage.set_stage(job.external_id, Stage.MATCHED)
            style = "green" if match.score >= profile.min_score_to_generate else "dim"
            tracker.update(job.external_id, "matched", "[{0}]score {1}[/{0}]".format(style, match.score))

    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    settings, profile, storage = _bootstrap(args)
    threshold = args.min_score if args.min_score is not None else profile.min_score_to_generate

    with storage, LLMClient(settings.require_llm()) as llm, _tracker(args) as tracker:
        jobs = storage.matches_above(threshold)
        if not jobs:
            console.print("No jobs scored at or above [bold]{0}[/bold].".format(threshold))
            return 0

        long_resumes = []
        for job in jobs[: args.limit] if args.limit else jobs:
            tracker.track(job.external_id, job.company, job.title, job.source)
            tracker.activity("writing application for {0}".format(job.company))
            brief = storage.get_brief(job.company)
            application = build_application(job, profile, llm, brief)
            storage.save_application(application)
            storage.set_stage(job.external_id, Stage.GENERATED)

            note = "{0} words".format(len(application.cover_letter.split()))
            pages = _resume_pages(application.resume_pdf_path)
            if pages > 1:
                long_resumes.append((job.company, pages))
                note += ", [yellow]{0}-page resume[/yellow]".format(pages)
            tracker.update(job.external_id, "generated", note)

    console.print("Applications ready. Review with [cyan]pipeline status[/cyan].")
    if long_resumes:
        console.print()
        console.print(
            "[yellow]{0} resume(s) ran past one page.[/yellow] "
            "Recruiters skim one page for a student or new grad.".format(len(long_resumes))
        )
        console.print("  Trim [cyan]config/profile.yml[/cyan]: fewer bullets per entry, or drop the oldest one.")
        console.print("  [dim]Then run: pipeline generate[/dim]")
    return 0


def _resume_pages(path: Optional[str]) -> int:
    """Page count of a generated resume, or 0 when the candidate brought their own."""
    if not path:
        return 0
    resume = Path(path)
    # Only judge resumes Pipeline produced. A candidate's own PDF is their call.
    if not resume.is_file() or "data/resumes" not in str(resume).replace("\\", "/"):
        return 0
    return page_count(resume)


def cmd_apply(args: argparse.Namespace) -> int:
    settings, profile, storage = _bootstrap(args)
    submit_mode = args.submit

    with storage, LLMClient(settings.require_llm()) as llm, _tracker(args) as tracker:
        jobs = storage.jobs_at_stage(Stage.GENERATED, limit=args.limit)
        if not jobs:
            console.print("Nothing generated yet. Run [cyan]pipeline generate[/cyan] first.")
            return 0

        try:
            engine_cm = ApplyEngine(
                profile, llm, tracker,
                submit_mode=submit_mode,
                headless=args.headless,
            )
        except ValueError as exc:
            # markup off: the message contains scrapling[fetchers], which Rich
            # would parse as a style tag and silently delete, handing the user
            # an install command that does not install the browser.
            console.print(str(exc), markup=False, highlight=False)
            return 1

        try:
            with engine_cm as engine:
                for job in jobs:
                    tracker.track(job.external_id, job.company, job.title, job.source)
                    tracker.update(job.external_id, "applying", "opening")
                    tracker.activity("applying to {0} at {1}".format(job.title, job.company))

                    application = storage.get_application(job.external_id)
                    if application is None:
                        tracker.update(job.external_id, "failed", "no application on file")
                        continue

                    brief = storage.get_brief(job.company)
                    outcome = engine.apply(job, application, brief)

                    if outcome.status == "submitted":
                        storage.set_stage(job.external_id, Stage.SUBMITTED)
                        tracker.update(job.external_id, "submitted", outcome.detail)
                    elif outcome.status == "blocked" and outcome.barrier is not None:
                        storage.add_block(job, outcome.barrier, outcome.detail, outcome.url)
                        tracker.update(job.external_id, "blocked", outcome.barrier.value.replace("_", " "))
                    elif outcome.status == "ready":
                        tracker.update(job.external_id, "generated", outcome.detail)
                    else:
                        storage.set_stage(job.external_id, Stage.FAILED)
                        tracker.update(job.external_id, "failed", outcome.detail)
        except RuntimeError as exc:
            # markup off: the message contains scrapling[fetchers], which Rich
            # would parse as a style tag and silently delete, handing the user
            # an install command that does not install the browser.
            console.print(str(exc), markup=False, highlight=False)
            return 1

    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """The whole loop, in order, stopping only at real walls."""
    for step in (cmd_scrape, cmd_research, cmd_match, cmd_generate):
        code = step(args)
        if code != 0:
            return code
    if args.no_apply:
        console.print("[dim]Skipping apply (--no-apply).[/dim]")
        return cmd_status(args)
    return cmd_apply(args)


def cmd_status(args: argparse.Namespace) -> int:
    settings, profile, storage = _bootstrap(args, needs_llm=False)
    with storage:
        counts = storage.stage_counts()
        table = Table(title="Pipeline", box=None, pad_edge=False)
        table.add_column("STAGE", style="bold")
        table.add_column("JOBS", justify="right")
        for stage in Stage:
            if counts.get(stage.value):
                table.add_row(stage.value, str(counts[stage.value]))
        console.print(table if counts else "[dim]Nothing tracked yet.[/dim]")

        blocked = storage.open_blocks()
        if blocked:
            console.print()
            console.print("[yellow]{0} blocked.[/yellow] See [cyan]pipeline unblock[/cyan].".format(len(blocked)))
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    settings, profile, storage = _bootstrap(args, needs_llm=False)
    with storage:
        items = storage.open_blocks()
        print_blocked(console, items)
        if items and args.resolve:
            index = args.resolve - 1
            if 0 <= index < len(items):
                item = items[index]
                storage.resolve_block(item.db_id or 0)
                storage.set_stage(item.job_external_id, Stage.SUBMITTED)
                console.print("[green]Marked done:[/green] {0} · {1}".format(item.company, item.title))
            else:
                console.print("[red]No blocked item #{0}.[/red]".format(args.resolve))
                return 1
    return 0


def _positive(value: str) -> int:
    """argparse type for --limit. Zero means "do nothing", which is never intended."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number, got {0!r}".format(value))
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more (leave it off for no limit)")
    return number


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:60] or "company"


# -- parser -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline",
        description="Find jobs, research them, write the application, and apply.",
    )
    parser.add_argument("--profile", metavar="PATH", help="Path to profile.yml.")
    parser.add_argument("--debug", action="store_true", help="Verbose logging.")
    parser.add_argument("--quiet", action="store_true", help="Plain output, no live tracker.")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create profile.yml and .env.").set_defaults(func=cmd_init)

    run = sub.add_parser("run", help="Everything: scrape, research, match, generate, apply.")
    run.add_argument("--limit", type=_positive, default=None, help="Cap jobs per stage.")
    run.add_argument("--min-score", type=int, default=None, help="Override the write-up threshold.")
    run.add_argument("--submit", choices=("confirm", "auto", "never"), default="confirm")
    run.add_argument("--headless", action="store_true", help="Hide the browser.")
    run.add_argument("--no-apply", action="store_true", help="Stop before the browser stage.")
    run.set_defaults(func=cmd_run)

    scrape = sub.add_parser("scrape", help="Pull postings from your configured boards.")
    scrape.set_defaults(func=cmd_scrape)

    research = sub.add_parser("research", help="Build company intel briefs.")
    research.add_argument("--limit", type=_positive, default=None)
    research.set_defaults(func=cmd_research)

    match = sub.add_parser("match", help="Score postings against your profile.")
    match.add_argument("--limit", type=_positive, default=None)
    match.set_defaults(func=cmd_match)

    generate = sub.add_parser("generate", help="Write cover letters, answers, and resumes.")
    generate.add_argument("--limit", type=_positive, default=None)
    generate.add_argument("--min-score", type=int, default=None)
    generate.set_defaults(func=cmd_generate)

    apply_cmd = sub.add_parser("apply", help="Open a browser and fill the forms.")
    apply_cmd.add_argument("--limit", type=_positive, default=None)
    apply_cmd.add_argument("--submit", choices=("confirm", "auto", "never"), default="confirm")
    apply_cmd.add_argument("--headless", action="store_true")
    apply_cmd.set_defaults(func=cmd_apply)

    sub.add_parser("status", help="Where everything stands.").set_defaults(func=cmd_status)

    unblock = sub.add_parser("unblock", help="Show what needs you, and clear items.")
    unblock.add_argument("--resolve", type=int, metavar="N", help="Mark blocked item N as done.")
    unblock.set_defaults(func=cmd_unblock)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Defaults for flags a given subcommand does not define, so shared code can
    # read them without checking hasattr everywhere.
    for name, default in (
        ("limit", None), ("min_score", None), ("submit", "confirm"),
        ("headless", False), ("no_apply", False),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped. Progress is saved, run the same command to resume.[/dim]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
