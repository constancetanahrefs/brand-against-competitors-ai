"""Engine for the Brand Answer Sentiment report.

Three concerns, no Flask:

  1. Brand Radar pulls  (``refresh_reports``, ``probe_report``, ``fetch_slice``)
  2. Mention detection + LLM verdicts  (``classify_pending``)
  3. Aggregation for the page  (``build_view``)

Money is spent only in (1) and (2); (3) is pure SQL/Python over stored rows, so
every filter change on the page is free.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import datetime as _dt
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select, func, delete

from src.connectors import invoke as _invoke, ahrefs as _ahrefs_default, ConnectorError

# A workspace can hold more than one Ahrefs OAuth token, and different tokens
# expose DIFFERENT, non-overlapping sets of Brand Radar reports (one per Ahrefs
# workspace the user belongs to). So every call must use the token that can
# actually see the report — resolved per report_id, never a global constant.
# List every secret name you have here; refresh_reports() walks them all and
# records which one returned each report.
AHREFS_SECRETS = ["ahrefs_oauth", "ahrefs_oauth_2"]
_secret_cache: dict[str, str] = {}


def secret_for(report_id: str | None) -> str:
    """Which OAuth secret can see this report. Falls back to the primary token."""
    if not report_id:
        return AHREFS_SECRETS[0]
    if report_id in _secret_cache:
        return _secret_cache[report_id]
    sec = AHREFS_SECRETS[0]
    try:
        with session_scope() as s:
            row = s.scalar(select(BasReport).where(BasReport.report_id == report_id))
            if row and getattr(row, "secret", None):
                sec = row.secret
    except Exception:
        pass
    _secret_cache[report_id] = sec
    return sec


def ahrefs(cap_id: str, args: dict, *, timeout: int = 90,
           full_payload: bool = True, secret: str | None = None) -> dict:
    """Ahrefs call bound to the token that owns the report named in `args`."""
    sec = secret or secret_for(args.get("report_id"))
    return _invoke(cap_id, args, secret=sec, timeout=timeout,
                   full_payload=full_payload)
from src.db import session_scope
from src.llm import console_openai_client

from ._brand_answer_sentiment_models import (
    BasReport, BasFetch, BasResponse, BasVerdict, BasJob,
)

APP_SLUG = "reports:brand_answer_sentiment"
LLM = console_openai_client(app_slug=APP_SLUG)
CLASSIFY_MODEL = "google/gemini-3-flash-preview"

PLATFORMS = ["chatgpt", "gemini", "perplexity", "copilot", "grok",
             "google_ai_overviews", "google_ai_mode"]
PLATFORM_LABELS = {
    "chatgpt": "ChatGPT", "gemini": "Gemini", "perplexity": "Perplexity",
    "copilot": "Copilot", "grok": "Grok",
    "google_ai_overviews": "Google AI Overviews",
    "google_ai_mode": "Google AI Mode",
}
DATASETS = {"custom": "only_custom_queries", "public": "only_public_queries"}
DATASET_LABELS = {"custom": "Brand Radar custom queries", "public": "Public queries"}

VERDICTS = ["positive", "neutral", "mixed", "negative", "absent"]
# Fixed render order of the stacked bar (matches the reference report).
BAR_ORDER = ["negative", "mixed", "neutral", "positive", "absent"]

# Sort keys the prompt table accepts. The `_asc` ("least …") variants also widen
# the table to include prompts with zero criticism — see build_view().
SORT_LABELS = {
    "criticism": "criticism intensity (negative = 2pts, mixed = 1pt), most critical first",
    "criticism_asc": "criticism intensity, least critical first (clean prompts included)",
    "pct_negative": "share of responses that are negative, highest first",
    "pct_negative_asc": "share of responses that are negative, lowest first (clean prompts included)",
    "negative_count": "number of negative responses, most first",
    "negative_count_asc": "number of negative responses, fewest first (clean prompts included)",
    "search_volume": "search volume of the underlying query, highest first",
    "responses": "number of dated responses, most first",
}
ASCENDING_SORTS = {"criticism_asc", "pct_negative_asc", "negative_count_asc"}

PAGE_LIMIT = 1000          # API max
SNIPPET_WINDOW = 340       # chars each side of a mention
MAX_SNIPPETS = 2           # per response — keeps classify cost sane
CLASSIFY_BATCH = 12
CLASSIFY_WORKERS = 14


# --------------------------------------------------------------------- utils
def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode())
        h.update(b"\x00")
    return h.hexdigest()[:32]


def cutoff_date(window_months: int) -> str:
    """Snapshot dates strictly before this are dropped. 0 = keep everything."""
    if not window_months:
        return ""
    today = _dt.date.today()
    month = today.month - window_months + 1
    year = today.year
    while month <= 0:
        month += 12
        year -= 1
    return f"{year:04d}-{month:02d}-01"


def _ahrefs_retry(cap: str, args: dict, *, timeout: int = 300, tries: int = 4,
                  job_id: str | None = None, secret: str | None = None) -> dict:
    """Brand Radar occasionally answers a page with a transient 500 (observed on
    long paginated pulls). Retry with backoff so an unattended run survives it."""
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            return ahrefs(cap, args, timeout=timeout, secret=secret)
        except ConnectorError as e:
            last = e
            msg = str(e).lower()
            transient = any(t in msg for t in
                            ("internalerror", "server_error", "timeout", "timed out",
                             "502", "503", "504", "request failed"))
            if not transient or attempt == tries:
                raise
            wait = 3 * attempt
            if job_id:
                job_update(job_id, log=f"upstream error ({str(e)[:60]}) — retry {attempt}/{tries - 1} in {wait}s")
            time.sleep(wait)
    raise last  # pragma: no cover


def _alias_rx(keywords: list[str]) -> re.Pattern | None:
    parts = [re.escape(k.strip()) for k in keywords if (k or "").strip()]
    if not parts:
        return None
    parts.sort(key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])",
                      re.IGNORECASE)


def _entity(raw: dict) -> dict:
    """Normalise a Brand Radar brand/competitor entry."""
    kw = [k for k in (raw.get("keywords") or []) if k]
    groups = raw.get("urls_groups") or []
    urls, mode = [], "subdomains"
    if groups:
        urls = groups[0].get("urls") or []
        mode = groups[0].get("mode") or "subdomains"
    name = kw[0] if kw else (urls[0] if urls else "")
    return {"name": name, "keywords": kw or ([name] if name else []),
            "urls": urls, "mode": mode}


def scope_of(entity: dict) -> dict:
    e = {"keywords": entity.get("keywords") or [entity.get("name", "")],
         "urls": entity.get("urls") or []}
    if e["urls"]:
        e["mode"] = entity.get("mode") or "subdomains"
    return e


# ----------------------------------------------------------------- job helper
def job_update(job_id: str, **kw):
    log_line = kw.pop("log", None)
    with session_scope() as s:
        j = s.scalar(select(BasJob).where(BasJob.job_id == job_id))
        if not j:
            return None
        for k, v in kw.items():
            setattr(j, k, v)
        if log_line:
            j.log = (j.log or []) + [f"{_now():%H:%M:%S} {log_line}"]
        j.updated_at = _now()
        if kw.get("status") in ("done", "error"):
            j.finished_at = _now()
        return j.cancel


def job_cancelled(job_id: str) -> bool:
    with session_scope() as s:
        return bool(s.scalar(select(BasJob.cancel).where(BasJob.job_id == job_id)))


# ------------------------------------------------------------ report catalogue
def refresh_reports(limit: int = 100) -> int:
    """Cache every Brand Radar report visible to ANY of our Ahrefs tokens.

    The tokens see different workspaces, so we walk them all and stamp each row
    with the secret that returned it — that's what later calls authenticate with.
    First token to return a report wins (they don't overlap in practice).
    """
    n = 0
    seen: set[str] = set()
    for sec in AHREFS_SECRETS:
        try:
            res = _invoke("ahrefs_brand_radar.get_reports", {"limit": limit},
                          secret=sec, timeout=120)
        except ConnectorError as e:
            print(f"[bas] get_reports failed for {sec}: {e}")
            continue
        recs = res.get("records") or []
        with session_scope() as s:
            for rec in recs:
                rid = rec.get("id")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                row = s.scalar(select(BasReport).where(BasReport.report_id == rid))
                if not row:
                    row = BasReport(report_id=rid)
                    s.add(row)
                row.name = rec.get("name") or "(untitled report)"
                row.owner = ((rec.get("owner") or {}).get("name") or "")
                row.brands = [_entity(b) for b in (rec.get("brands") or [])]
                row.competitors = [_entity(b) for b in (rec.get("competitors") or [])]
                row.niche = [_entity(b) for b in (rec.get("niche") or [])]
                row.countries = ((rec.get("filters") or {}).get("countries") or [])
                row.secret = sec
                row.refreshed_at = _now()
                n += 1
        _secret_cache.clear()
    return n


def load_report(report_id: str) -> dict | None:
    with session_scope() as s:
        r = s.scalar(select(BasReport).where(BasReport.report_id == report_id))
        if not r:
            return None
        return {
            "report_id": r.report_id, "name": r.name, "owner": r.owner,
            "brands": r.brands or [], "competitors": r.competitors or [],
            "niche": r.niche or [], "countries": r.countries or [],
            "n_custom_prompts": r.n_custom_prompts,
            "custom_prompt_countries": r.custom_prompt_countries or [],
            "probe": r.probe or {},
            "probed_at": r.probed_at.isoformat() if r.probed_at else None,
            "secret": getattr(r, "secret", None) or AHREFS_SECRETS[0],
        }


def entities_of(rep: dict) -> list[dict]:
    """All pickable subject entities: own brands, then competitors, then niche."""
    out = []
    for kind, lst in (("brand", rep.get("brands") or []),
                      ("competitor", rep.get("competitors") or []),
                      ("niche", rep.get("niche") or [])):
        for e in lst:
            if e.get("name"):
                out.append({**e, "kind": kind})
    return out


def probe_report(report_id: str) -> dict:
    """Row counts per (dataset, platform) — 14 count-only calls, ~1s each."""
    rep = load_report(report_id)
    if not rep:
        raise ConnectorError("Report not cached — refresh the report list first.")
    ents = entities_of(rep)
    own = [e for e in ents if e["kind"] == "brand"] or ents[:1]
    scope = {"brands": [scope_of(e) for e in own],
             "competitors": [scope_of(e) for e in ents if e["kind"] == "competitor"],
             "niche": [scope_of(e) for e in ents if e["kind"] == "niche"]}

    # custom-prompt roster (also the 'M prompts' denominator)
    n_prompts, prompt_countries = 0, []
    try:
        q = ahrefs("ahrefs_brand_radar.get_custom_queries",
                   {"report_id": report_id, "limit": 1000}, timeout=120)
        n_prompts = int(q.get("count") or 0)
        prompt_countries = sorted({(r.get("country") or "") for r in (q.get("records") or [])} - {""})
    except ConnectorError:
        pass

    def one(pair):
        ds, pl = pair
        args = {"pagination": {"limit": 0, "offset": 0},
                "filters": {"models": [pl], "country": [], "date": None},
                "report": scope,
                "queries_dataset_filter": DATASETS[ds],
                "report_id": report_id}
        try:
            res = ahrefs("ahrefs_brand_radar.ai_responses_results", args, timeout=120)
            return ds, pl, int(res.get("total_results") or 0)
        except ConnectorError:
            return ds, pl, 0

    probe = {"custom": {}, "public": {}}
    pairs = [(ds, pl) for ds in DATASETS for pl in PLATFORMS]
    with ThreadPoolExecutor(max_workers=7) as ex:
        for ds, pl, n in ex.map(one, pairs):
            probe[ds][pl] = n

    with session_scope() as s:
        r = s.scalar(select(BasReport).where(BasReport.report_id == report_id))
        r.probe = probe
        r.n_custom_prompts = n_prompts
        r.custom_prompt_countries = prompt_countries
        r.probed_at = _now()
    return probe


# ------------------------------------------------------------------- fetching
def fetch_slice(report_id: str, platform: str, dataset: str, window_months: int,
                job_id: str | None = None) -> dict:
    """Pull every response for one (platform, dataset) slice inside the window.

    Rows are stored for ALL countries; the page filters by country in SQL.
    Mention detection for every report entity happens here (free, regex).
    """
    rep = load_report(report_id)
    ents = entities_of(rep)
    own = [e for e in ents if e["kind"] == "brand"] or ents[:1]
    scope = {"brands": [scope_of(e) for e in own],
             "competitors": [scope_of(e) for e in ents if e["kind"] == "competitor"],
             "niche": [scope_of(e) for e in ents if e["kind"] == "niche"]}
    rxs = [(e["name"], _alias_rx(e["keywords"])) for e in ents]
    rxs = [(n, rx) for n, rx in rxs if rx]

    cutoff = cutoff_date(window_months)
    kept = seen = 0
    months: Counter = Counter()
    countries: Counter = Counter()
    offset = 0
    total = None

    while True:
        if job_id and job_cancelled(job_id):
            break
        args = {"pagination": {"limit": PAGE_LIMIT, "offset": offset},
                "filters": {"models": [platform], "country": [], "date": None},
                "report": scope,
                "queries_dataset_filter": DATASETS[dataset],
                "report_id": report_id,
                "sort_by": "relevance"}
        res = _ahrefs_retry("ahrefs_brand_radar.ai_responses_results", args,
                            timeout=300, job_id=job_id)
        recs = res.get("records") or []
        if total is None:
            total = int(res.get("total_results") or 0)
        if not recs:
            break
        seen += len(recs)

        rows = []
        for rec in recs:
            snap = (rec.get("updated_at") or "")[:10]
            if cutoff and snap and snap < cutoff:
                continue
            q = rec.get("question") or ""
            text = rec.get("response") or ""
            rh = sha(report_id, dataset, platform, snap, q, text[:400])
            hits = [name for name, rx in rxs if rx.search(text)]
            rows.append({
                "response_hash": rh, "report_id": report_id, "dataset": dataset,
                "platform": platform, "prompt_hash": sha(q),
                "question": q, "response": text,
                "search_volume": int(rec.get("search_volume") or 0),
                "country": rec.get("serp_country") or "",
                "snapshot_date": snap, "snapshot_month": snap[:7],
                "sitelinks": [{"url": s.get("url"), "title": s.get("title"),
                               "cited": bool(s.get("cited")), "position": s.get("position")}
                              for s in (rec.get("sitelinks") or [])][:12],
                "brands_hit": hits,
                "search_queries": (rec.get("search_queries") or [])[:6],
            })
            months[snap[:7]] += 1
            countries[rec.get("serp_country") or ""] += 1

        kept += _upsert_responses(rows)
        offset += len(recs)
        if job_id:
            job_update(job_id, done=seen,
                       stage=f"{PLATFORM_LABELS.get(platform, platform)} · {seen:,}/{total:,} pulled, {kept:,} in window")
        if total and offset >= total:
            break

    with session_scope() as s:
        f = s.scalar(select(BasFetch).where(BasFetch.report_id == report_id,
                                            BasFetch.platform == platform,
                                            BasFetch.dataset == dataset))
        if not f:
            f = BasFetch(report_id=report_id, platform=platform, dataset=dataset)
            s.add(f)
        f.window_months = window_months
        f.cutoff = cutoff
        f.n_seen = seen
        f.months = sorted(months)
        f.countries = sorted(k for k in countries if k)
        f.status = "ok"
        f.error = ""
        f.fetched_at = _now()
        f.n_rows = s.scalar(select(func.count()).select_from(BasResponse).where(
            BasResponse.report_id == report_id, BasResponse.platform == platform,
            BasResponse.dataset == dataset)) or 0
    return {"seen": seen, "kept": kept, "months": sorted(months)}


def slice_complete(report_id: str, platform: str, dataset: str,
                   window_months: int) -> bool:
    """True when this slice was already fetched to completion for a window at
    least as wide as the one asked for. Lets a restarted job skip finished work."""
    with session_scope() as s:
        f = s.scalar(select(BasFetch).where(BasFetch.report_id == report_id,
                                           BasFetch.platform == platform,
                                           BasFetch.dataset == dataset))
        if not f or f.status != "ok" or not f.n_seen:
            return False
        if window_months == 0:
            return f.window_months == 0
        return f.window_months == 0 or f.window_months >= window_months


def _upsert_responses(rows: list[dict]) -> int:
    """Insert rows, skipping ones already stored (ON CONFLICT DO NOTHING)."""
    if not rows:
        return 0
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(BasResponse).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["response_hash"])
    with session_scope() as s:
        s.execute(stmt)
    return len(rows)


# --------------------------------------------------------------- classification
RUBRIC = """You judge how an AI assistant's answer treats ONE subject brand.

Verdicts (choose exactly one per item):
- positive : the answer recommends or praises the subject with no material criticism.
- neutral  : the subject is listed, named or described without evaluation.
- mixed    : praise AND criticism together, or recommended-with-caveats ("great but expensive", "powerful yet steep learning curve").
- negative : criticism dominates, or the answer steers the reader away from the subject.

Rules:
- Judge ONLY the subject brand. Praise of a competitor is not criticism of the subject unless the answer contrasts them to the subject's detriment.
- Price complaints, missing features, data-quality doubts, difficulty, and "not for X" all count as criticism.
- evidence must be a VERBATIM span from the excerpt (<= 240 chars) that best supports the verdict.
- themes: 1-3 short snake_case tags describing what is being praised/criticised (e.g. pricing, data_accuracy, learning_curve, backlinks, reporting, support, integrations, limits, coverage, ux, value_for_money).
- one_liner: <= 120 chars, plain English, what this answer says about the subject.

Return STRICT JSON: {"items":[{"id":<int>,"verdict":"...","themes":["..."],"evidence":"...","one_liner":"..."}]}
One item per input id, same ids, no extras."""


def _snippets(text: str, rx: re.Pattern) -> list[str]:
    spans = [m.start() for m in rx.finditer(text or "")]
    if not spans:
        return []
    out, used = [], []
    for pos in spans:
        if any(abs(pos - u) < SNIPPET_WINDOW for u in used):
            continue
        used.append(pos)
        out.append(text[max(0, pos - SNIPPET_WINDOW): pos + SNIPPET_WINDOW])
        if len(out) >= MAX_SNIPPETS:
            break
    return out


def _parse_json(raw: str) -> dict:
    """Parse model JSON tolerantly: strips ``` fences and accepts a stream of
    concatenated objects (some models emit one object per batch chunk), merging
    their ``items`` arrays."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0].strip()
        if s.startswith("json"):
            s = s[4:].strip()
    dec = json.JSONDecoder()
    items, idx, first = [], 0, None
    while idx < len(s):
        while idx < len(s) and s[idx] in " \t\r\n,":
            idx += 1
        if idx >= len(s):
            break
        try:
            obj, end = dec.raw_decode(s, idx)
        except ValueError:
            break
        idx = end
        if first is None:
            first = obj
        if isinstance(obj, dict):
            items.extend(obj.get("items") or [])
        elif isinstance(obj, list):
            items.extend(obj)
    if items:
        return {"items": items}
    if isinstance(first, dict):
        return first
    raise ValueError("no JSON object in model output")


def pending_count(report_id: str, subject: str, platforms: list[str],
                  datasets: list[str]) -> int:
    with session_scope() as s:
        judged = select(BasVerdict.response_hash).where(
            BasVerdict.report_id == report_id, BasVerdict.subject == subject)
        q = select(func.count()).select_from(BasResponse).where(
            BasResponse.report_id == report_id,
            BasResponse.platform.in_(platforms),
            BasResponse.dataset.in_(datasets),
            BasResponse.response_hash.not_in(judged))
        return s.scalar(q) or 0


def classify_pending(report_id: str, subject: str, keywords: list[str],
                     platforms: list[str], datasets: list[str],
                     job_id: str | None = None, cap: int = 200_000) -> dict:
    """Fill in verdicts for every stored response that has none for this subject.

    Responses that don't mention the subject are marked 'absent' with no LLM call.
    """
    rx = _alias_rx(keywords or [subject])
    with session_scope() as s:
        judged = select(BasVerdict.response_hash).where(
            BasVerdict.report_id == report_id, BasVerdict.subject == subject)
        rows = s.execute(
            select(BasResponse.response_hash, BasResponse.question,
                   BasResponse.response, BasResponse.brands_hit)
            .where(BasResponse.report_id == report_id,
                   BasResponse.platform.in_(platforms),
                   BasResponse.dataset.in_(datasets),
                   BasResponse.response_hash.not_in(judged))
            .limit(cap)).all()

    absent, todo = [], []
    for rh, q, text, hits in rows:
        if rx and rx.search(text or ""):
            snips = _snippets(text or "", rx)
            todo.append((rh, q, snips))
        else:
            absent.append(rh)

    if job_id:
        job_update(job_id, total=len(todo), done=0,
                   log=f"{len(rows):,} unjudged · {len(absent):,} absent (free) · {len(todo):,} to classify")

    # absent rows — bulk insert, no LLM
    if absent:
        _save_verdicts([{"response_hash": rh, "report_id": report_id, "subject": subject,
                         "verdict": "absent", "themes": [], "evidence": "",
                         "one_liner": "", "llm_model": ""} for rh in absent])

    batches = [todo[i:i + CLASSIFY_BATCH] for i in range(0, len(todo), CLASSIFY_BATCH)]
    done = 0
    counts: Counter = Counter()

    def _ask(batch) -> tuple[dict, str | None]:
        items = []
        for i, (rh, q, snips) in enumerate(batch):
            excerpt = "\n…\n".join(snips)[:2600]
            items.append({"id": i, "prompt": q[:220], "excerpt": excerpt})
        payload = {"subject": subject, "items": items}
        try:
            resp = LLM.chat.completions.create(
                model=CLASSIFY_MODEL,
                messages=[{"role": "system", "content": RUBRIC},
                          {"role": "user", "content": json.dumps(payload)[:120_000]}],
                response_format={"type": "json_object"},
                temperature=0)
            return _parse_json(resp.choices[0].message.content), None
        except Exception as e:  # noqa: BLE001 — one bad batch must not kill the job
            return {}, str(e)[:160]

    def run(batch):
        """Classify one batch. Unreturned/garbled items are LEFT UNJUDGED rather than
        defaulted to neutral, so a later 'Analyse' pass retries them instead of
        baking a wrong verdict into the numbers."""
        data, err = _ask(batch)
        if err or not (data.get("items") or []):
            # one retry, split in half — long batches are the usual cause
            mid = max(1, len(batch) // 2)
            merged, errs = [], []
            for part in (batch[:mid], batch[mid:]):
                if not part:
                    continue
                d2, e2 = _ask(part)
                if e2:
                    errs.append(e2)
                    continue
                base = batch.index(part[0])
                for it in (d2.get("items") or []):
                    try:
                        it = dict(it)
                        it["id"] = int(it.get("id", -1)) + base
                        merged.append(it)
                    except (TypeError, ValueError):
                        continue
            if not merged:
                return [], (err or "; ".join(errs) or "empty model output")
            data = {"items": merged}

        out = []
        for it in (data.get("items") or []):
            try:
                i = int(it.get("id", -1))
            except (TypeError, ValueError):
                continue
            if not (0 <= i < len(batch)):
                continue
            v = (it.get("verdict") or "").lower()
            if v not in ("positive", "neutral", "mixed", "negative"):
                continue
            rh = batch[i][0]
            out.append({"response_hash": rh, "report_id": report_id, "subject": subject,
                        "verdict": v, "themes": [str(t)[:40] for t in (it.get("themes") or [])][:3],
                        "evidence": (it.get("evidence") or "")[:600],
                        "one_liner": (it.get("one_liner") or "")[:200],
                        "llm_model": CLASSIFY_MODEL})
        # dedupe within batch (a model can echo an id twice)
        seen, uniq = set(), []
        for o in out:
            if o["response_hash"] in seen:
                continue
            seen.add(o["response_hash"])
            uniq.append(o)
        return uniq, None

    with ThreadPoolExecutor(max_workers=CLASSIFY_WORKERS) as ex:
        futs = {ex.submit(run, b): b for b in batches}
        for fut in as_completed(futs):
            if job_id and job_cancelled(job_id):
                break
            out, err = fut.result()
            if out:
                _save_verdicts(out)
                counts.update(o["verdict"] for o in out)
            done += len(futs[fut])
            if job_id:
                job_update(job_id, done=done,
                           stage=f"classified {done:,}/{len(todo):,} mentions of {subject}",
                           **({"log": f"batch error: {err}"} if err else {}))

    return {"absent": len(absent), "classified": done, "counts": dict(counts)}


def _save_verdicts(rows: list[dict]):
    from sqlalchemy.dialects.postgresql import insert
    if not rows:
        return
    stmt = insert(BasVerdict).values(rows).on_conflict_do_nothing(
        index_elements=["response_hash", "subject"])
    with session_scope() as s:
        s.execute(stmt)


# -------------------------------------------------------------------- coverage
def coverage(report_id: str, subject: str) -> dict:
    """Per (dataset, platform): rows stored, rows judged for this subject."""
    with session_scope() as s:
        stored = s.execute(
            select(BasResponse.dataset, BasResponse.platform, func.count())
            .where(BasResponse.report_id == report_id)
            .group_by(BasResponse.dataset, BasResponse.platform)).all()
        judged = s.execute(
            select(BasResponse.dataset, BasResponse.platform, func.count())
            .join(BasVerdict, BasVerdict.response_hash == BasResponse.response_hash)
            .where(BasResponse.report_id == report_id, BasVerdict.subject == subject)
            .group_by(BasResponse.dataset, BasResponse.platform)).all()
        fetches = s.execute(select(BasFetch).where(BasFetch.report_id == report_id)).scalars().all()
        fmeta = {(f.dataset, f.platform): {"fetched_at": f.fetched_at.isoformat(),
                                           "window_months": f.window_months,
                                           "months": f.months or [],
                                           "countries": f.countries or [],
                                           "n_seen": f.n_seen}
                 for f in fetches}
    st = {(d, p): n for d, p, n in stored}
    jd = {(d, p): n for d, p, n in judged}
    out = {}
    for ds in DATASETS:
        out[ds] = {}
        for pl in PLATFORMS:
            out[ds][pl] = {"stored": st.get((ds, pl), 0),
                           "judged": jd.get((ds, pl), 0),
                           **(fmeta.get((ds, pl)) or {})}
    return out


def available_filters(report_id: str) -> dict:
    with session_scope() as s:
        countries = [c for (c,) in s.execute(
            select(BasResponse.country).where(BasResponse.report_id == report_id)
            .group_by(BasResponse.country).order_by(func.count().desc())).all() if c]
        months = [m for (m,) in s.execute(
            select(BasResponse.snapshot_month).where(BasResponse.report_id == report_id)
            .group_by(BasResponse.snapshot_month).order_by(BasResponse.snapshot_month)).all() if m]
    return {"countries": countries, "months": months}


# ------------------------------------------------------------------ aggregation
def build_view(report_id: str, subject: str, platforms: list[str], datasets: list[str],
               countries: list[str], months: list[str], min_responses: int = 1,
               sort: str = "negative_count", top_prompts: int = 200) -> dict:
    """Everything the page renders, computed from stored rows under the filters."""
    with session_scope() as s:
        q = (select(BasResponse.prompt_hash, BasResponse.question,
                    BasResponse.search_volume, BasResponse.snapshot_month,
                    BasResponse.platform, BasVerdict.verdict)
             .join(BasVerdict, (BasVerdict.response_hash == BasResponse.response_hash) &
                   (BasVerdict.subject == subject))
             .where(BasResponse.report_id == report_id,
                    BasResponse.platform.in_(platforms or PLATFORMS),
                    BasResponse.dataset.in_(datasets or ["custom"])))
        if countries:
            q = q.where(BasResponse.country.in_(countries))
        if months:
            q = q.where(BasResponse.snapshot_month.in_(months))
        rows = s.execute(q).all()

        bounds = (select(func.min(BasResponse.snapshot_date), func.max(BasResponse.snapshot_date),
                         func.count())
                  .join(BasVerdict, (BasVerdict.response_hash == BasResponse.response_hash) &
                        (BasVerdict.subject == subject))
                  .where(BasResponse.report_id == report_id,
                         BasResponse.platform.in_(platforms or PLATFORMS),
                         BasResponse.dataset.in_(datasets or ["custom"])))
        if countries:
            bounds = bounds.where(BasResponse.country.in_(countries))
        if months:
            bounds = bounds.where(BasResponse.snapshot_month.in_(months))
        dmin, dmax, n_total = s.execute(bounds).one()

        # unjudged rows under the same filters => coverage warning on the page
        judged_sub = select(BasVerdict.response_hash).where(
            BasVerdict.report_id == report_id, BasVerdict.subject == subject)
        uq = select(func.count()).select_from(BasResponse).where(
            BasResponse.report_id == report_id,
            BasResponse.platform.in_(platforms or PLATFORMS),
            BasResponse.dataset.in_(datasets or ["custom"]),
            BasResponse.response_hash.not_in(judged_sub))
        if countries:
            uq = uq.where(BasResponse.country.in_(countries))
        if months:
            uq = uq.where(BasResponse.snapshot_month.in_(months))
        n_unjudged = s.scalar(uq) or 0

    totals: Counter = Counter()
    per_prompt: dict[str, dict] = {}
    per_month: dict[str, Counter] = defaultdict(Counter)
    per_platform: dict[str, Counter] = defaultdict(Counter)
    themes: Counter = Counter()

    for ph, question, vol, month, platform, verdict in rows:
        totals[verdict] += 1
        p = per_prompt.setdefault(ph, {"prompt_hash": ph, "question": question,
                                       "search_volume": vol or 0, "counts": Counter(),
                                       "months": Counter(), "n": 0})
        p["counts"][verdict] += 1
        p["months"][month] += 1
        p["n"] += 1
        p["search_volume"] = max(p["search_volume"], vol or 0)
        per_month[month][verdict] += 1
        per_platform[platform][verdict] += 1

    n = sum(totals.values())
    mentioned = n - totals.get("absent", 0)

    def pct(x, d):
        return round(100.0 * x / d, 1) if d else 0.0

    kpis = {
        "total": n,
        "mentioned": mentioned,
        "not_mentioned": totals.get("absent", 0),
        "positive": totals.get("positive", 0),
        "neutral": totals.get("neutral", 0),
        "mixed": totals.get("mixed", 0),
        "negative": totals.get("negative", 0),
        "pct_mentioned": pct(mentioned, n),
        "pct_positive": pct(totals.get("positive", 0), n),
        "pct_negative": pct(totals.get("negative", 0), n),
    }

    prompts = []
    for p in per_prompt.values():
        c = p["counts"]
        intensity = 2 * c.get("negative", 0) + c.get("mixed", 0)
        seg = []
        for k in BAR_ORDER:
            if c.get(k):
                seg.append({"k": k, "n": c[k], "pct": pct(c[k], p["n"])})
        prompts.append({
            "prompt_hash": p["prompt_hash"], "question": p["question"],
            "search_volume": p["search_volume"], "n": p["n"],
            "counts": dict(c), "intensity": intensity,
            "pct_negative": pct(c.get("negative", 0), p["n"]),
            "pct_critical": pct(c.get("negative", 0) + c.get("mixed", 0), p["n"]),
            "segments": seg,
            "months": sorted(p["months"]),
        })

    # Ascending ("least …") sorts must include prompts with zero criticism —
    # otherwise the cleanest prompts, which are the actual answer to "least
    # negative", would be filtered out before sorting.
    ascending = sort in ASCENDING_SORTS
    eligible = [p for p in prompts if p["n"] >= min_responses]
    critical = eligible if ascending else [p for p in eligible if p["intensity"] > 0]
    praised = [p for p in eligible if p["counts"].get("positive", 0) > 0]
    keys = {
        # most critical first
        "criticism":        lambda p: (-p["intensity"], -p["n"]),
        "pct_negative":     lambda p: (-p["pct_negative"], -p["n"]),
        "negative_count":   lambda p: (-p["counts"].get("negative", 0), -p["n"]),
        # least critical first (clean prompts included)
        "criticism_asc":      lambda p: (p["intensity"], -p["n"]),
        "pct_negative_asc":   lambda p: (p["pct_negative"], -p["n"]),
        "negative_count_asc": lambda p: (p["counts"].get("negative", 0), -p["n"]),
        # neutral orderings
        "search_volume":    lambda p: (-p["search_volume"], -p["intensity"]),
        "responses":        lambda p: (-p["n"], -p["intensity"]),
    }
    critical.sort(key=keys.get(sort, keys["negative_count"]))
    praised.sort(key=lambda p: (-p["counts"].get("positive", 0), -p["n"]))

    trend = []
    for m in sorted(per_month):
        c = per_month[m]
        tot = sum(c.values())
        men = tot - c.get("absent", 0)
        trend.append({
            "month": m, "total": tot, "mentioned": men,
            "pct_mentioned": pct(men, tot),
            "pct_positive": pct(c.get("positive", 0), tot),
            "pct_neutral": pct(c.get("neutral", 0), tot),
            "pct_mixed": pct(c.get("mixed", 0), tot),
            "pct_negative": pct(c.get("negative", 0), tot),
            "pct_critical": pct(c.get("negative", 0) + c.get("mixed", 0), tot),
            "counts": dict(c),
        })

    movers = []
    if len(trend) >= 2:
        last, prev = trend[-1], trend[-2]
        movers = {"month": last["month"], "prev_month": prev["month"],
                  "d_critical": round(last["pct_critical"] - prev["pct_critical"], 1),
                  "d_positive": round(last["pct_positive"] - prev["pct_positive"], 1),
                  "d_mentioned": round(last["pct_mentioned"] - prev["pct_mentioned"], 1)}

    # per-prompt month-over-month criticism deltas (the "getting worse" list)
    prompt_movers = []
    if len(trend) >= 2:
        cur_m, prev_m = trend[-1]["month"], trend[-2]["month"]
        agg: dict[str, dict] = {}
        for ph, question, vol, month, platform, verdict in rows:
            if month not in (cur_m, prev_m):
                continue
            a = agg.setdefault(ph, {"question": question, cur_m: Counter(), prev_m: Counter()})
            a[month][verdict] += 1
        for ph, a in agg.items():
            cn, pn = sum(a[cur_m].values()), sum(a[prev_m].values())
            if cn < 2 or pn < 2:
                continue
            cc = pct(a[cur_m].get("negative", 0) + a[cur_m].get("mixed", 0), cn)
            pp = pct(a[prev_m].get("negative", 0) + a[prev_m].get("mixed", 0), pn)
            if abs(cc - pp) < 5:
                continue
            prompt_movers.append({"question": a["question"], "prompt_hash": ph,
                                  "now": cc, "before": pp, "delta": round(cc - pp, 1),
                                  "n_now": cn, "n_before": pn})
        prompt_movers.sort(key=lambda x: -x["delta"])

    platform_split = []
    for pl, c in per_platform.items():
        tot = sum(c.values())
        platform_split.append({
            "platform": pl, "label": PLATFORM_LABELS.get(pl, pl), "total": tot,
            "pct_mentioned": pct(tot - c.get("absent", 0), tot),
            "pct_positive": pct(c.get("positive", 0), tot),
            "pct_critical": pct(c.get("negative", 0) + c.get("mixed", 0), tot),
            "counts": dict(c)})
    platform_split.sort(key=lambda x: -x["total"])

    return {
        "kpis": kpis,
        "n_prompts_total": len(prompts),
        "n_prompts_critical": len(critical),
        "sort": sort,
        "sort_ascending": ascending,
        "sort_label": SORT_LABELS.get(sort, SORT_LABELS["criticism"]),
        "prompts_critical": critical[:top_prompts],
        "prompts_praised": praised[:top_prompts],
        "trend": trend,
        "movers": movers,
        "prompt_movers": prompt_movers[:12],
        "platform_split": platform_split,
        "window": {"start": dmin or "", "end": dmax or ""},
        "per_prompt_avg": round(n / len(prompts), 1) if prompts else 0.0,
        "n_unjudged": n_unjudged,
        "themes": themes.most_common(12),
    }


# ------------------------------------------------------------------ browse tab
BROWSE_SORTS = {
    "date_desc":   (BasResponse.snapshot_date, True),
    "date_asc":    (BasResponse.snapshot_date, False),
    "volume_desc": (BasResponse.search_volume, True),
    "volume_asc":  (BasResponse.search_volume, False),
    "prompt":      (BasResponse.question, False),
    "platform":    (BasResponse.platform, False),
}


def _plain_preview(md: str, n: int) -> str:
    """Flatten markdown to a one-line preview for the Browse table cell.

    The cell is too small for rendered markdown, and raw syntax (###, **, |---)
    reads as noise, so strip the markup rather than showing either.
    """
    t = md or ""
    t = re.sub(r"\[\s*\]\((?:#[a-z]+\d+)?\)", "", t, flags=re.I)
    t = re.sub(r"\[([^\]\n]{0,200})\]\((?:#[a-z]+\d+|[^)\n]*)\)", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\|.*$", "", t, flags=re.M)
    t = re.sub(r"[*_`>#]+", "", t)
    t = re.sub(r"\\([$#*_~`\[\](){}.\-+!>|])", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n]


def browse_responses(report_id: str, subject: str, *, platforms: list[str],
                     datasets: list[str], countries: list[str], months: list[str],
                     verdicts: list[str], mention: str = "any", search: str = "",
                     sort: str = "date_desc", limit: int = 50, offset: int = 0) -> dict:
    """One row per fetched AI answer, paginated server-side.

    ``verdicts``  filters on the sentiment label (positive/neutral/mixed/negative/absent).
    ``mention``   'any' | 'mentioned' | 'absent' — a coarser cut of the same column,
                  kept separate so the two filters can be combined naturally.
    Rows still awaiting classification surface with verdict ``unjudged`` (LEFT JOIN)
    instead of vanishing, so the table's total always matches what was fetched.
    """
    verdict_col = func.coalesce(BasVerdict.verdict, "unjudged")

    def base(cols):
        q = (select(*cols)
             .select_from(BasResponse)
             .join(BasVerdict, (BasVerdict.response_hash == BasResponse.response_hash) &
                   (BasVerdict.subject == subject), isouter=True)
             .where(BasResponse.report_id == report_id,
                    BasResponse.platform.in_(platforms or PLATFORMS),
                    BasResponse.dataset.in_(datasets or ["custom"])))
        if countries:
            q = q.where(BasResponse.country.in_(countries))
        if months:
            q = q.where(BasResponse.snapshot_month.in_(months))
        if verdicts:
            q = q.where(verdict_col.in_(verdicts))
        if mention == "mentioned":
            q = q.where(verdict_col.not_in(["absent", "unjudged"]))
        elif mention == "absent":
            q = q.where(verdict_col == "absent")
        if search:
            like = f"%{search}%"
            q = q.where(BasResponse.question.ilike(like) |
                        BasResponse.response.ilike(like))
        return q

    col, desc = BROWSE_SORTS.get(sort, BROWSE_SORTS["date_desc"])
    order = col.desc() if desc else col.asc()

    with session_scope() as s:
        total = s.scalar(base([func.count()])) or 0
        facets = dict(s.execute(base([verdict_col, func.count()])
                                .group_by(verdict_col)).all())
        rows = s.execute(
            base([BasResponse.response_hash, BasResponse.prompt_hash,
                  BasResponse.question, BasResponse.response, BasResponse.platform,
                  BasResponse.country, BasResponse.snapshot_date,
                  BasResponse.search_volume, BasResponse.sitelinks,
                  verdict_col.label("verdict"), BasVerdict.themes,
                  BasVerdict.evidence, BasVerdict.one_liner])
            .order_by(order, BasResponse.response_hash)
            .limit(min(limit, 200)).offset(offset)).all()

    out = []
    for r in rows:
        answer = r.response or ""
        out.append({
            "response_hash": r.response_hash, "prompt_hash": r.prompt_hash,
            "question": r.question, "platform": r.platform,
            "platform_label": PLATFORM_LABELS.get(r.platform, r.platform),
            "country": r.country, "snapshot_date": r.snapshot_date,
            "search_volume": r.search_volume or 0,
            "verdict": r.verdict,
            "mentioned": r.verdict not in ("absent", "unjudged"),
            "themes": r.themes or [], "evidence": r.evidence or "",
            "one_liner": r.one_liner or "",
            "answer_preview": _plain_preview(answer, 280),
            "answer_chars": len(answer),
            "n_links": len(r.sitelinks or []),
        })
    return {"rows": out, "total": total, "offset": offset, "limit": limit,
            "facets": facets, "sort": sort}


def response_detail(report_id: str, subject: str, response_hash: str) -> dict:
    """Full answer + citations for one row of the browse table."""
    with session_scope() as s:
        row = s.execute(
            select(BasResponse, BasVerdict)
            .join(BasVerdict, (BasVerdict.response_hash == BasResponse.response_hash) &
                  (BasVerdict.subject == subject), isouter=True)
            .where(BasResponse.report_id == report_id,
                   BasResponse.response_hash == response_hash)).first()
    if not row:
        return {}
    r, v = row
    return {
        "response_hash": r.response_hash, "question": r.question,
        "response": r.response, "platform": r.platform,
        "platform_label": PLATFORM_LABELS.get(r.platform, r.platform),
        "country": r.country, "snapshot_date": r.snapshot_date,
        "search_volume": r.search_volume or 0, "sitelinks": r.sitelinks or [],
        "brands_hit": r.brands_hit or [], "search_queries": r.search_queries or [],
        "dataset": r.dataset,
        "verdict": (v.verdict if v else "unjudged"),
        "themes": (v.themes if v else []) or [],
        "evidence": (v.evidence if v else "") or "",
        "one_liner": (v.one_liner if v else "") or "",
    }


def prompt_detail(report_id: str, subject: str, prompt_hash: str, platforms: list[str],
                  datasets: list[str], countries: list[str], months: list[str],
                  verdicts: list[str] | None = None, mention: str = "any",
                  sort: str = "date_desc") -> dict:
    """Every dated response for one prompt — the drill-down modal.

    Takes the same in-modal filters as the Browse tab (`verdicts`, `mention`,
    `sort`) on top of the page-level scope. Facet counts are computed BEFORE the
    modal filters are applied, so the checkboxes always show what's available in
    this prompt rather than only what's currently shown. Unjudged rows surface as
    verdict 'unjudged' (LEFT JOIN) instead of disappearing.
    """
    verdict_col = func.coalesce(BasVerdict.verdict, "unjudged")

    def base():
        q = (select(BasResponse, BasVerdict, verdict_col.label("v"))
             .select_from(BasResponse)
             .join(BasVerdict, (BasVerdict.response_hash == BasResponse.response_hash) &
                   (BasVerdict.subject == subject), isouter=True)
             .where(BasResponse.report_id == report_id,
                    BasResponse.prompt_hash == prompt_hash,
                    BasResponse.platform.in_(platforms or PLATFORMS),
                    BasResponse.dataset.in_(datasets or ["custom"])))
        if countries:
            q = q.where(BasResponse.country.in_(countries))
        if months:
            q = q.where(BasResponse.snapshot_month.in_(months))
        return q

    order = {
        "date_desc":   BasResponse.snapshot_date.desc(),
        "date_asc":    BasResponse.snapshot_date.asc(),
        "platform":    BasResponse.platform.asc(),
        "volume_desc": BasResponse.search_volume.desc(),
        "volume_asc":  BasResponse.search_volume.asc(),
    }.get(sort, BasResponse.snapshot_date.desc())

    with session_scope() as s:
        all_rows = s.execute(base().order_by(order, BasResponse.response_hash)).all()

    facets: Counter = Counter()
    platform_facets: Counter = Counter()
    for r, _v, vlabel in all_rows:
        facets[vlabel] += 1
        platform_facets[r.platform] += 1

    keep = []
    for r, v, vlabel in all_rows:
        if verdicts and vlabel not in verdicts:
            continue
        if mention == "mentioned" and vlabel in ("absent", "unjudged"):
            continue
        if mention == "absent" and vlabel != "absent":
            continue
        keep.append((r, v, vlabel))

    out = []
    for r, v, vlabel in keep:
        out.append({
            "response_hash": r.response_hash, "question": r.question,
            "snapshot_date": r.snapshot_date, "platform": r.platform,
            "platform_label": PLATFORM_LABELS.get(r.platform, r.platform),
            "country": r.country, "verdict": vlabel,
            "mentioned": vlabel not in ("absent", "unjudged"),
            "themes": (v.themes if v else []) or [],
            "evidence": (v.evidence if v else "") or "",
            "one_liner": (v.one_liner if v else "") or "",
            "response": r.response, "sitelinks": r.sitelinks or [],
            "brands_hit": r.brands_hit or [], "search_volume": r.search_volume,
            "search_queries": r.search_queries or [],
        })
    question = all_rows[0][0].question if all_rows else ""
    return {"question": question, "responses": out,
            "total": len(all_rows), "shown": len(out),
            "facets": dict(facets),
            "platform_facets": {p: n for p, n in platform_facets.items()},
            "sort": sort}
