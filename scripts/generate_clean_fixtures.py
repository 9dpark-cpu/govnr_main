#!/usr/bin/env python3
"""
Govnr QA fixture generator: deterministic CSVs for inventory_snapshot, annual_sold, po_activity.

All "random" draws use a fixed RNG seed (reproducible). Row counts and scenario overrides are exact.
"""
from __future__ import annotations

import os
import random
from datetime import date, timedelta
from pathlib import Path

import polars as pl

# ---------------------------------------------------------------------------
# Determinism anchor (stable fixture dates; change only if you need new dates)
# ---------------------------------------------------------------------------
RNG_SEED = 42
FIXTURE_ANCHOR = date(2026, 4, 14)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "netsuite_core" / "data" / "inbound"
INV_PATH = OUT_DIR / "inventory_snapshot.csv"
AS_PATH = OUT_DIR / "annual_sold.csv"
PO_PATH = OUT_DIR / "po_activity.csv"

WAREHOUSES = ("Warehouse_1", "Warehouse_2", "Warehouse_3", "Warehouse_4")
VENDORS = ("Bosch", "Denso", "ACDelco")


def _r2(x: float) -> float:
    return round(float(x), 2)


def _rand_date_in_last_days(rng: random.Random, anchor: date, days: int) -> date:
    """Uniform random calendar date in [anchor - (days-1), anchor] inclusive."""
    start = anchor - timedelta(days=days - 1)
    span = (anchor - start).days
    return start + timedelta(days=rng.randint(0, span))


def _build_unique_pairs_250() -> list[tuple[str, str]]:
    """
    Exactly 250 unique (Item_Identifier, Item_Location) pairs, 100 SKUs, all 4 warehouses used.
    """
    pairs: list[tuple[str, str]] = []
    for i in range(1, 51):
        sku = f"SKU-{i:03d}"
        for wh in ("Warehouse_1", "Warehouse_2", "Warehouse_3"):
            pairs.append((sku, wh))
    for i in range(51, 101):
        sku = f"SKU-{i:03d}"
        for wh in ("Warehouse_2", "Warehouse_4"):
            pairs.append((sku, wh))
    assert len(pairs) == 250
    assert len({p for p in pairs}) == 250
    assert {p[1] for p in pairs} == set(WAREHOUSES)
    return pairs


def _baseline_inventory_row(rng: random.Random, sku: str, wh: str, row_idx: int) -> dict:
    qa = _r2(rng.uniform(20.0, 200.0))
    qc = _r2(rng.uniform(0.0, 10.0))
    uac = _r2(rng.uniform(20.0, 80.0))
    ubp = _r2(uac * 1.4)
    tgt = _r2(qa * 1.5)
    vendor = rng.choice(VENDORS)
    mpn = f"MPN-{sku}"
    n = int(sku.split("-")[1])
    upc = f"123450000{n:03d}"
    last_dt = _rand_date_in_last_days(rng, FIXTURE_ANCHOR, 45)
    return {
        "Item_Identifier": sku,
        "Item_Description": f"Automotive Part {sku}",
        "Item_Type": "Inventory Item",
        "Quantity_Available": qa,
        "Quantity_Committed": qc,
        "Unit_Average_Cost": uac,
        "Unit_Base_Price": ubp,
        "Target_Stock_Level": tgt,
        "Preferred_Vendor": vendor,
        "Manufacturer_Part_Number": mpn,
        "UPC_Code": upc,
        "Manufacturer_Brand": vendor,
        "Item_Location": wh,
        "Last_Sales_Date": last_dt.isoformat(),
        "Replacement_Item_Identifier": "",
        "_row_idx": row_idx,
    }


