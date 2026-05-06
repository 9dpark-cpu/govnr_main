import os
import sys
from datetime import date

import polars as pl

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from engine.logic_cash import compute_rolling_po_exposure_total
from engine.polars_list_compat import coerce_scalar_string_columns
from engine.report_generator import generate_audit_csv_buffers, print_cfo_report
from engine.report_generator_pdf import generate_cfo_pdf

MIN_TRANSFER_VALUE_THRESHOLD = 50.0

_STRING_COLS_FOR_ENGINE = (
    "Item_Identifier",
    "Item_Location",
    "Item_Description",
    "Replacement_Item_Identifier",
    "Preferred_Vendor",
    "Item_Type",
    "UPC_Code",
    "Manufacturer_Part_Number",
    "Manufacturer_Brand",
)


def calculate_financial_health(
    df_inv: pl.DataFrame,
    df_po: pl.DataFrame,
    weekly_otb_limit: float = 100_000.0,
    transfer_hurdle_limit: float = 250.0,
    min_transfer_value_threshold: float = MIN_TRANSFER_VALUE_THRESHOLD,
):
    current_date = date.today()

    df_master = coerce_scalar_string_columns(df_inv, _STRING_COLS_FOR_ENGINE).with_columns([
        pl.max_horizontal(pl.col("Quantity_Available").fill_null(0) + pl.col("Quantity_Committed").fill_null(0), 0.0).alias(
            "Quantity_On_Hand"
        ),
        (pl.lit(current_date) - pl.col("Last_Sales_Date")).dt.total_days().fill_null(999.0).alias("Days_Since_Last_Sale"),
        (pl.col("Annual_Sold").fill_null(0.0) / 365.0).alias("Daily_Sales_Velocity"),
    ])

    df_master = df_master.with_columns(
        pl.when(pl.col("Daily_Sales_Velocity") > 0)
        .then(pl.col("Quantity_On_Hand") / pl.col("Daily_Sales_Velocity"))
        .otherwise(999.0)
        .alias("Location_Days_Supply")
    )

    df_master = df_master.with_columns([
        (pl.col("Quantity_On_Hand") * pl.col("Unit_Average_Cost")).alias("Location_Capital_Deployed"),
        (pl.col("Annual_Sold").fill_null(0.0) * pl.col("Unit_Average_Cost")).alias("Location_Annual_COGS"),
    ])

    df_rep_vel = df_master.group_by("Item_Identifier").agg(
        pl.col("Annual_Sold").fill_null(0.0).sum().alias("Replacement_Annual_Sold")
    ).rename({"Item_Identifier": "_replacement_lookup_key"})
    df_master = df_master.join(
        df_rep_vel,
        left_on="Replacement_Item_Identifier",
        right_on="_replacement_lookup_key",
        how="left",
    )
    df_master = df_master.with_columns(pl.col("Replacement_Annual_Sold").fill_null(0.0))

    non_performing = (
        (pl.col("Annual_Sold").fill_null(0.0) == 0)
        & (pl.col("Days_Since_Last_Sale") > 365)
        & (
            (pl.col("Replacement_Item_Identifier") == "")
            | (pl.col("Replacement_Annual_Sold") == 0)
        )
        & (pl.col("Quantity_On_Hand") > 0)
    )

    gross_profit_inversion = (pl.col("Unit_Average_Cost") >= pl.col("Unit_Base_Price")) & (
        pl.col("Quantity_On_Hand") > 0
    )
    cost_basis_integrity = (pl.col("Unit_Average_Cost") <= 0.0) & (pl.col("Quantity_On_Hand") > 0)

    df_item_cost = df_master.group_by("Item_Identifier").agg(
        [
            pl.col("Unit_Average_Cost").max().alias("Max_Unit_Cost"),
            pl.col("Unit_Average_Cost").min().alias("Min_Unit_Cost"),
        ]
    )
    df_item_cost = df_item_cost.with_columns(
        pl.when((pl.col("Min_Unit_Cost") > 0) & (pl.col("Max_Unit_Cost") > 0))
        .then((pl.col("Max_Unit_Cost") - pl.col("Min_Unit_Cost")) / pl.col("Min_Unit_Cost"))
        .otherwise(0.0)
        .alias("Interbranch_Cost_Variance_Ratio")
    )
    df_master = df_master.join(df_item_cost, on="Item_Identifier", how="left")

    loc_min = df_master.group_by("Item_Identifier").agg(pl.col("Location_Days_Supply").min().alias("_min_days"))
    loc_max = df_master.group_by("Item_Identifier").agg(pl.col("Location_Days_Supply").max().alias("_max_days"))

    df_transfer_math = (
        df_master.with_columns([
            pl.max_horizontal(
                pl.col("Quantity_Available").fill_null(0.0) - pl.col("Target_Stock_Level").fill_null(0.0), 0.0
            ).alias("_surplus"),
            pl.max_horizontal(
                pl.col("Target_Stock_Level").fill_null(0.0) - pl.col("Quantity_Available").fill_null(0.0), 0.0
            ).alias("_deficit"),
        ])
        .group_by("Item_Identifier")
        .agg([
            pl.col("_surplus").sum().alias("_total_surplus"),
            pl.col("_deficit").sum().alias("_total_deficit"),
            pl.col("Unit_Average_Cost").first().alias("_avg_cost"),
        ])
        .with_columns(
            (pl.min_horizontal(pl.col("_total_surplus"), pl.col("_total_deficit")) * pl.col("_avg_cost")).alias(
                "Potential_Transfer_Value"
            )
        )
        .select(["Item_Identifier", "Potential_Transfer_Value"])
    )

    df_loc_bounds = (
        loc_min.join(loc_max, on="Item_Identifier", how="inner")
        .join(df_transfer_math, on="Item_Identifier", how="left")
        .with_columns(
            (
                (pl.col("_min_days") < 15)
                & (pl.col("_max_days") > 180)
                & (pl.col("Potential_Transfer_Value").fill_null(0.0) >= pl.lit(transfer_hurdle_limit))
            ).alias("Inter_Branch_Misallocation")
        )
    )
    df_master = df_master.join(
        df_loc_bounds.select(["Item_Identifier", "Inter_Branch_Misallocation"]),
        on="Item_Identifier",
        how="left",
    ).with_columns(pl.col("Inter_Branch_Misallocation").fill_null(False))

    # Transfer math: bloated location surplus and starved location deficit per item.
    df_master = df_master.with_columns([
        (pl.col("Quantity_Available").fill_null(0.0).cast(pl.Float64, strict=False)).alias("Quantity_Available"),
        (pl.col("Target_Stock_Level").fill_null(0.0).cast(pl.Float64, strict=False)).alias("Target_Stock_Level"),
        (pl.col("Unit_Average_Cost").fill_null(0.0).cast(pl.Float64, strict=False)).alias("Unit_Average_Cost"),
    ])

    df_starved = (
        df_master.filter(pl.col("Location_Days_Supply") < 15.0)
        .with_columns(
            (pl.col("Target_Stock_Level") - pl.col("Quantity_Available"))
            .clip(lower_bound=0.0)
            .cast(pl.Float64, strict=False)
            .alias("Deficit_Qty")
        )
        .group_by("Item_Identifier")
        .agg(pl.col("Deficit_Qty").sum().cast(pl.Float64, strict=False).alias("Total_Deficit_Qty"))
    )
    df_bloated = (
        df_master.filter(pl.col("Location_Days_Supply") > 180.0)
        .with_columns(
            (pl.col("Quantity_Available") - pl.col("Target_Stock_Level"))
            .clip(lower_bound=0.0)
            .cast(pl.Float64, strict=False)
            .alias("Surplus_Qty")
        )
        .group_by("Item_Identifier")
        .agg(pl.col("Surplus_Qty").sum().cast(pl.Float64, strict=False).alias("Total_Surplus_Qty"))
    )
    df_transfer_qty = (
        df_bloated.join(df_starved, on="Item_Identifier", how="inner")
        .with_columns(
            pl.min_horizontal(
                pl.col("Total_Surplus_Qty").fill_null(0.0),
                pl.col("Total_Deficit_Qty").fill_null(0.0),
            )
            .clip(lower_bound=0.0)
            .cast(pl.Float64, strict=False)
            .alias("Transfer_Qty")
        )
        .select(["Item_Identifier", "Transfer_Qty"])
    )
    df_master = df_master.join(df_transfer_qty, on="Item_Identifier", how="left").with_columns(
        pl.col("Transfer_Qty").fill_null(0.0).cast(pl.Float64, strict=False)
    )
    df_master = df_master.with_columns(
        (pl.col("Transfer_Qty") * pl.col("Unit_Average_Cost"))
        .cast(pl.Float64, strict=False)
        .alias("Transfer_Capital_Value")
    )

    total_capital_deployed = float(df_master["Location_Capital_Deployed"].sum() or 0.0)
    sum_location_cogs = float(df_master["Location_Annual_COGS"].sum() or 0.0)
    estimated_turnover_ratio = (
        (sum_location_cogs / total_capital_deployed) if total_capital_deployed > 0 else 0.0
    )

    total_draft_po_amount = compute_rolling_po_exposure_total(df_po)
    cash_collision = total_draft_po_amount > float(weekly_otb_limit)

    df_dead = df_master.filter(non_performing)
    dead_val = float(df_dead["Location_Capital_Deployed"].sum() or 0.0)

    df_loss = df_master.filter(gross_profit_inversion)
    df_integrity = df_master.filter(cost_basis_integrity)

    df_neg = df_master.filter(pl.col("Quantity_On_Hand") < 0)
    neg_item_count = int(df_neg["Item_Identifier"].n_unique() or 0)

    integrity_qty = int(df_integrity["Quantity_On_Hand"].sum() or 0)
    integrity_item_count = int(df_integrity["Item_Identifier"].n_unique() or 0)

    effective_transfer_threshold = max(float(min_transfer_value_threshold or 0.0), 0.0)
    df_transfer = df_master.filter(
        pl.col("Inter_Branch_Misallocation")
        & (pl.col("Transfer_Capital_Value") >= pl.lit(effective_transfer_threshold, dtype=pl.Float64))
        & (pl.col("Transfer_Qty") > 0.0)
    )
    transfer_item_count = (
        int(df_transfer["Item_Identifier"].n_unique()) if not df_transfer.is_empty() else 0
    )

    df_cost_alert = df_master.filter(
        (pl.col("Interbranch_Cost_Variance_Ratio") > 0.25) & (pl.col("Min_Unit_Cost") > 0)
    )
    cost_variance_item_count = int(df_cost_alert["Item_Identifier"].n_unique() or 0)

    disparity_export = (
        df_transfer.sort(["Item_Identifier", "Item_Location"])
        if not df_transfer.is_empty()
        else df_master.clear()
    )

    grade = (
        "A (Highly Efficient)"
        if estimated_turnover_ratio > 4.0
        else "B (Healthy)"
        if estimated_turnover_ratio > 2.5
        else "C (Lagging)"
        if estimated_turnover_ratio > 1.0
        else "D (Critical Review Advised)"
    )

    print_cfo_report(
        grade,
        total_capital_deployed,
        estimated_turnover_ratio,
        dead_val,
        df_dead,
        integrity_qty,
        integrity_item_count,
        df_integrity,
        transfer_item_count,
        disparity_export,
        neg_item_count,
        df_neg,
        df_loss,
        cost_variance_item_count,
        weekly_otb_limit,
        total_draft_po_amount,
        cash_collision,
    )

    buffers = generate_audit_csv_buffers(df_dead, df_integrity, df_neg, disparity_export, df_loss, df_cost_alert)

    pdf_buffer = generate_cfo_pdf(
        grade=grade,
        total_capital_deployed=total_capital_deployed,
        estimated_turnover_ratio=estimated_turnover_ratio,
        dead_val=dead_val,
        integrity_qty=integrity_qty,
        integrity_item_count=integrity_item_count,
        transfer_item_count=transfer_item_count,
        neg_item_count=neg_item_count,
        weekly_otb_limit=float(weekly_otb_limit),
        total_draft_po_amount=total_draft_po_amount,
        cash_collision=cash_collision,
        loss_item_count=int(df_loss["Item_Identifier"].n_unique() or 0),
        cost_variance_item_count=cost_variance_item_count,
    )
    buffers["Govnr_Executive_Brief.pdf"] = pdf_buffer.getvalue()

    return buffers
