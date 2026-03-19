import os
import sys

# --- DEFENSIVE PATHING ---
# Dynamically add the "Netsuite Core" root folder to Python's path 
# so it can find the 'engine' module regardless of how you run the script.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import polars as pl
from datetime import date
from engine.fitment_rules import get_fitment_expr

def enrich_inventory_health(df_inv: pl.DataFrame) -> pl.DataFrame:
    """
    Evaluates the physical inventory for Obsolescence, Dead Stock, and Margin Blindness.
    """
    if df_inv.is_empty():
        return df_inv

    # Ensure dynamic date handling (Current Context: March 2026)
    current_date = date.today()

    # 1. Apply Fitment & Time-Based Logic
    df_inv = df_inv.with_columns([
        get_fitment_expr().alias("Max_Fitment_Year"),
        # Calculate days since last sale. If null (never sold), assign 999
        (pl.lit(current_date) - pl.col("Last_Sales_Date")).dt.total_days().fill_null(999).alias("Days_Since_Sale"),
        # Capital Deployed (clamped to 0 to ignore negative stock sync drift)
        (pl.max_horizontal(pl.col("Qty_On_Hand"), 0) * pl.col("Unit_Cost")).alias("Capital_Deployed")
    ])

    # 2. Assign the CFO "Stock Status"
    df_inv = df_inv.with_columns(
        pl.when(pl.col("Max_Fitment_Year") <= 2011)
          .then(pl.lit("Obsolete (Pre-2011)"))
        .when(pl.col("Days_Since_Sale") >= 365)
          .then(pl.lit("Dead Stock (>365d)"))
        .otherwise(pl.lit("Healthy"))
        .alias("Stock_Status")
    )
    
    return df_inv


def audit_purchase_orders(df_inv_enriched: pl.DataFrame, df_po: pl.DataFrame) -> pl.DataFrame:
    """
    The Core Governor: Cross-references pending POs against inventory health.
    Flags Toxic Buys and Overstock limits.
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

    # 2. Join pending POs with the Net Inventory Truth
    df_audit = df_po.join(df_sku_net, on="SKU", how="left")

    # 3. Apply the "Governor Action" Logic (CFO Guardrails)
    df_audit = df_audit.with_columns(
        pl.when(pl.col("Stock_Status").is_in(["Obsolete (Pre-2011)", "Dead Stock (>365d)"]))
          .then(pl.lit(" CANCEL LINE: Toxic Stock Detected"))
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