import os
import sys

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import polars as pl
from datetime import date

from engine.polars_list_compat import coerce_scalar_string_columns

def _legacy_ids_cell_to_plain_str(value) -> str:
    """
    Flatten legacy Item_Identifier list / Series / nested list cells to plain UTF-8
    for alert strings (no Polars Series repr).
    """
    if value is None:
        return ""
    if isinstance(value, pl.Series):
        lst = value.to_list()
        if not lst:
            return ""
        first = lst[0]
        if isinstance(first, list):
            return ", ".join(str(x) for x in first) if first else ""
        return str(first)
    if isinstance(value, (list, tuple)):
        if len(value) == 1 and isinstance(value[0], list):
            inner = value[0]
            return ", ".join(str(x) for x in inner) if inner else ""
        return ", ".join(str(x) for x in value)
    return str(value)


def _po_item_id_to_plain_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, pl.Series):
        lst = value.to_list()
        if not lst:
            return ""
        x = lst[0]
        if isinstance(x, list) and x:
            return str(x[0])
        return str(x)
    if isinstance(value, (list, tuple)):
        if len(value) == 1 and isinstance(value[0], list) and value[0]:
            return str(value[0][0])
        return str(value[0]) if value else ""
    return str(value)


_GOVERNOR_STRING_COLS = (
    "Item_Identifier",
    "Item_Location",
    "Item_Description",
    "Replacement_Item_Identifier",
    "Preferred_Vendor",
    "Item_Type",
)


def enrich_inventory_health(df_inv: pl.DataFrame) -> pl.DataFrame:
    """
    Evaluates physical inventory for operational stock status, including
    supersession-aware dead-stock classification at the location row level.
    """
    if df_inv.is_empty():
        return df_inv

    current_date = date.today()

    df_inv = coerce_scalar_string_columns(df_inv, _GOVERNOR_STRING_COLS)

    df_vel = df_inv.group_by("Item_Identifier").agg(
        pl.col("Annual_Sold").fill_null(0.0).sum().alias("Global_Annual_Sold")
    )
    df_inv = df_inv.join(
        df_vel,
        left_on="Replacement_Item_Identifier",
        right_on="Item_Identifier",
        how="left",
    ).rename({"Global_Annual_Sold": "Replacement_Annual_Sold"})
    df_inv = df_inv.with_columns(pl.col("Replacement_Annual_Sold").fill_null(0.0))

    df_inv = df_inv.with_columns([
        (pl.lit(current_date) - pl.col("Last_Sales_Date")).dt.total_days().fill_null(999.0).alias("Days_Since_Last_Sale"),
        (pl.max_horizontal(pl.col("Quantity_On_Hand"), 0.0) * pl.col("Unit_Average_Cost")).alias("Location_Capital_Deployed"),
    ])

    df_inv = df_inv.with_columns(
        pl.when(
            (pl.col("Annual_Sold").fill_null(0.0) == 0)
            & (pl.col("Days_Since_Last_Sale") >= 365)
            & (pl.col("Replacement_Annual_Sold") == 0)
        )
        .then(pl.lit("Dead Stock (>365d)"))
        .otherwise(pl.lit("Healthy"))
        .alias("Stock_Status")
    )

    return df_inv


