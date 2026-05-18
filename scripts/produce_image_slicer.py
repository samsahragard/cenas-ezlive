"""Slice a produce vendor catalog grid image into per-item photo crops.

Built by dck for the produce mobile redesign v1.2 photo-population pass
(Sam #2663 / cena #2666). Handed off to ck for the full-catalog wire-up
per cena #2668.

Background
----------
Sam supplied two reference images (chat attachments 105 + 106) showing a
commercial produce vendor catalog: ~57 items per image laid out in a grid,
each cell with a label above + product photo below on a uniform white
backdrop. Style across the full set matches cena #2638 (top-down, even
diffuse light, soft shadow, centered crop) — usable directly as our
production photo source, no AI generation needed.

The grid is NOT uniform: top 3 rows are 7-col, bottom 4 rows are 9-col.
This script detects row + column boundaries dynamically (rather than
assuming a fixed N×M grid), then crops each cell and the photo region
within it.

Source catalog has known label/photo mismatches in some middle-row cells
(e.g. label says "Roma Tomatoes" but photo is cilantro). Per Sam #2651
("find the correct fruit for the correct item"), MATCH BY PHOTO CONTENT,
not by source label text. The slug-mapping step requires visual ID per
cell — don't auto-derive slugs from labels.

Usage
-----
    from produce_image_slicer import slice_catalog, extract_photo_square

    # Slice both reference images into per-cell PNGs
    cells = slice_catalog('path/to/ref_image.png', out_dir='slices/')
    # cells -> list of dicts {row, col, x0, y0, x1, y1, path}

    # For one verified slug -> cell match, extract the square photo
    extract_photo_square('slices/ref105_r6_c2.png', 'avocado.jpg',
                         out_size=240, jpg_quality=90)

The 4 sample matches dck shipped in v1.2 (7b49de7):
    avocado       <- ref105 r6 c2  (label: "Avocados (Small)")
    tomato-roma   <- ref105 r2 c2  (label: "Tomates Roma (25lb)")
    onion-yellow  <- ref105 r0 c4  (label: "Onion Yel. Jumbo (50lb)")
    cilantro      <- ref105 r3 c1  (label: "Roma Tomatoes" — MISLABEL,
                                    photo is cilantro)
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from PIL import Image


def _white_bands(arr_1d: np.ndarray, thresh: float, min_h: int = 3) -> list[tuple[int, int]]:
    """Return [(start, end)] for runs of indices where arr_1d >= thresh."""
    white = np.where(arr_1d >= thresh)[0]
    if not len(white):
        return []
    bands: list[tuple[int, int]] = []
    start = prev = int(white[0])
    for i in white[1:]:
        i = int(i)
        if i - prev > 1:
            bands.append((start, prev))
            start = i
        prev = i
    bands.append((start, prev))
    return [b for b in bands if b[1] - b[0] + 1 >= min_h]


def _cells_from_bands(bands: Sequence[tuple[int, int]], end: int) -> list[tuple[int, int]]:
    """Convert gutter bands to cell (start, end) ranges. The last cell runs to `end`."""
    cells: list[tuple[int, int]] = []
    prev = 0
    for b in bands:
        cells.append((prev, b[0]))
        prev = b[1] + 1
    cells.append((prev, end))
    return cells


def detect_row_cells(arr_gray: np.ndarray, thresh: float = 250, min_h: int = 3) -> list[tuple[int, int]]:
    """Detect cell (y0, y1) ranges by finding horizontal white-gutter bands."""
    bands = _white_bands(arr_gray.mean(axis=1), thresh, min_h)
    return _cells_from_bands(bands, arr_gray.shape[0])


def detect_col_cells_in_strip(
    arr_gray: np.ndarray, y0: int, y1: int, thresh: float = 248, min_h: int = 3
) -> list[tuple[int, int]]:
    """Detect cell (x0, x1) ranges WITHIN a row-strip by vertical white-gutter bands.

    Use this per-row because the catalog has non-uniform column counts across rows.
    """
    strip = arr_gray[y0:y1, :]
    bands = _white_bands(strip.mean(axis=0), thresh, min_h)
    return _cells_from_bands(bands, arr_gray.shape[1])


def slice_catalog(image_path: str | Path, out_dir: str | Path, prefix: str | None = None) -> list[dict]:
    """Slice a catalog image into per-cell PNGs. Returns one dict per cell.

    The output dict shape (one entry per cell):
        {row, col, x0, y0, x1, y1, path}
    """
    image_path = Path(image_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = prefix or image_path.stem

    im = Image.open(image_path)
    arr = np.array(im.convert("L"))
    rows = detect_row_cells(arr)

    out: list[dict] = []
    for ri, (y0, y1) in enumerate(rows):
        cols = detect_col_cells_in_strip(arr, y0, y1)
        for ci, (x0, x1) in enumerate(cols):
            cell = im.crop((x0, y0, x1, y1))
            p = out_dir / f"{prefix}_r{ri}_c{ci}.png"
            cell.save(p)
            out.append({"row": ri, "col": ci, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "path": str(p)})
    return out


def extract_photo_square(
    cell_path: str | Path,
    out_path: str | Path,
    out_size: int = 240,
    jpg_quality: int = 90,
    label_band_pct: float = 0.25,
) -> None:
    """Extract the square photo region from a single cell crop and save as JPG.

    The top `label_band_pct` of the cell is treated as the label band and skipped.
    The remaining photo region is centre-square-cropped and upscaled to `out_size`
    (default 240 for 2× retina at 96×96 display in .pcard-photo).
    """
    cell = Image.open(cell_path)
    w, h = cell.size
    photo = cell.crop((0, int(h * label_band_pct), w, h))
    pw, ph = photo.size
    side = min(pw, ph)
    left = (pw - side) // 2
    top = (ph - side) // 2
    sq = photo.crop((left, top, left + side, top + side))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sq.resize((out_size, out_size), Image.LANCZOS).convert("RGB").save(
        out_path, "JPEG", quality=jpg_quality
    )


def batch_extract(
    matches: dict[str, tuple[str, int, int]],
    cells_by_image: dict[str, list[dict]],
    out_dir: str | Path,
) -> None:
    """Convenience: given {slug: (image_key, row, col)} mappings + slice results,
    write per-slug JPGs into `out_dir`.

    Example:
        cells_105 = slice_catalog('ref105.png', 'slices/')
        cells_106 = slice_catalog('ref106.png', 'slices/')
        matches = {'avocado': ('ref105', 6, 2), 'cilantro': ('ref105', 3, 1)}
        batch_extract(matches, {'ref105': cells_105, 'ref106': cells_106},
                      'app/static/img/produce/')
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for slug, (key, ri, ci) in matches.items():
        cell = next(
            c for c in cells_by_image[key] if c["row"] == ri and c["col"] == ci
        )
        extract_photo_square(cell["path"], out_dir / f"{slug}.jpg")
