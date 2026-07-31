"""SQLAlchemy models for the Brand Positioning report (slug: brand_positioning).

Reads the SAME raw answers as the sentiment report (`bas_response`) — no second
fetch from Brand Radar. Adds two layers on top:

  BpClaim    one "X is better for Y" claim extracted from one answer, about one
             brand. Keyed (response_hash, brand, claim_idx) so re-runs are free.
  BpCategory a semantic cluster of claims for one (report, brand): the label the
             LLM gave the group, plus its members. Rebuilt per clustering run —
             claims are the durable layer, categories are the interpretation.

Two-stage on purpose: extraction is per-answer and cacheable, clustering needs to
see all claims at once. Re-clustering (e.g. after a new month) never re-extracts.
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import (String, Text, DateTime, Integer, Float, Boolean, Index,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class BpClaim(Base):
    """One positioning claim: 'this brand is better for <use case>'."""
    __tablename__ = "bp_claim"
    __table_args__ = (
        UniqueConstraint("response_hash", "brand", "claim_idx", name="uq_bp_claim"),
        Index("ix_bp_claim_lookup", "report_id", "brand"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    response_hash: Mapped[str] = mapped_column(String(40), index=True)
    report_id: Mapped[str] = mapped_column(String(64), index=True)
    brand: Mapped[str] = mapped_column(String(120))
    claim_idx: Mapped[int] = mapped_column(Integer, default=0)
    # what the AI says this brand is better for, normalised to a short phrase
    strength: Mapped[str] = mapped_column(Text, default="")
    # verbatim span backing the claim
    evidence: Mapped[str] = mapped_column(Text, default="")
    # 'strength' (better at) | 'weakness' (worse at) — we store both so a category
    # can be shown as "what it's better for" vs "what it's NOT chosen for"
    polarity: Mapped[str] = mapped_column(String(12), default="strength", index=True)
    # comparative context: which brands it was being compared against, if named
    versus: Mapped[list] = mapped_column(JSONB, default=list)
    platform: Mapped[str] = mapped_column(String(40), default="", index=True)
    snapshot_month: Mapped[str] = mapped_column(String(7), default="", index=True)
    country: Mapped[str] = mapped_column(String(8), default="")
    search_volume: Mapped[int] = mapped_column(Integer, default=0)
    # set once clustering runs; null = not yet grouped
    category_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    llm_model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BpCategory(Base):
    """A semantic cluster of claims for one (report, brand, polarity)."""
    __tablename__ = "bp_category"
    __table_args__ = (
        UniqueConstraint("report_id", "brand", "polarity", "category_key",
                         name="uq_bp_category"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[str] = mapped_column(String(64), index=True)
    brand: Mapped[str] = mapped_column(String(120), index=True)
    polarity: Mapped[str] = mapped_column(String(12), default="strength")
    category_key: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    n_claims: Mapped[int] = mapped_column(Integer, default=0)
    n_responses: Mapped[int] = mapped_column(Integer, default=0)
    share: Mapped[float] = mapped_column(Float, default=0.0)   # % of that brand's claims
    platforms: Mapped[dict] = mapped_column(JSONB, default=dict)
    months: Mapped[dict] = mapped_column(JSONB, default=dict)
    examples: Mapped[list] = mapped_column(JSONB, default=list)  # [{evidence, response_hash}]
    is_own: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BpJob(Base):
    """Background job (extract | cluster). Survives the Console's auto-restart."""
    __tablename__ = "bp_job"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="extract")
    report_id: Mapped[str] = mapped_column(String(64), index=True)
    brands: Mapped[list] = mapped_column(JSONB, default=list)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="running")
    stage: Mapped[str] = mapped_column(Text, default="")
    done: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    log: Mapped[list] = mapped_column(JSONB, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    cancel: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
