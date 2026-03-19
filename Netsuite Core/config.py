import os
from dotenv import load_dotenv

# --- CORE PATHS ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INBOUND_DIR = os.path.join(BASE_DIR, "data", "inbound")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")

#  Force Python to look for the .env file exactly in this root folder
env_path = os.path.join(BASE_DIR, '.env')
load_dotenv(dotenv_path=env_path)

# --- CREDENTIALS ---
QBO_CLIENT_ID = os.getenv("QBO_CLIENT_ID", "").strip()
QBO_CLIENT_SECRET = os.getenv("QBO_CLIENT_SECRET", "").strip()
QBO_REALM_ID = os.getenv("QBO_REALM_ID", "").strip()
QBO_REFRESH_TOKEN = os.getenv("QBO_REFRESH_TOKEN", "").strip()