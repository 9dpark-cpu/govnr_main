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
import os
import shutil
import sys
import traceback
from datetime import datetime, timezone

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


def _newest_matching_csv(directory: str, pattern: str) -> tuple[str, str] | None:
    """Return (full_path, basename) for newest .csv in directory whose name contains pattern (case-insensitive)."""
    if not os.path.isdir(directory):
        return None
    candidates: list[tuple[float, str, str]] = []
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or not name.lower().endswith(".csv"):
            continue
        if pattern.lower() not in name.lower():
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        candidates.append((mtime, path, name))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, path, name = candidates[0]
    return path, name


def _copy_to_canonical(src_path: str, dest_name: str) -> None:
    dest = os.path.join(INBOUND_ROOT, dest_name)
    os.makedirs(INBOUND_ROOT, exist_ok=True)
    shutil.copy2(src_path, dest)


def stage_inbound_for_client(
    client_uuid: str,
    inventory_pattern: str,
    annual_pattern: str,
    po_pattern: str,
    single_enabled_client: bool,
) -> tuple[bool, str | None]:
    """
    Populate netsuite_core/data/inbound/{canonical csv names} from per-client staging
    or from flat inbound when only one enabled client.
    """
    os.makedirs(INBOUND_ROOT, exist_ok=True)
    client_dir = os.path.join(INBOUND_ROOT, client_uuid)

    inv_pat = (inventory_pattern or "inventory_snapshot").strip() or "inventory_snapshot"
    as_pat = (annual_pattern or "annual_sold").strip() or "annual_sold"
    po_pat = (po_pattern or "po_activity").strip() or "po_activity"

    if os.path.isdir(client_dir):
        inv = _newest_matching_csv(client_dir, inv_pat)
        ass = _newest_matching_csv(client_dir, as_pat)
        if not inv:
            return False, f"No inventory CSV in {client_dir} matching pattern {inv_pat!r}."
        if not ass:
            return False, f"No annual_sold CSV in {client_dir} matching pattern {as_pat!r}."
        _copy_to_canonical(inv[0], "inventory_snapshot.csv")
        _copy_to_canonical(ass[0], "annual_sold.csv")
        po = _newest_matching_csv(client_dir, po_pat)
        po_dest = os.path.join(INBOUND_ROOT, "po_activity.csv")
        if po:
            _copy_to_canonical(po[0], "po_activity.csv")
        elif os.path.isfile(po_dest):
            os.remove(po_dest)
        return True, None

    if single_enabled_client:
        inv_path = os.path.join(INBOUND_ROOT, "inventory_snapshot.csv")
        as_path = os.path.join(INBOUND_ROOT, "annual_sold.csv")
        if os.path.isfile(inv_path) and os.path.isfile(as_path):
            return True, None
        return (
            False,
            "Single-client flat inbound expected "
            f"{inv_path} and {as_path}; missing files or create {client_dir}/ for isolation.",
        )

    return False, f"Missing per-client inbound directory: {client_dir}"


def _destroy_raw_erp_canonical() -> None:
    for name in RAW_ERP_CANONICAL:
        path = os.path.join(INBOUND_ROOT, name)
        if os.path.isfile(path):
            os.remove(path)


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


def process_client_row(row: dict, single_enabled_client: bool) -> None:
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
        ok, stage_err = stage_inbound_for_client(
            client_uuid, inv_pat, as_pat, po_pat, single_enabled_client
        )
        if not ok:
            log_ingestion_run(client_uuid, log_key, file_hash, "failed", stage_err)
            print(f"[{client_uuid}] Staging failed: {stage_err}")
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
        _destroy_raw_erp_canonical()


def main() -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] Govnr daily cron (outbound delivery) starting.")
    rows = fetch_enabled_cron_clients()
    if not rows:
        print("No enabled client_ingestion_config rows found.")
        return
    single = len(rows) == 1
    for row in rows:
        process_client_row(dict(row), single_enabled_client=single)
    print("Govnr daily cron finished.")


if __name__ == "__main__":
    main()
