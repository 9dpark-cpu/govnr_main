import os
import sys
import polars as pl
from datetime import datetime

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from engine.polars_list_compat import flatten_list_columns_to_utf8_for_csv, preview_cell


def generate_audit_csv_buffers(
    df_dead: pl.DataFrame,
    df_integrity: pl.DataFrame,
    df_neg: pl.DataFrame,
    df_transfer: pl.DataFrame,
    df_loss: pl.DataFrame,
    df_cost_variance: pl.DataFrame,
) -> dict:
    """
    Generates in-memory CSV buffers instead of writing to ephemeral cloud disks.
    Returns a dictionary of filenames mapped to their encoded byte strings.
    """
    buffers = {}

    if not df_integrity.is_empty() or not df_loss.is_empty():
        parts = []
        if not df_integrity.is_empty():
            parts.append(df_integrity.with_columns(pl.lit("Missing / $0 Cost").alias("Alert_Type")))
        if not df_loss.is_empty():
            parts.append(df_loss.with_columns(pl.lit("Selling at a Loss").alias("Alert_Type")))
        df_combined_integrity = pl.concat(parts, how="diagonal")
        df_combined_integrity = flatten_list_columns_to_utf8_for_csv(df_combined_integrity)
        buffers["Report_2_cost_integrity.csv"] = df_combined_integrity.write_csv().encode("utf-8")

    datasets = [
        ("Report_1_Dead_stock.csv", df_dead),
        ("Report_3_negative_inventory.csv", df_neg),
    ]

    for filename, df in datasets:
        if not df.is_empty():
            df_out = flatten_list_columns_to_utf8_for_csv(df)
            buffers[filename] = df_out.write_csv().encode("utf-8")

    if not df_transfer.is_empty() or not df_cost_variance.is_empty():
        parts_r4 = []
        if not df_transfer.is_empty():
            parts_r4.append(
                df_transfer.with_columns(pl.lit("Warehouse Transfer Alert").alias("Alert_Type"))
            )
        if not df_cost_variance.is_empty():
            parts_r4.append(
                df_cost_variance.with_columns(pl.lit("Interbranch Cost Difference").alias("Alert_Type"))
            )
        df_r4 = pl.concat(parts_r4, how="diagonal")
        df_r4 = flatten_list_columns_to_utf8_for_csv(df_r4)
        buffers["Report_4_regional_disparity.csv"] = df_r4.write_csv().encode("utf-8")

    return buffers


