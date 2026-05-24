"""ezCater order-PDF extractor (Step 2 of Sam #530 pipeline, aick's lane).

Takes a downloaded ezCater partner-portal order PDF (Step 1 / ck's lane
produces these via scripts/ezcater_download_order_pdfs.py) and parses it
with pdfplumber into the EzcaterOrderDetails column set (Cena #534
field lock). Idempotent: same PDF in => same dict out. Fail-soft: on
parse error returns a dict with parse_error set and partial fields
populated where possible, never raises.

Field-list lock (Cena #534) — what this extracts that orders/order_items
does NOT already have from the API:
  * per-item prices (order_items has qty + name but no price)
  * setup-piece counts (chafing dishes / sternos / utensils / plates
    / napkins / cups / serving utensils)
  * per-item dietary notes
  * day-of contact name + phone (sometimes differs from billing)
  * gate codes (sometimes separate from delivery_instructions)
  * customer special-instructions free-text block
  * fee breakdown (commission + service fee + processing fee — the
    orders.fee column bundles these into a single total)

NOTE: regex/heading patterns below are SKETCH — refined once ck's Step 1
delivers a real sample PDF. Until then this skeleton compiles and runs
but the regexes are best-guesses from ezCater's typical merchant-portal
PDF layout. The `extractor_version` bumps when a substantive change to
the pattern set lands so backfills can target only stale rows.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber

logger = logging.getLogger(__name__)


EXTRACTOR_VERSION = "1"


# --- public dataclass ------------------------------------------------------

@dataclass
class ExtractResult:
    """One PDF's parse output. Mirrors EzcaterOrderDetails columns 1:1
    so the caller can do `EzcaterOrderDetails(**result.as_row())`."""
    external_order_id: str | None = None
    items_json: str | None = None
    setup_pieces_json: str | None = None
    special_instructions: str | None = None
    gate_code: str | None = None
    day_of_contact_name: str | None = None
    day_of_contact_phone: str | None = None
    commission_cents: int | None = None
    service_fee_cents: int | None = None
    processing_fee_cents: int | None = None
    source_pdf_path: str | None = None
    source_pdf_sha256: str | None = None
    extractor_version: str = EXTRACTOR_VERSION
    parse_error: str | None = None

    def as_row(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    @property
    def parse_status(self) -> str:
        """Three-state status the /sam/chat UI branches on. Matches
        the contract on /sam/cena/ezcater-order-full (parse_status added
        per cena #560: "extracted" / "parse_error" / "not_extracted"
        beats inferring from null)."""
        if self.parse_error:
            return "parse_error"
        if self.items_json or self.commission_cents is not None:
            return "extracted"
        return "not_extracted"


# --- main entry points -----------------------------------------------------

def extract_pdf(pdf_path: str | Path) -> ExtractResult:
    """Parse one ezCater order PDF from disk. Never raises — parse
    errors land in result.parse_error with partial fields populated."""
    path = Path(pdf_path)
    result = ExtractResult(source_pdf_path=str(path))
    try:
        raw = path.read_bytes()
        result.source_pdf_sha256 = hashlib.sha256(raw).hexdigest()
        _extract_into(io.BytesIO(raw), result)
    except FileNotFoundError as e:
        result.parse_error = f"file_not_found: {e}"
    except Exception as e:  # pdfplumber / PDF format failures
        logger.exception("ezcater_extractor: parse failed for %s", path)
        result.parse_error = f"{type(e).__name__}: {e}"
    return result


def extract_pdf_bytes(pdf_bytes: bytes, source_path: str | None = None) -> ExtractResult:
    """Parse a PDF already in memory (e.g. fresh download from Step 1
    that hasn't been written to disk yet)."""
    result = ExtractResult(
        source_pdf_path=source_path,
        source_pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    try:
        _extract_into(io.BytesIO(pdf_bytes), result)
    except Exception as e:
        logger.exception("ezcater_extractor: parse failed for in-memory PDF")
        result.parse_error = f"{type(e).__name__}: {e}"
    return result


# --- internals -------------------------------------------------------------

def _extract_into(stream: io.BytesIO, result: ExtractResult) -> None:
    """Drive the pdfplumber pass + section-by-section field extraction.
    Each section is best-effort; missing sections leave fields None."""
    with pdfplumber.open(stream) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)
        tables = []
        for page in pdf.pages:
            tables.extend(page.extract_tables() or [])

    result.external_order_id = _match_order_id(text)
    result.special_instructions = _match_special_instructions(text)
    result.gate_code = _match_gate_code(text)
    name, phone = _match_day_of_contact(text)
    result.day_of_contact_name = name
    result.day_of_contact_phone = phone

    fees = _match_fee_breakdown(text)
    result.commission_cents = fees.get("commission_cents")
    result.service_fee_cents = fees.get("service_fee_cents")
    result.processing_fee_cents = fees.get("processing_fee_cents")

    items = _extract_items(tables, text)
    if items:
        result.items_json = json.dumps(items, separators=(",", ":"))

    setup = _extract_setup_pieces(text)
    if setup:
        result.setup_pieces_json = json.dumps(setup, separators=(",", ":"))


# --- field-level matchers (SKETCH — refine on real PDF sample) ------------

# ezCater order numbers are 3 chars + dash + 3 chars (e.g. 742-V7Y).
_ORDER_ID_RE = re.compile(r"\b([0-9A-Z]{3}-[0-9A-Z]{3})\b")

def _match_order_id(text: str) -> str | None:
    m = _ORDER_ID_RE.search(text)
    return m.group(1) if m else None


_SPECIAL_INSTRUCTIONS_RE = re.compile(
    r"(?:Special Instructions|Delivery Instructions|Order Notes)\s*:?\s*\n(.+?)(?:\n\n|\Z)",
    re.DOTALL | re.IGNORECASE,
)

def _match_special_instructions(text: str) -> str | None:
    m = _SPECIAL_INSTRUCTIONS_RE.search(text)
    return m.group(1).strip() if m else None


_GATE_CODE_RE = re.compile(
    r"(?:Gate\s*Code|Gate)\s*:?\s*([A-Z0-9#*\-]{2,20})", re.IGNORECASE,
)

def _match_gate_code(text: str) -> str | None:
    m = _GATE_CODE_RE.search(text)
    return m.group(1).strip() if m else None


_DAY_OF_CONTACT_RE = re.compile(
    r"(?:On-?Site\s*Contact|Day[- ]of\s*Contact|Contact\s*Name)\s*:?\s*([^\n]+?)\s*(?:\(?(\+?\d[\d\-\s().]{8,})\)?)?",
    re.IGNORECASE,
)

def _match_day_of_contact(text: str) -> tuple[str | None, str | None]:
    m = _DAY_OF_CONTACT_RE.search(text)
    if not m:
        return None, None
    name = (m.group(1) or "").strip() or None
    phone = (m.group(2) or "").strip() or None
    return name, phone


_FEE_LINE_RES = {
    "commission_cents": re.compile(r"Commission\s*:?\s*\$?([\d,]+\.\d{2})", re.IGNORECASE),
    "service_fee_cents": re.compile(r"Service\s*Fee\s*:?\s*\$?([\d,]+\.\d{2})", re.IGNORECASE),
    "processing_fee_cents": re.compile(
        r"(?:Processing|Payment\s*Transaction)\s*Fee\s*:?\s*\$?([\d,]+\.\d{2})",
        re.IGNORECASE,
    ),
}

def _match_fee_breakdown(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, rx in _FEE_LINE_RES.items():
        m = rx.search(text)
        if m:
            dollars = m.group(1).replace(",", "")
            out[key] = int(round(float(dollars) * 100))
    return out


# Items table: ezCater PDFs typically have a 4-col table
# (qty | description | unit price | line total). Real format TBD on first
# sample PDF — this is the conservative shape that works for most caterer
# PDF layouts we've seen.
def _extract_items(tables: list[list[list[str | None]]], text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for table in tables:
        if not table or len(table) < 2:
            continue
        header = [(c or "").strip().lower() for c in table[0]]
        if not any("qty" in h or "quantity" in h for h in header):
            continue
        for row in table[1:]:
            cells = [(c or "").strip() for c in row]
            if not any(cells):
                continue
            item = _parse_item_row(cells, header)
            if item:
                items.append(item)
        if items:
            break  # first matching table wins
    return items


def _parse_item_row(cells: list[str], header: list[str]) -> dict[str, Any] | None:
    """Map one items-table row to {name, qty, unit_price_cents,
    line_total_cents, dietary_notes}. Returns None for unparseable rows
    (e.g. subtotal/separator lines)."""
    try:
        # find col indexes by header keyword
        idx_qty = next(i for i, h in enumerate(header) if "qty" in h or "quantity" in h)
        idx_name = next(
            (i for i, h in enumerate(header) if "description" in h or "item" in h or "name" in h),
            1,
        )
    except StopIteration:
        return None

    qty_raw = cells[idx_qty] if idx_qty < len(cells) else ""
    name_raw = cells[idx_name] if idx_name < len(cells) else ""
    qty_m = re.match(r"\s*(\d+)", qty_raw)
    if not qty_m or not name_raw:
        return None

    unit_cents = None
    line_cents = None
    for i, h in enumerate(header):
        if i >= len(cells):
            continue
        val = re.search(r"\$?([\d,]+\.\d{2})", cells[i])
        if not val:
            continue
        cents = int(round(float(val.group(1).replace(",", "")) * 100))
        if "unit" in h or "each" in h or "price" in h:
            unit_cents = cents
        elif "total" in h or "amount" in h or "line" in h:
            line_cents = cents

    return {
        "name": name_raw.strip(),
        "qty": int(qty_m.group(1)),
        "unit_price_cents": unit_cents,
        "line_total_cents": line_cents,
        "dietary_notes": None,  # populated on PDFs that surface per-item notes
    }


# Setup pieces section: free-text counts per category. ezCater PDFs that
# include catering setup tend to list these as "Chafing Dishes: 4" lines.
_SETUP_LABEL_TO_KEY = {
    "chafing": "chafing_dishes",
    "sterno": "sternos",
    "utensil set": "utensils_sets",
    "utensil": "utensils_sets",
    "plate": "plates",
    "napkin": "napkins",
    "cup": "cups",
    "serving utensil": "serving_utensils",
    "serving spoon": "serving_utensils",
}

_SETUP_LINE_RE = re.compile(
    r"^\s*(?:[-•*]\s*)?(\d{1,3})\s+([A-Za-z][A-Za-z\s]+?)\s*$",
)

def _extract_setup_pieces(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        m = _SETUP_LINE_RE.match(line)
        if not m:
            continue
        n = int(m.group(1))
        label = m.group(2).strip().lower()
        for needle, key in _SETUP_LABEL_TO_KEY.items():
            if needle in label:
                out[key] = out.get(key, 0) + n
                break
    return out
