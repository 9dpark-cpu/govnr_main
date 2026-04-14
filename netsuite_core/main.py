import os
import sys

import polars as pl

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from engine.netsuite_parser import ingest_inventory_snapshot, ingest_po_activity
from engine.logic_governor import enrich_inventory_health, audit_purchase_orders
from engine.logic_cash import calculate_working_capital
from engine.logic_advanced_erp import (
    calculate_abc_and_gmroi,
    generate_transfer_manifest,
    generate_tax_mitigation_plan,
)
from engine.financial_math import calculate_financial_health
from engine.ip_harvester import harvest_proprietary_metrics


def run_daily_governor(client_uuid: str, weekly_otb_limit: float = 100_000.0):
    print(f"[{client_uuid}] Initializing Govnr Pipeline...\n")

    try:
        df_inv_raw = ingest_inventory_snapshot(client_uuid)
        df_po_raw = ingest_po_activity(client_uuid)
    except FileNotFoundError as e:
        print(e)
        return

    generated_buffers = calculate_financial_health(
        df_inv_raw,
        df_po_raw,
        weekly_otb_limit=float(weekly_otb_limit),
    )

    df_inv_health = enrich_inventory_health(df_inv_raw)
    df_po_alerts = audit_purchase_orders(df_inv_health, df_po_raw)

    df_advanced = calculate_abc_and_gmroi(df_inv_health)
    df_manifest = generate_transfer_manifest(df_advanced)
    tax_writeoff = generate_tax_mitigation_plan(df_inv_health)

    financials = calculate_working_capital(df_po_raw, weekly_otb_limit=float(weekly_otb_limit))

    print("\n" + "=" * 75)
    print("  GOVNR: DAILY STRATEGIC COMPLIANCE BRIEF")
    print("=" * 75)

    print("\n[ROLLING OTB & DRAFT PO EXPOSURE]")
    print(f" Weekly OTB Limit (CFO):       ${financials['Weekly_OTB_Limit']:,.2f}")
    print(f" Draft PO Total (7-day roll): ${financials['Total_Draft_PO_Amount']:,.2f}")
    print("-" * 50)
    print(f" {financials['Status']}")
    print(f" Action: {financials['Action_Brief']}")

    print("\n[PURCHASE ORDER INTERVENTION REQUIRED]")
    if not df_po_alerts.is_empty():
        df_action_needed = df_po_alerts.filter(pl.col("Governor_Action") != " APPROVED")
        if df_action_needed.height > 0:
            cols = ["PO_Number", "Item_Identifier", "Order_Total_Amount", "Governor_Action"]
            print(df_action_needed.select([c for c in cols if c in df_action_needed.columns]).write_csv())
            print("\n Recommendation: Resolve flagged lines before releasing cash.")
        else:
            print("  All pending PO lines passed automated governor checks.")
    else:
        print(" No pending purchase orders detected today.")

    print("\n[LOGISTICS & TAX DIRECTIVES]")
    if not df_manifest.is_empty():
        print(
            f"  Action Required: Execute {df_manifest.height} internal warehouse transfers to prevent unnecessary procurement."
        )
    print(f"  Tax Mitigation: Provide your CPA with the projected ${tax_writeoff:,.2f} write-off potential for Q4.")

    print("\n[ASSET PRODUCTIVITY & ABC CLASSIFICATION]")
    if not df_advanced.is_empty():
        abc_counts = (
            df_advanced.group_by("ABC_Class").agg(pl.col("Item_Identifier").n_unique().alias("Count")).sort("ABC_Class")
        )
        print("  Inventory Criticality Breakdown:")
        for row in abc_counts.iter_rows():
            print(f"    -> {row[0]}: {row[1]} items")

        valid_gmroi = df_advanced.filter(pl.col("GMROI") > 0)
        if not valid_gmroi.is_empty():
            avg_gmroi = valid_gmroi["GMROI"].mean()
            print(f"  Average Fleet GMROI: {avg_gmroi:.2f} (Gross Margin Return on Inventory)")

        print("\n[PROPRIETARY DATA VAULT]")
        harvest_proprietary_metrics(
            client_uuid=client_uuid,
            df_master=df_advanced,
            financials=financials,
            df_po_alerts=df_po_alerts,
            liquidity_source="ROLLING_OTB",
        )

    print("=" * 75 + "\n")

    return generated_buffers


if __name__ == "__main__":
    run_daily_governor("client_test_001", weekly_otb_limit=100_000.0)
