import hashlib
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pandas as pd

MAX_ROWS = 5000
REQUIRED_COLUMNS = {"date", "description", "amount"}

NOISE_WORDS = {
    "POS", "PURCHASE", "PAYMENT", "ORDER", "ONLINE", "CARD", "COM", "WWW", "GE", "LLC", "LTD",
    "TBILISI", "BATUMI", "KUTAISI", "GEORGIA",
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
    "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
}

IBAN_PATTERN = re.compile(r"\b([A-Z]{2}\d{2})[A-Z0-9]{7,26}(\d{4})\b")
CARD_PATTERN = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?(\d{4})\b")
WORD_PATTERN = re.compile(r"[^\W\d_]+")


class CsvError(Exception):
    pass


def mask_pii(text: str) -> str:
    text = IBAN_PATTERN.sub(r"\1 **** **** \2", text)
    return CARD_PATTERN.sub(r"**** **** **** \1", text)


def clean_merchant(description: str) -> str:
    words = [word.upper() if word.isascii() else word for word in WORD_PATTERN.findall(description)]
    if "TRANSFER" in words:
        return "TRANSFER"
    words = [word for word in words if word not in NOISE_WORDS]
    return " ".join(words)[:120] or "UNKNOWN"


def make_fingerprint(user_id: int, day, amount: Decimal, description: str, occurrence: int) -> str:
    text = f"{user_id}|{day}|{amount}|{description}|{occurrence}"
    return hashlib.sha256(text.encode()).hexdigest()


def read_statement(content: bytes, user_id: int, currency: str) -> tuple[list[dict], list[dict], int]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvError("The file must be UTF-8 encoded")
    if not text.strip():
        raise CsvError("The file is empty")

    try:
        table = pd.read_csv(io.StringIO(text), sep=None, engine="python", dtype=str, keep_default_na=False)
    except Exception:
        raise CsvError("Could not read the CSV file")

    table.columns = [str(column).strip().lower() for column in table.columns]
    missing = REQUIRED_COLUMNS - set(table.columns)
    if missing:
        raise CsvError(f"Missing columns: {', '.join(sorted(missing))}")
    if len(table) == 0:
        raise CsvError("The file has no transactions")
    if len(table) > MAX_ROWS:
        raise CsvError(f"The file has more than {MAX_ROWS} rows")

    rows = []
    errors = []
    seen = {}
    for index, record in table.iterrows():
        line = index + 2
        try:
            day = datetime.strptime(record["date"].strip(), "%Y-%m-%d").date()
            amount = Decimal(record["amount"].strip().replace(",", "."))
        except (ValueError, InvalidOperation):
            errors.append({"row": line, "reason": "invalid date or amount"})
            continue

        description = mask_pii(record["description"].strip())
        row_currency = record.get("currency", "").strip().upper()
        if amount == 0 or amount != amount.quantize(Decimal("0.01")):
            errors.append({"row": line, "reason": "invalid amount"})
            continue
        if not description:
            errors.append({"row": line, "reason": "empty description"})
            continue
        if row_currency and row_currency != currency:
            errors.append({"row": line, "reason": f"currency {row_currency} is not {currency}"})
            continue

        key = (day, amount, description)
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1

        rows.append({
            "date": day.isoformat(),
            "amount": str(abs(amount)),
            "type": "expense" if amount < 0 else "income",
            "description": description[:255],
            "merchant": clean_merchant(description),
            "fingerprint": make_fingerprint(user_id, day, amount, description, occurrence),
        })
    return rows, errors, len(table)
