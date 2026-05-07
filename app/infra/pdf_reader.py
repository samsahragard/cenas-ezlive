from __future__ import annotations

import base64
import io
import logging
import os
import time
from typing import Any, cast

import pypdfium2 as pdfium  # statically-linked PDFium, no VC++ runtime dep
from PIL import Image

import anthropic
from pydantic import TypeAdapter

from app.config import Config
from app.domain.schemas import RawOrder

logger = logging.getLogger(__name__)

_CLAUDE_MAX_RETRIES = 3
_CLAUDE_RETRY_DELAY = 2  # seconds between retries

MAX_PDF_SIZE_BYTES = 30 * 1024 * 1024  # 30 MB
MAX_PDF_PAGES = 20
_REQUIRED_FIELDS = (
    "order_id",
    "store",
    "date",
    "deliver_at",
    "headcount",
    "delivery_address",
    "raw_items",
)


def crop_address_region(page_image: Image.Image) -> Image.Image:
    """
    Crops and upscales the delivery address block from page 1.
    """
    w, h = page_image.size
    # Approximate bounding box of the DELIVER TO section
    left = int(w * 0.00)
    top = int(h * 0.28)
    right = int(w * 0.45)
    bottom = int(h * 0.52)

    cropped = page_image.crop((left, top, right, bottom))
    return cropped.resize((cropped.width * 2, cropped.height * 2), Image.LANCZOS)


