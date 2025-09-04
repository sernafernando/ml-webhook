from flask import Flask, request, redirect, jsonify, send_from_directory
import os
import requests
import json
from dotenv import load_dotenv
from datetime import datetime
import time
import psycopg2
from psycopg2.extras import Json

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
conn.autocommit = True

load_dotenv()

app = Flask(__name__)

# Variables de entorno (todas con prefijo ML_)
ML_CLIENT_ID = os.getenv("ML_CLIENT_ID")
ML_CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET")
ML_REDIRECT_URI = os.getenv("ML_REDIRECT_URI")
ML_REFRESH_TOKEN = os.getenv("ML_REFRESH_TOKEN")

ACCESS_TOKEN = None
EXPIRATION = 0

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

def render_json_as_html(data):
    if isinstance(data, dict):
        rows = []
        for k, v in data.items():
            rows.append(
                f"<tr>"
                f"<th scope='row' class='table-dark'>{k}</th>"
                f"<td>{render_json_as_html(v)}</td>"
                f"</tr>"
            )
        return "<table class='table table-dark table-bordered table-sm table-hover'>" + "".join(rows) + "</table>"

    elif isinstance(data, list):
        rows = []
        for i, item in enumerate(data):
            rows.append(
                f"<tr>"
                f"<th scope='row' class='table-dark'>[{i}]</th>"
                f"<td>{render_json_as_html(item)}</td>"
                f"</tr>"
            )
        return "<table class='table table-dark table-bordered table-sm table-hover'>" + "".join(rows) + "</table>"

    else:
        return f"<span class='text-light'>{str(data)}</span>"



@app.route("/auth")
def auth():
    auth_url = (
        f"https://auth.mercadolibre.com.ar/authorization?"
        f"response_type=code&client_id={ML_CLIENT_ID}&redirect_uri={ML_REDIRECT_URI}"
    )
    return redirect(auth_url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Falta el parámetro 'code'", 400

    token_url = "https://api.mercadolibre.com/oauth/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": ML_CLIENT_ID,
        "client_secret": ML_CLIENT_SECRET,
        "code": code,
        "redirect_uri": ML_REDIRECT_URI,
    }

    try:
        response = requests.post(token_url, data=payload)
        token_data = response.json()
        print("🔑 Token recibido:", token_data)

        if "access_token" in token_data:
            return "Token obtenido correctamente ✅", 200
        else:
            return jsonify(token_data), 400

    except Exception as e:
        return f"Error: {str(e)}", 500

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        evento = request.get_json()
        if not evento:
            return "JSON inválido o vacío", 400

        print("📩 Webhook recibido:", json.dumps(evento, indent=2))

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO webhooks (topic, user_id, resource, payload)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    evento.get("topic"),
                    evento.get("user_id"),
                    evento.get("resource"),
                    Json(evento),
                ),
            )

        resource = evento.get("resource", "")
        if resource and resource.startswith("/items/MLA"):
            # si viene con /price_to_win, recortamos
            base_resource = resource.split("/price_to_win")[0]
            fetch_and_store_preview(base_resource)

        return "Evento recibido", 200

    except Exception as e:
        print("❌ Error en webhook:", e)
        return "Error interno", 500

