import streamlit as st
import os
import sys
import io
import json
import hashlib
import stat
from typing import Optional
from contextlib import redirect_stdout
import polars as pl
import paramiko
import auth
import pandas as pd

# 1. Defensively add your logic folder to Python's brain
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NETSUITE_DIR = os.path.join(BASE_DIR, "netsuite_core") 
if NETSUITE_DIR not in sys.path:
    sys.path.insert(0, NETSUITE_DIR)

st.set_page_config(
    page_title="Govnr | CFO Dashboard",
    layout="wide",
    initial_sidebar_state="expanded",
)

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
        # Streamlit forms support Enter-to-submit from focused fields (including password input).
        with st.form("login_form", clear_on_submit=False):
            # Unique keys: both tabs run every script execution; keys prevent widget identity collisions.
            email = st.text_input("Work Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Access Dashboard")

        if submitted:
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


def _load_private_key_from_env(env_var_name: str):
    key_data = os.getenv(env_var_name or "", "").strip()
    if not key_data:
        return None
    key_data = key_data.replace("\\n", "\n")
    for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_cls.from_private_key(io.StringIO(key_data))
        except Exception:
            continue
    raise ValueError(f"Invalid private key in environment variable '{env_var_name}'.")


def _sftp_newest_csv_name(sftp, inbound_path: str, pattern: str):
    """Return filename of newest .csv in inbound_path whose name contains pattern (matches sftp_watcher)."""
    candidates = []
    for entry in sftp.listdir_attr(inbound_path):
        if not stat.S_ISREG(entry.st_mode):
            continue
        name_lower = entry.filename.lower()
        if pattern.lower() in name_lower and name_lower.endswith(".csv"):
            candidates.append((entry.st_mtime, entry.filename))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _log_ingestion_run_row(client_uuid: str, file_name: str, file_hash: str, status: str, error_message: Optional[str] = None):
    conn = auth.get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO ingestion_runs (client_uuid, file_name, file_hash, status, error_message, source)
            VALUES (%s, %s, %s, %s, %s, 'admin_ui')
            ON CONFLICT (client_uuid, file_name, file_hash)
            DO UPDATE SET
                status = EXCLUDED.status,
                error_message = EXCLUDED.error_message,
                processed_at = CURRENT_TIMESTAMP,
                source = EXCLUDED.source
            """,
            (client_uuid, file_name, file_hash, status, error_message),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def onboarding_settings_page() -> None:
    """SFTP automation + CSV column mapping (single onboarding surface). CFO limits live on Executive Dashboard."""
    st.title("Onboarding Settings")
    st.caption(
        "Configure automated file pickup (SFTP) and one-time CSV column mapping. "
        "**Weekly OTB limit** and **minimum transfer value** are saved on the **Executive Dashboard**."
    )

    if not auth.enterprise_ingestion_tables_exist():
        st.warning(
            "The database does not have the enterprise ingestion tables yet. This is normal for databases "
            "created before SFTP automation was added. Run the one-time setup below (safe to run more than once)."
        )
        if st.button("Create enterprise ingestion tables (one-time)", key="btn_ensure_ingestion_schema"):
            try:
                auth.ensure_enterprise_ingestion_tables()
                st.success("Tables created. Reloading this page.")
                st.rerun()
            except Exception as e:
                st.error(f"Migration failed: {e}")
        st.stop()

    try:
        auth.ensure_enterprise_ingestion_tables()
    except Exception as e:
        st.error(f"Schema migration failed: {e}")
        st.stop()

    client_uuid = st.session_state["client_uuid"]
    tab_sftp, tab_mapping = st.tabs(["SFTP & automation", "Column mapping (CSV)"])
    with tab_sftp:
        _render_sftp_automation_tab(client_uuid)
    with tab_mapping:
        _render_column_mapping_tab(client_uuid)


def _render_sftp_automation_tab(client_uuid: str) -> None:
    conn = auth.get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT
                enabled,
                sftp_host,
                sftp_port,
                sftp_username,
                sftp_password_env_var,
                sftp_private_key_env_var,
                sftp_inbound_path,
                inventory_filename_pattern,
                annual_sold_filename_pattern,
                po_filename_pattern,
                delete_remote_after_success,
                max_file_size_mb
            FROM client_ingestion_config
            WHERE client_uuid = %s
            """,
            (client_uuid,),
        )
        existing = cursor.fetchone() or {}
    finally:
        cursor.close()
        conn.close()

    with st.form("ingestion_config_form"):
        st.subheader("SFTP Client Config")
        enabled = st.checkbox("Enable automated ingestion", value=bool(existing.get("enabled", False)))
        col1, col2 = st.columns(2)
        sftp_host = col1.text_input("SFTP Host", value=existing.get("sftp_host") or "")
        sftp_port = int(col2.number_input("SFTP Port", min_value=1, max_value=65535, value=int(existing.get("sftp_port") or 22)))

        col3, col4 = st.columns(2)
        sftp_username = col3.text_input("SFTP Username", value=existing.get("sftp_username") or "")
        sftp_inbound_path = col4.text_input("Remote inbound path", value=existing.get("sftp_inbound_path") or "/inbound")

        st.markdown("Credential references (recommended):")
        col5, col6 = st.columns(2)
        sftp_password_env_var = col5.text_input(
            "Password env var name",
            value=existing.get("sftp_password_env_var") or "",
            help="Example: CLIENT_ABC_SFTP_PASSWORD",
        )
        sftp_private_key_env_var = col6.text_input(
            "Private key env var name",
            value=existing.get("sftp_private_key_env_var") or "",
            help="Example: CLIENT_ABC_SFTP_PRIVATE_KEY",
        )

        st.markdown("File matching + processing:")
        col7, col8, col9 = st.columns(3)
        inventory_pattern = col7.text_input(
            "Inventory filename pattern",
            value=existing.get("inventory_filename_pattern") or "inventory_snapshot",
        )
        po_pattern = col8.text_input(
            "PO filename pattern",
            value=existing.get("po_filename_pattern") or "po_activity",
        )
        max_file_size_mb = int(
            col9.number_input(
                "Max file size (MB)",
                min_value=1,
                max_value=2000,
                value=int(existing.get("max_file_size_mb") or 150),
            )
        )
        annual_sold_pattern = st.text_input(
            "Annual sold filename pattern",
            value=existing.get("annual_sold_filename_pattern") or "annual_sold",
            help="Substring match for mandatory 365-day velocity CSV (case-insensitive).",
        )

        delete_remote_after_success = st.checkbox(
            "Delete remote after success",
            value=bool(existing.get("delete_remote_after_success", False)),
            help="Keep disabled until your workflow is fully validated.",
        )

        submitted = st.form_submit_button("Save Enterprise Ingestion Config")
        if submitted:
            if not sftp_host or not sftp_username:
                st.error("SFTP Host and SFTP Username are required.")
            elif not sftp_password_env_var and not sftp_private_key_env_var:
                st.error("Provide at least one credential reference env var (password or private key).")
            else:
                conn = auth.get_db_connection()
                cursor = conn.cursor()
                try:
                    cursor.execute(
                        """
                        INSERT INTO client_ingestion_config (
                            client_uuid,
                            enabled,
                            sftp_host,
                            sftp_port,
                            sftp_username,
                            sftp_password,
                            sftp_password_env_var,
                            sftp_private_key_env_var,
                            sftp_inbound_path,
                            inventory_filename_pattern,
                            annual_sold_filename_pattern,
                            po_filename_pattern,
                            delete_remote_after_success,
                            max_file_size_mb,
                            updated_at
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (client_uuid) DO UPDATE SET
                            enabled = EXCLUDED.enabled,
                            sftp_host = EXCLUDED.sftp_host,
                            sftp_port = EXCLUDED.sftp_port,
                            sftp_username = EXCLUDED.sftp_username,
                            sftp_password = NULL,
                            sftp_password_env_var = EXCLUDED.sftp_password_env_var,
                            sftp_private_key_env_var = EXCLUDED.sftp_private_key_env_var,
                            sftp_inbound_path = EXCLUDED.sftp_inbound_path,
                            inventory_filename_pattern = EXCLUDED.inventory_filename_pattern,
                            annual_sold_filename_pattern = EXCLUDED.annual_sold_filename_pattern,
                            po_filename_pattern = EXCLUDED.po_filename_pattern,
                            delete_remote_after_success = EXCLUDED.delete_remote_after_success,
                            max_file_size_mb = EXCLUDED.max_file_size_mb,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            client_uuid,
                            enabled,
                            sftp_host.strip(),
                            sftp_port,
                            sftp_username.strip(),
                            sftp_password_env_var.strip() or None,
                            sftp_private_key_env_var.strip() or None,
                            sftp_inbound_path.strip() or "/inbound",
                            inventory_pattern.strip() or "inventory_snapshot",
                            annual_sold_pattern.strip() or "annual_sold",
                            po_pattern.strip() or "po_activity",
                            delete_remote_after_success,
                            max_file_size_mb,
                        ),
                    )
                    conn.commit()
                    st.success("Enterprise ingestion config saved.")
                except Exception as e:
                    conn.rollback()
                    st.error(f"Failed to save ingestion config: {e}")
                finally:
                    cursor.close()
                    conn.close()

    st.markdown("---")
    st.subheader("Connection Test")
    if st.button("Test SFTP Connection", key="btn_test_sftp_connection"):
        if not sftp_host or not sftp_username:
            st.error("Set SFTP Host and SFTP Username first.")
        elif not sftp_password_env_var and not sftp_private_key_env_var:
            st.error("Set at least one credential env var name before testing.")
        else:
            try:
                with st.spinner("Testing SFTP connectivity..."):
                    password = os.getenv((sftp_password_env_var or "").strip()) if sftp_password_env_var else None
                    pkey = _load_private_key_from_env((sftp_private_key_env_var or "").strip()) if sftp_private_key_env_var else None
                    transport = paramiko.Transport((sftp_host.strip(), int(sftp_port)))
                    transport.connect(username=sftp_username.strip(), password=password, pkey=pkey)
                    sftp = paramiko.SFTPClient.from_transport(transport)
                    _ = sftp.listdir((sftp_inbound_path or "/inbound").strip())
                    sftp.close()
                    transport.close()
                st.success("SFTP connection successful and inbound path is readable.")
            except Exception as e:
                st.error(f"SFTP test failed: {e}")

    st.markdown("---")
    st.subheader("Dry run (latest files, no remote delete)")
    st.caption(
        "Downloads the newest **inventory** and **annual_sold** CSVs from SFTP (mandatory) and optionally **PO** into "
        "the local inbound folder, then runs the Govnr pipeline. Remote files are never deleted. The pipeline may still "
        "write metrics to the database like a normal run."
    )
    if st.button("Dry Run Ingestion (latest files)", key="btn_dry_run_ingestion"):
        if not sftp_host or not sftp_username:
            st.error("Set SFTP Host and SFTP Username first.")
        elif not sftp_password_env_var and not sftp_private_key_env_var:
            st.error("Set at least one credential env var name before running.")
        else:
            inv_pat = (inventory_pattern or "inventory_snapshot").strip()
            as_pat = (annual_sold_pattern or "annual_sold").strip()
            po_pat = (po_pattern or "po_activity").strip()
            inbound = (sftp_inbound_path or "/inbound").strip()
            max_bytes = int(max_file_size_mb) * 1024 * 1024
            transport = None
            sftp = None
            try:
                with st.spinner("Downloading from SFTP and running Govnr pipeline..."):
                    password = os.getenv((sftp_password_env_var or "").strip()) if sftp_password_env_var else None
                    pkey = _load_private_key_from_env((sftp_private_key_env_var or "").strip()) if sftp_private_key_env_var else None
                    transport = paramiko.Transport((sftp_host.strip(), int(sftp_port)))
                    transport.connect(username=sftp_username.strip(), password=password, pkey=pkey)
                    sftp = paramiko.SFTPClient.from_transport(transport)

                    inv_file = _sftp_newest_csv_name(sftp, inbound, inv_pat)
                    if not inv_file:
                        st.error(f"No inventory CSV found in {inbound} matching pattern “{inv_pat}”.")
                    else:
                        inv_remote = f"{inbound.rstrip('/')}/{inv_file}"
                        with sftp.file(inv_remote, "rb") as rf:
                            inv_bytes = rf.read()
                        if len(inv_bytes) > max_bytes:
                            st.error(f"Inventory file exceeds max size ({max_file_size_mb} MB).")
                        else:
                            inv_hash = hashlib.sha256(inv_bytes).hexdigest()
                            inbound_dir = os.path.join(NETSUITE_DIR, "data", "inbound")
                            os.makedirs(inbound_dir, exist_ok=True)
                            inv_local = os.path.join(inbound_dir, "inventory_snapshot.csv")
                            as_local = os.path.join(inbound_dir, "annual_sold.csv")
                            po_local = os.path.join(inbound_dir, "po_activity.csv")
                            with open(inv_local, "wb") as f:
                                f.write(inv_bytes)

                            run_pipeline = True
                            as_file = _sftp_newest_csv_name(sftp, inbound, as_pat)
                            if not as_file:
                                st.error(f"No annual_sold CSV found in {inbound} matching pattern “{as_pat}”.")
                                run_pipeline = False
                            else:
                                as_remote = f"{inbound.rstrip('/')}/{as_file}"
                                with sftp.file(as_remote, "rb") as rf:
                                    as_bytes = rf.read()
                                if len(as_bytes) > max_bytes:
                                    st.error(f"Annual sold file exceeds max size ({max_file_size_mb} MB).")
                                    run_pipeline = False
                                else:
                                    with open(as_local, "wb") as f:
                                        f.write(as_bytes)

                            po_file = _sftp_newest_csv_name(sftp, inbound, po_pat)
                            if po_file:
                                po_remote = f"{inbound.rstrip('/')}/{po_file}"
                                with sftp.file(po_remote, "rb") as rf:
                                    po_bytes = rf.read()
                                if len(po_bytes) > max_bytes:
                                    st.error(f"PO file exceeds max size ({max_file_size_mb} MB).")
                                    run_pipeline = False
                                else:
                                    with open(po_local, "wb") as f:
                                        f.write(po_bytes)
                            else:
                                if os.path.exists(po_local):
                                    os.remove(po_local)

                            if run_pipeline:
                                from main import run_daily_governor

                                report_buffer = io.StringIO()
                                with redirect_stdout(report_buffer):
                                    result = run_daily_governor(
                                        client_uuid=client_uuid,
                                        company_name=st.session_state["company_name"],
                                    )
                                out = report_buffer.getvalue()
                                st.subheader("Govnr pipeline output")
                                st.code(out or "(no stdout captured)", language="text")
                                if result is None:
                                    st.error(
                                        "Pipeline halted (missing mandatory file, schema drift, or engine error). "
                                        "See output above."
                                    )
                                _log_ingestion_run_row(
                                    client_uuid,
                                    inv_file,
                                    inv_hash,
                                    "dry_run" if result is not None else "failed",
                                    None if result is not None else "run_daily_governor returned no result",
                                )
                                if result is not None:
                                    st.success("Dry run completed. Remote SFTP files were not modified.")
            except Exception as e:
                err = str(e)
                st.error(f"Dry run failed: {err}")
                try:
                    _log_ingestion_run_row(client_uuid, "dry_run_error", hashlib.sha256(err.encode()).hexdigest()[:64], "failed", err)
                except Exception:
                    pass
            finally:
                if sftp:
                    sftp.close()
                if transport:
                    transport.close()

    st.markdown("---")
    st.subheader("Recent Ingestion Runs")
    conn = auth.get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT file_name, status, source, error_message, processed_at
            FROM ingestion_runs
            WHERE client_uuid = %s
            ORDER BY processed_at DESC
            LIMIT 100
            """,
            (client_uuid,),
        )
        run_rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    if not run_rows:
        st.info("No ingestion runs recorded yet for this client.")
    else:
        st.dataframe(pd.DataFrame(run_rows), use_container_width=True)


