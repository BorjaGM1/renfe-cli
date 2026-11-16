"""The personal-data check (.githooks/pii_check.py): patterns, known values, modes, agent hook, git hooks.

No network: the model layer is stubbed, and urlopen fails the test if anything reaches it. Every
PII-shaped value is generated at runtime (fake_digits, fake_locator, fake_card) or is an obviously
synthetic shape, so this file never holds a realistic value and passes the check itself.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".githooks"
GMAIL = "gm" + "ail.com"


def load():
    spec = importlib.util.spec_from_file_location("pii_check", HOOKS / "pii_check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pii_check"] = module  # dataclasses look the module up while it executes
    spec.loader.exec_module(module)
    return module


PC = load()  # for the value generators below; each test gets a fresh module from the `pc` fixture


@pytest.fixture
def pc(monkeypatch, tmp_path):
    """A fresh module (empty caches), no needles or values file, no API key, no `claude` CLI, no network."""
    module = load()
    monkeypatch.setenv(module.NEEDLES_ENV, str(tmp_path / "no-needles.json"))
    monkeypatch.setenv(module.VALUES_ENV, str(tmp_path / "no-values.json"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(module, "_api_key", lambda: None)
    monkeypatch.setattr(module, "_claude_cli", lambda: None)

    def no_network(*args, **kwargs):
        raise AssertionError("the tests must not reach the network")

    monkeypatch.setattr(module.urllib.request, "urlopen", no_network)
    return module


def fake_digits(n: int, seed: int) -> str:
    """n invented digits that aren't an obviously synthetic shape, so the pattern layer must flag them."""
    rng = random.Random(seed)
    while True:
        digits = "".join(rng.choice("0123456789") for _ in range(n))
        if not PC.synthetic(digits):
            return digits


def fake_locator(seed: int) -> str:
    """An invented six-character locator of letters and digits that isn't a placeholder shape."""
    rng = random.Random(seed)
    while True:
        token = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        if sum(c.isalpha() for c in token) >= 2 and any(c.isdigit() for c in token) and PC._code_number_personal(token):
            return token


def fake_letters(seed: int) -> str:
    """Six invented capitals that aren't a placeholder: a letter-only locator."""
    rng = random.Random(seed)
    while True:
        token = "".join(rng.choice("BCDFGHJKLMNPQRSTVWXZ") for _ in range(6))
        if PC._locator_personal(PC.LOCATOR.search(f"localizador {token}"), f"localizador {token}"):
            return token


def fake_card(prefix: str, length: int, seed: int) -> str:
    """An invented Luhn-valid card number that isn't a gateway test card or a synthetic shape."""
    rng = random.Random(seed)
    while True:
        body = prefix + "".join(rng.choice("0123456789") for _ in range(length - len(prefix) - 1))
        card = next(body + str(d) for d in range(10) if PC.luhn(body + str(d)))
        if PC._card_personal(card):
            return card


def with_nif_letter(number: str) -> str:
    return number + "TRWAGMYFPDXBNJZSQVHLCKE"[int(number) % 23]


def iban_es(bban: str) -> str:
    rearranged = "".join(str(int(c, 36)) for c in bban + "ES00")
    return f"ES{98 - int(rearranged) % 97:02d}{bban}"


def ccc(entity: str, office: str, account: str) -> str:
    return f"{entity}{office}{PC._ccc_digit('00' + entity + office)}{PC._ccc_digit(account)}{account}"


def grouped(digits: str, sizes: tuple[int, ...], sep: str = " ") -> str:
    out, i = [], 0
    for size in sizes:
        out.append(digits[i:i + size])
        i += size
    return sep.join(out)


def iban_with_card_slices(seed: int) -> str:
    """A valid ES IBAN whose grouped digits hold two Luhn-valid 16-digit card-shaped slices."""
    rng = random.Random(seed)
    while True:
        bban = "".join(rng.choice("0123456789") for _ in range(20))
        if (bban[0] in "23456" and bban[4] in "23456" and PC.luhn(bban[:16]) and PC.luhn(bban[4:])
                and PC._iban_personal(iban_es(bban))):
            return iban_es(bban)


def luhn_date_pair() -> str:
    """Two dates whose 16 digits together pass Luhn: only the card shapes keep them from flagging."""
    return next(s for s in (f"2026-11-{a:02d} 2026-12-{b:02d}" for a in range(1, 29) for b in range(1, 29))
                if PC.luhn(re.sub(r"\D", "", s)) and not PC.synthetic(re.sub(r"\D", "", s)))


def luhn_datetime() -> str:
    """A compact 20YYMMDDhhmmss timestamp that passes Luhn: the 2-series card range keeps it out."""
    return next(s for s in (f"2026{m:02d}{d:02d}{h:02d}3000" for m in range(1, 13) for d in range(1, 29)
                            for h in range(24)) if PC.luhn(s) and not PC.synthetic(s))


def fake_luhn(prefix: str, length: int, seed: int) -> str:
    """Luhn-valid digits with this prefix, whatever the issuer range."""
    rng = random.Random(seed)
    while True:
        body = prefix + "".join(rng.choice("0123456789") for _ in range(length - len(prefix) - 1))
        digits = next(body + str(d) for d in range(10) if PC.luhn(body + str(d)))
        if not PC.synthetic(digits):
            return digits