def get_pdf_as_images(pdf_path: str, dpi: int = 300) -> list[Image.Image]:
    """
    Converts a PDF into high-resolution PIL images using pypdfium2.
    No Poppler required; PDFium is bundled and statically linked.
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    file_size = os.path.getsize(pdf_path)
    if file_size > MAX_PDF_SIZE_BYTES:
        raise ValueError(
            f"PDF too large: {file_size / 1024 / 1024:.1f} MB (max {MAX_PDF_SIZE_BYTES // 1024 // 1024} MB)"
        )

    doc = pdfium.PdfDocument(pdf_path)
    try:
        if len(doc) > MAX_PDF_PAGES:
            raise ValueError(f"PDF has {len(doc)} pages (max {MAX_PDF_PAGES})")

        images: list[Image.Image] = []
        zoom = dpi / 72  # 72 DPI is PDFium's default rendering resolution

        for page in doc:
            try:
                bitmap = page.render(scale=zoom)
                images.append(bitmap.to_pil().convert("RGB"))
            finally:
                page.close()
    finally:
        doc.close()

    return images


def _img_to_b64_png(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


def _image_block(img: Image.Image) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": _img_to_b64_png(img),
        },
    }


# Derive the JSON schema for RawOrder from the TypedDict so it stays in sync
# if schemas.py changes. Anthropic accepts JSON Schema (incl. $defs / $ref).
_RAW_ORDER_SCHEMA: dict[str, Any] = TypeAdapter(RawOrder).json_schema()

_TOOL = {
    "name": "submit_order_data",
    "description": (
        "Submit the structured order data extracted from the catering order "
        "PDF. Call this tool exactly once with the RawOrder fields populated "
        "from what is literally printed on the PDF."
    ),
    "input_schema": _RAW_ORDER_SCHEMA,
}


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not Config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        _client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    return _client


def extract_order_from_pdf(pdf_images: list[Image.Image]) -> RawOrder:
    """
    Sends PDF pages (as images) to Claude to extract structured JSON via the
    `submit_order_data` tool.
    """

    prompt = """
        You are a catering order data extraction agent. Your ONLY job is to TRANSCRIBE what is printed on the PDF — you must never calculate, infer, derive, or "fix" any value. All downstream calculation is handled by the application.

        ══ DO NOT COMPUTE (critical) ══
        - **Date / Year**: Copy the date field EXACTLY as printed. If no year appears on the PDF, do NOT add or guess one — leave it out (e.g. return "January 13", not "January 13, 2026"). Year inference is handled by the application. Never put a year in 'extraction_notes' either.
        - **QTY**: Copy each quantity exactly as the digit(s) printed in the QTY column. Do NOT adjust, round, recalculate, or verify any quantity — not even if you think a number looks wrong.
        - **Tableware sub-quantities**: Set the Tableware item qty=1 (always). List sub-components as line items exactly as printed. Do NOT compute, sum, or verify sub-component quantities.
        - **Routing / drivers / store assignment**: Do NOT make any routing, driver, or location-assignment decisions. Only transcribe what is literally printed.

        ══ EXTRACTION RULES ══
        1. **Numbers**: Read all numbers visually and carefully, twice. If any number is unclear or blurry, downgrade confidence and note in 'extraction_notes'.
        2. **QTY column**: A multi-line item's QTY may appear above or below the item name — always associate the nearest QTY column value with its item.
        3. **raw_items**: Extract ALL line items that have a QTY. None may be skipped.
        4. **Line items**: Extract all bullet points / sub-lines under each menu item.
        5. **Most Popular**: If "Beans: Most Popular" appears, record as "Most Popular"; same for other line items.
        6. **Headcount**: Integer only.
        7. **Delivery time**: Capture the exact time and any time window.
        8. **Item names**: Copy exactly as written — never shorten (e.g. "Chicken Fajita Party Package", not "Chicken Fajita"). Preserve all qualifiers (Party Package, Tray (1 Dozen), Per Pound, etc.).
        9. **Tableware**: Collapse any Tableware section into one item (alias "Tableware", qty=1). List every sub-component as a line item exactly as it appears on the PDF.
        10. **Dressings/Sauces**: For salads use "Dressing: <choice>"; for enchiladas use "Sauce: <choice>" as a line item.
        11. **Notes**: Capture any free-text notes printed to the right of or below the Tableware section. If no notes are present, omit the field or return null.
        12. **Delivery address**: The last image is a zoomed crop of the address block — use it to verify all street numbers and zip code digits.
        13. **Confidence**:
            - `high`: crisp PDF, all fields clearly legible, no assumptions made.
            - `medium`: any field required visual inference, slight blur, or any assumption.
            - `low`: blurry/smudged/cropped PDF, or multiple uncertain fields.
            - Populate `uncertain_fields` with any field names you are unsure about.
            - Use `extraction_notes` for image quality issues or ambiguous values. Omit if none.

        Call the `submit_order_data` tool exactly once with the RawOrder fields filled in.
    """

    address_crop = crop_address_region(pdf_images[0])

    image_blocks = [_image_block(img) for img in pdf_images]
    image_blocks.append(_image_block(address_crop))

    user_content: list[dict[str, Any]] = [{"type": "text", "text": prompt}, *image_blocks]

    client = _get_client()

    last_error: Exception = RuntimeError("Claude extraction failed before any attempt")
    parsed: dict[str, Any] | None = None

    for attempt in range(1, _CLAUDE_MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=Config.ANTHROPIC_MODEL,
                max_tokens=Config.ANTHROPIC_MAX_TOKENS,
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": "submit_order_data"},
                messages=[{"role": "user", "content": user_content}],
            )
        except Exception as e:
            last_error = e
            if attempt < _CLAUDE_MAX_RETRIES:
                logger.warning(
                    "Claude attempt %d failed: %s. Retrying in %ds...",
                    attempt, e, _CLAUDE_RETRY_DELAY,
                )
                time.sleep(_CLAUDE_RETRY_DELAY)
            continue

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "submit_order_data":
                parsed = block.input  # type: ignore[union-attr]
                break

        if parsed is not None:
            break

        last_error = ValueError(
            f"Claude returned no tool_use block for submit_order_data. "
            f"stop_reason={getattr(response, 'stop_reason', None)!r}"
        )
        if attempt < _CLAUDE_MAX_RETRIES:
            logger.warning("Claude attempt %d returned no tool call. Retrying in %ds...", attempt, _CLAUDE_RETRY_DELAY)
            time.sleep(_CLAUDE_RETRY_DELAY)

    if parsed is None:
        raise RuntimeError(
            f"Claude API failed after {_CLAUDE_MAX_RETRIES} attempts: {last_error}"
        ) from last_error

    missing = [f for f in _REQUIRED_FIELDS if not parsed.get(f)]
    if missing:
        raise ValueError(f"Claude response missing required fields: {', '.join(missing)}")

    if not isinstance(parsed.get("raw_items"), list) or len(parsed["raw_items"]) == 0:
        raise ValueError("Claude response contains no order items (raw_items empty or missing)")

    headcount = parsed.get("headcount")
    if not isinstance(headcount, int) or headcount <= 0:
        raise ValueError(f"Invalid headcount from Claude: {headcount!r} (must be a positive integer)")

    return cast(RawOrder, parsed)
