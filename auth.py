from __future__ import annotations

import os
import bcrypt
import psycopg2
from psycopg2.extras import RealDictCursor
import streamlit as st
import json
from datetime import date

from enterprise_ingestion_schema import apply_enterprise_ingestion_schema


def _database_url() -> str:
    """Prefer DATABASE_URL from the environment (e.g. DigitalOcean App Platform), then Streamlit secrets."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    return st.secrets["DATABASE_URL"]


def get_db_connection():
    # connect_timeout avoids an indefinite hang (grey Streamlit overlay) when the DB is unreachable.
    return psycopg2.connect(
        _database_url(),
        connect_timeout=15,
        cursor_factory=RealDictCursor,
    )


def enterprise_ingestion_tables_exist() -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'client_ingestion_config'
            LIMIT 1
            """
        )
        return cursor.fetchone() is not None
    finally:
        cursor.close()
        conn.close()


def ensure_enterprise_ingestion_tables() -> None:
    """Idempotent: create client_ingestion_config + ingestion_runs (and columns) if missing."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        apply_enterprise_ingestion_schema(cursor)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def seed_client_ingestion_config_if_absent(client_uuid) -> None:
    """Create a default client_ingestion_config row for new clients (diagnostic-friendly defaults)."""
    ensure_enterprise_ingestion_tables()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO client_ingestion_config (
                client_uuid, enabled, sftp_host, sftp_port, sftp_username,
                sftp_inbound_path, inventory_filename_pattern, annual_sold_filename_pattern, po_filename_pattern,
                weekly_otb_limit, min_transfer_value_threshold,
                inventory_column_mapping, annual_sold_column_mapping, po_column_mapping
            )
            VALUES (
                %s, FALSE, '', 22, 'pending_setup',
                '/inbound', 'inventory_snapshot', 'annual_sold', 'po_activity',
                100000.00, 1000.00, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb
            )
            ON CONFLICT (client_uuid) DO NOTHING
            """,
            (client_uuid,),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def save_client_ingestion_financial_limits(
    client_uuid: str,
    weekly_otb_limit: float,
    min_transfer_value_threshold: float,
) -> None:
    """Persist CFO knobs only. Does not touch SFTP fields or column mappings."""
    ensure_enterprise_ingestion_tables()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO client_ingestion_config (
                client_uuid, enabled, sftp_host, sftp_port, sftp_username,
                sftp_inbound_path,
                inventory_filename_pattern, annual_sold_filename_pattern, po_filename_pattern,
                delete_remote_after_success,
                weekly_otb_limit, min_transfer_value_threshold,
                inventory_column_mapping, annual_sold_column_mapping, po_column_mapping,
                updated_at
            )
            VALUES (
                %s, FALSE, '', 22, 'pending_setup',
                '/inbound',
                'inventory_snapshot', 'annual_sold', 'po_activity',
                FALSE,
                %s, %s,
                '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT (client_uuid) DO UPDATE SET
                weekly_otb_limit = EXCLUDED.weekly_otb_limit,
                min_transfer_value_threshold = EXCLUDED.min_transfer_value_threshold,
                updated_at = CURRENT_TIMESTAMP
            """,
            (client_uuid, weekly_otb_limit, min_transfer_value_threshold),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def upsert_client_ingestion_column_mappings(
    client_uuid: str,
    inventory_filename_pattern: str,
    annual_sold_filename_pattern: str,
    po_filename_pattern: str,
    inventory_column_mapping: dict,
    annual_sold_column_mapping: dict,
    po_column_mapping: dict,
) -> None:
    """Update filename patterns and Rosetta Stone maps only. Leaves CFO limits unchanged."""
    ensure_enterprise_ingestion_tables()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE client_ingestion_config SET
                inventory_filename_pattern = %s,
                annual_sold_filename_pattern = %s,
                po_filename_pattern = %s,
                inventory_column_mapping = %s::jsonb,
                annual_sold_column_mapping = %s::jsonb,
                po_column_mapping = %s::jsonb,
                updated_at = CURRENT_TIMESTAMP
            WHERE client_uuid = %s
            """,
            (
                inventory_filename_pattern.strip() or "inventory_snapshot",
                annual_sold_filename_pattern.strip() or "annual_sold",
                po_filename_pattern.strip() or "po_activity",
                json.dumps(inventory_column_mapping or {}),
                json.dumps(annual_sold_column_mapping or {}),
                json.dumps(po_column_mapping or {}),
                client_uuid,
            ),
        )
        if cursor.rowcount == 0:
            cursor.execute(
                """
                INSERT INTO client_ingestion_config (
                    client_uuid, enabled, sftp_host, sftp_port, sftp_username,
                    sftp_inbound_path,
                    inventory_filename_pattern, annual_sold_filename_pattern, po_filename_pattern,
                    delete_remote_after_success,
                    inventory_column_mapping, annual_sold_column_mapping, po_column_mapping,
                    updated_at
                )
                VALUES (
                    %s, FALSE, '', 22, 'pending_setup',
                    '/inbound',
                    %s, %s, %s,
                    FALSE,
                    %s::jsonb, %s::jsonb, %s::jsonb,
                    CURRENT_TIMESTAMP
                )
                """,
                (
                    client_uuid,
                    inventory_filename_pattern.strip() or "inventory_snapshot",
                    annual_sold_filename_pattern.strip() or "annual_sold",
                    po_filename_pattern.strip() or "po_activity",
                    json.dumps(inventory_column_mapping or {}),
                    json.dumps(annual_sold_column_mapping or {}),
                    json.dumps(po_column_mapping or {}),
                ),
            )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def fetch_client_ingestion_row(client_uuid: str) -> dict:
    """Load full client_ingestion_config row as dict (empty dict if missing)."""
    ensure_enterprise_ingestion_tables()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT
                weekly_otb_limit,
                min_transfer_value_threshold,
                inventory_filename_pattern,
                annual_sold_filename_pattern,
                po_filename_pattern,
                inventory_column_mapping,
                annual_sold_column_mapping,
                po_column_mapping
            FROM client_ingestion_config
            WHERE client_uuid = %s
            """,
            (client_uuid,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        return dict(row)
    finally:
        cursor.close()
        conn.close()

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def register_new_client(company_name: str, email: str, password: str):
    prefix = "".join(e for e in company_name if e.isalnum()).lower()
    hashed_pw = hash_password(password)

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO clients (company_name, contact_email, password_hash, inbound_email_prefix)
            VALUES (%s, %s, %s, %s)
            RETURNING client_uuid, inbound_email_prefix;
        """, (company_name, email, hashed_pw, prefix))

        new_client = cursor.fetchone()
        conn.commit()
        try:
            seed_client_ingestion_config_if_absent(new_client["client_uuid"])
        except Exception:
            pass
        return True, new_client
    except psycopg2.IntegrityError:
        if conn:
            conn.rollback()
        return False, "Error: Email or Company name already exists in our system."
    except Exception as e:
        if conn:
            conn.rollback()
        return False, f"Database error: {str(e)}"
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

