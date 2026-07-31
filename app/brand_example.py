"""EXAMPLE per-project report. Copy this file once per Brand Radar project.

One Console report per Brand Radar project. This wrapper is all you write —
routes, engine, jobs and templates are shared, so there is nothing to duplicate.

    cp brand_example.py brand_acme.py     # then edit the three constants below

Naming rules that matter:
  * The FILENAME (minus .py) is the URL slug and must NOT start with `_`, or the
    Console loader will skip it. `brand_acme.py` -> /reports/brand_acme/
  * `NAME` is the label shown in the reports list.
  * The slug passed to make_report() MUST equal the filename, since the loader
    mounts the blueprint at /<category>/<filename>/.

The report is PINNED to REPORT_ID: it can only ever read that project's rows.
`report_id` is closed over here and never taken from the request, so no query
string can point this report at another project's data.

Find your report id with `ahrefs_brand_radar.get_reports` (or the report list in
the Brand Radar UI). If your workspace has several Ahrefs OAuth tokens, note that
each sees a different set of reports — see AHREFS_SECRETS in
_brand_answer_sentiment_engine.py.
"""

from ._brand_report_core import make_report

# ---------------------------------------------------------------- edit these
REPORT_ID = "00000000-0000-0000-0000-000000000000"   # Brand Radar report UUID
NAME = "Acme — what AI says vs competitors"          # shown in the reports list
SLUG = "brand_example"                               # MUST match this filename
# ---------------------------------------------------------------------------

OWNER = "me"

blueprint, _ = make_report(REPORT_ID, NAME, SLUG)
