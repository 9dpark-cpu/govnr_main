import os
import polars as pl
from datetime import datetime
from config import BASE_DIR

def export_audit_csvs(df_dead: pl.DataFrame, df_integrity: pl.DataFrame, df_obsolete: pl.DataFrame, df_neg: pl.DataFrame, df_disparity_export: pl.DataFrame):
    df_dead.write_csv(os.path.join(BASE_DIR, 'outputs', 'reports' , "Report_1_Dead_stock.csv"))
    df_integrity.write_csv(os.path.join(BASE_DIR, 'outputs', 'reports' , "Report_2_cost_integrity.csv"))
    df_obsolete.write_csv(os.path.join(BASE_DIR, 'outputs' , 'reports' , "Report_3_obsolescence.csv"))
    df_neg.write_csv(os.path.join(BASE_DIR, 'outputs' , 'reports' , "Report_4_negative_inventory.csv"))
    df_disparity_export.write_csv(os.path.join(BASE_DIR, 'outputs' , 'reports' , "Report_5_regional_disparity.csv"))
    
def print_cfo_report(grade, total_inventory_value, turnover_ratio, dead_val, df_dead, 
                     integrity_qty, integrity_skus, df_integrity, 
                     obsolete_val, df_obsolete, 
                     disparity_sku_count, df_disparity_export, 
                     neg_skus, df_neg):
                     
    print("\n" + "="*75)
    print("GOVNR DATA HEALTH SCAN RESULTS")
    print(f"   Target: Chief Financial Officer / Operations Director")
    print(f"   Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*75)
    
    print(f"\n FINANCIAL PERFORMANCE RATING: {grade}")
    print(f"   -> Total Tracked Capital Deployed: ${total_inventory_value:,.2f}")
    print(f"   -> Estimated Inventory Turnover Ratio: {turnover_ratio:.2f}x (Industry Benchmark: 4.0x - 6.0x)")
    
    print("\n NON-PERFORMING CAPITAL SUMMARY")
    print(f"   -> Dead Stock Value (Qty > 0, 365d Sales = 0): ${dead_val:,.2f}")

    print("\n COST ATTRIBUTION INTEGRITY (Margin Blindness)")
    print(f"   -> {integrity_qty:,} physical units currently lack cost guardrails (Cost $0.00 or Cost > Retail Price).")
    print(f"   -> Impact: Your downstream profit reports for {integrity_skus} SKUs are mathematically invalid.")

    print("\n INVENTORY OBSOLESCENCE VALUATION")
    print(f"   -> Capital tied to vehicle applications > 15 years old (Pre-2011): ${obsolete_val:,.2f}")

    print("\n REGIONAL STOCK-TO-SALES DISPARITY")
    if disparity_sku_count > 0:
        print(f"   -> {disparity_sku_count} SKUs exhibit >1.0x turnover variance across multiple warehouses.")
        print(f"   -> Impact: Capital is stranded in low-velocity locations while high-velocity locations risk stock-outs.")
    else:
        print(f"   -> 0 SKUs currently exhibit dangerous regional variance. Inventory balancing is healthy.")

    # ---  NEW: MULTI-POINT CAPITAL VERIFICATION ---
    print("\n  SAMPLE AUDIT: MULTI-POINT CAPITAL VERIFICATION")
    print("[High Exposure SKUs identified across your Logic Rail]\n")

    # 1. DEAD CAPITAL
    if not df_dead.is_empty():
        row = df_dead.sort("Total_Capital_Deployed", descending=True).row(0, named=True)
        print("   1. [DEAD CAPITAL]")
        print(f"      - {row['SKU']} | {row['Title'][:25]} | ${row['Total_Capital_Deployed']:,.2f} Trapped")
        print("        (Reason: 0 Sales in 365 days; High procurement volume)\n")

    # 2. INVENTORY OBSOLESCENCE
    if not df_obsolete.is_empty():
        row = df_obsolete.sort("Total_Capital_Deployed", descending=True).row(0, named=True)
        print("   2.[INVENTORY OBSOLESCENCE]")
        print(f"      - {row['SKU']} | {row['Title'][:25]} | ${row['Total_Capital_Deployed']:,.2f} Risk")
        print(f"        (Reason: Vehicle Fitment >15 years old; Shrinking demand)\n")

    # 3. MARGIN BLINDNESS (Sort by total Retail Value exposed)
    if not df_integrity.is_empty():
        df_int_exp = df_integrity.with_columns((pl.col("Price") * pl.col("Qty_Available")).alias("Retail_Exposure"))
        row = df_int_exp.sort("Retail_Exposure", descending=True).row(0, named=True)
        print("   3. [MARGIN BLINDNESS]")
        print(f"      - {row['SKU']} | {row['Title'][:25]} | ${row['Retail_Exposure']:,.2f} Exposure")
        print(f"        (Reason: Unit Cost is ${row['Unit_Cost']:,.2f} in Shopify; Profitability unverified)\n")

    # 4. REGIONAL DISPARITY
    if not df_disparity_export.is_empty():
        # Get the top SKU causing the disparity
        top_sku = df_disparity_export.group_by("SKU").agg(pl.col("Total_Capital_Deployed").sum()).sort("Total_Capital_Deployed", descending=True).row(0)[0]
        sku_rows = df_disparity_export.filter(pl.col("SKU") == top_sku)
        title = sku_rows[0, "Title"]
        total_cap = sku_rows["Total_Capital_Deployed"].sum()
        loc_0 = sku_rows[0, "Location"]
        qty_0 = sku_rows[0, "Qty_Available"]
        loc_1 = sku_rows[1, "Location"] if sku_rows.height > 1 else "Other Warehouse"
        qty_1 = sku_rows[1, "Qty_Available"] if sku_rows.height > 1 else 0
        
        print("   4. [REGIONAL DISPARITY]")
        print(f"      - {top_sku} | {title[:25]} | ${total_cap:,.2f} Stranded")
        print(f"        (Reason: {qty_0} Units in {loc_0[:15]}; {qty_1} Units in {loc_1[:15]}; Capital inefficiently allocated)\n")

    # 5. DATA INTEGRITY
    if not df_neg.is_empty():
        # Sort ascending to get the lowest (most negative) quantity
        row = df_neg.sort("Qty_Available", descending=False).row(0, named=True)
        print("   5. [DATA INTEGRITY]")
        print(f"      - {row['SKU']} | {row['Title'][:25]} | SYSTEM DRIFT")
        print(f"        (Reason: Negative Inventory ({row['Qty_Available']} Units) detected; Likely Bridge/Sync failure)\n")

    print("="*75)
    print(" GOVNR STRATEGIC GAP ANALYSIS")
    print("   The above data reflects your downstream e-commerce (Shopify) exposure.")
    print("   To calculate your True Landed Cost, Open-to-Buy (OTB) Budget, and ")
    print("   prevent upcoming Cash Collisions, Govnr requires your NetSuite ERP Ledger.")
    print("\n   >> NEXT STEP: Book the $3,000 Calibration Audit to unlock the full")
    print("                 5-part CSV Recovery Roadmap and activate the Daily Governor.")
    print("="*75 + "\n")
    print(f"[Govnr Internal] Silent Export Complete. 5 CSV audit files saved to: {BASE_DIR}")