def _render_column_mapping_tab(client_uuid: str) -> None:
    """Rosetta Stone mapping only. Filename patterns are edited under SFTP & automation."""
    from engine.erp_config_loader import fetch_ingestion_config
    from engine.erp_mapping_validation import (
        FLOAT_ANNUAL_SOLD_FIELDS,
        FLOAT_INVENTORY_FIELDS,
        FLOAT_PO_FIELDS,
        SELECT_TOKEN,
        validate_date_mapped_columns,
        validate_float_mapped_columns,
        validate_required_mappings,
    )
    from engine.netsuite_parser import ANNUAL_SOLD_REQUIRED_BACKEND, INVENTORY_REQUIRED_BACKEND, PO_REQUIRED_BACKEND

    st.subheader("Column mapping (CSV)")
    st.caption(
        "Upload sample exports and map headers to Govnr fields. Save is blocked until validation passes. "
        "SFTP filename patterns are configured in the other tab. CFO limits are on the Executive Dashboard."
    )
    st.markdown("---")
    st.subheader("Sample files for mapping (pre-flight validation only)")
    inv_fu = st.file_uploader("inventory_snapshot.csv (required)", type=["csv"], key="erp_fu_inv")
    as_fu = st.file_uploader("annual_sold.csv (required)", type=["csv"], key="erp_fu_as")
    po_fu = st.file_uploader("po_activity.csv (optional)", type=["csv"], key="erp_fu_po")

    if inv_fu is None or as_fu is None:
        st.info("Upload **inventory** and **annual_sold** sample exports to configure column mappings.")
        return

    cfg = fetch_ingestion_config(client_uuid)
    raw_inv = pl.read_csv(inv_fu, infer_schema_length=10000, null_values=["", " ", "NA", "null"])
    raw_as = pl.read_csv(as_fu, infer_schema_length=10000, null_values=["", " ", "NA", "null"])
    raw_po = (
        pl.read_csv(po_fu, infer_schema_length=10000, null_values=["", " ", "NA", "null"]) if po_fu is not None else None
    )

    inv_fields = {
        "Item_Identifier": "Item Name/Number",
        "Item_Description": "Description",
        "Item_Location": "Location",
        "Quantity_Available": "Available",
        "Quantity_Committed": "Quantity Committed",
        "Unit_Average_Cost": "Average Cost",
        "Unit_Base_Price": "Base Price",
        "Last_Sales_Date": "Last Sales Date",
        "Target_Stock_Level": "Preferred Stock Level",
        "Preferred_Vendor": "Vendor",
        "Item_Type": "Type",
        "Replacement_Item_Identifier": "Replaced By",
        "UPC_Code": "UPC Code",
        "Manufacturer_Part_Number": "MPN",
        "Manufacturer_Brand": "Brand",
    }
    as_fields = {
        "Item_Identifier": "Item Name/Number",
        "Item_Location": "Location",
        "Annual_Sold": "365 Qty Sold",
    }
    po_fields = {
        "PO_Number": "PO Number",
        "Date_Created": "Date Created",
        "Order_Status": "Status",
        "Purchase_Order_Vendor": "Vendor",
        "Item_Identifier": "Item Name/Number",
        "Quantity_Ordered": "Quantity",
        "Order_Total_Amount": "Amount",
    }

    saved_inv = cfg.get("inventory_column_mapping") or {}
    saved_as = cfg.get("annual_sold_column_mapping") or {}
    saved_po = cfg.get("po_column_mapping") or {}

    inv_opts = [SELECT_TOKEN] + list(raw_inv.columns)
    as_opts = [SELECT_TOKEN] + list(raw_as.columns)

    st.subheader("Inventory column mapping")
    inv_map_ui = {}
    cols_inv = st.columns(2)
    for i, (gk, hint) in enumerate(inv_fields.items()):
        idx = get_best_match_index(gk, hint, raw_inv.columns, saved_inv)
        inv_map_ui[gk] = cols_inv[i % 2].selectbox(
            gk,
            options=inv_opts,
            index=idx,
            key=f"erp_map_inv_{gk}",
        )

    st.subheader("Annual sold column mapping")
    as_map_ui = {}
    cols_as = st.columns(3)
    for i, (gk, hint) in enumerate(as_fields.items()):
        idx = get_best_match_index(gk, hint, raw_as.columns, saved_as)
        as_map_ui[gk] = cols_as[i % 3].selectbox(
            gk,
            options=as_opts,
            index=idx,
            key=f"erp_map_as_{gk}",
        )

    po_map_ui = {}
    if raw_po is not None:
        st.subheader("Purchase order column mapping")
        po_opts = [SELECT_TOKEN] + list(raw_po.columns)
        cols_po = st.columns(2)
        for i, (gk, hint) in enumerate(po_fields.items()):
            idx = get_best_match_index(gk, hint, raw_po.columns, saved_po)
            po_map_ui[gk] = cols_po[i % 2].selectbox(
                gk,
                options=po_opts,
                index=idx,
                key=f"erp_map_po_{gk}",
            )

    if st.button("Save Mapping Configuration", type="primary", key="erp_save_mappings"):

        def internal_dup(mapping: dict, label: str) -> list[str]:
            seen: dict[str, str] = {}
            for k, v in mapping.items():
                if not v or v == SELECT_TOKEN:
                    continue
                if v in seen:
                    return [f"{label}: CSV column '{v}' is mapped to both '{seen[v]}' and '{k}'."]
                seen[v] = k
            return []

        inv_out = {k: v for k, v in inv_map_ui.items() if v != SELECT_TOKEN}
        as_out = {k: v for k, v in as_map_ui.items() if v != SELECT_TOKEN}
        po_out = {k: v for k, v in po_map_ui.items() if v != SELECT_TOKEN} if raw_po is not None else {}

        errs: list[str] = []
        errs.extend(internal_dup(inv_map_ui, "inventory_snapshot"))
        errs.extend(internal_dup(as_map_ui, "annual_sold"))
        if raw_po is not None:
            errs.extend(internal_dup(po_map_ui, "po_activity"))

        errs.extend(validate_required_mappings(inv_map_ui, INVENTORY_REQUIRED_BACKEND, "inventory_snapshot"))
        errs.extend(validate_required_mappings(as_map_ui, ANNUAL_SOLD_REQUIRED_BACKEND, "annual_sold"))
        errs.extend(validate_float_mapped_columns(raw_inv, inv_map_ui, FLOAT_INVENTORY_FIELDS, "inventory_snapshot"))
        errs.extend(
            validate_date_mapped_columns(raw_inv, inv_map_ui, ("Last_Sales_Date",), "inventory_snapshot")
        )
        errs.extend(validate_float_mapped_columns(raw_as, as_map_ui, FLOAT_ANNUAL_SOLD_FIELDS, "annual_sold"))

        if raw_po is not None:
            errs.extend(validate_required_mappings(po_map_ui, PO_REQUIRED_BACKEND, "po_activity"))
            errs.extend(validate_float_mapped_columns(raw_po, po_map_ui, FLOAT_PO_FIELDS, "po_activity"))
            errs.extend(
                validate_date_mapped_columns(raw_po, po_map_ui, ("Date_Created",), "po_activity")
            )

        if errs:
            st.error("Save halted. Fix the issues below.")
            for msg in errs:
                st.warning(msg)
            st.stop()

        cfg_pat = fetch_ingestion_config(client_uuid)
        inv_pat = str(cfg_pat.get("inventory_filename_pattern") or "inventory_snapshot").strip()
        as_pat = str(cfg_pat.get("annual_sold_filename_pattern") or "annual_sold").strip()
        po_pat = str(cfg_pat.get("po_filename_pattern") or "po_activity").strip()
        auth.upsert_client_ingestion_column_mappings(
            client_uuid,
            inv_pat,
            as_pat,
            po_pat,
            inv_out,
            as_out,
            po_out,
        )

        inbound_dir = os.path.join(NETSUITE_DIR, "data", "inbound")
        os.makedirs(inbound_dir, exist_ok=True)
        inv_path = os.path.join(inbound_dir, "inventory_snapshot.csv")
        as_path = os.path.join(inbound_dir, "annual_sold.csv")
        po_path = os.path.join(inbound_dir, "po_activity.csv")
        with open(inv_path, "wb") as f:
            f.write(inv_fu.getbuffer())
        with open(as_path, "wb") as f:
            f.write(as_fu.getbuffer())
        if po_fu is not None:
            with open(po_path, "wb") as f:
                f.write(po_fu.getbuffer())
        elif os.path.exists(po_path):
            os.remove(po_path)

        st.success(
            "Column mappings and filename patterns saved to client_ingestion_config. "
            "Local inbound CSVs were updated for the next Manual Execution."
        )