def print_cfo_report(
    grade,
    total_inventory_value,
    turnover_ratio,
    dead_val,
    df_dead,
    integrity_qty,
    integrity_item_count,
    df_integrity,
    transfer_item_count,
    df_transfer_export,
    neg_item_count,
    df_neg,
    df_loss,
    cost_variance_item_count,
    weekly_otb_limit,
    total_draft_po_amount,
    cash_collision,
):
    print("\n" + "=" * 75)
    print("GOVNR DATA HEALTH SCAN RESULTS")
    print(f"   Target: Chief Financial Officer / Operations Director")
    print(f"   Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 75)

    print(f"\n FINANCIAL PERFORMANCE RATING: {grade}")
    print(f"   -> Total Capital Deployed: ${total_inventory_value:,.2f}")
    print(f"   -> Estimated Inventory Turnover (Velocity): {turnover_ratio:.2f}x (Industry Benchmark: 4.0x - 6.0x)")

    print("\n NON-PERFORMING CAPITAL (DEAD STOCK)")
    print(f"   -> Dead Stock Value: ${dead_val:,.2f}")

    print("\n COST ATTRIBUTION & MARGIN INTEGRITY")
    loss_row_count = 0 if df_loss.is_empty() else df_loss.height
    print(
        f"   -> {integrity_qty:,} physical units across {integrity_item_count} items lack deterministic cost guardrails ($0 or negative cost), "
        f"or selling-at-a-loss exposure on {loss_row_count} location rows."
    )

    print("\n WAREHOUSE TRANSFER & INTERBRANCH COST SIGNALS")
    if transfer_item_count > 0:
        print(
            f"   -> {transfer_item_count} items exhibit low days-supply in one location and excess (>180d) in another."
        )
    else:
        print("   -> No warehouse transfer misallocation pattern detected on current thresholds.")

    if cost_variance_item_count > 0:
        print(f"   -> {cost_variance_item_count} items show >25% average-cost spread across locations.")

    print("\n ROLLING OPEN-TO-BUY (7-DAY DRAFT PO EXPOSURE)")
    print(f"   -> Safe Spending Limit (Weekly OTB): ${weekly_otb_limit:,.2f}")
    print(f"   -> Draft PO Amount (rolling 7 days): ${total_draft_po_amount:,.2f}")
    if cash_collision:
        print("   -> CASH COLLISION WARNING: Draft POs exceed the Weekly OTB limit.")
    else:
        print("   -> Draft PO exposure is within the Weekly OTB limit.")

    print("\n NEGATIVE INVENTORY")
    if neg_item_count > 0:
        print(f"   -> {neg_item_count} distinct items show negative computed on-hand (data integrity).")
    else:
        print("   -> No negative on-hand rows after clamping.")

    print("\n  SAMPLE AUDIT: MULTI-POINT CAPITAL VERIFICATION")
    print("[High-exposure items identified across your logic rail]\n")

    if not df_dead.is_empty():
        row = df_dead.sort("Location_Capital_Deployed", descending=True).row(0, named=True)
        print("   1. [DEAD STOCK]")
        print(
            f"      - {row['Item_Identifier']} | {preview_cell(row.get('Item_Description'), 25)} | ${row['Location_Capital_Deployed']:,.2f} Non-Performing"
        )
        print("        (Reason: zero trailing velocity with no active replacement demand)\n")

    if not df_integrity.is_empty():
        df_int_exp = df_integrity.with_columns(
            (pl.col("Unit_Base_Price") * pl.col("Quantity_On_Hand")).alias("Retail_Exposure")
        )
        row = df_int_exp.sort("Retail_Exposure", descending=True).row(0, named=True)
        print("   2. [MISSING / $0 COST]")
        print(
            f"      - {row['Item_Identifier']} | {preview_cell(row.get('Item_Description'), 25)} | ${row['Retail_Exposure']:,.2f} Exposure"
        )
        print(f"        (Reason: unit average cost is ${row['Unit_Average_Cost']:,.2f})\n")

    if not df_transfer_export.is_empty():
        top_item = (
            df_transfer_export.group_by("Item_Identifier")
            .agg(pl.col("Location_Capital_Deployed").sum())
            .sort("Location_Capital_Deployed", descending=True)
            .row(0)[0]
        )
        item_rows = df_transfer_export.filter(pl.col("Item_Identifier") == top_item)
        title = item_rows[0, "Item_Description"]
        total_cap = item_rows["Location_Capital_Deployed"].sum()
        loc_0 = item_rows[0, "Item_Location"]
        qty_0 = item_rows[0, "Quantity_On_Hand"]
        loc_1 = item_rows[1, "Item_Location"] if item_rows.height > 1 else "Other Warehouse"
        qty_1 = item_rows[1, "Quantity_On_Hand"] if item_rows.height > 1 else 0

        print("   3. [WAREHOUSE TRANSFER ALERT]")
        print(f"      - {top_item} | {preview_cell(title, 25)} | ${total_cap:,.2f} Potentially Misallocated")
        print(
            f"        (Reason: {qty_0} units in {preview_cell(loc_0, 15)}; {qty_1} units in {preview_cell(loc_1, 15)}; days-supply imbalance)\n"
        )

    if not df_neg.is_empty():
        row = df_neg.sort("Quantity_On_Hand", descending=False).row(0, named=True)
        print("   4. [DATA INTEGRITY]")
        print(f"      - {row['Item_Identifier']} | {preview_cell(row.get('Item_Description'), 25)} | SYSTEM DRIFT")
        print(f"        (Reason: negative on-hand ({row['Quantity_On_Hand']} units) before clamp policy)\n")

    print("=" * 75)
    print(" GOVNR STRATEGIC GAP ANALYSIS")
    print("   The above data reflects your downstream ERP exposure.")
    print("   Rolling OTB compares trailing 7-day draft PO totals to your CFO-approved weekly ceiling.")
    print("=" * 75 + "\n")
    print("[Govnr Internal] Diagnostic scan complete. In-memory data buffers generated.")
