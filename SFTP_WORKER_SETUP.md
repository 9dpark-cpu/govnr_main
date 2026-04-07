# Govnr Enterprise SFTP Worker Setup

This worker pulls NetSuite CSVs from client SFTP inboxes, runs the Govnr pipeline, and logs each ingestion run.

## 1) Run the schema migration once

Run this once (locally or in a one-off job) so new tables exist:

```bash
python db_init.py
```

New tables:
- `client_ingestion_config`: per-client SFTP credentials/routing and worker settings.
- `ingestion_runs`: idempotent run ledger (success/failure logs).

## 2) Add worker dependency

`requirements.txt` already includes `paramiko`.

## 3) Configure DigitalOcean App Platform worker component

Create a **Worker** component in your existing app:
- **Source dir**: repo root
- **Run command**: `python sftp_watcher.py`
- **Environment variable** (required):
  - `DATABASE_URL`
- **Optional environment variables**:
  - `SFTP_WATCHER_POLL_SECONDS` (default `60`)
  - `DB_CONNECT_TIMEOUT_SECONDS` (default `15`)

## 4) Insert one client config row

Use SQL in your DB console:

```sql
INSERT INTO client_ingestion_config (
  client_uuid,
  enabled,
  sftp_host,
  sftp_port,
  sftp_username,
  sftp_password,
  sftp_inbound_path,
  inventory_filename_pattern,
  po_filename_pattern,
  delete_remote_after_success,
  proxy_cash,
  proxy_ap
)
VALUES (
  'REPLACE_WITH_CLIENT_UUID',
  TRUE,
  'YOUR_DROPLET_IP_OR_DNS',
  22,
  'client_test_001',
  'REPLACE_WITH_SFTP_PASSWORD',
  '/inbound',
  'inventory_snapshot',
  'po_activity',
  FALSE,
  35000,
  28000
)
ON CONFLICT (client_uuid) DO UPDATE SET
  enabled = EXCLUDED.enabled,
  sftp_host = EXCLUDED.sftp_host,
  sftp_port = EXCLUDED.sftp_port,
  sftp_username = EXCLUDED.sftp_username,
  sftp_password = EXCLUDED.sftp_password,
  sftp_inbound_path = EXCLUDED.sftp_inbound_path,
  inventory_filename_pattern = EXCLUDED.inventory_filename_pattern,
  po_filename_pattern = EXCLUDED.po_filename_pattern,
  delete_remote_after_success = EXCLUDED.delete_remote_after_success,
  proxy_cash = EXCLUDED.proxy_cash,
  proxy_ap = EXCLUDED.proxy_ap,
  updated_at = CURRENT_TIMESTAMP;
```

## 5) File naming expectations

The watcher looks for newest `.csv` files containing:
- inventory pattern: `inventory_snapshot` (default)
- PO pattern: `po_activity` (default, optional)

Example:
- `inventory_snapshot_2026-04-07.csv`
- `po_activity_2026-04-07.csv`

## 6) Idempotency and retries

- A processed inventory file is tracked by SHA-256 in `ingestion_runs`.
- If the same file is seen again, it is skipped.
- Failures are logged with `status='failed'` and error text.

## 7) Security baseline

- Use one SFTP account per client.
- Keep clients in chroot jail (`/var/sftp/%u`).
- Prefer SSH keys over passwords when ready.
- Restrict Droplet firewall to required ingress only.
