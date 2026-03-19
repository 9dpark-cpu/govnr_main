import os
import sys

# ---  GOVNR PATH INJECTION ---
# This ensures Python looks in the current folder for our modules
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# --- IMPORTS ---
try:
    from connectors.shopify_api import GovnrHighSpeedScanner
    from engine.financial_math import calculate_financial_health
except ModuleNotFoundError as e:
    print(f" [Path Error] Could not find a Govnr module: {e}")
    print("\n FOOLPROOF FIX:")
    print("1. Ensure you created empty __init__.py files in /connectors, /engine, and /outputs.")
    print(f"2. Your current folder is: {BASE_DIR}")
    sys.exit(1)

if __name__ == "__main__":
    print("Govnr Scan Initiating...")
    
    scanner = GovnrHighSpeedScanner()
    try:
        # 1. Extraction Layer
        df_inv = scanner.extract_inventory()
        df_sales = scanner.extract_sales_velocity()
        
        if df_inv is not None and not df_inv.is_empty():
            # 2. Logic & Math Layer
            calculate_financial_health(df_inv, df_sales)
        else:
            print(" [Error] No active inventory found on Shopify.")
            
    except Exception as e:
        print(f"\n [CRITICAL SYSTEM FAULT]: {e}")