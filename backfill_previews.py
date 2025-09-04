import psycopg2
from app import fetch_and_store_preview, conn

with conn.cursor() as cur:
    cur.execute("SELECT DISTINCT resource FROM webhooks WHERE resource LIKE '/items/MLA%/price_to_win'")
    resources = [row[0] for row in cur.fetchall()]

print(f"🔄 Encontrados {len(resources)} resources para backfill")

for res in resources:
    print(f"→ Refrescando {res}")
    fetch_and_store_preview(res)