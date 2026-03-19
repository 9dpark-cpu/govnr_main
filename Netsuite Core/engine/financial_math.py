import os
import sys
import polars as pl

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from engine.fitment_rules import get_fitment_expr
from engine.report_generator import export_audit_csvs, print_cfo_report

def calculate_financial_health(df_inv: pl.DataFrame):
    
    #  DEFENSIVE CASTING
    # df_inv already contains Annual_Velocity natively from our parser now
    df_master = df_inv.with_columns([
        pl.col("SKU").cast(pl.String).str.strip_chars(),
        pl.col("Location").cast(pl.String).str.strip_chars()
    ])
    
    # ---  LAYER 1: OPERATIONAL TRUTH (LOCATION LEVEL FOR CSV EXPORTS) ---
    df_master = df_master.with_columns([
        #  CLAMP AT 0: Ensure negative inventory never outputs negative dollar amounts in the CSVs
        (pl.max_horizontal(pl.col("Qty_On_Hand"), 0) * pl.col("Unit_Cost")).alias("Total_Capital_Deployed"),
        
        # Disparity fix: Ignores negative stock Turnover to prevent False Positives
        pl.when(pl.col("Qty_On_Hand") > 0)
          .then(pl.col("Annual_Velocity") / pl.col("Qty_On_Hand"))
          .otherwise(None).alias("Unit_Turnover"),
        
        get_fitment_expr().alias("Max_Fitment_Year")
    ])

    # ---  LAYER 2: FINANCIAL TRUTH (NET SKU LEVEL FOR CFO REPORT) ---
    # 1. Net the quantities across all locations for the SKU
    df_sku_net = df_master.group_by("SKU").agg([
        pl.col("Qty_On_Hand").sum().alias("Net_Qty"),
        pl.col("Unit_Cost").first().alias("Unit_Cost"),
        pl.col("Annual_Velocity").sum().alias("Annual_Velocity"), 
        pl.col("Max_Fitment_Year").first().alias("Max_Fitment_Year")
    ])
    
    # 2. Safely calculate Global CFO Capital using the Net Quantities, clamped strictly at 0
    df_sku_net = df_sku_net.with_columns([
        (pl.max_horizontal(pl.col("Net_Qty"), 0) * pl.col("Unit_Cost")).alias("Net_Capital"),
        (pl.col("Annual_Velocity") * pl.col("Unit_Cost")).alias("Net_COGS")
    ])

    # Calculate aggregate dollar values from the Netted SKU firewall
    total_inventory_value = df_sku_net["Net_Capital"].sum()
    total_annual_cogs = df_sku_net["Net_COGS"].sum()
    
    dead_val = df_sku_net.filter((pl.col("Net_Qty") > 0) & (pl.col("Annual_Velocity") == 0))["Net_Capital"].sum()
    obsolete_val = df_sku_net.filter((pl.col("Max_Fitment_Year") <= 2011) & (pl.col("Net_Qty") > 0))["Net_Capital"].sum()

    # ---  CSV METRIC ASSIGNMENTS ---
    # CSVs use df_master so the warehouse manager can see exactly WHICH location has the issue
    df_dead = df_master.filter((pl.col("Qty_On_Hand") > 0) & (pl.col("Annual_Velocity") == 0))
    df_obsolete = df_master.filter((pl.col("Max_Fitment_Year") <= 2011) & (pl.col("Qty_On_Hand") > 0))
    
    df_neg = df_master.filter(pl.col("Qty_On_Hand") < 0)
    neg_skus = df_neg["SKU"].n_unique()

    df_integrity = df_master.filter((pl.col("Unit_Cost") <= 0) | (pl.col("Unit_Cost") > pl.col("Price")))
    integrity_skus = df_integrity["SKU"].n_unique()
    integrity_qty = df_integrity["Qty_On_Hand"].sum()

    # Regional Disparity Logic
    df_loc_stats = df_master.group_by("SKU").agg([
        pl.col("Location").n_unique().alias("Loc_Count"),
        pl.col("Unit_Turnover").max().alias("Max_Turnover"),
        pl.col("Unit_Turnover").min().alias("Min_Turnover")
    ])
    
    df_disparity_skus = df_loc_stats.filter((pl.col("Loc_Count") > 1) & ((pl.col("Max_Turnover") - pl.col("Min_Turnover")) >= 1.0))
    disparity_sku_count = df_disparity_skus.height
    
    if disparity_sku_count > 0:
        df_disparity_export = df_master.join(df_disparity_skus.select("SKU"), on="SKU", how="inner").sort(["SKU", "Location"])
    else:
        df_disparity_export = df_master.clear()

    turnover_ratio = (total_annual_cogs / total_inventory_value) if total_inventory_value > 0 else 0
    grade = "A (Highly Efficient)" if turnover_ratio > 4.0 else "B (Healthy)" if turnover_ratio > 2.5 else "C (Lagging)" if turnover_ratio > 1.0 else "D (Critical Review Advised)"

    # Pass everything to the Output/Reporting module
    export_audit_csvs(df_dead, df_integrity, df_obsolete, df_neg, df_disparity_export)
    print_cfo_report(grade, total_inventory_value, turnover_ratio, dead_val, df_dead, 
                     integrity_qty, integrity_skus, df_integrity, 
                     obsolete_val, df_obsolete, 
                     disparity_sku_count, df_disparity_export, 
                     neg_skus, df_neg)
    
if __name__ == "__main__":
    from engine.netsuite_parser import ingest_inventory_snapshot
    
    # 1. Get the parsed NetSuite Inventory CSV Data
    df_inv_raw = ingest_inventory_snapshot("client_test_001")
    
    # 2. Run Your Exact Logic
    calculate_financial_health(df_inv_raw)   
