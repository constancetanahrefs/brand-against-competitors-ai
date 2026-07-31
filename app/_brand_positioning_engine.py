"""Engine for the Brand Positioning report — "What <brand> is better for".

Reuses the raw answers already stored by the sentiment report (`bas_response`),
so there is NO second Brand Radar fetch. Two LLM stages:

  STAGE 1 — extract  (per answer, cacheable)
      For each answer that mentions a brand, pull out the explicit positioning
      claims: "Ahrefs is better for backlink analysis", "Semrush is better for
      PPC". Verbatim evidence required. Absence of a claim is a valid result and
      is recorded so the answer is never re-sent.

  STAGE 2 — cluster  (per brand, over all its claims)
      Group the free-text claims into semantic categories and name each group.
      Done with embeddings + agglomerative merge, then one LLM call to LABEL the
      groups — not one giant LLM call to both group and label, which doesn't fit
      in context and isn't reproducible.

Why two stages: extraction is the expensive part and is per-answer, so it caches
perfectly. Clustering needs global view of all claims and is cheap, so it can be
re-run freely (e.g. after a new month of data) without re-extracting.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import datetime as _dt
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select, func, delete
from sqlalchemy.dialects.postgresql import insert

from src.db import session_scope
from src.llm import console_openai_client

from ._brand_answer_sentiment_models import BasResponse, BasVerdict
from ._brand_answer_sentiment_engine import (
    PLATFORMS, PLATFORM_LABELS, DATASETS, load_report, entities_of, _alias_rx,
)
from ._brand_positioning_models import BpClaim, BpCategory, BpJob

APP_SLUG = "reports:brand_positioning"
LLM = console_openai_client(app_slug=APP_SLUG)
EXTRACT_MODEL = "google/gemini-3-flash-preview"
LABEL_MODEL = "anthropic/claude-sonnet-4.6"
EMBED_MODEL = "text-embedding-3-small"

EXTRACT_BATCH = 8          # answers per LLM call (each carries a long excerpt)
EXTRACT_WORKERS = 12
EMBED_BATCH = 256
SNIPPET_WINDOW = 420       # chars around a mention — positioning needs context
MAX_SNIPPETS = 3
SIM_THRESHOLD = 0.48       # cosine cutoff for merging claim phrases
LABEL_MERGE_SIM = 0.78     # cosine cutoff for merging clusters whose LABELS mean the same
MIN_CATEGORY = 2           # claims needed before a cluster gets its own category
MAX_LABEL_CLUSTERS = 40    # only the biggest clusters get an LLM label


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# ---------------------------------------------------------------------- jobs
def job_update(job_id: str, **kw):
    log_line = kw.pop("log", None)
    with session_scope() as s:
        j = s.scalar(select(BpJob).where(BpJob.job_id == job_id))
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
        return bool(s.scalar(select(BpJob.cancel).where(BpJob.job_id == job_id)))


# ------------------------------------------------------------------ stage 1
EXTRACT_RUBRIC = """You extract COMPARATIVE POSITIONING claims about one brand from an AI assistant's answer.

A positioning claim answers: "what is this brand BETTER FOR / WORSE FOR — which use case, buyer, or job?"

Return one item per claim you find. For each:
- "strength": the use case in 2-6 words, lowercase, no brand name. Normalise wording:
  "great for agencies managing many clients" -> "agency client management"
  "best if you care about backlinks" -> "backlink analysis"
  "too expensive for solo users" -> "affordability for solo users"
- "polarity": "strength" if the brand is presented as better/recommended FOR that thing,
  "weakness" if presented as worse/unsuitable/not-chosen FOR that thing.
- "evidence": VERBATIM span from the excerpt (<=200 chars) that states it.
- "versus": array of other brand names the answer explicitly contrasts it with for this
  claim. Empty array if none named.

Rules:
- ONLY claims about the target brand. Ignore what the answer says about others.
- A bare mention in a list with no stated use case is NOT a claim. Return no item for it.
- Pricing, ease of use, data depth, integrations, team size, industry fit all count as use cases.
- Do NOT invent. If the answer states no use case for the brand, return an empty items array for that id.
- Max 4 claims per answer; pick the most explicit.