@app.route("/api/webhooks", methods=["GET"])
def get_webhooks():
    try:
        topic = request.args.get("topic")
        if not topic:
            return jsonify({"error": "Falta parámetro 'topic'"}), 400

        limit = int(request.args.get("limit", 500))
        offset = int(request.args.get("offset", 0))

        # total de registros del topic
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM webhooks WHERE topic = %s", (topic,))
            total = cur.fetchone()[0]

        rows = []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.payload, p.title, p.price, p.currency_id, p.thumbnail
                FROM webhooks w
                LEFT JOIN ml_previews p ON w.resource = p.resource
                WHERE w.topic = %s
                ORDER BY w.received_at DESC
                LIMIT %s OFFSET %s
                """,
                (topic, limit, offset),
            )
            for payload, title, price, currency_id, thumbnail in cur.fetchall():
                if isinstance(payload, str):
                    payload = json.loads(payload)

                resource = payload.get("resource", "")

                if resource.startswith("/items/MLA"):
                    base_resource = resource.split("/price_to_win")[0]
                    payload["preview"] = {
                        "title": title,
                        "price": price,
                        "currency_id": currency_id,
                        "thumbnail": thumbnail
                    }
                else:
                    payload["preview"] = None

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
        print("❌ Error leyendo DB:", e)
        return jsonify({"error": str(e)}), 500



@app.route("/api/ml/render")
def render_meli_resource():
    resource = request.args.get("resource")
    if not resource:
        return "Falta el parámetro 'resource'", 400

    try:
        access_token = get_token()
        response = requests.get(
            f"https://api.mercadolibre.com{resource}",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        data = response.json()

        html_parts = []

        # Caso especial: price_to_win
        if "/price_to_win" in resource:
            item_id = data.get("item_id")
            catalog_product_id = data.get("catalog_product_id")
            winner_id = data.get("winner", {}).get("item_id")
            current_price = data.get("current_price")
            winner_price = data.get("winner", {}).get("price")
            status = data.get("status")

            if item_id and catalog_product_id:
                ml_url = f"https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{item_id}"
                try:
                    access_token = get_token()
                    ml_res = requests.get(f"https://api.mercadolibre.com/items/{item_id}", headers={"Authorization": f"Bearer {access_token}"})
                    ml_data = ml_res.json()

                    title = ml_data.get("title", f"Item {item_id}")
                    price = ml_data.get("price", "—")
                    currency = ml_data.get("currency_id", "")
                    thumbnail = ml_data.get("thumbnail", "")
                except Exception as e:
                    title = f"Item {item_id}"
                    price, currency, thumbnail = "—", "", ""

                html_parts.append(
                    f"<h3>Vista de MercadoLibre</h3>"
                    f"<a href='{ml_url}' target='_blank' rel='noopener noreferrer' "
                    f"style='text-decoration:none;color:inherit;'>"
                    f"<div style='border:1px solid #444;border-radius:8px;padding:10px;"
                    f"margin:10px 0;display:flex;align-items:center;gap:10px;background:#222;'>"
                    f"<img src='{thumbnail}' alt='{title}' "
                    f"style='width:80px;height:80px;object-fit:cover;border-radius:6px;' />"
                    f"<div>"
                    f"<p style='margin:0;font-weight:bold;'>{title}</p>"
                    f"<p style='margin:0;'>{currency} {price}</p>"
                    f"<small>Click para ver en Mercado Libre</small>"
                    f"</div>"
                    f"</div>"
                    f"</a>"
                )
            
            if item_id == winner_id:
                html_parts.append(f"<div class='alert alert-success' role='alert'>🎉 Estás Ganando el Catálogo!</div>")
            
            if current_price > winner_price:
                html_parts.append(f"<div class='alert alert-danger' role='alert'>🚫 Estás perdiendo el catálogo por ${current_price - winner_price}</div>")
            
            if status == "sharing_first_place":
                html_parts.append(f"<div class='alert alert-warning' role='alert'>⚠️ Estás compartiendo el primer lugar.</div>")

        # siempre renderizar tabla del JSON
        html_parts.append(render_json_as_html(data))

        final_html = """
        <html>
            <head>
                <meta charset="utf-8">
                <title>ML Webhook Viewer</title>
                <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head>
            <body class="bg-dark text-light p-3">
        """
        final_html += "".join(html_parts)
        final_html += """
            </body>
        </html>
        """
        return final_html, 200

    except Exception as e:
        print("❌ Error en renderizado:", e)
        return "Error interno en renderizado", 500

@app.route("/api/webhooks/topics", methods=["GET"])
def get_topics():
    try:
        with conn.cursor() as cur:
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

def fetch_and_store_preview(resource):
    try:
        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"https://api.mercadolibre.com{resource}"
        res = requests.get(url, headers=headers)
        data = res.json()

        preview = {
            "resource": resource,
            "title": data.get("title", ""),
            "price": data.get("price", 0),
            "currency_id": data.get("currency_id", ""),
            "thumbnail": data.get("thumbnail", ""),
        }

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ml_previews (resource, title, price, currency_id, thumbnail, last_updated)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (resource) DO UPDATE
                SET title = EXCLUDED.title,
                    price = EXCLUDED.price,
                    currency_id = EXCLUDED.currency_id,
                    thumbnail = EXCLUDED.thumbnail,
                    last_updated = NOW();
            """, (preview["resource"], preview["title"], preview["price"], preview["currency_id"], preview["thumbnail"]))
            conn.commit()

        return preview
    except Exception as e:
        print(f"❌ Error obteniendo preview de {resource}:", e)
        return {"resource": resource, "title": "Error", "price": None, "currency_id": "", "thumbnail": ""}

@app.route("/api/ml/preview")
def ml_preview():
    resource = request.args.get("resource")
    if not resource:
        return jsonify({"error": "Falta parámetro resource"}), 400

    if not resource.startswith("/items/MLA"):
        return jsonify({"error": "Solo se soportan resources de items"}), 400

    return jsonify(fetch_and_store_preview(resource))

# frontend
@app.route("/")
def index():
    return send_from_directory("frontend/dist", "index.html")

@app.route("/<path:path>")
def assets(path):
    return send_from_directory("frontend/dist", path)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
