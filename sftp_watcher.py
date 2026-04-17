import hashlib
import os
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from typing import Optional

import paramiko
import psycopg2
from psycopg2.extras import RealDictCursor


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NETSUITE_DIR = os.path.join(BASE_DIR, "netsuite_core")
if NETSUITE_DIR not in sys.path:
    sys.path.insert(0, NETSUITE_DIR)

from enterprise_ingestion_schema import apply_enterprise_ingestion_schema
from main import run_daily_governor


POLL_SECONDS = int(os.getenv("SFTP_WATCHER_POLL_SECONDS", "60"))
DB_CONNECT_TIMEOUT_SECONDS = int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "15"))
SFTP_CONNECT_RETRIES = int(os.getenv("SFTP_CONNECT_RETRIES", "3"))
DEFAULT_MAX_FILE_MB = int(os.getenv("DEFAULT_MAX_FILE_MB", "150"))


@dataclass
class ClientConfig:
    client_uuid: str
    sftp_host: str
    sftp_port: int
    sftp_username: str
    sftp_password: Optional[str]
    sftp_password_env_var: Optional[str]
    sftp_private_key_env_var: Optional[str]
    sftp_inbound_path: str
    inventory_filename_pattern: str
    annual_sold_filename_pattern: str
    po_filename_pattern: str
    delete_remote_after_success: bool
    weekly_otb_limit: float
    max_file_size_mb: int


def get_db_connection():
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        raise RuntimeError("DATABASE_URL is required for sftp_watcher.")
    return psycopg2.connect(
        db_url,
        connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
        cursor_factory=RealDictCursor,
    )


