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
  - `SFTP_CONNECT_RETRIES` (default `3`)
  - `DEFAULT_MAX_FILE_MB` (default `150`)
  - `<PASSWORD_ENV_VAR_NAME>` if using env-based password reference
  - `<PRIVATE_KEY_ENV_VAR_NAME>` if using env-based SSH private key reference

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
  sftp_password_env_var,
  sftp_private_key_env_var,
  sftp_inbound_path,
  inventory_filename_pattern,
  po_filename_pattern,
  delete_remote_after_success,
  proxy_cash,
  proxy_ap,
  max_file_size_mb
)
VALUES (
  'REPLACE_WITH_CLIENT_UUID',
  TRUE,
  'YOUR_DROPLET_IP_OR_DNS',
  22,
  'client_test_001',
  NULL,
  'CLIENT_TEST_001_SFTP_PASSWORD',
  'CLIENT_TEST_001_SFTP_PRIVATE_KEY',
  '/inbound',
  'inventory_snapshot',
  'po_activity',
  FALSE,
  35000,
  28000,
  150
)
ON CONFLICT (client_uuid) DO UPDATE SET
  enabled = EXCLUDED.enabled,
  sftp_host = EXCLUDED.sftp_host,
  sftp_port = EXCLUDED.sftp_port,
  sftp_username = EXCLUDED.sftp_username,
  sftp_password = EXCLUDED.sftp_password,
  sftp_password_env_var = EXCLUDED.sftp_password_env_var,
  sftp_private_key_env_var = EXCLUDED.sftp_private_key_env_var,
  sftp_inbound_path = EXCLUDED.sftp_inbound_path,
  inventory_filename_pattern = EXCLUDED.inventory_filename_pattern,
  po_filename_pattern = EXCLUDED.po_filename_pattern,
  delete_remote_after_success = EXCLUDED.delete_remote_after_success,
  proxy_cash = EXCLUDED.proxy_cash,
  proxy_ap = EXCLUDED.proxy_ap,
  max_file_size_mb = EXCLUDED.max_file_size_mb,
  updated_at = CURRENT_TIMESTAMP;
```

## 5) File naming expectations

The watcher looks for newest `.csv` files containing:
- inventory pattern: `inventory_snapshot` (default)
- PO pattern: `po_activity` (default, optional)

Example:
- `inventory_snapshot_2026-04-07.csv`
- `po_activity_2026-04-07.csv`

## 6) Idempotency, retries, and quarantine

- A processed inventory file is tracked by SHA-256 in `ingestion_runs`.
- If the same file is seen again, it is skipped.
- Failures are logged with `status='failed'` and error text.
- Oversized files are moved to `netsuite_core/data/quarantine/<client_uuid>/` and logged as `status='quarantined'`.

## 7) Security baseline

- Use one SFTP account per client.
- Keep clients in chroot jail (`/var/sftp/%u`).
- Prefer SSH keys over passwords when ready.
- Restrict Droplet firewall to required ingress only.

## 8) Admin UI: test and dry run

In the Streamlit app, open **Enterprise Ingestion Admin**:

- **Test SFTP Connection** — non-destructive login + list inbound folder.
- **Dry Run Ingestion (latest files)** — downloads newest matching CSVs to local inbound, runs the Govnr pipeline, **does not delete** remote files. A row with `status=dry_run` is written to `ingestion_runs`. The math engine may still update database metrics like a normal run.