Return STRICT JSON: {"results":[{"id":<int>,"items":[{"strength":"...","polarity":"strength|weakness","evidence":"...","versus":["..."]}]}]}
One entry per input id, including ids with an empty items array."""


def _snippets(text: str, rx) -> list[str]:
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
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0].strip()
        if s.startswith("json"):
            s = s[4:].strip()
    dec = json.JSONDecoder()
    results, idx, first = [], 0, None
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
            results.extend(obj.get("results") or [])
    if results:
        return {"results": results}
    if isinstance(first, dict):
        return first
    raise ValueError("no JSON object in model output")


def pending_extract(report_id: str, brand: str, platforms: list[str],
                    datasets: list[str]) -> int:
    """Answers that mention this brand but have no extraction attempt recorded."""
    with session_scope() as s:
        done = select(BpClaim.response_hash).where(BpClaim.report_id == report_id,
                                                   BpClaim.brand == brand)
        q = (select(func.count()).select_from(BasResponse)
             .where(BasResponse.report_id == report_id,
                    BasResponse.platform.in_(platforms or PLATFORMS),
                    BasResponse.dataset.in_(datasets or ["custom"]),
                    BasResponse.brands_hit.contains([brand]),
                    BasResponse.response_hash.not_in(done)))
        return s.scalar(q) or 0


def extract_claims(report_id: str, brand: str, keywords: list[str],
                   platforms: list[str], datasets: list[str],
                   job_id: str | None = None, cap: int = 100_000) -> dict:
    """Stage 1: pull positioning claims for one brand out of the stored answers.

    Only answers whose `brands_hit` includes this brand are considered — that
    column was computed at fetch time for every entity, so this costs nothing.
    Answers yielding no claim get a sentinel row so they're never re-sent.
    """
    rx = _alias_rx(keywords or [brand])
    with session_scope() as s:
        done = select(BpClaim.response_hash).where(BpClaim.report_id == report_id,
                                                   BpClaim.brand == brand)
        rows = s.execute(
            select(BasResponse.response_hash, BasResponse.question, BasResponse.response,
                   BasResponse.platform, BasResponse.snapshot_month,
                   BasResponse.country, BasResponse.search_volume)
            .where(BasResponse.report_id == report_id,
                   BasResponse.platform.in_(platforms or PLATFORMS),
                   BasResponse.dataset.in_(datasets or ["custom"]),
                   BasResponse.brands_hit.contains([brand]),
                   BasResponse.response_hash.not_in(done))
            .limit(cap)).all()

    todo = []
    for rh, q, text, platform, month, country, vol in rows:
        snips = _snippets(text or "", rx) if rx else []
        if not snips:
            continue
        todo.append({"rh": rh, "q": q, "snips": snips, "platform": platform,
                     "month": month, "country": country, "vol": vol or 0})

    if job_id:
        job_update(job_id, total=len(todo), done=0,
                   log=f"{brand}: {len(rows):,} answers mention it, {len(todo):,} to read")

    batches = [todo[i:i + EXTRACT_BATCH] for i in range(0, len(todo), EXTRACT_BATCH)]
    n_claims = 0
    done_n = 0

    def run(batch):
        items = [{"id": i, "prompt": b["q"][:200],
                  "excerpt": "\n…\n".join(b["snips"])[:3200]}
                 for i, b in enumerate(batch)]
        try:
            resp = LLM.chat.completions.create(
                model=EXTRACT_MODEL,
                messages=[{"role": "system", "content": EXTRACT_RUBRIC},
                          {"role": "user", "content": json.dumps(
                              {"brand": brand, "answers": items})[:140_000]}],
                response_format={"type": "json_object"}, temperature=0)
            data = _parse_json(resp.choices[0].message.content)
        except Exception as e:  # noqa: BLE001
            return [], str(e)[:160]

        by_id = {}
        for r in (data.get("results") or []):
            try:
                by_id[int(r.get("id", -1))] = r.get("items") or []
            except (TypeError, ValueError):
                continue

        out = []
        for i, b in enumerate(batch):
            if i not in by_id:
                continue      # not returned -> leave unextracted, retried next run
            claims = by_id[i][:4]
            if not claims:
                # sentinel: answer read, no positioning claim found
                out.append({"response_hash": b["rh"], "report_id": report_id,
                            "brand": brand, "claim_idx": 0, "strength": "",
                            "evidence": "", "polarity": "none", "versus": [],
                            "platform": b["platform"], "snapshot_month": b["month"],
                            "country": b["country"], "search_volume": b["vol"],
                            "llm_model": EXTRACT_MODEL})
                continue
            for j, c in enumerate(claims):
                strength = re.sub(r"\s+", " ", str(c.get("strength") or "")).strip().lower()[:120]
                if not strength:
                    continue
                pol = (c.get("polarity") or "strength").lower()
                if pol not in ("strength", "weakness"):
                    pol = "strength"
                out.append({"response_hash": b["rh"], "report_id": report_id,
                            "brand": brand, "claim_idx": j, "strength": strength,
                            "evidence": (c.get("evidence") or "")[:400],
                            "polarity": pol,
                            "versus": [str(v)[:80] for v in (c.get("versus") or [])][:6],
                            "platform": b["platform"], "snapshot_month": b["month"],
                            "country": b["country"], "search_volume": b["vol"],
                            "llm_model": EXTRACT_MODEL})
        return out, None

    with ThreadPoolExecutor(max_workers=EXTRACT_WORKERS) as ex:
        futs = {ex.submit(run, b): b for b in batches}
        for fut in as_completed(futs):
            if job_id and job_cancelled(job_id):
                break
            out, err = fut.result()
            if out:
                _save_claims(out)
                n_claims += sum(1 for o in out if o["polarity"] != "none")
            done_n += len(futs[fut])
            if job_id:
                job_update(job_id, done=done_n,
                           stage=f"{brand}: read {done_n:,}/{len(todo):,} answers, {n_claims:,} claims",
                           **({"log": f"batch error: {err}"} if err else {}))
    return {"answers": len(todo), "claims": n_claims}


def _save_claims(rows: list[dict]):
    if not rows:
        return
    stmt = insert(BpClaim).values(rows).on_conflict_do_nothing(
        index_elements=["response_hash", "brand", "claim_idx"])
    with session_scope() as s:
        s.execute(stmt)


# ------------------------------------------------------------------ stage 2
LABEL_RUBRIC = """You name groups of positioning claims about a brand.

