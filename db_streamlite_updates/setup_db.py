import psycopg2
import toml
import os

# 1. Read the secret connection string
secrets_path = os.path.join(".streamlit", "secrets.toml")
with open(secrets_path, "r") as f:
    secrets = toml.load(f)
    
db_url = secrets.get("DATABASE_URL")

print("Connecting to DigitalOcean Postgres...")

# 2. Connect to the database
try:
    conn = psycopg2.connect(db_url)
    cursor = conn.cursor()
    print("Connection successful! Building tables...")

    # 3. Create the Clients Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            client_uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_name VARCHAR(255) NOT NULL,
            contact_email VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            inbound_email_prefix VARCHAR(50) UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 4. Create the Metrics History Table (The Data Moat)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS client_metrics_history (
            id SERIAL PRIMARY KEY,
            client_uuid UUID REFERENCES clients(client_uuid),
            date_recorded DATE NOT NULL,
            total_capital_deployed NUMERIC(15, 2),
            inventory_turnover NUMERIC(5, 2),
            gross_margin_pct NUMERIC(5, 2),
            avg_gmroi NUMERIC(5, 2),
            dead_stock_val NUMERIC(15, 2),
            obsolete_stock_val NUMERIC(15, 2),
            cash_balance NUMERIC(15, 2),
            UNIQUE(client_uuid, date_recorded)
        );
    """)

    # 5. Save and close
    conn.commit()
    cursor.close()
    conn.close()
    print("SUCCESS: Govnr Database schema created. You are ready for users.")

except Exception as e:
    print(f"ERROR: Could not complete setup. Details: {e}")