from __future__ import annotations

from datetime import date, datetime
import re

_DATE_FORMATS = [
    "%B %d %Y",   # January 13 2025
    "%b %d %Y",   # Jan 13 2025
    "%B %d, %Y",  # January 13, 2025
    "%b %d, %Y",  # Jan 13, 2025
    "%m/%d/%Y",   # 01/13/2025
    "%m-%d-%Y",   # 01-13-2025
]

_DATE_FORMATS_NO_YEAR = [
    "%B %d",   # January 13
    "%b %d",   # Jan 13
]

_TIME_FORMATS = [
    "%I:%M %p",   # 11:00 AM
    "%I:%M%p",    # 11:00AM
    "%H:%M",      # 13:00 (24-hour)
    "%H:%M:%S",   # 13:00:00
]


def clean_pdf_date_text(raw_date: str) -> str:
    """
    Examples:
      'Tuesday, January 13' -> 'January 13'
      'January 13' -> 'January 13'
    """
    cleaned = raw_date.strip()

    cleaned = re.sub(
        r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    return cleaned.strip()


def infer_order_date(raw_date: str, today: date | None = None) -> str:
    """
    Convert PDF date text with no year into canonical YYYY-MM-DD.

    Business rule:
    - orders are current/upcoming only
    - assume current year
    - if that date is more than 30 days in the past, roll to next year
    """
    if today is None:
        today = date.today()

    cleaned = clean_pdf_date_text(raw_date)

    # Try formats that include a year first
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt).date()
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Try formats without a year, then infer year from business rule
    for fmt in _DATE_FORMATS_NO_YEAR:
        try:
            current_year_date = datetime.strptime(
                f"{cleaned} {today.year}", fmt + " %Y"
            ).date()
            if (current_year_date - today).days < -30:
                chosen = current_year_date.replace(year=today.year + 1)
            else:
                chosen = current_year_date
            return chosen.strftime("%Y-%m-%d")
        except ValueError:
            continue

    raise ValueError(f"Could not parse date from PDF: {raw_date!r}")


def normalize_pdf_time(raw_time: str) -> str:
    """
    Examples:
      '11:00 AM CST' -> '11:00 AM'
      '11:30 AM CST' -> '11:30 AM'
      '9:05 AM' -> '9:05 AM'
      '13:00' -> '1:00 PM'
      '5:15' -> '5:15 PM'   (catering-heuristic: bare 1-11 without an
                              explicit AM/PM marker is PM — Cenas
                              doesn't deliver between 1am and 11am
                              for catering; pre-fix this returned
                              '5:15 AM' for ezCater orders whose
                              raw time field lacked the marker,
                              Sam #846 2026-05-24)
    """
    cleaned = raw_time.strip()

    # Check for the meridiem marker BEFORE timezone stripping — the
    # tz regex below ('AM'/'PM' both match 2-4 uppercase letters) would
    # eat 'AM' off '9:05 AM' and leave bare '9:05', causing the
    # heuristic below to mis-flip it to PM.
    has_meridiem = bool(
        re.search(r"(?:^|[^A-Za-z])[AaPp][Mm](?:$|[^A-Za-z])", cleaned)
    )

    # Strip trailing timezone text like CST / CDT. Use a negative
    # lookahead so we don't eat AM/PM (also 2 uppercase letters).
    cleaned = re.sub(r"\s+(?!AM|PM|am|pm)[A-Za-z]{2,5}$", "", cleaned).strip()

    for fmt in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt)
            # Catering-PM heuristic: when the source had no AM/PM
            # marker AND the parsed hour is 1-11, treat as PM. (Hour
            # 0 = midnight = 12 AM. Hour 12 = noon = 12 PM. Hours
            # 13-23 already format unambiguously as PM.)
            if not has_meridiem and 1 <= parsed.hour <= 11:
                parsed = parsed.replace(hour=parsed.hour + 12)
            return parsed.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            continue

    raise ValueError(f"Could not parse time from PDF: {raw_time!r}")