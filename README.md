# Brand Against Competitors AI

**What do AI assistants actually say about your brand — and is it getting worse?**

**One report per brand, two tabs each**, over every AI answer to your tracked
prompts from [Ahrefs Brand Radar](https://ahrefs.com/brand-radar):

1. **Sentiment & criticism** — was your brand mentioned at all, was the mention
   good or bad, and which prompts produce the most criticism.
2. **What each brand is better for** — the *positioning* the AI assigns each
   brand, as semantic categories ("Backlink Analysis", "Budget & Small Teams"),
   one sub-tab per brand plus a side-by-side compare.

Both tabs read one shared store of answers, so the second view costs no extra
API calls. Each report is **pinned to a single Brand Radar project** and can only
read that project's rows; the title names the brands under comparison ("Ahrefs vs
Semrush, Peec AI, +10 more") rather than describing the report generically.

Tracking three brands means three reports and three ~12-line files — the routes,
engine, jobs and templates are shared. See **[Adding a brand](#adding-a-brand)**.

Built as a [Letaido](https://letaido.com) Console report, but the logic is plain
Flask + SQLAlchemy + Pydantic. If you're on another stack, **[docs/PORTING.md](docs/PORTING.md)**
maps every call to its public Ahrefs API v3 endpoint.

> Each answer renders as markdown with brand names highlighted inline — the
> subject brand in amber, competitors in blue, other niche tools in purple — so
> you can see at a glance who the model put alongside you, with a "view raw
> markdown" toggle for auditing.

---

## Why this exists

Brand Radar tells you *how often* you're mentioned. It does not tell you:

- whether being mentioned was **good or bad** for you
- **which prompts** consistently produce criticism
- whether your absence is **growing** month over month

Those three questions are what this report answers.

The headline finding on our own data was not what we expected: outright negative
answers were only **0.8%** of responses — but the brand went **unmentioned in 72%**
of answers to its *own tracked prompts*, and that mention rate was falling month
over month. The risk wasn't reputation. It was absence.

## Tab 1 — Sentiment & criticism

**Overview tab**

- Six KPI cards over one denominator: Mentioned · Positive · Neutral · Mixed · Negative · Not mentioned
- **Criticism table** — prompts ranked by number of negative responses (default), criticism intensity (`negative×2 + mixed×1`), % negative, or search volume. Six sort directions, most- and least-negative. A 100%-stacked bar per prompt.
- **"Is it getting worse?"** — % critical, % negative, % positive and mention rate per snapshot month, plus the individual prompts that moved most month-over-month
- **Praise table** and a **by-platform split** (mention rate and tone differ enormously between assistants)

**Browse tab**

- Every fetched answer in one server-paged table: prompt, platform, date, sentiment, mentioned/absent, search volume
- Filters: sentiment (incl. *unjudged*), mentioned/absent, free text over prompt **and** answer, six sort orders
- Copy the current page as CSV

**Both**

- Click any row for every dated response: the prompt asked, the verdict, a verbatim evidence quote, the full answer rendered as markdown, and the cited links
- Answers highlight the **subject brand in amber**, **competitors in blue**, **niche entries in purple**

> The prompt drill-down lists every answer behind a prompt, each with its
> sentiment verdict, the reasoning, the matched brands and the originating
> question, filterable by platform / sentiment / mention without re-querying.

### Multi-workspace Ahrefs tokens

If you belong to more than one Ahrefs workspace you'll have more than one OAuth
token, and **each one sees a different, non-overlapping set of Brand Radar
reports**. Hardcoding a single secret makes reports in the other workspace look
like they don't exist.

`AHREFS_SECRETS` in the engine lists every secret name. `refresh_reports()` walks
them all and stamps each cached report with the token that returned it
(`bas_report.secret`); `secret_for(report_id)` resolves it on every later call.
Add a third workspace by adding one list entry.

## Tab 2 — What each brand is better for

Same raw answers, a different question: **what does the AI think each brand is
FOR?** Instead of counting sentiment, it extracts the explicit positioning
claims ("better for backlink analysis", "too expensive for solo users") and
groups them into named semantic categories.

- **One sub-tab per brand** — your brand first, plus a **Compare all** tab
  showing each brand's top categories side by side
- Toggle between **"What it's better for"** and **"What it's NOT chosen for"**
- Per category: share of claims, distinct answers, platform split, which brands
  it was contrasted against, a verbatim quote, and a drill-down to every claim
  with its prompt and evidence
- Minimum-share filter hides the long tail (a 2-claim group out of 4,000 is
  noise, not a theme)

### How the categories are built — two stages

**Stage 1, extract (per answer, cached forever).** ±420 characters around each
brand mention go to a cheap model in batches of 8. It returns only *explicit*
positioning claims, each with a **verbatim evidence span**, a normalised 2–6 word
use-case phrase, a polarity (strength/weakness), and any brands it was contrasted
against. A bare mention in a list is not a claim. Answers yielding nothing get a
sentinel row so they're never re-read.

**Stage 2, group (cheap, re-runnable).** Phrases are deduped, embedded, and
merged by cosine similarity — most-common phrasing seeds each cluster so groups
are **stable across re-runs**. A stronger model then *labels* the groups; it
names them, it does not decide the grouping. Finally a **second merge pass at
label level** collapses near-synonym categories.

That last pass matters more than it sounds. Phrase-level clustering alone gave
one brand six separate budget categories — "Entry-Level Pricing", "Startup
Affordability", "Budget Prompt Monitoring"… Merging semantically-identical
*labels* turned 11 fragmented categories into 8 clean ones.

Splitting the stages is what makes it affordable: extraction is the expensive
part and caches per answer; grouping needs a global view but is cheap, so you can
re-group after a new month, or at a different similarity threshold, without
re-reading anything.

### What it surfaced on real data

Running both reports on the same 37k answers, the positioning view found what the
sentiment view couldn't: the brand's single biggest "not chosen for" category was
**Affordability & Ease Of Use at 36%** of all its weakness claims — and
**AI Search Visibility Tracking appeared in *both* its strength and weakness
lists** (5.9% vs 20.1%). The models are actively split on whether the brand is
credible in the exact category it's trying to own. A sentiment score alone can't
tell you that; it just says "mixed".

## Everything is pickable

A settings panel at the top drives the whole report, showing the active selection as chips:

| Filter | Options |
|---|---|
| Brand Radar report | any report in your workspace |
| Subject brand | your own brand **or any competitor in the report** — so you can run "What ChatGPT says about `<competitor>`" |
| Platforms | ChatGPT · Gemini · Perplexity · Copilot · Grok · Google AI Overviews · Google AI Mode |
| Prompt dataset | custom prompts / Ahrefs public prompts / both |
| Countries · Months | whatever is present in the stored data |
| Fetch window | 3 / 6 / 12 months / all history |

Only two actions cost anything, and both are explicit buttons:

| Action | Cost | Dedup key |
|---|---|---|
| Change any filter, including the subject brand | **free** (SQL over stored rows) | — |
| **Fetch** a (report, platform, dataset) slice | Ahrefs API | `response_hash` — re-pulling a month is free |
| **Analyse** a subject brand | LLM, mentioned responses only | `(response_hash, subject)` — old months are never re-judged |

## How the numbers are produced

1. **Fetch** — `ai-responses` per platform × dataset, with **no date filter** so every dated snapshot comes back (this is what makes the trend possible), paged 1,000 rows at a time.
2. **Detect mentions** — a whole-word, case-insensitive regex over every alias of every brand in the report, longest alias first. *Brand Radar does not give you this* (see the gotcha below). Responses with no hit are `absent` and **cost no LLM call** — 72% of rows in our data.
3. **Classify** — for mentioned responses, up to two ±340-character excerpts around the mentions go to an LLM in batches of 12 with a fixed rubric:
   - **positive** — recommended or praised, no material criticism
   - **neutral** — named or described without evaluation
   - **mixed** — praise *and* criticism, or recommended-with-caveats ("powerful but expensive")
   - **negative** — criticism dominates, or it steers the reader away
4. **Aggregate** — KPIs, criticism intensity, monthly trend and movers are all SQL over the stored verdicts.

Garbled model output is retried in half-batches and otherwise **left unjudged** rather than defaulted to neutral — the UI shows the count and offers to re-run, so a bad batch never silently becomes a wrong number.

## Gotchas worth knowing before you build this

These cost us time; they're the real value of this repo.

1. **`matched_brands` comes back empty.** The AI-responses endpoint returned an empty matched-brands array on every single row we tested (600+ sampled). You must detect mentions yourself. Everything downstream — including the "Not mentioned" KPI — depends on it.
2. **Non-mentioning answers ARE returned.** Sorted by relevance, mentioned rows come first and absent rows later, so a naive `limit=100` looks like a 100% mention rate. Page to exhaustion.
3. **Google sources can't be mixed.** `google_ai_overviews` and `google_ai_mode` can't be combined with each other or with the chatbot models. One call each.
4. **AIO / AI Mode history is short.** The current index only goes back to ~March 2026; the older keyword-index variants go further but carry different fields.
5. **Custom prompts are free; the public prompt pool is not.** Same endpoint, one parameter apart, wildly different cost.
6. **Mixed >> negative.** In our data mixed outnumbered negative 5:1. Where you draw the mixed/negative line determines your headline, so validate the rubric on real examples before classifying at scale.
7. **Never run a DB query at import time in a Console app.** We recovered orphaned
   background jobs with `threading.Timer(2.0, adopt_jobs)`. That query landed while
   the app loader was still importing *other* apps' modules, so SQLAlchemy
   configured its mappers against a half-built registry, **cached the failure, and
   poisoned every ORM query in the process** — including apps we hadn't touched.
   The traceback pointed at an unrelated model and was thoroughly misleading. Do
   this work lazily in `@blueprint.before_request` instead, by which time every
   model is registered.
8. **Multiple Ahrefs tokens see different reports.** If the user belongs to more
   than one Ahrefs workspace, a report that plainly exists will look absent
   because you queried with the wrong credential. Persist which token owns each
   report — see [Multi-workspace Ahrefs tokens](#multi-workspace-ahrefs-tokens).

## Repo layout

```
app/
  brand_example.py                      ONE PER BRAND — copy it, edit 3 constants (this is the only file you write)
  _brand_report_core.py                 Factory: make_report() builds a pinned report — routes, schemas, jobs, tab 1
  _brand_answer_sentiment_engine.py     Ahrefs pulls, mention regex, sentiment rubric, SQL aggregation
  _brand_answer_sentiment_models.py     bas_* models (5 tables) — the shared answer store
  brand_answer_sentiment_monthly.py     Monthly refresh script (cron) — covers every brand automatically
  _brand_positioning_view.py            Factory: make_positioning() builds tab 2, nested under its parent
  _brand_positioning_engine.py          Claim extraction, embedding clustering, label merge
  _brand_positioning_models.py          bp_* models (3 tables)
  templates/
    brand_answer_sentiment/index.html   Tailwind CDN + ApexCharts + marked/DOMPurify
    brand_positioning/index.html
docs/
  PORTING.md                            Public Ahrefs API v3 endpoint map + what you must build yourself
```

Everything with a leading underscore is shared machinery. The Console loader only
registers modules that expose `NAME` and don't start with `_`, so the factories
stay invisible and each `brand_*.py` wrapper becomes its own report.

Tab 2 reads `bas_response` directly — it has no fetch path of its own, by
design. Fetch in tab 1, analyse in tab 2.

## Adding a brand

```bash
cp app/brand_example.py app/brand_acme.py
```

Edit the three constants:

```python
REPORT_ID = "abc12345-…"                      # Brand Radar report UUID
NAME = "Acme — what AI says vs competitors"   # label in the reports list
SLUG = "brand_acme"                           # MUST equal the filename
```

That's it — `/reports/brand_acme/` and `/reports/brand_acme/positioning/` appear
on the next reload. No routes, engine or templates to copy.

Two rules the loader enforces: the filename must not start with `_` (or it's
skipped), and `SLUG` must equal the filename (the blueprint is mounted at
`/reports/<filename>/`).

### Why one report per project, not a picker

The first version had a single report with a dropdown of every Brand Radar report
in the workspace. It was worse in practice:

* With ~70 reports, the one you'd just backfilled was invisible — the picker sat
  inside a collapsed settings panel and the page auto-loaded the largest report.
* A dropdown implies you can compare across projects, but each project has its
  own brands, prompts and competitors — there's nothing meaningful to compare.
* Scoping by a **request parameter** is fragile. Several routes read
  `report_id` from the query string, and one stray read is a cross-project data
  leak.

Pinning fixes all three: `report_id` is closed over by the factory and never read
from the request, so the report is *structurally* incapable of reading another
project's data. The `report_id` fields on the request schemas are ignored, and
`api/jobs/active` plus orphan-job recovery are scoped per report too, so one
project's background job can't surface in another's progress box.

## Running it

### On Letaido

Drop `app/*.py` into `/home/console/http/default/reports/` and the two template
folders into `/home/console/http/default/templates/`. Copy `brand_example.py` once
per brand (see **[Adding a brand](#adding-a-brand)**); the scaffold auto-discovers
each one at `/reports/<slug>/`, with its positioning tab at
`/reports/<slug>/positioning/`. Requires an Ahrefs connector secret and
PostgreSQL — both provided by the platform.

For the monthly refresh, schedule `brand_answer_sentiment_monthly.py`
(set `CONSOLE_ROOT` if your scaffold isn't at the default path).

### Anywhere else

Read **[docs/PORTING.md](docs/PORTING.md)** first. You'll need to replace three
platform imports (`src.connectors`, `src.llm`, `src.db`) with your own Ahrefs
client, OpenAI-compatible client and SQLAlchemy setup. Nothing else is
Letaido-specific.

**Nothing is hardcoded** — no report IDs, brand names, API keys or model names
are baked into the source. `docs/PORTING.md` ends with the questions an AI agent
should ask you before rebuilding this, because guessing them wrong means a
rebuild.

## Stack

Flask 3 · SQLAlchemy 2 · Pydantic v2 · PostgreSQL · Jinja2 · Tailwind (CDN) ·
ApexCharts · marked + DOMPurify · an OpenAI-compatible LLM endpoint

## License

MIT — see [LICENSE](LICENSE).

Ahrefs and Brand Radar are trademarks of Ahrefs Pte. Ltd. This project is not an
official Ahrefs product; it's an application built on top of their API.
