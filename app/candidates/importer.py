"""Google Form export (.xlsx / .csv) -> candidate rows. Pure parsing, no DB."""

import csv
import io
import re
from datetime import date, datetime

from openpyxl import load_workbook

from app.core.errors import AppError

MAX_ROWS = 5000
# canonical field -> normalised header must contain one of these (headers are free text)
REQUIRED = {
    "email": ("email",),
    "full_name": ("fullname", "name"),
    "student_id": ("studentid", "rollno"),
    "dob": ("dateofbirth", "dob"),
}
OPTIONAL = {"phone": ("phone", "mobile")}
# shortcut: DD/MM/YYYY assumed for slashed dates (India); a US-format export swaps day/month
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h).lower())


def _read(filename: str, data: bytes) -> list[list]:
    name = filename.lower()
    if name.endswith(".xlsx"):
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        except Exception as e:  # openpyxl raises many types for a bad file
            raise AppError(422, "BAD_SHEET", "Couldn't read this .xlsx file") from e
    if name.endswith(".csv"):
        return list(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))))
    raise AppError(422, "BAD_SHEET", "Upload an .xlsx or .csv file")


def _dob(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(str(v).strip().split(" ")[0], fmt).date()
        except ValueError:
            pass
    return None


def _column_map(headers: list[str]) -> dict[str, int]:
    normed = [_norm(h) for h in headers]
    found: dict[str, int] = {}
    for field, keys in {**REQUIRED, **OPTIONAL}.items():
        for i, h in enumerate(normed):
            if i not in found.values() and any(k in h for k in keys):
                found[field] = i
                break
    missing = [f for f in REQUIRED if f not in found]
    if missing:
        raise AppError(422, "MISSING_COLUMNS", f"Sheet has no column for: {', '.join(missing)}")
    return found


def _cell(row: list, col: dict[str, int], field: str) -> str:
    i = col.get(field)
    return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""


def parse_sheet(filename: str, data: bytes) -> tuple[list[dict], list[dict]]:
    """Return (candidates, errors). Row numbers match the spreadsheet (header = row 1)."""
    rows = _read(filename, data)
    if not rows:
        raise AppError(422, "BAD_SHEET", "The sheet is empty")
    if len(rows) - 1 > MAX_ROWS:
        raise AppError(422, "TOO_MANY_ROWS", f"At most {MAX_ROWS} rows per import")
    headers = [str(h or "").strip() for h in rows[0]]
    col = _column_map(headers)
    mapped = set(col.values())
    out, errors = [], []
    for n, row in enumerate(rows[1:], start=2):
        if not any(c not in (None, "") for c in row):
            continue  # blank line
        email, sid, name = (
            _cell(row, col, "email").lower(),
            _cell(row, col, "student_id"),
            _cell(row, col, "full_name"),
        )
        dob = _dob(row[col["dob"]]) if col["dob"] < len(row) and row[col["dob"]] else None
        problem = (
            "Student ID is empty"
            if not sid
            else "Full name is empty"
            if not name
            else "Email is not valid"
            if not EMAIL.match(email)
            else "Date of birth is missing or not a date"
            if dob is None
            else None
        )
        if problem:
            errors.append({"row": n, "message": problem})
            continue
        form = {
            headers[i]: (v.isoformat() if isinstance(v, date) else v)
            for i, v in enumerate(row)
            if i not in mapped and i < len(headers) and headers[i] and v not in (None, "")
        }
        out.append(
            {
                "student_id": sid,
                "full_name": name,
                "email": email,
                "dob": dob,
                "phone": _cell(row, col, "phone") or None,
                "form_data": form,
            }
        )
    return out, errors
