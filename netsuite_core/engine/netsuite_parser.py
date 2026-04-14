import os
import polars as pl

# Defensive Pathing: Resolve base directory dynamically
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOUND_DIR = os.path.join(BASE_DIR, "data", "inbound")

# ---------------------------------------------------------
# THE GOVERNOR'S STANDARD SCHEMA (NetSuite Mappings)
# ---------------------------------------------------------
# Key = Expected NetSuite UI / export label, Value = Internal backend variable (Data Dictionary)
INVENTORY_COLUMN_MAP = {
    "Item Name/Number": "Item_Identifier",
    "Description": "Item_Description",
    "Location": "Item_Location",
    "Available": "Quantity_Available",
    "Quantity Committed": "Quantity_Committed",
    "Average Cost": "Unit_Average_Cost",
    "Base Price": "Unit_Base_Price",
    "Last Sales Date": "Last_Sales_Date",
    "Preferred Stock Level": "Target_Stock_Level",
    "Vendor": "Preferred_Vendor",
    "Type": "Item_Type",
    "365 Qty Sold": "Annual_Sold",
    "Replaced By": "Replacement_Item_Identifier",
    "UPC Code": "UPC_Code",
    "MPN": "Manufacturer_Part_Number",
    "Brand": "Manufacturer_Brand",
}

PO_COLUMN_MAP = {
    "PO Number": "PO_Number",
    "Date Created": "Date_Created",
    "Status": "Order_Status",
    "Vendor": "Purchase_Order_Vendor",
    "Item Name/Number": "Item_Identifier",
    "Quantity": "Quantity_Ordered",
    "Amount": "Order_Total_Amount",
}


def ingest_inventory_snapshot(client_uuid: str, file_name: str = "inventory_snapshot.csv") -> pl.DataFrame:
    """
    Ingests, validates, and sanitizes the NetSuite daily inventory export.
    Filters out Non-Inventory items (Freight, Labor, etc.).
    """
    file_path = os.path.join(INBOUND_DIR, file_name)

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"[Govnr Fatal] Expected ERP export missing at: {file_path}")

    df = pl.read_csv(file_path, infer_schema_length=10000, null_values=["", " ", "NA", "null"])

    existing_cols = df.columns
    rename_dict = {col: INVENTORY_COLUMN_MAP[col] for col in INVENTORY_COLUMN_MAP if col in existing_cols}
    df = df.rename(rename_dict)

    expected_string_cols = [
        "Item_Identifier",
        "Item_Description",
        "Item_Location",
        "Preferred_Vendor",
        "Item_Type",
        "Replacement_Item_Identifier",
        "UPC_Code",
        "Manufacturer_Part_Number",
        "Manufacturer_Brand",
    ]
    for col in expected_string_cols:
        if col not in df.columns:
            df = df.with_columns(pl.lit(None).cast(pl.String).alias(col))

    expected_numeric_cols = [
        "Quantity_Available",
        "Quantity_Committed",
        "Unit_Average_Cost",
        "Unit_Base_Price",
        "Target_Stock_Level",
        "Annual_Sold",
    ]
    for col in expected_numeric_cols:
        if col not in df.columns:
            df = df.with_columns(pl.lit(0).cast(pl.Float64).alias(col))

    if "Last_Sales_Date" not in df.columns:
        df = df.with_columns(pl.lit(None).cast(pl.String).alias("Last_Sales_Date"))

    if "Item_Type" in df.columns:
        df = df.filter(
            pl.col("Item_Type").str.to_lowercase().str.contains("inventory") | pl.col("Item_Type").is_null()
        )

    df = df.with_columns([
        pl.col("Item_Identifier").cast(pl.String).str.strip_chars(),
        pl.col("Item_Description").cast(pl.String).fill_null("Unknown Application"),
        pl.col("Item_Location").cast(pl.String).str.strip_chars().fill_null("Unassigned"),
        pl.col("Preferred_Vendor").cast(pl.String).fill_null("Unknown"),
        pl.col("Replacement_Item_Identifier").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("UPC_Code").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("Manufacturer_Part_Number").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("Manufacturer_Brand").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("Quantity_Available").cast(pl.Float64).fill_null(0.0),
        pl.col("Quantity_Committed").cast(pl.Float64).fill_null(0.0),
        pl.col("Unit_Average_Cost").cast(pl.Float64).fill_null(0.0),
        pl.col("Unit_Base_Price").cast(pl.Float64).fill_null(0.0),
        pl.col("Target_Stock_Level").cast(pl.Float64).fill_null(0.0),
        pl.col("Annual_Sold").cast(pl.Float64).fill_null(0.0),
        pl.col("Last_Sales_Date").str.strptime(pl.Date, format="%Y-%m-%d", strict=False),
    ])

    df = df.with_columns(
        pl.max_horizontal(pl.col("Quantity_Available") + pl.col("Quantity_Committed"), 0.0).alias("Quantity_On_Hand")
    )

    print(f"[Govnr Engine] {client_uuid} | Ingested {df.height} valid inventory records.")
    return df


def ingest_po_activity(client_uuid: str, file_name: str = "po_activity.csv") -> pl.DataFrame:
    """
    Ingests ERP Draft/Pending Purchase Orders. Saved search is trusted to export only open lines.
    """
    file_path = os.path.join(INBOUND_DIR, file_name)

    empty_schema = {
        "PO_Number": pl.String,
        "Date_Created": pl.Date,
        "Order_Status": pl.String,
        "Purchase_Order_Vendor": pl.String,
        "Item_Identifier": pl.String,
        "Quantity_Ordered": pl.Float64,
        "Order_Total_Amount": pl.Float64,
    }

    if not os.path.exists(file_path):
        return pl.DataFrame(schema=empty_schema)

    df = pl.read_csv(file_path, null_values=["", " "])

    existing_cols = df.columns
    rename_dict = {col: PO_COLUMN_MAP[col] for col in PO_COLUMN_MAP if col in existing_cols}
    df = df.rename(rename_dict)

    for col, dtype in empty_schema.items():
        if col not in df.columns:
            if dtype == pl.Date:
                df = df.with_columns(pl.lit(None).cast(pl.Date).alias(col))
            elif dtype == pl.String:
                df = df.with_columns(pl.lit("").cast(pl.String).alias(col))
            else:
                df = df.with_columns(pl.lit(0.0).cast(pl.Float64).alias(col))

    df = df.with_columns([
        pl.col("PO_Number").cast(pl.String).str.strip_chars(),
        pl.col("Order_Status").cast(pl.String).str.strip_chars(),
        pl.col("Purchase_Order_Vendor").cast(pl.String).str.strip_chars().fill_null(""),
        pl.col("Item_Identifier").cast(pl.String).str.strip_chars(),
        pl.col("Quantity_Ordered").cast(pl.Float64).fill_null(0.0),
        pl.col("Order_Total_Amount").cast(pl.Float64).fill_null(0.0),
    ])

    if df["Date_Created"].dtype == pl.String:
        df = df.with_columns(
            pl.col("Date_Created").str.strptime(pl.Date, format="%Y-%m-%d", strict=False)
        )
    else:
        df = df.with_columns(pl.col("Date_Created").cast(pl.Date, strict=False))

    print(f"[Govnr Engine] {client_uuid} | Ingested {df.height} pending PO lines.")
    return df


if __name__ == "__main__":
    df_inv = ingest_inventory_snapshot(client_uuid="client_test_001")
    df_po = ingest_po_activity(client_uuid="client_test_001")

    print("\n--- INVENTORY SANITY CHECK ---")
    print(df_inv.head(3))
