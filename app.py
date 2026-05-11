import streamlit as st

st.set_page_config(
    page_title="Govnr",
    layout="wide",
    initial_sidebar_state="expanded",
)

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
import plotly.graph_objects as go

# 1. Defensively add your logic folder to Python's brain
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NETSUITE_DIR = os.path.join(BASE_DIR, "netsuite_core")
if NETSUITE_DIR not in sys.path:
    sys.path.insert(0, NETSUITE_DIR)

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
                    st.session_state["govnr_nav_module"] = "Executive Dashboard"
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
        "**Weekly procurement ceiling** and **minimum transfer threshold** are saved on the **Executive Dashboard**."
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
    inbound_dir = os.path.join(NETSUITE_DIR, "data", "inbound")
    inv_disk_path = os.path.join(inbound_dir, "inventory_snapshot.csv")
    as_disk_path = os.path.join(inbound_dir, "annual_sold.csv")
    po_disk_path = os.path.join(inbound_dir, "po_activity.csv")

    with st.form("mapping_upload_form", clear_on_submit=True):
        st.subheader("Sample files for mapping (pre-flight validation only)")
        inv_fu = st.file_uploader("inventory_snapshot.csv (required)", type=["csv"], key="erp_fu_inv")
        as_fu = st.file_uploader("annual_sold.csv (required)", type=["csv"], key="erp_fu_as")
        po_fu = st.file_uploader("po_activity.csv (optional)", type=["csv"], key="erp_fu_po")
        if inv_fu is None or as_fu is None:
            st.info("Upload **inventory** and **annual_sold** sample exports, then click the button below.")
        submitted = st.form_submit_button("Upload & Validate Mappings", type="primary")

    if submitted:
        if inv_fu is None or as_fu is None:
            st.error("Both inventory_snapshot and annual_sold CSVs are required to validate.")
        else:
            os.makedirs(inbound_dir, exist_ok=True)
            with open(inv_disk_path, "wb") as f:
                f.write(inv_fu.getbuffer())
            with open(as_disk_path, "wb") as f:
                f.write(as_fu.getbuffer())
            if po_fu is not None:
                with open(po_disk_path, "wb") as f:
                    f.write(po_fu.getbuffer())
            elif os.path.exists(po_disk_path):
                os.remove(po_disk_path)
            try:
                pl.read_csv(inv_disk_path, infer_schema_length=10000, null_values=["", " ", "NA", "null"])
                pl.read_csv(as_disk_path, infer_schema_length=10000, null_values=["", " ", "NA", "null"])
                if os.path.exists(po_disk_path):
                    pl.read_csv(po_disk_path, infer_schema_length=10000, null_values=["", " ", "NA", "null"])
                st.session_state["erp_mapping_files_ready"] = True
                st.success("Files staged and validated. Configure mappings below, then save.")
            except Exception as e:
                st.session_state["erp_mapping_files_ready"] = False
                st.error(f"CSV validation failed: {e}")

    files_ready = bool(st.session_state.get("erp_mapping_files_ready")) and os.path.isfile(inv_disk_path) and os.path.isfile(
        as_disk_path
    )
    if not files_ready:
        st.info("Stage inventory and annual_sold CSVs above to enable column mapping.")
        return

    cfg = fetch_ingestion_config(client_uuid)
    raw_inv = pl.read_csv(inv_disk_path, infer_schema_length=10000, null_values=["", " ", "NA", "null"])
    raw_as = pl.read_csv(as_disk_path, infer_schema_length=10000, null_values=["", " ", "NA", "null"])
    raw_po = (
        pl.read_csv(po_disk_path, infer_schema_length=10000, null_values=["", " ", "NA", "null"])
        if os.path.isfile(po_disk_path)
        else None
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

    with st.form("mapping_save_form"):
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

        save_mapping_submitted = st.form_submit_button("Save Mapping Configuration", type="primary")

    if save_mapping_submitted:

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

        st.success(
            "Column mappings and filename patterns saved to client_ingestion_config. "
            "Staged inbound CSVs remain in netsuite_core/data/inbound for Manual Execution."
        )


def _render_executive_dashboard():
    """Enterprise SaaS layout: single column, spacing, typography tokens; native Streamlit only."""
    st.markdown(
        """
<style>
    /* 1. Main block — wide layout, institutional padding */
    .block-container {
        padding-top: 2rem !important;
        padding-bottom: 2rem !important;
        max-width: 95% !important;
    }

    /* 2. S&P / Capital IQ style metric cards */
    [data-testid="stMetric"] {
        background-color: #FFFFFF;
        border: 1px solid #E2E8F0;
        border-radius: 6px;
        padding: 1rem;
        box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
    }

    /* Metric labels (e.g. "WEEKLY PROCUREMENT") */
    [data-testid="stMetricLabel"] > div {
        color: #64748B !important;
        font-size: 0.75rem !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 0.05em !important;
    }

    /* Metric values (e.g. "$206k") */
    [data-testid="stMetricValue"] > div {
        color: #0F172A !important;
        font-size: 1.8rem !important;
        font-weight: 600 !important;
        font-family: "Helvetica Neue", Helvetica, Arial, sans-serif !important;
    }

    /* Metric delta (e.g. "Ceiling: $120k") */
    [data-testid="stMetricDelta"] > div {
        font-size: 0.85rem !important;
        font-weight: 500 !important;
    }

    /* 3. Alert contrast (status tier) — :contains() is not valid CSS; use structure + text color */
    [data-testid="stAlert"] {
        border-radius: 4px;
        font-weight: 600;
        border-left: 4px solid;
    }
    [data-testid="stAlert"] [data-testid="stMarkdownContainer"],
    [data-testid="stAlert"] [data-testid="stMarkdownContainer"] p,
    [data-testid="stAlert"] [data-testid="stMarkdownContainer"] span {
        color: #0F172A !important;
    }
    /* st.error — cash collision / breach (red shell, legible body) */
    div[data-testid="stAlert"][kind="error"],
    div[data-testid="stAlert"][data-baseweb="notification"][kind="error"] {
        background-color: #FEF2F2 !important;
        border-left-color: #DC2626 !important;
    }
    div[data-testid="stAlert"][kind="error"] [data-testid="stMarkdownContainer"] p,
    div[data-testid="stAlert"][kind="error"] [data-testid="stMarkdownContainer"] span {
        color: #991B1B !important;
    }
    /* st.warning */
    div[data-testid="stAlert"][kind="warning"] {
        background-color: #FFFBEB !important;
        border-left-color: #D97706 !important;
    }
    div[data-testid="stAlert"][kind="warning"] [data-testid="stMarkdownContainer"] p,
    div[data-testid="stAlert"][kind="warning"] [data-testid="stMarkdownContainer"] span {
        color: #92400E !important;
    }
    /* st.success */
    div[data-testid="stAlert"][kind="success"] {
        background-color: #ECFDF5 !important;
        border-left-color: #16A34A !important;
    }
    div[data-testid="stAlert"][kind="success"] [data-testid="stMarkdownContainer"] p,
    div[data-testid="stAlert"][kind="success"] [data-testid="stMarkdownContainer"] span {
        color: #166534 !important;
    }

    /* 4. Expander / governance */
    [data-testid="stExpander"] {
        background-color: #FFFFFF !important;
        border: 1px solid #E2E8F0 !important;
        border-radius: 6px !important;
    }
    [data-testid="stExpander"] summary p {
        font-weight: 600 !important;
        color: #0F172A !important;
    }

    /* 5. Section titles — beats main_dashboard section.main h3 (same specificity, later cascade) */
    section.main h3 {
        color: #004385 !important;
        font-weight: 600 !important;
        font-size: 1.1rem !important;
        margin-bottom: -0.5rem !important;
        border-bottom: 1px solid #E2E8F0;
        padding-bottom: 0.5rem;
    }

    /* Custom callouts (raw HTML in this view) */
    .govnr-exec-soft-info {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 24px 28px;
        color: #334155;
        font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
        font-size: 14px;
        line-height: 1.55;
        margin-bottom: 1rem;
    }
    .govnr-exec-soft-ok {
        background: #ecfdf5;
        border: 1px solid #bbf7d0;
        border-radius: 12px;
        padding: 20px 24px;
        color: #166534;
        font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
        font-size: 14px;
        margin-bottom: 1rem;
    }
</style>
        """,
        unsafe_allow_html=True,
    )

    def _gap():
        st.markdown(
            '<div style="height:2.5rem;min-height:32px" aria-hidden="true"></div>',
            unsafe_allow_html=True,
        )

    def _exec_transfer_df(artifacts: dict) -> pd.DataFrame:
        for name, payload in (artifacts or {}).items():
            if "Report_4" in name and str(name).lower().endswith(".csv"):
                try:
                    return pd.read_csv(io.BytesIO(payload))
                except Exception:
                    return pd.DataFrame()
        return pd.DataFrame()

    def _exec_tile_label(filename: str) -> str:
        base = filename.rsplit("/", 1)[-1]
        stem = base.replace("_", " ")
        low = stem.lower()
        if low.endswith(".pdf"):
            return f"{stem[:-4]} (PDF)"
        if low.endswith(".csv"):
            return f"{stem[:-4]} (CSV)"
        return base

    def _usd_pulse(n: float) -> str:
        n = float(n or 0.0)
        ax = abs(n)
        if ax >= 1_000_000:
            return f"${n / 1_000_000:.2f}M"
        if ax >= 1_000:
            return f"${n / 1_000:,.0f}k".replace(",", "")
        return f"${n:,.0f}"

    def _partition_audit_artifacts(
        artifacts: dict,
    ) -> tuple[list[tuple[str, bytes]], list[tuple[str, bytes]], list[tuple[str, bytes]]]:
        """Route vault files into 3-pillar locker: acute intercepts, Report_* ledger CSVs, executive PDFs."""
        acute: list[tuple[str, bytes]] = []
        systemic: list[tuple[str, bytes]] = []
        brief: list[tuple[str, bytes]] = []
        for fname, payload in sorted((artifacts or {}).items()):
            if not isinstance(payload, (bytes, bytearray)):
                continue
            raw = bytes(payload)
            low = fname.lower()
            base = fname.rsplit("/", 1)[-1]
            if low.endswith(".pdf") or "executive_brief" in low:
                brief.append((fname, raw))
            elif "action_intercept" in low:
                acute.append((fname, raw))
            elif low.endswith(".csv") and base.lower().startswith("report_"):
                systemic.append((fname, raw))
            elif low.endswith(".csv"):
                systemic.append((fname, raw))
        return acute, systemic, brief

    EXEC_CARD_BG = "#FFFFFF"

    st.markdown("# Executive Dashboard")
    if st.session_state.pop("exec_governance_applied", False):
        st.markdown(
            '<div class="govnr-exec-soft-ok">Governance rules saved to <strong>client_ingestion_config</strong>. '
            "Pulse and the action queue use the updated thresholds.</div>",
            unsafe_allow_html=True,
        )
    st.caption("CFO view — procurement integrity, inactive capital, and inter-branch optimization.")

    timeframe = st.selectbox(
        "History window",
        ["Last 30 Days", "Last Quarter", "Year-to-Date", "All Time"],
        key="exec_timeframe",
    )

    client_uuid_dash = st.session_state["client_uuid"]
    from engine.erp_config_loader import (
        fetch_ingestion_config,
        resolve_enforce_supersession,
        resolve_toxic_po_material_threshold,
        resolve_min_transfer_value_threshold,
        resolve_weekly_otb_limit,
    )
    from engine.netsuite_parser import ingest_po_activity
    from engine.logic_cash import compute_rolling_po_exposure_total, rolling_po_orders_in_window

    _cfg_dash = fetch_ingestion_config(client_uuid_dash)
    _w_show, _ = resolve_weekly_otb_limit(_cfg_dash.get("weekly_otb_limit"))
    _m_show, _ = resolve_min_transfer_value_threshold(_cfg_dash.get("min_transfer_value_threshold"))
    toxic_raw, _ = resolve_toxic_po_material_threshold(_cfg_dash.get("toxic_po_material_threshold"))
    super_raw = resolve_enforce_supersession(_cfg_dash.get("enforce_supersession"))
    weekly_ceiling_resolved, _ = resolve_weekly_otb_limit(_cfg_dash.get("weekly_otb_limit"))

    records = auth.get_client_metrics_history(client_uuid_dash)
    df_history = pd.DataFrame(records) if records else pd.DataFrame()
    db_artifacts = auth.get_latest_report_artifacts(client_uuid_dash)

    df_xfer = _exec_transfer_df(db_artifacts)
    xfer_n = (
        int(df_xfer["Item_Identifier"].nunique())
        if not df_xfer.empty and "Item_Identifier" in df_xfer.columns
        else 0
    )

    df_po_live = ingest_po_activity(client_uuid_dash, erp_config=_cfg_dash)
    draft_live = None if df_po_live.is_empty() else compute_rolling_po_exposure_total(df_po_live)
    po_window_pl = rolling_po_orders_in_window(df_po_live) if not df_po_live.is_empty() else None

    if not records:
        latest = None
        df_filtered = pd.DataFrame()
        draft_po = float(draft_live) if draft_live is not None else 0.0
        weekly_ceiling = float(weekly_ceiling_resolved)
        xfer_opportunity_count = xfer_n
        t_imb = 0
        c_int = 0
        inactive_cap = 0.0
        turnover_x = 0.0
        intercepted = 0.0
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
            "otb_breach_amount",
        )
        for col in numeric_cols:
            if col in df_history.columns:
                df_history[col] = pd.to_numeric(df_history[col], errors="coerce")
        for col in ("transfer_imbalance_items", "cost_integrity_items", "otb_breach_count"):
            if col in df_history.columns:
                df_history[col] = pd.to_numeric(df_history[col], errors="coerce").fillna(0).astype(int)

        df_history["date_recorded"] = pd.to_datetime(df_history["date_recorded"])
        today = pd.Timestamp.today().normalize()
        df_filtered = df_history.copy()
        if timeframe == "Last 30 Days":
            df_filtered = df_filtered[df_filtered["date_recorded"] >= (today - pd.Timedelta(days=30))]
        elif timeframe == "Last Quarter":
            df_filtered = df_filtered[df_filtered["date_recorded"] >= (today - pd.Timedelta(days=90))]
        elif timeframe == "Year-to-Date":
            df_filtered = df_filtered[
                df_filtered["date_recorded"] >= pd.Timestamp(year=today.year, month=1, day=1)
            ]
        if df_filtered.empty:
            df_filtered = df_history.copy()

        latest = df_filtered.iloc[0]
        draft_po = float(draft_live) if draft_live is not None else float(
            pd.to_numeric(latest.get("rolling_po_exposure"), errors="coerce") or 0.0
        )
        weekly_ceiling = float(weekly_ceiling_resolved)

        t_imb = int(latest.get("transfer_imbalance_items") or 0)
        c_int = int(latest.get("cost_integrity_items") or 0)
        xfer_opportunity_count = max(xfer_n, t_imb)

        inactive_cap = float(pd.to_numeric(latest.get("dead_stock_val"), errors="coerce") or 0.0)
        turnover_x = float(pd.to_numeric(latest.get("inventory_turnover"), errors="coerce") or 0.0)
        intercepted = float(pd.to_numeric(latest.get("intercepted_cash_total"), errors="coerce") or 0.0)

    cash_collision_risk = weekly_ceiling > 0 and draft_po > weekly_ceiling
    over_otb = cash_collision_risk

    # --- Level 1: Immediate alerts (status) ---
    if cash_collision_risk:
        st.error(
            "🚨 CASH COLLISION DETECTED: Draft PO exposure exceeds the Weekly Procurement Ceiling."
        )
    if not records:
        st.warning(
            "**Optimization required** — Run Manual Execution (or your scheduled sync) to generate the first "
            "snapshot. Pulse metrics populate after the first successful engine run."
        )
    elif not cash_collision_risk:
        if xfer_opportunity_count > 0 or c_int > 0:
            st.warning(
                "**Optimization required** — Non-urgent flags (inter-warehouse transfers or integrity items). "
                "Review the action queue when convenient."
            )
        else:
            st.success(
                "**All systems nominal** — No procurement limit breach. Latest snapshot is within the active ceiling."
            )

    _gap()

    # --- Level 2: Executive metrics (pulse) ---
    with st.container(border=True):
        st.caption("Executive pulse — rolling 7-day draft exposure, capital at risk, and intercept posture.")
        c_m1, c_m2, c_m3, c_m4 = st.columns(4)
        ceiling_delta = f"Ceiling: {_usd_pulse(weekly_ceiling)}" if weekly_ceiling > 0 else None
        c_m1.metric("Weekly procurement", _usd_pulse(draft_po), delta=ceiling_delta)
        c_m2.metric("Dead stock risk", _usd_pulse(inactive_cap))
        c_m3.metric("Inv. turnover", f"{turnover_x:.2f}x")
        c_m4.metric("Capital intercepted", _usd_pulse(intercepted))

    _gap()

    # --- Level 3: Governance & limits (control knobs) ---
    with st.expander("⚙️ Procurement Governance & Alert Thresholds", expanded=False):
        st.caption(
            "Values persist in PostgreSQL **client_ingestion_config** and apply on the next engine run. "
            "Saving refreshes procurement math on this screen."
        )
        with st.form("cfo_limits_form"):
            gov_ceiling = st.number_input(
                "Weekly Procurement Ceiling ($) (formerly Weekly OTB limit)",
                min_value=0.0,
                step=1000.0,
                value=float(_w_show),
                help="Rolling 7-day draft PO ceiling.",
                key="cfo_weekly_procurement_ceiling",
            )
            gov_min_xfer = st.number_input(
                "Inter-Warehouse Liquidity Threshold ($) (formerly Min transfer value)",
                min_value=0.0,
                step=100.0,
                value=float(_m_show),
                help="Minimum economic size for inter-warehouse liquidity moves.",
                key="cfo_inter_wh_liquidity_threshold",
            )
            gov_stagnant = st.number_input(
                "Stagnant Procurement Threshold ($) (formerly Toxic PO materiality)",
                min_value=0.0,
                step=50.0,
                value=float(toxic_raw),
                help="Materiality floor for stagnant / dead-stock procurement intercepts.",
                key="cfo_stagnant_procurement_threshold",
            )
            gov_super = st.checkbox(
                "Enforce supersession blocks",
                value=bool(super_raw),
                help="Block new part POs when superseded legacy stock is available.",
                key="cfo_enforce_supersession",
            )
            submitted_cfo = st.form_submit_button("Update Governance Rules", type="primary")
    if submitted_cfo:
        auth.upsert_client_ingestion_erp_settings(
            client_uuid_dash,
            float(gov_ceiling),
            float(gov_min_xfer),
            float(gov_stagnant),
            bool(gov_super),
        )
        st.session_state["exec_governance_applied"] = True
        st.rerun()

    _gap()

    # --- 3-Pillar Audit Locker (native downloads; below governance) ---
    st.subheader("Audit locker")
    st.caption("Latest engine artifacts from PostgreSQL — acute intercepts, ledger audits, and executive briefing.")
    if not db_artifacts:
        st.info("No artifacts in PostgreSQL yet. Run **Manual Execution** (or scheduled ingestion) to populate the locker.")

    _acute_art, _systemic_art, _brief_art = _partition_audit_artifacts(db_artifacts)
    c_aud1, c_aud2, c_aud3 = st.columns(3)
    with c_aud1:
        st.markdown("### 🔴 Alert Intercepts (Acute Action required).")
        if not _acute_art:
            st.caption("No acute intercept exports in vault.")
        for i, (fname, payload) in enumerate(_acute_art):
            st.download_button(
                label=f"Download {_exec_tile_label(fname)}",
                data=payload,
                file_name=fname,
                mime="text/csv" if fname.lower().endswith(".csv") else "application/octet-stream",
                key=f"audit_p3_acute_{i}_{hashlib.sha256(fname.encode()).hexdigest()[:10]}",
                use_container_width=True,
                type="primary",
            )
    with c_aud2:
        st.markdown("### 🛠️ Systemic Cleanup (Ledger Audits).")
        if not _systemic_art:
            st.caption("No systemic ledger audit CSVs in vault.")
        for i, (fname, payload) in enumerate(_systemic_art):
            st.download_button(
                label=f"Download {_exec_tile_label(fname)}",
                data=payload,
                file_name=fname,
                mime="text/csv",
                key=f"audit_p3_sys_{i}_{hashlib.sha256(fname.encode()).hexdigest()[:10]}",
                use_container_width=True,
                type="secondary",
            )
    with c_aud3:
        st.markdown("### 📅 Executive Brief (Monthly PDF).")
        if not _brief_art:
            st.caption("No executive briefing PDF in vault.")
        for i, (fname, payload) in enumerate(_brief_art):
            st.download_button(
                label=f"Download {_exec_tile_label(fname)}",
                data=payload,
                file_name=fname,
                mime="application/pdf" if fname.lower().endswith(".pdf") else "application/octet-stream",
                key=f"audit_p3_brf_{i}_{hashlib.sha256(fname.encode()).hexdigest()[:10]}",
                use_container_width=True,
                type="primary",
            )

    _gap()

    if records:
        with st.container(border=True):
            st.markdown(
                '<div style="font-size:18px;font-weight:700;color:#0F172A;margin-bottom:20px;'
                'font-family:Helvetica,Arial,sans-serif;">Trends</div>',
                unsafe_allow_html=True,
            )
            cap_hi = (
                max(weekly_ceiling * 1.15, draft_po * 1.1, 1.0)
                if weekly_ceiling > 0
                else max(draft_po * 1.1, 1.0)
            )
            ref_line = weekly_ceiling if weekly_ceiling > 0 else 0.0
            _ind_kw = dict(
                mode="number+gauge+delta" if ref_line > 0 else "number+gauge",
                value=draft_po,
                number={
                    "prefix": "$",
                    "valueformat": ",.0f",
                    "font": {"size": 18, "color": "#0F172A"},
                },
                gauge={
                    "shape": "bullet",
                    "axis": {
                        "range": [0, cap_hi],
                        "tickcolor": "#94a3b8",
                        "tickwidth": 1,
                        "tickfont": {"size": 11, "color": "#64748b"},
                        "showticklabels": True,
                    },
                    "threshold": {
                        "line": {"color": "#2563eb", "width": 1},
                        "thickness": 0.85,
                        "value": ref_line if ref_line > 0 else cap_hi,
                    },
                    "bgcolor": "#e2e8f0",
                    "bar": {"color": "#1e40af"},
                },
                title={"text": ""},
            )
            if ref_line > 0:
                _ind_kw["delta"] = {
                    "reference": ref_line,
                    "valueformat": ",.0f",
                    "increasing": {"color": "#64748B"},
                    "decreasing": {"color": "#64748B"},
                }
            fig_bullet = go.Figure(go.Indicator(**_ind_kw))
            fig_bullet.update_layout(
                height=160,
                margin=dict(l=16, r=16, t=24, b=16),
                paper_bgcolor=EXEC_CARD_BG,
                plot_bgcolor=EXEC_CARD_BG,
                font=dict(color="#64748B", family="Helvetica, Arial, sans-serif"),
                xaxis=dict(showgrid=False, zeroline=False, visible=False),
                yaxis=dict(showgrid=False, zeroline=False, visible=False),
            )
            st.plotly_chart(fig_bullet, use_container_width=True, config={"displayModeBar": False})
            _gap()

            dh = df_filtered.sort_values("date_recorded")[["date_recorded", "dead_stock_val"]].copy()
            dh["month"] = dh["date_recorded"].dt.strftime("%Y-%m")
            agg_dead = dh.groupby("month", as_index=False).last()
            fig_dead = go.Figure(
                data=[
                    go.Bar(
                        x=agg_dead["month"],
                        y=agg_dead["dead_stock_val"],
                        name="Inactive capital",
                        marker_color="#64748b",
                        width=0.45,
                    )
                ]
            )
            fig_dead.update_layout(
                height=160,
                margin=dict(l=12, r=12, t=16, b=12),
                showlegend=False,
                paper_bgcolor=EXEC_CARD_BG,
                plot_bgcolor=EXEC_CARD_BG,
                font=dict(color="#64748B", family="Helvetica, Arial, sans-serif"),
            )
            fig_dead.update_xaxes(showgrid=False, zeroline=False, tickfont=dict(size=11, color="#64748b"))
            fig_dead.update_yaxes(
                showgrid=False,
                zeroline=False,
                showticklabels=True,
                tickfont=dict(size=11, color="#64748b"),
                title_text=None,
            )
            st.plotly_chart(fig_dead, use_container_width=True, config={"displayModeBar": False})
            _gap()

            turn_hist = df_filtered.sort_values("date_recorded")
            fig_spark = go.Figure(
                go.Scatter(
                    x=turn_hist["date_recorded"],
                    y=turn_hist["inventory_turnover"],
                    mode="lines",
                    line=dict(width=2, color="#2563eb"),
                )
            )
            fig_spark.update_xaxes(visible=False, showgrid=False, zeroline=False)
            fig_spark.update_yaxes(visible=False, showgrid=False, zeroline=False)
            fig_spark.update_layout(
                height=88,
                margin=dict(l=8, r=8, t=8, b=8),
                plot_bgcolor=EXEC_CARD_BG,
                paper_bgcolor=EXEC_CARD_BG,
                font=dict(color="#64748B", family="Helvetica, Arial, sans-serif"),
            )
            st.plotly_chart(fig_spark, use_container_width=True, config={"displayModeBar": False})
    else:
        st.markdown(
            '<div class="govnr-exec-soft-info">No historical ledger rows yet. Run <strong>Manual Execution</strong> '
            "(or your scheduled sync) to populate trend charts.</div>",
            unsafe_allow_html=True,
        )

    _gap()
    po_breakdown_open = bool(st.session_state.get("exec_open_po_breakdown")) or (
        bool(latest is not None) and over_otb
    )

    collision_rows = []
    if po_window_pl is not None and not po_window_pl.is_empty():
        pdf = po_window_pl.to_pandas()
        for _, r in pdf.iterrows():
            amt = float(r.get("Order_Total_Amount") or 0.0)
            if weekly_ceiling > 0 and amt > weekly_ceiling:
                collision_rows.append(r)
        if over_otb and not collision_rows:
            pdf2 = pdf.sort_values("Order_Total_Amount", ascending=False).head(12)
            collision_rows = [pdf2.iloc[i] for i in range(len(pdf2))]

    show_po_cards = bool(collision_rows) and (
        over_otb
        or any(
            float(r.get("Order_Total_Amount") or 0) > weekly_ceiling
            for r in collision_rows
            if weekly_ceiling > 0
        )
    )

    xfer_cards_df = pd.DataFrame()
    if not df_xfer.empty and "Item_Identifier" in df_xfer.columns:
        loc_col = "Item_Location" if "Item_Location" in df_xfer.columns else None
        val_col = "Transfer_Capital_Value" if "Transfer_Capital_Value" in df_xfer.columns else None
        if loc_col and val_col:
            xfer_cards_df = (
                df_xfer.groupby("Item_Identifier", as_index=False)
                .agg(
                    locations=(loc_col, lambda s: " · ".join(sorted({str(x) for x in s.dropna().unique()}))),
                    transfer_value=(val_col, "sum"),
                )
                .sort_values("transfer_value", ascending=False)
            )

    queue_empty = not show_po_cards and xfer_cards_df.empty

    st.subheader("Immediate action queue")
    st.caption("Decision-ready items only; threshold-filtered noise reduction.")
    _gap()

    if over_otb and queue_empty:
        st.error(
            "**Limited PO detail** — Procurement limit breach is active on rolling exposure, but there are no "
            "PO rows or transfer lines to show. Stage **po_activity.csv** and refresh, or run the engine to "
            "repopulate artifacts."
        )
    elif queue_empty:
        st.success("**Procurement strategy optimized** — No action required.")
    else:
        if show_po_cards:
            st.markdown(
                '<div style="font-size:18px;font-weight:700;color:#0F172A;margin:24px 0 12px 0;">'
                "Procurement limit breach — orders</div>",
                unsafe_allow_html=True,
            )
            exp_po = st.expander(
                "PO exposure breakdown (filtered table)",
                expanded=po_breakdown_open,
            )
            with exp_po:
                if po_window_pl is not None and not po_window_pl.is_empty():
                    ptab = po_window_pl.to_pandas()
                    _sty = (
                        ptab.style.map(
                            lambda v: "color: #b91c1c; font-weight: 600"
                            if isinstance(v, (int, float)) and weekly_ceiling > 0 and float(v) > weekly_ceiling
                            else "",
                            subset=["Order_Total_Amount"],
                        )
                        if "Order_Total_Amount" in ptab.columns
                        else ptab.style
                    )
                    st.dataframe(_sty, use_container_width=True, hide_index=True)
                else:
                    st.caption("Stage **po_activity.csv** to enumerate PO-level exposure.")

            st.caption(
                "Govnr surfaces alerts only — execute decisions in your ERP or workflow tools."
            )
            _gap()
            for _idx, row in enumerate(collision_rows):
                po_num = str(row.get("PO_Number", ""))
                val = float(row.get("Order_Total_Amount") or 0.0)
                vendor = str(row.get("Purchase_Order_Vendor", "") or "—")
                po_label = po_num if po_num else f"PO row {_idx + 1}"
                st.metric(label=po_label, value=f"${val:,.2f}", help=vendor)
                _gap()

        if not xfer_cards_df.empty:
            st.markdown(
                '<div style="font-size:18px;font-weight:700;color:#0F172A;margin:24px 0 12px 0;">'
                "Inter-branch opportunities</div>",
                unsafe_allow_html=True,
            )
            for j, xr in xfer_cards_df.head(20).iterrows():
                item = str(xr.get("Item_Identifier", ""))
                locs = str(xr.get("locations", ""))
                tv = float(xr.get("transfer_value") or 0.0)
                xfer_label = f"Part {item}" if item else f"Transfer line {j}"
                st.metric(label=xfer_label, value=f"${tv:,.2f}", help=f"Locations: {locs}")
                _gap()

    _gap()
    with st.expander("Diagnostics & historical ledger", expanded=False):
        st.caption("Integrity audit exports and raw metric history for engineering review.")
        if records:
            st.dataframe(df_history.head(12), use_container_width=True, hide_index=True)
        base_path = os.path.dirname(os.path.abspath(__file__))
        outbound_dir = os.path.join(base_path, "netsuite_core", "data", "outbound")
        st.markdown(
            f'<p style="color:#334155;font-size:14px;">Outbound path: <code>{outbound_dir}</code> '
            f"(exists: {os.path.exists(outbound_dir)})</p>",
            unsafe_allow_html=True,
        )


