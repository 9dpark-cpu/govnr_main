import os
import sys
from datetime import date

import polars as pl

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
for _p in (BASE_DIR, REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import auth

from engine.erp_config_loader import fetch_ingestion_config, resolve_weekly_otb_limit
from engine.financial_math import calculate_financial_health
from engine.polars_list_compat import coerce_scalar_string_columns
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


def _persist_audit_csvs_to_outbound(buffers: dict) -> None:
    """Ensure outbound exists on ephemeral hosts (e.g. DO App Platform) and write CSV hit-lists to disk."""
    outbound_dir = os.path.join(REPO_ROOT, "netsuite_core", "data", "outbound")
    os.makedirs(outbound_dir, exist_ok=True)
    for fname, payload in buffers.items():
        if not isinstance(fname, str) or not fname.lower().endswith(".csv"):
            continue
        if not isinstance(payload, (bytes, bytearray)):
            continue
        out_path = os.path.join(outbound_dir, fname)
        with open(out_path, "wb") as fp:
            fp.write(payload)


_STRING_COLS_FOR_SNAPSHOT = (
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


def _strict_location_first_db_snapshot(df_inv: pl.DataFrame) -> dict[str, float]:
    """
    Location-first KPIs for DB persistence, aligned with the certified engine's
    ``calculate_financial_health`` terminal/PDF path (same df_master / non_performing definition;
    ``financial_math.py`` is not modified—logic is mirrored here in lockstep).
    """
    if df_inv.is_empty():
        return {
            "total_capital_deployed": 0.0,
            "dead_stock_val": 0.0,
            "inventory_turnover": 0.0,
            "gross_margin_pct": 0.0,
        }

    current_date = date.today()
    df_master = coerce_scalar_string_columns(df_inv, _STRING_COLS_FOR_SNAPSHOT).with_columns([
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

    total_capital_deployed = float(df_master["Location_Capital_Deployed"].sum() or 0.0)
    sum_location_cogs = float(df_master["Location_Annual_COGS"].sum() or 0.0)
    estimated_turnover_ratio = (
        (sum_location_cogs / total_capital_deployed) if total_capital_deployed > 0 else 0.0
    )

    df_dead = df_master.filter(non_performing)
    dead_val = float(df_dead["Location_Capital_Deployed"].sum() or 0.0)

    sum_location_revenue = float(
        (df_master["Annual_Sold"].fill_null(0.0) * df_master["Unit_Base_Price"].fill_null(0.0)).sum() or 0.0
    )
    gross_margin_pct = (
        ((sum_location_revenue - sum_location_cogs) / sum_location_revenue * 100.0)
        if sum_location_revenue > 0
        else 0.0
    )

    return {
        "total_capital_deployed": float(total_capital_deployed),
        "dead_stock_val": float(dead_val),
        "inventory_turnover": float(estimated_turnover_ratio),
        "gross_margin_pct": float(gross_margin_pct),
    }


def run_daily_governor(
    client_uuid: str,
    weekly_otb_limit: float | None = None,
    run_tier: str | None = None,
):
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
    _persist_audit_csvs_to_outbound(generated_buffers)

    df_inv_health = enrich_inventory_health(df_inv_raw)
    df_po_alerts = audit_purchase_orders(df_inv_health, df_po_raw)

    df_advanced = calculate_abc_and_gmroi(df_inv_health)
    df_manifest = generate_transfer_manifest(df_advanced)
    tax_writeoff = generate_tax_mitigation_plan(df_inv_health)

    financials = calculate_working_capital(df_po_raw, weekly_otb_limit=float(wlim))

    engine_db_snapshot = _strict_location_first_db_snapshot(df_inv_raw)
    draft_roll = float(financials["Total_Draft_PO_Amount"])
    weekly_lim = float(financials["Weekly_OTB_Limit"])
    dead_stock_strict = float(engine_db_snapshot["dead_stock_val"])
    metrics = {
        **engine_db_snapshot,
        "dead_stock_value": dead_stock_strict,
        "rolling_po_exposure": draft_roll,
        "rolling_po_total": draft_roll,
        "weekly_otb_limit": weekly_lim,
    }
    tier = run_tier if run_tier is not None else "diagnostic"
    metrics_save_error: str | None = None
    try:
        auth.save_cfo_metrics(
            client_uuid,
            tier,
            float(metrics["total_capital_deployed"]),
            float(metrics["inventory_turnover"]),
            float(metrics["gross_margin_pct"]),
            "Tier A",
            dead_stock_value=float(metrics["dead_stock_value"]),
            rolling_po_exposure=float(metrics["rolling_po_total"]),
            weekly_otb_ceiling=float(metrics["weekly_otb_limit"]),
        )
    except Exception as e:
        metrics_save_error = str(e)
        print(f"[Govnr] Failed to persist CFO metrics to vault: {e}")

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

    return {
        "buffers": generated_buffers,
        "metrics": metrics,
        "metrics_save_error": metrics_save_error,
    }


if __name__ == "__main__":
    out = run_daily_governor("client_test_001", weekly_otb_limit=100_000.0)
    print("Pipeline result keys:", None if out is None else list(out.keys()))