def main() -> None:
    rng = random.Random(RNG_SEED)
    pairs = _build_unique_pairs_250()

    inv_rows: list[dict] = []
    for idx, (sku, wh) in enumerate(pairs):
        row = _baseline_inventory_row(rng, sku, wh, idx)
        inv_rows.append(row)

    # --- Scenario A: Gross profit inversion (exactly 5 rows) ---
    for i in range(5):
        inv_rows[i]["Unit_Average_Cost"] = 120.0
        inv_rows[i]["Unit_Base_Price"] = 100.0

    # --- Scenario B: Dead stock (exactly 10 rows) ---
    dead_indices = list(range(5, 15))
    for i in dead_indices:
        inv_rows[i]["Last_Sales_Date"] = "2024-01-01"
        inv_rows[i]["Quantity_Available"] = max(_r2(inv_rows[i]["Quantity_Available"]), 1.0)
        inv_rows[i]["Replacement_Item_Identifier"] = ""

    # --- Scenario C: Missing / $0 cost (exactly 3 rows) ---
    for i in range(15, 18):
        inv_rows[i]["Unit_Average_Cost"] = 0.0
        inv_rows[i]["Quantity_Available"] = max(_r2(inv_rows[i]["Quantity_Available"]), 1.0)

    # --- Scenario D: TEST-TRANSFER-SKU (2 inventory rows) ---
    # Note: With Target = 1.5 * QA, surplus/deficit logic never creates cross-branch transfers.
    # For a non-empty transfer manifest, Target_Stock_Level is set explicitly on these two rows only.
    uac_transfer = _r2(rng.uniform(20.0, 80.0))
    vendor_transfer = rng.choice(VENDORS)
    inv_rows.append(
        {
            "Item_Identifier": "TEST-TRANSFER-SKU",
            "Item_Description": "Automotive Part TEST-TRANSFER-SKU",
            "Item_Type": "Inventory Item",
            "Quantity_Available": 0.0,
            "Quantity_Committed": 0.0,
            "Unit_Average_Cost": uac_transfer,
            "Unit_Base_Price": _r2(uac_transfer * 1.4),
            "Target_Stock_Level": 400.0,
            "Preferred_Vendor": vendor_transfer,
            "Manufacturer_Part_Number": "MPN-TEST-TRANSFER-SKU",
            "UPC_Code": "123450000999",
            "Manufacturer_Brand": vendor_transfer,
            "Item_Location": "Warehouse_1",
            "Last_Sales_Date": _rand_date_in_last_days(rng, FIXTURE_ANCHOR, 45).isoformat(),
            "Replacement_Item_Identifier": "",
            "_row_idx": 250,
        }
    )

    inv_rows.append(
        {
            "Item_Identifier": "TEST-TRANSFER-SKU",
            "Item_Description": "Automotive Part TEST-TRANSFER-SKU",
            "Item_Type": "Inventory Item",
            "Quantity_Available": 1000.0,
            "Quantity_Committed": 0.0,
            "Unit_Average_Cost": uac_transfer,
            "Unit_Base_Price": _r2(uac_transfer * 1.4),
            "Target_Stock_Level": 200.0,
            "Preferred_Vendor": vendor_transfer,
            "Manufacturer_Part_Number": "MPN-TEST-TRANSFER-SKU",
            "UPC_Code": "123450000999",
            "Manufacturer_Brand": vendor_transfer,
            "Item_Location": "Warehouse_2",
            "Last_Sales_Date": _rand_date_in_last_days(rng, FIXTURE_ANCHOR, 45).isoformat(),
            "Replacement_Item_Identifier": "",
            "_row_idx": 251,
        }
    )

    # --- Scenario F: SKU-LEGACY / SKU-NEW ---
    uac_new = _r2(rng.uniform(20.0, 80.0))
    vendor_new = rng.choice(VENDORS)
    qa_new = _r2(rng.uniform(20.0, 200.0))
    qc_new = _r2(rng.uniform(0.0, 10.0))
    inv_rows.append(
        {
            "Item_Identifier": "SKU-LEGACY",
            "Item_Description": "Automotive Part SKU-LEGACY",
            "Item_Type": "Inventory Item",
            "Quantity_Available": 50.0,
            "Quantity_Committed": 0.0,
            "Unit_Average_Cost": uac_new,
            "Unit_Base_Price": _r2(uac_new * 1.4),
            "Target_Stock_Level": _r2(50.0 * 1.5),
            "Preferred_Vendor": vendor_new,
            "Manufacturer_Part_Number": "MPN-SKU-LEGACY",
            "UPC_Code": "123450000888",
            "Manufacturer_Brand": vendor_new,
            "Item_Location": "Warehouse_1",
            "Last_Sales_Date": _rand_date_in_last_days(rng, FIXTURE_ANCHOR, 45).isoformat(),
            "Replacement_Item_Identifier": "SKU-NEW",
            "_row_idx": 252,
        }
    )
    inv_rows.append(
        {
            "Item_Identifier": "SKU-NEW",
            "Item_Description": "Automotive Part SKU-NEW",
            "Item_Type": "Inventory Item",
            "Quantity_Available": qa_new,
            "Quantity_Committed": qc_new,
            "Unit_Average_Cost": uac_new,
            "Unit_Base_Price": _r2(uac_new * 1.4),
            "Target_Stock_Level": _r2(qa_new * 1.5),
            "Preferred_Vendor": vendor_new,
            "Manufacturer_Part_Number": "MPN-SKU-NEW",
            "UPC_Code": "123450000887",
            "Manufacturer_Brand": vendor_new,
            "Item_Location": "Warehouse_1",
            "Last_Sales_Date": _rand_date_in_last_days(rng, FIXTURE_ANCHOR, 45).isoformat(),
            "Replacement_Item_Identifier": "",
            "_row_idx": 253,
        }
    )

    # Round floats on all inventory rows
    for r in inv_rows:
        for k in (
            "Quantity_Available",
            "Quantity_Committed",
            "Unit_Average_Cost",
            "Unit_Base_Price",
            "Target_Stock_Level",
        ):
            if k in r and r[k] is not None:
                r[k] = _r2(r[k])

    # --- annual_sold: one row per (Item_Identifier, Item_Location) from inventory ---
    annual_rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for r in inv_rows:
        key = (r["Item_Identifier"], r["Item_Location"])
        if key in seen:
            raise RuntimeError(f"Duplicate inventory pair: {key}")
        seen.add(key)

    for r in inv_rows:
        sku, loc = r["Item_Identifier"], r["Item_Location"]
        if (sku, loc) == ("TEST-TRANSFER-SKU", "Warehouse_1"):
            annual_sold = 500.0
        elif (sku, loc) == ("TEST-TRANSFER-SKU", "Warehouse_2"):
            annual_sold = 0.0
        elif r.get("_row_idx") in dead_indices:
            annual_sold = 0.0
        else:
            annual_sold = _r2(rng.uniform(100.0, 500.0))
        annual_rows.append(
            {
                "Item_Identifier": sku,
                "Item_Location": loc,
                "Annual_Sold": annual_sold,
            }
        )

    # --- po_activity: 30 baseline + Scenario E + Scenario F ---
    po_rows: list[dict] = []

    def _inv_lookup_cost_vendor(item_id: str) -> tuple[float, str]:
        for row in inv_rows:
            if row["Item_Identifier"] == item_id:
                return float(row["Unit_Average_Cost"]), str(row["Preferred_Vendor"])
        raise KeyError(item_id)

    for n in range(1, 31):
        po_num = f"PO-{n:04d}"
        d_created = _rand_date_in_last_days(rng, FIXTURE_ANCHOR, 7).isoformat()
        inv_pick = inv_rows[(n - 1) % len(inv_rows)]
        item_id = inv_pick["Item_Identifier"]
        uac, vendor = _inv_lookup_cost_vendor(item_id)
        qty = _r2(rng.uniform(10.0, 50.0))
        total = _r2(qty * uac)
        if total >= 5000.0:
            qty = _r2(4999.99 / max(uac, 0.01))
            total = _r2(qty * uac)
        po_rows.append(
            {
                "PO_Number": po_num,
                "Date_Created": d_created,
                "Order_Status": "Pending Receipt",
                "Purchase_Order_Vendor": vendor,
                "Item_Identifier": item_id,
                "Quantity_Ordered": qty,
                "Order_Total_Amount": total,
            }
        )

    # Scenario E: cash collision
    _, e_vendor = _inv_lookup_cost_vendor(inv_rows[30]["Item_Identifier"])
    po_rows.append(
        {
            "PO_Number": "PO-CASH-COLLISION",
            "Date_Created": _rand_date_in_last_days(rng, FIXTURE_ANCHOR, 7).isoformat(),
            "Order_Status": "Pending Receipt",
            "Purchase_Order_Vendor": e_vendor,
            "Item_Identifier": inv_rows[30]["Item_Identifier"],
            "Quantity_Ordered": 1.0,
            "Order_Total_Amount": 150000.0,
        }
    )

    # Scenario F: Draft PO for SKU-NEW
    uac_new_po, vendor_new_po = _inv_lookup_cost_vendor("SKU-NEW")
    po_rows.append(
        {
            "PO_Number": "PO-DRAFT-REPLACEMENT",
            "Date_Created": _rand_date_in_last_days(rng, FIXTURE_ANCHOR, 7).isoformat(),
            "Order_Status": "Draft",
            "Purchase_Order_Vendor": vendor_new_po,
            "Item_Identifier": "SKU-NEW",
            "Quantity_Ordered": 100.0,
            "Order_Total_Amount": _r2(100.0 * uac_new_po),
        }
    )

    assert len(po_rows) == 32

    # Strip helper column before write
    for r in inv_rows:
        r.pop("_row_idx", None)

    inv_cols = [
        "Item_Identifier",
        "Item_Description",
        "Item_Type",
        "Quantity_Available",
        "Quantity_Committed",
        "Unit_Average_Cost",
        "Unit_Base_Price",
        "Target_Stock_Level",
        "Preferred_Vendor",
        "Manufacturer_Part_Number",
        "UPC_Code",
        "Manufacturer_Brand",
        "Item_Location",
        "Last_Sales_Date",
        "Replacement_Item_Identifier",
    ]
    df_inv = pl.DataFrame(inv_rows).select(inv_cols)
    df_as = pl.DataFrame(annual_rows)
    df_po = pl.DataFrame(po_rows)

    assert len(df_inv) == 254
    assert len(df_as) == 254
    assert df_inv.select(["Item_Identifier", "Item_Location"]).is_duplicated().sum() == 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df_inv.write_csv(INV_PATH)
    df_as.write_csv(AS_PATH)
    df_po.write_csv(PO_PATH)

    print(f"[fixtures] Wrote inventory ({len(df_inv)} rows): {INV_PATH}")
    print(f"[fixtures] Wrote annual_sold ({len(df_as)} rows): {AS_PATH}")
    print(f"[fixtures] Wrote po_activity ({len(df_po)} rows): {PO_PATH}")
    print()
    print("Reminder: add netsuite_core/data/inbound/*.csv to your .gitignore if these files must not be committed.")


if __name__ == "__main__":
    main()
