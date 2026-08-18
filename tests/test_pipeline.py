"""Checks on the logic that decides things. Run with: pytest"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pipeline.barriers import AUTOMATION_TIER, detect, detect_ats
from pipeline.generate import _structured_answers
from pipeline.matcher import _clamp_score
from pipeline.models import Barrier, Job, Match, Profile, Stage
from pipeline.resume import page_count, render_resume
from pipeline.settings import load_profile
from pipeline.sources import make_id, strip_html, title_matches
from pipeline.storage import Storage


# -- source filtering ---------------------------------------------------------

def test_title_matches_accepts_close_variants():
    assert title_matches("Software Engineering Intern", ["Software Engineer Intern"])
    assert title_matches("Security Analyst Intern", ["Security Analyst Intern"])


def test_title_matches_rejects_single_token_overlap():
    # "Security" alone must not pull in every security-adjacent senior role.
    assert not title_matches("Applied AI Architect, National Security", ["Security Analyst Intern"])
    assert not title_matches("Account Executive, Public Sector", ["Software Engineer Intern"])


def test_title_matches_with_no_targets_lets_everything_through():
    assert title_matches("Anything At All", [])


def test_make_id_is_stable_and_distinct():
    a = make_id("greenhouse", "Acme", "SWE Intern", "Austin")
    assert a == make_id("greenhouse", "acme", "swe intern", "austin")
    assert a != make_id("lever", "Acme", "SWE Intern", "Austin")


def test_strip_html_unescapes_and_flattens():
    out = strip_html("&lt;p&gt;Hello&lt;br&gt;world &amp;amp; friends&lt;/p&gt;")
    assert "Hello" in out and "world" in out
    assert "<" not in out and "&lt;" not in out


# -- scoring ------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [(85, 85), ("85", 85), (85.7, 85), ("85/100", 85), ("score: 92", 92),
     (None, 0), (True, 0), (-5, 0), (300, 100), ("", 0), ("none", 0)],
)
def test_clamp_score_survives_whatever_the_model_returns(raw, expected):
    assert _clamp_score(raw) == expected


# -- barriers -----------------------------------------------------------------

def test_detect_finds_captcha_first():
    text = "please create an account and complete the g-recaptcha challenge"
    found = detect(text, "https://jobs.example.com/apply")
    assert found is not None and found[0] == Barrier.CAPTCHA


def test_detect_finds_email_verification():
    found = detect("We sent a code to your inbox", "https://jobs.example.com")
    assert found is not None and found[0] == Barrier.EMAIL_VERIFICATION


def test_detect_flags_workday_without_a_form():
    found = detect("Welcome back", "https://acme.wd1.myworkdayjobs.com/careers")
    assert found is not None and found[0] == Barrier.ACCOUNT_CREATION


def test_detect_lets_workday_through_once_the_form_is_visible():
    form = "first name last name phone upload resume cover letter"
    assert detect(form, "https://acme.wd1.myworkdayjobs.com/careers") is None


def test_detect_reports_unfilled_required_fields():
    found = detect("a normal form page", "https://boards.greenhouse.io/x", filled_all_required=False)
    assert found is not None and found[0] == Barrier.UNKNOWN_FIELD


def test_detect_returns_none_on_a_clean_page():
    assert detect("Apply for this job", "https://boards.greenhouse.io/acme/jobs/1") is None


@pytest.mark.parametrize(
    "url,expected",
    [("https://boards.greenhouse.io/a/jobs/1", "greenhouse"),
     ("https://jobs.lever.co/spotify/abc", "lever"),
     ("https://jobs.ashbyhq.com/ramp/x", "ashby"),
     ("https://acme.wd5.myworkdayjobs.com/en-US/careers", "workday"),
     ("https://example.com/careers/1", "unknown")],
)
def test_detect_ats(url, expected):
    assert detect_ats(url) == expected
    assert detect_ats(url) in AUTOMATION_TIER


# -- legal-form answers -------------------------------------------------------

def _profile(**kwargs) -> Profile:
    base = dict(raw={}, name="Jordan Rivera", email="j@example.com")
    base.update(kwargs)
    return Profile(**base)


def test_work_authorization_is_left_blank_when_unknown():
    # Guessing on a legal question is worse than escalating it.
    answers = _structured_answers(_profile())
    assert "authorized_to_work" not in answers
    assert "requires_sponsorship" not in answers


def test_work_authorization_citizen():
    answers = _structured_answers(_profile(work_authorization="US citizen", requires_sponsorship=False))
    assert answers["authorized_to_work"] == "Yes"
    assert answers["requires_sponsorship"] == "No"


def test_work_authorization_needs_sponsorship():
    answers = _structured_answers(_profile(work_authorization="F-1 OPT", requires_sponsorship=True))
    assert answers["authorized_to_work"] == "No"
    assert answers["requires_sponsorship"] == "Yes"


# -- storage ------------------------------------------------------------------

def _job(external_id="abc123") -> Job:
    return Job(
        external_id=external_id, source="greenhouse", company="Acme",
        title="Security Intern", url="https://example.com/j/1",
        apply_url="https://example.com/j/1/apply", location="Austin, TX",
    )


def test_storage_roundtrip_and_dedupe(tmp_path: Path):
    with Storage(tmp_path / "t.db") as store:
        job = _job()
        assert store.upsert_job(job) is True
        assert store.upsert_job(job) is False, "same posting must not duplicate"
        assert store.get_job(job.external_id).company == "Acme"
        assert store.stage_counts()[Stage.SCRAPED.value] == 1


def test_rescraping_does_not_reset_stage(tmp_path: Path):
    with Storage(tmp_path / "t.db") as store:
        job = _job()
        store.upsert_job(job)
        store.set_stage(job.external_id, Stage.SUBMITTED)
        store.upsert_job(job)
        assert store.stage_counts()[Stage.SUBMITTED.value] == 1


def test_blocking_is_idempotent_and_resolvable(tmp_path: Path):
    with Storage(tmp_path / "t.db") as store:
        job = _job()
        store.upsert_job(job)
        store.add_block(job, Barrier.EMAIL_VERIFICATION, "code sent", job.apply_url)
        store.add_block(job, Barrier.EMAIL_VERIFICATION, "code sent", job.apply_url)
        open_blocks = store.open_blocks()
        assert len(open_blocks) == 1
        assert open_blocks[0].help_text
        store.resolve_block(open_blocks[0].db_id)
        assert store.open_blocks() == []


def test_matches_above_threshold(tmp_path: Path):
    with Storage(tmp_path / "t.db") as store:
        good, bad = _job("good"), _job("bad")
        store.upsert_job(good)
        store.upsert_job(bad)
        store.save_match(Match(job_external_id="good", score=88, reasoning="fit"))
        store.save_match(Match(job_external_id="bad", score=30, reasoning="no"))
        store.set_stage("good", Stage.MATCHED)
        store.set_stage("bad", Stage.MATCHED)
        ids = [j.external_id for j in store.matches_above(70)]
        assert ids == ["good"]


# -- resume -------------------------------------------------------------------

def test_example_profile_renders_a_one_page_resume(tmp_path: Path):
    profile = load_profile(Path("config/profile.example.yml"))
    out = render_resume(profile, tmp_path / "r.pdf")
    assert out.is_file() and out.stat().st_size > 1000
    assert page_count(out) == 1, "resumes must stay on one page"
