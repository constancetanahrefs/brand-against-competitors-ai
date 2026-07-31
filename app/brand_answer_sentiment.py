"""Brand Answer Sentiment — "What <platform> says about <brand>".

A Brand-Radar-driven report over AI answers to tracked prompts. Everything is
pickable in the Report Settings panel (report, subject brand, platforms,
countries, dataset, time window, sort) and every filter change is a SQL query
over stored rows — no API calls, no LLM spend.

Costed operations are explicit buttons:
  * Fetch    — pull a (platform, dataset) slice from Brand Radar
  * Analyse  — LLM verdicts for the picked subject brand on stored rows
Both run as background jobs with polling (nginx kills anything over 30s).
"""

NAME = "What AI says about your brand against competitors"
OWNER = "me"

import threading
import uuid

from flask import Blueprint, render_template, jsonify, request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, delete, func

from src.db import db_session, session_scope
from src.schemas import validate_json, validate_query

from ._brand_answer_sentiment_models import (
    BasReport, BasFetch, BasResponse, BasVerdict, BasJob,
)
from . import _brand_answer_sentiment_engine as E
# The positioning view is a SUB-REPORT of this one: same stored answers, different
# question. It lives in its own module for size, but is mounted below so the pair
# shows up as ONE report in /reports.
from ._brand_positioning_view import blueprint as _positioning_bp

blueprint = Blueprint("brand_answer_sentiment", __name__,
                      template_folder="../templates/brand_answer_sentiment")
blueprint.register_blueprint(_positioning_bp, url_prefix="/positioning")


# Idempotent forward migration — runs at import under the `console` OS user,
# which owns these tables (the agent user cannot ALTER them).
def _run_migrations():
    try:
        from sqlalchemy import text
        with db_session.begin():
            db_session.execute(text(
                "ALTER TABLE bas_report ADD COLUMN IF NOT EXISTS "
                "secret varchar(64) DEFAULT 'ahrefs_oauth'"))
    except Exception as e:
        print(f"[brand_answer_sentiment] migration note: {e}")


@blueprint.record_once
def _on_register(setup_state):
    _run_migrations()

DEFAULT_WINDOW = 3   # months — confirmed default backfill scope
VALID_VERDICTS = {"positive", "neutral", "mixed", "negative", "absent", "unjudged"}


# ------------------------------------------------------------------- schemas
class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ViewQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str = Field(min_length=8, max_length=64)
    subject: str = Field(min_length=1, max_length=120)
    platforms: str = Field(default="")     # comma-separated
    datasets: str = Field(default="custom")
    countries: str = Field(default="")
    months: str = Field(default="")
    # Default = most negative responses first (user preference, 2026-07-30).
    sort: str = Field(
        default="negative_count",
        pattern=r"^(criticism|criticism_asc|pct_negative|pct_negative_asc"
                r"|negative_count|negative_count_asc|search_volume|responses)$")
    min_responses: int = Field(default=1, ge=1, le=1000)


class DetailQuery(BaseModel):
    """Prompt drill-down: page-level scope + the same in-modal filters as Browse."""
    model_config = ConfigDict(extra="forbid")
    report_id: str = Field(min_length=8, max_length=64)
    subject: str = Field(min_length=1, max_length=120)
    prompt_hash: str = Field(min_length=4, max_length=40)
    platforms: str = Field(default="")
    datasets: str = Field(default="custom")
    countries: str = Field(default="")
    months: str = Field(default="")
    verdicts: str = Field(default="")
    mention: str = Field(default="any", pattern=r"^(any|mentioned|absent)$")
    sort: str = Field(default="date_desc",
                      pattern=r"^(date_desc|date_asc|volume_desc|volume_asc|platform)$")


class BrowseQuery(BaseModel):
    """Filters for the Browse tab. `verdicts` and `mention` are separate cuts of
    the same column so they can be combined (e.g. mentioned + only negative)."""
    model_config = ConfigDict(extra="forbid")
    report_id: str = Field(min_length=8, max_length=64)
    subject: str = Field(min_length=1, max_length=120)
    platforms: str = Field(default="")
    datasets: str = Field(default="custom")
    countries: str = Field(default="")
    months: str = Field(default="")
    verdicts: str = Field(default="")          # positive,neutral,mixed,negative,absent,unjudged
    mention: str = Field(default="any", pattern=r"^(any|mentioned|absent)$")
    search: str = Field(default="", max_length=200)
    sort: str = Field(default="date_desc",
                      pattern=r"^(date_desc|date_asc|volume_desc|volume_asc|prompt|platform)$")
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=1_000_000)


class ResponseQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str = Field(min_length=8, max_length=64)
    subject: str = Field(min_length=1, max_length=120)
    response_hash: str = Field(min_length=8, max_length=40)


class FetchRequest(_Req):
    report_id: str = Field(min_length=8, max_length=64)
    platforms: list[str] = Field(default_factory=list, max_length=7)
    datasets: list[str] = Field(default_factory=lambda: ["custom"], max_length=2)
    window_months: int = Field(default=DEFAULT_WINDOW, ge=0, le=48)
    subject: str = Field(default="", max_length=120)
    analyse: bool = Field(default=True)


class AnalyseRequest(_Req):
    report_id: str = Field(min_length=8, max_length=64)
    subject: str = Field(min_length=1, max_length=120)
    platforms: list[str] = Field(default_factory=list, max_length=7)
    datasets: list[str] = Field(default_factory=lambda: ["custom"], max_length=2)


class ProbeRequest(_Req):
    report_id: str = Field(min_length=8, max_length=64)


class JobQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=8, max_length=40)


class JobCancel(_Req):
    job_id: str = Field(min_length=8, max_length=40)


# --------------------------------------------------------------------- helpers
def _csv(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _reports_list() -> list[dict]:
    rows = db_session.execute(
        select(BasReport).order_by(BasReport.name)).scalars().all()
    stored = dict(db_session.execute(
        select(BasResponse.report_id, func.count())
        .group_by(BasResponse.report_id)).all())
    out = []
    for r in rows:
        probe = r.probe or {}
        out.append({
            "report_id": r.report_id, "name": r.name, "owner": r.owner,
            "n_custom_prompts": r.n_custom_prompts,
            "probed": bool(r.probed_at),
            "probe": probe,
            "n_stored": stored.get(r.report_id, 0),
            "entities": E.entities_of({"brands": r.brands or [],
                                       "competitors": r.competitors or [],
                                       "niche": r.niche or []}),
            "countries": r.countries or [],
            "has_custom": bool(sum((probe.get("custom") or {}).values())),
        })
    # reports with data first, then ones with custom prompts, then the rest
    out.sort(key=lambda x: (-x["n_stored"], -x["n_custom_prompts"], x["name"].lower()))
    return out


def _subject_keywords(report_id: str, subject: str) -> list[str]:
    rep = E.load_report(report_id) or {}
    for e in E.entities_of(rep):
        if e["name"] == subject:
            return e.get("keywords") or [subject]
    return [subject]


def _new_job(kind: str, report_id: str, subject: str, params: dict) -> str:
    jid = uuid.uuid4().hex[:16]
    with session_scope() as s:
        s.add(BasJob(job_id=jid, kind=kind, report_id=report_id, subject=subject,
                     params=params, status="running", stage="starting"))
    return jid


# ---------------------------------------------------------------------- routes
@blueprint.route("/")
def index():
    reports = _reports_list()
    return render_template("brand_answer_sentiment/index.html",
                           reports=reports,
                           platforms=E.PLATFORMS,
                           platform_labels=E.PLATFORM_LABELS,
                           dataset_labels=E.DATASET_LABELS,
                           default_window=DEFAULT_WINDOW,
                           sentiment_url="/reports/brand_answer_sentiment/",
                           positioning_url="/reports/brand_answer_sentiment/positioning/")


@blueprint.route("/api/reports")
def api_reports():
    return jsonify({"reports": _reports_list()})


@blueprint.route("/api/reports/refresh", methods=["POST"])
def api_refresh_reports():
    try:
        n = E.refresh_reports()
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "n": n, "reports": _reports_list()})


