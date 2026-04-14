"""
Polars 1.x: columns can infer as list[str]. Do not use .str.* or list.join on those dtypes
without normalizing first (see .cursorrules).
"""

from __future__ import annotations

import polars as pl


def dtype_is_list_like(dt: pl.DataType) -> bool:
    if isinstance(dt, pl.List):
        return True
    if hasattr(dt, "is_nested"):
        try:
            return bool(dt.is_nested())
        except Exception:
            pass
    s = str(dt).lower()
    return "list[" in s or s.startswith("list(")


def _join_list_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(x) for x in value)
    return str(value)


def _first_scalar_stripped(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        return str(value[0]).strip()
    return str(value).strip()


def coerce_scalar_string_columns(df: pl.DataFrame, names: tuple[str, ...]) -> pl.DataFrame:
    """Ensure named columns are scalar Utf8 before any .str.* ops (take first list element if nested)."""
    exprs: list[pl.Expr] = []
    for name in names:
        if name not in df.columns:
            continue
        if dtype_is_list_like(df.schema[name]):
            exprs.append(
                pl.col(name).map_elements(_first_scalar_stripped, return_dtype=pl.Utf8).alias(name)
            )
        else:
            exprs.append(
                pl.col(name).cast(pl.Utf8, strict=False).fill_null("").str.strip_chars().alias(name)
            )
    return df.with_columns(exprs) if exprs else df


def flatten_list_columns_to_utf8_for_csv(df: pl.DataFrame) -> pl.DataFrame:
    """Turn any List-typed columns into plain Utf8 so write_csv does not call list.join on list[str]."""
    if df.is_empty():
        return df
    exprs: list[pl.Expr] = []
    for name, dt in df.schema.items():
        if dtype_is_list_like(dt):
            exprs.append(
                pl.col(name).map_elements(_join_list_cell, return_dtype=pl.Utf8).alias(name)
            )
    return df.with_columns(exprs) if exprs else df


def preview_cell(value, max_len: int = 25) -> str:
    """Safe one-line preview for console/PDF (handles list cells)."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        base = str(value[0])
    else:
        base = str(value)
    return base[:max_len]
