#!/usr/bin/env python3
"""Stop the owner's and their passengers' personal data before it reaches git or GitHub.

renfe-cli talks directly to Renfe's own web backend (api.rsv.renfe.com, venta.renfe.com) to search trains
and book tickets as a guest, without a Renfe account. Most commits are written by AI agents that have seen
the owner's real bookings, and anything committed should be treated as public for good. Three layers
check the same text (staged changes, a commit message, PR, issue or release text):

1. Patterns, standard library only, always on: Spanish phone numbers (6xx/7xx/8xx/9xx, with or without
   +34/0034) and +1 ones, DNI/NIF and NIE (checksum letter validated), payment card numbers (13-19
   digits, compact or in card groups, Luhn valid), booking locators, booking and ticket numbers (only
   right after a word or field name like localizador, PNR, booking ref, codigoReserva or billete), IBANs
   (mod-97) and Spanish CCC account numbers (check digits), personal emails (any address except
   example.*, GitHub no-reply and role addresses), Spanish street addresses and postal code + town, and
   data files that never belong in git (*.har, .local-secrets/, tokens*.json, *.pdf, *.pkpass,
   screenshots, csv/tsv/xlsx/jsonl/zip, ...). Staged file names are checked like their contents.
   Obviously synthetic shapes (repeated or sequential digits, the usual example IBANs, the payment
   gateways' test cards, XXXXXX or ABC123 locators) are allowed, and so are Renfe's public constants:
   station codes and names (60000, 71801, MADRI), train service numbers (03063) and train types (AV1).
2. Known values, always on once built: `needles build` reads a values file the owner writes by hand,
   outside every repo ($RENFE_PII_VALUES, default ~/.config/renfe-cli/pii-values.json):
     {"names": [], "emails": [], "phones": [], "documents": [], "locators": [], "addresses": [], "other": []}
   and stores only salted SHA-256 hashes of them (passenger names, emails, phone numbers and their masked
   forms, DNI/NIE/passport numbers, booking locators and numbers, addresses, anything else listed) in
   ~/.config/renfe-cli/pii-needles.json ($RENFE_PII_NEEDLES), mode 600. The check hashes candidate tokens
   of the text and compares, so it catches real values that look synthetic, like a locator in a test that
   matches a real booking. Without the file it's skipped with a note; a file that is there but
   unreadable or corrupt blocks until it's rebuilt.
3. Claude Haiku (`claude-haiku-5-5`) reads the text for what fixed rules can't see (a name, free text
   about someone's trip). With ANTHROPIC_API_KEY (environment, or the main checkout's gitignored .env) it
   calls the Messages API; otherwise the `claude` CLI on the user's Claude Code login, with no tools,
   settings, hooks or CLAUDE.md. If neither works it prints a note and lets the other layers decide: an
   outage never blocks a commit. It runs only when the other layers found nothing.

No layer prints a matched value. A line containing `pii:allow` is skipped by the pattern and model
layers; the known-values layer ignores the marker, because what it finds is the owner's real data.

Usage
  pii_check.py staged          added lines and paths of the staged diff     (.githooks/pre-commit and
                               pre-merge-commit, which a clean merge runs instead)
  pii_check.py msg <file>      a commit message file                        (.githooks/commit-msg)
  pii_check.py text            text on stdin, e.g. a PR body
  pii_check.py files [--patterns-only] <path>...   whole files (audits, calibration); --patterns-only
                               skips the model (patterns and known values still run)
  pii_check.py agent-hook      an agent's pre-shell-command hook payload on stdin (Claude Code, Codex,
                               Cursor): checks gh pr/issue/release/api text before it's posted, refuses
                               `git commit --no-verify`, and installs the git hooks on first use
  pii_check.py enable          install the git hooks for this clone (humans: once per clone)
  pii_check.py needles build   (re)build the known-values file from the hand-written values file
  pii_check.py needles status  where the file is, when it was built, hashes per kind
  pii_check.py --self-test     pattern and known-values layers only, no network

Exit codes: 0 clean, 2 blocked (Claude Code, Codex and Cursor all treat 2 as "deny").
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ALLOW_MARKER = "pii:allow"
MAINTAINER = "luisgonzaleznf"  # the maintainer's public GitHub handle
REPO = "renfe-cli"

EMAIL = re.compile(r"(?<![\w.%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
SAFE_DOMAINS = ("example.com", "example.org", "example.net", "example.es", "users.noreply.github.com",
                "noreply.github.com")
SAFE_TLDS = (".example", ".test", ".invalid", ".localhost", ".local")
ROLE_LOCAL_PARTS = {
    "support", "help", "hello", "hi", "info", "contact", "team", "sales", "billing", "security", "privacy",
    "legal", "noreply", "no-reply", "donotreply", "do-not-reply", "notifications", "notification", "admin",
    "postmaster", "abuse", "press", "careers", "jobs", "feedback", "partners", "api", "dev", "developers",
    "ops", "accounts", "receipts", "invoices", "service", "marketing", "dpo", "webmaster", "bot", "alerts",
    "reply", "git", "atencion", "atencioncliente", "atencion.cliente", "atencionalcliente", "clientes",
    "soporte", "contacto", "hola", "facturacion", "privacidad", "prensa", "comercial", "ventas", "venta",
    "reservas", "billetes", "viajeros", "reclamaciones",
}
PLACEHOLDER_LOCAL_PARTS = {
    "member", "user", "username", "owner", "someone", "somebody", "name", "first.last", "firstname.lastname",
    "you", "your.name", "yourname", "jane", "john", "jane.doe", "john.doe", "test", "tester", "example",
    "foo", "bar", "email", "client", "customer", "person", "demo", "sample", "holder", "usuario", "nombre",
    "cliente", "titular", "correo", "ejemplo", "prueba", "persona", "ana", "ana.ejemplo", "passenger",
    "traveller", "traveler", "buyer", "pasajero", "viajero", "comprador",
}

# Spanish numbers: nine digits from 6-9, compact or grouped 3-3-3 / 3-2-2-2, with an optional +34/0034.
ES_PHONE = re.compile(
    r"(?<![\w.*+@-])(?:(?:\+|00)34[ .-]?|\(\+34\)\s?)?"
    r"([6-9]\d{8}|[6-9]\d{2}[ .-]\d{3}[ .-]\d{3}|[6-9]\d{2}[ .-]\d{2}[ .-]\d{2}[ .-]\d{2})"
    r"(?![\w*]|[.,-]\d)"
)
ES_SERVICE_PREFIXES = ("800", "803", "806", "807", "900", "901", "902", "905")  # free-phone and premium lines
US_PHONE = re.compile(
    r"(?<![\w.+-])(?:\+|00)1[ .-]?\(?([2-9]\d{2})\)?[ .-]?([2-9]\d{2})[ .-]?(\d{4})(?![\w-])"
    r"|(?<![\w.+(-])\(([2-9]\d{2})\)[ ]?([2-9]\d{2})[ .-](\d{4})(?![\w-])"
)
US_TOLL_FREE = {"800", "833", "844", "855", "866", "877", "888"}

NIF_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
DNI = re.compile(r"(?<![\w-])(\d{8})-?([A-Za-z])(?![\w-])")
NIE = re.compile(r"(?<![\w-])([XYZxyz])-?(\d{7})-?([A-Za-z])(?![\w-])")

IBAN = re.compile(r"(?<!\w)([A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?)(?!\w)")
IBAN_LENGTHS = {"ES": 24, "AD": 24, "PT": 25, "FR": 27, "MC": 27, "IT": 27, "DE": 22, "GB": 22, "IE": 22,
                "NL": 18, "BE": 16, "LU": 20, "CH": 21, "AT": 20, "PL": 28, "SE": 24, "DK": 18, "NO": 15,
                "FI": 18}
EXAMPLE_IBANS = {  # the examples banks, ISO 13616 and Spanish documentation use
    "ES9121000418450200051332", "ES7921000813610123456789", "ES7620770024003102575766",
    "ES6000491500051234567892", "GB82WEST12345698765432", "GB29NWBK60161331926819",
    "DE89370400440532013000", "FR1420041010050500013M02606", "NL91ABNA0417164300", "BE68539007547034",
}
CCC = re.compile(r"(?<![\w-])(\d{4})[ -]?(\d{4})[ -]?(\d{2})[ -]?(\d{10})(?![\w-])")  # entity office DC account
CCC_WEIGHTS = (1, 2, 4, 8, 5, 10, 9, 7, 3, 6)

# Payment cards: a whole digit run, compact (13-19 digits) or in the groups cards are printed in
# (4-4-4-4, 4-4-4-4-3, Amex 4-6-5, Diners 4-6-4), never a slice of a longer grouped number (IBAN, CCC):
# no digit group right before it, and no 4-digit group right after a grouped one. An expiry or CVV
# after it is fine. Issuer ranges are 3-6 and Mastercard's 2221-2720, which keeps timestamps and dates out.
CARD = re.compile(
    r"(?<![\w.+-])(?<!\d[ -])(?:"
    r"((?:2[2-7]|[3-6]\d)\d{11,17})(?![\w-]|[.,]\d)"
    r"|((?:2[2-7]|[3-6]\d)\d{2}([ -])\d{4}\3\d{4}\3\d{4}(?:\3\d{3})?|3\d{3}([ -])\d{6}\4\d{4,5})"
    r"(?![\w-]|[.,]\d|[ -]\d{4}))"
)
TEST_CARDS = {  # the test numbers payment gateways (Stripe, Adyen, Braintree, Redsys) document
    "4111111111111111", "4242424242424242", "4012888888881881", "4222222222222", "4000056655665556",
    "4000002500003155", "4000000000003220", "4000002760003184", "4000000000009995", "4000000000000002",
    "4000000000000077", "4548812049400004", "4917610000000000", "4988438843884305", "5555555555554444",
    "5105105105105100", "5200828282828210", "5454545454545454", "5425233430109903", "5577000055770004",
    "2223003122003222", "2223000048400011", "378282246310005", "371449635398431", "378734493671000",
    "6011111111111117", "6011000990139424", "6011601160116611", "3056930009020004", "36227206271667",
    "3566002020360505", "3530111333300000", "6200000000000005",
}

# Booking locators ("localizador", usually six letters and digits) and booking numbers, flagged only
# right after a word that says what they are, so station codes, train numbers and hashes in code aren't.
# Field names count too, in snake_case or camelCase (codigo_reserva, bookingNumber, recordLocator).
_LOCATOR_WORDS = r"localizador(?:es)?|(?:record[ _-]?)?locators?|pnr(?:[ _-]?code)?"
_BOOKING_WORDS = (r"(?:booking|reservation|purchase)[ _-]?(?:ref(?:erence)?|number|no|code|id)|"
                  r"(?:c[oó]digo|n[uú]mero|n\.?[º°]|referencia)[ _-]*(?:de[ _-]+)?(?:la[ _-]+)?(?:reserva|compra)")
_WORD_START = r"(?:(?<![A-Za-z0-9])|(?<=[a-z])(?=[A-Z]))"  # a word, or the next word of a camelCase name
LOCATOR = re.compile(
    rf"{_WORD_START}(?i:(?P<locator>{_LOCATOR_WORDS})|(?P<booking>{_BOOKING_WORDS}))(?![A-Za-z0-9])"
    r"(?i:\s+(?:is|es|was|era|code|c[oó]digo|number|n[uú]mero|del?\s+(?:la\s+)?(?:reserva|billete|compra)))?"
    r"[^\w\n]{1,6}(?P<token>[A-Za-z0-9]{5,13})(?![A-Za-z0-9_-])"
)
# Ticket numbers: only digits (six or more) after billete, Nº billete or ticket number.
TICKET_NUMBER = re.compile(
    rf"{_WORD_START}(?i:billetes?|ticket[ _-]?(?:number|no))(?![A-Za-z0-9])"
    r"(?i:\s*(?:n[uú]mero|n\.?\s?[º°o]\.?|number|no\.?|#))?"
    r"[^\w\n]{1,6}(\d{6,19})(?![\w-]|[.,]\d)"
)
PLACEHOLDER_LOCATORS = {"ABC123", "ABC1234", "AB1234", "XYZ123", "XYZ789", "A1B2C3", "X1Y2Z3", "AAA111",
                        "ZZZ999", "ABCD1234"}
# Six capitals after localizador, locator or PNR are a locator, unless they're a placeholder or one of the
# words that follow those words in table headers, enums and docs.
PLACEHOLDER_LETTERS = {
    "QWERTY", "ASDFGH", "ORIGEN", "ORIGIN", "PRECIO", "ESTADO", "STATUS", "NUMBER", "NUMERO", "CODIGO", "STRING",
    "FORMAT", "LENGTH", "TICKET", "FIELDS", "PREFIX", "SUFFIX", "VALUES", "COLUMN", "HEADER", "RECORD", "SEARCH",
    "RESULT", "ACTIVE", "FAILED", "CLOSED", "ISSUED", "PAGADO", "ACTIVO", "VALIDO", "NOMBRE", "FECHAS", "VIAJES",
    "TRENES", "LOOKUP", "UNIQUE", "OBJECT", "SCHEMA", "BUSCAR", "PLAZAS", "SAMPLE", "RANDOM", "HIDDEN", "OCULTO",
    "MASKED", "CHANGE", "UPDATE", "DELETE", "INSERT", "SELECT", "CREATE", "CANCEL", "TARIFA", "NOTSET", "PARSED",
    "FILTER", "MANUAL", "SOURCE", "TARGET", "PRUEBA", "FICTIC",
}

STREET_TYPES = (r"Calle|Avenida|Avda\.?|Av\.|Plaza|Pza\.|Paseo|Ronda|Rda\.|Carretera|Ctra\.|Camino|Travesía|"
                r"Glorieta|Rambla|Vía|Pasaje|Alameda|Bulevar|Callejón|Cuesta|Urbanización|Urb\.")
_STREET_WORD = r"[A-ZÁÉÍÓÚÑÜ][\w'.-]*"
_STREET_LINK = r"(?:de\s+(?:la\s+|las\s+|los\s+)?|del\s+|el\s+|la\s+|y\s+)"
ADDRESS = re.compile(
    rf"(?<![\w/])(?:(?:{STREET_TYPES})\s+|C/\s*){_STREET_LINK}?({_STREET_WORD}(?:\s+{_STREET_LINK}?{_STREET_WORD}){{0,4}})"
    r",?\s+(?:n[º°o]\.?\s*|núm\.?\s*|número\s+)?\d{1,4}(?!\d|[.,]\d)"
)
ADDRESS_CAPS = re.compile(  # the abbreviated, upper-case form billing systems print: CL FALSA 12
    r"(?<![\w/])(CL|AV|AVDA|PZ|PS|CTRA|CM|RD|TR|GL|UR|PJ)\.?\s+((?:[A-ZÁÉÍÓÚÑÜ'.-]{2,}\s+){1,5})\d{1,4}(?!\w)"
)
PLACEHOLDER_STREETS = {"falsa", "ejemplo", "example", "fake", "test", "prueba", "inventada", "ficticia",
                       "imaginaria", "sample", "placeholder"}
POSTAL_TOWN = re.compile(  # a postal code (01000-52999) followed by a capitalised town name
    r"(?<![\w.,/-])((?:0[1-9]|[1-4]\d|5[0-2])\d{3}),?\s+\(?([A-ZÁÉÍÓÚÑÜ][a-záéíóúñü]{2,}|[A-ZÁÉÍÓÚÑÜ]{3,})\b"
)
PLACEHOLDER_POSTAL = {"01234", "11111", "12345", "22222", "33333", "44444"}
# A postal code's first two digits are its province. Renfe's five-digit station codes don't follow that,
# so "17000 Madrid-Chamartín" or "51003 Sevilla-Santa Justa" is a station, not an address.
TOWN_PROVINCES = {
    "vitoria": "01", "gasteiz": "01", "albacete": "02", "alicante": "03", "alacant": "03", "elche": "03",
    "elx": "03", "benidorm": "03", "villena": "03", "almeria": "04", "avila": "05", "badajoz": "06",
    "merida": "06", "palma": "07", "ibiza": "07", "eivissa": "07", "barcelona": "08", "badalona": "08",
    "terrassa": "08", "sabadell": "08", "mataro": "08", "manresa": "08", "granollers": "08", "burgos": "09",
    "miranda": "09", "caceres": "10", "plasencia": "10", "cadiz": "11", "jerez": "11", "algeciras": "11",
    "castellon": "12", "castello": "12", "puertollano": "13", "cordoba": "14", "coruna": "15",
    "santiago": "15", "ferrol": "15", "cuenca": "16", "girona": "17", "gerona": "17", "figueres": "17",
    "granada": "18", "guadalajara": "19", "donostia": "20", "irun": "20", "huelva": "21", "huesca": "22",
    "jaen": "23", "linares": "23", "leon": "24", "ponferrada": "24", "astorga": "24", "lleida": "25",
    "lerida": "25", "logrono": "26", "lugo": "27", "monforte": "27", "madrid": "28", "aranjuez": "28",
    "getafe": "28", "mostoles": "28", "fuenlabrada": "28", "leganes": "28", "alcobendas": "28",
    "malaga": "29", "marbella": "29", "antequera": "29", "murcia": "30", "cartagena": "30", "lorca": "30",
    "pamplona": "31", "iruna": "31", "tudela": "31", "ourense": "32", "orense": "32", "oviedo": "33",
    "gijon": "33", "aviles": "33", "mieres": "33", "palencia": "34", "pontevedra": "36", "vigo": "36",
    "salamanca": "37", "santander": "39", "torrelavega": "39", "segovia": "40", "sevilla": "41",
    "soria": "42", "tarragona": "43", "reus": "43", "tortosa": "43", "teruel": "44", "toledo": "45",
    "talavera": "45", "valencia": "46", "gandia": "46", "xativa": "46", "sagunto": "46",
    "valladolid": "47", "bilbao": "48", "zamora": "49", "zaragoza": "50", "calatayud": "50", "ceuta": "51",
    "melilla": "52",
}
# Words and Renfe train types that make a line about trains: there "11014 Villaejemplo" is a station and
# "AV MADRID 3100" a train, not an address. The short codes only count in capitals.
RAIL_CONTEXT = re.compile(
    r"(?i:\b(?:estaci[oó]n(?:es)?|estaci[oó]|stations?|apeaderos?|paradas?|origen|origin|destino|destination|uic|"
    r"trenes|tren|trains?|cercan[ií]as|rodalies|avlo|alvia|avant|euromed|intercity)\b)"
    r"|\b(?:AVE|MD|LD|REG|(?:AV|AVE|LD|MD)\d{1,2})\b"
)

BLOCKED_EXTS = {"har", "pdf", "pkpass", "csv", "tsv", "xlsx", "xls", "ods", "jsonl", "ndjson", "parquet", "zip",
                "gz", "tgz", "tar", "7z", "cookies", "part", "eml", "msg", "sqlite", "db",
                "png", "jpg", "jpeg", "heic", "heif", "webp", "gif", "bmp", "tiff", "tif"}  # ticket screenshots

HINT = ("Replace it with an obviously fake value (600000001, 00000000T, user@example.com, Ana Ejemplo, Calle "
        "Falsa 123, locator XXXXXX, card 4111 1111 1111 1111) or a placeholder (<phone>, <DNI>, <locator>); "
        "keep HAR captures, tickets (PDF, pkpass), token files and output of real bookings out of git. A "
        "known-value hit is the owner's real data even if it looks synthetic: change the value, "
        f"`{ALLOW_MARKER}` doesn't apply to it. For any other false alarm, show the user the flagged lines "
        f"and ask before adding `{ALLOW_MARKER}` (with a reason) to them.")

Lines = list[tuple[int, str]]
Blocks = list[tuple[str, Lines]]


@dataclass(frozen=True)
class Finding:
    where: str
    kind: str


# --- pattern layer -------------------------------------------------------------------------------

def synthetic(digits: str) -> bool:
    """Obviously invented digits: five equal in a row (600000001) or six in sequence (612345678)."""
    if re.search(r"(\d)\1{4}", digits):
        return True
    return any(digits[i:i + 6] in "0123456789" or digits[i:i + 6] in "9876543210" for i in range(len(digits) - 5))


def _email_ok(local: str, domain: str) -> bool:
    local, domain = local.lower(), domain.lower()
    if any(domain == d or domain.endswith("." + d) for d in SAFE_DOMAINS):
        return True
    if domain.endswith(SAFE_TLDS) or local in ROLE_LOCAL_PARTS or local in PLACEHOLDER_LOCAL_PARTS:
        return True
    if local.split("+")[0] in ROLE_LOCAL_PARTS or "noreply" in local or "no-reply" in local:
        return True
    return len(local) == 1 or bool(re.fullmatch(r"x+|\.+|_+|<.*>|\{.*\}|\$.*", local))  # a@b.com, xxx@, {email}@


def _es_phone_personal(number: str) -> bool:
    digits = re.sub(r"\D", "", number)
    return not digits.startswith(ES_SERVICE_PREFIXES) and not synthetic(digits)


def _us_phone_personal(m: re.Match) -> bool:
    area, exch, num = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(4), m.group(5), m.group(6))
    return area not in US_TOLL_FREE and exch != "555" and not synthetic(area + exch + num)


def _nif_valid(number: str, letter: str) -> bool:
    return NIF_LETTERS[int(number) % 23] == letter.upper() and not synthetic(number)


def _iban_personal(raw: str) -> bool:
    iban = raw.replace(" ", "")
    country = iban[:2]
    if len(iban) != IBAN_LENGTHS.get(country) or iban in EXAMPLE_IBANS:
        return False
    if country == "ES" and not iban[2:].isdigit():
        return False
    if int("".join(str(int(c, 36)) for c in iban[4:] + iban[:4])) % 97 != 1:
        return False
    return not synthetic(re.sub(r"\D", "", iban[4:]))


def _ccc_digit(ten: str) -> int:
    d = 11 - sum(int(c) * w for c, w in zip(ten, CCC_WEIGHTS)) % 11
    return {10: 1, 11: 0}.get(d, d)


def _ccc_personal(entity: str, office: str, dc: str, account: str) -> bool:
    valid = dc == f"{_ccc_digit('00' + entity + office)}{_ccc_digit(account)}"
    return valid and not synthetic(entity + office + dc + account)


def luhn(digits: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(digits)):
        n = int(c) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def _card_personal(raw: str) -> bool:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("2") and not 2221 <= int(digits[:4]) <= 2720:
        return False
    return 13 <= len(digits) <= 19 and luhn(digits) and digits not in TEST_CARDS and not synthetic(digits)


def _card_match_personal(m: re.Match) -> bool:
    """A card number, including a 4-4-4-4 one followed by its CVV (which reads as 4-4-4-4-3)."""
    if m.group(1):
        return _card_personal(m.group(1))
    digits = re.sub(r"\D", "", m.group(2))
    return _card_personal(digits) or (bool(m.group(3)) and len(digits) == 19 and _card_personal(digits[:16]))


def _run(group: str) -> bool:
    """One character repeated (AAA, 000) or an ascending run (ABC, 123)."""
    return len(set(group)) == 1 or all(ord(b) - ord(a) == 1 for a, b in zip(group, group[1:]))


def _code_number_personal(token: str) -> bool:
    """A locator, booking or ticket number that isn't a placeholder. Five digits alone are a station code
    or a train number (60000, 03063), not a booking."""
    up = token.upper()
    if up in PLACEHOLDER_LOCATORS or "XXX" in up or (up.isdigit() and len(up) < 6):
        return False
    if re.search(r"(.)\1{3}", up):  # four equal in a row: X0000, 00000A
        return False
    groups = re.findall(r"[A-Z]+|\d+", up)
    if len(groups) <= 2 and all(_run(g) for g in groups):  # ABC123, XYZ789, AB1234, 123456
        return False
    return not (up.isdigit() and synthetic(up))


def _locator_personal(m: re.Match, line: str) -> bool:
    """A real-looking token after a locator or booking word.

    Letters and digits, in either case; lowercase ones that read as code (a git SHA, `loc2.upper()`,
    `fn1(`) or follow an `id` field are left alone. Letters only: exactly six capitals after
    localizador, locator or PNR, unless a placeholder; other letter-only tokens are words (headers,
    PENDING)."""
    token = m.group("token")
    if token.isalpha():
        return (bool(m.group("locator")) and token.isupper() and len(token) == 6 and "XXX" not in token
                and not _run(token) and token not in PLACEHOLDER_LETTERS)
    if token != token.upper():
        after = line[m.end():m.end() + 2]
        if (re.fullmatch(r"[0-9a-f]{7,}", token) or re.match(r"[(\[]|\.[A-Za-z_]", after)
                or (m.group("booking") or "").lower().endswith("id")):
            return False
    return _code_number_personal(token)


def _street_personal(name: str) -> bool:
    return not any(w.lower() in PLACEHOLDER_STREETS for w in re.findall(r"\w+", name))


def _postal_personal(code: str, town: str, rail: bool) -> bool:
    if code in PLACEHOLDER_POSTAL or rail:
        return False
    province = TOWN_PROVINCES.get(fold(town))
    return province is None or province == code[:2]


def scan_line(line: str) -> list[str]:
    """Kinds of PII-looking patterns in one line (never the values)."""
    if ALLOW_MARKER in line:
        return []
    kinds = []
    if any(not _email_ok(m.group(1), m.group(2)) for m in EMAIL.finditer(line)):
        kinds.append("email")
    if (any(_es_phone_personal(m.group(1)) for m in ES_PHONE.finditer(line))
            or any(_us_phone_personal(m) for m in US_PHONE.finditer(line))):
        kinds.append("phone number")
    if (any(_nif_valid(m.group(1), m.group(2)) for m in DNI.finditer(line))
            or any(_nif_valid(str("XYZ".index(m.group(1).upper())) + m.group(2), m.group(3))
                   for m in NIE.finditer(line))):
        kinds.append("DNI/NIF/NIE")
    if any(_card_match_personal(m) for m in CARD.finditer(line)):
        kinds.append("payment card number")
    if (any(_locator_personal(m, line) for m in LOCATOR.finditer(line))
            or any(_code_number_personal(m.group(1)) for m in TICKET_NUMBER.finditer(line))):
        kinds.append("booking locator or number")
    if any(_iban_personal(m.group(1)) for m in IBAN.finditer(line)):
        kinds.append("IBAN")
    if any(_ccc_personal(*m.groups()) for m in CCC.finditer(line)):
        kinds.append("bank account number")
    rail = bool(RAIL_CONTEXT.search(line))
    streets = [m.group(1) for m in ADDRESS.finditer(line)]
    streets += [m.group(2) for m in ADDRESS_CAPS.finditer(line) if not (rail and m.group(1) == "AV")]  # AV: a train
    if any(_street_personal(name) for name in streets):
        kinds.append("street address")
    if any(_postal_personal(m.group(1), m.group(2), rail) for m in POSTAL_TOWN.finditer(line)):
        kinds.append("postal code + town")
    return kinds


def numbered(text: str) -> Lines:
    return list(enumerate(text.splitlines(), 1))


def scan_lines(lines: Lines, label: str) -> list[Finding]:
    """Pattern and known-value findings for these lines."""
    return [Finding(f"{label}:{n}", k) for n, line in lines for k in scan_line(line)] + known_values(lines, label)


def scan_text(text: str, label: str) -> list[Finding]:
    return scan_lines(numbered(text), label)


def blocked_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    ext = name.rsplit(".", 1)[1].lower() if "." in name.lstrip(".") else ""
    return (".local-secrets" in parts[:-1] or name == ".local-secrets"
            or bool(re.fullmatch(r"(?i)tokens.*\.json|pii-(?:needles|values).*\.json", name))
            or name == ".env" or (name.startswith(".env.") and name not in (".env.example", ".env.sample"))
            or ext in BLOCKED_EXTS)


def git_bytes(*args: str, cwd: str | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def git(*args: str, cwd: str | None = None) -> str:
    """Git's output as text, read as bytes so a lone CR stays inside its line; bytes that aren't UTF-8 (a
    Latin-1 file, an odd path) become U+FFFD."""
    return git_bytes(*args, cwd=cwd).decode("utf-8", "replace")


# --text --no-textconv: files git (or a `-diff` attribute) calls binary are diffed as their bytes too.
GIT_DIFF = ("-c", "core.quotePath=false", "diff", "--cached", "--no-color", "--no-ext-diff", "--no-renames",
            "--src-prefix=a/", "--dst-prefix=b/", "--text", "--no-textconv")
CHANGED = "AMT"  # added, modified, type changed (a symlink replaced by a file)
HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
NOT_SCANNED = ("not scanned: git treats it as binary (keep it out of git, or convert it to text; the user can "
               "check it and commit it themselves)")


def _unquote(text: str) -> str:
    """A path as git prints it in a diff header: as is, or C-quoted when it has unusual characters."""
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text
    body, out, i = text[1:-1], bytearray(), 0
    while i < len(body):
        if body[i] == "\\" and re.match(r"[0-7]{3}", body[i + 1:i + 4]):
            out.append(int(body[i + 1:i + 4], 8))
            i += 4
        elif body[i] == "\\" and body[i + 1:i + 2] in C_ESCAPES:
            out.append(C_ESCAPES[body[i + 1]])
            i += 2
        else:
            out += body[i].encode()
            i += 1
    return out.decode(errors="replace")


def added_lines(diff: str) -> dict[str, Lines]:
    """Added lines per path from `git diff -U0`. Hunks are read by their line counts, and `+++ ` is a file
    header only right after `--- `, so an added line that looks like a header stays content."""
    lines: dict[str, Lines] = {}
    path, prev, lineno, old_left, new_left = "", "", 0, 0, 0
    for line in diff.split("\n"):
        after_minus, prev = prev.startswith("--- "), line
        if old_left or new_left:
            if line.startswith("+") and new_left:
                if path:
                    lines.setdefault(path, []).append((lineno, line[1:].removesuffix("\r")))
                lineno, new_left = lineno + 1, new_left - 1
                continue
            if line.startswith("-") and old_left:
                old_left -= 1
                continue
            if line.startswith("\\"):  # \ No newline at end of file
                continue
            old_left = new_left = 0  # not what the hunk header promised: read headers again
        if line.startswith("diff --git "):
            path = ""
        elif line.startswith("+++ ") and after_minus:
            target = _unquote(line[4:].removesuffix("\t"))  # git adds a tab after a name with a space
            path = target[2:] if target.startswith("b/") else ""
        else:
            m = HUNK.match(line)
            if m:
                old_left = int(m.group(1)) if m.group(1) is not None else 1
                lineno = int(m.group(2))
                new_left = int(m.group(3)) if m.group(3) is not None else 1
    return lines


def decode_wide(data: bytes) -> str | None:
    """UTF-16 or UTF-32 text (by its BOM, or UTF-16 by where its NULs fall), or None when it isn't."""
    boms = ((b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"), (b"\xff\xfe", "utf-16-le"),
            (b"\xfe\xff", "utf-16-be"))
    encoding = next(((enc, len(bom)) for bom, enc in boms if data.startswith(bom)), None)
    if encoding is None and len(data) >= 2:
        sample = data[:4096]
        even, odd = sample[0::2].count(0) / len(sample[0::2]), sample[1::2].count(0) / max(len(sample[1::2]), 1)
        if odd > 0.9 and even < 0.1:
            encoding = ("utf-16-le", 0)
        elif even > 0.9 and odd < 0.1:
            encoding = ("utf-16-be", 0)
    if encoding is None:
        return None
    try:
        text = data[encoding[1]:].decode(encoding[0])
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text


def path_findings(path: str, where: str) -> list[Finding]:
    """Findings for a file's name: a locator, name or number can sit in it (trip_<locator>.json). They're
    reported at `where`, an alias, since the path itself holds the value (in the file or a folder name)."""
    kinds = set(scan_line(path)) | set(scan_line(path.replace("_", " ")))
    found = [Finding(where, f"{kind} in the file name") for kind in sorted(kinds)]
    return found + [Finding(where, f"{f.kind} in the file name") for f in known_values([(1, path)], path)]


def scan_staged() -> tuple[list[Finding], Blocks]:
    """Pattern and known-value findings for the staged diff (paths included), and its added lines per file
    for the model. A file whose name holds personal data is called "staged file #N" in everything printed
    or sent to the model."""
    findings, labels, blocked, changed = [], {}, set(), []
    fields = git(*GIT_DIFF, "--name-status", "-z").split("\0")
    for n, (status, path) in enumerate(zip(fields[0::2], fields[1::2]), 1):
        if not status or status[0] not in CHANGED:
            continue
        named = path_findings(path, f"staged file #{n}")
        labels[path] = f"staged file #{n}" if named else path
        findings += named
        changed.append(path)
        if blocked_path(path):
            findings.append(Finding(labels[path], "data file that never belongs in git"))
            blocked.add(path)
    numstat = git(*GIT_DIFF, "--numstat", "-z", f"--diff-filter={CHANGED}").split("\0")
    grew = {row.split("\t", 2)[2] for row in numstat if row.count("\t") >= 2 and not row.startswith("0\t")}
    parsed = added_lines(git(*GIT_DIFF, "-U0", f"--diff-filter={CHANGED}"))
    added = []
    for path in changed:
        label, file_lines = labels[path], parsed.get(path, [])
        if any("\x00" in text for _, text in file_lines):  # binary bytes, or UTF-16/32 text
            wide = decode_wide(git_bytes("cat-file", "blob", f":{path}"))
            file_lines = numbered(wide) if wide is not None else []
            if wide is None and path not in blocked:
                findings.append(Finding(label, NOT_SCANNED))
        elif not file_lines and path in grew and path not in blocked:
            findings.append(Finding(label, NOT_SCANNED))  # git says lines were added, none were read
        findings += scan_lines(file_lines, label)
        if file_lines and path not in blocked:
            added.append((label, file_lines))
    return findings, added


def message_lines(path: str) -> Lines:
    """Every line above the scissors (`git commit -v`). Comment lines too: `git commit -m "# ..."` keeps them."""
    lines = []
    for n, line in numbered(Path(path).read_text(errors="replace")):
        if re.match(r"\S -+ >8 -+$", line):  # the diff below the scissors isn't the message
            break
        lines.append((n, line))
    return lines


# --- known-values layer (needles) ----------------------------------------------------------------

NEEDLES_ENV = "RENFE_PII_NEEDLES"
VALUES_ENV = "RENFE_PII_VALUES"
VALUE_KINDS = {"names": "name", "emails": "email", "phones": "phone", "documents": "document",
               "locators": "locator", "addresses": "address", "other": "other"}
VALUES_TEMPLATE = json.dumps({key: [] for key in VALUE_KINDS})
KIND_LABELS = {"phone": "phone number", "masked": "masked phone number", "document": "DNI/NIE/passport",
               "locator": "booking locator or number", "email": "email", "name": "passenger name",
               "address": "address", "other": "other listed value"}
# Dropped before word pairs are formed, on both sides, so "Calle de la Prueba 5" pairs as "prueba 5".
STOPWORDS = {"de", "del", "la", "las", "los", "el", "y", "e", "i", "d", "l", "a", "calle", "c", "cl", "avenida",
             "avda", "av", "plaza", "pza", "pz", "paseo", "ps", "ronda", "rda", "rd", "carretera", "ctra", "camino",
             "cm", "travesia", "tr", "glorieta", "gl", "rambla", "via", "pasaje", "pj", "urbanizacion", "urb", "n",
             "no", "num", "numero", "piso", "planta", "puerta", "pta", "esc", "escalera", "bajo", "bis"}
# Words of public place and station names (Málaga-María Zambrano, Valencia-Joaquín Sorolla, Toledo): a
# passenger name word among them only counts next to another name word, so station names stay usable.
PLACE_WORDS = set(TOWN_PROVINCES) | {
    "atocha", "almudena", "grandes", "puerta", "chamartin", "clara", "campoamor", "sants", "franca", "passeig",
    "gracia", "clot", "arago", "santa", "justa", "maria", "zambrano", "joaquin", "sorolla", "delicias",
    "abando", "indalecio", "prieto", "principe", "pio", "campo", "grande", "julio", "anguita", "fernando",
    "zobel", "rosa", "manzano", "guiomar", "vilafant", "pirineus", "llanos", "herrera", "urzaiz", "empalme",
    "nord", "norte", "estacio", "estacion", "terminal", "central", "camp", "ana", "carmen", "san", "sant",
    "cruz", "plana", "henares", "compostela", "medina", "alcala", "todas",
}
WORD = re.compile(r"[^\W_]+")
DIGIT_GROUPS = re.compile(r"(?<![\d])(?:\+|00)?\d{1,4}(?:[ .()-]{1,2}\d{1,4}){1,6}(?!\d)")
ALNUM_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")
MASKED = re.compile(r"(?<![\d*•xX·])(\d{1,3})?[*•xX·]{2,}(\d{2,4})(?!\d)")

_needles_cache: dict[str, tuple[str, dict[str, str]] | str | None] = {}


class NeedlesUnreadable(Exception):
    """The needles file is there but can't be used: block rather than skip the check."""


def fold(text: str) -> str:
    """Lower case without accents: José -> jose."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()


def _national(digits: str) -> str:
    if digits.startswith("0034"):
        return digits[4:]
    if digits.startswith("34") and len(digits) == 11:
        return digits[2:]
    if digits.startswith("001") and len(digits) == 13:
        return digits[3:]
    if digits.startswith("1") and len(digits) == 11:
        return digits[1:]
    return digits


def _digit_tokens(digits: str) -> set[str]:
    return {"d:" + d for d in (digits, _national(digits)) if len(d) >= 5}


def candidates(line: str) -> set[str]:
    """Normalised tokens of one line that a known value could hash to."""
    out: set[str] = set()
    for m in re.finditer(r"\d+", line):
        out |= _digit_tokens(m.group())
    for m in DIGIT_GROUPS.finditer(line):  # 612 34 56 78, +34-612-345-678, card groups: every run of adjacent groups
        groups = re.findall(r"\d+", m.group())
        for i in range(len(groups)):
            for j in range(i + 2, len(groups) + 1):
                joined = "".join(groups[i:j])
                if 7 <= len(joined) <= 19:
                    out |= _digit_tokens(joined)
    for m in ALNUM_TOKEN.finditer(line):  # DNI/NIE, passports, locators, other alphanumeric codes
        tok = m.group().upper()  # whole, and every run of parts joined: trip-<locator>.json, doc_<PAA-000000>
        parts = re.split(r"[-_]", tok)
        for piece in {tok, *("".join(parts[i:j]) for i in range(len(parts)) for j in range(i + 1, len(parts) + 1))}:
            if len(piece) >= 5 and any(c.isdigit() for c in piece) and any(c.isalpha() for c in piece):
                out.add("a:" + piece)
    for m in MASKED.finditer(line):  # 6*****789, ***6789
        first, last = m.group(1), m.group(2)
        if first and len(last) >= 3:
            out.add(f"m:{first[0]}|{last[-3:]}")
            if len(last) == 4:
                out.add(f"m:{first[0]}|{last}")
        if len(last) == 4:
            out.add(f"l:{last}")
    for m in EMAIL.finditer(line):
        out.add("e:" + m.group(0).lower())
    words = WORD.findall(fold(line))
    out |= {"w:" + w for w in words if len(w) >= 3 and w.isalpha()}
    kept = [w for w in words if w not in STOPWORDS]
    out |= {f"p:{a} {b}" for a, b in zip(kept, kept[1:])}
    return out


def _hash(salt: str, token: str) -> str:
    return hashlib.sha256(f"{salt}\0{token}".encode()).hexdigest()


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "")
    return (Path(base) if os.path.isabs(base) else Path.home() / ".config") / REPO


def needles_path() -> Path:
    return Path(os.environ[NEEDLES_ENV]).expanduser() if os.environ.get(NEEDLES_ENV) else config_home() / "pii-needles.json"


def values_path() -> Path:
    return Path(os.environ[VALUES_ENV]).expanduser() if os.environ.get(VALUES_ENV) else config_home() / "pii-values.json"


def load_needles() -> tuple[str, dict[str, str]] | None:
    """(salt, hash -> kind) from the needles file, or None when there is none. A file that is there but
    unreadable or corrupt raises NeedlesUnreadable: that's a broken check, not a missing one."""
    path = needles_path()
    key = str(path)
    if key not in _needles_cache:
        try:
            text = path.read_text()
        except FileNotFoundError:
            _needles_cache[key] = None
        except (OSError, ValueError):
            _needles_cache[key] = "unreadable"
        else:
            try:
                data = json.loads(text)
                table = {h: kind for kind, hashes in data["kinds"].items() for h in hashes
                         if isinstance(kind, str) and isinstance(h, str)}
                if not isinstance(data["salt"], str) or not isinstance(data["kinds"], dict):
                    raise TypeError
                _needles_cache[key] = (data["salt"], table)
            except (ValueError, KeyError, TypeError, AttributeError):
                _needles_cache[key] = "corrupt"
    state = _needles_cache[key]
    if isinstance(state, str):
        raise NeedlesUnreadable(f"the known-values file {path} is {state}; rebuild it with "
                                "`python3 .githooks/pii_check.py needles build`")
    return state


def needle_kinds(line: str, salt: str, table: dict[str, str]) -> set[str]:
    return {table[h] for h in (_hash(salt, c) for c in candidates(line)) if h in table}


_needles_note_shown = False


def known_values(lines: Lines, label: str) -> list[Finding]:
    """Findings for lines holding a known personal value. `pii:allow` doesn't apply: a hit is real data."""
    global _needles_note_shown
    needles = load_needles()
    if needles is None:
        if not _needles_note_shown:
            print(f"pii check: known-values check skipped: no {needles_path()} (list the values in "
                  f"{values_path()} and run `python3 .githooks/pii_check.py needles build`)", file=sys.stderr)
            _needles_note_shown = True
        return []
    salt, table = needles
    return [Finding(f"{label}:{n}", f"known personal value: {KIND_LABELS.get(k, k)}")
            for n, line in lines for k in sorted(needle_kinds(line, salt, table))]


def _phone_shaped(value: str) -> bool:
    digits = re.sub(r"[\s.()+-]", "", value)
    return digits.isdigit() and len(_national(digits)) == 9 and _national(digits)[0] in "6789"


def _document_shaped(value: str) -> bool:
    return bool(re.fullmatch(r"\d{8}-?[A-Za-z]|[XYZxyz]-?\d{7}-?[A-Za-z]", value.strip()))


def _classify(value: str) -> str:
    """The kind of a value listed under "other": email, phone, document or other."""
    if EMAIL.fullmatch(value.strip()):
        return "email"
    if _phone_shaped(value):
        return "phone"
    return "document" if _document_shaped(value) else "other"


def _code_tokens(code: str, kind: str) -> dict[str, str]:
    """Tokens for one code with its separators removed: digits, letters, or both."""
    if code.isdigit():
        return {"d:" + code: kind} if len(code) >= 5 else {}
    if code.isalpha():
        return {"w:" + fold(code): kind} if len(code) >= 3 else {}
    tokens = {"a:" + code.upper(): kind} if len(code) >= 5 and re.fullmatch(r"[A-Za-z0-9]+", code) else {}
    tokens.update({"d:" + run: kind for run in re.findall(r"\d{6,}", code)})
    return tokens


def _word_pairs(value: str, kind: str) -> dict[str, str]:
    """Adjacent word pairs, stopwords dropped, where at least one word is a real word (street names)."""
    words = [w for w in WORD.findall(fold(value)) if w not in STOPWORDS]
    return {f"p:{a} {b}": kind for a, b in zip(words, words[1:]) if any(len(w) >= 3 and w.isalpha() for w in (a, b))}


def value_tokens(kind: str, value: str) -> dict[str, str]:
    """Normalised token -> needle kind for one listed value."""
    tokens: dict[str, str] = {}
    if kind == "phone":
        digits = re.sub(r"\D", "", value)
        nat = _national(digits)
        tokens.update({t: "phone" for t in _digit_tokens(digits)})
        if len(nat) >= 7:
            tokens.update({f"m:{nat[0]}|{nat[-3:]}": "masked", f"m:{nat[0]}|{nat[-4:]}": "masked",
                           f"l:{nat[-4:]}": "masked"})
    elif kind == "document":
        doc = re.sub(r"[\s.-]", "", value).upper()
        if len(doc) >= 5 and re.fullmatch(r"[A-Z0-9]+", doc) and not doc.isdigit() and not doc.isalpha():
            tokens["a:" + doc] = "document"
        digits = re.sub(r"\D", "", doc)
        if len(digits) >= (5 if doc.isdigit() else 7):
            tokens["d:" + digits] = "document"
    elif kind == "locator":
        tokens.update(_code_tokens(re.sub(r"[\s-]", "", value).upper(), "locator"))
    elif kind == "email":
        tokens["e:" + value.lower()] = "email"
        local = fold(value.split("@", 1)[0])
        if len(local) >= 6 and local.isalpha():
            tokens["w:" + local] = "email"
    elif kind == "name":
        words = [w for w in WORD.findall(fold(value)) if w not in STOPWORDS and w.isalpha()]
        tokens.update({f"p:{a} {b}": "name" for a in words for b in words if a != b})  # any order: SURNAME, NAME
        # Name words alone, except those in the maintainer's public handle (a relative may share a surname
        # with them) and public place names: those only count next to another name word.
        tokens.update({"w:" + w: "name" for w in words if len(w) >= 3 and w not in MAINTAINER and w not in PLACE_WORDS})
    elif kind == "address":
        tokens.update(_word_pairs(value, "address"))
    else:
        compact = re.sub(r"[\s.-]", "", value)
        if compact.isalnum() and (any(c.isdigit() for c in compact) or len(value.split()) == 1):
            tokens.update(_code_tokens(compact.upper(), "other"))
        tokens.update(_word_pairs(value, "other"))
    return tokens


def needle_tokens(values: list[tuple[str, str]]) -> dict[str, str]:
    """Normalised token -> needle kind for these (kind, value) pairs."""
    tokens: dict[str, str] = {}
    for kind, value in values:
        tokens.update(value_tokens(kind, value))
    return tokens


def read_values(path: Path) -> tuple[list[tuple[str, str, str]] | None, list[str]]:
    """((kind, value, where) for every listed value, problems), or (None, problems) when the file can't be
    used. `where` is the key and index (names[0]); no problem message holds anything from the file."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None, [f"no values file at {path}"]
    except OSError as e:
        return None, [f"can't read {path} ({e.strerror or 'error'})"]
    except ValueError as e:
        return None, [f"{path} isn't valid JSON (line {getattr(e, 'lineno', '?')})"]
    if not isinstance(data, dict):
        return None, [f"{path} must hold one JSON object"]
    values, problems = [], []
    for i, (key, items) in enumerate(data.items(), 1):
        if key not in VALUE_KINDS:
            problems.append(f"key #{i} isn't one of {', '.join(VALUE_KINDS)}; ignored")
            continue
        if not isinstance(items, list):
            problems.append(f"{key} must be a list of strings; ignored")
            continue
        for n, item in enumerate(items):
            if not isinstance(item, str) or not item.strip():
                problems.append(f"{key}[{n}] isn't a non-empty string; ignored")
                continue
            kind = VALUE_KINDS[key]
            values.append((_classify(item) if kind == "other" else kind, item.strip(), f"{key}[{n}]"))
    return values, problems


def self_check(values: list[tuple[str, str, str]], salt: str, table: dict[str, str]) -> tuple[dict[str, int], list[str]]:
    """Scan each value's own text (and, for a phone, its masked rendering) against the table: hits per
    kind, and where (names[0]) a value isn't caught. Only counts and locations leave this function."""
    hits: dict[str, int] = {}
    missed = []
    for kind, value, where in values:
        texts = [(value, kind)]
        nat = _national(re.sub(r"\D", "", value)) if kind == "phone" else ""
        if len(nat) >= 7:
            texts.append((f"{nat[0]}*****{nat[-3:]}", "masked"))
        for text, want in texts:
            found = set().union(*(needle_kinds(line, salt, table) for line in text.splitlines() or [""]))
            if want in found:
                hits[want] = hits.get(want, 0) + 1
            elif where not in missed:
                missed.append(where)
    return hits, missed


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # created 0600
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def needles_build() -> int:
    source = values_path()
    values, problems = read_values(source)
    if values is None:
        for p in problems:
            print(f"needles build: {p}", file=sys.stderr)
        print(f"needles build: write it by hand, outside every repo (or point {VALUES_ENV} at it), mode 600:\n"
              f"  {VALUES_TEMPLATE}", file=sys.stderr)
        return 1
    if _checkout_root(cwd=str(source.parent)) is not None:
        print(f"needles build: {source} is inside a git checkout; keep it outside every repo", file=sys.stderr)
    if source.stat().st_mode & 0o077:
        print(f"needles build: other users can read {source}; run `chmod 600` on it", file=sys.stderr)
    tokens = needle_tokens([(kind, value) for kind, value, _ in values])
    if not tokens:
        print(f"needles build: no usable values in {source}; nothing written", file=sys.stderr)
        return 1
    salt = secrets.token_hex(16)
    kinds: dict[str, list[str]] = {}
    for token, kind in tokens.items():
        kinds.setdefault(kind, []).append(_hash(salt, token))
    path = needles_path()
    _write_private(path, json.dumps({"version": 1, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                     "salt": salt, "kinds": {k: sorted(v) for k, v in sorted(kinds.items())}}))
    table = {h: kind for kind, hashes in kinds.items() for h in hashes}
    hits, missed = self_check(values, salt, table)
    value_counts: dict[str, int] = {}
    for kind, value in {(kind, value) for kind, value, _ in values}:
        value_counts[kind] = value_counts.get(kind, 0) + 1
    print(f"needles build: wrote {path} (mode 600) from {len(values)} value(s) in {source}")
    for kind in KIND_LABELS:
        n_hashes = len(kinds.get(kind, []))
        if not n_hashes:
            continue
        source_note = f"{value_counts.get(kind, 0)} value(s)" if kind != "masked" else "derived from the phone numbers"
        print(f"  {KIND_LABELS[kind]:<30} {n_hashes:>4} hashes  {source_note:<32} self-check: "
              f"{'ok, ' + str(hits[kind]) + ' hit(s)' if hits.get(kind) else 'NO HIT'}")
    for p in problems:
        print(f"needles build: {p}", file=sys.stderr)
    for where in missed:
        print(f"needles build: {where} isn't caught by its own text (too short, or only words that count next "
              "to another name word: a public place name or part of the maintainer's handle)", file=sys.stderr)
    return 1 if problems or missed else 0


def needles_status() -> int:
    path = needles_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        print(f"no needles file at {path}; list the values in {values_path()} and run "
              "`python3 .githooks/pii_check.py needles build`")
        return 1
    mode = oct(path.stat().st_mode & 0o777)
    print(f"{path} (mode {mode[2:]}), built {data.get('built_at', '?')}")
    for kind, hashes in data.get("kinds", {}).items():
        print(f"  {KIND_LABELS.get(kind, kind):<30} {len(hashes):>4} hashes")
    return 0


# --- model layer (Claude Haiku) ------------------------------------------------------------------

MODEL = "claude-haiku-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
CHUNK_CHARS = 200_000  # about 50K tokens per request
MAX_CHUNKS = 5
MODEL_BUDGET_SECONDS = 75  # all chunks together; the agent hook's own timeout is 90 s
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "personal_data_found": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"location": {"type": "string"}, "kind": {"type": "string"}},
                "required": ["location", "kind"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["personal_data_found", "findings"],
    "additionalProperties": False,
}
SYSTEM_PROMPT = f"""You check text that someone is about to commit to the GitHub repository \
{MAINTAINER}/{REPO}, or post there as a pull request, issue, comment or release notes. {REPO} is a \
command-line client that talks directly to Renfe's own web backend (api.rsv.renfe.com, venta.renfe.com) to \
search Spanish trains and book tickets as a guest, without a Renfe account. Most commits are written by AI \
coding agents that have seen the owner's real bookings. Treat anything committed as public for good, so a \
real person's personal data must never appear.

Decide whether the text contains personal data of a real person, above all the owner and the passengers \
they book for:
- a name: a passenger's or buyer's full name, a surname, or a first name that refers to a real traveller
- a DNI, NIF, NIE or passport number, or a date of birth
- a phone number: Spanish (nine digits starting with 6, 7, 8 or 9, with or without +34 or 0034, in any \
grouping) or foreign (for example a US +1 number), or a masked number copied from real data
- a personal email address, a postal address, a street with its number, or a postal code with its town
- a booking locator ("localizador", usually six letters and digits), a booking, order or ticket number
- payment details: a card number, its expiry or CVV, a PayPal account, an IBAN or other bank account digits
- a real trip tied to a person: the seat, coach, dates or price paid next to a passenger or a booking
- pasted CLI or API output, HAR captures, cookies, session tokens or ticket files carrying any of the above

These are fine; don't flag them:
- obviously invented test data: repeated or sequential digits (600000000, 600000001, 612345678, 699999999, \
00000000T, 12345678Z), placeholders (<phone>, <DNI>, <locator>, XXXXXX, ABC123, Ana Ejemplo, Calle Falsa \
123, user@example.com), the payment gateways' test cards (4111 1111 1111 1111, 4242 4242 4242 4242)
- Renfe's public constants: API hosts and paths, header names, public API keys of the web app, station \
names and codes (60000, 71801, MADRI, BARCE), train service numbers (03063), train type codes (AV1), \
product and fare family codes, public timetables and fares, error codes
- the maintainer's public GitHub handle {MAINTAINER} and its noreply address, and AI agents (Claude, Codex) \
named as authors
- code, tests, configuration, schemas, field names, and descriptions of what a field contains
- text that only talks about personal data (that a value was removed or masked, or must never be committed) \
without including any

Each block starts with "### <source>" and each line with its line number and "|". For each finding, give \
the location as "<source>:<line>" and the kind as a short category label only (for example "passenger \
name", "DNI", "booking locator", "card number"). Never repeat any of the personal data, not even part of it."""


