import os
import sys
from datetime import date, timedelta

import polars as pl

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def calculate_working_capital(
    df_pending_pos: pl.DataFrame,
    weekly_otb_limit: float = 100_000.00,
) -> dict:
    """
    Rolling 7-day draft PO exposure vs CFO-approved weekly OTB ceiling.
    All rows in the PO export are summed (client saved search is authoritative).
    """
    if df_pending_pos.is_empty():
        total_draft_po_amount = 0.0
    else:
        anchor_date = df_pending_pos.select(
            pl.col("Date_Created").cast(pl.Date, strict=False).max().alias("anchor_date")
        )["anchor_date"].item()
        if anchor_date is None:
            anchor_date = date.today()
        cutoff = anchor_date - timedelta(days=7)
        df_eff = df_pending_pos.with_columns(
            pl.col("Date_Created").cast(pl.Date, strict=False).fill_null(pl.lit(anchor_date)).alias("_eff_date")
        )
        total_draft_po_amount = float(
            df_eff.filter(pl.col("_eff_date") >= pl.lit(cutoff))["Order_Total_Amount"].sum()
        )

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
