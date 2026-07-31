#!/usr/bin/env python3
"""Monthly refresh for the Brand Answer Sentiment Console report.

For every (report, platform, dataset) slice that has already been fetched at
least once, pull the newest Brand Radar snapshots and classify anything new for
every subject brand already in use. Both halves are idempotent:

  * responses dedupe on ``response_hash``  -> re-pulling a month is free
  * verdicts dedupe on (response_hash, subject) -> old months are never re-judged

So a run costs only the genuinely new month of data. Prints one summary line per
slice and exits non-zero if any slice failed.
"""
from __future__ import annotations

import os
import sys
import traceback

# Path to the Console scaffold root (where src/db.py, src/llm.py, src/connectors.py live).
# Override with CONSOLE_ROOT if your install differs from the Letaido default.
CONSOLE_ROOT = os.environ.get("CONSOLE_ROOT", "/home/console/http/default")
sys.path.insert(0, CONSOLE_ROOT)

from sqlalchemy import select, func                      # noqa: E402
from src.db import session_scope                          # noqa: E402
from reports._brand_answer_sentiment_models import (      # noqa: E402
    BasFetch, BasResponse, BasVerdict,
)
import reports._brand_answer_sentiment_engine as E        # noqa: E402

WINDOW = 3   # months pulled per run; dedup makes the overlap free


def main() -> int:
    failures = 0

    try:
        n = E.refresh_reports()
        print(f"reports cached: {n}")
    except Exception as e:  # noqa: BLE001
        print(f"WARN could not refresh report list: {e}")

    with session_scope() as s:
        slices = [(f.report_id, f.platform, f.dataset)
                  for f in s.execute(select(BasFetch).where(BasFetch.status == "ok"))
                  .scalars().all()]
        subjects: dict[str, list[str]] = {}
        for rid, subj in s.execute(
                select(BasVerdict.report_id, BasVerdict.subject)
                .group_by(BasVerdict.report_id, BasVerdict.subject)).all():
            subjects.setdefault(rid, []).append(subj)

    if not slices:
        print("nothing fetched yet — open the report and fetch a slice first")
        return 0

    for rid, platform, dataset in slices:
        try:
            before = _count(rid, platform, dataset)
            r = E.fetch_slice(rid, platform, dataset, WINDOW)
            after = _count(rid, platform, dataset)
            print(f"FETCH {rid[:8]} {platform}/{dataset}: seen={r['seen']:,} "
                  f"new_rows={after - before:,} total={after:,}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR fetch {rid[:8]} {platform}/{dataset}: {e}")
            traceback.print_exc()

    for rid, subs in subjects.items():
        platforms = sorted({p for r, p, _d in slices if r == rid})
        datasets = sorted({d for r, _p, d in slices if r == rid})
        for subject in subs:
            try:
                pending = E.pending_count(rid, subject, platforms, datasets)
                if not pending:
                    print(f"CLASSIFY {rid[:8]} {subject}: nothing new")
                    continue
                res = E.classify_pending(rid, subject,
                                         _keywords(rid, subject), platforms, datasets)
                print(f"CLASSIFY {rid[:8]} {subject}: pending={pending:,} "
                      f"absent={res['absent']:,} classified={res['classified']:,} "
                      f"{res['counts']}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"ERROR classify {rid[:8]} {subject}: {e}")
                traceback.print_exc()

    print(f"done; failures={failures}")
    return 1 if failures else 0


def _count(rid: str, platform: str, dataset: str) -> int:
    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(BasResponse).where(
            BasResponse.report_id == rid, BasResponse.platform == platform,
            BasResponse.dataset == dataset)) or 0


def _keywords(rid: str, subject: str) -> list[str]:
    rep = E.load_report(rid) or {}
    for e in E.entities_of(rep):
        if e["name"] == subject:
            return e.get("keywords") or [subject]
    return [subject]


if __name__ == "__main__":
    sys.exit(main())
