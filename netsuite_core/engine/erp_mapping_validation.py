"""
Pre-flight validation for ERP Integration Settings (Save Mapping Configuration).
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

import polars as pl

SELECT_TOKEN = "--- Select Column ---"

# (backend_var, strip_money_commas)
FLOAT_INVENTORY_FIELDS = (
    ("Quantity_Available", False),
    ("Quantity_Committed", False),
    ("Unit_Average_Cost", True),
    ("Unit_Base_Price", True),
    ("Target_Stock_Level", False),
)

FLOAT_ANNUAL_SOLD_FIELDS = (("Annual_Sold", False),)

FLOAT_PO_FIELDS = (
    ("Quantity_Ordered", False),
    ("Order_Total_Amount", True),
)


def _clean_float_token(s: str, strip_money: bool) -> str:
    s = s.strip()
    if strip_money:
        s = s.replace("$", "").replace(",", "").replace(" ", "")
    return s


def _try_parse_float_cell(value, strip_money: bool) -> bool:
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return not isinstance(value, float) or value == value  # not NaN
    if isinstance(value, str):
        clean = _clean_float_token(value, strip_money)
        if clean in ("", "-", "NA", "null", "N/A"):
            return True
        try:
            float(clean)
            return True
        except ValueError:
            return False
    return False


def validate_float_mapped_columns(
    df: pl.DataFrame,
    mapping: dict[str, str],
    fields: tuple[tuple[str, bool], ...],
    file_label: str,
) -> list[str]:
    """
    For each mapped float backend var, take up to 50 non-null raw samples and ensure they parse as float.
    Returns list of error strings (empty if OK).
    """
    errors: list[str] = []
    for backend_key, strip_money in fields:
        csv_col = mapping.get(backend_key)
        if not csv_col or csv_col == SELECT_TOKEN:
            errors.append(
                f"Validation Failed ({file_label}): {backend_key} is required and must be mapped to a column."
            )
            continue
        if csv_col not in df.columns:
            errors.append(
                f"Validation Failed ({file_label}): mapped column '{csv_col}' for {backend_key} not found in CSV."
            )
            continue
        sample = df[csv_col].drop_nulls().head(50)
        if sample.is_empty():
            continue
        for v in sample.to_list():
            if not _try_parse_float_cell(v, strip_money):
                errors.append(
                    f"Validation Failed ({file_label}): {backend_key} expects numbers, but was mapped to a column "
                    f"containing non-numeric values (e.g. '{v}')."
                )
                break
    return errors


def _try_parse_date_cell(value) -> bool:
    from datetime import date as date_type

    if value is None:
        return True
    if isinstance(value, datetime):
        return True
    if isinstance(value, date_type):
        return True
    if isinstance(value, str):
        s = value.strip()
        if not s or s in ("-", "NA", "null", "N/A"):
            return True
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                datetime.strptime(s[:10], fmt)
                return True
            except ValueError:
                continue
        return False
    return True


def validate_date_mapped_columns(
    df: pl.DataFrame,
    mapping: dict[str, str],
    backend_keys: tuple[str, ...],
    file_label: str,
) -> list[str]:
    errors: list[str] = []
    for backend_key in backend_keys:
        csv_col = mapping.get(backend_key)
        if not csv_col or csv_col == SELECT_TOKEN:
            continue
        if csv_col not in df.columns:
            errors.append(
                f"Validation Failed ({file_label}): mapped column '{csv_col}' for {backend_key} not found in CSV."
            )
            continue
        sample = df[csv_col].head(50)
        for v in sample.to_list():
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            if not _try_parse_date_cell(v):
                errors.append(
                    f"Validation Failed ({file_label}): {backend_key} must be parseable as a date when populated; "
                    f"found invalid value (e.g. '{v}')."
                )
                break
    return errors


def validate_required_mappings(
    mapping: dict[str, str],
    required_backend_keys: tuple[str, ...],
    file_label: str,
) -> list[str]:
    errors: list[str] = []
    for k in required_backend_keys:
        v = mapping.get(k)
        if not v or v == SELECT_TOKEN:
            errors.append(f"Validation Failed ({file_label}): required field '{k}' is not mapped.")
    return errors
