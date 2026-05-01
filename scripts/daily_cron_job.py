"""
Headless DigitalOcean App Platform scheduled job: run Govnr per enabled client,
email CFO the PDF + CSV buffers, destroy canonical inbound ERP extracts, log runs.

Inbound layout:
  - Preferred: netsuite_core/data/inbound/<client_uuid>/*.csv (newest match per pattern).
  - If exactly one enabled client: may use flat netsuite_core/data/inbound/*.csv
    with canonical names (inventory_snapshot.csv, annual_sold.csv, po_activity.csv).
"""
from __future__ import annotations

import hashlib
import io
import os
import stat
import shutil
import sys
import traceback
from datetime import datetime, timezone

import paramiko
import psycopg2
from psycopg2.extras import RealDictCursor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
NETSUITE_DIR = os.path.join(REPO_ROOT, "netsuite_core")
for _p in (REPO_ROOT, NETSUITE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from enterprise_ingestion_schema import apply_enterprise_ingestion_schema  # noqa: E402
from main import run_daily_governor  # noqa: E402

from engine.mailer import send_outbound_email  # noqa: E402

INBOUND_ROOT = os.path.join(NETSUITE_DIR, "data", "inbound")
RAW_ERP_CANONICAL = (
    "inventory_snapshot.csv",
    "annual_sold.csv",
    "po_activity.csv",
)
DB_CONNECT_TIMEOUT_SECONDS = int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "15"))
CRON_SOURCE = "cron_outbound"


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is required for daily_cron_job.")
    return url


def get_db_connection():
    return psycopg2.connect(
        _database_url(),
        connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
        cursor_factory=RealDictCursor,
    )


