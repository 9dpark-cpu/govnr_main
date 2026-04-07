import os
import sys
import polars as pl
import hashlib
from datetime import date
import psycopg2
import toml
import uuid

# --- DEFENSIVE PATHING ---
ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
NETSUITE_DIR = os.path.dirname(ENGINE_DIR)
GOVNR_DIR = os.path.dirname(NETSUITE_DIR) # Root Govnr_Platform
SECRETS_PATH = os.path.join(GOVNR_DIR, ".streamlit", "secrets.toml")

from engine.fitment_rules import get_fitment_expr

CATEGORY_MAP = {
    "alt": "Alternators", "alternator": "Alternators",
    "brk": "Brakes & Rotors", "brake": "Brakes & Rotors", "pad": "Brakes & Rotors",
    "spk": "Ignition", "spark": "Ignition",
    "str": "Suspension", "strut": "Suspension",
    "flt": "Filters", "filter": "Filters"
}

def extract_part_category(title: str) -> str:
    if not title: return "Other/Misc"
    title_lower = title.lower()
    for keyword, clean_category in CATEGORY_MAP.items():
        if keyword in title_lower:
            return clean_category
    return "Other/Misc"

def _get_db_connection():
    """Securely connects to DigitalOcean using Cloud Environment Variables."""
    db_url = os.environ.get("DATABASE_URL")
    
    # Fallback for local Streamlit testing if env var isn't set
    if not db_url:
        if os.path.exists(SECRETS_PATH):
            with open(SECRETS_PATH, "r") as f:
                secrets = toml.load(f)
            db_url = secrets.get("DATABASE_URL")
            
    if not db_url:
        raise ValueError("DATABASE_URL environment variable is missing. Vault connection aborted.")
        
    return psycopg2.connect(db_url)

def is_valid_uuid(val):
    try:
        uuid.UUID(str(val))
        return True
    except ValueError:
        return False

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

    today_str = str(date.today()) 

    # --- VAULT 1: FINANCIAL BENCHMARK (DIGITALOCEAN DB) ---
    total_capital = df_master["Capital_Deployed"].sum()
    
    if total_capital > 0 and is_valid_uuid(client_uuid):
        
        # COVERT IP INJECTION: Apply the Regex exclusively for the backend vault
        df_ip_eval = df_master.with_columns(get_fitment_expr().alias("Max_Fitment_Year"))
        
        dead_stock_val = df_ip_eval.filter((pl.col("Annual_Velocity") == 0) & (pl.col("Capital_Deployed") > 0))["Capital_Deployed"].sum()
        obsolete_stock_val = df_ip_eval.filter((pl.col("Max_Fitment_Year") <= 2011) & (pl.col("Capital_Deployed") > 0))["Capital_Deployed"].sum()
        
        total_cogs = (df_ip_eval["Annual_Velocity"] * df_ip_eval["Unit_Cost"]).sum()
        total_revenue = (df_ip_eval["Annual_Velocity"] * df_ip_eval["Price"]).sum()
        
        turnover_ratio = total_cogs / total_capital if total_capital > 0 else 0.0
        gross_margin_pct = ((total_revenue - total_cogs) / total_revenue * 100) if total_revenue > 0 else 0.0
        
        valid_gmroi = df_ip_eval.filter(pl.col("GMROI") > 0) if "GMROI" in df_ip_eval.columns else pl.DataFrame()
        avg_gmroi = valid_gmroi["GMROI"].mean() if not valid_gmroi.is_empty() else 0.0
        
        cash_balance = financials.get('Cash_Position', 0.0)

        conn = None
        cursor = None
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()
            
            # --- VAULT 1: UPSERT Client Metrics ---
            upsert_query = """
                INSERT INTO client_metrics_history (
                    client_uuid, date_recorded, total_capital_deployed, inventory_turnover,
                    gross_margin_pct, avg_gmroi, dead_stock_val, obsolete_stock_val, cash_balance
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                ) ON CONFLICT (client_uuid, date_recorded) DO UPDATE SET
                    total_capital_deployed = EXCLUDED.total_capital_deployed,
                    inventory_turnover = EXCLUDED.inventory_turnover,
                    gross_margin_pct = EXCLUDED.gross_margin_pct,
                    avg_gmroi = EXCLUDED.avg_gmroi,
                    dead_stock_val = EXCLUDED.dead_stock_val,
                    obsolete_stock_val = EXCLUDED.obsolete_stock_val,
                    cash_balance = EXCLUDED.cash_balance;
            """
            cursor.execute(upsert_query, (
                client_uuid, today_str, float(total_capital), float(turnover_ratio), 
                float(gross_margin_pct), float(avg_gmroi), float(dead_stock_val), 
                float(obsolete_stock_val), float(cash_balance)
            ))
            
            # --- VAULT 2: THE DEMAND RADAR (PUSHED TO DIGITALOCEAN) ---
            df_radar = df_ip_eval.with_columns([
                pl.col("Title").map_elements(extract_part_category, return_dtype=pl.String).alias("Part_Category")
            ])

            df_radar_cleaned = df_radar.filter(
                (pl.col("Part_Category") != "Other/Misc") & 
                (pl.col("Max_Fitment_Year") != 9999) & 
                (pl.col("Capital_Deployed") > 0) 
            )

            if not df_radar_cleaned.is_empty():
                client_hash = hashlib.sha256(client_uuid.encode('utf-8')).hexdigest()[:12]
                
                df_radar_aggregated = df_radar_cleaned.group_by(["Part_Category", "Max_Fitment_Year"]).agg([
                    pl.col("Qty_On_Hand").sum().alias("Regional_Stock_Count"),
                    pl.col("Annual_Velocity").sum().alias("Regional_Velocity"),
                    pl.col("Capital_Deployed").sum().alias("Regional_Capital_Deployed"), 
                    (pl.col("Qty_Available") - pl.col("Target_Stock")).sum().alias("Regional_Surplus_Deficit") 
                ])
                
                radar_insert_query = """
                    INSERT INTO demand_radar_vault (
                        date_recorded, client_hash, region, part_category, max_fitment_year,
                        regional_stock_count, regional_velocity, regional_capital_deployed, regional_surplus_deficit
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    ) ON CONFLICT (date_recorded, client_hash, part_category, max_fitment_year) DO UPDATE SET
                        regional_stock_count = EXCLUDED.regional_stock_count,
                        regional_velocity = EXCLUDED.regional_velocity,
                        regional_capital_deployed = EXCLUDED.regional_capital_deployed,
                        regional_surplus_deficit = EXCLUDED.regional_surplus_deficit;
                """
                
                for row in df_radar_aggregated.iter_rows(named=True):
                    cursor.execute(radar_insert_query, (
                        today_str, client_hash, "South", row["Part_Category"], row["Max_Fitment_Year"],
                        float(row["Regional_Stock_Count"]), float(row["Regional_Velocity"]),
                        float(row["Regional_Capital_Deployed"]), float(row["Regional_Surplus_Deficit"])
                    ))

            conn.commit()
            print("[Govnr Cloud Vault] Financial Telemetry and Demand Radar safely secured in DigitalOcean Database.")
            
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"[Govnr Cloud Error] Failed to upload metrics to database: {e}")
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    elif not is_valid_uuid(client_uuid):
        print(f"[Govnr Sandbox] Invalid UUID '{client_uuid}'. Skipping Cloud Database injection.")