MOBILE = "6" + fake_digits(8, 1)
LANDLINE = "91" + fake_digits(7, 2)
NIF = with_nif_letter(fake_digits(8, 3))
NIE_DIGITS = fake_digits(7, 4)
NIE = "Y" + NIE_DIGITS + "TRWAGMYFPDXBNJZSQVHLCKE"[int("1" + NIE_DIGITS) % 23]
IBAN = iban_es(fake_digits(20, 5))
CCC_NUMBER = ccc(fake_digits(4, 6), fake_digits(4, 7), fake_digits(10, 8))
US_LINE = fake_digits(4, 10)
EMAIL = f"ana.ejemplo.{fake_digits(3, 11)}@{GMAIL}"
LOCATOR = fake_locator(14)
LETTERS = fake_letters(23)
BOOKING = "4" + fake_digits(9, 15)
CARD = fake_card("4", 16, 16)
AMEX = fake_card("37", 15, 17)
SHA = hashlib.sha1(b"renfe-cli").hexdigest()
SYNTHETIC_CARD = next(f"4{'1234567890'}1234{d}" for d in range(10) if PC.luhn(f"4{'1234567890'}1234{d}"))  # 123456...
FLAGGED = {
    "mobile": f"llama al {MOBILE}",
    "mobile 3-3-3": f"tel. {MOBILE[:3]} {MOBILE[3:6]} {MOBILE[6:]}",
    "mobile 3-2-2-2": f"tel. {MOBILE[:3]} {MOBILE[3:5]} {MOBILE[5:7]} {MOBILE[7:]}",
    "+34": f"+34 {MOBILE}",
    "+34 grouped": f"+34-{MOBILE[:3]}-{MOBILE[3:6]}-{MOBILE[6:]}",
    "0034": f"0034{MOBILE}",
    "landline": f"fijo: {LANDLINE}",
    "+1": f"+1 (415) 8{fake_digits(2, 12)}-{US_LINE}",
    "+1 compact": f"+14158{fake_digits(2, 13)}{US_LINE}",
    "DNI": f"NIF {NIF}",
    "DNI dash": f"DNI {NIF[:8]}-{NIF[8]}",
    "NIE": f"NIE {NIE}",
    "IBAN": f"IBAN {IBAN}",
    "IBAN grouped": "IBAN " + " ".join(IBAN[i:i + 4] for i in range(0, 24, 4)),
    "CCC": f"cuenta {CCC_NUMBER}",
    "email": f"escribe a {EMAIL}",
    "locator": f"localizador: {LOCATOR}",
    "locator JSON": f'{{"localizador": "{LOCATOR}"}}',
    "locator camelCase": f'"codigoLocalizador": "{LOCATOR}"',
    "locator prose": f"Your booking reference is {LOCATOR}",
    "locator es": f"El código de reserva es {LOCATOR}.",
    "locator URL": f"/reservas?localizador={LOCATOR}&x=1",
    "PNR": f"PNR {LOCATOR}",
    "locator lowercase": f"localizador: {LOCATOR.lower()}",
    "locator letters only": f"localizador: {LETTERS}",
    "PNR letters only": f'{{"pnr": "{LETTERS}"}}',
    "pnrCode": f'"pnrCode": "{LOCATOR}"',
    "pnr_code": f"pnr_code={LOCATOR}",
    "purchaseCode": f'"purchaseCode": "{LOCATOR}"',
    "purchase_code": f"purchase_code: {LOCATOR}",
    "bookingCode": f'"bookingCode": "{LOCATOR}"',
    "booking_code": f"booking_code = '{LOCATOR}'",
    "bookingNumber": f'"bookingNumber": "{BOOKING}"',
    "booking_number": f"booking_number={BOOKING}",
    "codigoReserva": f'"codigoReserva": "{LOCATOR}"',
    "codigo_reserva": f"codigo_reserva={LOCATOR}",
    "booking number": f"número de reserva {BOOKING}",
    "billete": f"billete {BOOKING}",
    "Nº billete": f"Nº billete: {BOOKING}",
    "ticket number": f"ticket number {BOOKING}",
    "card": f"tarjeta {CARD}",
    "card grouped": "card " + grouped(CARD, (4, 4, 4, 4)),
    "card dashes": grouped(CARD, (4, 4, 4, 4), "-"),
    "amex grouped": "amex " + grouped(AMEX, (4, 6, 5)),
    "card + expiry": f"{CARD} 12/27",
    "card + expiry year": f"{CARD} 2029",
    "card grouped + expiry": "card " + grouped(CARD, (4, 4, 4, 4)) + " 12/27",
    "card grouped + CVV": grouped(CARD, (4, 4, 4, 4)) + " 123",
    "card 19 digits": f"tarjeta {fake_card('4', 19, 19)}",
    "card Mastercard 2-series": f"tarjeta {fake_card('2221', 16, 20)}",
    "street": "vive en Calle de " + "Alcalá 42",
    "avenue": "Avda. de la " + "Constitución, 7",
    "C/": "C/ " + "Mayor 3",
    "caps street": "CL " + "MAYOR 12, 3 B",
    "plaza": "Plaza " + "Mayor 1",
    "street on a rail line": "la estación queda en la Plaza " + "Mayor 1",
    "postal code": "28001 " + "Madrid",
}
NOT_FLAGGED = {
    "synthetic phones": "600000000 600000001 612345678 600123456 699999999 600000999",
    "service number": "900" + "123123",
    "hex hash": "sha256:a6" + "71829304b",
    "timestamp": "expires_at 1700000000",
    "ms timestamp": "created 1731682345123",
    "decimal": "x = 6.28318530",
    "longer run": "id 1" + MOBILE + "0",
    "DNI bad letter": "NIF " + NIF[:8] + ("A" if NIF[8] != "A" else "B"),
    "DNI synthetic": "00000000T 12345678Z",
    "example IBAN": "ES9121000418450200051332",
    "IBAN bad check": "ES00" + IBAN[4:],
    "CCC bad check digits": "cuenta " + CCC_NUMBER[:8] + "99" + CCC_NUMBER[10:],
    "placeholder emails": "user@example.com you@example.org a@b.co git@github.com x@renfe-cli.git",
    "role emails": f"support@{GMAIL} noreply@anthropic.com 1+luis@users.noreply.github.com",
    "test cards": "4111 1111 1111 1111, 4242424242424242, 5555-5555-5555-4444, 378282246310005",
    "card bad Luhn": "tarjeta " + CARD[:-1] + str((int(CARD[-1]) + 1) % 10),
    "card synthetic": f"tarjeta {SYNTHETIC_CARD}",
    "Luhn-valid date pair": f"between {luhn_date_pair()}",
    "Luhn-valid compact datetime": f"generated {luhn_datetime()}",
    "outside the 2-series range": f"id {fake_luhn('21', 16, 21)} {fake_luhn('28', 16, 22)}",
    "station codes in a row": "60000 71801 04040 18000",
    "locator shape without context": f"service {LOCATOR}, code {LOCATOR}",
    "locator placeholders": "localizador XXXXXX; localizador: ABC123; booking ref <locator>; PNR: <LOCATOR>",
    "locator header": "LOCALIZADOR | ORIGEN | DESTINO | PRECIO",
    "locator in code": "localizador = booking.locator  # LOCALIZADOR_RE, recordLocatorId",
    "locator prose": "the localizador is a 6-character code; allocator = 'ABCD5'",
    "public constants": "MADRI 60000 -> BARCE 71801, train 03063 type AV1",
    "public constants after locator words": "localizador MADRI, booking number 03063, locator 60000, PNR AV1",
    "letters after a booking word": f"booking ref {LETTERS}, código de reserva {LETTERS}",
    "letters in lower case or not six": f"localizador {LETTERS.lower()}, localizador {LETTERS}Q, PNR {LETTERS[:5]}",
    "lowercase code after locator words": "localizador = match1.group(1); booking_id = uuid4(); locator = locs22[0]",
    "ticket words need six digits": f"billete 03063, Nº billete 60000, ticket number {LOCATOR}, billete {LOCATOR}",
    "bare code is no context": f"code {LOCATOR}, status_code: {LOCATOR}",
    "AV train on a rail line": "tren AV MADRID 3100; AVE 03063 / AV MADRID 3100",
    "git SHA": f"commit {SHA[:7]} ({SHA}); booking ref {SHA[:7]}; localizador {SHA}",
    "placeholder street": "Calle Falsa 123",
    "postal placeholder": "12345 Ejemplo",
    "station code + city": "17000 Madrid-Chamartín, 51003 " + "Sevilla-Santa Justa, 03216 Valencia-Joaquín Sorolla",
    "station code with rail context": "estación 11014 Villaejemplo",
    "masked number": "6*****789 (pattern layer leaves masked numbers to the known-values layer)",
    "allow marker": f"tel {MOBILE}  # pii:allow synthetic fixture",
    "prose": "Plaza de toros, the Madrid office and booking locators stay out of git.",
}


