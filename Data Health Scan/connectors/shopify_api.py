import requests
import time
import polars as pl
import json
import socket
import urllib3
from datetime import datetime, timedelta
from config import SHOP_URL, ACCESS_TOKEN, API_VERSION

# ---  THE 85-SECOND TIMEOUT FIX ---
# Forces Python's requests library to use IPv4. 
# Bypasses local Windows/Conda IPv6 routing blackholes that cause 84-second hangs.
# 100% safe and compatible with DigitalOcean/Airbyte production environments.
urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET

class GovnrHighSpeedScanner:
    def __init__(self):
        self.graphql_url = f"https://{SHOP_URL}/admin/api/{API_VERSION}/graphql.json"
        self.headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": ACCESS_TOKEN
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        
    def _cancel_running_bulk_ops(self):
        poll_query = "{ currentBulkOperation { id status } }"
        try:
            resp = self.session.post(self.graphql_url, json={"query": poll_query}, timeout=30)
            data = resp.json().get("data", {}).get("currentBulkOperation")
            
            if data and data.get("status") == "RUNNING":
                print("[Govnr] Clearing a stalled Shopify operation from the queue...")
                cancel_mut = f'mutation {{ bulkOperationCancel(id: "{data["id"]}") {{ bulkOperation {{ status }} }} }}'
                self.session.post(self.graphql_url, json={"query": cancel_mut}, timeout=30)
                time.sleep(3) 
        except Exception:
            pass 

    def _fast_download_requests(self, url: str) -> list:
        # User-Agent spoofing + IPv4 strictly enforced
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.text.strip().split('\n')

    def extract_inventory(self) -> pl.DataFrame:
        print(f"[Govnr Live] Initiating Bulk Operation for Inventory (10k+ SKU capacity)...")
        start_time = time.time()
        
        self._cancel_running_bulk_ops()
        
        bulk_mutation = """
        mutation {
          bulkOperationRunQuery(
            query: \"\"\"
            {
              productVariants {
                edges {
                  node {
                    id
                    title
                    price
                    product { title }
                    inventoryItem {
                      id
                      sku
                      tracked
                      unitCost { amount }
                      inventoryLevels {
                        edges {
                          node {
                            id
                            location { name }
                            quantities(names:["available"]) { quantity }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            \"\"\"
          ) {
            bulkOperation { id status }
            userErrors { field message }
          }
        }
        """
        
        try:
            response = self.session.post(self.graphql_url, json={"query": bulk_mutation}, timeout=30)
            user_errors = response.json().get("data", {}).get("bulkOperationRunQuery", {}).get("userErrors",[])
            if user_errors:
                print(f" [GraphQL Error] Inventory Bulk Operation failed: {user_errors}")
                return pl.DataFrame()
        except Exception as e:
            print(f" [Network Error] Could not trigger Inventory Bulk Operation: {e}")
            return pl.DataFrame()

        poll_query = "{ currentBulkOperation { status errorCode objectCount url } }"
        download_url = None
        
        while True:
            time.sleep(2) 
            try:
                poll_resp = self.session.post(self.graphql_url, json={"query": poll_query}, timeout=30)
                poll_data = poll_resp.json().get("data", {}).get("currentBulkOperation", {})
                
                if not poll_data: continue
                status = poll_data.get("status")
                
                print(f"   ...Inventory Bulk Status: {status} | Elapsed: {int(time.time() - start_time)}s | Nodes: {poll_data.get('objectCount', 0)}", end="\r")
                
                if status == "COMPLETED":
                    download_url = poll_data.get("url")
                    print("\n") 
                    break
                elif status in["FAILED", "CANCELED", "EXPIRED"]:
                    print(f"\n [Error] Inventory Bulk Operation Halted. Code: {poll_data.get('errorCode')}")
                    return pl.DataFrame()
            except Exception as e:
                 print(f"\n [Network Error] Polling failed: {e}")
                 break

        inventory_records =[]
        if download_url:
            print(" [Govnr Live] Downloading & Vectorizing File (IPv4 Fast Mode)...")
            dl_start = time.time()
            
            variant_map = {}
            level_records =[]
            
            try:
                lines = self._fast_download_requests(download_url)
                
                for line in lines:
                    if not line: continue
                    record = json.loads(line)
                    
                    if "id" in record and "gid://shopify/ProductVariant/" in record.get("id", ""):
                        variant_map[record["id"]] = record
                    elif "__parentId" in record and "gid://shopify/ProductVariant/" in record.get("__parentId", ""):
                        level_records.append(record)

                for level in level_records:
                    parent_id = level.get("__parentId")
                    parent_node = variant_map.get(parent_id)
                    if not parent_node: continue
                    
                    inv_item = parent_node.get("inventoryItem") or {}
                    
                    sku = str(inv_item.get("sku") or "NO-SKU").strip()
                    prod_title = parent_node.get("product", {}).get("title", "")
                    var_title = parent_node.get("title", "")
                    full_title = f"{prod_title} ({var_title})" if var_title != "Default Title" else prod_title
                    
                    price = float(parent_node.get("price") or 0.0)
                    cost_data = inv_item.get("unitCost")
                    cost = float(cost_data.get("amount") or 0.0) if cost_data else 0.0
                    
                    loc_name = level.get("location", {}).get("name", "Unknown Loc")
                    
                    quants = level.get("quantities",[])
                    qty = int(quants[0].get("quantity", 0)) if quants else 0

                    inventory_records.append({
                        "SKU": sku, 
                        "Title": full_title, 
                        "Location": loc_name, 
                        "Price": price, 
                        "Unit_Cost": cost, 
                        "Qty_Available": qty
                    })

            except Exception as e:
                print(f" [File Error] Failed to parse Inventory Bulk file: {e}")
                
            print(f"   -> Download & Parse completed in {time.time() - dl_start:.2f} seconds.")
        
        df_inv = pl.DataFrame(inventory_records)
        print(f" [Scan Complete] Extracted {df_inv.height} active inventory locations. Total Time: {time.time() - start_time:.2f}s")
        return df_inv

    def extract_sales_velocity(self) -> pl.DataFrame:
        print("\n[Govnr Live] Initiating Bulk Operation for 365-Day Order Velocity...")
        start_time = time.time()
        
        self._cancel_running_bulk_ops()
        
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bulk_mutation = f"""
        mutation {{
          bulkOperationRunQuery(
            query: \"\"\"
            {{
              orders(query: "created_at:>={start_date}") {{
                edges {{
                  node {{
                    id
                    lineItems {{ edges {{ node {{ sku quantity }} }} }}
                  }}
                }}
              }}
            }}
            \"\"\"
          ) {{ bulkOperation {{ id status }} userErrors {{ field message }} }}
        }}
        """
        
        try:
            response = self.session.post(self.graphql_url, json={"query": bulk_mutation}, timeout=30)
            user_errors = response.json().get("data", {}).get("bulkOperationRunQuery", {}).get("userErrors",[])
            if user_errors:
                print(f" [GraphQL Error] Bulk Operation failed: {user_errors}")
                return pl.DataFrame({"SKU": [], "Qty_Sold":[]})
        except Exception as e:
            print(f" [Network Error] Could not trigger Bulk Operation: {e}")
            return pl.DataFrame({"SKU": [], "Qty_Sold":[]})

        poll_query = "{ currentBulkOperation { status errorCode objectCount url } }"
        download_url = None
        
        while True:
            time.sleep(2) 
            try:
                poll_resp = self.session.post(self.graphql_url, json={"query": poll_query}, timeout=30)
                poll_data = poll_resp.json().get("data", {}).get("currentBulkOperation", {})
                
                if not poll_data: continue
                status = poll_data.get("status")
                
                print(f"   ...Sales Bulk Status: {status} | Elapsed: {int(time.time() - start_time)}s | Nodes: {poll_data.get('objectCount', 0)}", end="\r")
                
                if status == "COMPLETED":
                    download_url = poll_data.get("url")
                    print("\n")
                    break
                elif status in["FAILED", "CANCELED", "EXPIRED"]:
                    print(f"\n [Error] Sales Bulk Operation Halted. Code: {poll_data.get('errorCode')}")
                    return pl.DataFrame({"SKU":[], "Qty_Sold":[]})
            except Exception as e:
                 print(f"\n [Network Error] Polling failed: {e}")
                 break

        sales_data =[]
        if download_url:
            print(" [Govnr] Downloading & Vectorizing File (IPv4 Fast Mode)...")
            dl_start = time.time()
            try:
                lines = self._fast_download_requests(download_url)
                
                for line in lines:
                    if not line: continue
                    record = json.loads(line)
                    sku = record.get("sku")
                    qty = record.get("quantity")
                    
                    if sku and qty is not None:
                        sales_data.append({"SKU": str(sku).strip(), "Qty_Sold": int(qty)})
            except Exception as e:
                print(f" [File Error] Failed to download/parse Bulk file: {e}")
                
            print(f"   -> Download & Parse completed in {time.time() - dl_start:.2f} seconds.")

        if not sales_data:
            return pl.DataFrame({"SKU": [], "Qty_Sold":[]}, schema={"SKU": pl.String, "Qty_Sold": pl.Int64})
            
        df_sales = pl.DataFrame(sales_data)
        print(f" [Scan Complete] Aggregated 365-day velocity from {df_sales.height} line items. Total Time: {time.time() - start_time:.2f}s")
        return df_sales