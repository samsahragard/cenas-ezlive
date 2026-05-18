"""One-shot wipe of the EzcaterKnownDriver roster table per Sam #2870 Item 6
(scope A confirmed by Sam in /sam/chat msg 595 + cena #596/#598).

Flow:
1. Pre-delete count from ezcater_known_driver
2. CREATE TABLE ezcater_known_driver_archive_2026_05_18 AS SELECT * FROM source
3. Verify archive row count == source row count (cena #598 sanity check before DELETE)
4. DELETE FROM ezcater_known_driver
5. Verify source count == 0 post-DELETE

Out of scope (NOT TOUCHED):
- drivers table (the 10 test drivers stay)
- orders.ezcater_driver_name (audit history)
- any other table

Re-run safety: CREATE TABLE will raise if archive already exists. Manual
DROP TABLE ezcater_known_driver_archive_2026_05_18 required to re-run.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402


ARCHIVE_TABLE = "ezcater_known_driver_archive_2026_05_18"
SOURCE_TABLE = "ezcater_known_driver"


def main() -> int:
    db = SessionLocal()
    try:
        src_count = db.execute(text(f"SELECT COUNT(*) FROM {SOURCE_TABLE}")).scalar()
        print(f"source rows pre-archive: {src_count}")

        if src_count == 0:
            print("source already empty - nothing to wipe")
            return 0

        archive_exists = db.execute(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
        ), {"t": ARCHIVE_TABLE}).first()
        if archive_exists:
            print(f"FAIL: archive table {ARCHIVE_TABLE} already exists; "
                  f"manual DROP required to re-run")
            return 1

        db.execute(text(f"CREATE TABLE {ARCHIVE_TABLE} AS SELECT * FROM {SOURCE_TABLE}"))
        db.commit()
        print(f"archive table {ARCHIVE_TABLE} created")

        archive_count = db.execute(text(f"SELECT COUNT(*) FROM {ARCHIVE_TABLE}")).scalar()
        print(f"archive rows: {archive_count}")

        if archive_count != src_count:
            print(f"FAIL: archive count {archive_count} != source count {src_count} - "
                  f"aborting before DELETE")
            return 1
        print("sanity check pass: archive count == source count")

        deleted = db.execute(text(f"DELETE FROM {SOURCE_TABLE}")).rowcount
        db.commit()
        print(f"deleted {deleted} rows from {SOURCE_TABLE}")

        post_count = db.execute(text(f"SELECT COUNT(*) FROM {SOURCE_TABLE}")).scalar()
        print(f"source rows post-delete: {post_count}")

        if post_count != 0:
            print(f"FAIL: source still has {post_count} rows after DELETE")
            return 1

        ck1_post = db.execute(text(
            f"SELECT COUNT(*) FROM {SOURCE_TABLE} WHERE ck_prefix = 1"
        )).scalar()
        ck2_post = db.execute(text(
            f"SELECT COUNT(*) FROM {SOURCE_TABLE} WHERE ck_prefix = 2"
        )).scalar()
        ck_null_post = db.execute(text(
            f"SELECT COUNT(*) FROM {SOURCE_TABLE} WHERE ck_prefix IS NULL"
        )).scalar()
        print(f"per-prefix post-delete: ck1(Copperfield)={ck1_post} "
              f"ck2(Tomball)={ck2_post} null(ambiguous)={ck_null_post}")

        try:
            from flask import current_app as _ca, render_template_string
            from datetime import date as _d, timedelta as _td
            tpl = (_ca.jinja_env.loader.get_source(_ca.jinja_env, "driver_payroll_list.html")[0])
            content_start = tpl.find("{% block content %}")
            content_end = tpl.find("{% endblock %}", content_start)
            content_block = tpl[content_start + len("{% block content %}"):content_end] if content_start >= 0 else tpl
            today = _d.today()
            rendered = render_template_string(
                content_block,
                rows=[],
                current_period_start=today,
                current_period_end=today + _td(days=14),
                current_check_date=today + _td(days=21),
                g=type("G", (), {"store_label": "verify"})(),
            )
            empty_marker = "No ezCater drivers in this store" in rendered
            no_name_link = 'class="driver-link"' not in rendered and "driver_paycheck" not in rendered
            print(f"jinja-render verify (rows=[]): empty_marker={empty_marker} "
                  f"no_name_links={no_name_link} body_len={len(rendered)}")
        except Exception as e:  # noqa: BLE001
            print(f"jinja-render verify skipped: {type(e).__name__}: {e}")

        print(f"\nSUMMARY:")
        print(f"  archived: {archive_count} rows -> {ARCHIVE_TABLE}")
        print(f"  deleted:  {deleted} rows from {SOURCE_TABLE}")
        print(f"  source now: {post_count} rows "
              f"(ck1={ck1_post}, ck2={ck2_post}, null={ck_null_post})")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
