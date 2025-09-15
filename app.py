from flask import Flask, request, redirect, jsonify, send_from_directory
import os
import requests
import json
from dotenv import load_dotenv
from datetime import datetime
import time
import psycopg2
from psycopg2 import pool
from psycopg2.extras import Json
from zoneinfo import ZoneInfo
from contextlib import contextmanager

load_dotenv()

app = Flask(__name__)

# Variables de entorno (todas con prefijo ML_)
ML_CLIENT_ID = os.getenv("ML_CLIENT_ID")
ML_CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET")
ML_REDIRECT_URI = os.getenv("ML_REDIRECT_URI")
ML_REFRESH_TOKEN = os.getenv("ML_REFRESH_TOKEN")

ACCESS_TOKEN = None
EXPIRATION = 0

DEBUG_WEBHOOK = os.getenv("DEBUG_WEBHOOK", "0") == "1"

FAVICON_DIR = "https://ml-webhook.gaussonline.com.ar/assets/white-g-BfxDaKwI.png"

# 🔹 Pool de conexiones
db_pool = pool.SimpleConnectionPool(
    1, 10,
    dsn=os.getenv("DATABASE_URL")
)

@contextmanager
def db_cursor():
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    finally:
        db_pool.putconn(conn)

# ---------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------
def refresh_token():
    global ACCESS_TOKEN, EXPIRATION

    if not ML_REFRESH_TOKEN:
        raise Exception("❌ No hay ML_REFRESH_TOKEN en variables de entorno")

    url = "https://api.mercadolibre.com/oauth/token"
    payload = {
        "grant_type": "refresh_token",
        "client_id": ML_CLIENT_ID,
        "client_secret": ML_CLIENT_SECRET,
        "refresh_token": ML_REFRESH_TOKEN
    }

    response = requests.post(url, data=payload)
    data = response.json()

    if "access_token" in data:
        ACCESS_TOKEN = data["access_token"]
        EXPIRATION = time.time() + data["expires_in"] - 60
        print("✅ Nuevo access_token obtenido.")
    else:
        print("❌ Error al refrescar token:", data)
        raise Exception("No se pudo refrescar el access_token")

def get_token():
    if ACCESS_TOKEN is None or time.time() >= EXPIRATION:
        refresh_token()
    return ACCESS_TOKEN

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _fmt_ars(val):
    try:
        n = float(val)
        s = f"{n:,.2f}"
        return "$" + s.replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return "—" if val in (None, "") else str(val)

def render_json_as_html(data):
    if isinstance(data, dict):
        rows = []
        for k, v in data.items():
            rows.append(
                f"<tr><th scope='row' class='table-dark'>{k}</th><td>{render_json_as_html(v)}</td></tr>"
            )
        return "<table class='table table-dark table-bordered table-sm table-hover'>" + "".join(rows) + "</table>"

    elif isinstance(data, list):
        rows = []
        for i, item in enumerate(data):
            rows.append(
                f"<tr><th scope='row' class='table-dark'>[{i}]</th><td>{render_json_as_html(item)}</td></tr>"
            )
        return "<table class='table table-dark table-bordered table-sm table-hover'>" + "".join(rows) + "</table>"

    else:
        return f"<span class='text-light'>{str(data)}</span>"

