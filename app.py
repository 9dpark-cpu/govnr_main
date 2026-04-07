import streamlit as st
import os
import sys
import io
import json
from contextlib import redirect_stdout
import polars as pl
import auth
import pandas as pd

# 1. Defensively add your logic folder to Python's brain
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NETSUITE_DIR = os.path.join(BASE_DIR, "netsuite_core") 
if NETSUITE_DIR not in sys.path:
    sys.path.insert(0, NETSUITE_DIR)

st.set_page_config(page_title="Govnr | CFO Dashboard", layout="wide")

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False
if "client_uuid" not in st.session_state:
    st.session_state["client_uuid"] = None
if "company_name" not in st.session_state:
    st.session_state["company_name"] = None

def login_page():
    st.title("Govnr Platform Login")
    st.markdown("Secure SaaS CFO Automotive Wholesaler Portal")
    
    tab1, tab2 = st.tabs(["Log In", "Register New Account"])
    
    with tab1:
        st.subheader("Client Login")
        # Unique keys: both tabs run every script execution; keys prevent widget identity collisions.
        email = st.text_input("Work Email", key="login_email")
        password = st.text_input("Password", type="password", key="login_password")

        if st.button("Access Dashboard", key="btn_login"):
            if email and password:
                with st.spinner("Signing in…"):
                    success, result = auth.login_client(email, password)
                if success:
                    st.session_state["authenticated"] = True
                    st.session_state["client_uuid"] = str(result["client_uuid"])
                    st.session_state["company_name"] = result["company_name"]
                    st.session_state["account_tier"] = result.get("account_tier", "diagnostic")
                    st.session_state["inventory_column_mapping"] = result.get("inventory_column_mapping") or {}
                    st.session_state["inbound_email_prefix"] = result.get("inbound_email_prefix", "client")
                    st.rerun()
                else:
                    st.error(result)
            else:
                st.warning("Please enter both email and password.")

    with tab2:
        st.subheader("Onboarding Registration")
        reg_company = st.text_input("Company Name", key="reg_company")
        reg_email = st.text_input("CFO / Admin Email", key="reg_email")
        reg_password = st.text_input("Create Password", type="password", key="reg_password")

        if st.button("Register & Generate Secure ID", key="btn_register"):
            if reg_company and reg_email and reg_password:
                with st.spinner("Creating your account…"):
                    success, result = auth.register_new_client(reg_company, reg_email, reg_password)
                if success:
                    st.success("Account created successfully! Please switch to the Log In tab to access your dashboard.")
                else:
                    st.error(result)
            else:
                st.warning("Please fill out all registration fields.")

# --- WATERFALL FUZZY MATCHER ---
def get_best_match_index(govnr_key, default_ns_name, columns_list, mapping_dict):
    columns_list = list(columns_list)
    
    saved_val = mapping_dict.get(govnr_key)
    if saved_val and saved_val in columns_list:
        return columns_list.index(saved_val) + 1 
        
    if default_ns_name in columns_list:
        return columns_list.index(default_ns_name) + 1
        
    clean_ns = default_ns_name.lower().replace(" ", "").replace("_", "").replace("/", "")
    for i, col in enumerate(columns_list):
        clean_col = col.lower().replace(" ", "").replace("_", "").replace("/", "")
        if clean_col == clean_ns:
            return i + 1
            
    clean_govnr = govnr_key.lower().replace(" ", "").replace("_", "").replace("/", "")
    for i, col in enumerate(columns_list):
        clean_col = col.lower().replace(" ", "").replace("_", "").replace("/", "")
        if clean_col == clean_govnr:
            return i + 1
            
    return 0 

