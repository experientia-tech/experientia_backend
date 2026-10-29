"""Creative verification codes — PP-NNNN-NNNN.

PP is the first two letters of the campaign name; NNNN-NNNN is seven random digits
plus a Luhn check digit, so a single misread digit never silently matches another
task's code. The digit part (the code's "key") is unique across every campaign —
the prefix is only a human hint and never needed to tell two tasks apart.

Codes made before this format are CR-XXXX-XXXX (Crockford Base32 minus the
characters OCR most often confuses, with a Luhn mod 30 check character). They are
still read and matched, but no longer generated.
"""

from __future__ import annotations

import re
import secrets
import unicodedata

DIGITS = "0123456789"
NUMBER_LEN = 8  # 7 random digits + 1 check digit
CODE_RE = re.compile(r"^[A-Z]{2}-\d{4}-\d{4}$")
FALLBACK_PREFIX = "X"

# Legacy CR-XXXX-XXXX codes.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
PREFIX = "CR"
PAYLOAD_LEN = 7
LEGACY_RE = re.compile(r"^CR-[%s]{4}-[%s]{4}$" % (ALPHABET, ALPHABET))

# Characters outside the legacy alphabet that OCR returns for ones inside it.
_OCR_FIXES = str.maketrans({"U": "V", "O": "D", "0": "D"})
# Letters OCR returns for digits.
_DIGIT_FIXES = str.maketrans(
    {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "G": "6", "B": "8"}
)
_DASHES = str.maketrans({"–": "-", "—": "-", "_": "-", ".": "-", "=": "-", "~": "-"})

# Optional two-letter prefix; separators may be lost or doubled.
_CANDIDATE_RE = re.compile(r"(?:([A-Z]{2})[\s-]*)?([A-Z0-9]{4})[\s-]*([A-Z0-9]{4})")


def check_char(payload: str, alphabet: str = ALPHABET) -> str:
    """Luhn mod N check character for `payload` (characters from `alphabet`)."""
    n = len(alphabet)
    factor = 2
    total = 0
    for ch in reversed(payload):
        addend = factor * alphabet.index(ch)
        factor = 1 if factor == 2 else 2
        total += addend // n + addend % n
    return alphabet[(n - total % n) % n]


def campaign_prefix(name: str | None) -> str:
    """First two letters of the campaign name (accents dropped), e.g. "Nike Air" -> "NI"."""
    folded = unicodedata.normalize("NFKD", name or "").upper()
    letters = "".join(ch for ch in folded if "A" <= ch <= "Z")
    return (letters + FALLBACK_PREFIX * 2)[:2]


def code_key(code: str) -> str:
    """The NNNN-NNNN part — what makes a code unique across campaigns."""
    return code[-(NUMBER_LEN + 1):]


def format_code(chars: str, prefix: str = PREFIX) -> str:
    return f"{prefix}-{chars[:4]}-{chars[4:]}"


def generate_number() -> str:
    payload = "".join(secrets.choice(DIGITS) for _ in range(NUMBER_LEN - 1))
    return payload + check_char(payload, DIGITS)


def generate_code(prefix: str) -> str:
    return format_code(generate_number(), prefix)


def _number_valid(digits: str) -> bool:
    return (
        len(digits) == NUMBER_LEN
        and digits.isdigit()
        and check_char(digits[:-1], DIGITS) == digits[-1]
    )


def _legacy_valid(chars: str) -> bool:
    return (
        len(chars) == PAYLOAD_LEN + 1
        and all(c in ALPHABET for c in chars)
        and check_char(chars[:-1]) == chars[-1]
    )


def is_valid(code: str) -> bool:
    code = code or ""
    if CODE_RE.match(code):
        return _number_valid(code[3:7] + code[8:12])
    if LEGACY_RE.match(code):
        return _legacy_valid(code[3:7] + code[8:12])
    return False


def _interpretations(prefix: str | None, chars: str) -> list[str]:
    """Every checksum-valid code `chars` (8 characters, no separators) can be read as."""
    out = []
    digits = chars.translate(_DIGIT_FIXES)
    if _number_valid(digits):
        out.append(format_code(digits, prefix or "??"))
    legacy = chars.translate(_OCR_FIXES)
    if _legacy_valid(legacy):
        out.append(format_code(legacy))
    return out


def normalize(raw: str, prefix: str | None = None) -> str | None:
    """Clean a single candidate (e.g. a QR payload or typed code) into a valid code, or None.

    `prefix` fills in a missing prefix (e.g. the campaign's when a manager types digits only).
    """
    text = (raw or "").upper().translate(_DASHES)
    chars = re.sub(r"[\s-]+", "", text)
    read_prefix = None
    if len(chars) == NUMBER_LEN + 2 and chars[:2].isalpha():
        read_prefix, chars = chars[:2], chars[2:]
    if len(chars) != NUMBER_LEN:
        return None
    legacy_ok = read_prefix in (None, PREFIX)
    # Exact reads first, so a legacy code is never re-read as digits (or vice versa).
    if _number_valid(chars):
        return format_code(chars, read_prefix or prefix or "??")
    if legacy_ok and _legacy_valid(chars):
        return format_code(chars)
    digits = chars.translate(_DIGIT_FIXES)
    if _number_valid(digits):
        return format_code(digits, read_prefix or prefix or "??")
    legacy = chars.translate(_OCR_FIXES)
    return format_code(legacy) if legacy_ok and _legacy_valid(legacy) else None


def extract_codes(text: str) -> list[str]:
    """All checksum-valid codes found in free text (OCR output), in order, deduped.

    A code whose prefix was not read comes back as ??-NNNN-NNNN; match on `code_key`.
    """
    cleaned = (text or "").upper().translate(_DASHES)
    found: list[str] = []
    pos = 0
    while (m := _CANDIDATE_RE.search(cleaned, pos)) is not None:
        prefix, body = m.group(1), m.group(2) + m.group(3)
        codes = _interpretations(prefix, body)
        if not codes and prefix:
            # The "prefix" may have been the first two characters of an unprefixed code.
            codes = _interpretations(None, (prefix + body)[:NUMBER_LEN])
        for code in codes:
            if code not in found:
                found.append(code)
        # Resume right after a hit; otherwise retry one character on, so a noisy
        # leading character can't swallow a real code.
        pos = m.end() if codes else m.start() + 1
    return found
