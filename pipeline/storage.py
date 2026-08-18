"""SQLite persistence. One file, no server, safe to delete and rebuild."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from pipeline.models import (
    Application,
    Barrier,
    BlockedItem,
    Brief,
    Job,
    Match,
    Stage,
)

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    external_id  TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    apply_url    TEXT,
    location     TEXT,
    department   TEXT,
    description  TEXT,
    remote       INTEGER NOT NULL DEFAULT 0,
    salary_range TEXT,
    posted_at    TEXT,
    raw          TEXT,
    stage        TEXT NOT NULL DEFAULT 'scraped',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS briefs (
    company         TEXT PRIMARY KEY,
    summary         TEXT NOT NULL,
    what_they_do    TEXT,
    recent_signals  TEXT,
    tech_stack      TEXT,
    talking_points  TEXT,
    sources         TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_external_id TEXT NOT NULL UNIQUE REFERENCES jobs(external_id) ON DELETE CASCADE,
    score           INTEGER NOT NULL,
    reasoning       TEXT,
    strengths       TEXT,
    concerns        TEXT,
    model_used      TEXT,
    prompt_version  TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_external_id TEXT NOT NULL UNIQUE REFERENCES jobs(external_id) ON DELETE CASCADE,
    cover_letter    TEXT,
    resume_pdf_path TEXT,
    answers         TEXT,
    model_used      TEXT,
    prompt_version  TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocked (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_external_id TEXT NOT NULL REFERENCES jobs(external_id) ON DELETE CASCADE,
    barrier         TEXT NOT NULL,
    detail          TEXT,
    url             TEXT,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_stage ON jobs(stage);
CREATE INDEX IF NOT EXISTS idx_blocked_open ON blocked(resolved_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False)


def _loads(value: Optional[str]) -> Any:
    if not value:
        return []
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        logger.warning("Ignoring malformed JSON column value.")
        return []


class Storage:
    """Every read and write the pipeline makes goes through here."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- jobs -----------------------------------------------------------------

    def upsert_job(self, job: Job) -> bool:
        """Insert a job. Returns True when it is new to the database.

        Re-scraping never clobbers stage: a job already generated or submitted
        stays where it is even if the source lists it again.
        """
        now = _now()
        cur = self.conn.execute(
            "SELECT external_id FROM jobs WHERE external_id = ?", (job.external_id,)
        )
        exists = cur.fetchone() is not None

        if exists:
            self.conn.execute(
                """UPDATE jobs SET description = COALESCE(?, description),
                       salary_range = COALESCE(?, salary_range),
                       apply_url = COALESCE(NULLIF(?, ''), apply_url),
                       updated_at = ?
                   WHERE external_id = ?""",
                (job.description, job.salary_range, job.apply_url, now, job.external_id),
            )
            self.conn.commit()
            return False

        self.conn.execute(
            """INSERT INTO jobs (external_id, source, company, title, url, apply_url,
                                 location, department, description, remote, salary_range,
                                 posted_at, raw, stage, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job.external_id, job.source, job.company, job.title, job.url,
                job.apply_url or job.url, job.location, job.department, job.description,
                1 if job.remote else 0, job.salary_range, job.posted_at,
                json.dumps(job.raw or {}, ensure_ascii=False),
                Stage.SCRAPED.value, now, now,
            ),
        )
        self.conn.commit()
        return True

    def set_stage(self, external_id: str, stage: Stage) -> None:
        self.conn.execute(
            "UPDATE jobs SET stage = ?, updated_at = ? WHERE external_id = ?",
            (stage.value, _now(), external_id),
        )
        self.conn.commit()

    def jobs_at_stage(self, stage: Stage, limit: Optional[int] = None) -> List[Job]:
        sql = "SELECT * FROM jobs WHERE stage = ? ORDER BY created_at"
        params: List[Any] = [stage.value]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._row_to_job(r) for r in self.conn.execute(sql, params)]

    def get_job(self, external_id: str) -> Optional[Job]:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE external_id = ?", (external_id,)
        ).fetchone()
        return self._row_to_job(row) if row else None

    def stage_counts(self) -> Dict[str, int]:
        rows = self.conn.execute("SELECT stage, COUNT(*) c FROM jobs GROUP BY stage")
        return {r["stage"]: r["c"] for r in rows}

    # -- briefs ---------------------------------------------------------------

    def save_brief(self, brief: Brief) -> None:
        self.conn.execute(
            """INSERT INTO briefs (company, summary, what_they_do, recent_signals,
                                   tech_stack, talking_points, sources, created_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(company) DO UPDATE SET
                   summary=excluded.summary,
                   what_they_do=excluded.what_they_do,
                   recent_signals=excluded.recent_signals,
                   tech_stack=excluded.tech_stack,
                   talking_points=excluded.talking_points,
                   sources=excluded.sources,
                   created_at=excluded.created_at""",
            (
                brief.company, brief.summary, brief.what_they_do,
                _dumps(brief.recent_signals), _dumps(brief.tech_stack),
                _dumps(brief.talking_points), _dumps(brief.sources), _now(),
            ),
        )
        self.conn.commit()

    def get_brief(self, company: str) -> Optional[Brief]:
        row = self.conn.execute(
            "SELECT * FROM briefs WHERE company = ?", (company,)
        ).fetchone()
        if not row:
            return None
        return Brief(
            company=row["company"],
            summary=row["summary"],
            what_they_do=row["what_they_do"] or "",
            recent_signals=_loads(row["recent_signals"]),
            tech_stack=_loads(row["tech_stack"]),
            talking_points=_loads(row["talking_points"]),
            sources=_loads(row["sources"]),
        )

    # -- matches --------------------------------------------------------------

    def save_match(self, match: Match) -> None:
        self.conn.execute(
            """INSERT INTO matches (job_external_id, score, reasoning, strengths,
                                    concerns, model_used, prompt_version, created_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(job_external_id) DO UPDATE SET
                   score=excluded.score, reasoning=excluded.reasoning,
                   strengths=excluded.strengths, concerns=excluded.concerns,
                   model_used=excluded.model_used,
                   prompt_version=excluded.prompt_version,
                   created_at=excluded.created_at""",
            (
                match.job_external_id, match.score, match.reasoning,
                _dumps(match.strengths), _dumps(match.concerns),
                match.model_used, match.prompt_version, _now(),
            ),
        )
        self.conn.commit()

    def get_match(self, external_id: str) -> Optional[Match]:
        row = self.conn.execute(
            "SELECT * FROM matches WHERE job_external_id = ?", (external_id,)
        ).fetchone()
        if not row:
            return None
        return Match(
            job_external_id=row["job_external_id"],
            score=row["score"],
            reasoning=row["reasoning"] or "",
            strengths=_loads(row["strengths"]),
            concerns=_loads(row["concerns"]),
            model_used=row["model_used"] or "",
            prompt_version=row["prompt_version"] or "",
            db_id=row["id"],
        )

    def matches_above(self, score: int) -> List[Job]:
        rows = self.conn.execute(
            """SELECT j.* FROM jobs j JOIN matches m ON m.job_external_id = j.external_id
               WHERE m.score >= ? AND j.stage = ? ORDER BY m.score DESC""",
            (score, Stage.MATCHED.value),
        )
        return [self._row_to_job(r) for r in rows]

    # -- applications ---------------------------------------------------------

    def save_application(self, app: Application) -> None:
        self.conn.execute(
            """INSERT INTO applications (job_external_id, cover_letter, resume_pdf_path,
                                         answers, model_used, prompt_version, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(job_external_id) DO UPDATE SET
                   cover_letter=excluded.cover_letter,
                   resume_pdf_path=excluded.resume_pdf_path,
                   answers=excluded.answers,
                   model_used=excluded.model_used,
                   prompt_version=excluded.prompt_version,
                   created_at=excluded.created_at""",
            (
                app.job_external_id, app.cover_letter, app.resume_pdf_path,
                json.dumps(app.answers or {}, ensure_ascii=False),
                app.model_used, app.prompt_version, _now(),
            ),
        )
        self.conn.commit()

    def get_application(self, external_id: str) -> Optional[Application]:
        row = self.conn.execute(
            "SELECT * FROM applications WHERE job_external_id = ?", (external_id,)
        ).fetchone()
        if not row:
            return None
        answers = _loads(row["answers"])
        return Application(
            job_external_id=row["job_external_id"],
            cover_letter=row["cover_letter"] or "",
            resume_pdf_path=row["resume_pdf_path"],
            answers=answers if isinstance(answers, dict) else {},
            model_used=row["model_used"] or "",
            prompt_version=row["prompt_version"] or "",
            db_id=row["id"],
        )

    # -- barriers -------------------------------------------------------------

    def add_block(self, job: Job, barrier: Barrier, detail: str, url: str) -> None:
        """Record a wall. Re-blocking the same job on the same barrier is a no-op."""
        existing = self.conn.execute(
            """SELECT id FROM blocked
               WHERE job_external_id = ? AND barrier = ? AND resolved_at IS NULL""",
            (job.external_id, barrier.value),
        ).fetchone()
        if existing:
            return
        self.conn.execute(
            """INSERT INTO blocked (job_external_id, barrier, detail, url, created_at)
               VALUES (?,?,?,?,?)""",
            (job.external_id, barrier.value, detail, url, _now()),
        )
        self.set_stage(job.external_id, Stage.BLOCKED)
        self.conn.commit()

    def open_blocks(self) -> List[BlockedItem]:
        rows = self.conn.execute(
            """SELECT b.*, j.company, j.title FROM blocked b
               JOIN jobs j ON j.external_id = b.job_external_id
               WHERE b.resolved_at IS NULL ORDER BY b.created_at"""
        )
        items: List[BlockedItem] = []
        for r in rows:
            items.append(
                BlockedItem(
                    job_external_id=r["job_external_id"],
                    company=r["company"],
                    title=r["title"],
                    barrier=Barrier(r["barrier"]),
                    detail=r["detail"] or "",
                    url=r["url"] or "",
                    created_at=datetime.fromisoformat(r["created_at"]),
                    db_id=r["id"],
                )
            )
        return items

    def resolve_block(self, block_id: int) -> None:
        self.conn.execute(
            "UPDATE blocked SET resolved_at = ? WHERE id = ?", (_now(), block_id)
        )
        self.conn.commit()

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            external_id=row["external_id"],
            source=row["source"],
            company=row["company"],
            title=row["title"],
            url=row["url"],
            apply_url=row["apply_url"] or row["url"],
            location=row["location"],
            department=row["department"],
            description=row["description"],
            remote=bool(row["remote"]),
            salary_range=row["salary_range"],
            posted_at=row["posted_at"],
            raw=_loads(row["raw"]) if row["raw"] else {},
        )