Each group is a list of short phrases that were clustered by semantic similarity —
they should describe the same underlying use case, buyer or job.

For each group return:
- "label": a clean 2-5 word category name in Title Case, describing the use case
  (e.g. "Backlink Analysis", "Agency Client Reporting", "Budget & Small Teams",
  "Enterprise Scale", "Ease Of Use"). No brand names.
- "summary": one sentence (<=140 chars) stating what the AI says the brand is
  better/worse for in this category.
- "outlier_indices": indices of phrases in the group that clearly do NOT belong.

Return STRICT JSON: {"groups":[{"id":<int>,"label":"...","summary":"...","outlier_indices":[]}]}
One entry per input group id."""


def _embed(texts: list[str]) -> list[list[float]]:
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        chunk = texts[i:i + EMBED_BATCH]
        resp = LLM.embeddings.create(model=EMBED_MODEL, input=chunk)
        out.extend([d.embedding for d in resp.data])
    return out


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _cluster(vectors: list[list[float]], threshold: float) -> list[int]:
    """Greedy centroid clustering — deterministic, no sklearn dependency.

    Claims arrive sorted by frequency, so the most common phrasing seeds each
    cluster and rarer variants attach to it. That makes the grouping stable
    across re-runs, which matters because categories are user-facing.
    """
    centroids: list[list[float]] = []
    counts: list[int] = []
    assign: list[int] = []
    for v in vectors:
        best, best_sim = -1, -1.0
        for ci, c in enumerate(centroids):
            sim = _cosine(v, c)
            if sim > best_sim:
                best, best_sim = ci, sim
        if best >= 0 and best_sim >= threshold:
            n = counts[best]
            centroids[best] = [(c * n + x) / (n + 1) for c, x in zip(centroids[best], v)]
            counts[best] += 1
            assign.append(best)
        else:
            centroids.append(list(v))
            counts.append(1)
            assign.append(len(centroids) - 1)
    return assign


def cluster_brand(report_id: str, brand: str, is_own: bool, polarity: str = "strength",
                  job_id: str | None = None, threshold: float = SIM_THRESHOLD) -> dict:
    """Stage 2: group this brand's claims into named categories."""
    with session_scope() as s:
        rows = s.execute(
            select(BpClaim.id, BpClaim.strength, BpClaim.response_hash, BpClaim.evidence,
                   BpClaim.platform, BpClaim.snapshot_month, BpClaim.versus)
            .where(BpClaim.report_id == report_id, BpClaim.brand == brand,
                   BpClaim.polarity == polarity)).all()
    if not rows:
        return {"categories": 0, "claims": 0}

    # dedupe identical phrasings before embedding — big cost saver
    by_phrase: dict[str, list] = defaultdict(list)
    for r in rows:
        by_phrase[r.strength].append(r)
    phrases = sorted(by_phrase, key=lambda p: -len(by_phrase[p]))

    if job_id:
        job_update(job_id, log=f"{brand}/{polarity}: {len(rows):,} claims, "
                               f"{len(phrases):,} distinct phrases — embedding")
    vectors = _embed(phrases)
    assign = _cluster(vectors, threshold)

    groups: dict[int, list[str]] = defaultdict(list)
    for phrase, gi in zip(phrases, assign):
        groups[gi].append(phrase)

    # rank clusters by number of underlying claims, label the biggest
    ranked = sorted(groups.items(),
                    key=lambda kv: -sum(len(by_phrase[p]) for p in kv[1]))
    labelled = ranked[:MAX_LABEL_CLUSTERS]

    if job_id:
        job_update(job_id, log=f"{brand}/{polarity}: {len(ranked)} clusters — labelling top {len(labelled)}")

    labels = _label_groups([g for _k, g in labelled], brand, polarity)

    # ---- consolidate near-synonym categories -------------------------------
    # Phrase-level clustering reliably splits things a reader would consider one
    # category ("Entry-Level Pricing" vs "Startup Affordability" vs "Budget
    # Monitoring"). Embed the LABELS and merge the ones that mean the same thing,
    # keeping the label of the biggest group.
    labelled, labels = _merge_by_label(labelled, labels, by_phrase)

    with session_scope() as s:
        s.execute(delete(BpCategory).where(BpCategory.report_id == report_id,
                                           BpCategory.brand == brand,
                                           BpCategory.polarity == polarity))
    total_claims = len(rows)
    made = 0
    cat_rows = []
    claim_updates: list[tuple[int, str]] = []

    for gi, (_key, phrase_list) in enumerate(labelled):
        members = [r for p in phrase_list for r in by_phrase[p]]
        if len(members) < MIN_CATEGORY:
            continue
        meta = labels.get(gi) or {}
        label = (meta.get("label") or phrase_list[0].title())[:120]
        key = hashlib.sha256(f"{brand}|{polarity}|{label}".encode()).hexdigest()[:16]
        plat = Counter(m.platform for m in members if m.platform)
        mon = Counter(m.snapshot_month for m in members if m.snapshot_month)
        examples = []
        for m in members:
            if m.evidence and len(examples) < 4:
                examples.append({"evidence": m.evidence[:300],
                                 "response_hash": m.response_hash,
                                 "phrase": m.strength})
        cat_rows.append({
            "report_id": report_id, "brand": brand, "polarity": polarity,
            "category_key": key, "label": label,
            "summary": (meta.get("summary") or "")[:400],
            "n_claims": len(members),
            "n_responses": len({m.response_hash for m in members}),
            "share": round(100.0 * len(members) / total_claims, 1) if total_claims else 0.0,
            "platforms": dict(plat), "months": dict(sorted(mon.items())),
            "examples": examples, "is_own": is_own,
        })
        claim_updates.extend((m.id, key) for m in members)
        made += 1

    with session_scope() as s:
        if cat_rows:
            s.execute(insert(BpCategory).values(cat_rows).on_conflict_do_nothing(
                index_elements=["report_id", "brand", "polarity", "category_key"]))
        for cid, key in claim_updates:
            s.execute(BpClaim.__table__.update()
                      .where(BpClaim.id == cid).values(category_key=key))
    return {"categories": made, "claims": total_claims}


