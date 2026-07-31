"""SQLAlchemy models for the Brand Answer Sentiment report (slug: brand_answer_sentiment).

Design notes
------------
Everything the page renders comes out of Postgres, so switching any filter
(platform / country / dataset / window / subject brand) is a SQL query, not an
API call. Only two operations cost money:

  * FETCH     — pull Brand Radar custom/public prompt responses for a
                (report, platform, dataset) pair. Deduped on ``response_hash``.
  * CLASSIFY  — LLM verdict per (response, subject brand). Deduped on
                (response_hash, subject) so re-runs and monthly rollovers never
                re-pay for a response already judged.

``BasResponse.brands_hit`` is a JSONB array of every report entity (own brand +
competitors + niche) whose alias matched the answer text with a whole-word
regex. It is computed at fetch time for ALL entities, which makes switching the
subject brand free for the mentioned/not-mentioned math — only the LLM verdicts
have to be filled in for a newly-picked subject.
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import (String, Text, DateTime, Integer, Boolean, Index,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class BasReport(Base):
    """Cached Brand Radar report config + a per-platform/dataset row-count probe."""
    __tablename__ = "bas_report"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(Text, default="")
    brands: Mapped[list] = mapped_column(JSONB, default=list)       # [{name,keywords,urls,mode}]
    competitors: Mapped[list] = mapped_column(JSONB, default=list)
    niche: Mapped[list] = mapped_column(JSONB, default=list)
    countries: Mapped[list] = mapped_column(JSONB, default=list)    # report's own country filter
    n_custom_prompts: Mapped[int] = mapped_column(Integer, default=0)
    custom_prompt_countries: Mapped[list] = mapped_column(JSONB, default=list)
    probe: Mapped[dict] = mapped_column(JSONB, default=dict)        # {"custom":{"chatgpt":9848,...},"public":{...}}
    probed_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refreshed_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BasFetch(Base):
    """One fetched slice: (report, platform, dataset). Drives the 'Captured' badge
    and the coverage / 'what's missing' logic in the settings panel."""
    __tablename__ = "bas_fetch"
    __table_args__ = (
        UniqueConstraint("report_id", "platform", "dataset", name="uq_bas_fetch_slice"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[str] = mapped_column(String(64), index=True)
    platform: Mapped[str] = mapped_column(String(40))
    dataset: Mapped[str] = mapped_column(String(16))                # custom | public
    window_months: Mapped[int] = mapped_column(Integer, default=3)  # 0 = all history
    cutoff: Mapped[str] = mapped_column(String(10), default="")     # YYYY-MM-DD kept from
    n_rows: Mapped[int] = mapped_column(Integer, default=0)         # rows stored for this slice
    n_seen: Mapped[int] = mapped_column(Integer, default=0)         # rows returned by the API
    months: Mapped[list] = mapped_column(JSONB, default=list)       # ['2026-05', ...]
    countries: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16), default="ok")   # ok | error
    error: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BasResponse(Base):
    """One AI answer to one prompt on one dated snapshot. Deduped on response_hash."""
    __tablename__ = "bas_response"
    __table_args__ = (
        Index("ix_bas_resp_slice", "report_id", "dataset", "platform", "snapshot_date"),
        Index("ix_bas_resp_prompt", "report_id", "prompt_hash"),
        Index("ix_bas_resp_brands", "brands_hit", postgresql_using="gin"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    response_hash: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    report_id: Mapped[str] = mapped_column(String(64), index=True)
    dataset: Mapped[str] = mapped_column(String(16), default="custom")
    platform: Mapped[str] = mapped_column(String(40), index=True)
    prompt_hash: Mapped[str] = mapped_column(String(40), index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    response: Mapped[str] = mapped_column(Text, default="")
    search_volume: Mapped[int] = mapped_column(Integer, default=0)
    country: Mapped[str] = mapped_column(String(8), default="", index=True)
    snapshot_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    snapshot_month: Mapped[str] = mapped_column(String(7), default="", index=True)
    sitelinks: Mapped[list] = mapped_column(JSONB, default=list)
    brands_hit: Mapped[list] = mapped_column(JSONB, default=list)
    search_queries: Mapped[list] = mapped_column(JSONB, default=list)
    fetched_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BasVerdict(Base):
    """LLM sentiment verdict for one response, about one subject brand."""
    __tablename__ = "bas_verdict"
    __table_args__ = (
        UniqueConstraint("response_hash", "subject", name="uq_bas_verdict_resp_subject"),
        Index("ix_bas_verdict_lookup", "report_id", "subject", "verdict"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    response_hash: Mapped[str] = mapped_column(String(40), index=True)
    report_id: Mapped[str] = mapped_column(String(64), index=True)
    subject: Mapped[str] = mapped_column(String(120))
    verdict: Mapped[str] = mapped_column(String(12), default="neutral")  # absent|positive|neutral|mixed|negative
    themes: Mapped[list] = mapped_column(JSONB, default=list)
    evidence: Mapped[str] = mapped_column(Text, default="")
    one_liner: Mapped[str] = mapped_column(Text, default="")
    llm_model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BasJob(Base):
    """Background job record — survives the app's auto-reload, so polling never 404s."""
    __tablename__ = "bas_job"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="fetch")   # fetch | classify | full
    report_id: Mapped[str] = mapped_column(String(64), index=True)
    subject: Mapped[str] = mapped_column(String(120), default="")
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done|error
    stage: Mapped[str] = mapped_column(Text, default="")
    done: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    log: Mapped[list] = mapped_column(JSONB, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    cancel: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
