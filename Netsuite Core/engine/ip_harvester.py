import os
import sys
import polars as pl
import hashlib
from datetime import date

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

LEDGER_PATH = os.path.join(DATA_DIR, "master_benchmark_ledger.csv")
RADAR_PATH = os.path.join(DATA_DIR, "demand_radar_vault.csv")

# ==========================================
# 1. METADATA & NLP EXTRACTION DICTIONARIES
# ==========================================
CLIENT_META = {
    "client_test_001": {"Region": "South", "Revenue_Tier": "$2M-$5M"}
}

CATEGORY_MAP = {
    "alt": "Alternators",
    "alternator": "Alternators",
    "brk": "Brakes & Rotors",
    "brake": "Brakes & Rotors",
    "pad": "Brakes & Rotors",
    "spk": "Ignition",
    "spark": "Ignition",
    "str": "Suspension",
    "strut": "Suspension",
    "flt": "Filters",
    "filter": "Filters"
}

def extract_part_category(title: str) -> str:
    if not title: 
        return "Other/Misc"
    title_lower = title.lower()
    for keyword, clean_category in CATEGORY_MAP.items():
        if keyword in title_lower:
            return clean_category
    return "Other/Misc"

# ==========================================
# 2. THE IDEMPOTENT UPSERT ENGINE
# ==========================================

def _upsert_csv(df_new: pl.DataFrame, path: str, unique_cols: list):
    """
    Prevents duplicates by appending new data to the bottom, 
    and then dropping older duplicates. Immune to Schema Evolution crashes.
    """
    if not os.path.exists(path):
        df_new.write_csv(path)
        return

    try:
        # Read existing file 
        df_existing = pl.read_csv(path, infer_schema_length=10000)
    except Exception:
        df_new.write_csv(path)
        return

    if df_existing.is_empty():
        df_new.write_csv(path)
        return

    # 1.  DATA TYPE ALIGNMENT: Force old CSV to match the New Data's types EXACTLY
    # (This solves the Int32 vs Int64 crash before it can happen)
    cast_exprs =[]
    for col in df_new.columns:
        if col not in df_existing.columns:
            # If a column is completely missing in the old CSV, add it safely
            df_existing = df_existing.with_columns(pl.lit(None).cast(df_new.schema[col]).alias(col))
        else:
            # If it exists, queue it up to be perfectly cast to the exact right type
            cast_exprs.append(pl.col(col).cast(df_new.schema[col]))
            
    df_existing = df_existing.with_columns(cast_exprs)

    # 2. APPEND: Glue the new data to the bottom of the old data
    df_combined = pl.concat([df_existing, df_new], how="diagonal")

    # 3. DEDUPLICATE: Keep the 'last' (newest) row for any matching unique keys!
    df_final = df_combined.unique(subset=unique_cols, keep="last")

    # 4. Overwrite
    df_final.write_csv(path)

# ==========================================
# 3. THE MASTER HARVESTER LOGIC
# ==========================================

