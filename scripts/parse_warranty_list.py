"""parse_warranty_list.py — one-shot parser for the WebstaurantStore
Extended Warranty Protection list Sam pasted in dev-chat #1129
(2026-05-26). 76 active plans.

Input format (line-oriented blocks):
    <title>            # item name
    <title>            # repeated (image alt text)
    Item #: <model>
    <order_no>         # numeric
    Waiting | Safeware Warranty | Expired
    <date> | Visit Safeware Portal for Details
    Register           # optional, only when status='Waiting'

Output:
    JSON file at docs/equipment_warranties.json, list of dicts.

Run:
    python scripts/parse_warranty_list.py <input_text_file> [output_json]
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path


_DATE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
_ORDER = re.compile(r"^\d{6,}$")
_ITEM_NO = re.compile(r"^Item #:\s*(.+)$")
_STATUS = {"Waiting", "Safeware Warranty", "Expired"}


def parse(text: str) -> list[dict]:
    # Strip header lines + flatten whitespace
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Find the start: first "Item #: ..." line marks the first block.
    try:
        start = next(i for i, ln in enumerate(lines) if _ITEM_NO.match(ln))
    except StopIteration:
        return []

    rows: list[dict] = []
    i = start - 1  # rewind one line: title precedes "Item #:"
    while i < len(lines):
        # Block: try to read [title (1+ lines), Item #, order_no, status, date|portal, Register?]
        # Heuristic: title line(s) up to the next "Item #:" line.
        # Find next Item # line.
        try:
            item_idx = next(j for j in range(i, min(i + 8, len(lines)))
                            if _ITEM_NO.match(lines[j]))
        except StopIteration:
            break
        # Title is the line before Item # (titles are typically duplicated;
        # use the unique one immediately before).
        title = lines[item_idx - 1]
        m = _ITEM_NO.match(lines[item_idx])
        item_no = m.group(1) if m else ""
        # Order # is the next line
        order_no = lines[item_idx + 1] if item_idx + 1 < len(lines) else ""
        if not _ORDER.match(order_no):
            # Skip malformed block
            i = item_idx + 1
            continue
        # Status
        status_line = lines[item_idx + 2] if item_idx + 2 < len(lines) else ""
        status = next((s for s in _STATUS if s in status_line), "Unknown")
        # Next line: expiration or portal blurb
        nl = lines[item_idx + 3] if item_idx + 3 < len(lines) else ""
        expiration = nl if _DATE.match(nl) else None
        portal_only = "Safeware Portal" in nl
        # Optional "Register" line
        cursor = item_idx + 4
        if cursor < len(lines) and lines[cursor] == "Register":
            cursor += 1

        rows.append({
            "title": title,
            "item_number": item_no,
            "order_number": order_no,
            "status": status,
            "expiration_date": expiration,
            "portal_only": portal_only,
            "source": "WebstaurantStore",
        })
        i = cursor
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output", nargs="?",
                    default="docs/equipment_warranties.json")
    args = ap.parse_args()
    text = Path(args.input).read_text(encoding="utf-8", errors="replace")
    rows = parse(text)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"parsed {len(rows)} warranty rows -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