def main_dashboard():
    st.sidebar.title(f"{st.session_state['company_name']}")
    if st.sidebar.button("Log Out"):
        st.session_state["authenticated"] = False
        st.session_state["client_uuid"] = None
        st.session_state["company_name"] = None
        st.rerun()
        
    st.sidebar.markdown("---")
    st.sidebar.title("Govnr Navigation")
    app_mode = st.sidebar.selectbox(
        "Select Module",
        [
            "Welcome",
            "Executive Dashboard",
            "Onboarding Settings",
            "Manual Execution",
        ],
        key="govnr_nav_module",
    )

    if app_mode == "Welcome":
        st.title("Govnr: Strategic Compliance Brief")
        st.success("Authentication Verified. Secure connection established.")
        st.write(f"Welcome, {st.session_state['company_name']}.")
        st.write("Please select a module from the sidebar to begin.")

    elif app_mode == "Executive Dashboard":
        st.title("Executive Financial Dashboard")
        st.write("Historical trends and key performance indicators based on your Govnr syncs.")
        timeframe = st.selectbox(
            "Select Timeframe",
            ["Last 30 Days", "Last Quarter", "Year-to-Date", "All Time"],
        )

        client_uuid_dash = st.session_state["client_uuid"]
        from engine.erp_config_loader import (
            fetch_ingestion_config,
            resolve_enforce_supersession,
            resolve_toxic_po_material_threshold,
            resolve_min_transfer_value_threshold,
            resolve_weekly_otb_limit,
        )

        _cfg_dash = fetch_ingestion_config(client_uuid_dash)
        _w_show, _ = resolve_weekly_otb_limit(_cfg_dash.get("weekly_otb_limit"))
        _m_show, _ = resolve_min_transfer_value_threshold(_cfg_dash.get("min_transfer_value_threshold"))
        toxic_raw, _ = resolve_toxic_po_material_threshold(_cfg_dash.get("toxic_po_material_threshold"))
        super_raw = resolve_enforce_supersession(_cfg_dash.get("enforce_supersession"))

        st.subheader("CFO limits")
        st.caption(
            "Weekly OTB and minimum transfer value are stored in PostgreSQL and drive the math engine. "
            "Update here anytime without re-uploading CSV samples."
        )
        with st.form("exec_dashboard_cfo_limits"):
            fc1, fc2, fc3, fc4 = st.columns(4)
            cfo_weekly_otb = fc1.number_input(
                "Weekly OTB limit ($)",
                min_value=0.0,
                step=1000.0,
                value=float(_w_show),
                help="Rolling 7-day draft PO ceiling (cash collision threshold).",
                key="dash_cfo_weekly_otb",
            )
            cfo_min_transfer = fc2.number_input(
                "Minimum transfer value ($)",
                min_value=0.0,
                step=100.0,
                value=float(_m_show),
                help="Floor for inter-branch misallocation detection and transfer alerts.",
                key="dash_cfo_min_transfer",
            )
            toxic_po_material_threshold = fc3.number_input(
                "Toxic PO Materiality ($)",
                min_value=0.0,
                step=50.0,
                value=float(toxic_raw),
                help="Minimum dollar value to trigger a PO intercept alert (e.g. ignores $5 dead stock POs).",
                key="dash_toxic_po_materiality",
            )
            enforce_supersession = fc4.checkbox(
                "Enforce Supersession Blocks",
                value=bool(super_raw),
                help="If True, halts POs for new parts when legacy superseded parts are in stock.",
                key="dash_enforce_supersession",
            )
            if st.form_submit_button("Save CFO limits"):
                auth.upsert_client_ingestion_erp_settings(
                    client_uuid_dash,
                    float(cfo_weekly_otb),
                    float(cfo_min_transfer),
                    float(toxic_po_material_threshold),
                    bool(enforce_supersession),
                )
                st.success("CFO limits saved. The next engine run will use these values.")

        st.markdown("---")

        records = auth.get_client_metrics_history(st.session_state["client_uuid"])
        df_history = pd.DataFrame(records) if records else pd.DataFrame()

        with st.expander("🛠️ DevOps Telemetry (DigitalOcean App Platform)", expanded=True):
            st.markdown("**1. Database State Feed:**")
            try:
                st.dataframe(df_history.head(3))
                latest_row = df_history.iloc[0]
                st.write(f"**Latest DB Timestamp:** {latest_row.get('date_recorded', 'N/A')}")
                st.write(f"**Latest DB Dead Stock:** {latest_row.get('dead_stock_val', 'N/A')}")
                st.write(f"**Latest DB OTB limit:** {latest_row.get('weekly_otb_limit', 'N/A')}")
                st.write(f"**Latest DB Draft PO:** {latest_row.get('rolling_po_exposure', 'N/A')}")
            except Exception as e:
                st.error(f"Failed to fetch or parse DB history: {e}")

            st.markdown("**2. File System Pathing:**")
            base_path = os.path.dirname(os.path.abspath(__file__))
            outbound_dir = os.path.join(base_path, "netsuite_core", "data", "outbound")
            os.makedirs(outbound_dir, exist_ok=True)
            st.write(f"**Resolved Absolute Path:** `{outbound_dir}`")
            st.write(f"**Path Exists?** `{os.path.exists(outbound_dir)}`")
            if os.path.exists(outbound_dir):
                st.write(f"**Directory Contents:** `{os.listdir(outbound_dir)}`")

        if not records:
            st.info("No historical data found. Please run Manual Execution to generate your first data point.")
        else:
            numeric_cols = (
                "total_capital_deployed",
                "dead_stock_val",
                "inventory_turnover",
                "gross_margin_pct",
                "rolling_po_exposure",
                "weekly_otb_limit",
                "intercepted_cash_total",
                "suppressed_cash_total",
            )
            for col in numeric_cols:
                if col in df_history.columns:
                    df_history[col] = pd.to_numeric(df_history[col], errors="coerce").astype("float64")
            df_history["date_recorded"] = pd.to_datetime(df_history["date_recorded"])
            today = pd.Timestamp.today().normalize()
            df_filtered = df_history.copy()
            if timeframe == "Last 30 Days":
                df_filtered = df_filtered[df_filtered["date_recorded"] >= (today - pd.Timedelta(days=30))]
            elif timeframe == "Last Quarter":
                df_filtered = df_filtered[df_filtered["date_recorded"] >= (today - pd.Timedelta(days=90))]
            elif timeframe == "Year-to-Date":
                df_filtered = df_filtered[df_filtered["date_recorded"] >= pd.Timestamp(year=today.year, month=1, day=1)]
            if df_filtered.empty:
                st.info("No records found for the selected timeframe.")
                df_filtered = df_history.copy()
            # Query returns newest-first; metric cards use row 0. Charts need chronological order.
            latest = df_filtered.iloc[0]
            dead_stock_val = float(latest["dead_stock_val"])
            weekly_otb_limit = (
                0.0
                if pd.isna(pd.to_numeric(latest.get("weekly_otb_limit"), errors="coerce"))
                else float(pd.to_numeric(latest.get("weekly_otb_limit"), errors="coerce"))
            )
            total_saved = (
                float(df_filtered["intercepted_cash_total"].sum())
                if "intercepted_cash_total" in df_filtered.columns
                else 0.0
            )
            total_suppressed = (
                float(df_filtered["suppressed_cash_total"].sum())
                if "suppressed_cash_total" in df_filtered.columns
                else 0.0
            )
            roi_col1, roi_col2 = st.columns(2)
            roi_col1.metric(
                "Capital Intercepted (Saved)",
                f"${total_saved:,.2f}",
                delta_color="normal",
            )
            roi_col2.metric(
                "Opportunity Cost (Suppressed)",
                f"${total_suppressed:,.2f}",
                help="Alerts suppressed due to low materiality or CFO override posture.",
            )
            df_chart_source = df_filtered.sort_values("date_recorded")
            st.subheader(f"Latest Snapshot: {latest['date_recorded'].date()}")

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Capital Deployed", f"${float(latest['total_capital_deployed']):,.2f}")
            col2.metric("Dead Stock Value", f"${float(latest['dead_stock_val']):,.2f}")
            col3.metric("Inventory Turnover", f"{float(latest['inventory_turnover']):.2f}x")
            col4.metric("Gross Margin", f"{float(latest['gross_margin_pct']):.2f}%")

            _draft_raw = pd.to_numeric(latest.get("rolling_po_exposure"), errors="coerce")
            _weekly_raw = pd.to_numeric(latest.get("weekly_otb_limit"), errors="coerce")
            draft_po = 0.0 if pd.isna(_draft_raw) else float(_draft_raw)
            weekly_otb = 0.0 if pd.isna(_weekly_raw) else float(_weekly_raw)
            over_otb = draft_po > weekly_otb
            st.metric(
                "Draft PO Exposure",
                f"${draft_po:,.2f}",
                delta=f"Weekly OTB limit ${weekly_otb:,.2f}",
                delta_color="inverse" if over_otb else "normal",
                help="7-day rolling draft purchase order total (same definition as the compliance brief).",
            )
            if over_otb:
                st.warning(
                    "Draft PO exposure exceeds the CFO weekly OTB limit — cash collision risk. "
                    "Review open PO lines before releasing additional procurement."
                )

            st.markdown("---")
            df_chart = df_chart_source.set_index("date_recorded")[list(numeric_cols[:4])].astype("float64")
            colA, colB = st.columns(2)

            with colA:
                st.subheader("Capital vs. Dead Stock")
                st.line_chart(df_chart[["total_capital_deployed", "dead_stock_val"]])

            with colB:
                st.subheader("Turnover Efficiency")
                st.line_chart(df_chart[["inventory_turnover"]])

            st.subheader("Gross Margin %")
            st.line_chart(df_chart[["gross_margin_pct"]])

            st.subheader("Actionable Warehouse Hit-Lists")
            db_artifacts = auth.get_latest_report_artifacts(st.session_state["client_uuid"])
            if not db_artifacts:
                st.caption("No report artifacts found in PostgreSQL yet. Run the engine to generate reports.")

            st.markdown("### 🔴 Immediate Operational Directives")
            st.caption(
                "Operational directives require immediate action to protect cash flow. These files mirror the acute email alerts."
            )
            acute_found = False
            for filename, payload in db_artifacts.items():
                if "Action_Intercept" in filename or "Govnr_Executive_Brief" in filename:
                    acute_found = True
                    st.download_button(
                        label=f"Download {filename}",
                        data=payload,
                        file_name=filename,
                        mime="text/csv" if filename.endswith(".csv") else "application/pdf",
                        key=f"exec_acute_{filename}",
                    )
            if not acute_found:
                st.caption("No acute operational directive files available.")

            st.markdown("### 🛠️ Systemic Cleanup Projects")
            st.caption("Systemic projects are for scheduled data integrity maintenance and do not trigger email alerts.")
            systemic_found = False
            for filename, payload in db_artifacts.items():
                if filename.startswith("Report_"):
                    systemic_found = True
                    st.download_button(
                        label=f"Download {filename}",
                        data=payload,
                        file_name=filename,
                        mime="text/csv",
                        key=f"exec_systemic_{filename}",
                    )
            if not systemic_found:
                st.caption("No systemic cleanup project files available.")

    elif app_mode == "Onboarding Settings":
        for _k in (
            "manual_exec_buffers",
            "manual_exec_metrics",
            "manual_exec_stdout",
            "manual_exec_success",
            "manual_exec_metrics_saved",
            "manual_exec_save_error",
            "man_inv",
            "man_as",
            "man_po",
        ):
            st.session_state.pop(_k, None)
        onboarding_settings_page()

    elif app_mode == "Manual Execution":
        st.title("Manual Execution")
        tier = st.session_state.get("account_tier", "diagnostic")

        st.info(
            "Runs the Govnr engine on CSVs already placed in the secure inbound folder: "
            "**inventory_snapshot.csv**, **annual_sold.csv**, and optionally **po_activity.csv**. "
            "Use **Onboarding Settings** to map columns and SFTP patterns; set **Weekly OTB** and "
            "**minimum transfer value** on the Executive Dashboard."
        )
        with st.expander("Intraday CSV Upload (Manual Override)", expanded=False):
            inv_fu = st.file_uploader("inventory_snapshot.csv (required)", type=["csv"], key="man_inv")
            as_fu = st.file_uploader("annual_sold.csv (required)", type=["csv"], key="man_as")
            po_fu = st.file_uploader("po_activity.csv (optional)", type=["csv"], key="man_po")
            if st.button("Stage Uploaded Files", key="btn_stage_manual_uploads"):
                if inv_fu is None or as_fu is None:
                    st.error("Both inventory_snapshot.csv and annual_sold.csv are required to stage files.")
                else:
                    inbound_dir = os.path.join(NETSUITE_DIR, "data", "inbound")
                    os.makedirs(inbound_dir, exist_ok=True)
                    with open(os.path.join(inbound_dir, "inventory_snapshot.csv"), "wb") as f:
                        f.write(inv_fu.getbuffer())
                    with open(os.path.join(inbound_dir, "annual_sold.csv"), "wb") as f:
                        f.write(as_fu.getbuffer())
                    po_path = os.path.join(inbound_dir, "po_activity.csv")
                    if po_fu is not None:
                        with open(po_path, "wb") as f:
                            f.write(po_fu.getbuffer())
                    elif os.path.exists(po_path):
                        os.remove(po_path)
                    st.success("Files securely staged for execution.")
        if tier == "diagnostic":
            st.caption(
                "Diagnostic tier: place files in inbound (or save from Onboarding Settings) and run from here."
            )
        else:
            st.markdown(
                "Enterprise SFTP Pipeline: Automated 6:00 AM ingestion relies on the Port 443 Drop Zone. "
                "Ensure filename patterns match configuration."
            )

        if st.button("Run Manual Execution", type="primary", key="btn_run_health_scan"):
            from main import run_daily_governor

            st.session_state.pop("manual_exec_metrics_saved", None)
            st.session_state.pop("manual_exec_save_error", None)

            with st.spinner("Executing Govnr logic…"):
                report_buffer = io.StringIO()
                with redirect_stdout(report_buffer):
                    result = run_daily_governor(
                        client_uuid=st.session_state["client_uuid"],
                        run_tier=tier,
                        company_name=st.session_state["company_name"],
                    )

            st.session_state["manual_exec_stdout"] = report_buffer.getvalue()

            if result is None:
                st.session_state["manual_exec_success"] = False
                st.session_state.pop("manual_exec_buffers", None)
                st.session_state.pop("manual_exec_metrics", None)
            else:
                st.session_state["manual_exec_success"] = True
                st.session_state["manual_exec_buffers"] = result["buffers"]
                st.session_state["manual_exec_metrics"] = result.get("metrics")
                err = result.get("metrics_save_error")
                if err:
                    st.session_state["manual_exec_save_error"] = err
                    st.session_state["manual_exec_metrics_saved"] = False
                else:
                    st.session_state.pop("manual_exec_save_error", None)
                    st.session_state["manual_exec_metrics_saved"] = True

        # Persisted UI: survives reruns triggered by download buttons or other widgets.
        stdout_text = st.session_state.get("manual_exec_stdout")
        if stdout_text is not None:
            st.subheader("Govnr terminal output")
            st.code(stdout_text, language="markdown")

        if st.session_state.get("manual_exec_success") and st.session_state.get("manual_exec_buffers"):
            buffers = st.session_state["manual_exec_buffers"]
            metrics = st.session_state.get("manual_exec_metrics") or {}

            if metrics:
                st.subheader("Run summary (OTB)")
                intercepted = float(metrics.get("intercepted_cash_total", 0.0) or 0.0)
                suppressed = float(metrics.get("suppressed_cash_total", 0.0) or 0.0)
                col_otb1, col_otb2, col_otb3, col_otb4 = st.columns(4)
                col_otb1.metric(
                    "Weekly OTB limit (CFO)",
                    f"${float(metrics.get('weekly_otb_limit', 0)):,.2f}",
                )
                col_otb2.metric(
                    "Rolling 7-day PO exposure",
                    f"${float(metrics.get('rolling_po_total', 0)):,.2f}",
                    help="Same total as the compliance brief and CFO PDF (anchor = max PO Date_Created).",
                )
                col_otb3.metric(
                    "Capital Intercepted",
                    f"${intercepted:,.2f}",
                )
                col_otb4.metric(
                    "Opportunity Cost (Suppressed)",
                    f"${suppressed:,.2f}",
                )

                is_exception = bool(metrics.get("is_exception_state", False))
                if is_exception:
                    st.warning(
                        "⚠️ Acute Exceptions Detected: In an automated SFTP run, this state triggers immediate Email and SMS dispatch."
                    )
                else:
                    st.success(
                        "✅ Status Normal: In an automated SFTP run, outward communications would be suppressed to prevent alert fatigue."
                    )

            if st.session_state.get("manual_exec_metrics_saved"):
                st.success("Financial metrics securely logged to Govnr Vault.")
            elif st.session_state.get("manual_exec_save_error"):
                st.error(f"Internal Metrics Logging Error: {st.session_state['manual_exec_save_error']}")

            st.subheader("Generated Manual Execution deliverables")
            if st.button("Dispatch CFO Alerts (Email/SMS)", type="primary", key="btn_manual_dispatch_alerts"):
                from engine.mailer import send_outbound_email, send_outbound_sms

                client_email = auth.fetch_client_contact_email(st.session_state["client_uuid"])
                acute_attachments = {}
                for key in (
                    "Govnr_Executive_Brief.pdf",
                    "Action_Intercept_Audit_Log.csv",
                    "Report_4_warehouse_transfers.csv",
                ):
                    payload = st.session_state["manual_exec_buffers"].get(key)
                    if isinstance(payload, (bytes, bytearray)):
                        acute_attachments[key] = bytes(payload)

                send_outbound_email(
                    recipient_email=client_email,
                    subject="Govnr Manual Dispatch: Strategic Brief",
                    body_text="Manual execution complete. Review acute directives attached.",
                    attachments=acute_attachments,
                )
                send_outbound_sms("+15555555555", "Govnr Manual Run complete. Check email for acute PO intercepts.")
                st.success("Alerts successfully dispatched to CFO and Operations.")

            st.markdown("### 🔴 Immediate Operational Directives")
            acute_found = False
            for filename, payload in buffers.items():
                if "Action_Intercept" in filename or "Govnr_Executive_Brief" in filename:
                    acute_found = True
                    st.download_button(
                        label=f"Download {filename}",
                        data=payload,
                        file_name=filename,
                        mime="text/csv" if filename.endswith(".csv") else "application/pdf",
                        key=f"manual_acute_{filename}",
                    )
            if not acute_found:
                st.caption("No acute directive reports were triggered in this run.")

            st.markdown("### 🛠️ Systemic Cleanup Projects")
            systemic_found = False
            for filename, payload in buffers.items():
                if filename.startswith("Report_"):
                    systemic_found = True
                    st.download_button(
                        label=f"Download {filename}",
                        data=payload,
                        file_name=filename,
                        mime="text/csv",
                        key=f"manual_systemic_{filename}",
                    )
            if not systemic_found:
                st.caption("No systemic cleanup project reports were triggered in this run.")

        elif st.session_state.get("manual_exec_success") is False and stdout_text is not None:
            st.error(
                "Manual Execution halted. Ensure mandatory inbound files exist, mappings are saved, "
                "and required columns are present (see terminal output for MissingFileError or SchemaDriftError)."
            )
if __name__ == "__main__":
    if st.session_state.get("authenticated", False):
        main_dashboard()
    else:
        login_page()