# --- patterns ------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(FLAGGED))
def test_pattern_flags(pc, case):
    assert pc.scan_line(FLAGGED[case]), case


@pytest.mark.parametrize("case", sorted(NOT_FLAGGED))
def test_pattern_allows_synthetic_and_unrelated(pc, case):
    assert pc.scan_line(NOT_FLAGGED[case]) == [], case


def test_pattern_kinds(pc):
    assert pc.scan_line(FLAGGED["mobile"]) == ["phone number"]
    assert pc.scan_line(FLAGGED["NIE"]) == ["DNI/NIF/NIE"]
    assert pc.scan_line(FLAGGED["IBAN"]) == ["IBAN"]
    assert pc.scan_line(FLAGGED["locator"]) == ["booking locator or number"]
    assert pc.scan_line(FLAGGED["booking number"]) == ["booking locator or number"]
    assert pc.scan_line(FLAGGED["card grouped"]) == ["payment card number"]
    assert pc.scan_line(f"{FLAGGED['street']}, {FLAGGED['postal code']}") == ["street address", "postal code + town"]


def test_locator_needs_its_context_word(pc):
    assert pc.scan_line(f"localizador {LOCATOR}") and not pc.scan_line(f"ref {LOCATOR}")
    assert pc.scan_line(f"booking_ref = '{LOCATOR}'") and not pc.scan_line(f"train_ref = '{LOCATOR}'")
    for public in ("MADRI", "60000", "03063", "AV1", SHA[:7], SHA):
        assert pc.scan_line(f"localizador: {public}") == [], public


def test_card_shapes_never_match_a_slice_of_a_longer_number(pc):
    iban = iban_with_card_slices(18)
    assert pc.scan_line("IBAN " + " ".join(iban[i:i + 4] for i in range(0, 24, 4))) == ["IBAN"]


@pytest.mark.parametrize("card", sorted(PC.TEST_CARDS))
def test_gateway_test_cards_pass(pc, card):
    assert pc.luhn(card)
    assert pc.scan_line(f"card {card}") == [] and pc.scan_line("card " + grouped(card, (4, 4, 4, 4, 3))) == []


def test_postal_code_with_its_own_province_still_flags(pc):
    assert pc.scan_line("41001 " + "Sevilla") == ["postal code + town"]
    assert pc.scan_line("11014 " + "Villaejemplo") == ["postal code + town"]  # a town the table doesn't know


@pytest.mark.parametrize("path", ["capture.har", "x/.local-secrets/tokens.json", "tokens.json",
                                  "tokens.before-refresh.json", "Billete.PDF", "ticket.pkpass", "trips.csv",
                                  "export.xlsx", "dump.jsonl", "tickets.zip", ".env", ".env.local",
                                  "pii-needles.json", "pii-values.json", "billete.png", "IMG_0001.HEIC",
                                  "ticket.jpeg", "x.jpg", "x.heif", "x.webp", "x.gif", "x.bmp", "scan.TIFF", "x.tif"])
def test_blocked_paths(pc, path):
    assert pc.blocked_path(path)


@pytest.mark.parametrize("path", ["src/renfe/tokens.py", "tests/test_tokens.py", "README.md", ".env.example",
                                  "uv.lock", "Formula/renfe.rb"])
def test_allowed_paths(pc, path):
    assert not pc.blocked_path(path)


def test_self_test_passes(pc):
    assert pc.self_test() == 0


# --- known values (needles) ----------------------------------------------------------------------

# A fake owner's values file. Values are synthetic shapes (the pattern layer passes them), which is the
# point: the known-values layer must catch the owner's real values even when they look invented.
FAKE_PHONES = ("600000123", "+34 699 999 456", "910000078")
FAKE_VALUES_FILE = {
    "names": ["Ana Ejemplo Prueba", "Lucas Inventado Toledo"],
    "emails": ["ana.ejemplo@example.com"],
    "phones": list(FAKE_PHONES),
    "documents": ["00000000T", "PAA000000"],
    "locators": ["QRS000", "QRSTUV", "100000777"],
    "addresses": ["Calle Falsa 123, 00000 Villaejemplo"],
    "other": ["4111 1111 1111 1111", "1990-01-01"],
}
FAKE_VALUES = [v for values in FAKE_VALUES_FILE.values() for v in values]
FAKE_WORDS = ["ana", "ejemplo", "prueba", "lucas", "inventado", "toledo", "falsa", "villaejemplo", "qrstuv",
              "qrs000", "paa000000", "000000777", "4111111111111111"]


@pytest.fixture
def fake_values(tmp_path, monkeypatch):
    """The fake values file (mode 600) and the needles path, both outside any repo. Returns the needles path."""
    config = tmp_path / "config"
    config.mkdir()
    source = config / "pii-values.json"
    source.write_text(json.dumps(FAKE_VALUES_FILE, indent=2))
    source.chmod(0o600)
    monkeypatch.setenv("RENFE_PII_VALUES", str(source))
    needles = config / "pii-needles.json"
    monkeypatch.setenv("RENFE_PII_NEEDLES", str(needles))
    return needles


def test_needles_build(pc, fake_values, capsys):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    out = capsys.readouterr()
    printed = (out.out + out.err).lower()
    assert stat.S_IMODE(fake_values.stat().st_mode) == 0o600
    stored = fake_values.read_text().lower()
    for value in FAKE_VALUES + FAKE_WORDS:
        assert value.lower() not in stored, value
        assert value.lower() not in printed, value
    for kind in ("phone number", "masked phone number", "DNI/NIE/passport", "booking locator or number",
                 "email", "passenger name", "address", "other listed value"):
        assert kind in out.out
    assert "NO HIT" not in out.out and out.err == ""
    assert pc.main(["pii_check.py", "needles", "status"]) == 0


