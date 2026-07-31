# Porting this outside Letaido — the Ahrefs API endpoints you need

This app was built on the Letaido platform, where Ahrefs is reached through a
typed connector layer (`src.connectors.invoke("ahrefs_brand_radar.…")`) that
handles auth, retries and pagination. If you're rebuilding this on your own
stack, you talk to the **public Ahrefs API v3** directly instead.

- **Base URL:** `https://api.ahrefs.com/v3`
- **Auth:** `Authorization: Bearer $AHREFS_API_KEY`
- **Docs:** <https://docs.ahrefs.com/api/reference/brand-radar/post-ai-responses>
- Brand Radar requires an Ahrefs plan with the Brand Radar entitlement.

## Connector → public endpoint map

| What this app calls | Public Ahrefs API v3 equivalent | Notes |
|---|---|---|
| `ahrefs_brand_radar.ai_responses_results` | `POST /v3/brand-radar/ai-responses` (or `GET` with query params) | **The core call.** Returns the prompt, the full answer text, cited links, volume, country, data source. |
| `ahrefs_brand_radar.get_reports` | `GET /v3/management/brand-radar-reports`-style management reads | Used only to populate the report dropdown. You can skip it and configure brands/competitors yourself. |
| `ahrefs_brand_radar.get_custom_queries` | `GET /v3/management/brand-radar-prompts` | Gives the prompt roster — the `M` in the report's "N of M prompts" badge, including prompts with zero responses. |
| `ahrefs_brand_radar.add_custom_queries` | `PUT /v3/management/brand-radar-prompts-add` | Only needed if you want the app to create prompts too. Free (no API units). |
| `ahrefs_brand_radar.ai_response_available_filters` | `GET /v3/brand-radar/ai-responses` with a narrow filter | Optional: discover which models/countries/dates exist before pulling. |

## The one call that matters

```bash
curl "https://api.ahrefs.com/v3/brand-radar/ai-responses" \
  -X POST \
  -H "Authorization: Bearer $AHREFS_API_KEY" \
  -H "Content-Type: application/json" \
  --data-raw '{
    "select": ["question","response","volume","country","links","search_queries","tags","data_source","last_updated"],
    "report_id": "<YOUR_REPORT_ID>",
    "prompts": "custom",
    "data_source": ["chatgpt"],
    "limit": 1000,
    "order_by": "relevance",
    "brands": [
      { "names": ["Your Brand"], "url_groups": [{ "target": "yourbrand.com", "scope": "subdomains" }] }
    ],
    "competitors": [
      { "names": ["Competitor"], "url_groups": [{ "target": "competitor.com", "scope": "subdomains" }] }
    ],
    "output": "json"
  }'
```

### Parameter equivalences

| This app's arg | Public API field | Notes |
|---|---|---|
| `filters.models` | `data_source` | **Google sources cannot be mixed.** `google_ai_overviews` and `google_ai_mode` can't be combined with each other or with `chatgpt`/`gemini`/`perplexity`/`copilot`/`grok`. Make one call per Google source. |
| `queries_dataset_filter: only_custom_queries` | `prompts: "custom"` | **Custom-prompt requests are free** (no API units). Requests including Ahrefs' public prompt data consume units at standard pricing — so `prompts: "ahrefs"` is the expensive path. |
| `queries_dataset_filter: only_public_queries` | `prompts: "ahrefs"` | Much larger pool; costs units. |
| `report.brands[].urls` / `.mode` | `brands[].url_groups[].target` / `.scope` | `scope`: `url` / `domain` / `subdomains`. |
| `report.brands[].keywords` | `brands[].names` | Name aliases for the brand. |
| `filters.country` | `country` | Comma-separated ISO 3166-1 alpha-2, lowercase (`us,gb,de`). |
| `filters.date` | `date` | `YYYY-MM-DD`. **Omit it to get every dated snapshot** — that's what makes the month-over-month trend possible. |
| `pagination.limit` | `limit` | Default 1000. |
| `sort_by` | `order_by` | `relevance` or `volume`. |
| `filters.text_filter` | `where` | Filter expression over `question`, `response`, `topic`, `search_queries`, `cited_domain`, `cited_url_prefix`, … See the docs' filter-syntax page. |
| `search_volume` (result) | `volume` | Estimated monthly searches for the question. Costs 10 units when selected. |
| `sitelinks` (result) | `links` | Cited URLs. Costs 10 units when selected. |
| `updated_at` (result) | `last_updated` | The snapshot timestamp — this is what you bucket by month. |
| `model` (result) | `data_source` | Which chatbot produced the answer. |

### Cost control

`response`, `volume` and `links` each cost **10 API units** when included in
`select`. Only request what you render. Rate limit is ~60 requests/minute.

## Things that are NOT in the API — you have to build them

These are the parts of this app that aren't a Brand Radar feature:

1. **Mention detection.** The AI-responses endpoint does **not** reliably tell you
   which of your tracked brands appear in a given answer (in our testing
   `matched_brands` came back empty on every row). You must detect mentions
   yourself with a whole-word, case-insensitive regex over every alias of every
   brand — longest alias first, so `"Acme Pro"` isn't shredded into `"Acme"`. See
   `_alias_rx()` in `app/_brand_answer_sentiment_engine.py`.
