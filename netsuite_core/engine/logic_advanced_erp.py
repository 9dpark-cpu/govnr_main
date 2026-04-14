import os
import sys
import polars as pl

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def calculate_abc_and_gmroi(df_master: pl.DataFrame) -> pl.DataFrame:
    """
    1. Inventory criticality (ABC analysis based on revenue)
    2. Asset productivity (GMROI per item per location)
    """
    df = df_master.with_columns([
        (pl.col("Annual_Sold").fill_null(0.0) * pl.col("Unit_Base_Price")).alias("Annual_Revenue"),
        (
            (pl.col("Unit_Base_Price") - pl.col("Unit_Average_Cost"))
            * pl.col("Annual_Sold").fill_null(0.0)
        ).alias("Annual_Margin"),
    ])

    df_item_rev = (
        df.group_by("Item_Identifier")
        .agg(pl.col("Annual_Revenue").sum().alias("Total_Item_Revenue"))
        .sort("Total_Item_Revenue", descending=True)
    )

    total_rev = df_item_rev["Total_Item_Revenue"].sum()

    if total_rev > 0:
        df_item_rev = df_item_rev.with_columns(
            [(pl.col("Total_Item_Revenue").cum_sum() / total_rev).alias("Cum_Rev_Pct")]
        )
        df_item_rev = df_item_rev.with_columns(
            [
                pl.when(pl.col("Cum_Rev_Pct") <= 0.80)
                .then(pl.lit("A-Item (Top 80%)"))
                .when(pl.col("Cum_Rev_Pct") <= 0.95)
                .then(pl.lit("B-Item (Mid 15%)"))
                .otherwise(pl.lit("C-Item (Bottom 5%)"))
                .alias("ABC_Class")
            ]
        )
    else:
        df_item_rev = df_item_rev.with_columns(pl.lit("C-Item (Bottom 5%)").alias("ABC_Class"))

    df = df.join(df_item_rev.select(["Item_Identifier", "ABC_Class"]), on="Item_Identifier", how="left")

    df = df.with_columns(
        [
            pl.when(pl.col("Location_Capital_Deployed") > 0)
            .then(pl.col("Annual_Margin") / pl.col("Location_Capital_Deployed"))
            .otherwise(0.0)
            .alias("GMROI")
        ]
    )

    return df


def generate_transfer_manifest(df_master: pl.DataFrame) -> pl.DataFrame:
    """Inter-branch allocation manifest from target stock vs available quantity."""
    df = df_master.with_columns([
        pl.max_horizontal(pl.col("Quantity_Available").fill_null(0.0) - pl.col("Target_Stock_Level").fill_null(0.0), 0.0).alias(
            "Surplus_Qty"
        ),
        pl.max_horizontal(pl.col("Target_Stock_Level").fill_null(0.0) - pl.col("Quantity_Available").fill_null(0.0), 0.0).alias(
            "Deficit_Qty"
        ),
    ])

    df_loc_count = df.group_by("Item_Identifier").agg(pl.col("Item_Location").n_unique().alias("Loc_Count"))
    multi_loc_items = df_loc_count.filter(pl.col("Loc_Count") > 1)["Item_Identifier"].to_list()

    df_multi = df.filter(pl.col("Item_Identifier").is_in(multi_loc_items))

    if df_multi.is_empty():
        return pl.DataFrame()

    df_surplus = df_multi.filter(pl.col("Surplus_Qty") > 0).select(
        ["Item_Identifier", "Item_Location", "Surplus_Qty"]
    ).rename({"Item_Location": "Source_Whse"})
    df_deficit = df_multi.filter(pl.col("Deficit_Qty") > 0).select(
        ["Item_Identifier", "Item_Location", "Deficit_Qty"]
    ).rename({"Item_Location": "Dest_Whse"})

    manifest = df_surplus.join(df_deficit, on="Item_Identifier", how="inner")

    if not manifest.is_empty():
        manifest = manifest.with_columns(
            [pl.min_horizontal(pl.col("Surplus_Qty"), pl.col("Deficit_Qty")).alias("Transfer_Qty")]
        )
        return manifest.select(["Item_Identifier", "Source_Whse", "Dest_Whse", "Transfer_Qty"]).sort(
            "Transfer_Qty", descending=True
        )

    return pl.DataFrame()


def generate_tax_mitigation_plan(df_master: pl.DataFrame) -> float:
    """Potential write-off exposure from zero-velocity inventory (unique items)."""
    df_writeoff = df_master.filter((pl.col("Annual_Sold").fill_null(0.0) == 0)).unique(subset=["Item_Identifier"])

    return float(df_writeoff["Location_Capital_Deployed"].sum()) if not df_writeoff.is_empty() else 0.0


if __name__ == "__main__":
    from engine.netsuite_parser import ingest_inventory_snapshot
    from engine.logic_governor import enrich_inventory_health

    df_inv = ingest_inventory_snapshot("client_test_001")
    df_master = enrich_inventory_health(df_inv)

    df_advanced = calculate_abc_and_gmroi(df_master)
    df_manifest = generate_transfer_manifest(df_advanced)
    tax_writeoff = generate_tax_mitigation_plan(df_master)

    print("\n[TAX MITIGATION & ASSET RETIREMENT PLAN]")
    print(f" -> Projected Year-End Write-Off Potential: ${tax_writeoff:,.2f}")