def harvest_proprietary_metrics(
    client_uuid: str, 
    df_master: pl.DataFrame, 
    financials: dict, 
    df_po_alerts: pl.DataFrame,
    liquidity_source: str = "MANUAL_STATIC"
):
    if df_master.is_empty():
        return

    # Extract Meta & Generate Hashes
    region = CLIENT_META.get(client_uuid, {}).get("Region", "Unknown")
    today_str = str(date.today()) 
    client_hash = hashlib.sha256(client_uuid.encode('utf-8')).hexdigest()[:12]

    # --- VAULT 1: FINANCIAL BENCHMARK ---
    total_capital = df_master["Capital_Deployed"].sum()
    
    if total_capital > 0:
        dead_stock_val = df_master.filter((pl.col("Annual_Velocity") == 0) & (pl.col("Capital_Deployed") > 0))["Capital_Deployed"].sum()
        obsolete_stock_val = df_master.filter((pl.col("Max_Fitment_Year") <= 2011) & (pl.col("Capital_Deployed") > 0))["Capital_Deployed"].sum()
        
        total_cogs = (df_master["Annual_Velocity"] * df_master["Unit_Cost"]).sum()
        total_revenue = (df_master["Annual_Velocity"] * df_master["Price"]).sum()
        
        # --- DYNAMIC REVENUE TIER CALCULATION ---
        if total_revenue < 2_000_000:
            revenue_tier = "< $2M"
        elif total_revenue < 5_000_000:
            revenue_tier = "$2M - $5M"
        elif total_revenue < 12_000_000:
            revenue_tier = "$5M - $12M"
        else:
            revenue_tier = "$12M+"
            
        turnover_ratio = total_cogs / total_capital
        gross_margin_pct = ((total_revenue - total_cogs) / total_revenue * 100) if total_revenue > 0 else 0.0
        
        valid_gmroi = df_master.filter(pl.col("GMROI") > 0)
        avg_gmroi = valid_gmroi["GMROI"].mean() if not valid_gmroi.is_empty() else 0.0
        
        cash_to_po_ratio = (financials['Total_PO_Exposure'] / financials['Cash_Position']) if financials['Cash_Position'] > 0 else 0.0
        
        capital_saved = 0.0
        if not df_po_alerts.is_empty():
            df_blocked = df_po_alerts.filter(pl.col("Governor_Action").str.contains("|"))
            capital_saved = df_blocked["Total_Amount"].sum()

        df_financial = pl.DataFrame({
            "Date": [today_str],
            "Client_Hash":[client_hash],             # FIXED: Completely Anonymized
            "Region": [region],
            "Revenue_Tier": [revenue_tier],           # FIXED: Dynamically Calculated
            "Location_Count": [df_master["Location"].n_unique()],
            "Vendor_Count": [df_master["Vendor"].n_unique()],
            "Total_SKU_Count": [df_master.height],
            "Total_Capital_Deployed": [round(total_capital, 2)],
            "Inventory_Turnover":[round(turnover_ratio, 2)],
            "Gross_Margin_Pct":[round(gross_margin_pct, 2)],
            "Avg_GMROI": [round(avg_gmroi, 2)],
            "Dead_Stock_Pct":[round((dead_stock_val / total_capital) * 100, 2)],
            "Obsolete_Stock_Pct":[round((obsolete_stock_val / total_capital) * 100, 2)],
            "Liquidity_Source":[liquidity_source],   # FIXED: Provenance Tagged
            "Cash_to_PO_Ratio": [round(cash_to_po_ratio, 4)], 
            "Capital_Saved_by_Govnr": [round(capital_saved, 2)] 
        })
        
        # Upsert Ledger (Matches on Date + Client_Hash)
        _upsert_csv(df_financial, LEDGER_PATH, unique_cols=["Date", "Client_Hash"])

    # --- VAULT 2: THE DEMAND RADAR ---
    df_radar = df_master.with_columns([
        pl.col("Title").map_elements(extract_part_category, return_dtype=pl.String).alias("Part_Category")
    ])

    df_radar_cleaned = df_radar.filter(
        (pl.col("Part_Category") != "Other/Misc") & 
        (pl.col("Max_Fitment_Year") != 9999) & 
        (pl.col("Capital_Deployed") > 0) 
    )

    if not df_radar_cleaned.is_empty():
        df_radar_aggregated = df_radar_cleaned.group_by(["Part_Category", "Max_Fitment_Year"]).agg([
            pl.col("Qty_On_Hand").sum().alias("Regional_Stock_Count"), #  CFO FIX: Total physical boxes on the shelf
            pl.col("Annual_Velocity").sum().alias("Regional_Velocity"),
            pl.col("Capital_Deployed").sum().alias("Regional_Capital_Deployed"), 
            (pl.col("Qty_Available") - pl.col("Target_Stock")).sum().alias("Regional_Surplus_Deficit") #  REMAINS AVAILABLE: You can't fulfill new orders with committed stock
        ]).with_columns([
            pl.lit(today_str).alias("Date"),
            pl.lit(client_hash).alias("Client_Hash"),
            pl.lit(region).alias("Region")
        ]).select([
            "Date", "Client_Hash", "Region", "Part_Category", "Max_Fitment_Year", 
            "Regional_Stock_Count", "Regional_Velocity", "Regional_Capital_Deployed",
            "Regional_Surplus_Deficit" # FIXED: Appended to the final select statement
        ])

        # Upsert Radar
        _upsert_csv(df_radar_aggregated, RADAR_PATH, unique_cols=["Date", "Client_Hash", "Part_Category", "Max_Fitment_Year"])
        
    print("[Govnr IP]  Financial Telemetry & Regional Demand Radar safely extracted (Idempotent Upsert).")