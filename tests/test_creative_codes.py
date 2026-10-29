import re

from app.services.creative_codes import (
    ALPHABET,
    DIGITS,
    campaign_prefix,
    check_char,
    code_key,
    extract_codes,
    format_code,
    generate_code,
    is_valid,
    normalize,
)


def _code(payload: str, prefix: str = "NI") -> str:
    return format_code(payload + check_char(payload, DIGITS), prefix)


def _legacy(payload: str) -> str:
    return format_code(payload + check_char(payload))


def test_alphabet_excludes_ambiguous_characters():
    assert len(ALPHABET) == 30
    assert not set("0O1ILU") & set(ALPHABET)


def test_campaign_prefix_is_first_two_letters():
    assert campaign_prefix("Nike Air Max") == "NI"
    assert campaign_prefix("  coca-cola") == "CO"
    assert campaign_prefix("7Up Summer") == "UP"  # digits and spaces skipped
    assert campaign_prefix("Été 2026") == "ET"  # accents dropped
    assert campaign_prefix("Q") == "QX"
    assert campaign_prefix("") == "XX"
    assert campaign_prefix(None) == "XX"


def test_generated_codes_are_well_formed_and_valid():
    codes = {generate_code("NI") for _ in range(2000)}
    assert len(codes) > 1990  # 10^7 numbers; a rare birthday collision is fine here
    for code in codes:
        assert re.fullmatch(r"NI-\d{4}-\d{4}", code)
        assert is_valid(code)
        assert code_key(code) == code[3:]


def test_checksum_catches_every_single_digit_error():
    for _ in range(25):
        code = generate_code("AB")
        digits = code[3:7] + code[8:]
        for pos in range(len(digits)):
            for ch in DIGITS:
                if ch == digits[pos]:
                    continue
                mutated = digits[:pos] + ch + digits[pos + 1 :]
                assert not is_valid(format_code(mutated, "AB")), (code, mutated)


def test_is_valid_rejects_malformed():
    assert not is_valid("")
    assert not is_valid("NI-1234-567")
    assert not is_valid("N1-1234-5678")
    assert not is_valid(generate_code("NI").lower())


def test_legacy_codes_stay_valid():
    code = _legacy("HK7R3MW")
    assert is_valid(code)
    assert normalize(code.lower()) == code
    assert extract_codes(f"... {code} ...") == [code]
    assert code_key(code) == code[3:]


def test_normalize_cleans_case_spacing_and_dashes():
    code = generate_code("NI")
    assert normalize(code.lower()) == code
    assert normalize(code.replace("-", " ")) == code
    assert normalize(code.replace("-", "—")) == code
    assert normalize(code[3:], "NI") == code  # prefix filled in from the campaign
    assert normalize(code[3:]) == "??-" + code[3:]
    assert normalize("NI-1234") is None


def test_normalize_maps_ocr_confusions_to_digits():
    code = _code("0123456")
    assert normalize("NI-O1Z3-" + code[8:]) == code  # O read for 0, Z for 2
    assert normalize("NI-0IZ3-" + code[8:]) == code  # I read for 1


def test_extract_codes_from_noisy_ocr_text():
    code = generate_code("NI")
    a, b = code[3:7], code[8:]
    assert extract_codes(f"SHOP NOW\n{code}\nwww") == [code]
    assert extract_codes(f"NI {a} {b}") == [code]  # dashes lost
    assert extract_codes(f"NI{a}{b}") == [code]
    assert extract_codes(f"x{code}") == [code]  # stray character glued on
    found = extract_codes(f"{a}-{b}")  # prefix not read
    assert [code_key(c) for c in found] == [code_key(code)]
    assert extract_codes("nothing to see here") == []
