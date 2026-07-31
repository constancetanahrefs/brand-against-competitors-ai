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

# NOTE: no module-level NAME/OWNER on purpose — this file is a SUB-REPORT of
# brand_answer_sentiment, not a separate entry in /reports. The parent imports
# `blueprint` from here and mounts it at /reports/brand_answer_sentiment/positioning/.
# The loader skips this file because its name starts with no underscore but it
# exposes no NAME... (see brand_answer_sentiment.py, which registers it.)
def _api_map() -> dict:
    """Absolute URLs for the JS layer. Built here (not via url_for in the
    template) because this blueprint is nested, so endpoint names are prefixed
    by the parent and would be brittle to hardcode in Jinja."""
    b = POSITIONING_URL
    return {"ctx": b+"api/context", "brand": b+"api/brand", "category": b+"api/category",
            "compare": b+"api/compare", "extract": b+"api/extract", "group": b+"api/group",
            "job": b+"api/job", "cancel": b+"api/job/cancel", "active": b+"api/jobs/active"}


blueprint = Blueprint("brand_positioning", __name__,
                      template_folder="../templates/brand_positioning")

SENTIMENT_URL = "/reports/brand_answer_sentiment/"
POSITIONING_URL = "/reports/brand_answer_sentiment/positioning/"


# ------------------------------------------------------------------- schemas
class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractRequest(_Req):
    report_id: str = Field(min_length=8, max_length=64)
    brands: list[str] = Field(default_factory=list, max_length=30)
    platforms: list[str] = Field(default_factory=list, max_length=7)
    datasets: list[str] = Field(default_factory=lambda: ["custom"], max_length=2)
    also_group: bool = Field(default=True)


class GroupRequest(_Req):
    report_id: str = Field(min_length=8, max_length=64)
    brands: list[str] = Field(default_factory=list, max_length=30)
    threshold: float = Field(default=E.SIM_THRESHOLD, ge=0.3, le=0.95)


class BrandQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str = Field(min_length=8, max_length=64)
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
    report_id: str = Field(min_length=8, max_length=64)
    polarity: str = Field(default="strength", pattern=r"^(strength|weakness)$")


class JobQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=8, max_length=40)


class JobCancel(_Req):
    job_id: str = Field(min_length=8, max_length=40)


# -------------------------------------------------------------------- helpers
def _csv(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _reports_with_data() -> list[dict]:
    """Only reports that already have raw answers stored — this report can't fetch."""
    with session_scope() as s:
        counts = dict(s.execute(
            select(BasResponse.report_id, __import__("sqlalchemy").func.count())
            .group_by(BasResponse.report_id)).all())
    out = []
    for rid, n in counts.items():
        rep = load_report(rid)
        if not rep:
            continue
        out.append({"report_id": rid, "name": rep["name"], "n_stored": n,
                    "entities": entities_of(rep)})
    out.sort(key=lambda r: -r["n_stored"])
    return out


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


# ---------------------------------------------------------------------- routes
@blueprint.route("/")
def index():
    reports = _reports_with_data()
    return render_template("brand_positioning/index.html",
                           api=_api_map(),
                           reports=reports, platforms=PLATFORMS,
                           platform_labels=PLATFORM_LABELS,
                           sentiment_url=SENTIMENT_URL,
                           positioning_url=POSITIONING_URL)


@blueprint.route("/api/context")
def api_context():
    report_id = (request.args.get("report_id") or "").strip()
    cov = E.coverage(report_id)
    if not cov:
        return jsonify({"error": "unknown report"}), 404
    return jsonify({**cov, "own_brand": _own_brand(report_id),
                    "filters": E.available_filters(report_id)})


@blueprint.route("/api/brand")
def api_brand():
    q = validate_query(BrandQuery)
    return jsonify(E.brand_view(q.report_id, q.brand, q.polarity,
                                _csv(q.platforms), _csv(q.months), q.sort,
                                min_share=q.min_share))


@blueprint.route("/api/category")
def api_category():
    q = validate_query(CategoryQuery)
    return jsonify(E.category_claims(q.report_id, q.brand, q.polarity,
                                     q.category_key, _csv(q.platforms), _csv(q.months)))


@blueprint.route("/api/compare")
def api_compare():
    q = validate_query(CompareQuery)
    return jsonify(E.compare_view(q.report_id, _own_brand(q.report_id), q.polarity))


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
    brands = body.brands or [_own_brand(body.report_id)]
    jid = _new_job("extract", body.report_id, brands,
                   {"platforms": body.platforms or PLATFORMS,
                    "datasets": body.datasets or ["custom"],
                    "also_group": body.also_group})
    threading.Thread(target=_run_extract, daemon=True,
                     args=(jid, body.report_id, brands,
                           body.platforms or PLATFORMS,
                           body.datasets or ["custom"], body.also_group)).start()
    return jsonify({"job_id": jid})


@blueprint.route("/api/group", methods=["POST"])
def api_group():
    body = validate_json(GroupRequest)
    brands = body.brands or [_own_brand(body.report_id)]
    jid = _new_job("cluster", body.report_id, brands, {"threshold": body.threshold})

    def work():
        try:
            _group_brands(jid, body.report_id, brands, body.threshold)
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
        rows = s.execute(select(BpJob).where(BpJob.status == "running")
                         .order_by(BpJob.started_at.desc()).limit(5)).scalars().all()
        return jsonify({"jobs": [{"job_id": j.job_id, "kind": j.kind, "stage": j.stage,
                                  "done": j.done, "total": j.total,
                                  "report_id": j.report_id} for j in rows]})


# --------------------------------------------------------- restart recovery
_ADOPTED = False


def _adopt_orphan_jobs():
    """The Console restarts on every file edit, killing worker threads. Adopt any
    job row still marked running and re-run it (both stages are idempotent)."""
    global _ADOPTED
    if _ADOPTED:
        return
    _ADOPTED = True
    try:
        with session_scope() as s:
            rows = s.execute(select(BpJob).where(BpJob.status == "running")).scalars().all()
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


threading.Timer(2.0, _adopt_orphan_jobs).start()
