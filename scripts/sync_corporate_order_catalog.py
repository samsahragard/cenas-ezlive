from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import corporate_shop


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed/sync the corporate order Product catalog."
    )
    parser.add_argument(
        "--update-stock",
        action="store_true",
        help="Also reset existing product stock counts to the repo catalog values.",
    )
    parser.add_argument(
        "--normalize-names",
        action="store_true",
        help="Also clean legacy spacing in existing product names.",
    )
    parser.add_argument(
        "--db-url-file",
        help="Optional file containing CORPORATE_DB_URL for local maintenance runs.",
    )
    args = parser.parse_args()

    if args.db_url_file and not os.getenv("CORPORATE_DB_URL"):
        os.environ["CORPORATE_DB_URL"] = Path(args.db_url_file).read_text(
            encoding="utf-8"
        ).strip()

    result = corporate_shop.sync_catalog_from_seed(
        update_existing_stock=args.update_stock,
        normalize_existing_names=args.normalize_names,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