def _merge_by_label(labelled: list, labels: dict, by_phrase: dict) -> tuple[list, dict]:
    """Merge clusters whose LLM labels are semantically near-identical."""
    if len(labelled) < 2:
        return labelled, labels
    names = []
    for gi, (_k, phrases) in enumerate(labelled):
        meta = labels.get(gi) or {}
        names.append(meta.get("label") or (phrases[0] if phrases else f"group {gi}"))
    try:
        vecs = _embed(names)
    except Exception:  # noqa: BLE001 — if embedding fails, keep the fine-grained groups
        return labelled, labels

    size = [sum(len(by_phrase[p]) for p in phrases) for _k, phrases in labelled]
    order = sorted(range(len(labelled)), key=lambda i: -size[i])
    parent: dict[int, int] = {}
    for i in order:
        if i in parent:
            continue
        parent[i] = i
        for j in order:
            if j in parent or j == i:
                continue
            if _cosine(vecs[i], vecs[j]) >= LABEL_MERGE_SIM:
                parent[j] = i

    merged_phrases: dict[int, list[str]] = defaultdict(list)
    for i, (_k, phrases) in enumerate(labelled):
        merged_phrases[parent.get(i, i)].extend(phrases)

    out_groups, out_labels = [], {}
    for new_i, root in enumerate(sorted(merged_phrases,
                                        key=lambda r: -sum(len(by_phrase[p])
                                                           for p in merged_phrases[r]))):
        out_groups.append((root, merged_phrases[root]))
        out_labels[new_i] = labels.get(root) or {}
    return out_groups, out_labels