def _checkout_root(cwd: str | None = None) -> Path | None:
    try:
        return Path(git("rev-parse", "--show-toplevel", cwd=cwd).strip())
    except (subprocess.CalledProcessError, OSError):
        return None


def _main_checkout(cwd: str | None = None) -> Path | None:
    """The clone that owns .git (worktrees share it): where a gitignored .env would live."""
    try:
        return Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd).strip()).parent
    except (subprocess.CalledProcessError, OSError):
        return None


def _api_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    main = _main_checkout()
    env = main / ".env" if main else None
    if env and env.is_file():
        for line in env.read_text(errors="replace").splitlines():
            m = re.match(r"\s*(?:export\s+)?ANTHROPIC_API_KEY\s*=\s*(.*?)\s*$", line)
            if m:
                value = m.group(1).strip("'\"")
                return value if value and not value.startswith("$") else None
    return None


def _claude_cli() -> str | None:
    """The `claude` CLI; GUI git clients run hooks with a short PATH, so try its default install too."""
    found = shutil.which("claude")
    fallback = Path.home() / ".local" / "bin" / "claude"
    return found or (str(fallback) if fallback.is_file() and os.access(fallback, os.X_OK) else None)


def _chunks(blocks: Blocks) -> list[str]:
    """Render blocks as numbered text and pack them into requests of at most CHUNK_CHARS."""
    parts = []
    for label, lines in blocks:
        piece = f"### {label}"
        for n, text in lines:
            row = f"\n{n}| {text}"
            if len(piece) + len(row) > CHUNK_CHARS:
                parts.append(piece)
                piece = f"### {label} (continued)"
            piece += row
        parts.append(piece)
    chunks, current = [], ""
    for part in parts:
        if current and len(current) + len(part) + 2 > CHUNK_CHARS:
            chunks.append(current)
            current = ""
        current += ("\n\n" if current else "") + part
    return chunks + ([current] if current else [])


