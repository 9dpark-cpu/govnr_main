import psycopg2
import toml
import os

secrets_path = os.path.join(".streamlit", "secrets.toml")
with open(secrets_path, "r") as f:
    secrets = toml.load(f)

print("Connecting to DigitalOcean Postgres...")
try:
    conn = psycopg2.connect(secrets.get("DATABASE_URL"))
    cursor = conn.cursor()
    
    # Add the Data Integrity Shield
    cursor.execute("ALTER TABLE client_metrics_history ADD COLUMN IF NOT EXISTS confidence_tier VARCHAR(50) DEFAULT 'Tier A';")
    
    conn.commit()
    cursor.close()
    conn.close()
    print("SUCCESS: 'confidence_tier' column added. The IP Moat is now shielded.")
except Exception as e:
    print(f"ERROR: {e}")