# ---------------------------------------------------------------------
# Fetch and store preview
# ---------------------------------------------------------------------
def fetch_and_store_preview(resource: str):
    try:
        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}

        preview = {"resource": resource}

        if resource.endswith("/price_to_win"):
            item_id = resource.split("/")[2]

            res_item = requests.get(f"https://api.mercadolibre.com/items/{item_id}", headers=headers)
            item_data = res_item.json()

            catalog_product_id = item_data.get("catalog_product_id")
            brand_name = next(
                (a.get("value_name") for a in item_data.get("attributes", []) if a.get("id") == "BRAND"),
                ""
            )

            preview.update({
                "title": item_data.get("title", ""),
                "thumbnail": item_data.get("thumbnail", ""),
                "currency_id": item_data.get("currency_id", ""),
                "permalink": item_data.get("permalink", ""),
                "catalog_product_id": catalog_product_id,
                "brand": brand_name,
            })

            res_ptw = requests.get(f"https://api.mercadolibre.com/items/{item_id}/price_to_win?version=v2", headers=headers)
            ptw_data = res_ptw.json()

            winner_id = (ptw_data.get("winner") or {}).get("item_id")
            winner_price = (ptw_data.get("winner") or {}).get("price")
            current_price = ptw_data.get("current_price")

            preview.update({
                "price": current_price,
                "status": ptw_data.get("status"),
                "winner": winner_id,
                "winner_price": winner_price,
            })

            if catalog_product_id and winner_id:
                winner_url = f"https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{winner_id}"
            else:
                winner_url = None

            preview["winner_url"] = winner_url
            preview["winner_price_fmt"] = _fmt_ars(winner_price)
            if winner_url:
                preview["winner_line_html"] = (
                    f'🏆 Ganador: <a href="{winner_url}" target="_blank" rel="noopener noreferrer">{winner_id}</a>'
                    f' — {_fmt_ars(winner_price)}'
                )
            else:
                preview["winner_line_html"] = (
                    f'🏆 Ganador: {winner_id or "—"} — {_fmt_ars(winner_price)}'
                )

        else:
            res_item = requests.get(f"https://api.mercadolibre.com{resource}", headers=headers)
            item_data = res_item.json()
            brand_name = next(
                (a.get("value_name") for a in item_data.get("attributes", []) if a.get("id") == "BRAND"),
                ""
            )
            preview.update({
                "title": item_data.get("title", ""),
                "thumbnail": item_data.get("thumbnail", ""),
                "currency_id": item_data.get("currency_id", ""),
                "price": item_data.get("price"),
                "permalink": item_data.get("permalink", ""),
                "catalog_product_id": item_data.get("catalog_product_id"),
                "brand": brand_name,
            })

        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO ml_previews (resource, title, price, currency_id, thumbnail, winner, winner_price, status, brand, last_updated)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (resource) DO UPDATE SET
                    title = EXCLUDED.title,
                    price = EXCLUDED.price,
                    currency_id = EXCLUDED.currency_id,
                    thumbnail = EXCLUDED.thumbnail,
                    winner = EXCLUDED.winner,
                    winner_price = EXCLUDED.winner_price,
                    status = EXCLUDED.status,
                    brand = EXCLUDED.brand,
                    last_updated = NOW();
            """, (
                preview["resource"],
                preview.get("title"),
                preview.get("price"),
                preview.get("currency_id"),
                preview.get("thumbnail"),
                preview.get("winner"),
                preview.get("winner_price"),
                preview.get("status"),
                preview.get("brand"),
            ))

        print("🔍 Preview generado:", preview)
        return preview

    except Exception as e:
        print(f"❌ Error obteniendo preview de {resource}:", e)
        return {"resource": resource, "title": "Error"}

# ---------------------------------------------------------------------
# Resto de tus rutas (NO cambié la lógica, solo la parte DB)
# ---------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        evento = request.get_json(silent=True, force=True)
        if not evento:
            return "JSON inválido o vacío", 400

        resource = evento.get("resource", "")
        results = {
            "received_resource": resource,
            "insert_original": None,
            "preview_refreshed": False,
            "errors": [],
        }

        try:
            with db_cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO webhooks (topic, user_id, resource, payload, webhook_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (webhook_id) DO NOTHING
                    """,
                    (
                        evento.get("topic"),
                        evento.get("user_id"),
                        resource,
                        Json(evento),
                        evento.get("_id"),
                    ),
                )
                results["insert_original"] = {
                    "rowcount": cur.rowcount,
                    "webhook_id": evento.get("_id"),
                }
        except Exception as e:
            results["errors"].append(f"insert_original: {e}")

        try:
            if resource:
                fetch_and_store_preview(resource)
                results["preview_refreshed"] = True
        except Exception as e:
            results["errors"].append(f"fetch_and_store_preview: {e}")

        if DEBUG_WEBHOOK:
            return jsonify(results), 200
        return "Evento recibido", 200

    except Exception as e:
        if DEBUG_WEBHOOK:
            return jsonify({"ok": False, "fatal": str(e)}), 500
        return "Error interno", 500

