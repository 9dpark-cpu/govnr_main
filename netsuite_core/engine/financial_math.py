import os
import sys
import polars as pl

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from engine.report_generator import generate_audit_csv_buffers, print_cfo_report
from engine.report_generator_pdf import generate_cfo_pdf

def calculate_financial_health(df_inv: pl.DataFrame):
    
    df_master = df_inv.with_columns([
        pl.col("SKU").cast(pl.String).str.strip_chars(),
        pl.col("Location").cast(pl.String).str.strip_chars(),
        pl.col("Replaced_By").cast(pl.String).str.strip_chars().fill_null("")
    ])

    # --- SUPERSESSION LOGIC (PREVENT FALSE POSITIVE DEAD STOCK) ---
    df_global_vel = df_master.group_by("SKU").agg(pl.col("Annual_Velocity").sum().alias("Global_Velocity"))

    df_master = df_master.join(
        df_global_vel,
        left_on="Replaced_By",
        right_on="SKU",
        how="left"
    ).rename({"Global_Velocity": "Replacement_Velocity"}).with_columns(
        pl.col("Replacement_Velocity").fill_null(0)
    )
    
    # ---  LAYER 1: OPERATIONAL TRUTH (LOCATION LEVEL FOR CSV EXPORTS) ---
    df_master = df_master.with_columns([
        (pl.max_horizontal(pl.col("Qty_On_Hand"), 0) * pl.col("Unit_Cost")).alias("Total_Capital_Deployed"),
        pl.when(pl.col("Qty_On_Hand") > 0)
          .then(pl.col("Annual_Velocity") / pl.col("Qty_On_Hand"))
          .otherwise(None).alias("Unit_Turnover")
    ])

    # ---  LAYER 2: FINANCIAL TRUTH (NET SKU LEVEL FOR CFO REPORT) ---
    df_sku_net = df_master.group_by("SKU").agg([
        pl.col("Qty_On_Hand").sum().alias("Net_Qty"),
        pl.col("Unit_Cost").first().alias("Unit_Cost"),
        pl.col("Annual_Velocity").sum().alias("Annual_Velocity"), 
        pl.col("Replacement_Velocity").first().alias("Replacement_Velocity")
    ])
    
    df_sku_net = df_sku_net.with_columns([
        (pl.max_horizontal(pl.col("Net_Qty"), 0) * pl.col("Unit_Cost")).alias("Net_Capital"),
        (pl.col("Annual_Velocity") * pl.col("Unit_Cost")).alias("Net_COGS")
    ])

    total_inventory_value = df_sku_net["Net_Capital"].sum()
    total_annual_cogs = df_sku_net["Net_COGS"].sum()
    
    # DEAD STOCK LOGIC
    dead_val = df_sku_net.filter(
        (pl.col("Net_Qty") > 0) & 
        (pl.col("Annual_Velocity") == 0) & 
        (pl.col("Replacement_Velocity") == 0)
    )["Net_Capital"].sum()

    # ---  CSV METRIC ASSIGNMENTS ---
    df_dead = df_master.filter(
        (pl.col("Qty_On_Hand") > 0) & 
        (pl.col("Annual_Velocity") == 0) & 
        (pl.col("Replacement_Velocity") == 0)
    )
    
    df_neg = df_master.filter(pl.col("Qty_On_Hand") < 0)
    neg_skus = df_neg["SKU"].n_unique()

    df_integrity = df_master.filter((pl.col("Unit_Cost") <= 0) | (pl.col("Unit_Cost") > pl.col("Price")))
    integrity_skus = df_integrity["SKU"].n_unique()
    integrity_qty = df_integrity["Qty_On_Hand"].sum()

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

    # Terminal Report
    print_cfo_report(grade, total_inventory_value, turnover_ratio, dead_val, df_dead, 
                     integrity_qty, integrity_skus, df_integrity, 
                     disparity_sku_count, df_disparity_export, 
                     neg_skus, df_neg)

    # In-Memory Buffer Generation
    buffers = generate_audit_csv_buffers(df_dead, df_integrity, df_neg, df_disparity_export)
    
    # Inject the PDF
    pdf_buffer = generate_cfo_pdf(grade, total_inventory_value, turnover_ratio, dead_val, 
                                  integrity_qty, integrity_skus, disparity_sku_count, neg_skus)
    buffers["Govnr_Executive_Brief.pdf"] = pdf_buffer.getvalue()
                     
    return buffers