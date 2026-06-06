"""Nightly verifier for assistant route candidates.

Run on CK. Promotes only learning routes that already met the required
verification count and have aged long enough for review visibility.
"""
from __future__ import annotations

import argparse
import json

from scripts.assistant_ck_runtime import _auto_verify_tool_routes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-age-days", type=int, default=7)
    args = parser.parse_args()
    print(json.dumps(_auto_verify_tool_routes(args.min_age_days), sort_keys=True))


if __name__ == "__main__":
    main()
