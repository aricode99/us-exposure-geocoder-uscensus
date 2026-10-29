"""In-memory file handling for SOV uploads and exports.

Nothing here touches the disk: uploads arrive as bytes and exports are returned
as bytes. All values are read as text so ZIP codes, IDs and leading zeroes are
never converted to numbers.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from utils import constants as C

logger = logging.getLogger(__name__)


class FileReadError(Exception):
    """Raised with a user-friendly message when a file cannot be used."""


# Normalised column-name aliases (lower-case, letters/digits only).
ALIASES: Dict[str, List[str]] = {
    "location_id": ["locationid", "locid", "location", "locno", "locationnumber", "locnum", "id",
                    "uniqueid", "siteid", "premiseid", "propertyid", "bldgid", "buildingid", "locationno"],
    "street": ["streetaddress", "address", "address1", "street", "addressline1", "addr1", "addr",
               "street1", "siteaddress", "locationaddress", "physicaladdress", "streetname"],
    "city": ["city", "town", "cityname", "locationcity", "sitecity"],
    "state": ["state", "statecode", "st", "stateprovince", "stateabbr", "statename", "locationstate"],
    "zip": ["zip", "zipcode", "zip5", "postalcode", "postcode", "zippostalcode", "zipcd", "locationzip"],
    "county": ["county", "countyname", "countyparish", "parish"],
}

MAPPING_LABELS = {
    "location_id": "Unique Location ID",
    "street": "Street Address",
    "city": "City",
    "state": "State",
    "zip": "ZIP Code",
    "county": "County (optional, used only for fallback)",
}


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def suggest_mapping(columns: List[str]) -> Dict[str, Optional[str]]:
    """Suggest a column for each field. Exact alias matches win; each column is used once."""
    suggestions: Dict[str, Optional[str]] = {k: None for k in ALIASES}
    used: set = set()
    normalised = {c: _key(c) for c in columns}
    for field_name, aliases in ALIASES.items():
        for alias in aliases:                      # alias order = priority
            hit = next((c for c, k in normalised.items() if k == alias and c not in used), None)
            if hit:
                suggestions[field_name] = hit
                used.add(hit)
                break
    return suggestions


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def _check_size(data: bytes) -> None:
    if not data:
        raise FileReadError("The uploaded file is empty.")
    if len(data) > C.MAX_UPLOAD_MB * 1024 * 1024:
        raise FileReadError(f"The file is larger than the {C.MAX_UPLOAD_MB} MB limit.")


def _check_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.shape[1] == 0 or len(df) == 0:
        raise FileReadError("The file contains no data rows.")
    if len(df) > C.MAX_ROWS:
        raise FileReadError(
            f"The file has {len(df):,} rows; the limit for this tool is {C.MAX_ROWS:,}. "
            "Please split the file.")
    df.columns = [str(c).strip() for c in df.columns]
    dup = [c for c in set(df.columns) if list(df.columns).count(c) > 1]
    if dup:
        raise FileReadError(f"Duplicate column names found: {', '.join(sorted(dup))}. "
                            "Please make column names unique.")
    return df.reset_index(drop=True)


def _parse_csv(data: bytes, encoding: str) -> pd.DataFrame:
    """Comma first; if that yields a single column, sniff the delimiter (e.g. ';' or tab)."""
    df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding=encoding)
    if df.shape[1] == 1:
        try:
            sniffed = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False,
                                  encoding=encoding, sep=None, engine="python")
            if sniffed.shape[1] > 1:
                return sniffed
        except (pd.errors.ParserError, ValueError):
            pass
    return df


def read_csv_bytes(data: bytes) -> pd.DataFrame:
    _check_size(data)
    last: Optional[Exception] = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return _check_rows(_parse_csv(data, enc))
        except UnicodeDecodeError as exc:
            last = exc
        except (pd.errors.ParserError, pd.errors.EmptyDataError, ValueError) as exc:
            raise FileReadError("The file could not be read as CSV. Please check the format.") from exc
    raise FileReadError("The file encoding is not supported.") from last


def excel_sheet_names(data: bytes) -> List[str]:
    _check_size(data)
    try:
        with pd.ExcelFile(io.BytesIO(data), engine="openpyxl") as xls:
            return list(xls.sheet_names)
    except Exception as exc:                      # openpyxl/zipfile raise many different types
        raise FileReadError("This does not look like a valid .xlsx workbook.") from exc


def read_excel_bytes(data: bytes, sheet_name: Optional[str] = None) -> pd.DataFrame:
    _check_size(data)
    try:
        df = pd.read_excel(io.BytesIO(data), sheet_name=sheet_name or 0, dtype=str,
                           keep_default_na=False, engine="openpyxl")
    except Exception as exc:
        raise FileReadError("The Excel file could not be read. It may be corrupt or password protected.") from exc
    return _check_rows(df)


def read_upload(name: str, data: bytes, sheet_name: Optional[str] = None) -> pd.DataFrame:
    lower = (name or "").lower()
    if lower.endswith(".csv"):
        return read_csv_bytes(data)
    if lower.endswith(".xlsx"):
        return read_excel_bytes(data, sheet_name)
    raise FileReadError("Unsupported file type. Please upload a .csv or .xlsx file.")


# ---------------------------------------------------------------------------
# Mapping validation
# ---------------------------------------------------------------------------
@dataclass
class MappingReport:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    duplicate_ids: int = 0
    blank_ids: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_mapping(df: pd.DataFrame, mapping: Dict[str, Optional[str]]) -> MappingReport:
    rep = MappingReport()
    id_col, street_col = mapping.get("location_id"), mapping.get("street")
    if not id_col:
        rep.errors.append("Select the Unique Location ID column.")
    if not street_col:
        rep.errors.append("Select the Street Address column.")
    chosen = [v for v in mapping.values() if v]
    if len(chosen) != len(set(chosen)):
        rep.errors.append("The same column is selected for more than one field.")
    if rep.errors:
        return rep

    ids = df[id_col].astype(str).str.strip()
    rep.blank_ids = int((ids == "").sum())
    rep.duplicate_ids = int(ids[ids != ""].duplicated(keep=False).sum())
    if rep.blank_ids:
        rep.warnings.append(f"{rep.blank_ids:,} row(s) have a blank Location ID.")
    if rep.duplicate_ids:
        rep.warnings.append(
            f"{rep.duplicate_ids:,} row(s) share a Location ID with another row. All rows are still "
            "geocoded (an internal row key is used), but downstream joins on Location ID will be ambiguous.")

    blank_street = int((df[street_col].astype(str).str.strip() == "").sum())
    if blank_street:
        rep.warnings.append(f"{blank_street:,} row(s) have no street address; these can only receive an "
                            "area-centroid fallback (ZIP, city or county).")

    for key, label in (("city", "City"), ("state", "State"), ("zip", "ZIP")):
        if not mapping.get(key):
            rep.warnings.append(f"No {label} column mapped. Census needs street plus ZIP, or street plus "
                                "city and state; missing fields reduce match rates.")
    if not mapping.get("zip") and not (mapping.get("city") and mapping.get("state")):
        rep.warnings.append("Without a ZIP column, both City and State are needed for Census matching.")
    return rep


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def to_csv_bytes(df: pd.DataFrame) -> bytes:
    """UTF-8 with BOM so Excel opens non-ASCII text correctly."""
    return df.to_csv(index=False).encode("utf-8-sig")


def to_xlsx_bytes(df: pd.DataFrame, sheet_name: str = "Geocoded") -> bytes:
    if len(df) >= 1_048_576:
        raise FileReadError("Too many rows for a single Excel sheet; use CSV.")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
        ws = writer.sheets[sheet_name[:31]]
        ws.freeze_panes = "A2"
    return buf.getvalue()