2. **Sentiment.** There is no sentiment field. This app sends ±340-character
   excerpts around each mention to an LLM in batches of 12 with a fixed 4-way
   rubric (positive / neutral / mixed / negative). See `RUBRIC` in the engine.
3. **The "not mentioned" bucket.** Responses that never mention the brand *are*
   returned by the endpoint, so "brand absent" is computable — but only after you
   do step 1. This is the most important metric in the whole report and it does
   not exist in the API.
4. **Persistence + dedup.** The API has no notion of "which rows have I already
   analysed". Store responses keyed by a hash of
   (report, dataset, platform, snapshot date, prompt, answer) and verdicts keyed
   by (response hash, subject brand) so re-runs and monthly refreshes never
   re-pay for old data.
5. **Criticism intensity, trend, movers.** All derived in SQL from the stored
   verdicts.

## One report per project

Each report is pinned to a single Brand Radar report UUID, closed over at
construction time and never read from the request. If you rebuild this, keep that
property: a report id arriving in a query string is a cross-tenant data leak
waiting to happen, and "filter by the id the client sent" is exactly the bug we
had to remove. Scope background-job listings by the same pinned id, or one
project's jobs show up in another's UI.

The shape that worked: one factory function returning a blueprint, plus a tiny
per-project wrapper module that supplies `(report_id, title, slug)`. Adding a
project is one file; the routes, SQL and templates are shared.

## One API token is not always enough

Brand Radar reports are scoped to an Ahrefs workspace. A user in several
workspaces has several credentials, and `GET /v3/brand-radar/reports` returns
only the reports the presented token can see. If you rebuild this on the public
API, treat "which credential owns this report" as part of the report's identity
and persist it — otherwise a perfectly valid report id will 404 for no visible
reason.

## The positioning report needs no extra Ahrefs endpoints

Report 2 ("What each brand is better for") reads the answers report 1 already
stored. It calls **no** Ahrefs endpoint at all. What it needs instead:

| Need | What we used | Notes |
|---|---|---|
| Claim extraction | a cheap chat model, JSON mode | ~1 call per 8 answers. Requires verbatim evidence so claims are auditable. |
| Embeddings | `text-embedding-3-small` | On *distinct phrases*, not claims — dedup first, it's a big cost saver. |
| Cluster labelling | a stronger chat model | ~1 call per 12 clusters. It labels; it does not group. |

The clustering itself is greedy centroid assignment in ~20 lines — no sklearn,
no vector DB. Feeding claims in frequency order makes it deterministic, which
matters because the categories are user-facing and shouldn't reshuffle between
runs.

## Platform services this app assumes

Replace these three imports with your own equivalents:

| Letaido import | What it does | Your equivalent |
|---|---|---|
| `from src.connectors import ahrefs` | Authenticated Ahrefs call, unwraps large payloads | `requests.post` to `api.ahrefs.com/v3` with a Bearer key |
| `from src.llm import console_openai_client` | OpenAI-compatible client with spend attribution | `OpenAI(api_key=…)` or any OpenAI-compatible endpoint |
| `from src.db import Base, session_scope, db_session` | SQLAlchemy 2.x session/base against PostgreSQL | Standard SQLAlchemy setup |

Everything else is plain Flask 3 + SQLAlchemy 2 + Pydantic v2 + Jinja2 +
Tailwind (CDN) + ApexCharts, with no Letaido-specific behaviour.

## If you're an AI agent reading this to rebuild the app

**Stop and ask the human these questions first** — the answers change the schema
and the cost profile, and guessing them wrong means a rebuild:

1. **Which Brand Radar report, and what's the report ID?** (Found in the report
   URL: `https://app.ahrefs.com/brand-radar/reports/<REPORT_ID>/…`.) The report
   supplies the brand, competitors, niche and country scope.
2. **Which brand is the subject?** The report's own brand, or a competitor in it?
   Verdicts are stored per subject, so this determines what you classify. Ask
   whether they want to switch subjects later (if yes, detect mentions for *all*
   entities up front — it's free — and only the LLM verdicts become per-subject).
3. **Which platforms and which prompt dataset?** Custom prompts are **free**;
   Ahrefs' public prompt pool costs API units and is much larger. Do not pull the
   public pool without explicit confirmation.
4. **How far back?** Every extra month is more rows to fetch and classify. Ask
   for a window (this app defaults to 3 months) rather than assuming all history.
5. **Which LLM, and is the spend approved?** Classification is the dominant cost.
   Give them the estimate: roughly (mentioned responses ÷ 12) requests. Note that
   non-mentioning responses need **no** LLM call — in our data that was 72% of
   rows, so the estimate collapses once mention detection runs.
6. **What counts as "mixed" vs "negative" for them?** Show them 5 real classified
   examples and confirm the rubric before classifying tens of thousands of rows.
   In our data `mixed` outnumbered `negative` 5:1, so where that line sits
   determines the whole report's headline.

Do not hardcode a report ID, brand name, API key or model into the source. Read
them from config/env, and surface the per-run cost before spending.
