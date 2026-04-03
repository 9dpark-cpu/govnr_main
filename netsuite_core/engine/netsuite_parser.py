import os
import polars as pl

# Defensive Pathing: Resolve base directory dynamically
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOUND_DIR = os.path.join(BASE_DIR, "data", "inbound")

# ---------------------------------------------------------
# THE GOVERNOR'S STANDARD SCHEMA (NetSuite Mappings)
# ---------------------------------------------------------
# This allows us to handle clients who rename their NetSuite columns.
# Key = Our Internal Logic Name, Value = Expected NetSuite UI Label
INVENTORY_COLUMN_MAP = {
    "Item Name/Number": "SKU",
    "Description": "Title",
    "Location": "Location",
    "Available": "Qty_Available",
    "Quantity Committed": "Qty_Committed",
    "Average Cost": "Unit_Cost",
    "Base Price": "Price",
    "Last Sales Date": "Last_Sales_Date",
    "Preferred Stock Level": "Target_Stock",
    "Vendor": "Vendor",
    "Type": "Item_Type",
    "365 Qty Sold": "Annual_Velocity",
    # --- ENTERPRISE 23-COLUMN SCHEMA ADDITIONS ---
    "Replaced By": "Replaced_By",
    "UPC Code": "UPC",
    "MPN": "MPN",
    "Brand": "Brand",
    "Category": "Category"
}

PO_COLUMN_MAP = {
    "PO Number": "PO_Number",
    "Date Created": "Date_Created",
    "Status": "Status",
    "Vendor": "Vendor",
    "Item Name/Number": "SKU",
    "Quantity": "Qty_Ordered",
    "Amount": "Total_Amount"
}

def ingest_inventory_snapshot(client_uuid: str, file_name: str = "inventory_snapshot.csv") -> pl.DataFrame:
    """
    Ingests, validates, and sanitizes the NetSuite daily inventory export.
    Filters out Non-Inventory items (Freight, Labor, etc.).
    Handles both 6-column Diagnostic Scans and 23-column Enterprise Payloads.
    """
    file_path = os.path.join(INBOUND_DIR, file_name)
    
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"[Govnr Fatal] Expected ERP export missing at: {file_path}")

    # 1. Read CSV as strings initially to prevent Polars from making wrong assumptions on dirty data
    df = pl.read_csv(file_path, infer_schema_length=10000, null_values=["", " ", "NA", "null"])

    # 2. Rename columns based on our Standard Schema (handles varying client labels if mapped)
    existing_cols = df.columns
    rename_dict = {col: INVENTORY_COLUMN_MAP[col] for col in INVENTORY_COLUMN_MAP if col in existing_cols}
    df = df.rename(rename_dict)

    # 3. Defensive Schema Injection (Allows 6-column Diagnostic Scans to run on 23-column logic)
    expected_string_cols =["SKU", "Title", "Location", "Vendor", "Item_Type", "Replaced_By", "UPC", "MPN", "Brand", "Category"]
    for col in expected_string_cols:
        if col not in df.columns:
            df = df.with_columns(pl.lit(None).cast(pl.String).alias(col))

    expected_numeric_cols =["Qty_Available", "Qty_Committed", "Unit_Cost", "Price", "Target_Stock", "Annual_Velocity"]
    for col in expected_numeric_cols:
        if col not in df.columns:
            df = df.with_columns(pl.lit(0).cast(pl.Float64).alias(col))
            
    if "Last_Sales_Date" not in df.columns:
        df = df.with_columns(pl.lit(None).cast(pl.String).alias("Last_Sales_Date"))

    # 4. Filter "Dirty Data" - Keep ONLY Inventory Items
    if "Item_Type" in df.columns:
        df = df.filter(pl.col("Item_Type").str.to_lowercase().str.contains("inventory") | pl.col("Item_Type").is_null())

    # 5. Enforce Types and Sanitize
    df = df.with_columns([
        pl.col("SKU").cast(pl.String).str.strip_chars(),
        pl.col("Title").cast(pl.String).fill_null("Unknown Application"),
        pl.col("Location").cast(pl.String).str.strip_chars().fill_null("Unassigned"),
        pl.col("Vendor").cast(pl.String).fill_null("Unknown"),
        pl.col("Replaced_By").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("UPC").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("MPN").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("Brand").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("Category").cast(pl.String).str.strip_chars().fill_null(""),
        
        # Financials & Quantities: Fill nulls with 0, cast to float/int
        pl.col("Qty_Available").cast(pl.Int64).fill_null(0),
        pl.col("Qty_Committed").cast(pl.Int64).fill_null(0),
        pl.col("Unit_Cost").cast(pl.Float64).fill_null(0.0),
        pl.col("Price").cast(pl.Float64).fill_null(0.0),
        pl.col("Target_Stock").cast(pl.Int64).fill_null(0),
        pl.col("Annual_Velocity").cast(pl.Int64).fill_null(0),
        
        # Dates: Parse string to Date, handle blanks
        pl.col("Last_Sales_Date").str.strptime(pl.Date, format="%Y-%m-%d", strict=False)
    ])

    df = df.with_columns(
        (pl.col("Qty_Available") + pl.col("Qty_Committed")).alias("Qty_On_Hand")
    )
    
    print(f"[Govnr Engine] {client_uuid} | Ingested {df.height} valid inventory records.")
    return df

def ingest_po_activity(client_uuid: str, file_name: str = "po_activity.csv") -> pl.DataFrame:
    """
    Ingests ERP Draft/Pending Purchase Orders for the Toxic Stock Alert logic.
    """
    file_path = os.path.join(INBOUND_DIR, file_name)
    
    if not os.path.exists(file_path):
        return pl.DataFrame(schema={"PO_Number": pl.String, "SKU": pl.String, "Qty_Ordered": pl.Int64, "Total_Amount": pl.Float64})

    df = pl.read_csv(file_path, null_values=["", " "])
    
    existing_cols = df.columns
    rename_dict = {col: PO_COLUMN_MAP[col] for col in PO_COLUMN_MAP if col in existing_cols}
    df = df.rename(rename_dict)

    df = df.with_columns([
        pl.col("PO_Number").cast(pl.String).str.strip_chars(),
        pl.col("SKU").cast(pl.String).str.strip_chars(),
        pl.col("Status").cast(pl.String).str.to_lowercase(),
        pl.col("Qty_Ordered").cast(pl.Float64).fill_null(0.0),
        pl.col("Total_Amount").cast(pl.Float64).fill_null(0.0)
    ])

    print(f"[Govnr Engine] {client_uuid} | Ingested {df.height} pending PO lines.")
    return df

if __name__ == "__main__":
    # Local Testing Execution
    df_inv = ingest_inventory_snapshot(client_uuid="client_test_001")
    df_po = ingest_po_activity(client_uuid="client_test_001")
    
    print("\n--- INVENTORY SANITY CHECK ---")
    print(df_inv.head(3))