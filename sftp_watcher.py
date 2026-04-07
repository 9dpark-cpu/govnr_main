import hashlib
import io
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import paramiko
import psycopg2
from psycopg2.extras import RealDictCursor


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NETSUITE_DIR = os.path.join(BASE_DIR, "netsuite_core")
if NETSUITE_DIR not in sys.path:
    sys.path.insert(0, NETSUITE_DIR)

from main import run_daily_governor


POLL_SECONDS = int(os.getenv("SFTP_WATCHER_POLL_SECONDS", "60"))
DB_CONNECT_TIMEOUT_SECONDS = int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "15"))


@dataclass
class ClientConfig:
    client_uuid: str
    sftp_host: str
    sftp_port: int
    sftp_username: str
    sftp_password: Optional[str]
    sftp_inbound_path: str
    inventory_filename_pattern: str
    po_filename_pattern: str
    delete_remote_after_success: bool
    proxy_cash: float
    proxy_ap: float


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
        cursor.execute(
            """
            SELECT
                c.client_uuid::text AS client_uuid,
                cfg.sftp_host,
                cfg.sftp_port,
                cfg.sftp_username,
                cfg.sftp_password,
                cfg.sftp_inbound_path,
                cfg.inventory_filename_pattern,
                cfg.po_filename_pattern,
                cfg.delete_remote_after_success,
                cfg.proxy_cash,
                cfg.proxy_ap
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
                    sftp_inbound_path=row.get("sftp_inbound_path") or "/inbound",
                    inventory_filename_pattern=row.get("inventory_filename_pattern") or "inventory_snapshot",
                    po_filename_pattern=row.get("po_filename_pattern") or "po_activity",
                    delete_remote_after_success=bool(row.get("delete_remote_after_success")),
                    proxy_cash=float(row.get("proxy_cash") or 35000.0),
                    proxy_ap=float(row.get("proxy_ap") or 28000.0),
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


def log_ingestion_run(client_uuid: str, file_name: str, file_hash: str, status: str, error_message: Optional[str] = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO ingestion_runs (client_uuid, file_name, file_hash, status, error_message)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (client_uuid, file_name, file_hash)
            DO UPDATE SET
                status = EXCLUDED.status,
                error_message = EXCLUDED.error_message,
                processed_at = CURRENT_TIMESTAMP
            """,
            (client_uuid, file_name, file_hash, status, error_message),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def connect_sftp(cfg: ClientConfig):
    transport = paramiko.Transport((cfg.sftp_host, cfg.sftp_port))
    transport.connect(username=cfg.sftp_username, password=cfg.sftp_password)
    sftp = paramiko.SFTPClient.from_transport(transport)
    return transport, sftp


def newest_matching_file(sftp: paramiko.SFTPClient, inbound_path: str, pattern: str) -> Optional[str]:
    candidates = []
    for entry in sftp.listdir_attr(inbound_path):
        if pattern.lower() in entry.filename.lower() and entry.filename.lower().endswith(".csv"):
            candidates.append((entry.st_mtime, entry.filename))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def read_remote_file_bytes(sftp: paramiko.SFTPClient, remote_path: str) -> bytes:
    with sftp.file(remote_path, "rb") as remote_file:
        return remote_file.read()


def write_local_inbound(file_name: str, file_bytes: bytes):
    inbound_dir = os.path.join(NETSUITE_DIR, "data", "inbound")
    os.makedirs(inbound_dir, exist_ok=True)
    target_path = os.path.join(inbound_dir, file_name)
    with open(target_path, "wb") as f:
        f.write(file_bytes)


def process_client_config(cfg: ClientConfig):
    print(f"[{datetime.utcnow().isoformat()}Z] Polling SFTP for client {cfg.client_uuid}")
    transport = None
    sftp = None
    try:
        transport, sftp = connect_sftp(cfg)
        inv_file = newest_matching_file(sftp, cfg.sftp_inbound_path, cfg.inventory_filename_pattern)
        if not inv_file:
            print(f"[{cfg.client_uuid}] No inventory file found in {cfg.sftp_inbound_path}")
            return

        po_file = newest_matching_file(sftp, cfg.sftp_inbound_path, cfg.po_filename_pattern)

        inv_remote_path = f"{cfg.sftp_inbound_path.rstrip('/')}/{inv_file}"
        inv_bytes = read_remote_file_bytes(sftp, inv_remote_path)
        inv_hash = sha256_bytes(inv_bytes)
        if has_been_processed(cfg.client_uuid, inv_file, inv_hash):
            print(f"[{cfg.client_uuid}] Already processed inventory file: {inv_file}")
            return

        write_local_inbound("inventory_snapshot.csv", inv_bytes)
        if po_file:
            po_remote_path = f"{cfg.sftp_inbound_path.rstrip('/')}/{po_file}"
            po_bytes = read_remote_file_bytes(sftp, po_remote_path)
            write_local_inbound("po_activity.csv", po_bytes)
        else:
            po_bytes = b""
            po_remote_path = None

        run_daily_governor(
            client_uuid=cfg.client_uuid,
            proxy_cash=cfg.proxy_cash,
            proxy_ap=cfg.proxy_ap,
        )

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
