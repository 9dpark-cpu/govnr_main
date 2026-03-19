import os
import sys
import polars as pl

# --- DEFENSIVE PATHING ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# In the future, this imports from config.py
# from config import QBO_CLIENT_ID, etc.

def calculate_working_capital(
    df_pending_pos: pl.DataFrame, 
    proxy_cash_balance: float = 35000.00, 
    proxy_upcoming_ap: float = 28000.00,
    reserve_percentage: float = 0.10
) -> dict:
    """
    Calculates the Daily Working Capital Budget (OTB) and checks for Cash Collisions.
    
    :param proxy_cash_balance: The simulated bank balance (Later: QBO API)
    :param proxy_upcoming_ap: Bills due in the next 1-15 days (Later: NetSuite AP Aging)
    :param reserve_percentage: Minimum cash buffer to hold (10% default)
    """
    
    # 1. Base Financials
    operating_reserve = proxy_cash_balance * reserve_percentage
    # OTB = Cash - Bills - Safety Net
    safe_spending_limit = proxy_cash_balance - proxy_upcoming_ap - operating_reserve
    
    # Floor the OTB at $0 (Can't have a negative budget)
    safe_spending_limit = max(0.0, safe_spending_limit)

    # 2. Calculate Intended Spend (Pending POs)
    if not df_pending_pos.is_empty():
        total_po_exposure = df_pending_pos["Total_Amount"].sum()
    else:
        total_po_exposure = 0.0

    # 3. Cash-Collision Logic Engine
    liquidity_status = "Healthy"
    action_brief = ""

    if proxy_upcoming_ap > proxy_cash_balance:
        liquidity_status = " SEVERE LIQUIDITY CONSTRAINT"
        action_brief = f"Your upcoming payables (${proxy_upcoming_ap:,.2f}) exceed your cash position (${proxy_cash_balance:,.2f}). FREEZE ALL NON-ESSENTIAL PROCUREMENT."
    
    elif total_po_exposure > safe_spending_limit:
        liquidity_status = " CASH COLLISION WARNING"
        shortfall = total_po_exposure - safe_spending_limit
        action_brief = f"Draft POs total ${total_po_exposure:,.2f}, but your Safe Spending Limit is only ${safe_spending_limit:,.2f}. Please cut ${shortfall:,.2f} from pending POs to protect payroll."
    
    else:
        liquidity_status = " BUDGET COMPLIANT"
        action_brief = f"Pending POs (${total_po_exposure:,.2f}) are within your Safe Spending Limit (${safe_spending_limit:,.2f}). Proceed with procurement."

    # Return the CFO Report Dictionary
    return {
        "Cash_Position": proxy_cash_balance,
        "Upcoming_AP": proxy_upcoming_ap,
        "Operating_Reserve": operating_reserve,
        "Safe_Spending_Limit_OTB": safe_spending_limit,
        "Total_PO_Exposure": total_po_exposure,
        "Status": liquidity_status,
        "Action_Brief": action_brief
    }

def print_cfo_morning_brief(financials: dict):
    """
    Formats the data into the 'CFO Sandwich' style requested in the Audit docs.
    (Professional Term -> Plain English -> Cash Impact)
    """
    print("\n" + "="*60)
    print("  GOVNR: DAILY STRATEGIC COMPLIANCE BRIEF")
    print("="*60)
    print("\n[Liquidity Constraint Alert] / Cash Collision Warning")
    
    print(f"-> Bank Balance (True Cash):    ${financials['Cash_Position']:,.2f}")
    print(f"-> Accounts Payable (1-15d):    ${financials['Upcoming_AP']:,.2f}")
    print(f"-> Operating Reserve (10%):     ${financials['Operating_Reserve']:,.2f}")
    
    print("-" * 60)
    print(f" SAFE SPENDING LIMIT (OTB):   ${financials['Safe_Spending_Limit_OTB']:,.2f}")
    print(f" PENDING DRAFT POs:           ${financials['Total_PO_Exposure']:,.2f}")
    print("\n" + financials['Status'])
    print(f"Action: {financials['Action_Brief']}")
    print("="*60 + "\n")

# --- EXECUTION BLOCK MUST BE FLUSH LEFT ---
if __name__ == "__main__":
    # Import our previous parsers to get the actual Draft PO exposure
    from engine.netsuite_parser import ingest_po_activity

    # 1. Get the pending POs from NetSuite CSV
    df_po_raw = ingest_po_activity(client_uuid="client_test_001")

    # 2. Run the Cash Logic with our proxies
    # Let's say they have $35k in cash, but $28k in bills due next week.
    financials_test = calculate_working_capital(
        df_pending_pos=df_po_raw,
        proxy_cash_balance=35000.00,
        proxy_upcoming_ap=28000.00
    )

    # 3. Print the Brief
    print_cfo_morning_brief(financials_test)