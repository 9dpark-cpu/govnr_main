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

def save_cfo_metrics(client_uuid: str, run_tier: str, total_cap: float, dead_val: float, turnover: float, margin: float, confidence_tier: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    today = date.today()
    try:
        cursor.execute("""
            INSERT INTO client_metrics_history 
            (client_uuid, date_recorded, run_tier, total_capital_deployed, dead_stock_val, inventory_turnover, gross_margin_pct, confidence_tier)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (client_uuid, date_recorded) 
            DO UPDATE SET 
                run_tier = EXCLUDED.run_tier,
                total_capital_deployed = EXCLUDED.total_capital_deployed,
                dead_stock_val = EXCLUDED.dead_stock_val,
                inventory_turnover = EXCLUDED.inventory_turnover,
                gross_margin_pct = EXCLUDED.gross_margin_pct,
                confidence_tier = EXCLUDED.confidence_tier;
        """, (client_uuid, today, run_tier, total_cap, dead_val, turnover, margin, confidence_tier))
        conn.commit()
    except Exception as e:
        print(f"Failed to log metrics: {e}")
    finally:
        cursor.close()
        conn.close()