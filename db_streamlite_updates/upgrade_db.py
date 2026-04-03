import psycopg2
import toml
import os

secrets_path = os.path.join(".streamlit", "secrets.toml")
with open(secrets_path, "r") as f:
    secrets = toml.load(f)

print("Connecting to DigitalOcean Postgres to apply infrastructure upgrades...")
try:
    conn = psycopg2.connect(secrets.get("DATABASE_URL"))
    cursor = conn.cursor()
    
    # 1. Add the Tier Gate
    cursor.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS account_tier VARCHAR(50) DEFAULT 'free';")
    
    # 2. Add the Mapping Memory Bank
    cursor.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS inventory_column_mapping JSONB;")
    
    conn.commit()
    cursor.close()
    conn.close()
    print("SUCCESS: Database upgraded. 'account_tier' and 'inventory_column_mapping' added.")
except Exception as e:
    print(f"ERROR: {e}")