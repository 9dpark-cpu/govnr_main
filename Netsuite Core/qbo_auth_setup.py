import os
import urllib.parse
import requests
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- 1. PASTE YOUR CREDENTIALS HERE ---
CLIENT_ID = "ABZc2u2yPcxH1Te2wf2vZR75nfOfp09r0mLvwcTNjb6QmVwz7L"
CLIENT_SECRET = "GWlV63etvKtPU4uvF1XJK0tzmKaSSrZWwOwXvph8"
# -------------------------------------

REDIRECT_URI = "http://localhost:8000/callback"
AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

class OAuthHandler(BaseHTTPRequestHandler):
    # This creates a tiny local web server just to catch the token QuickBooks sends back
    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        
        if parsed_path.path == "/callback":
            query_params = urllib.parse.parse_qs(parsed_path.query)
            auth_code = query_params.get("code", [None])[0]
            realm_id = query_params.get("realmId",[None])[0]
            
            if not auth_code:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Error: No auth code received from QuickBooks.")
                return
            
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Success! You can close this browser window and look at your terminal.")
            
            # Instantly exchange the code for the permanent Refresh Token
            exchange_code_for_token(auth_code, realm_id)
            
            # Shut down the temporary server
            raise KeyboardInterrupt

def exchange_code_for_token(auth_code, realm_id):
    print("\n[2/2] Exchanging Authorization Code for Refresh Token...")
    
    auth_header = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    
    headers = {
        "Authorization": f"Basic {auth_header}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }
    
    data = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": REDIRECT_URI
    }
    
    response = requests.post(TOKEN_URL, headers=headers, data=data)
    
    if response.status_code == 200:
        tokens = response.json()
        refresh_token = tokens.get("refresh_token")
        
        print("\n" + "="*70)
        print(" 🎉 SUCCESS! HERE ARE YOUR GOVNR ENVIRONMENT VARIABLES")
        print("="*70)
        print("Create a file named '.env' in your root folder and paste this exactly:\n")
        print(f"QBO_CLIENT_ID={CLIENT_ID}")
        print(f"QBO_CLIENT_SECRET={CLIENT_SECRET}")
        print(f"QBO_REALM_ID={realm_id}")
        print(f"QBO_REFRESH_TOKEN={refresh_token}")
        print("\n" + "="*70 + "\n")
    else:
        print(f"\n❌ Error exchanging token: {response.text}")

def run_auth_flow():
    if CLIENT_ID == "YOUR_CLIENT_ID_HERE":
        print("❌ STOP: Please edit this script and paste your CLIENT_ID and CLIENT_SECRET at the top.")
        return

    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": "com.intuit.quickbooks.accounting",
        "redirect_uri": REDIRECT_URI,
        "state": "govnr_init"
    }
    
    auth_request_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    
    print("\n" + "="*60)
    print(" 🏦 GOVNR QUICKBOOKS OAUTH 2.0 SETUP")
    print("="*60)
    print("\n[1/2] Please CTRL+Click the link below to authorize Govnr:\n")
    print(auth_request_url + "\n")
    print("Waiting for you to log in and approve in your browser... (Press Ctrl+C to cancel)")
    
    server = HTTPServer(('localhost', 8000), OAuthHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    run_auth_flow()