# --- FOOLPROOF SANITY CHECK ENGINE ---
def run_sanity_checks(df, mapping_dict, numeric_keys):
    errors =[]
    
    unmapped = [k for k, v in mapping_dict.items() if v == "--- Select Column ---"]
    if unmapped:
        errors.append(f"Missing Mappings: Please assign a column for {', '.join(unmapped)}")
        return errors 

    seen_csv_cols = {}
    for govnr_key, csv_col in mapping_dict.items():
        if csv_col in seen_csv_cols:
            errors.append(f"Duplicate Mapping: You assigned '{csv_col}' to both '{seen_csv_cols[csv_col]}' and '{govnr_key}'. Each field requires a unique column.")
        else:
            seen_csv_cols[csv_col] = govnr_key
            
    if errors:
        return errors 
        
    for govnr_key, csv_col in mapping_dict.items():
        if govnr_key in numeric_keys:
            if csv_col not in df.columns:
                continue 
            
            sample_list = df[csv_col].drop_nulls().head(50).to_list()
            if not sample_list:
                continue 
            
            valid_count = 0
            for item in sample_list:
                if isinstance(item, (int, float)):
                    valid_count += 1
                    continue
                
                if isinstance(item, str):
                    clean_str = item.replace("$", "").replace(",", "").replace(" ", "").strip()
                    if clean_str in ["", "-"]: 
                        continue 
                    try:
                        float(clean_str)
                        valid_count += 1
                    except ValueError:
                        pass
                        
            if valid_count == 0 and len(sample_list) > 0:
                example_val = sample_list[0]
                errors.append(f"Data Mismatch: '{govnr_key}' expects numbers, but you mapped it to '{csv_col}' which contains text (e.g., '{example_val}').")
                
    return errors

def safe_polars_rename(df, mapping_dict):
    rename_dict = {mapping_dict[k]: k for k in mapping_dict}
    valid_rename = {k: v for k, v in rename_dict.items() if k in df.columns}
    
    collision_renames = {}
    for old_col, new_col in valid_rename.items():
        if new_col in df.columns and new_col not in valid_rename.keys():
            collision_renames[new_col] = f"{new_col}_unmapped"
            
    if collision_renames:
        df = df.rename(collision_renames)
        
    return df.rename(valid_rename)

