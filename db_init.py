import psycopg2

from enterprise_ingestion_schema import apply_enterprise_ingestion_schema

# PASTE YOUR DIGITALOCEAN CONNECTION STRING HERE
DB_URL = "postgresql://doadmin:AVNS_pxRbIfpMo_9gAuyg44q@govnr-db-do-user-34983742-0.h.db.ondigitalocean.com:25060/defaultdb?sslmode=require"  

def init_db():
    print("Connecting to DigitalOcean Postgres...")
    conn = psycopg2.connect(DB_URL)
    cursor = conn.cursor()
    
    # 1. Clients Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            client_uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_name VARCHAR(255) NOT NULL,
            contact_email VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            inbound_email_prefix VARCHAR(50) UNIQUE NOT NULL,
            account_tier VARCHAR(50) DEFAULT 'diagnostic',
            inventory_column_mapping JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 2. Client Metrics History (The Moat)
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
            run_tier VARCHAR(50) DEFAULT 'diagnostic',
            confidence_tier VARCHAR(50) DEFAULT 'Tier A',
            UNIQUE(client_uuid, date_recorded)
        );
    """)

    # 3. Demand Radar Vault (The IP Engine)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS demand_radar_vault (
            id SERIAL PRIMARY KEY,
            date_recorded DATE NOT NULL,
            client_hash VARCHAR(64) NOT NULL,
            region VARCHAR(50),
            part_category VARCHAR(100),
            max_fitment_year INT,
            regional_stock_count NUMERIC(15, 2),
            regional_velocity NUMERIC(15, 2),
            regional_capital_deployed NUMERIC(15, 2),
            regional_surplus_deficit NUMERIC(15, 2),
            UNIQUE(date_recorded, client_hash, part_category, max_fitment_year)
        );
    """)

    # 4–5. Enterprise SFTP ingestion (shared module)
    apply_enterprise_ingestion_schema(cursor)

    conn.commit()
    cursor.close()
    conn.close()
    print("SUCCESS: Enterprise database schema deployed to DigitalOcean.")

if __name__ == "__main__":
    init_db()