import os
import sys
import polars as pl

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

def calculate_abc_and_gmroi(df_master: pl.DataFrame) -> pl.DataFrame:
    """
    1. Inventory Criticality (ABC Analysis based on Revenue)
    2. Asset Productivity (GMROI per SKU per Location)
    """
    df = df_master.with_columns([
        (pl.col("Annual_Velocity") * pl.col("Price")).alias("Annual_Revenue"),
        ((pl.col("Price") - pl.col("Unit_Cost")) * pl.col("Annual_Velocity")).alias("Annual_Margin")
    ])

    df_sku_rev = df.group_by("SKU").agg([
        pl.col("Annual_Revenue").sum().alias("Total_SKU_Revenue")
    ]).sort("Total_SKU_Revenue", descending=True)

    total_rev = df_sku_rev["Total_SKU_Revenue"].sum()
    
    if total_rev > 0:
        df_sku_rev = df_sku_rev.with_columns([
            (pl.col("Total_SKU_Revenue").cum_sum() / total_rev).alias("Cum_Rev_Pct")
        ])
        df_sku_rev = df_sku_rev.with_columns([
            pl.when(pl.col("Cum_Rev_Pct") <= 0.80).then(pl.lit("A-Item (Top 80%)"))
            .when(pl.col("Cum_Rev_Pct") <= 0.95).then(pl.lit("B-Item (Mid 15%)"))
            .otherwise(pl.lit("C-Item (Bottom 5%)"))
            .alias("ABC_Class")
        ])
    else:
        df_sku_rev = df_sku_rev.with_columns(pl.lit("C-Item (Bottom 5%)").alias("ABC_Class"))

    df = df.join(df_sku_rev.select(["SKU", "ABC_Class"]), on="SKU", how="left")
    
    # FIX: Changed to "Capital_Deployed"
    df = df.with_columns([
        pl.when(pl.col("Capital_Deployed") > 0)
          .then(pl.col("Annual_Margin") / pl.col("Capital_Deployed"))
          .otherwise(0.0).alias("GMROI")
    ])

    return df

def generate_transfer_manifest(df_master: pl.DataFrame) -> pl.DataFrame:
    """Inter-Branch Allocation Manifest."""
    df = df_master.with_columns([
        pl.max_horizontal(pl.col("Qty_Available") - pl.col("Target_Stock"), 0).alias("Surplus_Qty"),
        pl.max_horizontal(pl.col("Target_Stock") - pl.col("Qty_Available"), 0).alias("Deficit_Qty")
    ])

    df_loc_count = df.group_by("SKU").agg(pl.col("Location").n_unique().alias("Loc_Count"))
    multi_loc_skus = df_loc_count.filter(pl.col("Loc_Count") > 1)["SKU"].to_list()
    
    df_multi = df.filter(pl.col("SKU").is_in(multi_loc_skus))

    if df_multi.is_empty():
        return pl.DataFrame()

    df_surplus = df_multi.filter(pl.col("Surplus_Qty") > 0).select(["SKU", "Location", "Surplus_Qty"]).rename({"Location": "Source_Whse"})
    df_deficit = df_multi.filter(pl.col("Deficit_Qty") > 0).select(["SKU", "Location", "Deficit_Qty"]).rename({"Location": "Dest_Whse"})

    manifest = df_surplus.join(df_deficit, on="SKU", how="inner")
    
    if not manifest.is_empty():
        manifest = manifest.with_columns([
            pl.min_horizontal(pl.col("Surplus_Qty"), pl.col("Deficit_Qty")).alias("Transfer_Qty")
        ])
        return manifest.select(["SKU", "Source_Whse", "Dest_Whse", "Transfer_Qty"]).sort("Transfer_Qty", descending=True)
    
    return pl.DataFrame()

def generate_tax_mitigation_plan(df_master: pl.DataFrame) -> float:
    """Calculates unique $ value of potential write-offs (Dead OR Obsolete)."""
    df_writeoff = df_master.filter(
        (pl.col("Annual_Velocity") == 0) | (pl.col("Max_Fitment_Year") <= 2011)
    ).unique(subset=["SKU"])
    
    # FIX: Changed to "Capital_Deployed"
    return df_writeoff["Capital_Deployed"].sum() if not df_writeoff.is_empty() else 0.0

if __name__ == "__main__":
    from engine.netsuite_parser import ingest_inventory_snapshot
    from engine.fitment_rules import get_fitment_expr
    
    df_inv = ingest_inventory_snapshot("client_test_001")
    
    df_master = df_inv.with_columns(pl.col("SKU").str.strip_chars())
    df_master = df_master.with_columns([
        (pl.max_horizontal(pl.col("Qty_Available"), 0) * pl.col("Unit_Cost")).alias("Capital_Deployed"),
        get_fitment_expr().alias("Max_Fitment_Year")
    ])

    df_advanced = calculate_abc_and_gmroi(df_master)
    df_manifest = generate_transfer_manifest(df_advanced)
    tax_writeoff = generate_tax_mitigation_plan(df_master)

    print("\n[TAX MITIGATION & ASSET RETIREMENT PLAN]")
    print(f" -> Projected Year-End Write-Off Potential: ${tax_writeoff:,.2f}")