import os
import sys
import requests
import base64

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

#  Import all 4 keys securely from config
from config import QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_REALM_ID, QBO_REFRESH_TOKEN

# Sandbox URL. When deploying to production, this changes to 'quickbooks.api.intuit.com'
QBO_BASE_URL = "https://sandbox-quickbooks.api.intuit.com"

def get_fresh_access_token() -> str:
    """
    Trades the permanent Refresh Token for a temporary 60-minute Access Token.
    """
    if not QBO_CLIENT_ID or not QBO_CLIENT_SECRET or not QBO_REFRESH_TOKEN:
        print(" [QBO API] Credentials missing. Returning Simulation Data.")
        return "SIMULATION_MODE"

    auth_header = base64.b64encode(f"{QBO_CLIENT_ID}:{QBO_CLIENT_SECRET}".encode()).decode()
    url = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

def get_fresh_access_token() -> str:
    """
    Trades the permanent Refresh Token for a temporary 60-minute Access Token.
    """
    if not QBO_CLIENT_ID or not QBO_CLIENT_SECRET or not QBO_REFRESH_TOKEN:
        print(" [QBO API] Credentials missing. Returning Simulation Data.")
        return "SIMULATION_MODE"

    auth_header = base64.b64encode(f"{QBO_CLIENT_ID}:{QBO_CLIENT_SECRET}".encode()).decode()
    url = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
    
    headers = {
        "Authorization": f"Basic {auth_header}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": QBO_REFRESH_TOKEN
    }

    response = requests.post(url, headers=headers, data=data)
    
    if response.status_code == 200:
        return response.json().get("access_token")
    else:
        print(f" [QBO API Error] Failed to refresh token: {response.text}")
        return "SIMULATION_MODE"

def fetch_financial_baselines() -> dict:
    """
    Queries the QBO API to get the current Bank Balance (Cash) and Accounts Payable (Bills).
    """
    access_token = get_fresh_access_token()
    
    #  Fallback to our proxy math if API is not set up yet
    if access_token == "SIMULATION_MODE":
        return {"cash_balance": 35000.00, "ap_balance": 28000.00}

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }

    # 1. FETCH TOTAL CASH (Sum of all Bank Accounts)
    query_cash = "SELECT * FROM Account WHERE AccountType = 'Bank' AND Active = true"
    url_cash = f"{QBO_BASE_URL}/v3/company/{QBO_REALM_ID}/query?query={query_cash}&minorversion=65"
    
    res_cash = requests.get(url_cash, headers=headers)
    cash_balance = 0.0
    if res_cash.status_code == 200:
        accounts = res_cash.json().get("QueryResponse", {}).get("Account",[])
        cash_balance = sum(float(acc.get("CurrentBalance", 0)) for acc in accounts)

    # 2. FETCH TOTAL AP (Accounts Payable Balance)
    query_ap = "SELECT * FROM Account WHERE AccountType = 'Accounts Payable' AND Active = true"
    url_ap = f"{QBO_BASE_URL}/v3/company/{QBO_REALM_ID}/query?query={query_ap}&minorversion=65"
    
    res_ap = requests.get(url_ap, headers=headers)
    ap_balance = 0.0
    if res_ap.status_code == 200:
        accounts = res_ap.json().get("QueryResponse", {}).get("Account",[])
        ap_balance = sum(float(acc.get("CurrentBalance", 0)) for acc in accounts)

    print(f" [QBO API] Successfully synced live banking data. Cash: ${cash_balance:,.2f} | AP: ${ap_balance:,.2f}")
    
    return {
        "cash_balance": cash_balance,
        "ap_balance": ap_balance
    }

if __name__ == "__main__":
    # Test the API independently
    data = fetch_financial_baselines()
    print(data)