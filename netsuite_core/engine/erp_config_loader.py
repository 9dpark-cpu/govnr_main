"""
Load client_ingestion_config for parsers and workers without importing Streamlit.
Uses DATABASE_URL from the environment only.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import psycopg2
from psycopg2.extras import RealDictCursor


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is required for ERP config loading.")
    return url


def _as_dict(v: Any) -> dict:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {}
    return {}


def _split_po_prefix_from_inventory_map(inv_map: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Legacy clients stored PO fields under clients.inventory_column_mapping as PO_* keys."""
    clean: dict[str, Any] = {}
    po: dict[str, Any] = {}
    for k, v in inv_map.items():
        if str(k).startswith("PO_"):
            po[str(k)[3:]] = v
        else:
            clean[str(k)] = v
    return clean, po


def fetch_ingestion_config(client_uuid: str) -> dict[str, Any]:
    """
    Returns weekly_otb_limit, filename patterns, and three mapping dicts
    (backend_var -> raw_csv_column_name). Missing row returns dict of Nones/empty dicts.
    """
    conn = psycopg2.connect(_database_url(), connect_timeout=15, cursor_factory=RealDictCursor)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT
                weekly_otb_limit,
                transfer_hurdle_limit,
                min_transfer_value_threshold,
                inventory_filename_pattern,
                annual_sold_filename_pattern,
                po_filename_pattern,
                inventory_column_mapping,
                annual_sold_column_mapping,
                po_column_mapping
            FROM client_ingestion_config
            WHERE client_uuid = %s
            """,
            (client_uuid,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                "SELECT inventory_column_mapping FROM clients WHERE client_uuid = %s",
                (client_uuid,),
            )
            legacy = cursor.fetchone()
            legacy_inv = _as_dict(legacy.get("inventory_column_mapping")) if legacy else {}
            legacy_inv, legacy_po = _split_po_prefix_from_inventory_map(legacy_inv)
            return {
                "weekly_otb_limit": None,
                "transfer_hurdle_limit": None,
                "min_transfer_value_threshold": None,
                "inventory_filename_pattern": "inventory_snapshot",
                "annual_sold_filename_pattern": "annual_sold",
                "po_filename_pattern": "po_activity",
                "inventory_column_mapping": legacy_inv,
                "annual_sold_column_mapping": {},
                "po_column_mapping": legacy_po,
            }

        inv_map = _as_dict(row.get("inventory_column_mapping"))
        po_map = _as_dict(row.get("po_column_mapping"))
        as_map = _as_dict(row.get("annual_sold_column_mapping"))

        if not inv_map:
            cursor.execute(
                "SELECT inventory_column_mapping FROM clients WHERE client_uuid = %s",
                (client_uuid,),
            )
            legacy = cursor.fetchone()
            if legacy and legacy.get("inventory_column_mapping"):
                inv_map = _as_dict(legacy["inventory_column_mapping"])

        inv_map, split_po = _split_po_prefix_from_inventory_map(inv_map)
        if not po_map and split_po:
            po_map = split_po

        return {
            "weekly_otb_limit": row.get("weekly_otb_limit"),
            "transfer_hurdle_limit": row.get("transfer_hurdle_limit"),
            "min_transfer_value_threshold": row.get("min_transfer_value_threshold"),
            "inventory_filename_pattern": row.get("inventory_filename_pattern") or "inventory_snapshot",
            "annual_sold_filename_pattern": row.get("annual_sold_filename_pattern") or "annual_sold",
            "po_filename_pattern": row.get("po_filename_pattern") or "po_activity",
            "inventory_column_mapping": inv_map,
            "annual_sold_column_mapping": as_map,
            "po_column_mapping": po_map,
        }
    finally:
        cursor.close()
        conn.close()


def resolve_weekly_otb_limit(raw: Optional[Any]) -> tuple[float, bool]:
    """
    Returns (limit, used_fallback). Invalid or missing -> 100000.0 and fallback True.
    """
    import math

    try:
        v = float(raw)
        if math.isnan(v) or v < 0:
            return 100000.0, True
        return v, False
    except (TypeError, ValueError):
        return 100000.0, True


def resolve_min_transfer_value_threshold(raw: Optional[Any]) -> tuple[float, bool]:
    """
    Returns (threshold, used_fallback). Invalid or missing -> 1000.0 and fallback True.
    """
    import math

    try:
        v = float(raw)
        if math.isnan(v) or v < 0:
            return 1000.0, True
        return v, False
    except (TypeError, ValueError):
        return 1000.0, True


def resolve_transfer_hurdle_limit(raw: Optional[Any]) -> tuple[float, bool]:
    """
    Returns (threshold, used_fallback). Invalid or missing -> 250.0 and fallback True.
    """
    import math

    try:
        v = float(raw)
        if math.isnan(v) or v < 0:
            return 250.0, True
        return v, False
    except (TypeError, ValueError):
        return 250.0, True
