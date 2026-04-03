import psycopg2
import toml
import os

secrets_path = os.path.join(".streamlit", "secrets.toml")
with open(secrets_path, "r") as f:
    secrets = toml.load(f)

email_to_upgrade = input("Enter the email of the account to upgrade to PAID: ")

try:
    conn = psycopg2.connect(secrets.get("DATABASE_URL"))
    cursor = conn.cursor()
    
    # Update the tier in the database
    cursor.execute("UPDATE clients SET account_tier = 'paid' WHERE contact_email = %s RETURNING company_name;", (email_to_upgrade,))
    result = cursor.fetchone()
    
    if result:
        print(f"SUCCESS: {result[0]} has been upgraded to the Premium Tier!")
        conn.commit()
    else:
        print("Error: Could not find an account with that email. Did you type it correctly?")
        
    cursor.close()
    conn.close()
except Exception as e:
    print(f"Database error: {e}")