@app.route("/api/webhooks", methods=["GET"])
def get_webhooks():
    try:
        topic = request.args.get("topic")
        if not topic:
            return jsonify({"error": "Falta parámetro 'topic'"}), 400

        limit = int(request.args.get("limit", 500))
        offset = int(request.args.get("offset", 0))

        with db_cursor() as cur:
            cur.execute("""
                WITH latest AS (
                    SELECT resource, MAX(received_at) AS max_received
                    FROM webhooks
                    WHERE topic = %s
                    GROUP BY resource
                )
                SELECT COUNT(*) FROM latest
            """, (topic,))
            total = cur.fetchone()[0]

        with db_cursor() as cur:
            cur.execute("""
                WITH latest AS (
                    SELECT resource, MAX(received_at) AS max_received
                    FROM webhooks
                    WHERE topic = %s
                    GROUP BY resource
                )
                SELECT
                    w.payload,
                    p.title, p.price, p.currency_id, p.thumbnail, p.winner, p.winner_price, p.status, w.received_at, p.brand
                FROM latest
                JOIN webhooks w
                  ON w.resource = latest.resource
                 AND w.received_at = latest.max_received
                LEFT JOIN ml_previews p
                  ON p.resource = w.resource        
                ORDER BY w.received_at DESC
                LIMIT %s OFFSET %s
            """, (topic, limit, offset))
            rows_db = cur.fetchall()

        rows = []
        for row in rows_db:
            payload = row[0]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {"raw": payload}

            preview = {
                "title": row[1],
                "price": row[2],
                "currency_id": row[3],
                "thumbnail": row[4],
                "winner": row[5],
                "winner_price": row[6],
                "status": row[7],
                "brand": row[9],
            }

            payload["db_preview"] = preview
            local_dt = row[8].astimezone(ZoneInfo("America/Argentina/Buenos_Aires"))
            payload["received_at"] = local_dt.strftime("%Y-%m-%d %H:%M:%S")
            rows.append(payload)

        return jsonify({
            "topic": topic,
            "events": rows,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total
            }
        })

    except Exception as e:
        import traceback
        print("❌ Error leyendo DB:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/webhooks/topics", methods=["GET"])
def get_topics():
    try:
        with db_cursor() as cur:
            cur.execute("""
                SELECT topic, COUNT(*)
                FROM webhooks
                GROUP BY topic
                ORDER BY COUNT(*) DESC
            """)
            topics = [{"topic": row[0], "count": row[1]} for row in cur.fetchall()]
        return jsonify(topics)
    except Exception as e:
        print("❌ Error obteniendo topics:", e)
        return jsonify({"error": str(e)}), 500

@app.route("/debug/dbinfo")
def debug_dbinfo():
    try:
        with db_cursor() as cur:
            cur.execute("SELECT current_database(), current_user, inet_server_addr(), inet_server_port();")
            db, user, host, port = cur.fetchone()

            cur.execute("""
                SELECT COUNT(*)
                FROM webhooks
                WHERE resource LIKE '/items/MLA2243355590%%'
            """)
            count = cur.fetchone()[0]

        return jsonify({
            "db": db,
            "user": user,
            "host": str(host),
            "port": port,
            "webhooks_for_item": count
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------
# Resto de tus rutas (sin cambios en la lógica de negocio)
# ---------------------------------------------------------------------
# ... (dejé igual /auth, /callback, /api/ml/render, /consulta, etc.)
# ---------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
