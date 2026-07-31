"""Shared implementation for the per-project brand answer report.

ONE CONSOLE REPORT PER BRAND RADAR PROJECT. This module is a factory, not a
report: it has no NAME/OWNER and its filename is underscore-prefixed, so
app.discover() skips it. Each thin wrapper in reports/ pins ONE report_id and
gets its own entry under /reports/:

    reports/brand_ahrefs.py   -> make_report(REPORT_ID, NAME) -> /reports/brand_ahrefs/
    reports/brand_hubspot.py  -> make_report(REPORT_ID, NAME) -> /reports/brand_hubspot/

Every route below reads the pinned report_id from the blueprint's own config
instead of a query string, so a report can only ever see its own project's data.
The positioning view is mounted at <report>/positioning/ as a second tab.

Costed operations stay explicit buttons (Fetch / Analyse), run as background
jobs with polling (nginx kills anything over 30s), and never fire on page load.
"""

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
# The positioning view is the SECOND TAB of every per-project report: same stored
# answers, different question. Also a factory, pinned to the same report_id.
from ._brand_positioning_view import make_positioning as _make_positioning

# Set by make_report() before the routes are registered. Read via _rid().
_PINNED: dict[str, str] = {}


def _rid() -> str:
    """The report_id this blueprint is pinned to. Never from user input."""
    from flask import request as _rq
    bp = (_rq.blueprint or "").split(".")[0]
    return _PINNED.get(bp, "")


def _slug() -> str:
    from flask import request as _rq
    return (_rq.blueprint or "").split(".")[0]


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


DEFAULT_WINDOW = 3   # months — confirmed default backfill scope
VALID_VERDICTS = {"positive", "neutral", "mixed", "negative", "absent", "unjudged"}


# ------------------------------------------------------------------- schemas
class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ViewQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str = ""   # ignored: the blueprint is pinned to one report
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
    report_id: str = ""   # ignored: the blueprint is pinned to one report
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
    report_id: str = ""   # ignored: the blueprint is pinned to one report
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
    report_id: str = ""   # ignored: the blueprint is pinned to one report
    subject: str = Field(min_length=1, max_length=120)
    response_hash: str = Field(min_length=8, max_length=40)


class FetchRequest(_Req):
    report_id: str = ""   # ignored: the blueprint is pinned to one report
    platforms: list[str] = Field(default_factory=list, max_length=7)
    datasets: list[str] = Field(default_factory=lambda: ["custom"], max_length=2)
    window_months: int = Field(default=DEFAULT_WINDOW, ge=0, le=48)
    subject: str = Field(default="", max_length=120)
    analyse: bool = Field(default=True)


class AnalyseRequest(_Req):
    report_id: str = ""   # ignored: the blueprint is pinned to one report
    subject: str = Field(min_length=1, max_length=120)
    platforms: list[str] = Field(default_factory=list, max_length=7)
    datasets: list[str] = Field(default_factory=lambda: ["custom"], max_length=2)


class ProbeRequest(_Req):
    report_id: str = ""   # ignored: the blueprint is pinned to one report


class JobQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=8, max_length=40)


class JobCancel(_Req):
    job_id: str = Field(min_length=8, max_length=40)


