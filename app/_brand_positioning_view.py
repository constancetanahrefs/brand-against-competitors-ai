"""What each brand is better for — semantic positioning categories from AI answers.

Companion to the sentiment report. Reads the SAME stored answers (`bas_response`),
so there is no second Brand Radar fetch. Two LLM stages, both explicit buttons:

  Extract  — pull "X is better for Y" claims out of the answers that mention X
  Group    — cluster those claims into named semantic categories

Each brand in the report gets its own sub-tab, plus a side-by-side compare tab.
"""

# SUB-REPORT of brand_answer_sentiment. Deliberately no NAME/OWNER and a leading
# underscore in the filename so app.discover() does NOT list it as its own report;
# the parent imports `blueprint` from here and nests it under /positioning/.
SUBTITLE = "What each brand is better for"

import threading
import uuid

from flask import Blueprint, render_template, jsonify, request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from src.db import session_scope
from src.schemas import validate_json, validate_query

from ._brand_answer_sentiment_models import BasResponse
from ._brand_answer_sentiment_engine import (
    PLATFORMS, PLATFORM_LABELS, load_report, entities_of, refresh_reports,
)
from ._brand_positioning_models import BpClaim, BpCategory, BpJob
from . import _brand_positioning_engine as E

# SECOND TAB of each per-project brand report. Factory, not a report: no
# NAME/OWNER and an underscore-prefixed filename, so app.discover() skips it.
# _brand_report_core.make_report() calls make_positioning() with the same pinned
# report_id, so this tab can only ever see its own project's rows.
_PINNED: dict[str, str] = {}
# report_ids whose orphan jobs have already been adopted this process
_ADOPTED_ONCE: set[str] = set()


def _rid() -> str:
    """The report_id this blueprint is pinned to. Never from user input."""
    from flask import request as _rq
    bp = (_rq.blueprint or "").split(".")[-1]
    return _PINNED.get(bp, "")


def _api_map(slug: str) -> dict:
    """Absolute URLs for the JS layer. Built server-side (not url_for) because
    this blueprint is nested, so endpoint names are prefixed by the parent."""
    b = f"/reports/{slug}/positioning/"   # slug = PARENT report slug
    return {"ctx": b+"api/context", "brand": b+"api/brand", "category": b+"api/category",
            "compare": b+"api/compare", "extract": b+"api/extract", "group": b+"api/group",
            "job": b+"api/job", "cancel": b+"api/job/cancel", "active": b+"api/jobs/active"}


# ------------------------------------------------------------------- schemas
class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractRequest(_Req):
    report_id: str = ""   # ignored: blueprint is pinned to one report
    brands: list[str] = Field(default_factory=list, max_length=30)
    platforms: list[str] = Field(default_factory=list, max_length=7)
    datasets: list[str] = Field(default_factory=lambda: ["custom"], max_length=2)
    also_group: bool = Field(default=True)


class GroupRequest(_Req):
    report_id: str = ""   # ignored: blueprint is pinned to one report
    brands: list[str] = Field(default_factory=list, max_length=30)
    threshold: float = Field(default=E.SIM_THRESHOLD, ge=0.3, le=0.95)


class BrandQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str = ""   # ignored: blueprint is pinned to one report
    brand: str = Field(min_length=1, max_length=120)
    polarity: str = Field(default="strength", pattern=r"^(strength|weakness)$")
    platforms: str = Field(default="")
    months: str = Field(default="")
    sort: str = Field(default="claims", pattern=r"^(claims|responses|label)$")
    # Hide long-tail categories by default: a 2-claim group out of 3,800 is noise,
    # not a positioning theme. 0 = show everything.
    min_share: float = Field(default=1.0, ge=0.0, le=50.0)


class CategoryQuery(BrandQuery):
    category_key: str = Field(min_length=4, max_length=64)


class CompareQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str = ""   # ignored: blueprint is pinned to one report
    polarity: str = Field(default="strength", pattern=r"^(strength|weakness)$")


class JobQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=8, max_length=40)


class JobCancel(_Req):
    job_id: str = Field(min_length=8, max_length=40)