@blueprint.route("/api/probe", methods=["POST"])
def api_probe():
    body = validate_json(ProbeRequest)
    try:
        probe = E.probe_report(body.report_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502
    rep = E.load_report(body.report_id)
    return jsonify({"ok": True, "probe": probe, "report": rep,
                    "entities": E.entities_of(rep)})


@blueprint.route("/api/context")
def api_context():
    """Report config + coverage + available filter values for the settings panel."""
    report_id = (request.args.get("report_id") or "").strip()
    subject = (request.args.get("subject") or "").strip()
    rep = E.load_report(report_id)
    if not rep:
        return jsonify({"error": "unknown report"}), 404
    ents = E.entities_of(rep)
    subject = subject or (ents[0]["name"] if ents else "")
    return jsonify({
        "report": rep, "entities": ents, "subject": subject,
        "coverage": E.coverage(report_id, subject),
        "filters": E.available_filters(report_id),
    })


@blueprint.route("/api/view")
def api_view():
    q = validate_query(ViewQuery)
    view = E.build_view(q.report_id, q.subject,
                        _csv(q.platforms) or E.PLATFORMS,
                        _csv(q.datasets) or ["custom"],
                        _csv(q.countries), _csv(q.months),
                        min_responses=q.min_responses, sort=q.sort)
    return jsonify(view)


@blueprint.route("/api/prompt")
def api_prompt():
    q = validate_query(DetailQuery)
    return jsonify(E.prompt_detail(q.report_id, q.subject, q.prompt_hash,
                                   _csv(q.platforms) or E.PLATFORMS,
                                   _csv(q.datasets) or ["custom"],
                                   _csv(q.countries), _csv(q.months),
                                   verdicts=[v for v in _csv(q.verdicts)
                                             if v in VALID_VERDICTS],
                                   mention=q.mention, sort=q.sort))


def _run_fetch(jid: str, report_id: str, subject: str, platforms: list[str],
               datasets: list[str], window_months: int, analyse: bool):
    """Fetch slices then (optionally) classify. Idempotent — safe to re-run."""
    try:
        slices = [(ds, pl) for ds in datasets for pl in platforms]
        E.job_update(jid, total=len(slices), stage="pulling from Brand Radar",
                     log=f"{len(slices)} slice(s), window={window_months or 'all'} month(s)")
        for i, (ds, pl) in enumerate(slices, 1):
            if E.job_cancelled(jid):
                E.job_update(jid, status="error", error="cancelled")
                return
            # Resume support: a slice already completed for this window is skipped,
            # so a mid-job server restart doesn't re-pull what we already have.
            if E.slice_complete(report_id, pl, ds, window_months):
                E.job_update(jid, done=i,
                             log=f"{E.PLATFORM_LABELS.get(pl, pl)}/{ds}: already fetched, skipping")
                continue
            E.job_update(jid, stage=f"[{i}/{len(slices)}] {E.PLATFORM_LABELS.get(pl, pl)} · {E.DATASET_LABELS[ds]}")
            r = E.fetch_slice(report_id, pl, ds, window_months, job_id=jid)
            E.job_update(jid, log=f"{E.PLATFORM_LABELS.get(pl, pl)}/{ds}: {r['seen']:,} seen, {r['kept']:,} in window")
        if analyse and subject:
            E.job_update(jid, stage="classifying", log=f"analysing sentiment for {subject}")
            res = E.classify_pending(report_id, subject,
                                     _subject_keywords(report_id, subject),
                                     platforms, datasets, job_id=jid)
            E.job_update(jid, log=f"absent {res['absent']:,} · classified {res['classified']:,}")
        E.job_update(jid, status="done", stage="complete")
    except Exception as e:  # noqa: BLE001
        E.job_update(jid, status="error", error=str(e)[:500], log=f"ERROR {e}")


def _run_classify(jid: str, report_id: str, subject: str, platforms: list[str],
                  datasets: list[str]):
    try:
        E.job_update(jid, stage=f"analysing {subject}")
        res = E.classify_pending(report_id, subject, _subject_keywords(report_id, subject),
                                 platforms, datasets, job_id=jid)
        E.job_update(jid, status="done", stage="complete",
                     log=f"absent {res['absent']:,} · classified {res['classified']:,}")
    except Exception as e:  # noqa: BLE001
        E.job_update(jid, status="error", error=str(e)[:500], log=f"ERROR {e}")


@blueprint.route("/api/browse")
def api_browse():
    q = validate_query(BrowseQuery)
    verdicts = [v for v in _csv(q.verdicts) if v in VALID_VERDICTS]
    return jsonify(E.browse_responses(
        q.report_id, q.subject,
        platforms=_csv(q.platforms) or E.PLATFORMS,
        datasets=_csv(q.datasets) or ["custom"],
        countries=_csv(q.countries), months=_csv(q.months),
        verdicts=verdicts, mention=q.mention, search=q.search.strip(),
        sort=q.sort, limit=q.limit, offset=q.offset))


@blueprint.route("/api/response")
def api_response():
    q = validate_query(ResponseQuery)
    d = E.response_detail(q.report_id, q.subject, q.response_hash)
    if not d:
        return jsonify({"error": "not found"}), 404
    return jsonify(d)


@blueprint.route("/api/fetch", methods=["POST"])
def api_fetch():
    body = validate_json(FetchRequest)
    platforms = body.platforms or ["chatgpt"]
    datasets = body.datasets or ["custom"]
    jid = _new_job("fetch", body.report_id, body.subject,
                   {"platforms": platforms, "datasets": datasets,
                    "window_months": body.window_months, "analyse": body.analyse})
    threading.Thread(target=_run_fetch, daemon=True,
                     args=(jid, body.report_id, body.subject, platforms, datasets,
                           body.window_months, body.analyse)).start()
    return jsonify({"job_id": jid})


@blueprint.route("/api/analyse", methods=["POST"])
def api_analyse():
    body = validate_json(AnalyseRequest)
    platforms = body.platforms or E.PLATFORMS
    datasets = body.datasets or ["custom"]
    jid = _new_job("classify", body.report_id, body.subject,
                   {"platforms": platforms, "datasets": datasets})
    threading.Thread(target=_run_classify, daemon=True,
                     args=(jid, body.report_id, body.subject, platforms, datasets)).start()
    return jsonify({"job_id": jid})


@blueprint.route("/api/pending")
def api_pending():
    report_id = (request.args.get("report_id") or "").strip()
    subject = (request.args.get("subject") or "").strip()
    platforms = _csv(request.args.get("platforms", "")) or E.PLATFORMS
    datasets = _csv(request.args.get("datasets", "")) or ["custom"]
    if not report_id or not subject:
        return jsonify({"error": "report_id and subject required"}), 422
    return jsonify({"pending": E.pending_count(report_id, subject, platforms, datasets)})


@blueprint.route("/api/job")
def api_job():
    q = validate_query(JobQuery)
    with session_scope() as s:
        j = s.scalar(select(BasJob).where(BasJob.job_id == q.job_id))
        if not j:
            return jsonify({"error": "unknown job"}), 404
        return jsonify({"job_id": j.job_id, "kind": j.kind, "status": j.status,
                        "stage": j.stage, "done": j.done, "total": j.total,
                        "log": (j.log or [])[-12:], "error": j.error,
                        "subject": j.subject})


@blueprint.route("/api/job/cancel", methods=["POST"])
def api_job_cancel():
    body = validate_json(JobCancel)
    with session_scope() as s:
        j = s.scalar(select(BasJob).where(BasJob.job_id == body.job_id))
        if j:
            j.cancel = True
    return jsonify({"ok": True})


# --------------------------------------------------------- restart recovery
# The Console server restarts on every file edit, which kills worker threads.
# A job row left in 'running' with no live thread would spin the poller forever,
# so on import we adopt any orphan and re-run it (both operations are idempotent:
# fetch dedupes on response_hash, classify skips responses already judged).
_ADOPTED = False


def _adopt_orphan_jobs():
    global _ADOPTED
    if _ADOPTED:
        return
    _ADOPTED = True
    try:
        with session_scope() as s:
            rows = s.execute(select(BasJob).where(BasJob.status == "running")).scalars().all()
            orphans = [(j.job_id, j.kind, j.report_id, j.subject, dict(j.params or {}))
                       for j in rows]
    except Exception:  # noqa: BLE001 — table may not exist yet on first import
        return
    for jid, kind, rid, subject, params in orphans:
        pl = params.get("platforms") or E.PLATFORMS
        ds = params.get("datasets") or ["custom"]
        E.job_update(jid, log="server restarted — resuming job")
        if kind == "fetch":
            threading.Thread(target=_run_fetch, daemon=True,
                             args=(jid, rid, subject, pl, ds,
                                   int(params.get("window_months") or DEFAULT_WINDOW),
                                   bool(params.get("analyse", True)))).start()
        else:
            threading.Thread(target=_run_classify, daemon=True,
                             args=(jid, rid, subject, pl, ds)).start()


threading.Timer(2.0, _adopt_orphan_jobs).start()


@blueprint.route("/api/jobs/active")
def api_jobs_active():
    with session_scope() as s:
        rows = s.execute(select(BasJob).where(BasJob.status == "running")
                         .order_by(BasJob.started_at.desc()).limit(5)).scalars().all()
        return jsonify({"jobs": [{"job_id": j.job_id, "kind": j.kind, "stage": j.stage,
                                  "done": j.done, "total": j.total,
                                  "report_id": j.report_id, "subject": j.subject}
                                 for j in rows]})