def _label_groups(groups: list[list[str]], brand: str, polarity: str) -> dict[int, dict]:
    """One LLM call per 12 clusters — labels only, grouping already decided."""
    out: dict[int, dict] = {}
    CHUNK = 12
    for start in range(0, len(groups), CHUNK):
        chunk = groups[start:start + CHUNK]
        payload = {"brand": brand, "polarity": polarity,
                   "groups": [{"id": start + i, "phrases": g[:25]}
                              for i, g in enumerate(chunk)]}
        try:
            resp = LLM.chat.completions.create(
                model=LABEL_MODEL,
                messages=[{"role": "system", "content": LABEL_RUBRIC},
                          {"role": "user", "content": json.dumps(payload)[:100_000]}],
                response_format={"type": "json_object"}, temperature=0)
            data = json.loads(_strip_fence(resp.choices[0].message.content))
            for g in (data.get("groups") or []):
                try:
                    out[int(g.get("id"))] = g
                except (TypeError, ValueError):
                    continue
        except Exception:  # noqa: BLE001 — fall back to the top phrase as label
            continue
    return out


def _strip_fence(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0].strip()
        if s.startswith("json"):
            s = s[4:].strip()
    return s


# ---------------------------------------------------------------- read side
def coverage(report_id: str) -> dict:
    """Per brand: answers mentioning it, answers read, claims, categories."""
    rep = load_report(report_id)
    if not rep:
        return {}
    ents = entities_of(rep)
    with session_scope() as s:
        mention_counts = {}
        for e in ents:
            mention_counts[e["name"]] = s.scalar(
                select(func.count()).select_from(BasResponse)
                .where(BasResponse.report_id == report_id,
                       BasResponse.brands_hit.contains([e["name"]]))) or 0
        read = dict(s.execute(
            select(BpClaim.brand, func.count(func.distinct(BpClaim.response_hash)))
            .where(BpClaim.report_id == report_id).group_by(BpClaim.brand)).all())
        claims = dict(s.execute(
            select(BpClaim.brand, func.count())
            .where(BpClaim.report_id == report_id, BpClaim.polarity != "none")
            .group_by(BpClaim.brand)).all())
        cats = dict(s.execute(
            select(BpCategory.brand, func.count())
            .where(BpCategory.report_id == report_id).group_by(BpCategory.brand)).all())
    out = []
    for e in ents:
        out.append({"brand": e["name"], "kind": e["kind"],
                    "mentions": mention_counts.get(e["name"], 0),
                    "read": read.get(e["name"], 0),
                    "claims": claims.get(e["name"], 0),
                    "categories": cats.get(e["name"], 0)})
    return {"brands": out, "report": rep}