@pytest.mark.parametrize("text, kind", [
    (f"tel {FAKE_PHONES[0]}", "phone number"),
    ("+34 699 99 94 56", "phone number"),
    ("0034 910 000 078", "phone number"),
    ("sms enviado al 6*****123", "masked phone number"),
    ("acaba en ***9456", "masked phone number"),
    ("DNI 00000000-T", "DNI/NIE/passport"),
    ("pasaporte paa000000", "DNI/NIE/passport"),
    ('"localizador": "QRS000"', "booking locator or number"),
    ("ref qrstuv", "booking locator or number"),
    ("/bookings/100000777", "booking locator or number"),
    ("ana.ejemplo@example.com", "email"),
    ("Ana Ejemplo compró los billetes", "passenger name"),
    ("PRUEBA, ANA", "passenger name"),
    ("the passenger is Inventado", "passenger name"),
    ("Lucas Toledo", "passenger name"),
    ("Calle Falsa, 123", "address"),
    ("card 4111-1111-1111-1111", "other listed value"),
    ("born 1990-01-01", "other listed value"),
    ("tickets/trip-qrs000.json", "booking locator or number"),
    ("2027-01-29-QRS000", "booking locator or number"),
    ("billete_QRS000.pdf", "booking locator or number"),
    ("pasaporte_PAA-000000.pdf", "DNI/NIE/passport"),
])
def test_needles_find_known_values(pc, fake_values, text, kind):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    assert f"known personal value: {kind}" in [f.kind for f in pc.known_values([(1, text)], "t")]


@pytest.mark.parametrize("text", ["tel 600000124", "6*****124", "600000001 6*****001", "Lucía Inventada",
                                  "Calle Ficticia 124", "localizador QRS111", "otra@example.com", "100000999",
                                  "Madrid - Toledo", "Toledo", "4242 4242 4242 4242", "1990-01-02"])
def test_needles_ignore_other_values(pc, fake_values, text):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    assert pc.known_values([(1, text)], "t") == []


def test_needles_ignore_allow_marker(pc, fake_values):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    assert pc.known_values([(1, f"tel {FAKE_PHONES[0]}  # pii:allow")], "t")


@pytest.mark.parametrize("state", ["corrupt", "unreadable", "wrong shape"])
def test_a_broken_needles_file_blocks(pc, tmp_path, monkeypatch, capsys, state):
    needles = tmp_path / "no-needles.json"  # where the `pc` fixture points the check
    needles.write_text('{"salt": 1, "kinds": []}' if state == "wrong shape" else "{not json")
    if state == "unreadable":
        needles.chmod(0)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"PR body\n{LOCATOR}\n"))
    try:
        assert pc.main(["pii_check.py", "text"]) == 2
    finally:
        needles.chmod(0o600)
    err = capsys.readouterr().err
    assert "needles build" in err and LOCATOR not in err


def test_a_broken_needles_file_denies_in_the_agent_hook(pc, model, repo, tmp_path, monkeypatch, capsys):
    (tmp_path / "no-needles.json").write_text("{not json")
    code, out, err = run_hook(pc, monkeypatch, capsys, {"command": "gh pr create --body 'hola'", "cwd": str(repo)})
    assert code == 2 and json.loads(out)["permission"] == "deny" and "needles build" in err
    assert model.calls == []


def test_maintainer_handle_words_only_count_in_pairs(pc):
    tokens = pc.needle_tokens([("name", "Lucía González Ejemplo")])
    assert "w:gonzalez" not in tokens and "w:lucia" in tokens and "w:ejemplo" in tokens
    assert tokens["p:lucia gonzalez"] == "name" and tokens["p:gonzalez lucia"] == "name"


def test_place_name_words_only_count_in_pairs(pc):
    tokens = pc.needle_tokens([("name", "María Toledo Ejemplo")])
    assert "w:maria" not in tokens and "w:toledo" not in tokens and "w:ejemplo" in tokens
    assert tokens["p:maria toledo"] == "name"


def test_needles_missing_file_is_a_note(pc, capsys):
    assert pc.known_values([(1, "anything")], "t") == []
    assert "known-values check skipped" in capsys.readouterr().err


def test_needles_build_without_a_values_file(pc, tmp_path, capsys):
    assert pc.main(["pii_check.py", "needles", "build"]) == 1
    err = capsys.readouterr().err
    assert "no values file at" in err and str(tmp_path / "no-values.json") in err and '"names": []' in err
    assert not (tmp_path / "no-needles.json").exists()


def test_needles_build_invalid_json(pc, tmp_path, monkeypatch, capsys):
    source = tmp_path / "values.json"
    source.write_text('{"names": ["Ana Ejemplo"')
    monkeypatch.setenv("RENFE_PII_VALUES", str(source))
    assert pc.main(["pii_check.py", "needles", "build"]) == 1
    err = capsys.readouterr().err
    assert "isn't valid JSON" in err and "Ejemplo" not in err


def test_needles_build_reports_problems_by_location_only(pc, tmp_path, monkeypatch, capsys):
    source = tmp_path / "values.json"
    source.write_text(json.dumps({"names": ["Ana Ejemplo", 7, "Al"], "Zq Ejemplo": ["x"], "phones": "600000123"}))
    source.chmod(0o600)
    monkeypatch.setenv("RENFE_PII_VALUES", str(source))
    monkeypatch.setenv("RENFE_PII_NEEDLES", str(tmp_path / "needles.json"))
    assert pc.main(["pii_check.py", "needles", "build"]) == 1
    out = capsys.readouterr()
    printed = out.out + out.err
    assert "names[1] isn't a non-empty string" in out.err and "names[2] isn't caught" in out.err
    assert "key #2 isn't one of" in out.err and "phones must be a list" in out.err
    for value in ("Ana", "Ejemplo", "Zq", "600000123"):
        assert value not in printed
    assert (tmp_path / "needles.json").exists()  # the usable values still count


def test_needles_build_warns_about_a_readable_values_file(pc, fake_values, tmp_path, capsys):
    (tmp_path / "config" / "pii-values.json").chmod(0o644)
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    assert "chmod 600" in capsys.readouterr().err


# --- modes and exit codes ------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    shutil.copytree(HOOKS, path / ".githooks", ignore=shutil.ignore_patterns("__pycache__"))
    return path


@pytest.fixture
def model(pc, monkeypatch):
    """Stub the model layer; records the blocks it was asked about and returns `model.result`."""

    class Stub:
        calls: list = []
        result: tuple = ([], None)

        def __call__(self, blocks):
            self.calls.append(blocks)
            return self.result

    stub = Stub()
    stub.calls = []
    monkeypatch.setattr(pc, "model_check", stub)
    return stub


def test_staged_blocks_pattern_hit_without_asking_the_model(pc, model, repo, monkeypatch, capsys):
    (repo / "notes.md").write_text(f"ok line\n{FLAGGED['mobile']}\n")
    git(repo, "add", "notes.md")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    err = capsys.readouterr().err
    assert "notes.md:2  phone number" in err and MOBILE not in err
    assert model.calls == []


def test_staged_clean_asks_the_model_about_added_lines(pc, model, repo, monkeypatch):
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-q", "--no-verify", "-m", "base")
    (repo / "a.py").write_text("x = 1\ny = 2\n")
    git(repo, "add", "a.py")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 0
    assert model.calls == [[("a.py", [(2, "y = 2")])]]