def log_ingestion_run(
    client_uuid: str,
    file_name: str,
    file_hash: str,
    status: str,
    error_message: str | None = None,
    source: str = CRON_SOURCE,
) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO ingestion_runs (client_uuid, file_name, file_hash, status, error_message, source)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (client_uuid, file_name, file_hash)
            DO UPDATE SET
                status = EXCLUDED.status,
                error_message = EXCLUDED.error_message,
                processed_at = CURRENT_TIMESTAMP
            """,
            (client_uuid, file_name, file_hash, status, error_message, source),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def fetch_enabled_cron_clients():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        apply_enterprise_ingestion_schema(cursor)
        conn.commit()
        cursor.execute(
            """
            SELECT
                cfg.client_uuid::text AS client_uuid,
                cfg.weekly_otb_limit,
                cfg.inventory_filename_pattern,
                cfg.annual_sold_filename_pattern,
                cfg.po_filename_pattern,
                cfg.sftp_host,
                cfg.sftp_port,
                cfg.sftp_username,
                cfg.sftp_password,
                cfg.sftp_password_env_var,
                cfg.sftp_private_key_env_var,
                cfg.sftp_inbound_path,
                cfg.max_file_size_mb,
                c.contact_email
            FROM client_ingestion_config cfg
            JOIN clients c ON c.client_uuid = cfg.client_uuid
            WHERE cfg.enabled = TRUE
            """
        )
        return list(cursor.fetchall())
    finally:
        cursor.close()
        conn.close()


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
    """Return filename of newest .csv in inbound_path whose name contains pattern (case-insensitive)."""
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


def _download_client_inbound_files(
    client_uuid: str,
    sftp_host: str,
    sftp_port: int,
    sftp_username: str,
    sftp_password: str | None,
    sftp_password_env_var: str | None,
    sftp_private_key_env_var: str | None,
    sftp_inbound_path: str,
    inventory_pattern: str,
    annual_sold_pattern: str,
    po_pattern: str,
    max_file_size_mb: int | None,
) -> tuple[bool, str | None]:
    inv_pat = (inventory_pattern or "inventory_snapshot").strip() or "inventory_snapshot"
    as_pat = (annual_sold_pattern or "annual_sold").strip() or "annual_sold"
    po_pat = (po_pattern or "po_activity").strip() or "po_activity"
    inbound = (sftp_inbound_path or "/inbound").strip() or "/inbound"
    max_bytes = int(max_file_size_mb or 150) * 1024 * 1024

    os.makedirs(INBOUND_ROOT, exist_ok=True)
    inv_local = os.path.join(INBOUND_ROOT, "inventory_snapshot.csv")
    as_local = os.path.join(INBOUND_ROOT, "annual_sold.csv")
    po_local = os.path.join(INBOUND_ROOT, "po_activity.csv")

    password = os.getenv((sftp_password_env_var or "").strip()) if sftp_password_env_var else sftp_password
    pkey = _load_private_key_from_env((sftp_private_key_env_var or "").strip()) if sftp_private_key_env_var else None

    transport = None
    sftp = None
    try:
        transport = paramiko.Transport((sftp_host.strip(), int(sftp_port or 22)))
        transport.connect(username=(sftp_username or "").strip(), password=password, pkey=pkey)
        sftp = paramiko.SFTPClient.from_transport(transport)

        inv_file = _sftp_newest_csv_name(sftp, inbound, inv_pat)
        if not inv_file:
            return False, f"No inventory CSV found in {inbound} matching pattern '{inv_pat}'."
        inv_remote = f"{inbound.rstrip('/')}/{inv_file}"
        with sftp.file(inv_remote, "rb") as rf:
            inv_bytes = rf.read()
        if len(inv_bytes) > max_bytes:
            return False, f"Inventory file exceeds max size ({int(max_file_size_mb or 150)} MB)."
        with open(inv_local, "wb") as f:
            f.write(inv_bytes)

        as_file = _sftp_newest_csv_name(sftp, inbound, as_pat)
        if not as_file:
            return False, f"No annual_sold CSV found in {inbound} matching pattern '{as_pat}'."
        as_remote = f"{inbound.rstrip('/')}/{as_file}"
        with sftp.file(as_remote, "rb") as rf:
            as_bytes = rf.read()
        if len(as_bytes) > max_bytes:
            return False, f"Annual sold file exceeds max size ({int(max_file_size_mb or 150)} MB)."
        with open(as_local, "wb") as f:
            f.write(as_bytes)

        po_file = _sftp_newest_csv_name(sftp, inbound, po_pat)
        if po_file:
            po_remote = f"{inbound.rstrip('/')}/{po_file}"
            with sftp.file(po_remote, "rb") as rf:
                po_bytes = rf.read()
            if len(po_bytes) > max_bytes:
                return False, f"PO file exceeds max size ({int(max_file_size_mb or 150)} MB)."
            with open(po_local, "wb") as f:
                f.write(po_bytes)
        elif os.path.exists(po_local):
            os.remove(po_local)
        return True, None
    finally:
        if sftp:
            sftp.close()
        if transport:
            transport.close()


def _run_hash(client_uuid: str) -> str:
    stamp = datetime.now(timezone.utc).isoformat()
    return hashlib.sha256(f"{client_uuid}|{stamp}".encode()).hexdigest()


def _email_attachments_from_buffers(buffers: dict) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for fname, payload in buffers.items():
        if not isinstance(fname, str):
            continue
        if not isinstance(payload, (bytes, bytearray)):
            continue
        out[fname] = bytes(payload)
    return out


def process_client_row(row: dict) -> None:
    client_uuid = row["client_uuid"]
    file_hash = _run_hash(client_uuid)
    log_key = f"govnr_daily_{datetime.now(timezone.utc).strftime('%Y%m%d')}"

    weekly = row.get("weekly_otb_limit")
    try:
        wlim = float(weekly) if weekly is not None else None
    except (TypeError, ValueError):
        wlim = None

    contact = (row.get("contact_email") or "").strip()
    inv_pat = row.get("inventory_filename_pattern") or "inventory_snapshot"
    as_pat = row.get("annual_sold_filename_pattern") or "annual_sold"
    po_pat = row.get("po_filename_pattern") or "po_activity"

    try:
        ok, fetch_err = _download_client_inbound_files(
            client_uuid=client_uuid,
            sftp_host=str(row.get("sftp_host") or ""),
            sftp_port=int(row.get("sftp_port") or 22),
            sftp_username=str(row.get("sftp_username") or ""),
            sftp_password=row.get("sftp_password"),
            sftp_password_env_var=row.get("sftp_password_env_var"),
            sftp_private_key_env_var=row.get("sftp_private_key_env_var"),
            sftp_inbound_path=str(row.get("sftp_inbound_path") or "/inbound"),
            inventory_pattern=inv_pat,
            annual_sold_pattern=as_pat,
            po_pattern=po_pat,
            max_file_size_mb=int(row.get("max_file_size_mb") or 150),
        )
        if not ok:
            log_ingestion_run(client_uuid, log_key, file_hash, "failed", fetch_err)
            print(f"[{client_uuid}] SFTP fetch failed: {fetch_err}")
            return

        if not contact:
            log_ingestion_run(
                client_uuid,
                log_key,
                file_hash,
                "failed",
                "clients.contact_email is empty; cannot deliver outbound report.",
            )
            print(f"[{client_uuid}] No contact_email on file.")
            return

        result = run_daily_governor(client_uuid, weekly_otb_limit=wlim)
        if result is None:
            log_ingestion_run(
                client_uuid,
                log_key,
                file_hash,
                "failed",
                "run_daily_governor returned no result (missing file or schema drift).",
            )
            print(f"[{client_uuid}] Pipeline returned no result.")
            return

        buffers = result.get("buffers") or {}
        attachments = _email_attachments_from_buffers(buffers)
        if not attachments:
            log_ingestion_run(client_uuid, log_key, file_hash, "failed", "No report buffers produced.")
            print(f"[{client_uuid}] Empty buffers.")
            return

        subject = os.getenv(
            "GOVNR_OUTBOUND_SUBJECT",
            f"Govnr daily strategic brief ({client_uuid[:8]}...)",
        )
        body = os.getenv(
            "GOVNR_OUTBOUND_BODY",
            "Attached: CFO strategic brief (PDF) and CSV hit-lists from today's Govnr run.\n",
        )
        send_outbound_email(contact, subject, body, attachments)
        log_ingestion_run(client_uuid, log_key, file_hash, "success", None)
        print(f"[{client_uuid}] Outbound email sent to {contact!r}.")
    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        log_ingestion_run(client_uuid, log_key, file_hash, "failed", err[:8000])
        print(f"[{client_uuid}] ERROR: {e}")
    finally:
        client_inbound_dir = os.path.join(
            REPO_ROOT, "netsuite_core", "data", "inbound", str(client_uuid)
        )
        if os.path.exists(client_inbound_dir):
            shutil.rmtree(client_inbound_dir)
            print(f"[{client_uuid}] Data destruction mandate executed. Raw files purged.")
        for name in RAW_ERP_CANONICAL:
            path = os.path.join(INBOUND_ROOT, name)
            if os.path.isfile(path):
                os.remove(path)


def main() -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] Govnr daily cron (outbound delivery) starting.")
    rows = fetch_enabled_cron_clients()
    if not rows:
        print("No enabled client_ingestion_config rows found.")
        return
    for row in rows:
        process_client_row(dict(row))
    print("Govnr daily cron finished.")


if __name__ == "__main__":
    main()
