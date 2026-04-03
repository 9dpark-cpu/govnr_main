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
from engine.logic_advanced_erp import calculate_abc_and_gmroi, generate_transfer_manifest, generate_tax_mitigation_plan
from engine.financial_math import calculate_financial_health
from engine.ip_harvester import harvest_proprietary_metrics  

def run_daily_governor(client_uuid: str, proxy_cash: float, proxy_ap: float):
    print(f"[{client_uuid}] Initializing Govnr Pipeline...\n")

    # 1. INGESTION PHASE
    try:
        df_inv_raw = ingest_inventory_snapshot(client_uuid)
        df_po_raw = ingest_po_activity(client_uuid)
    except FileNotFoundError as e:
        print(e)
        return

    # 2. FINANCIAL MATH PHASE (Data Health Scan)
    generated_buffers = calculate_financial_health(df_inv_raw)

    # 3. INVENTORY HEALTH & GOVERNOR PHASE (Toxic Stock Checks)
    df_inv_health = enrich_inventory_health(df_inv_raw)
    df_po_alerts = audit_purchase_orders(df_inv_health, df_po_raw)

    # 4. ADVANCED ERP PHASE (ABC, GMROI, Transfers, Tax)
    df_advanced = calculate_abc_and_gmroi(df_inv_health)
    df_manifest = generate_transfer_manifest(df_advanced)
    tax_writeoff = generate_tax_mitigation_plan(df_inv_health)

    # 5. CASH LOGIC PHASE (Open-To-Buy limits)
    financials = calculate_working_capital(df_po_raw, proxy_cash, proxy_ap)

    # --- UNIFIED TERMINAL REPORT ---
    print("\n" + "="*75)
    print("  GOVNR: DAILY STRATEGIC COMPLIANCE BRIEF")
    print("="*75)
    
    # Section A: Cash Position
    print("\n[LIQUIDITY CONSTRAINT & OTB LIMITS]")
    print(f" Bank Balance (True Cash):    ${financials['Cash_Position']:,.2f}")
    print(f" Accounts Payable (1-15d):    ${financials['Upcoming_AP']:,.2f}")
    print(f" Operating Reserve (10%):     ${financials['Operating_Reserve']:,.2f}")
    print("-" * 50)
    print(f"  SAFE SPENDING LIMIT:        ${financials['Safe_Spending_Limit_OTB']:,.2f}")
    print(f"  PENDING DRAFT POs:          ${financials['Total_PO_Exposure']:,.2f}")
    print(f"\n {financials['Status']}")
    print(f" Action: {financials['Action_Brief']}")
    
    # Section B: PO Interventions
    print("\n[PURCHASE ORDER INTERVENTION REQUIRED]")
    if not df_po_alerts.is_empty():
        df_action_needed = df_po_alerts.filter(pl.col("Governor_Action") != " APPROVED")
        if df_action_needed.height > 0:
            with pl.Config(tbl_rows=10, tbl_cols=6, fmt_str_lengths=40):
                print(df_action_needed.select(["PO_Number", "SKU", "Total_Amount", "Governor_Action"]))
            print("\n Recommendation: Cancel the flagged Toxic Stock lines to instantly clear your Cash Collision.")
        else:
            print("  All pending POs represent healthy inventory.")
    else:
        print(" No pending Purchase Orders detected today.")

    # Section C: Advanced Operations
    print("\n[LOGISTICS & TAX DIRECTIVES]")
    if not df_manifest.is_empty():
        print(f"  Action Required: Execute {df_manifest.height} internal warehouse transfers to prevent unnecessary procurement.")
    print(f"  Tax Mitigation: Provide your CPA with the projected ${tax_writeoff:,.2f} write-off potential for Q4.")

    # Section D: Asset Productivity (ABC & GMROI)
    print("\n[ASSET PRODUCTIVITY & ABC CLASSIFICATION]")
    if not df_advanced.is_empty():
        # Count SKUs by ABC Class
        abc_counts = df_advanced.group_by("ABC_Class").agg(pl.col("SKU").n_unique().alias("Count")).sort("ABC_Class")
        print("  Inventory Criticality Breakdown:")
        for row in abc_counts.iter_rows():
            print(f"    -> {row[0]}: {row[1]} SKUs")
        
        # Calculate Average GMROI for the fleet (excluding zeros/negatives)
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
            liquidity_source="MANUAL_STATIC" 
        )
    return generated_buffers

    print("="*75 + "\n")

if __name__ == "__main__":
    # 1. Static API-Free Proxies for Local Testing
    proxy_cash = 35000.0
    proxy_ap = 28000.0
    
    # 2. Inject static data into the Governor
    run_daily_governor("client_test_001", proxy_cash=proxy_cash, proxy_ap=proxy_ap)