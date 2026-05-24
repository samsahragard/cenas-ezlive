"""ezcater_order_details table (Step 2 of Sam #530 ezCater PDF pipeline)

Revision ID: 38_ezcater_order_details
Revises: 37_sam_chat_todos
Create Date: 2026-05-23

Per Sam #530 + Cena #534 field-list lock. Two-step pipeline: Step 1
(ck's lane) downloads ezCater order PDFs via Playwright into a local
archive; Step 2 (aick's lane, this migration + ezcater_extractor.py +
ezcater_get_order_full_details Cena tool) parses each PDF with
pdfplumber and writes the structured fields the orders/order_items
tables do NOT already have.

What lands here vs already-in-DB (Cena #534 split):
  - orders / order_items already have: phone, address, delivery
    instructions free-text, headcount, totals, tip, fee, food_total,
    line items (qty, name).
  - PDF-only fields landing here:
      * per-item prices (order_items has qty + name, no price)
      * setup-piece counts (chafing dishes / sternos / utensils /
        plates / napkins / cups — none of these in DB today)
      * per-item dietary notes
      * day-of contact name + phone (sometimes differs from billing)
      * gate codes (sometimes separate from delivery_instructions)
      * customer special-instructions free-text block
      * ezCater fee breakdown (commission + service fee + processing
        fee — orders.fee is the combined total only)

Schema kept separate from orders (Cena #534: "don't bolt onto orders,
keep PDF-derived fields separate from API-authoritative ones"). One
row per external_order_id; UPSERT-by-order_id on re-extraction.

Render note: matches migrations 8-35 convention — alembic isn't wired
on the live Render service. Actual CREATE TABLE happens via the
idempotent boot-time table-backfill in app/__init__.py (table-presence
gated metadata.create_all subset).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "38_ezcater_order_details"
down_revision: Union[str, Sequence[str], None] = "37_sam_chat_todos"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ezcater_order_details",
        sa.Column("id", sa.Integer(), primary_key=True),

        # Join key back to orders.external_order_id (NOT a hard FK —
        # PDFs can land for orders that arrived via different paths and
        # we don't want to lose extraction data if the orders row gets
        # restructured. Unique index gives us UPSERT semantics on
        # re-extraction of the same PDF).
        sa.Column("external_order_id", sa.String(100), nullable=False,
                  unique=True, index=True),

        # ---- PDF-only fields per Cena #534 ----

        # Per-item line items WITH prices. JSON array of objects:
        #   [{"name": "...", "qty": 12, "unit_price_cents": 1295,
        #     "line_total_cents": 15540, "dietary_notes": "gluten free"}]
        # JSON not a separate items table because (a) it's PDF-extracted
        # ground truth that may have free-text quirks, (b) order_items
        # is already the structured representation from the API and we
        # don't want a join-conflict. Cena tool merges both views.
        sa.Column("items_json", sa.Text(), nullable=True),

        # Setup pieces — counts of each kind. Free-text JSON because
        # ezCater PDFs vary in what they enumerate:
        #   {"chafing_dishes": 4, "sternos": 8, "utensils_sets": 25,
        #    "plates": 25, "napkins": 25, "cups": 25,
        #    "serving_utensils": 5}
        # Empty-{} when PDF has no setup section.
        sa.Column("setup_pieces_json", sa.Text(), nullable=True),

        # Customer-facing free-text fields PDFs surface that the API
        # often truncates or omits.
        sa.Column("special_instructions", sa.Text(), nullable=True),
        sa.Column("gate_code", sa.String(120), nullable=True),

        # Day-of contact (sometimes differs from billing customer).
        sa.Column("day_of_contact_name", sa.String(160), nullable=True),
        sa.Column("day_of_contact_phone", sa.String(40), nullable=True),

        # ezCater fee breakdown — orders.fee is the combined total
        # only. PDF itemizes commission vs service vs processing.
        # Stored as cents (integer) to avoid float drift.
        sa.Column("commission_cents", sa.Integer(), nullable=True),
        sa.Column("service_fee_cents", sa.Integer(), nullable=True),
        sa.Column("processing_fee_cents", sa.Integer(), nullable=True),

        # ---- Provenance ----

        # Where the source PDF lives (so re-extraction is reproducible
        # and audit-trail-able). Full Windows path on aick.
        sa.Column("source_pdf_path", sa.String(500), nullable=True),

        # SHA256 of the source PDF so we can detect when ezCater
        # updates a previously-archived PDF and re-extract (otherwise
        # idempotent skip would miss real changes).
        sa.Column("source_pdf_sha256", sa.String(64), nullable=True),

        # Extractor version that produced this row. Lets us schema-
        # migrate extractor logic without re-extracting everything —
        # we can backfill only rows below a given version.
        sa.Column("extractor_version", sa.String(20), nullable=True,
                  server_default=sa.text("'1'")),

        # If the parse failed (PDF too garbled / format change), keep
        # the order_id row anyway with this set to the error class
        # so we don't infinite-retry. NULL on success.
        sa.Column("parse_error", sa.Text(), nullable=True),

        sa.Column("extracted_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index(
        "ix_ezcater_order_details_extracted_at",
        "ezcater_order_details", ["extracted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ezcater_order_details_extracted_at",
                  table_name="ezcater_order_details")
    op.drop_table("ezcater_order_details")