def fetch_enabled_configs():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        apply_enterprise_ingestion_schema(cursor)
        conn.commit()
        cursor.execute(
            """
            SELECT
                c.client_uuid::text AS client_uuid,
                cfg.sftp_host,
                cfg.sftp_port,
                cfg.sftp_username,
                cfg.sftp_password,
                cfg.sftp_password_env_var,
                cfg.sftp_private_key_env_var,
                cfg.sftp_inbound_path,
                cfg.inventory_filename_pattern,
                cfg.annual_sold_filename_pattern,
                cfg.po_filename_pattern,
                cfg.delete_remote_after_success,
                cfg.weekly_otb_limit,
                cfg.max_file_size_mb
            FROM client_ingestion_config cfg
            JOIN clients c ON c.client_uuid = cfg.client_uuid
            WHERE cfg.enabled = TRUE
            """
        )
        rows = cursor.fetchall()
        configs = []
        for row in rows:
            configs.append(
                ClientConfig(
                    client_uuid=row["client_uuid"],
                    sftp_host=row["sftp_host"],
                    sftp_port=int(row.get("sftp_port") or 22),
                    sftp_username=row["sftp_username"],
                    sftp_password=row.get("sftp_password"),
                    sftp_password_env_var=row.get("sftp_password_env_var"),
                    sftp_private_key_env_var=row.get("sftp_private_key_env_var"),
                    sftp_inbound_path=row.get("sftp_inbound_path") or "/inbound",
                    inventory_filename_pattern=row.get("inventory_filename_pattern") or "inventory_snapshot",
                    annual_sold_filename_pattern=row.get("annual_sold_filename_pattern") or "annual_sold",
                    po_filename_pattern=row.get("po_filename_pattern") or "po_activity",
                    delete_remote_after_success=bool(row.get("delete_remote_after_success")),
                    weekly_otb_limit=float(row.get("weekly_otb_limit") or 100000.0),
                    max_file_size_mb=int(row.get("max_file_size_mb") or DEFAULT_MAX_FILE_MB),
                )
            )
        return configs
    finally:
        cursor.close()
        conn.close()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def has_been_processed(client_uuid: str, file_name: str, file_hash: str) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT 1
            FROM ingestion_runs
            WHERE client_uuid = %s AND file_name = %s AND file_hash = %s AND status = 'success'
            LIMIT 1
            """,
            (client_uuid, file_name, file_hash),
        )
        return cursor.fetchone() is not None
    finally:
        cursor.close()
        conn.close()


def log_ingestion_run(
    client_uuid: str,
    file_name: str,
    file_hash: str,
    status: str,
    error_message: Optional[str] = None,
    source: str = "sftp",
):
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


def _resolve_password(cfg: ClientConfig) -> Optional[str]:
    if cfg.sftp_password_env_var:
        return os.getenv(cfg.sftp_password_env_var)
    return cfg.sftp_password


def _resolve_private_key(cfg: ClientConfig):
    if not cfg.sftp_private_key_env_var:
        return None
    key_data = os.getenv(cfg.sftp_private_key_env_var, "").strip()
    if not key_data:
        return None
    for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_cls.from_private_key(StringIO(key_data.replace("\\n", "\n")))
        except Exception:
            continue
    raise RuntimeError(f"Invalid private key in env var '{cfg.sftp_private_key_env_var}'.")


def connect_sftp(cfg: ClientConfig):
    transport = paramiko.Transport((cfg.sftp_host, cfg.sftp_port))
    pkey = _resolve_private_key(cfg)
    password = _resolve_password(cfg)
    transport.connect(username=cfg.sftp_username, password=password, pkey=pkey)
    sftp = paramiko.SFTPClient.from_transport(transport)
    return transport, sftp


def newest_matching_file(sftp: paramiko.SFTPClient, inbound_path: str, pattern: str):
    candidates = []
    for entry in sftp.listdir_attr(inbound_path):
        if not stat.S_ISREG(entry.st_mode):
            continue
        if pattern.lower() in entry.filename.lower() and entry.filename.lower().endswith(".csv"):
            candidates.append((entry.st_mtime, entry.filename, entry.st_size))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0]


def read_remote_file_bytes(sftp: paramiko.SFTPClient, remote_path: str) -> bytes:
    with sftp.file(remote_path, "rb") as remote_file:
        return remote_file.read()


def write_local_inbound(file_name: str, file_bytes: bytes):
    inbound_dir = os.path.join(NETSUITE_DIR, "data", "inbound")
    os.makedirs(inbound_dir, exist_ok=True)
    target_path = os.path.join(inbound_dir, file_name)
    with open(target_path, "wb") as f:
        f.write(file_bytes)


def quarantine_file(client_uuid: str, file_name: str, file_bytes: bytes, reason: str):
    safe_reason = reason.replace(" ", "_").replace("/", "_")
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    quarantine_dir = os.path.join(NETSUITE_DIR, "data", "quarantine", client_uuid)
    os.makedirs(quarantine_dir, exist_ok=True)
    file_path = os.path.join(quarantine_dir, f"{stamp}_{file_name}")
    reason_path = os.path.join(quarantine_dir, f"{stamp}_{file_name}.reason.txt")
    with open(file_path, "wb") as f:
        f.write(file_bytes)
    with open(reason_path, "w", encoding="utf-8") as f:
        f.write(f"Reason: {safe_reason}\nOriginal file: {file_name}\n")


def process_client_config(cfg: ClientConfig):
    print(f"[{datetime.utcnow().isoformat()}Z] Polling SFTP for client {cfg.client_uuid}")
    transport = None
    sftp = None
    try:
        last_error = None
        for _ in range(SFTP_CONNECT_RETRIES):
            try:
                transport, sftp = connect_sftp(cfg)
                last_error = None
                break
            except Exception as conn_err:
                last_error = conn_err
                time.sleep(2)
        if last_error:
            raise last_error

        inv_match = newest_matching_file(sftp, cfg.sftp_inbound_path, cfg.inventory_filename_pattern)
        inv_file = inv_match[1] if inv_match else None
        if not inv_file:
            print(f"[{cfg.client_uuid}] No inventory file found in {cfg.sftp_inbound_path}")
            return

        as_match = newest_matching_file(sftp, cfg.sftp_inbound_path, cfg.annual_sold_filename_pattern)
        as_file = as_match[1] if as_match else None
        if not as_file:
            msg = (
                f"No annual_sold CSV found in {cfg.sftp_inbound_path} "
                f"matching pattern “{cfg.annual_sold_filename_pattern}”."
            )
            print(f"[{cfg.client_uuid}] {msg}")
            log_ingestion_run(cfg.client_uuid, "annual_sold_missing", "nohash", "failed", msg)
            return

        po_match = newest_matching_file(sftp, cfg.sftp_inbound_path, cfg.po_filename_pattern)
        po_file = po_match[1] if po_match else None

        inv_remote_path = f"{cfg.sftp_inbound_path.rstrip('/')}/{inv_file}"
        inv_bytes = read_remote_file_bytes(sftp, inv_remote_path)
        inv_hash = sha256_bytes(inv_bytes)
        max_bytes = cfg.max_file_size_mb * 1024 * 1024
        if len(inv_bytes) > max_bytes:
            reason = f"inventory file exceeds max_file_size_mb={cfg.max_file_size_mb}"
            quarantine_file(cfg.client_uuid, inv_file, inv_bytes, reason)
            log_ingestion_run(cfg.client_uuid, inv_file, inv_hash, "quarantined", reason)
            print(f"[{cfg.client_uuid}] Quarantined {inv_file}: {reason}")
            return
        if has_been_processed(cfg.client_uuid, inv_file, inv_hash):
            log_ingestion_run(cfg.client_uuid, inv_file, inv_hash, "skipped", "duplicate file hash")
            print(f"[{cfg.client_uuid}] Already processed inventory file: {inv_file}")
            return

        as_remote_path = f"{cfg.sftp_inbound_path.rstrip('/')}/{as_file}"
        as_bytes = read_remote_file_bytes(sftp, as_remote_path)
        if len(as_bytes) > max_bytes:
            as_hash = sha256_bytes(as_bytes)
            reason = f"annual_sold file exceeds max_file_size_mb={cfg.max_file_size_mb}"
            quarantine_file(cfg.client_uuid, as_file, as_bytes, reason)
            log_ingestion_run(cfg.client_uuid, as_file, as_hash, "quarantined", reason)
            print(f"[{cfg.client_uuid}] Quarantined {as_file}: {reason}")
            return

        write_local_inbound("inventory_snapshot.csv", inv_bytes)
        write_local_inbound("annual_sold.csv", as_bytes)

        po_local_path = os.path.join(NETSUITE_DIR, "data", "inbound", "po_activity.csv")
        if po_file:
            po_remote_path = f"{cfg.sftp_inbound_path.rstrip('/')}/{po_file}"
            po_bytes = read_remote_file_bytes(sftp, po_remote_path)
            if len(po_bytes) > max_bytes:
                po_hash = sha256_bytes(po_bytes)
                reason = f"po file exceeds max_file_size_mb={cfg.max_file_size_mb}"
                quarantine_file(cfg.client_uuid, po_file, po_bytes, reason)
                log_ingestion_run(cfg.client_uuid, po_file, po_hash, "quarantined", reason)
                print(f"[{cfg.client_uuid}] Quarantined {po_file}: {reason}")
                return
            write_local_inbound("po_activity.csv", po_bytes)
        else:
            po_bytes = b""
            po_remote_path = None
            if os.path.exists(po_local_path):
                os.remove(po_local_path)

        pipeline_result = run_daily_governor(
            client_uuid=cfg.client_uuid,
            weekly_otb_limit=cfg.weekly_otb_limit,
        )
        if pipeline_result is None:
            log_ingestion_run(
                cfg.client_uuid,
                inv_file,
                inv_hash,
                "failed",
                "Govnr engine halted (missing mandatory file, schema drift, or internal error).",
            )
            print(f"[{cfg.client_uuid}] Engine run failed after download.")
            return

        # Mark success using inventory hash as the primary idempotency signal.
        log_ingestion_run(cfg.client_uuid, inv_file, inv_hash, "success")
        print(f"[{cfg.client_uuid}] Processed inventory file successfully: {inv_file}")

        if cfg.delete_remote_after_success:
            sftp.remove(inv_remote_path)
            if po_remote_path:
                po_hash = sha256_bytes(po_bytes)
                log_ingestion_run(cfg.client_uuid, po_file, po_hash, "success")
                sftp.remove(po_remote_path)
            print(f"[{cfg.client_uuid}] Remote files deleted after successful processing.")
    except Exception as e:
        msg = str(e)
        print(f"[{cfg.client_uuid}] ERROR: {msg}")
        try:
            # The inventory values may not be available on early failures, so use placeholders.
            log_ingestion_run(cfg.client_uuid, "unknown", "unknown", "failed", msg)
        except Exception as log_err:
            print(f"[{cfg.client_uuid}] Failed to write ingestion_runs log: {log_err}")
    finally:
        if sftp:
            sftp.close()
        if transport:
            transport.close()


def main():
    print("Govnr SFTP watcher started.")
    print(f"Poll interval: {POLL_SECONDS} seconds")
    while True:
        try:
            configs = fetch_enabled_configs()
            if not configs:
                print("No enabled client_ingestion_config rows found.")
            for cfg in configs:
                process_client_config(cfg)
        except Exception as e:
            print(f"[Watcher Error] {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
