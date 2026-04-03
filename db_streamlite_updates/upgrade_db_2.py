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
    
    # Add a column to track if the audit was run as a Free or Paid client
    cursor.execute("ALTER TABLE client_metrics_history ADD COLUMN IF NOT EXISTS run_tier VARCHAR(50) DEFAULT 'free';")
    
    conn.commit()
    cursor.close()
    conn.close()
    print("SUCCESS: 'run_tier' column added to metrics history.")
except Exception as e:
    print(f"ERROR: {e}")