# --------------------------------------------------------------------- helpers
def _csv(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


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


# --------------------------------------------------------------------- factory
def make_report(report_id: str, title: str, slug: str):
    """Build a Console report pinned to ONE Brand Radar report.

    Returns (blueprint, NAME) for a thin wrapper module to expose. The wrapper is
    what app.discover() sees, so each project appears as its own report and can
    never read another project's rows — report_id is closed over here, not taken
    from the request.
    """
    blueprint = Blueprint(slug, __name__,
                          template_folder="../templates/brand_answer_sentiment")
    _PINNED[slug] = report_id

    # second tab: same stored answers, positioning question
    blueprint.register_blueprint(
        _make_positioning(report_id, slug, title), url_prefix="/positioning")

    @blueprint.record_once
    def _on_register(setup_state):  # noqa: ARG001
        _run_migrations()

    @blueprint.before_request
    def _lazy_adopt():
        _adopt_on_first_request()

    # ---------------------------------------------------------------------- routes
    def _api_map() -> dict:
        """Absolute URLs for the JS layer. Built here rather than with url_for in
        the template because the endpoint prefix is the per-project slug."""
        b = f"/reports/{slug}/"
        return {"ctx": b+"api/context", "view": b+"api/view", "prompt": b+"api/prompt",
                "fetch": b+"api/fetch", "analyse": b+"api/analyse", "probe": b+"api/probe",
                "refresh": b+"api/reports/refresh", "job": b+"api/job",
                "cancel": b+"api/job/cancel", "active": b+"api/jobs/active",
                "browse": b+"api/browse", "response": b+"api/response",
                "pending": b+"api/pending"}

    @blueprint.route("/")
    def index():
        rep = E.load_report(report_id) or {}
        return render_template("brand_answer_sentiment/index.html",
                               api=_api_map(),
                               report=rep,
                               report_id=report_id,
                               report_title=title,
                               platforms=E.PLATFORMS,
                               platform_labels=E.PLATFORM_LABELS,
                               dataset_labels=E.DATASET_LABELS,
                               default_window=DEFAULT_WINDOW,
                               sentiment_url=f"/reports/{slug}/",
                               positioning_url=f"/reports/{slug}/positioning/")

    @blueprint.route("/api/reports/refresh", methods=["POST"])
    def api_refresh_reports():
        """Re-read THIS report's config from Brand Radar (name, entities, filters)."""
        try:
            n = E.refresh_reports()
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 502
        return jsonify({"ok": True, "n": n, "report": E.load_report(report_id)})

    @blueprint.route("/api/probe", methods=["POST"])
    def api_probe():
        body = validate_json(ProbeRequest)
        try:
            probe = E.probe_report(_rid())
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 502
        rep = E.load_report(_rid())
        return jsonify({"ok": True, "probe": probe, "report": rep,
                        "entities": E.entities_of(rep)})

    @blueprint.route("/api/context")
    def api_context():
        """Report config + coverage + available filter values for the settings panel."""
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
        view = E.build_view(_rid(), q.subject,
                            _csv(q.platforms) or E.PLATFORMS,
                            _csv(q.datasets) or ["custom"],
                            _csv(q.countries), _csv(q.months),
                            min_responses=q.min_responses, sort=q.sort)
        return jsonify(view)

    @blueprint.route("/api/prompt")
    def api_prompt():
        q = validate_query(DetailQuery)
        return jsonify(E.prompt_detail(_rid(), q.subject, q.prompt_hash,
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
            _rid(), q.subject,
            platforms=_csv(q.platforms) or E.PLATFORMS,
            datasets=_csv(q.datasets) or ["custom"],
            countries=_csv(q.countries), months=_csv(q.months),
            verdicts=verdicts, mention=q.mention, search=q.search.strip(),
            sort=q.sort, limit=q.limit, offset=q.offset))

    @blueprint.route("/api/response")
    def api_response():
        q = validate_query(ResponseQuery)
        d = E.response_detail(_rid(), q.subject, q.response_hash)
        if not d:
            return jsonify({"error": "not found"}), 404
        return jsonify(d)

    @blueprint.route("/api/fetch", methods=["POST"])
    def api_fetch():
        body = validate_json(FetchRequest)
        platforms = body.platforms or ["chatgpt"]
        datasets = body.datasets or ["custom"]
        jid = _new_job("fetch", _rid(), body.subject,
                       {"platforms": platforms, "datasets": datasets,
                        "window_months": body.window_months, "analyse": body.analyse})
        threading.Thread(target=_run_fetch, daemon=True,
                         args=(jid, _rid(), body.subject, platforms, datasets,
                               body.window_months, body.analyse)).start()
        return jsonify({"job_id": jid})

    @blueprint.route("/api/analyse", methods=["POST"])
    def api_analyse():
        body = validate_json(AnalyseRequest)
        platforms = body.platforms or E.PLATFORMS
        datasets = body.datasets or ["custom"]
        jid = _new_job("classify", _rid(), body.subject,
                       {"platforms": platforms, "datasets": datasets})
        threading.Thread(target=_run_classify, daemon=True,
                         args=(jid, _rid(), body.subject, platforms, datasets)).start()
        return jsonify({"job_id": jid})

    @blueprint.route("/api/pending")
    def api_pending():
        subject = (request.args.get("subject") or "").strip()
        platforms = _csv(request.args.get("platforms", "")) or E.PLATFORMS
        datasets = _csv(request.args.get("datasets", "")) or ["custom"]
        if not subject:
            return jsonify({"error": "subject required"}), 422
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


    @blueprint.route("/api/jobs/active")
    def api_jobs_active():
        """Only jobs for THIS report — a sibling project's job must not show here."""
        with session_scope() as s:
            rows = s.execute(select(BasJob)
                             .where(BasJob.status == "running",
                                    BasJob.report_id == report_id)
                             .order_by(BasJob.started_at.desc()).limit(5)).scalars().all()
            return jsonify({"jobs": [{"job_id": j.job_id, "kind": j.kind, "stage": j.stage,
                                      "done": j.done, "total": j.total,
                                      "report_id": j.report_id, "subject": j.subject}
                                     for j in rows]})

    return blueprint, title


# --------------------------------------------------------- restart recovery
# The Console server restarts on every file edit, which kills worker threads.
# A job row left in 'running' with no live thread would spin the poller forever,
# so on import we adopt any orphan and re-run it (both operations are idempotent:
# fetch dedupes on response_hash, classify skips responses already judged).
_ADOPTED: list[bool] = []


def _adopt_orphan_jobs():
    # list-as-flag: the timer can fire before module-level assignment completes,
    # and `global` on a not-yet-bound name raises NameError.
    if _ADOPTED:
        return
    _ADOPTED.append(True)
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


# NOTE: do NOT run this on a timer. A DB query fired ~2s after import lands while
# app.discover() is still importing OTHER apps' modules, so SQLAlchemy configures
# its mappers against a HALF-DEFINED registry (e.g. WAWebinar present, WABatch not
# yet) and CACHES that failure — poisoning every ORM query in the process. Instead
# adopt orphans lazily on this report's first request, by which time every model
# in the app is registered.
def _adopt_on_first_request():
    if _ADOPTED:
        return
    _adopt_orphan_jobs()

