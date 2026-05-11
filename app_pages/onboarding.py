"""Onboarding Settings: SFTP automation and ERP column mapping."""
from __future__ import annotations

import hashlib
import io
import os
import stat
from contextlib import redirect_stdout
from typing import Optional

import pandas as pd
import paramiko
import polars as pl
import streamlit as st

import auth

from app_pages.paths import NETSUITE_DIR

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
                st.success("Files staged and validated. Configure mappings below, then save.")
            except Exception as e:
                st.error(f"CSV validation failed: {e}")

    files_ready = os.path.isfile(inv_disk_path) and os.path.isfile(as_disk_path)
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

