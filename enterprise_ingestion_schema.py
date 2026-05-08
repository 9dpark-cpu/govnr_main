"""
Enterprise SFTP ingestion DDL (single source of truth).
Used by db_init.py and auth.ensure_enterprise_ingestion_tables().
"""


def apply_enterprise_ingestion_schema(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS client_ingestion_config (
            id SERIAL PRIMARY KEY,
            client_uuid UUID REFERENCES clients(client_uuid) UNIQUE,
            enabled BOOLEAN DEFAULT FALSE,
            sftp_host VARCHAR(255) NOT NULL,
            sftp_port INTEGER DEFAULT 22,
            sftp_username VARCHAR(255) NOT NULL,
            sftp_password VARCHAR(255),
            sftp_inbound_path VARCHAR(255) DEFAULT '/inbound',
            inventory_filename_pattern VARCHAR(255) DEFAULT 'inventory_snapshot',
            annual_sold_filename_pattern VARCHAR(255) DEFAULT 'annual_sold',
            po_filename_pattern VARCHAR(255) DEFAULT 'po_activity',
            delete_remote_after_success BOOLEAN DEFAULT FALSE,
            weekly_otb_limit NUMERIC(15, 2) DEFAULT 100000.00,
            min_transfer_value_threshold NUMERIC(15, 2) DEFAULT 1000.00,
            inventory_column_mapping JSONB DEFAULT '{}'::jsonb,
            annual_sold_column_mapping JSONB DEFAULT '{}'::jsonb,
            po_column_mapping JSONB DEFAULT '{}'::jsonb,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    cursor.execute(
        "ALTER TABLE client_ingestion_config ADD COLUMN IF NOT EXISTS sftp_password_env_var VARCHAR(255);"
    )
    cursor.execute(
        "ALTER TABLE client_ingestion_config ADD COLUMN IF NOT EXISTS sftp_private_key_env_var VARCHAR(255);"
    )
    cursor.execute(
        "ALTER TABLE client_ingestion_config ADD COLUMN IF NOT EXISTS max_file_size_mb INTEGER DEFAULT 150;"
    )
    cursor.execute(
        "ALTER TABLE client_ingestion_config ADD COLUMN IF NOT EXISTS weekly_otb_limit NUMERIC(15, 2) DEFAULT 100000.00;"
    )
    cursor.execute(
        "ALTER TABLE client_ingestion_config ADD COLUMN IF NOT EXISTS min_transfer_value_threshold NUMERIC(15, 2) DEFAULT 1000.00;"
    )
    cursor.execute("ALTER TABLE client_ingestion_config DROP COLUMN IF EXISTS proxy_cash;")
    cursor.execute("ALTER TABLE client_ingestion_config DROP COLUMN IF EXISTS proxy_ap;")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            id SERIAL PRIMARY KEY,
            client_uuid UUID REFERENCES clients(client_uuid),
            file_name VARCHAR(255) NOT NULL,
            file_hash VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL,
            error_message TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(client_uuid, file_name, file_hash)
        );
        """
    )
    cursor.execute(
        "ALTER TABLE ingestion_runs ADD COLUMN IF NOT EXISTS source VARCHAR(32) DEFAULT 'sftp';"
    )
    cursor.execute(
        "ALTER TABLE client_ingestion_config ADD COLUMN IF NOT EXISTS annual_sold_filename_pattern VARCHAR(255) DEFAULT 'annual_sold';"
    )
    cursor.execute(
        "ALTER TABLE client_ingestion_config ADD COLUMN IF NOT EXISTS inventory_column_mapping JSONB DEFAULT '{}'::jsonb;"
    )
    cursor.execute(
        "ALTER TABLE client_ingestion_config ADD COLUMN IF NOT EXISTS annual_sold_column_mapping JSONB DEFAULT '{}'::jsonb;"
    )
    cursor.execute(
        "ALTER TABLE client_ingestion_config ADD COLUMN IF NOT EXISTS po_column_mapping JSONB DEFAULT '{}'::jsonb;"
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS client_report_artifacts (
            id SERIAL PRIMARY KEY,
            client_uuid UUID REFERENCES clients(client_uuid),
            report_name VARCHAR(255) NOT NULL,
            report_data BYTEA NOT NULL,
            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(client_uuid, report_name)
        );
        """
    )
