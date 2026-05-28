"""Seed roster of ezCater drivers from Sam's 5/10 screenshots.

These are the drivers Sam recognizes as legitimate ezCater couriers, with
their phone numbers (digits-only / E.164 without +1) so we can match
against Driver.phone on signup. ck_prefix tells us their home kitchen:
  1 = Copperfield (UNO MAS)
  2 = Tomball     (DOS MAS)
  None = ambiguous (no prefix in source roster; verify with Sam later)

Bootstrapped into ezcater_known_driver on app start (idempotent — only
inserts rows for phones not already present). Manager can add / edit
later via a future admin page.
"""
from __future__ import annotations

import re
import unicodedata


def _norm(raw_phone: str) -> str:
    """Phone in (XXX) XXX-XXXX → 'XXXXXXXXXX'. Used both at seed time and
    when matching incoming Driver.phone. Strip everything but digits, drop
    a leading '1' if present so the comparison is consistent."""
    digits = "".join(ch for ch in (raw_phone or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


# (name, raw phone string from Sam's screenshots, ck_prefix)
_RAW_ROSTER: list[tuple[str, str, int | None]] = [
    # ===== CK #1 (Copperfield / UNO MAS) =====
    ("Alejandro Martinez",      "(832) 305-1769", 1),
    ("Amy Luu",                 "(832) 348-3265", 1),
    ("Cristopher Gordillo",     "(305) 310-1498", 1),
    ("Jose Gonzalez",           "(346) 640-8074", 1),
    ("Abdiel Martinez",         "(832) 891-1370", 1),
    ("ANA ISA Perez",           "(346) 539-9154", 1),
    ("Andres Rivera",           "(832) 869-9609", 1),
    ("ARIANNA MENENDEZ",        "(786) 835-3317", 1),
    ("BRAYAN LOZANO",           "(832) 805-5705", 1),
    ("BRITNEY MATHIS",          "(832) 343-9767", 1),
    ("Bryan Ruiz",              "(832) 975-5605", 1),
    ("Daritza Pitty Pitty",     "(832) 938-8314", 1),
    ("Francis Medina",          "(832) 886-7449", 1),
    ("Jessica Sanchez",         "(346) 395-9021", 1),
    ("Jose Rivero",             "(832) 871-6959", 1),
    ("Kenia Garcia",            "(281) 736-0426", 1),
    ("Lourdes Perez",           "(346) 697-7843", 1),
    ("Roy Roberto Echarte Vila","(786) 760-8422", 1),
    ("Sonny Sangolana",         "(346) 580-3462", 1),
    ("Yordani Sosa",            "(646) 920-0918", 1),
    # ===== CK #2 (Tomball / DOS MAS) =====
    ("Anibal Medina",           "(346) 578-8593", 2),
    ("Gina Buritica",           "(475) 276-9760", 2),
    ("Tatiana Campos",          "(346) 468-9339", 2),
    ("Victor Lopez",            "(346) 450-2455", 2),
    ("Alejandro Medina",        "(832) 874-4960", 2),
    ("Bryan Estrada",           "(281) 746-0288", 2),
    ("Ester Reyes",             "(832) 948-8952", 2),
    ("Javier Cruz",             "(346) 634-9994", 2),
    ("Jesus Figueroa",          "(305) 930-5296", 2),
    ("Kenia Becerra",           "(346) 970-0630", 2),
    ("Leandro Jose Barreto",    "(346) 652-2044", 2),
    ("Veronica Sigaran",        "(281) 714-8830", 2),
    ("Joel Ojeda",              "(936) 730-8730", 2),
    ("Janeth Arvizy",           "(832) 541-1871", 2),
    ("Oscar Rodriguez",         "(754) 214-4882", 2),
    # ===== Ambiguous (no CK# prefix — pending Sam confirmation) =====
    ("Angelica Truss",          "(832) 920-9404", None),
    ("James Paddie",            "(832) 855-2337", None),
]


def seed_roster() -> list[dict]:
    """Normalized list of {name, phone_e164, ck_prefix} dicts ready for
    insertion into ezcater_known_driver."""
    return [
        {"name": name, "phone_e164": _norm(phone), "ck_prefix": prefix}
        for name, phone, prefix in _RAW_ROSTER
    ]


def normalize_phone(raw: str) -> str:
    """Public helper used by login + admin matching to compare against
    seeded phone_e164 values. Matches the same normalization used at seed."""
    return _norm(raw)


def fold_name(name: str) -> str:
    """Normalize a driver name for fuzzy matching: strip accents
    (Rodríguez -> rodriguez), drop punctuation, collapse whitespace,
    lowercase. Used to compare ezCater-roster spellings against the
    free-typed names in the local Driver table."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _within_one_edit(a: str, b: str) -> bool:
    """True if a and b differ by at most one single-character edit
    (substitution, insertion, or deletion). Levenshtein <= 1."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    if abs(la - lb) != 1:
        return False
    if la > lb:
        a, b, la, lb = b, a, lb, la  # a is now the shorter one
    i = j = 0
    skipped = False
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            if skipped:
                return False
            skipped = True
            j += 1  # consume the extra char in the longer string
    return True


def names_match(a: str, b: str) -> bool:
    """True if two driver names are the same person modulo accents,
    case, spacing/punctuation, and a single-character typo. Catches the
    Buritica/Buritiga, Arvizy/Arvizu, Rodriguez/Rodríguez variants Sam
    flagged (#1431). Conservative — only collapses a 1-edit difference,
    not arbitrary fuzziness."""
    fa, fb = fold_name(a), fold_name(b)
    if not fa or not fb:
        return False
    return _within_one_edit(fa, fb)