def _ask_api(key: str, chunk: str, timeout: float) -> tuple[dict | None, str | None]:
    """(verdict, None) from the Messages API, or (None, why not)."""
    body = {"model": MODEL, "max_tokens": 8192, "system": SYSTEM_PROMPT,
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
            "messages": [{"role": "user", "content": chunk}]}
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response = json.load(resp)
    except urllib.error.HTTPError as e:
        return None, f"model check failed (HTTP {e.code})"
    except (urllib.error.URLError, OSError, ValueError):
        return None, "model check failed (no connection)"
    text = next((b.get("text", "") for b in response.get("content", []) if b.get("type") == "text"), "")
    try:
        verdict = json.loads(text) if response.get("stop_reason") == "end_turn" else None
    except ValueError:
        verdict = None
    return (verdict, None) if isinstance(verdict, dict) else (None, f"model check gave no verdict ({response.get('stop_reason')})")


def _ask_cli(exe: str, chunk: str, timeout: float) -> tuple[dict | None, str | None]:
    """(verdict, None) from `claude -p` on the user's Claude Code login, or (None, why not).

    No tools, no settings (so no hooks: it can't recurse into this check), no MCP, no CLAUDE.md
    (--safe-mode, and it runs in an empty directory), and nothing saved.
    """
    cmd = [exe, "-p", "--model", MODEL, "--effort", "low", "--output-format", "json", "--safe-mode",
           "--setting-sources", "", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--system-prompt", SYSTEM_PROMPT, "--json-schema", json.dumps(VERDICT_SCHEMA)]
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    try:
        with tempfile.TemporaryDirectory(prefix="pii-check-") as cwd:
            r = subprocess.run(cmd, input=chunk, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        return None, f"model check timed out after {timeout:.0f}s"
    except OSError:
        return None, "model check failed (the claude CLI didn't start)"
    try:
        result = json.loads(r.stdout)
    except ValueError:
        result = None
    if not isinstance(result, dict) or result.get("is_error") or result.get("subtype") != "success":
        detail = ("error" if result.get("is_error") else result.get("subtype")) if isinstance(result, dict) \
            else f"exit {r.returncode}"
        return None, f"model check failed (claude CLI: {detail}; signed in?)"
    verdict = result.get("structured_output")
    return (verdict, None) if isinstance(verdict, dict) else (None, "model check gave no verdict")


def _model_kind(kind) -> str:
    """The model's category label, if it is one: letters, spaces and /+-, at most 40 characters."""
    kind = kind.strip() if isinstance(kind, str) else ""
    ok = re.fullmatch(r"(?:[^\W\d_]|[ /+-]){1,40}", kind) and re.search(r"[^\W\d_]", kind)
    return kind if ok else "personal data"


def _model_location(location, known: dict[str, set[int]]) -> str:
    """The model's location, only as a source and line we sent it (or a bare line number); anything else
    becomes the source (or "text"), so a value the model repeats never reaches the output."""
    text = location.strip() if isinstance(location, str) else str(location) if isinstance(location, int) else ""
    only = next(iter(known)) if len(known) == 1 else None
    label, _, number = text.rpartition(":")
    label = label.removesuffix(" (continued)")
    if label in known and re.fullmatch(r"[0-9]{1,9}", number):
        return f"{label}:{int(number)}" if int(number) in known[label] else label
    if re.fullmatch(r"[0-9]{1,9}", text):
        if only is not None:
            return f"{only}:{int(text)}" if int(text) in known[only] else only
        return f"line {int(text)}"
    if text.removesuffix(" (continued)") in known:
        return text.removesuffix(" (continued)")
    return only if only is not None else "text"


def model_check(blocks: Blocks) -> tuple[list[Finding], str | None]:
    """Claude Haiku's findings for these lines, plus a note when the check couldn't (fully) run."""
    blocks = [(label, [(n, t) for n, t in lines if ALLOW_MARKER not in t and t.strip()]) for label, lines in blocks]
    blocks = [(label, lines) for label, lines in blocks if lines]
    if not blocks:
        return [], None
    known = {label: {n for n, _ in lines} for label, lines in blocks}
    key = _api_key()
    exe = None if key else _claude_cli()
    if not key and not exe:
        return [], "model check skipped: no ANTHROPIC_API_KEY and no `claude` CLI"
    chunks = _chunks(blocks)
    deadline = time.monotonic() + MODEL_BUDGET_SECONDS
    findings: list[Finding] = []
    for chunk in chunks[:MAX_CHUNKS]:
        remaining = deadline - time.monotonic()
        if remaining < 5:
            return findings, "model check stopped: out of time; the other checks still ran"
        verdict, note = _ask_api(key, chunk, remaining) if key else _ask_cli(exe, chunk, remaining)
        if verdict is None:
            return findings, f"{note}; the other checks still ran"
        if verdict.get("personal_data_found"):
            found = [Finding(_model_location(f.get("location"), known), f"{_model_kind(f.get('kind'))} (model)")
                     for f in verdict.get("findings", []) if isinstance(f, dict)]
            findings += found or [Finding("text", "personal data (model)")]
    note = (f"the model checked the first {MAX_CHUNKS} of {len(chunks)} parts; split very large commits"
            if len(chunks) > MAX_CHUNKS else None)
    return findings, note


# --- agent hook ----------------------------------------------------------------------------------

GH_TEXT_CMDS = re.compile(r"^(?:pr|issue|release)\s+(?:create|edit|comment|review)\b|^api\b")
VALUE_FLAGS = {"-m", "--message", "-F", "--file", "-c", "-C", "--author", "--date", "-t", "--template",
               "--reuse-message", "--reedit-message", "--fixup", "--squash", "--trailer", "--cleanup"}
FILE_FLAGS = {"--body-file", "--notes-file", "-F", "--input", "--file"}


def _segments(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return [command.split()]
    segs, cur = [], []
    for t in tokens:
        if t and set(t) <= set(";&|"):
            segs.append(cur)
            cur = []
        else:
            cur.append(t)
    return [s for s in segs + [cur] if s]


def _git_commit_args(seg: list[str]) -> list[str] | None:
    if "git" not in seg:
        return None
    rest = seg[seg.index("git") + 1:]
    i = 0
    while i < len(rest) and rest[i].startswith("-"):  # git -C dir / -c k=v before the subcommand
        i += 2 if rest[i] in ("-C", "-c") else 1
    return rest[i + 1:] if i < len(rest) and rest[i] == "commit" else None


def _gh_args(seg: list[str]) -> list[str] | None:
    """What follows `gh` and its global flags (`gh -R owner/repo pr create ...` -> ["pr", "create", ...])."""
    if "gh" not in seg:
        return None
    rest = seg[seg.index("gh") + 1:]
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in ("-R", "--repo") else rest[1:]
    return rest


def _skips_hooks(args: list[str]) -> bool:
    skip_next = False
    for t in args:
        if skip_next:
            skip_next = False
            continue
        if t in VALUE_FLAGS:
            skip_next = True
        elif t in ("-n", "--no-verify") or re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", t) and not t.startswith("--"):
            return True
    return False


def check_command(command: str, cwd: str) -> tuple[list[Finding], str | None, Blocks]:
    """(pattern and known-value findings, refusal, text for the model) for one shell command an agent is
    about to run.

    `git commit` gets only the --no-verify check here: the git hooks check its content.
    `gh pr|issue|release create|edit|comment|review` and `gh api` get their text checked, body files included.
    """
    findings, refusal, blocks, posts_text, n_files = [], None, [], False, 0
    for seg in _segments(command):
        commit_args = _git_commit_args(seg)
        if commit_args is not None and _skips_hooks(commit_args):
            refusal = "`git commit --no-verify` skips the personal-data check. Commit without it."
        tail = _gh_args(seg)
        if tail is not None and GH_TEXT_CMDS.match(" ".join(tail)):
            posts_text = True
            for flag, val in zip(tail, tail[1:]):
                if flag in FILE_FLAGS or val.startswith("@") or re.match(r"^[\w.-]+=@", val):
                    target = (val[1:] if val.startswith("@") else val).split("=@", 1)[-1]
                    if target == "-":
                        refusal = "Text piped in on stdin can't be checked; write it to a file and pass the file."
                        continue
                    p = Path(cwd, target)
                    if p.is_file():
                        n_files += 1
                        named = path_findings(target, f"body file #{n_files}")
                        label = f"body file #{n_files}" if named else target
                        lines = numbered(p.read_text(errors="replace"))
                        findings += named + scan_lines(lines, label)
                        blocks.append((label, lines))
    if posts_text:
        lines = numbered(command)
        findings += scan_lines(lines, "command")
        blocks.insert(0, ("command", lines))
    return findings, refusal, blocks


def agent_hook() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    command = tool_input.get("command") or data.get("command") or ""
    cwd = data.get("cwd") or os.getcwd()
    cursor_style = "tool_input" not in data and "command" in data
    try:
        enable(quiet=True, cwd=cwd)
    except (subprocess.CalledProcessError, OSError):
        pass
    try:
        findings, refusal, blocks = check_command(command, cwd) if command else ([], None, [])
    except NeedlesUnreadable as e:
        findings, refusal, blocks = [], f"Blocked: {e}.", []
    if not findings and not refusal and blocks:
        findings, note = model_check(blocks)
        if note:
            print(f"pii check: {note}", file=sys.stderr)
    if not findings and not refusal:
        if cursor_style:
            print(json.dumps({"permission": "allow"}))
        return 0
    note = alias_note(findings)
    reason = refusal or ("Possible personal data of the owner or a passenger: "
                         + "; ".join(f"{f.where} ({f.kind})" for f in findings) + ". "
                         + (f"({note}.) " if note else "") + HINT)
    if cursor_style:
        print(json.dumps({"permission": "deny", "user_message": reason, "agent_message": reason}))
    print(reason, file=sys.stderr)
    return 2


# --- git hook install ----------------------------------------------------------------------------

SHIM_MARKER = "installed by .githooks/pii_check.py"
HOOK_NAMES = ("pre-commit", "pre-merge-commit", "commit-msg")  # a clean merge runs pre-merge-commit, not pre-commit
SHIM = """#!/bin/sh
# {marker}: runs the repo's tracked hook. A checkout without the
# check (another branch) passes; one where the hook is there but can't run fails closed.
top="$(git rev-parse --show-toplevel)" || exit 1
hook="$top/.githooks/{name}"
[ -x "$hook" ] && exec "$hook" "$@"
if [ -e "$hook" ] || [ -e "$top/.githooks/pii_check.py" ]; then
  echo "pii check: $hook is missing or not executable; restore it (chmod +x) to commit" >&2
  exit 1
fi
exit 0
"""


def enable(quiet: bool = False, cwd: str | None = None) -> int:
    top = Path(git("rev-parse", "--show-toplevel", cwd=cwd).strip())
    if not (top / ".githooks" / "pii_check.py").is_file():
        return 0  # another repo: leave its hooks alone
    hooks = Path(git("rev-parse", "--path-format=absolute", "--git-path", "hooks", cwd=cwd).strip())
    hooks.mkdir(parents=True, exist_ok=True)
    for name in HOOK_NAMES:
        target, content = hooks / name, SHIM.format(marker=SHIM_MARKER, name=name)
        if target.exists():
            current = target.read_text(errors="replace")
            if current == content:
                continue
            if SHIM_MARKER not in current and "File generated by pre-commit" not in current:
                if not quiet:
                    print(f"WARN: {target} is someone else's hook; left it alone. Call .githooks/{name} from it.",
                          file=sys.stderr)
                continue
        target.write_text(content)
        target.chmod(0o755)
        if not quiet:
            print(f"installed {target}")
    return 0


# --- CLI -----------------------------------------------------------------------------------------

ALIASES = {"staged file #": "staged file #N is the Nth path of `git diff --cached --name-only --no-renames`",
           "file #": "file #N is the Nth path given", "body file #": "body file #N is the Nth body file in the command"}


def alias_note(findings: list[Finding]) -> str:
    """How to find the files reported by alias (their names hold personal data, so they aren't printed)."""
    used = {prefix for f in findings for prefix in ALIASES if f.where.startswith(prefix)}
    return "; ".join(ALIASES[prefix] for prefix in ALIASES if prefix in used)


def report(findings: list[Finding], note: str | None = None) -> int:
    if note:
        print(f"pii check: {note}", file=sys.stderr)
    if not findings:
        return 0
    print("Blocked: possible personal data of the owner or a passenger (values not shown):", file=sys.stderr)
    for f in findings:
        print(f"  {f.where}  {f.kind}", file=sys.stderr)
    note = alias_note(findings)
    if note:
        print(f"  ({note})", file=sys.stderr)
    print(HINT, file=sys.stderr)
    return 2


def check(findings: list[Finding], blocks: Blocks, use_model: bool = True) -> int:
    """Patterns and known values first; the model only runs when they found nothing (a hit blocks either way)."""
    if findings or not use_model:
        return report(findings)
    return report(*model_check(blocks))


def self_test() -> int:
    # Fabricated samples only, built at runtime so this file never holds a realistic value.
    mobile, nif_digits = "6" + "71" + "829304", "31" + "415926"
    nif = nif_digits + NIF_LETTERS[int(nif_digits) % 23]
    nie = "Y" + "2718281" + NIF_LETTERS[int("1" + "2718281") % 23]
    bban = "2100" + "0418" + "4502" + "0005" + "1333"
    iban = next(f"ES{c:02d}{bban}" for c in range(100) if _iban_personal(f"ES{c:02d}{bban}"))
    card_base = "4" + "9271" + "8350" + "6612" + "83"
    card = next(card_base + str(c) for c in range(10) if _card_personal(card_base + str(c)))
    bad_luhn = card[:-1] + str((int(card[-1]) + 1) % 10)
    locator, booking = "Q7" + "K2M9", "48" + "29173650"
    gmail = "gm" + "ail.com"
    bad = [f"reach me at jo.smith@{gmail}", f"tel {mobile}", f"+34 {mobile[:3]} {mobile[3:6]} {mobile[6:]}",
           f"+1 (415) 867-{'53' + '09'}", f"NIF {nif}", f"NIE {nie}", f"IBAN {iban}", f"tarjeta {card}",
           f"card {card[:4]} {card[4:8]} {card[8:12]} {card[12:]} 12/27", f"localizador: {locator}",
           f'{{"localizador": "{locator}"}}', f"Your booking reference is {locator}", f"PNR {locator}",
           f"codigoLocalizador={locator}", f"número de reserva {booking}", f"localizador: {locator.lower()}",
           f"bookingNumber={locator}", f"Nº billete {booking}", "PNR " + "QW" + "HZTK", f"{card} 2029",
           "vive en Calle de " + "Alcalá 42", "estación cerca de Plaza " + "Mayor 1", "28001 " + "Madrid"]
    good = ["user@example.com", f"support@{gmail}", "600000001", "612345678", "699999999", "00000000T",
            "12345678Z", "ES9121000418450200051332", "Calle Falsa 123", f"tel {mobile}  # {ALLOW_MARKER}",
            "noreply@anthropic.com", "git@github.com", "sha256:a612345679b", "timeout 1700000000", "900123456",
            "4111 1111 1111 1111", "4242424242424242", "5555-5555-5555-4444", f"id {bad_luhn}", "ts 1731682345123",
            "localizador XXXXXX", "localizador: ABC123", "booking ref <locator>", "LOCALIZADOR | ORIGEN | DESTINO",
            f"service {locator}", "MADRI 60000 -> BARCE 71801, train 03063 AV1", "commit 3f04d09 locator",
            "17000 Madrid-Chamartín", "51003 " + "Sevilla-Santa Justa", "estación 11014 Villaejemplo",
            "localizador 3f04d09", "PNR: XXXXXX", "billete 03063", "tren AV MADRID 3100", "AVE 03063 AV MADRID 3100"]
    fails = [s for s in bad if not scan_line(s)] + [s for s in good if scan_line(s)]
    cmd_cases = {
        "git commit --no-verify -m 'x'": True, "git commit -nm 'x'": True, "git commit -am 'fix'": False,
        "git -C repo commit -m 'msg -n here'": False, f"gh pr create --body 'cc jo@{gmail}'": True,
        "gh pr create --title t --body 'all good'": False, f"echo jo@{gmail}": False,
        f"gh -R o/r issue comment 1 --body 'tel {mobile}'": True, "gh pr create --body-file -": True,
    }
    for cmd, should_block in cmd_cases.items():
        found, refusal, _ = check_command(cmd, ".")
        if bool([f for f in found if "known personal value" not in f.kind] or refusal) != should_block:
            fails.append(cmd)
    if not all(blocked_path(p) for p in ("a.har", ".local-secrets/x", "tokens.json", "x/tokens.bak.json",
                                         "bill.PDF", "ticket.pkpass", "d.csv", ".env", "pii-values.json")):
        fails.append("blocked_path positive")
    if any(blocked_path(p) for p in ("src/renfe/tokens.py", "docs/x.md", ".env.example", "uv.lock")):
        fails.append("blocked_path negative")
    salt = "self-test"
    fake = [("phone", mobile), ("name", "Ana Ejemplo Prueba"), ("document", nif), ("locator", locator),
            ("locator", "QRSTUV"), ("locator", booking)]
    table = {_hash(salt, t): k for t, k in needle_tokens(fake).items()}
    for i, (line, kind) in enumerate(((f"{mobile[0]}*****{mobile[-3:]}", "masked"), ("EJEMPLO PRUEBA, ANA", "name"),
                                      (nif, "document"), (f"{mobile[:3]} {mobile[3:5]} {mobile[5:7]} {mobile[7:]}", "phone"),
                                      (f"ref {locator.lower()}", "locator"), ("QRSTUV", "locator"), (booking, "locator"))):
        if kind not in needle_kinds(line, salt, table):
            fails.append(f"needles #{i}: {kind}")
    if needle_kinds("600000001 6*****001 Calle Falsa Madrid-Atocha", salt, table):
        fails.append("needles negative")
    if len(_chunks([("f", [(i, "x" * 1000) for i in range(1, 450)])])) != 3:
        fails.append("chunking")
    for f in fails:
        print(f"self-test FAILED: {f!r}", file=sys.stderr)
    return 1 if fails else 0


def main(argv: list[str]) -> int:
    try:
        return run(argv)
    except NeedlesUnreadable as e:
        print(f"pii check: blocked: {e}", file=sys.stderr)
        return 2


def run(argv: list[str]) -> int:
    mode, args = (argv[1] if len(argv) > 1 else ""), argv[2:]
    if mode == "staged":
        return check(*scan_staged())
    if mode == "msg" and len(args) == 1:
        lines = message_lines(args[0])
        return check(scan_lines(lines, "commit message"), [("commit message", lines)])
    if mode == "text":
        lines = numbered(sys.stdin.read())
        return check(scan_lines(lines, "text"), [("text", lines)])
    if mode == "files":
        findings, blocks = [], []
        for n, path in enumerate([a for a in args if a != "--patterns-only"], 1):
            if not Path(path).is_file():
                continue
            named = path_findings(path, f"file #{n}")
            label = f"file #{n}" if named else path
            lines = numbered(Path(path).read_text(errors="replace"))
            findings += named
            if blocked_path(path):
                findings.append(Finding(label, "data file that never belongs in git"))
            findings += scan_lines(lines, label)
            blocks.append((label, lines))
        return check(findings, blocks, "--patterns-only" not in args)
    if mode == "agent-hook":
        return agent_hook()
    if mode == "enable":
        return enable()
    if mode == "needles" and args == ["build"]:
        return needles_build()
    if mode == "needles" and args in ([], ["status"]):
        return needles_status()
    if mode == "--self-test":
        return self_test()
    print(__doc__, file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