def main_dashboard():
    st.sidebar.title(f"{st.session_state['company_name']}")
    if st.sidebar.button("Log Out"):
        st.session_state["authenticated"] = False
        st.session_state["client_uuid"] = None
        st.session_state["company_name"] = None
        st.rerun()
        
    st.sidebar.markdown("---")
    st.sidebar.title("Govnr Navigation")
    app_mode = st.sidebar.selectbox("Select Module",["Welcome", "Executive Dashboard", "ERP Data Health Scan"])

    if app_mode == "Welcome":
        st.title("Govnr: Strategic Compliance Brief")
        st.success("Authentication Verified. Secure connection established.")
        st.write(f"Your System Client UUID: {st.session_state['client_uuid']}")
        st.write("Please select a module from the sidebar to begin.")

    elif app_mode == "Executive Dashboard":
        st.title("Executive Financial Dashboard")
        st.write("Historical trends and key performance indicators based on your Govnr syncs.")

        conn = auth.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT date_recorded, total_capital_deployed, dead_stock_val, 
                   inventory_turnover, gross_margin_pct, avg_gmroi 
            FROM client_metrics_history 
            WHERE client_uuid = %s 
            ORDER BY date_recorded ASC
        """, (st.session_state["client_uuid"],))
        
        records = cursor.fetchall()
        cursor.close()
        conn.close()

        if not records:
            st.info("No historical data found. Please run a Data Health Scan to generate your first data point.")
        else:
            df_history = pd.DataFrame(records)
            latest = df_history.iloc[-1]
            st.subheader(f"Latest Snapshot: {latest['date_recorded']}")
            
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Capital Deployed", f"${float(latest['total_capital_deployed']):,.2f}")
            col2.metric("Dead Stock Value", f"${float(latest['dead_stock_val']):,.2f}")
            col3.metric("Inventory Turnover", f"{float(latest['inventory_turnover']):.2f}x")
            col4.metric("Gross Margin", f"{float(latest['gross_margin_pct']):.2f}%")

            st.markdown("---")
            colA, colB = st.columns(2)
            
            with colA:
                st.subheader("Capital vs. Dead Stock")
                chart_data_capital = df_history.set_index('date_recorded')[['total_capital_deployed', 'dead_stock_val']]
                st.line_chart(chart_data_capital)
                
            with colB:
                st.subheader("Turnover Efficiency")
                chart_data_turnover = df_history.set_index('date_recorded')[['inventory_turnover']]
                st.line_chart(chart_data_turnover)

    elif app_mode == "ERP Data Health Scan":
        st.title("ERP: Data Health Scan")
        
        tier = st.session_state.get("account_tier", "diagnostic")
        
        if tier == "diagnostic":
            st.info("DIAGNOSTIC TIER: Manual Upload Mode")
            st.write("Drag and drop your ERP CSVs below for an instant Data Health Scan.")
            
            user_cash = 35000.0
            user_ap = 28000.0
            st.markdown("Upgrade to Enterprise to unlock automated daily webhooks and advanced Open-To-Buy (OTB) guardrails.")
            
        elif tier == "enterprise":
            st.success("ENTERPRISE TIER")
            
            st.markdown("---")
            st.subheader("Liquidity and Working Capital")
            colA, colB = st.columns(2)
            user_cash = colA.number_input("Current Cash Balance ($)", value=35000.0, step=1000.0)
            user_ap = colB.number_input("Upcoming A/P (1-15 days) ($)", value=28000.0, step=1000.0)
                
            st.markdown("---")
            st.subheader("Automated Daily Sync Settings")
            st.write("Set your ERP Daily Inventory Status Saved Search to automatically email this address every morning at 6:00 AM:")
            prefix = st.session_state.get("inbound_email_prefix", "client")
            st.code(f"netsuite_{prefix}@inbound.getgovnr.com", language="text")
            st.write("Our Webhook Engine will automatically catch the file, apply your saved column mapping, run the math, and update your dashboard without any manual uploads.")
            st.markdown("---")

        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("1. Inventory Snapshot")
            inv_file = st.file_uploader("Upload inventory_snapshot.csv", type=["csv"])
            
        with col2:
            st.subheader("2. PO Activity (Optional)")
            po_file = st.file_uploader("Upload po_activity.csv", type=["csv"])

        if inv_file is not None:
            raw_inv_df = pl.read_csv(inv_file, infer_schema_length=10000, null_values=["", " ", "NA", "null"])
            inv_options =["--- Select Column ---"] + list(raw_inv_df.columns)
            
            st.markdown("---")
            st.subheader("Inventory Data Mapping")
            
            inv_required_fields = {
                "SKU": "Item Name/Number",
                "Title": "Description",
                "Location": "Location",
                "Qty_Available": "Available",
                "Qty_Committed": "Quantity Committed",
                "Unit_Cost": "Average Cost",
                "Price": "Base Price",
                "Last_Sales_Date": "Last Sales Date",
                "Target_Stock": "Preferred Stock Level",
                "Vendor": "Vendor",
                "Item_Type": "Type",
                "Annual_Velocity": "365 Qty Sold"
            }
            
            saved_mapping = st.session_state.get("inventory_column_mapping") or {}
            mapped_inv_columns = {}
            
            map_col1, map_col2 = st.columns(2)
            for i, (govnr_field, expected_ns_name) in enumerate(inv_required_fields.items()):
                default_index = get_best_match_index(govnr_field, expected_ns_name, raw_inv_df.columns, saved_mapping)
                    
                target_col = map_col1 if i % 2 == 0 else map_col2
                mapped_inv_columns[govnr_field] = target_col.selectbox(
                    f"Select column for {govnr_field}:",
                    options=inv_options,
                    index=default_index,
                    key=f"map_inv_{govnr_field}"
                )

            mapped_po_columns = {}
            if po_file is not None:
                st.markdown("---")
                st.subheader("Purchase Order Data Mapping")
                raw_po_df = pl.read_csv(po_file, infer_schema_length=10000, null_values=["", " ", "NA", "null"])
                po_options =["--- Select Column ---"] + list(raw_po_df.columns)
                
                po_required_fields = {
                    "PO_Number": "PO Number",
                    "SKU": "Item Name/Number",
                    "Qty_Ordered": "Quantity",
                    "Total_Amount": "Amount"
                }
                
                map_col3, map_col4 = st.columns(2)
                for i, (govnr_field, expected_ns_name) in enumerate(po_required_fields.items()):
                    db_key = f"PO_{govnr_field}"
                    default_index = get_best_match_index(db_key, expected_ns_name, raw_po_df.columns, saved_mapping)
                        
                    target_col = map_col3 if i % 2 == 0 else map_col4
                    mapped_po_columns[govnr_field] = target_col.selectbox(
                        f"Select column for PO {govnr_field}:",
                        options=po_options,
                        index=default_index,
                        key=f"map_po_{govnr_field}"
                    )
            
            if st.button("Confirm Mapping & Execute Data Health Scan"):
                expected_numeric_fields =["Qty_Available", "Qty_Committed", "Unit_Cost", "Price", "Target_Stock", "Annual_Velocity", "Qty_Ordered", "Total_Amount"]
                
                inv_errors = run_sanity_checks(raw_inv_df, mapped_inv_columns, expected_numeric_fields)
                po_errors =[]
                if po_file is not None:
                    po_errors = run_sanity_checks(raw_po_df, mapped_po_columns, expected_numeric_fields)
                    
                all_errors = inv_errors + po_errors
                
                if all_errors:
                    st.error("Data Health Scan halted due to Mapping errors. Please correct the fields below:")
                    for err in all_errors:
                        st.warning(err)
                    st.stop()
                    
                with st.spinner("Executing Govnr logic..."):
                    
                    combined_db_mapping = mapped_inv_columns.copy()
                    if mapped_po_columns:
                        for k, v in mapped_po_columns.items():
                            combined_db_mapping[f"PO_{k}"] = v
                            
                    auth.save_column_mapping(st.session_state["client_uuid"], combined_db_mapping)
                    st.session_state["inventory_column_mapping"] = combined_db_mapping
                    
                    clean_inv_df = safe_polars_rename(raw_inv_df, mapped_inv_columns)
                    
                    inbound_dir = os.path.join(NETSUITE_DIR, "data", "inbound")
                    os.makedirs(inbound_dir, exist_ok=True)
                    inv_path = os.path.join(inbound_dir, "inventory_snapshot.csv")
                    clean_inv_df.write_csv(inv_path)
                    
                    po_path = os.path.join(inbound_dir, "po_activity.csv")
                    if po_file is not None:
                        clean_po_df = safe_polars_rename(raw_po_df, mapped_po_columns)
                        clean_po_df.write_csv(po_path)
                    else:
                        if os.path.exists(po_path):
                            os.remove(po_path)
                            
                    from main import run_daily_governor
                    
                    report_buffer = io.StringIO()
                    with redirect_stdout(report_buffer):
                        run_daily_governor(client_uuid=st.session_state["client_uuid"], proxy_cash=user_cash, proxy_ap=user_ap)
                    
                    terminal_output = report_buffer.getvalue()
                    
                    st.subheader("Govnr Terminal Output")
                    st.code(terminal_output, language="markdown")

                    try:
                        df_calc = clean_inv_df.with_columns([
                            (pl.max_horizontal(pl.col("Qty_Available").cast(pl.Float64, strict=False), 0) * pl.col("Unit_Cost").cast(pl.Float64, strict=False)).alias("Cap_Deployed"),
                            (pl.col("Annual_Velocity").cast(pl.Float64, strict=False) * pl.col("Unit_Cost").cast(pl.Float64, strict=False)).alias("COGS"),
                            (pl.col("Annual_Velocity").cast(pl.Float64, strict=False) * pl.col("Price").cast(pl.Float64, strict=False)).alias("Revenue")
                        ])
                        
                        total_cap = df_calc["Cap_Deployed"].sum() or 0.0
                        dead_val = df_calc.filter(pl.col("Annual_Velocity") == 0)["Cap_Deployed"].sum() or 0.0
                        total_cogs = df_calc["COGS"].sum() or 0.0
                        total_rev = df_calc["Revenue"].sum() or 0.0
                        
                        turnover = total_cogs / total_cap if total_cap > 0 else 0.0
                        margin = ((total_rev - total_cogs) / total_rev * 100) if total_rev > 0 else 0.0
                        
                        auth.save_cfo_metrics(st.session_state["client_uuid"], tier, total_cap, dead_val, turnover, margin, "Tier A")
                        st.success("Financial metrics securely logged to Govnr Vault.")
                    except Exception as e:
                        st.error(f"Internal Metrics Logging Error: {e}")
                    
                    st.subheader("Generated Data Health Scan Deliverables")
                    
                    if isinstance(terminal_output, dict):
                        # Fallback in case terminal_output was overridden
                        buffers = terminal_output
                    else:
                        # Extract returned buffers from main.py
                        from main import run_daily_governor
                        # We execute without redirect_stdout to capture the return value silently
                        buffers = run_daily_governor(client_uuid=st.session_state["client_uuid"], proxy_cash=user_cash, proxy_ap=user_ap)

                    for file_name, file_bytes in buffers.items():
                        mime_type = "application/pdf" if file_name.endswith(".pdf") else "text/csv"
                        st.download_button(
                            label=f"Download {file_name}",
                            data=file_bytes,
                            file_name=file_name,
                            mime=mime_type
                        )

if __name__ == "__main__":
    if st.session_state.get("authenticated", False):
        main_dashboard()
    else:
        login_page()