import os
import sys
from datetime import date, timedelta

import polars as pl

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

_OTB_ROLLING_STATUSES = ("Pending Receipt", "Draft")


def compute_rolling_po_exposure_total(df_po: pl.DataFrame) -> float:
    """
    Single source of truth for trailing 7-day PO exposure used by OTB / cash collision logic.

    Anchor date is the maximum ``Date_Created`` in the PO export (not system clock).
    Sums ``Order_Total_Amount`` for rows with status Pending Receipt or Draft in
    ``[anchor - 7 days, anchor]`` (inclusive on the lower bound via ``>= cutoff``).
    """
    if df_po.is_empty():
        return 0.0

    df_eff = df_po.with_columns(
        pl.col("Date_Created")
        .cast(pl.Utf8)
        .str.slice(0, 10)
        .str.strptime(pl.Date, "%Y-%m-%d", strict=False)
        .alias("_eff_date")
    )
    anchor_scalar = df_eff.select(pl.col("_eff_date").max()).to_series()[0]
    if anchor_scalar is None:
        anchor_date = date.today()
    else:
        anchor_date = anchor_scalar

    cutoff = anchor_date - timedelta(days=7)
    df_window = df_eff.filter(
        pl.col("_eff_date").is_not_null()
        & (pl.col("_eff_date") >= pl.lit(cutoff))
        & pl.col("Order_Status").is_in(list(_OTB_ROLLING_STATUSES))
    )
    return float(df_window["Order_Total_Amount"].sum())


def rolling_po_orders_in_window(df_po: pl.DataFrame) -> pl.DataFrame:
    """
    One row per PO_Number in the same 7-day rolling window as ``compute_rolling_po_exposure_total``.
    ``Order_Total_Amount`` is de-duplicated per PO (line exports often repeat the PO header amount).
    """
    if df_po.is_empty():
        return pl.DataFrame(
            schema={
                "PO_Number": pl.String,
                "Order_Total_Amount": pl.Float64,
                "Purchase_Order_Vendor": pl.String,
                "Order_Status": pl.String,
            }
        )

    df_eff = df_po.with_columns(
        pl.col("Date_Created")
        .cast(pl.Utf8)
        .str.slice(0, 10)
        .str.strptime(pl.Date, "%Y-%m-%d", strict=False)
        .alias("_eff_date")
    )
    anchor_scalar = df_eff.select(pl.col("_eff_date").max()).to_series()[0]
    if anchor_scalar is None:
        anchor_date = date.today()
    else:
        anchor_date = anchor_scalar

    cutoff = anchor_date - timedelta(days=7)
    df_window = df_eff.filter(
        pl.col("_eff_date").is_not_null()
        & (pl.col("_eff_date") >= pl.lit(cutoff))
        & pl.col("Order_Status").is_in(list(_OTB_ROLLING_STATUSES))
    )
    if df_window.is_empty():
        return pl.DataFrame(
            schema={
                "PO_Number": pl.String,
                "Order_Total_Amount": pl.Float64,
                "Purchase_Order_Vendor": pl.String,
                "Order_Status": pl.String,
            }
        )

    return (
        df_window.group_by("PO_Number")
        .agg(
            pl.col("Order_Total_Amount").max().alias("Order_Total_Amount"),
            pl.col("Purchase_Order_Vendor").first().alias("Purchase_Order_Vendor"),
            pl.col("Order_Status").first().alias("Order_Status"),
        )
        .sort("Order_Total_Amount", descending=True)
    )


def calculate_working_capital(
    df_pending_pos: pl.DataFrame,
    weekly_otb_limit: float = 100_000.00,
) -> dict:
    """
    Rolling 7-day draft PO exposure vs CFO-approved weekly OTB ceiling.
    All rows in the PO export are summed (client saved search is authoritative).
    """
    total_draft_po_amount = compute_rolling_po_exposure_total(df_pending_pos)

    liquidity_status = " BUDGET COMPLIANT"
    action_brief = ""

    if total_draft_po_amount > float(weekly_otb_limit):
        liquidity_status = " CASH COLLISION WARNING"
        shortfall = total_draft_po_amount - float(weekly_otb_limit)
        action_brief = (
            f"Draft PO lines over the last 7 days total ${total_draft_po_amount:,.2f}, "
            f"exceeding your Weekly OTB limit of ${weekly_otb_limit:,.2f}. "
            f"Reduce pending exposure by approximately ${shortfall:,.2f} to restore compliance."
        )
    else:
        action_brief = (
            f"Rolling 7-day draft PO total (${total_draft_po_amount:,.2f}) is within the "
            f"Weekly OTB limit (${weekly_otb_limit:,.2f})."
        )

    return {
        "Weekly_OTB_Limit": float(weekly_otb_limit),
        "Total_Draft_PO_Amount": total_draft_po_amount,
        "Cash_Collision_Warning": total_draft_po_amount > float(weekly_otb_limit),
        "Status": liquidity_status,
        "Action_Brief": action_brief,
    }


def print_cfo_morning_brief(financials: dict):
    print("\n" + "=" * 60)
    print("  GOVNR: DAILY STRATEGIC COMPLIANCE BRIEF")
    print("=" * 60)
    print("\n[Rolling OTB vs Draft PO Exposure]")
    print(f"-> Weekly OTB Limit:              ${financials['Weekly_OTB_Limit']:,.2f}")
    print(f"-> Total Draft POs (7-day roll): ${financials['Total_Draft_PO_Amount']:,.2f}")
    print("-" * 60)
    print("\n" + financials["Status"])
    print(f"Action: {financials['Action_Brief']}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    from engine.netsuite_parser import ingest_po_activity

    df_po_raw = ingest_po_activity(client_uuid="client_test_001")

    financials_test = calculate_working_capital(df_pending_pos=df_po_raw, weekly_otb_limit=100_000.00)

    print_cfo_morning_brief(financials_test)