def brand_view(report_id: str, brand: str, polarity: str = "strength",
               platforms: list[str] | None = None, months: list[str] | None = None,
               sort: str = "claims", min_share: float = 0.0) -> dict:
    """Categories for one brand, with optional platform/month filtering.

    Filtering re-aggregates from the CLAIM rows (not the stored category totals)
    so a filtered view shows real filtered numbers rather than global ones.
    """
    with session_scope() as s:
        cats = s.execute(
            select(BpCategory).where(BpCategory.report_id == report_id,
                                     BpCategory.brand == brand,
                                     BpCategory.polarity == polarity)).scalars().all()
        if not cats:
            return {"brand": brand, "polarity": polarity, "categories": [],
                    "total_claims": 0, "n_categories": 0, "filtered": False}

        q = (select(BpClaim.category_key, BpClaim.response_hash, BpClaim.platform,
                    BpClaim.snapshot_month, BpClaim.strength, BpClaim.evidence,
                    BpClaim.versus, BpClaim.search_volume)
             .where(BpClaim.report_id == report_id, BpClaim.brand == brand,
                    BpClaim.polarity == polarity,
                    BpClaim.category_key.is_not(None)))
        if platforms:
            q = q.where(BpClaim.platform.in_(platforms))
        if months:
            q = q.where(BpClaim.snapshot_month.in_(months))
        claim_rows = s.execute(q).all()

    agg: dict[str, dict] = defaultdict(lambda: {"n": 0, "responses": set(),
                                                "platforms": Counter(),
                                                "months": Counter(),
                                                "versus": Counter(),
                                                "examples": [], "volume": 0})
    for r in claim_rows:
        a = agg[r.category_key]
        a["n"] += 1
        a["responses"].add(r.response_hash)
        if r.platform:
            a["platforms"][r.platform] += 1
        if r.snapshot_month:
            a["months"][r.snapshot_month] += 1
        for v in (r.versus or []):
            a["versus"][v] += 1
        a["volume"] = max(a["volume"], r.search_volume or 0)
        if r.evidence and len(a["examples"]) < 4:
            a["examples"].append({"evidence": r.evidence[:300],
                                  "response_hash": r.response_hash,
                                  "phrase": r.strength})

    total = sum(a["n"] for a in agg.values())
    out = []
    for c in cats:
        a = agg.get(c.category_key)
        if not a or not a["n"]:
            continue
        out.append({
            "category_key": c.category_key, "label": c.label, "summary": c.summary,
            "n_claims": a["n"], "n_responses": len(a["responses"]),
            "share": round(100.0 * a["n"] / total, 1) if total else 0.0,
            "platforms": [{"platform": p, "label": PLATFORM_LABELS.get(p, p), "n": n}
                          for p, n in a["platforms"].most_common()],
            "months": dict(sorted(a["months"].items())),
            "versus": [{"brand": b, "n": n} for b, n in a["versus"].most_common(5)],
            "examples": a["examples"] or (c.examples or [])[:4],
            "search_volume": a["volume"],
        })
    keys = {
        "claims": lambda c: (-c["n_claims"], -c["n_responses"]),
        "responses": lambda c: (-c["n_responses"], -c["n_claims"]),
        "label": lambda c: c["label"].lower(),
    }
    out.sort(key=keys.get(sort, keys["claims"]))
    n_all = len(out)
    shown = [c for c in out if c["share"] >= min_share] if min_share else out
    # never return an empty list purely because the threshold was too high
    if min_share and not shown and out:
        shown = out[:5]
    hidden = n_all - len(shown)
    return {"brand": brand, "polarity": polarity, "categories": shown,
            "total_claims": total, "n_categories": len(shown),
            "n_categories_all": n_all, "n_hidden": hidden,
            "min_share": min_share,
            "hidden_claims": sum(c["n_claims"] for c in out if c not in shown),
            "filtered": bool(platforms or months)}


