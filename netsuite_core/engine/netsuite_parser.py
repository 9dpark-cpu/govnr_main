import os
from typing import Any, Optional

import polars as pl

from engine.erp_config_loader import fetch_ingestion_config
from engine.govnr_exceptions import MissingFileError, SchemaDriftError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOUND_DIR = os.path.join(BASE_DIR, "data", "inbound")

INVENTORY_OPTIONAL_IN_DRIFT = frozenset({"UPC_Code", "Manufacturer_Part_Number", "Manufacturer_Brand"})

INVENTORY_REQUIRED_BACKEND = (
    "Item_Identifier",
    "Item_Description",
    "Item_Type",
    "Quantity_Available",
    "Quantity_Committed",
    "Unit_Average_Cost",
    "Unit_Base_Price",
    "Target_Stock_Level",
    "Preferred_Vendor",
    "Item_Location",
    "Last_Sales_Date",
    "Replacement_Item_Identifier",
)

ANNUAL_SOLD_REQUIRED_BACKEND = ("Item_Identifier", "Item_Location", "Annual_Sold")

PO_REQUIRED_BACKEND = (
    "PO_Number",
    "Date_Created",
    "Order_Status",
    "Purchase_Order_Vendor",
    "Item_Identifier",
    "Quantity_Ordered",
    "Order_Total_Amount",
)


def _load_erp_config(client_uuid: str, erp_config: Optional[dict[str, Any]]) -> dict[str, Any]:
    if erp_config is not None:
        return erp_config
    return fetch_ingestion_config(client_uuid)


def _rename_from_mapping(df: pl.DataFrame, mapping: dict[str, Any], file_label: str) -> pl.DataFrame:
    if not mapping:
        raise SchemaDriftError(
            f"Schema Drift ({file_label}): No column mapping is configured. "
            f"Please save mapping in ERP Integration Settings."
        )
    rename_pairs: dict[str, str] = {}
    for backend_key, csv_col in mapping.items():
        if csv_col is None or str(csv_col).strip() == "":
            continue
        csv_col = str(csv_col).strip()
        if csv_col not in df.columns:
            continue
        rename_pairs[csv_col] = str(backend_key)

    collision: dict[str, str] = {}
    for old_col, new_col in rename_pairs.items():
        if new_col in df.columns and old_col != new_col:
            collision[new_col] = f"{new_col}_unmapped"
    if collision:
        df = df.rename(collision)
    df = df.rename(rename_pairs)
    return df


