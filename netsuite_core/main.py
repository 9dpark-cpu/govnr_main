import os
import sys

import polars as pl

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from engine.erp_config_loader import fetch_ingestion_config, resolve_weekly_otb_limit
from engine.financial_math import calculate_financial_health
from engine.govnr_exceptions import MissingFileError, SchemaDriftError
from engine.ip_harvester import harvest_proprietary_metrics
from engine.logic_advanced_erp import (
    calculate_abc_and_gmroi,
    generate_transfer_manifest,
    generate_tax_mitigation_plan,
)
from engine.logic_cash import calculate_working_capital
from engine.logic_governor import audit_purchase_orders, enrich_inventory_health
from engine.netsuite_parser import ingest_annual_sold, ingest_inventory_snapshot, ingest_po_activity


def _compute_cfo_metrics(df: pl.DataFrame) -> dict[str, float] | None:
    """Dashboard snapshot metrics from merged inventory + Annual_Sold (post-join, pre-enrich)."""
    if df.is_empty():
        return None
    qoh = pl.max_horizontal(pl.col("Quantity_Available") + pl.col("Quantity_Committed"), 0.0)
    annual = pl.col("Annual_Sold").fill_null(0.0)
    calc = df.with_columns([
        (qoh * pl.col("Unit_Average_Cost").fill_null(0.0)).alias("Cap_Deployed"),
        (annual * pl.col("Unit_Average_Cost").fill_null(0.0)).alias("COGS"),
        (annual * pl.col("Unit_Base_Price").fill_null(0.0)).alias("Revenue"),
    ])
    total_cap = float(calc["Cap_Deployed"].sum() or 0.0)
    dead_val = float(
        calc.filter(pl.col("Annual_Sold").fill_null(0.0) == 0.0)["Cap_Deployed"].sum() or 0.0
    )
    total_cogs = float(calc["COGS"].sum() or 0.0)
    total_rev = float(calc["Revenue"].sum() or 0.0)
    turnover = total_cogs / total_cap if total_cap > 0 else 0.0
    margin = ((total_rev - total_cogs) / total_rev * 100.0) if total_rev > 0 else 0.0
    return {
        "total_capital_deployed": total_cap,
        "dead_stock_val": dead_val,
        "inventory_turnover": turnover,
        "gross_margin_pct": margin,
    }


def run_daily_governor(client_uuid: str, weekly_otb_limit: float | None = None):
    print(f"[{client_uuid}] Initializing Govnr Pipeline...\n")

    cfg = fetch_ingestion_config(client_uuid)
    if weekly_otb_limit is not None:
        wlim, used_fallback = resolve_weekly_otb_limit(weekly_otb_limit)
    else:
        wlim, used_fallback = resolve_weekly_otb_limit(cfg.get("weekly_otb_limit"))
    if used_fallback:
        print(
            "[Govnr] Weekly_OTB_Limit missing or invalid; using default 100000.00. "
            "Update ERP Integration Settings to set your CFO-approved ceiling."
        )

    try:
        df_inv_raw = ingest_inventory_snapshot(client_uuid, erp_config=cfg)
        df_as = ingest_annual_sold(client_uuid, erp_config=cfg)
        df_po_raw = ingest_po_activity(client_uuid, erp_config=cfg)
    except MissingFileError as e:
        print(str(e))
        return None
    except SchemaDriftError as e:
        print(str(e))
        return None

    df_inv_raw = df_inv_raw.join(df_as, on=["Item_Identifier", "Item_Location"], how="left")
    df_inv_raw = df_inv_raw.with_columns(pl.col("Annual_Sold").fill_null(0.0))

    generated_buffers = calculate_financial_health(
        df_inv_raw,
        df_po_raw,
        weekly_otb_limit=float(wlim),
    )

    df_inv_health = enrich_inventory_health(df_inv_raw)
    df_po_alerts = audit_purchase_orders(df_inv_health, df_po_raw)

    df_advanced = calculate_abc_and_gmroi(df_inv_health)
    df_manifest = generate_transfer_manifest(df_advanced)
    tax_writeoff = generate_tax_mitigation_plan(df_inv_health)

    financials = calculate_working_capital(df_po_raw, weekly_otb_limit=float(wlim))

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

    metrics = _compute_cfo_metrics(df_inv_raw)
    return {"buffers": generated_buffers, "metrics": metrics}


if __name__ == "__main__":
    out = run_daily_governor("client_test_001", weekly_otb_limit=100_000.0)
    print("Pipeline result keys:", None if out is None else list(out.keys()))