# -------------------------------------------------------------------- helpers
def _csv(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _keywords(report_id: str, brand: str) -> list[str]:
    rep = load_report(report_id) or {}
    for e in entities_of(rep):
        if e["name"] == brand:
            return e.get("keywords") or [brand]
    return [brand]


def _own_brand(report_id: str) -> str:
    rep = load_report(report_id) or {}
    ents = entities_of(rep)
    own = [e for e in ents if e["kind"] == "brand"]
    return (own[0]["name"] if own else (ents[0]["name"] if ents else ""))


def _new_job(kind: str, report_id: str, brands: list[str], params: dict) -> str:
    jid = uuid.uuid4().hex[:16]
    with session_scope() as s:
        s.add(BpJob(job_id=jid, kind=kind, report_id=report_id, brands=brands,
                    params=params, status="running", stage="starting"))
    return jid


# --------------------------------------------------------------------- factory
def make_positioning(report_id: str, parent_slug: str, title: str = ""):
    """Positioning tab pinned to one Brand Radar report, nested under its parent."""
    slug = f"{parent_slug}_positioning"
    blueprint = Blueprint(slug, __name__,
                          template_folder="../templates/brand_positioning")
    _PINNED[slug] = report_id

    # ---------------------------------------------------------------------- routes
    @blueprint.route("/")
    def index():
            return render_template("brand_positioning/index.html",
                                   api=_api_map(parent_slug),
                                   report=E.load_report(report_id) or {},
                                   report_id=report_id,
                                   report_title=title,
                                   platforms=PLATFORMS,
                                   platform_labels=PLATFORM_LABELS,
                                   sentiment_url=f"/reports/{parent_slug}/",
                                   positioning_url=f"/reports/{parent_slug}/positioning/")

    @blueprint.route("/api/context")
    def api_context():
        cov = E.coverage(report_id)
        if not cov:
            return jsonify({"error": "unknown report"}), 404
        return jsonify({**cov, "own_brand": _own_brand(report_id),
                        "filters": E.available_filters(report_id)})

    @blueprint.route("/api/brand")
    def api_brand():
        q = validate_query(BrandQuery)
        return jsonify(E.brand_view(_rid(), q.brand, q.polarity,
                                    _csv(q.platforms), _csv(q.months), q.sort,
                                    min_share=q.min_share))

    @blueprint.route("/api/category")
    def api_category():
        q = validate_query(CategoryQuery)
        return jsonify(E.category_claims(_rid(), q.brand, q.polarity,
                                         q.category_key, _csv(q.platforms), _csv(q.months)))

    @blueprint.route("/api/compare")
    def api_compare():
        q = validate_query(CompareQuery)
        return jsonify(E.compare_view(_rid(), _own_brand(_rid()), q.polarity))

    def _run_extract(jid, report_id, brands, platforms, datasets, also_group):
        try:
            for i, brand in enumerate(brands, 1):
                if E.job_cancelled(jid):
                    E.job_update(jid, status="error", error="cancelled")
                    return
                E.job_update(jid, stage=f"[{i}/{len(brands)}] reading answers about {brand}")
                r = E.extract_claims(report_id, brand, _keywords(report_id, brand),
                                     platforms, datasets, job_id=jid)
                E.job_update(jid, log=f"{brand}: {r['answers']:,} answers read, {r['claims']:,} claims")
            if also_group:
                _group_brands(jid, report_id, brands)
            E.job_update(jid, status="done", stage="complete")
        except Exception as e:  # noqa: BLE001
            E.job_update(jid, status="error", error=str(e)[:500], log=f"ERROR {e}")

    def _group_brands(jid, report_id, brands, threshold=E.SIM_THRESHOLD):
        own = _own_brand(report_id)
        for i, brand in enumerate(brands, 1):
            if E.job_cancelled(jid):
                E.job_update(jid, status="error", error="cancelled")
                return
            E.job_update(jid, stage=f"[{i}/{len(brands)}] grouping {brand}")
            for pol in ("strength", "weakness"):
                r = E.cluster_brand(report_id, brand, brand == own, pol,
                                    job_id=jid, threshold=threshold)
                if r["claims"]:
                    E.job_update(jid, log=f"{brand}/{pol}: {r['categories']} categories "
                                          f"from {r['claims']:,} claims")

    @blueprint.route("/api/extract", methods=["POST"])
    def api_extract():
        body = validate_json(ExtractRequest)
        brands = body.brands or [_own_brand(_rid())]
        jid = _new_job("extract", _rid(), brands,
                       {"platforms": body.platforms or PLATFORMS,
                        "datasets": body.datasets or ["custom"],
                        "also_group": body.also_group})
        threading.Thread(target=_run_extract, daemon=True,
                         args=(jid, _rid(), brands,
                               body.platforms or PLATFORMS,
                               body.datasets or ["custom"], body.also_group)).start()
        return jsonify({"job_id": jid})

    @blueprint.route("/api/group", methods=["POST"])
    def api_group():
        body = validate_json(GroupRequest)
        brands = body.brands or [_own_brand(_rid())]
        jid = _new_job("cluster", _rid(), brands, {"threshold": body.threshold})

        def work():
            try:
                _group_brands(jid, _rid(), brands, body.threshold)
                E.job_update(jid, status="done", stage="complete")
            except Exception as e:  # noqa: BLE001
                E.job_update(jid, status="error", error=str(e)[:500], log=f"ERROR {e}")

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"job_id": jid})

    @blueprint.route("/api/job")
    def api_job():
        q = validate_query(JobQuery)
        with session_scope() as s:
            j = s.scalar(select(BpJob).where(BpJob.job_id == q.job_id))
            if not j:
                return jsonify({"error": "unknown job"}), 404
            return jsonify({"job_id": j.job_id, "kind": j.kind, "status": j.status,
                            "stage": j.stage, "done": j.done, "total": j.total,
                            "log": (j.log or [])[-12:], "error": j.error})

    @blueprint.route("/api/job/cancel", methods=["POST"])
    def api_job_cancel():
        body = validate_json(JobCancel)
        with session_scope() as s:
            j = s.scalar(select(BpJob).where(BpJob.job_id == body.job_id))
            if j:
                j.cancel = True
        return jsonify({"ok": True})

    @blueprint.route("/api/jobs/active")
    def api_jobs_active():
        with session_scope() as s:
            rows = s.execute(select(BpJob)
                             .where(BpJob.status == "running",
                                    BpJob.report_id == report_id)
                             .order_by(BpJob.started_at.desc()).limit(5)).scalars().all()
            return jsonify({"jobs": [{"job_id": j.job_id, "kind": j.kind, "stage": j.stage,
                                      "done": j.done, "total": j.total,
                                      "report_id": j.report_id} for j in rows]})

    # --------------------------------------------------------- restart recovery
    def _adopt_orphan_jobs():
        """The Console restarts on every file edit, killing worker threads. Adopt any
        job row still marked running for THIS report and re-run it (idempotent).

        `_ADOPTED_ONCE` is a module-level set keyed by report_id: `global` on a
        closure variable is illegal, and each per-project report adopts only its
        own orphans.
        """
        if report_id in _ADOPTED_ONCE:
            return
        _ADOPTED_ONCE.add(report_id)
        try:
            with session_scope() as s:
                rows = s.execute(select(BpJob).where(
                    BpJob.status == "running",
                    BpJob.report_id == report_id)).scalars().all()
                orphans = [(j.job_id, j.kind, j.report_id, list(j.brands or []),
                            dict(j.params or {})) for j in rows]
        except Exception:  # noqa: BLE001 — table may not exist on first import
            return
        for jid, kind, rid, brands, params in orphans:
            E.job_update(jid, log="server restarted — resuming job")
            if kind == "extract":
                threading.Thread(target=_run_extract, daemon=True,
                                 args=(jid, rid, brands,
                                       params.get("platforms") or PLATFORMS,
                                       params.get("datasets") or ["custom"],
                                       bool(params.get("also_group", True)))).start()
            else:
                def work(jid=jid, rid=rid, brands=brands, params=params):
                    try:
                        _group_brands(jid, rid, brands,
                                      float(params.get("threshold") or E.SIM_THRESHOLD))
                        E.job_update(jid, status="done", stage="complete")
                    except Exception as e:  # noqa: BLE001
                        E.job_update(jid, status="error", error=str(e)[:500])
                threading.Thread(target=work, daemon=True).start()

    # Lazy, not on a timer: an import-time query configures SQLAlchemy mappers
    # while other apps' models are still being imported, which caches a mapper
    # failure process-wide. See the note in _brand_report_core.
    @blueprint.before_request
    def _lazy_adopt():
        if report_id not in _ADOPTED_ONCE:
            _adopt_orphan_jobs()

    return blueprint