def _audit_schema(df: pl.DataFrame, required: tuple[str, ...], file_label: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise SchemaDriftError(
            f"Schema Drift ({file_label}): Missing mapped column for "
            + ", ".join(missing)
            + ". Please update mapping in ERP Integration Settings."
        )


def ingest_inventory_snapshot(
    client_uuid: str,
    erp_config: Optional[dict[str, Any]] = None,
    file_name: str = "inventory_snapshot.csv",
) -> pl.DataFrame:
    cfg = _load_erp_config(client_uuid, erp_config)
    mapping = cfg.get("inventory_column_mapping") or {}

    file_path = os.path.join(INBOUND_DIR, file_name)
    if not os.path.exists(file_path):
        raise MissingFileError(
            f"[Govnr] Missing mandatory file: {file_name}. "
            f"Inventory and annual_sold exports are required before the engine can run."
        )

    df = pl.read_csv(file_path, infer_schema_length=10000, null_values=["", " ", "NA", "null"])
    df = _rename_from_mapping(df, mapping, "inventory_snapshot")
    _audit_schema(df, INVENTORY_REQUIRED_BACKEND, "inventory_snapshot")

    for col in INVENTORY_OPTIONAL_IN_DRIFT:
        if col not in df.columns:
            df = df.with_columns(pl.lit("").cast(pl.String).alias(col))

    if "Last_Sales_Date" not in df.columns:
        df = df.with_columns(pl.lit(None).cast(pl.String).alias("Last_Sales_Date"))

    if "Item_Type" in df.columns:
        df = df.filter(
            pl.col("Item_Type").str.to_lowercase().str.contains("inventory") | pl.col("Item_Type").is_null()
        )

    if "Annual_Sold" in df.columns:
        df = df.drop("Annual_Sold")

    df = df.with_columns([
        pl.col("Item_Identifier").cast(pl.String).str.strip_chars(),
        pl.col("Item_Description").cast(pl.String).fill_null("Unknown Application"),
        pl.col("Item_Location").cast(pl.String).str.strip_chars().fill_null("Unassigned"),
        pl.col("Preferred_Vendor").cast(pl.String).fill_null("Unknown"),
        pl.col("Replacement_Item_Identifier").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("UPC_Code").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("Manufacturer_Part_Number").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("Manufacturer_Brand").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("Quantity_Available").cast(pl.Float64).fill_null(0.0),
        pl.col("Quantity_Committed").cast(pl.Float64).fill_null(0.0),
        pl.col("Unit_Average_Cost").cast(pl.Float64).fill_null(0.0),
        pl.col("Unit_Base_Price").cast(pl.Float64).fill_null(0.0),
        pl.col("Target_Stock_Level").cast(pl.Float64).fill_null(0.0),
        pl.col("Last_Sales_Date").str.strptime(pl.Date, format="%Y-%m-%d", strict=False),
    ])

    df = df.with_columns(
        pl.max_horizontal(pl.col("Quantity_Available") + pl.col("Quantity_Committed"), 0.0).alias("Quantity_On_Hand")
    )

    print(f"[Govnr Engine] {client_uuid} | Ingested {df.height} valid inventory records.")
    return df


def ingest_annual_sold(
    client_uuid: str,
    erp_config: Optional[dict[str, Any]] = None,
    file_name: str = "annual_sold.csv",
) -> pl.DataFrame:
    cfg = _load_erp_config(client_uuid, erp_config)
    mapping = cfg.get("annual_sold_column_mapping") or {}

    file_path = os.path.join(INBOUND_DIR, file_name)
    if not os.path.exists(file_path):
        raise MissingFileError(
            f"[Govnr] Missing mandatory file: {file_name}. "
            f"Annual sold (365-day velocity) export is required."
        )

    df = pl.read_csv(file_path, infer_schema_length=10000, null_values=["", " ", "NA", "null"])
    df = _rename_from_mapping(df, mapping, "annual_sold")
    _audit_schema(df, ANNUAL_SOLD_REQUIRED_BACKEND, "annual_sold")

    df = df.with_columns([
        pl.col("Item_Identifier").cast(pl.String).str.strip_chars(),
        pl.col("Item_Location").cast(pl.String).str.strip_chars().fill_null("Unassigned"),
        pl.col("Annual_Sold").cast(pl.Float64).fill_null(0.0),
    ])

    print(f"[Govnr Engine] {client_uuid} | Ingested {df.height} annual_sold rows.")
    return df


def ingest_po_activity(
    client_uuid: str,
    erp_config: Optional[dict[str, Any]] = None,
    file_name: str = "po_activity.csv",
) -> pl.DataFrame:
    cfg = _load_erp_config(client_uuid, erp_config)
    mapping = cfg.get("po_column_mapping") or {}

    empty_schema = {
        "PO_Number": pl.String,
        "Date_Created": pl.Date,
        "Order_Status": pl.String,
        "Purchase_Order_Vendor": pl.String,
        "Item_Identifier": pl.String,
        "Quantity_Ordered": pl.Float64,
        "Order_Total_Amount": pl.Float64,
    }

    file_path = os.path.join(INBOUND_DIR, file_name)
    if not os.path.exists(file_path):
        return pl.DataFrame(schema=empty_schema)

    df = pl.read_csv(file_path, infer_schema_length=10000, null_values=["", " ", "NA", "null"])
    if df.is_empty():
        return pl.DataFrame(schema=empty_schema)

    df = _rename_from_mapping(df, mapping, "po_activity")
    _audit_schema(df, PO_REQUIRED_BACKEND, "po_activity")

    df = df.with_columns([
        pl.col("PO_Number").cast(pl.String).str.strip_chars(),
        pl.col("Order_Status").cast(pl.String).str.strip_chars(),
        pl.col("Purchase_Order_Vendor").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("Item_Identifier").cast(pl.String).str.strip_chars(),
        pl.col("Quantity_Ordered").cast(pl.Float64).fill_null(0.0),
        pl.col("Order_Total_Amount").cast(pl.Float64).fill_null(0.0),
    ])

    if df["Date_Created"].dtype == pl.String:
        df = df.with_columns(pl.col("Date_Created").str.strptime(pl.Date, format="%Y-%m-%d", strict=False))
    else:
        df = df.with_columns(pl.col("Date_Created").cast(pl.Date, strict=False))

    print(f"[Govnr Engine] {client_uuid} | Ingested {df.height} pending PO lines.")
    return df


if __name__ == "__main__":
    import sys

    cid = sys.argv[1] if len(sys.argv) > 1 else ""
    if not cid:
        print("Usage: python netsuite_parser.py <client_uuid>  (from netsuite_core, DATABASE_URL required)")
        sys.exit(0)
    try:
        df_inv = ingest_inventory_snapshot(client_uuid=cid)
        df_as = ingest_annual_sold(client_uuid=cid)
        df_po = ingest_po_activity(client_uuid=cid)
        print(df_inv.head(2))
        print(df_as.head(2))
        print(df_po.head(2))
    except (MissingFileError, SchemaDriftError) as exc:
        print(str(exc))
        sys.exit(1)