def audit_purchase_orders(
    df_inv_enriched: pl.DataFrame,
    df_po: pl.DataFrame,
    df_manifest: pl.DataFrame,
    *,
    toxic_po_material_threshold: float = 250.0,
    enforce_supersession: bool = True,
) -> pl.DataFrame:
    """
    Cross-references pending PO lines against inventory and supersession rules.
    """
    if df_po.is_empty():
        print("[Govnr Logic] No pending POs to audit today.")
        return df_po

    df_po = coerce_scalar_string_columns(
        df_po,
        ("PO_Number", "Order_Status", "Purchase_Order_Vendor", "Item_Identifier"),
    )

    df_legacy = (
        df_inv_enriched.filter(
            (pl.col("Replacement_Item_Identifier") != "") & (pl.col("Quantity_On_Hand") > 0)
        )
        .group_by("Replacement_Item_Identifier")
        .agg(
            [
                pl.col("Item_Identifier").implode().alias("Legacy_Item_Identifiers"),
                pl.col("Quantity_On_Hand").sum().alias("Legacy_On_Hand_Total"),
            ]
        )
    )

    df_item_net = df_inv_enriched.group_by("Item_Identifier").agg([
        pl.col("Quantity_On_Hand").sum().alias("Net_Qty"),
        pl.col("Quantity_Committed").sum().alias("Net_Committed"),
        pl.col("Target_Stock_Level").fill_null(0.0).sum().alias("Global_Target_Stock_Level"),
        pl.col("Stock_Status").first().alias("Stock_Status"),
        pl.col("Item_Description").first().alias("Item_Description"),
    ])

    df_manifest_join = (
        df_manifest.group_by("Item_Identifier")
        .agg(pl.col("Transfer_Qty").sum().alias("Transfer_Qty"))
        if (df_manifest is not None and not df_manifest.is_empty() and "Item_Identifier" in df_manifest.columns)
        else pl.DataFrame({"Item_Identifier": [], "Transfer_Qty": []})
    )

    df_audit = df_po.join(df_item_net, on="Item_Identifier", how="left")
    df_audit = df_audit.join(df_manifest_join, on="Item_Identifier", how="left")
    df_audit = df_audit.join(
        df_legacy,
        left_on="Item_Identifier",
        right_on="Replacement_Item_Identifier",
        how="left",
    )
    df_audit = df_audit.with_columns(pl.col("Legacy_On_Hand_Total").fill_null(0.0))

    df_audit = df_audit.with_columns(
        pl.col("Legacy_Item_Identifiers")
        .map_elements(_legacy_ids_cell_to_plain_str, return_dtype=pl.Utf8)
        .alias("_legacy_ids_display")
    )
    df_audit = df_audit.with_columns(
        pl.col("Item_Identifier")
        .map_elements(_po_item_id_to_plain_str, return_dtype=pl.Utf8)
        .alias("_replacement_item_plain")
    )

    df_audit = df_audit.with_columns(
        pl.when(pl.col("Legacy_On_Hand_Total") > 0)
        .then(
            pl.concat_str(
                [
                    pl.lit(
                        "Replacement Item Alert: Recommended to deplete legacy Item_Identifier ["
                    ),
                    pl.col("_legacy_ids_display"),
                    pl.lit("] before ordering replacement Item_Identifier ["),
                    pl.col("_replacement_item_plain"),
                    pl.lit("]"),
                ],
                separator="",
            )
        )
        .when(pl.col("Stock_Status") == "Dead Stock (>365d)")
        .then(pl.lit("CANCEL LINE: Dead Stock (NPI)"))
        .when(pl.col("Transfer_Qty").fill_null(0.0) > 0)
        .then(pl.lit("CANCEL LINE: Internal Transfer Available"))
        .when(
            (pl.col("Net_Qty").fill_null(0.0) + pl.col("Quantity_Ordered").fill_null(0.0) - pl.col("Net_Committed").fill_null(0.0))
            > pl.col("Global_Target_Stock_Level").fill_null(0.0)
        )
        .then(pl.lit(" REDUCE QTY: Exceeds Target Stock"))
        .otherwise(pl.lit(" APPROVED"))
        .alias("Governor_Action")
    )
    df_audit = df_audit.with_columns(
        pl.when(pl.col("Governor_Action") == " APPROVED")
        .then(pl.lit(" APPROVED"))
        .when(
            pl.col("Order_Total_Amount").cast(pl.Float64, strict=False).fill_null(0.0)
            < pl.lit(float(toxic_po_material_threshold), dtype=pl.Float64)
        )
        .then(pl.lit("SUPPRESSED: Under Material Threshold"))
        .when(
            pl.col("Governor_Action").str.contains("Replacement Item Alert")
            & pl.lit(not enforce_supersession)
        )
        .then(pl.lit("SUPPRESSED: Supersession Advisory Only"))
        .otherwise(pl.col("Governor_Action"))
        .alias("Governor_Action")
    )

    for _tmp in ("_legacy_ids_display", "_replacement_item_plain"):
        if _tmp in df_audit.columns:
            df_audit = df_audit.drop(_tmp)

    return df_audit


if __name__ == "__main__":
    from engine.netsuite_parser import ingest_inventory_snapshot, ingest_po_activity
    from engine.logic_advanced_erp import calculate_abc_and_gmroi, generate_transfer_manifest

    print("\n--- GOVNR LOGIC ENGINE EXECUTION ---")

    df_inv_raw = ingest_inventory_snapshot(client_uuid="client_test_001")
    df_po_raw = ingest_po_activity(client_uuid="client_test_001")

    df_inv_health = enrich_inventory_health(df_inv_raw)
    df_advanced = calculate_abc_and_gmroi(df_inv_health)
    df_manifest = generate_transfer_manifest(df_advanced)
    df_po_alerts = audit_purchase_orders(df_inv_health, df_po_raw, df_manifest)

    display_cols = [
        "PO_Number",
        "Item_Identifier",
        "Quantity_Ordered",
        "Order_Total_Amount",
        "Stock_Status",
        "Governor_Action",
    ]
    print("\n[DAILY STRATEGIC COMPLIANCE BRIEF (PO AUDIT)]")
    with pl.Config(tbl_rows=10, tbl_cols=10, fmt_str_lengths=50):
        print(df_po_alerts.select([c for c in display_cols if c in df_po_alerts.columns]))