def test_staged_model_finding_blocks(pc, model, repo, monkeypatch, capsys):
    (repo / "a.md").write_text("La pasajera es Ana Ejemplo.\n")
    git(repo, "add", "a.md")
    model.result = ([pc.Finding("a.md:1", "passenger name (model)")], None)
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    assert "a.md:1  passenger name (model)" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["purchase.har", "billete.pkpass"])
def test_staged_blocks_data_files(pc, model, repo, monkeypatch, capsys, name):
    (repo / name).write_text("{}\n")
    git(repo, "add", name)
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    assert f"{name}  data file" in capsys.readouterr().err


def test_staged_known_value(pc, model, repo, fake_values, monkeypatch, capsys):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    (repo / "test_x.py").write_text('MASKED = "6*****456"  # pii:allow looks synthetic\n')
    git(repo, "add", "test_x.py")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert pc.main(["pii_check.py", "staged"]) == 2
    assert "test_x.py:1  known personal value: masked phone number" in capsys.readouterr().err
    assert model.calls == []


def test_staged_non_ascii_path_is_scanned(pc, model, repo, monkeypatch, capsys):
    (repo / "guía.md").write_text(f"{FLAGGED['mobile']}\n")
    git(repo, "add", "guía.md")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    assert "guía.md:1  phone number" in capsys.readouterr().err


def test_staged_lines_that_look_like_diff_headers_stay_content(pc, model, repo, monkeypatch, capsys):
    (repo / "notes.md").write_text("-- old\nkeep\n")
    git(repo, "add", "notes.md")
    git(repo, "commit", "-q", "--no-verify", "-m", "base")
    (repo / "notes.md").write_text(f"++ looks like a header\nkeep\n{FLAGGED['mobile']}\n++ {FLAGGED['email']}\n")
    git(repo, "add", "notes.md")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    err = capsys.readouterr().err
    assert "notes.md:3  phone number" in err and "notes.md:4  email" in err


def test_staged_header_lookalikes_reach_the_model_with_their_line_numbers(pc, model, repo, monkeypatch):
    (repo / "a.md").write_text("-- removed\n")
    git(repo, "add", "a.md")
    git(repo, "commit", "-q", "--no-verify", "-m", "base")
    (repo / "a.md").write_text("++ added\n+++ b/fake\nlast\n")
    git(repo, "add", "a.md")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 0
    assert model.calls == [[("a.md", [(1, "++ added"), (2, "+++ b/fake"), (3, "last")])]]


def test_staged_non_utf8_content_is_scanned_without_a_traceback(pc, model, repo, monkeypatch, capsys):
    (repo / "latin1.txt").write_bytes(b"tel\xe9fono " + MOBILE.encode() + b"\n\xff\xfe\n")
    git(repo, "add", "latin1.txt")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    assert "latin1.txt:1  phone number" in capsys.readouterr().err


@pytest.mark.parametrize("name, kind", [
    (f"billete_localizador_{LOCATOR}.txt", "booking locator or number in the file name"),
    (f"notes-{EMAIL}.md", "email in the file name"),
    (f"llamar_{MOBILE}.md", "phone number in the file name"),
])
def test_staged_file_names_are_scanned(pc, model, repo, monkeypatch, capsys, name, kind):
    (repo / name).write_text("nothing here\n")
    git(repo, "add", name)
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    err = capsys.readouterr().err
    assert f"staged file #1  {kind}" in err and "Nth path of `git diff --cached --name-only --no-renames`" in err
    assert name not in err and LOCATOR not in err and EMAIL not in err and MOBILE not in err  # path never shown


def test_staged_file_name_known_value(pc, model, repo, fake_values, monkeypatch, capsys):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    (repo / "trip-qrs000.json").write_text("{}\n")
    git(repo, "add", "trip-qrs000.json")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert pc.main(["pii_check.py", "staged"]) == 2
    err = capsys.readouterr().err
    assert "known personal value: booking locator or number in the file name" in err
    assert "trip-qrs000" not in err