def main_dashboard():
    # Binary buffers are never persisted in session state; keep this key clean across reruns.
    st.session_state.pop("manual_exec_buffers", None)
    st.markdown(
        """
<style>
[data-testid="stAppViewContainer"] .main {
    background-color: #F8FAFC !important;
}
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0f172a 0%, #1e293b 100%) !important;
    border-right: none !important;
}
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] span:not([data-baseweb="tag"]),
[data-testid="stSidebar"] label {
    color: #e2e8f0 !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] span {
    color: #f8fafc !important;
}
section.main h1, section.main h2, section.main h3 {
    color: #1E293B !important;
}
section.main [data-testid="stCaption"] {
    color: #64748B !important;
}
</style>
        """,
        unsafe_allow_html=True,
    )
    st.sidebar.title(f"{st.session_state['company_name']}")
    if st.sidebar.button("Log Out"):
        st.session_state["authenticated"] = False
        st.session_state["client_uuid"] = None
        st.session_state["company_name"] = None
        st.rerun()
        
    st.sidebar.markdown("---")
    st.sidebar.title("Govnr Navigation")
    def _on_nav_module_change() -> None:
        """Drop transient manual terminal output when leaving Manual Execution."""
        new = st.session_state.get("govnr_nav_module")
        old = st.session_state.get("_govnr_nav_previous")
        if old == "Manual Execution" and new != "Manual Execution":
            st.session_state.pop("manual_exec_stdout", None)
        st.session_state["_govnr_nav_previous"] = new

    if "_govnr_nav_previous" not in st.session_state:
        st.session_state["_govnr_nav_previous"] = st.session_state.get(
            "govnr_nav_module", "Executive Dashboard"
        )

    _nav_modules = ("Executive Dashboard", "Onboarding Settings", "Manual Execution")
    if st.session_state.get("govnr_nav_module") not in _nav_modules:
        st.session_state["govnr_nav_module"] = "Executive Dashboard"

    app_mode = st.sidebar.selectbox(
        "Select Module",
        list(_nav_modules),
        key="govnr_nav_module",
        on_change=_on_nav_module_change,
    )

    if app_mode == "Executive Dashboard":
        _render_executive_dashboard()

    elif app_mode == "Onboarding Settings":
        onboarding_settings_page()

    elif app_mode == "Manual Execution":
        st.title("Manual Execution")
        tier = st.session_state.get("account_tier", "diagnostic")

        st.info(
            "Runs the Govnr engine on CSVs already placed in the secure inbound folder: "
            "**inventory_snapshot.csv**, **annual_sold.csv**, and optionally **po_activity.csv**. "
            "Use **Onboarding Settings** to map columns and SFTP patterns; set **weekly procurement ceiling** and "
            "**minimum transfer threshold** on the Executive Dashboard."
        )
        with st.expander("Intraday CSV Upload (Manual Override)", expanded=False):
            with st.form("intraday_upload_form", clear_on_submit=True):
                inv_fu = st.file_uploader("inventory_snapshot.csv (required)", type=["csv"], key="man_inv")
                as_fu = st.file_uploader("annual_sold.csv (required)", type=["csv"], key="man_as")
                po_fu = st.file_uploader("po_activity.csv (optional)", type=["csv"], key="man_po")
                submitted = st.form_submit_button("Stage Uploaded Files")
            if submitted:
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
                    st.success("Files securely staged. Browser memory flushed.")
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
                st.session_state.pop("manual_exec_metrics", None)
            else:
                st.session_state["manual_exec_success"] = True
                st.session_state.pop("manual_exec_buffers", None)
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

        if st.session_state.get("manual_exec_success"):
            buffers = auth.get_latest_report_artifacts(st.session_state["client_uuid"])
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
            if st.button("🚀 Dispatch CFO Alerts (Email/SMS)", type="primary", key="btn_manual_dispatch_alerts"):
                from engine.mailer import send_outbound_email, send_outbound_sms

                client_email = auth.fetch_client_contact_email(st.session_state["client_uuid"])
                acute_attachments = {}
                db_artifacts = auth.get_latest_report_artifacts(st.session_state["client_uuid"])
                for key in (
                    "Action_Intercept_Audit_Log.csv",
                    "Report_4_warehouse_transfers.csv",
                ):
                    payload = db_artifacts.get(key)
                    if isinstance(payload, (bytes, bytearray)):
                        acute_attachments[key] = bytes(payload)

                send_outbound_email(
                    recipient_email=client_email,
                    subject="Govnr Manual Dispatch: Acute CSV Hit-Lists",
                    body_text="Manual execution complete. Review the attached intercept and transfer CSV reports.",
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

    st.session_state["_govnr_nav_previous"] = app_mode


if __name__ == "__main__":
    if st.session_state.get("authenticated", False):
        main_dashboard()
    else:
        login_page()