def login_client(email: str, password: str):
    try:
        conn = get_db_connection()
    except Exception as e:
        return False, f"Database connection failed: {e}"

    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT client_uuid, company_name, password_hash, account_tier, inventory_column_mapping, inbound_email_prefix
            FROM clients WHERE contact_email = %s
        """, (email,))
        user = cursor.fetchone()
        cursor.close()

        if user and verify_password(password, user["password_hash"]):
            return True, user
        return False, "Invalid email or password."
    finally:
        conn.close()

def save_column_mapping(client_uuid: str, mapping_dict: dict):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE clients 
            SET inventory_column_mapping = %s 
            WHERE client_uuid = %s
        """, (json.dumps(mapping_dict), client_uuid))
        conn.commit()
    except Exception as e:
        print(f"Failed to save mapping: {e}")
    finally:
        cursor.close()
        conn.close()

def get_client_metrics_history(client_uuid: str) -> list:
    """
    Fetch CFO dashboard history for a client. Newest snapshot always first.
    Intentionally uncached — dashboards must reflect the latest DB state immediately.
    """
    ensure_client_metrics_otb_columns()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT date_recorded, total_capital_deployed, dead_stock_val,
                   inventory_turnover, gross_margin_pct, avg_gmroi,
                   rolling_po_exposure, weekly_otb_limit
            FROM client_metrics_history
            WHERE client_uuid = %s
            ORDER BY date_recorded DESC NULLS LAST
            """,
            (client_uuid,),
        )
        return list(cursor.fetchall())
    finally:
        cursor.close()
        conn.close()


def ensure_client_metrics_otb_columns() -> None:
    """Migrate older DBs: rolling PO exposure and weekly OTB ceiling on client_metrics_history."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "ALTER TABLE client_metrics_history ADD COLUMN IF NOT EXISTS rolling_po_exposure NUMERIC(15, 2);"
        )
        cursor.execute(
            "ALTER TABLE client_metrics_history ADD COLUMN IF NOT EXISTS weekly_otb_limit NUMERIC(15, 2);"
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def save_cfo_metrics(
    client_uuid: str,
    run_tier: str,
    total_cap: float,
    turnover: float,
    margin: float,
    confidence_tier: str,
    *,
    dead_stock_value: float,
    rolling_po_exposure: float,
    weekly_otb_ceiling: float,
):
    ensure_client_metrics_otb_columns()
    conn = get_db_connection()
    cursor = conn.cursor()
    today = date.today()
    try:
        cursor.execute("""
            INSERT INTO client_metrics_history 
            (client_uuid, date_recorded, run_tier, total_capital_deployed, dead_stock_val, inventory_turnover,
             gross_margin_pct, confidence_tier, rolling_po_exposure, weekly_otb_limit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (client_uuid, date_recorded)
            DO UPDATE SET
                run_tier = EXCLUDED.run_tier,
                total_capital_deployed = EXCLUDED.total_capital_deployed,
                inventory_turnover = EXCLUDED.inventory_turnover,
                gross_margin_pct = EXCLUDED.gross_margin_pct,
                confidence_tier = EXCLUDED.confidence_tier,
                dead_stock_val = EXCLUDED.dead_stock_val,
                rolling_po_exposure = EXCLUDED.rolling_po_exposure,
                weekly_otb_limit = EXCLUDED.weekly_otb_limit;
        """, (
            client_uuid,
            today,
            run_tier,
            total_cap,
            dead_stock_value,
            turnover,
            margin,
            confidence_tier,
            rolling_po_exposure,
            weekly_otb_ceiling,
        ))
        conn.commit()
    except Exception as e:
        print(f"Failed to log metrics: {e}")
    finally:
        cursor.close()
        conn.close()