def test_staged_blocks_screenshots(pc, model, repo, monkeypatch, capsys):
    (repo / "billete.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    git(repo, "add", "billete.png")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    assert "billete.png  data file" in capsys.readouterr().err


def test_staged_lone_cr_keeps_the_rest_of_the_line(pc, model, repo, monkeypatch, capsys):
    (repo / "cr.txt").write_bytes(b"a\rtel " + MOBILE.encode() + b"\r\nnext\r\n")
    git(repo, "add", "cr.txt")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    assert "cr.txt:1  phone number" in capsys.readouterr().err


def test_staged_crlf_line_ends_are_stripped(pc, model, repo, monkeypatch):
    (repo / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    git(repo, "add", "a.txt")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 0
    assert model.calls == [[("a.txt", [(1, "one"), (2, "two")])]]


def test_staged_typechange_is_scanned(pc, model, repo, monkeypatch, capsys):
    os.symlink("target", repo / "link")
    git(repo, "add", "link")
    git(repo, "commit", "-q", "--no-verify", "-m", "base")
    (repo / "link").unlink()
    (repo / "link").write_text(f"tel {MOBILE}\n")
    git(repo, "add", "link")
    assert git(repo, "diff", "--cached", "--name-status").startswith("T")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    assert "link:1  phone number" in capsys.readouterr().err


@pytest.mark.parametrize("name, content, attributes", [
    ("notes.txt", f"llama al {MOBILE}\n".encode("utf-16"), None),
    ("notes.txt", f"llama al {MOBILE}\n".encode("utf-16-le"), None),
    ("secret.dat", f"llama al {MOBILE}\n".encode(), "secret.dat -diff\n"),
])
def test_staged_binary_looking_text_is_scanned(pc, model, repo, monkeypatch, capsys, name, content, attributes):
    if attributes:
        (repo / ".gitattributes").write_text(attributes)
    (repo / name).write_bytes(content)
    git(repo, "add", "-A")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    err = capsys.readouterr().err
    assert f"{name}:1  phone number" in err and MOBILE not in err


def test_staged_binary_that_cannot_be_read_blocks(pc, model, repo, monkeypatch, capsys):
    (repo / "blob.bin").write_bytes(bytes(range(256)) * 4)
    git(repo, "add", "blob.bin")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    assert "blob.bin  not scanned: git treats it as binary" in capsys.readouterr().err
    assert model.calls == []


def test_staged_changes_without_added_lines_are_not_called_binary(pc, model, repo, monkeypatch, capsys):
    (repo / "a.md").write_text("one\ntwo\n")
    (repo / "run.sh").write_text("echo hi\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "base")
    (repo / "a.md").write_text("one\n")  # a deletion only
    (repo / "run.sh").chmod(0o755)  # a mode change only
    (repo / "empty.txt").write_text("")  # an empty file
    git(repo, "add", "-A")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 0
    assert "not scanned" not in capsys.readouterr().err


def test_staged_path_with_a_space(pc, model, repo, monkeypatch, capsys):
    (repo / "my notes.md").write_text(f"tel {MOBILE}\n")
    (repo / "my ticket.pdf").write_text("all text\n")
    git(repo, "add", "my notes.md", "my ticket.pdf")
    monkeypatch.chdir(repo)
    findings, blocks = pc.scan_staged()
    assert pc.Finding("my notes.md:1", "phone number") in findings
    assert pc.Finding("my ticket.pdf", "data file that never belongs in git") in findings
    assert [label for label, _ in blocks] == ["my notes.md"]  # the blocked file isn't sent to the model


def test_names_holding_personal_data_are_never_shown(pc, model, repo, fake_values, monkeypatch, capsys):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    (repo / "billete-QRS000.pdf").write_text(f"tel {MOBILE}\n")
    (repo / "trip_QRS000").mkdir()
    (repo / "trip_QRS000" / "x.md").write_text(f"tel {MOBILE}\n")
    git(repo, "add", "billete-QRS000.pdf", "trip_QRS000/x.md")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert pc.main(["pii_check.py", "staged"]) == 2
    err = capsys.readouterr().err
    assert "staged file #1  data file" in err and "staged file #2:1  phone number" in err
    assert "qrs000" not in err.lower() and MOBILE not in err and model.calls == []
    findings, blocks = pc.scan_staged()
    assert [label for label, _ in blocks] == ["staged file #2"]  # what the model would be sent
    assert "qrs000" not in json.dumps(blocks).lower() and all("qrs000" not in f.where.lower() for f in findings)


def test_files_mode_names_holding_personal_data_are_never_shown(pc, model, fake_values, tmp_path, capsys):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    folder = tmp_path / "trip_QRS000"
    folder.mkdir()
    (folder / "x.md").write_text(f"tel {MOBILE}\n")
    (tmp_path / "clean.md").write_text("nothing\n")
    capsys.readouterr()
    assert pc.main(["pii_check.py", "files", str(tmp_path / "clean.md"), str(folder / "x.md")]) == 2
    err = capsys.readouterr().err
    assert "file #2:1  phone number" in err and "file #N is the Nth path given" in err and "qrs000" not in err.lower()
    assert model.calls == []


def test_msg_mode_scans_comment_lines_but_not_the_diff_below_the_scissors(pc, model, tmp_path, capsys):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"Fix search\n\n# {FLAGGED['mobile']}\n")  # `git commit -m "# ..."` keeps such lines
    assert pc.main(["pii_check.py", "msg", str(msg)]) == 2
    assert "commit message:3  phone number" in capsys.readouterr().err
    msg.write_text(f"Fix search\n\n# Please enter the message\n; ------------------------ >8 ------------------------\n"
                   f"+{FLAGGED['mobile']}\n")
    assert pc.main(["pii_check.py", "msg", str(msg)]) == 0
    assert model.calls == [[("commit message", [(1, "Fix search"), (2, ""), (3, "# Please enter the message")])]]
    msg.write_text(f"Fix search\n\nReported from {EMAIL}\n")
    assert pc.main(["pii_check.py", "msg", str(msg)]) == 2
    err = capsys.readouterr().err
    assert "commit message:3  email" in err and EMAIL not in err


def test_text_mode(pc, model, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"PR body\n{FLAGGED['locator']}\n"))
    assert pc.main(["pii_check.py", "text"]) == 2
    assert "text:2  booking locator or number" in capsys.readouterr().err
    monkeypatch.setattr(sys, "stdin", io.StringIO("PR body\nall good\n"))
    assert pc.main(["pii_check.py", "text"]) == 0
    assert model.calls == [[("text", [(1, "PR body"), (2, "all good")])]]


def test_files_mode_patterns_only_skips_the_model(pc, model, tmp_path):
    f = tmp_path / "a.md"
    f.write_text("nothing here\n")
    assert pc.main(["pii_check.py", "files", "--patterns-only", str(f)]) == 0
    assert model.calls == []
    assert pc.main(["pii_check.py", "files", str(f)]) == 0
    assert len(model.calls) == 1


def test_unknown_mode_prints_usage(pc, capsys):
    assert pc.main(["pii_check.py", "bogus"]) == 64
    assert "Usage" in capsys.readouterr().err


def test_no_flagged_value_is_ever_printed(pc, model, monkeypatch, capsys):
    text = "\n".join(FLAGGED.values())
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    assert pc.main(["pii_check.py", "text"]) == 2
    out = capsys.readouterr()
    printed = out.out + out.err
    flagged_lines = {int(n) for n in re.findall(r"\n  text:(\d+)  ", printed)}
    assert flagged_lines == set(range(1, len(FLAGGED) + 1))
    for value in (MOBILE, LANDLINE, NIF, NIE, IBAN, CCC_NUMBER, EMAIL, LOCATOR, LOCATOR.lower(), LETTERS, BOOKING,
                  CARD, CARD[-4:], AMEX,
                  "Alcalá", "Constitución", "MAYOR"):
        assert value not in printed


# --- model layer (stubbed transport) -------------------------------------------------------------


def fake_claude(tmp_path: Path, stdout: dict | str, code: int = 0) -> Path:
    """A fake `claude` that records its argv and cwd, and prints `stdout`."""
    script = tmp_path / "claude"
    payload = stdout if isinstance(stdout, str) else json.dumps(stdout)
    script.write_text(f"""#!{sys.executable}
import json, os, sys
json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": sys.stdin.read(),
           "claudecode": os.environ.get("CLAUDECODE")}}, open({str(tmp_path / 'call.json')!r}, "w"))
print({payload!r})
sys.exit({code})
""")
    script.chmod(0o755)
    return script


def test_cli_path_runs_isolated_and_parses_the_verdict(pc, tmp_path, monkeypatch):
    verdict = {"personal_data_found": True, "findings": [{"location": "a.md:3", "kind": "passenger name"}]}
    exe = fake_claude(tmp_path, {"type": "result", "subtype": "success", "is_error": False,
                                 "structured_output": verdict})
    monkeypatch.setattr(pc, "_claude_cli", lambda: str(exe))
    monkeypatch.setenv("CLAUDECODE", "1")
    findings, note = pc.model_check([("a.md", [(3, "La pasajera es Ana Ejemplo"), (4, "x  # pii:allow")])])
    assert findings == [pc.Finding("a.md:3", "passenger name (model)")] and note is None
    call = json.loads((tmp_path / "call.json").read_text())
    argv = call["argv"]
    for flag in ("-p", "--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    assert "renfe-cli" in argv[argv.index("--system-prompt") + 1]
    assert not Path(call["cwd"]).resolve().is_relative_to(ROOT) and call["claudecode"] is None
    assert "Ana Ejemplo" in call["stdin"] and "pii:allow" not in call["stdin"]


@pytest.mark.parametrize("stdout, code", [
    ({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in"}, 1),
    ({"type": "result", "subtype": "error_max_turns", "is_error": False}, 0),
    ("not json", 1),
])
def test_cli_path_fails_open(pc, tmp_path, monkeypatch, stdout, code):
    monkeypatch.setattr(pc, "_claude_cli", lambda: str(fake_claude(tmp_path, stdout, code)))
    findings, note = pc.model_check([("a.md", [(1, "text")])])
    assert findings == [] and "the other checks still ran" in note


def test_no_model_available_is_a_note(pc):
    findings, note = pc.model_check([("a.md", [(1, "text")])])
    assert findings == [] and "model check skipped" in note


def test_api_path(pc, monkeypatch):
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout):
        seen["body"], seen["headers"] = json.loads(req.data), dict(req.header_items())
        verdict = {"personal_data_found": False, "findings": []}
        return Response(json.dumps({"stop_reason": "end_turn", "content": [
            {"type": "thinking", "thinking": ""}, {"type": "text", "text": json.dumps(verdict)}]}).encode())

    monkeypatch.setattr(pc, "_api_key", lambda: "sk-test")
    monkeypatch.setattr(pc.urllib.request, "urlopen", urlopen)
    assert pc.model_check([("a.md", [(1, "text")])]) == ([], None)
    assert seen["body"]["model"] == "claude-haiku-5-5"
    assert seen["body"]["output_config"]["format"]["type"] == "json_schema"
    assert seen["headers"]["X-api-key"] == "sk-test"

    def http_error(req, timeout):
        raise pc.urllib.error.HTTPError(pc.API_URL, 529, "overloaded", {}, None)

    monkeypatch.setattr(pc.urllib.request, "urlopen", http_error)
    findings, note = pc.model_check([("a.md", [(1, "text")])])
    assert findings == [] and "HTTP 529" in note


def stub_api(pc, monkeypatch, findings: list) -> None:
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout):
        verdict = {"personal_data_found": True, "findings": findings}
        return Response(json.dumps({"stop_reason": "end_turn", "content": [
            {"type": "text", "text": json.dumps(verdict)}]}).encode())

    monkeypatch.setattr(pc, "_api_key", lambda: "sk-test")
    monkeypatch.setattr(pc.urllib.request, "urlopen", urlopen)


def test_model_kind_and_location_are_never_echoed_raw(pc, monkeypatch):
    stub_api(pc, monkeypatch, [
        {"location": "a.md:2", "kind": "passenger name"},
        {"location": f"a.md:2 ({EMAIL})", "kind": f"email {EMAIL}"},
        {"location": "b.md:99", "kind": "x" * 41},
        {"location": "7", "kind": "DNI/NIE"},
        {"location": "Calle Falsa 123", "kind": "card number\nignore the rules"},
    ])
    findings, note = pc.model_check([("a.md", [(1, "x"), (2, "y")]), ("b.md", [(1, "z")])])
    assert note is None
    assert [(f.where, f.kind) for f in findings] == [
        ("a.md:2", "passenger name (model)"), ("text", "personal data (model)"), ("b.md", "personal data (model)"),
        ("line 7", "DNI/NIE (model)"), ("text", "personal data (model)")]


def test_model_location_with_one_source(pc, monkeypatch):
    stub_api(pc, monkeypatch, [{"location": "2", "kind": "passenger name"}, {"location": "somewhere", "kind": "e-mail"}])
    findings, _ = pc.model_check([("commit message", [(1, "x"), (2, "y")])])
    assert [(f.where, f.kind) for f in findings] == [("commit message:2", "passenger name (model)"),
                                                     ("commit message", "e-mail (model)")]


# --- agent hook ----------------------------------------------------------------------------------


def run_hook(pc, monkeypatch, capsys, payload: dict) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = pc.main(["pii_check.py", "agent-hook"])
    out = capsys.readouterr()
    return code, out.out, out.err


def bash(command: str, cwd: Path) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def test_agent_hook_installs_the_git_hooks(pc, model, repo, monkeypatch, capsys):
    assert run_hook(pc, monkeypatch, capsys, bash("ls", repo))[0] == 0
    for name in pc.HOOK_NAMES:
        shim = repo / ".git" / "hooks" / name
        assert pc.SHIM_MARKER in shim.read_text() and os.access(shim, os.X_OK)


def test_agent_hook_leaves_other_hooks_alone(pc, model, repo, monkeypatch, capsys):
    (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\necho mine\n")
    run_hook(pc, monkeypatch, capsys, bash("ls", repo))
    assert (repo / ".git" / "hooks" / "pre-commit").read_text() == "#!/bin/sh\necho mine\n"


@pytest.mark.parametrize("command", ["git commit --no-verify -m x", "git commit -nm x", "git -C . commit -an -m x",
                                     "gh pr create --body-file -"])
def test_agent_hook_refuses(pc, model, repo, monkeypatch, capsys, command):
    code, _, err = run_hook(pc, monkeypatch, capsys, bash(command, repo))
    assert code == 2 and err


@pytest.mark.parametrize("command", ["git commit -m 'msg -n here'", "git commit -am fix", "ls -la", f"echo {EMAIL}"])
def test_agent_hook_allows_without_the_model(pc, model, repo, monkeypatch, capsys, command):
    assert run_hook(pc, monkeypatch, capsys, bash(command, repo))[0] == 0
    assert model.calls == []


@pytest.mark.parametrize("command", [
    f"gh pr create --title t --body 'cc {EMAIL}'",
    f"gh issue comment 3 --body \"llama al {MOBILE}\"",
    f"gh -R luisgonzaleznf/renfe-cli release create v1 --notes '{FLAGGED['IBAN']}'",
    f"gh api repos/o/r/issues -f body='NIF {NIF}'",
    f"gh pr comment 5 --body 'Probado con el {FLAGGED['locator']}'",
    f"gh issue create --title t --body 'Pagado con {FLAGGED['card grouped']}'",
])
def test_agent_hook_blocks_posted_text(pc, model, repo, monkeypatch, capsys, command):
    code, _, err = run_hook(pc, monkeypatch, capsys, bash(command, repo))
    assert code == 2 and "command:1" in err
    for value in (EMAIL, MOBILE, IBAN, NIF, LOCATOR, CARD[-4:]):
        assert value not in err


def test_agent_hook_reads_body_files(pc, model, repo, monkeypatch, capsys):
    (repo / "body.md").write_text(f"Summary\n\n{FLAGGED['street']}\n")
    code, _, err = run_hook(pc, monkeypatch, capsys, bash("gh pr edit 4 --body-file body.md", repo))
    assert code == 2 and "body.md:3 (street address)" in err
    (repo / "notes.md").write_text("Release notes\n")
    code, _, _ = run_hook(pc, monkeypatch, capsys, bash("gh release edit v1 --notes-file notes.md", repo))
    assert code == 0
    assert [label for label, _ in model.calls[-1]] == ["command", "notes.md"]


def test_agent_hook_body_file_names_holding_personal_data_are_never_shown(pc, model, repo, fake_values, monkeypatch,
                                                                        capsys):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    (repo / "body-QRS000.md").write_text(f"Summary\ntel {MOBILE}\n")
    capsys.readouterr()
    code, _, err = run_hook(pc, monkeypatch, capsys, bash("gh pr create --title t --body-file body-QRS000.md", repo))
    assert code == 2 and "body file #1:2 (phone number)" in err and "body file #1 (known personal value" in err
    assert "qrs000" not in err.lower() and MOBILE not in err and model.calls == []


def test_agent_hook_model_verdict_blocks(pc, model, repo, monkeypatch, capsys):
    model.result = ([pc.Finding("command:1", "passenger name (model)")], None)
    code, _, err = run_hook(pc, monkeypatch, capsys, bash("gh pr create --body 'Gracias, Ana Ejemplo'", repo))
    assert code == 2 and "passenger name (model)" in err and "owner or a passenger" in err


def test_agent_hook_cursor_payload(pc, model, repo, monkeypatch, capsys):
    code, out, _ = run_hook(pc, monkeypatch, capsys, {"command": "git commit --no-verify -m x", "cwd": str(repo)})
    assert code == 2 and json.loads(out)["permission"] == "deny"
    code, out, _ = run_hook(pc, monkeypatch, capsys, {"command": "ls", "cwd": str(repo)})
    assert code == 0 and json.loads(out) == {"permission": "allow"}


def test_agent_hook_ignores_bad_payloads(pc, model, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert pc.main(["pii_check.py", "agent-hook"]) == 0


# --- the git hooks, end to end -------------------------------------------------------------------


def hook_env(tmp_path: Path, needles: Path) -> dict:
    """PATH with python3 and git only (no `claude`), an empty HOME, the given needles file."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "RENFE_", "CLAUDE"))}
    env.update(PATH=f"{Path(sys.executable).parent}{os.pathsep}/usr/bin{os.pathsep}/bin", HOME=str(tmp_path / "home"),
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", RENFE_PII_NEEDLES=str(needles))
    return env


def commit(repo: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", f"core.hooksPath={repo / '.githooks'}", "commit", *args], cwd=repo, env=env,
                          capture_output=True, text=True)


def test_git_hooks_end_to_end(pc, repo, fake_values, tmp_path):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    env = hook_env(tmp_path, fake_values)
    (repo / "README.md").write_text("# demo\n")
    git(repo, "add", "README.md")
    clean = commit(repo, env, "-m", "Add a readme")
    assert clean.returncode == 0, clean.stderr
    assert "model check skipped" in clean.stderr  # fails open: no API key, no claude CLI

    blocked_msg = commit(repo, env, "--allow-empty", "-m", "Fix the booking lookup\n\nSeen on localizador QRS000")
    assert blocked_msg.returncode != 0 and "commit message:3  known personal value" in blocked_msg.stderr

    hash_line = commit(repo, env, "--allow-empty", "-m", f"Fix search\n\n# {FLAGGED['mobile']}")  # -m keeps # lines
    assert hash_line.returncode != 0 and "commit message:3  phone number" in hash_line.stderr

    (repo / "fixture.py").write_text(f'BOOKING = {{"localizador": "{LOCATOR}"}}\nCARD = "{CARD}"\n')
    git(repo, "add", "fixture.py")
    blocked_diff = commit(repo, env, "-m", "Add a fixture")
    assert blocked_diff.returncode != 0 and "fixture.py:1  booking locator or number" in blocked_diff.stderr
    assert "fixture.py:2  payment card number" in blocked_diff.stderr
    assert LOCATOR not in blocked_diff.stdout + blocked_diff.stderr and CARD not in blocked_diff.stdout + blocked_diff.stderr
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "1"


def test_clean_merge_runs_the_staged_check(pc, repo, tmp_path):
    env = hook_env(tmp_path, tmp_path / "no-needles.json")
    (repo / "README.md").write_text("# demo\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "--no-verify", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "fixture.py").write_text(f'PHONE = "{MOBILE}"\n')
    git(repo, "add", "fixture.py")
    git(repo, "commit", "-q", "--no-verify", "-m", "Add a fixture")
    git(repo, "checkout", "-q", "main")
    (repo / "other.md").write_text("other\n")
    git(repo, "add", "other.md")
    git(repo, "commit", "-q", "--no-verify", "-m", "Other work")
    merged = subprocess.run(["git", "-c", f"core.hooksPath={repo / '.githooks'}", "merge", "--no-edit", "feature"],
                            cwd=repo, env=env, capture_output=True, text=True)
    assert merged.returncode != 0 and "fixture.py:1  phone number" in merged.stderr
    assert MOBILE not in merged.stdout + merged.stderr
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "2"



def shim_commit(repo: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    """`git commit` through the hooks `enable` installed in .git/hooks (no core.hooksPath)."""
    return subprocess.run(["git", "commit", *args], cwd=repo, env=env, capture_output=True, text=True)


def test_installed_shims_run_the_tracked_hooks_and_fail_closed(pc, model, repo, tmp_path, monkeypatch, capsys):
    run_hook(pc, monkeypatch, capsys, bash("ls", repo))  # installs the shims
    env = hook_env(tmp_path, tmp_path / "no-needles.json")
    (repo / "fixture.py").write_text(f'PHONE = "{MOBILE}"\n')
    git(repo, "add", "fixture.py")
    blocked = shim_commit(repo, env, "-m", "Add a fixture")
    assert blocked.returncode != 0 and "fixture.py:1  phone number" in blocked.stderr
    (repo / ".githooks" / "pre-commit").chmod(0o644)  # the tracked hook lost its exec bit
    broken = shim_commit(repo, env, "-m", "Add a fixture")
    assert broken.returncode != 0 and "not executable" in broken.stderr
    assert git(repo, "rev-list", "--all", "--count").strip() == "0"


def test_installed_shims_pass_where_the_check_is_absent(pc, model, repo, tmp_path, monkeypatch, capsys):
    run_hook(pc, monkeypatch, capsys, bash("ls", repo))
    shutil.rmtree(repo / ".githooks")  # e.g. a branch from before the check
    (repo / "README.md").write_text("# demo\n")
    git(repo, "add", "README.md")
    assert shim_commit(repo, hook_env(tmp_path, tmp_path / "no-needles.json"), "-m", "Add a readme").returncode == 0