def compare_view(report_id: str, own_brand: str, polarity: str = "strength",
                 top: int = 12) -> dict:
    """Side-by-side: the categories each brand owns, for the compare sub-tab."""
    with session_scope() as s:
        cats = s.execute(
            select(BpCategory).where(BpCategory.report_id == report_id,
                                     BpCategory.polarity == polarity)).scalars().all()
    by_brand: dict[str, list] = defaultdict(list)
    for c in cats:
        by_brand[c.brand].append(c)
    rows = []
    for brand, lst in by_brand.items():
        lst.sort(key=lambda c: -c.n_claims)
        rows.append({"brand": brand, "is_own": brand == own_brand,
                     "total_claims": sum(c.n_claims for c in lst),
                     "categories": [{"label": c.label, "n_claims": c.n_claims,
                                     "share": c.share, "summary": c.summary}
                                    for c in lst[:top]]})
    rows.sort(key=lambda r: (not r["is_own"], -r["total_claims"]))
    return {"brands": rows, "polarity": polarity}


def available_filters(report_id: str) -> dict:
    with session_scope() as s:
        plats = [p for (p,) in s.execute(
            select(BpClaim.platform).where(BpClaim.report_id == report_id)
            .group_by(BpClaim.platform)).all() if p]
        months = [m for (m,) in s.execute(
            select(BpClaim.snapshot_month).where(BpClaim.report_id == report_id)
            .group_by(BpClaim.snapshot_month).order_by(BpClaim.snapshot_month)).all() if m]
    return {"platforms": plats, "months": months}


def category_claims(report_id: str, brand: str, polarity: str, category_key: str,
                    platforms: list[str] | None = None,
                    months: list[str] | None = None, limit: int = 100) -> dict:
    """Every claim in one category — the drill-down."""
    with session_scope() as s:
        cat = s.scalar(select(BpCategory).where(BpCategory.report_id == report_id,
                                                BpCategory.brand == brand,
                                                BpCategory.polarity == polarity,
                                                BpCategory.category_key == category_key))
        q = (select(BpClaim, BasResponse.question, BasResponse.snapshot_date)
             .join(BasResponse, BasResponse.response_hash == BpClaim.response_hash)
             .where(BpClaim.report_id == report_id, BpClaim.brand == brand,
                    BpClaim.polarity == polarity,
                    BpClaim.category_key == category_key))
        if platforms:
            q = q.where(BpClaim.platform.in_(platforms))
        if months:
            q = q.where(BpClaim.snapshot_month.in_(months))
        rows = s.execute(q.order_by(BasResponse.snapshot_date.desc()).limit(limit)).all()
    return {
        "label": cat.label if cat else "",
        "summary": cat.summary if cat else "",
        "claims": [{
            "phrase": c.strength, "evidence": c.evidence,
            "versus": c.versus or [], "platform": c.platform,
            "platform_label": PLATFORM_LABELS.get(c.platform, c.platform),
            "snapshot_month": c.snapshot_month, "snapshot_date": snap,
            "question": q, "response_hash": c.response_hash,
            "search_volume": c.search_volume,
        } for c, q, snap in rows],
    }
