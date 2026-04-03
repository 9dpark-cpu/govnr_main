import os
import sys

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import polars as pl
from datetime import date

def enrich_inventory_health(df_inv: pl.DataFrame) -> pl.DataFrame:
    """
    Evaluates the physical inventory for Dead Stock, protecting against 
    false positives using Supersession (Replaced_By) logic.
    """
    if df_inv.is_empty():
        return df_inv

    current_date = date.today()
    
    df_inv = df_inv.with_columns([
        pl.col("Replaced_By").cast(pl.String).str.strip_chars().fill_null("")
    ])

    # 1. Supersession Velocity Sync
    df_vel = df_inv.group_by("SKU").agg(pl.col("Annual_Velocity").sum().alias("Global_Velocity"))
    df_inv = df_inv.join(df_vel, left_on="Replaced_By", right_on="SKU", how="left")
    df_inv = df_inv.rename({"Global_Velocity": "Replacement_Velocity"}).with_columns(pl.col("Replacement_Velocity").fill_null(0))

    # 2. Apply Time-Based Logic
    df_inv = df_inv.with_columns([
        (pl.lit(current_date) - pl.col("Last_Sales_Date")).dt.total_days().fill_null(999).alias("Days_Since_Sale"),
        (pl.max_horizontal(pl.col("Qty_On_Hand"), 0) * pl.col("Unit_Cost")).alias("Capital_Deployed")
    ])

    # 3. Assign the CFO "Stock Status"
    df_inv = df_inv.with_columns(
        pl.when((pl.col("Days_Since_Sale") >= 365) & (pl.col("Replacement_Velocity") == 0))
          .then(pl.lit("Dead Stock (>365d)"))
        .otherwise(pl.lit("Healthy"))
        .alias("Stock_Status")
    )
    
    return df_inv


def audit_purchase_orders(df_inv_enriched: pl.DataFrame, df_po: pl.DataFrame) -> pl.DataFrame:
    """
    The Core Governor: Cross-references pending POs against inventory health.
    Flags Toxic Buys, Overstock limits, and Legacy SKU Depletion mandates.
    """
    if df_po.is_empty():
        print("[Govnr Logic] No pending POs to audit today.")
        return df_po

    # 1. Net the inventory across all locations
    df_sku_net = df_inv_enriched.group_by("SKU").agg([
        pl.col("Qty_On_Hand").sum().alias("Net_Qty"),
        pl.col("Qty_Committed").sum().alias("Net_Committed"),
        pl.col("Target_Stock").sum().alias("Global_Target_Stock"),
        pl.col("Stock_Status").first().alias("Stock_Status"), 
        pl.col("Title").first().alias("Title")
    ])
    
    # 2. Identify Legacy SKUs with stock (for the Supersession Depletion Alert)
    df_legacy = df_inv_enriched.filter((pl.col("Replaced_By") != "") & (pl.col("Qty_On_Hand") > 0))
    df_legacy_agg = df_legacy.group_by("Replaced_By").agg([
        pl.col("SKU").first().alias("Legacy_SKU"), 
        pl.col("Qty_On_Hand").sum().alias("Legacy_Qty")
    ])

    # 3. Join pending POs with the Net Inventory Truth AND Legacy Stock Info
    df_audit = df_po.join(df_sku_net, on="SKU", how="left")
    df_audit = df_audit.join(df_legacy_agg, left_on="SKU", right_on="Replaced_By", how="left")
    df_audit = df_audit.with_columns(pl.col("Legacy_Qty").fill_null(0))

    # 4. Apply the "Governor Action" Logic (CFO Guardrails)
    df_audit = df_audit.with_columns(
        pl.when(pl.col("Stock_Status") == "Dead Stock (>365d)")
          .then(pl.lit(" CANCEL LINE: Toxic Stock Detected"))
        .when(pl.col("Legacy_Qty") > 0)
          .then(pl.format(" HOLD PO: Deplete Legacy SKU ({}) First", pl.col("Legacy_SKU")))
        .when((pl.col("Net_Qty") + pl.col("Qty_Ordered") - pl.col("Net_Committed")) > pl.col("Global_Target_Stock"))
          .then(pl.lit(" REDUCE QTY: Exceeds Target Stock"))
        .otherwise(pl.lit(" APPROVED"))
          .alias("Governor_Action")
    )

    return df_audit

if __name__ == "__main__":
    from engine.netsuite_parser import ingest_inventory_snapshot, ingest_po_activity
    
    print("\n--- GOVNR LOGIC ENGINE EXECUTION ---")
    
    # 1. Ingest Data
    df_inv_raw = ingest_inventory_snapshot(client_uuid="client_test_001")
    df_po_raw = ingest_po_activity(client_uuid="client_test_001")
    
    # 2. Enrich Inventory
    df_inv_health = enrich_inventory_health(df_inv_raw)
    
    # 3. Run PO Audit
    df_po_alerts = audit_purchase_orders(df_inv_health, df_po_raw)
    
    # Display the results
    display_cols =["PO_Number", "SKU", "Qty_Ordered", "Total_Amount", "Stock_Status", "Governor_Action"]
    print("\n[DAILY STRATEGIC COMPLIANCE BRIEF (PO AUDIT)]")
    with pl.Config(tbl_rows=10, tbl_cols=10, fmt_str_lengths=50):
        print(df_po_alerts.select(display_cols))