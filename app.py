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
                f"<th style='text-align:left;padding:6px;border:1px solid #444;background:#222;color:#0ff'>{k}</th>"
                f"<td style='padding:6px;border:1px solid #444'>{render_json_as_html(v)}</td>"
                f"</tr>"
            )
        return "<table style='border-collapse:collapse;font-family:sans-serif;font-size:14px;margin:10px 0;width:100%'>" + "".join(rows) + "</table>"

    elif isinstance(data, list):
        rows = []
        for i, item in enumerate(data):
            rows.append(
                f"<tr>"
                f"<th style='text-align:left;padding:6px;border:1px solid #444;background:#333;color:#0ff'>[{i}]</th>"
                f"<td style='padding:6px;border:1px solid #444'>{render_json_as_html(item)}</td>"
                f"</tr>"
            )
        return "<table style='border-collapse:collapse;font-family:sans-serif;font-size:14px;margin:10px 0;width:100%'>" + "".join(rows) + "</table>"

    else:
        return f"<span style='color:#eee'>{str(data)}</span>"


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

        return "Evento recibido", 200

    except Exception as e:
        print("❌ Error en webhook:", e)
        return "Error interno", 500

@app.route("/api/webhooks", methods=["GET"])
def get_webhooks():
    events_by_topic = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT topic, payload FROM webhooks ORDER BY received_at DESC LIMIT 500")
            for topic, payload in cur.fetchall():
                events_by_topic.setdefault(topic, []).append(payload)
    except Exception as e:
        print("❌ Error leyendo DB:", e)
    return jsonify(events_by_topic)

@app.route("/api/ml")
def consultar_ml():
    resource = request.args.get("resource")
    if not resource:
        return jsonify({"error": "Falta parámetro 'resource'"}), 400

    try:
        token = get_token()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    url = f"https://api.mercadolibre.com{resource}"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        res = requests.get(url, headers=headers)
        return jsonify(res.json()), res.status_code
    except Exception as e:
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

            if item_id and catalog_product_id:
                ml_url = f"https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{item_id}"
                html_parts.append(
                    f"<h3>Vista de MercadoLibre</h3>"
                    f"<iframe src='{ml_url}' width='100%' height='600' style='border:1px solid #444;border-radius:8px;'></iframe>"
                )

        # siempre renderizar tabla del JSON
        html_parts.append(render_json_as_html(data))

        final_html = "<html><body style='background:#111;color:#eee;padding:20px'>" + "".join(html_parts) + "</body></html>"
        return final_html, 200

    except Exception as e:
        print("❌ Error en renderizado:", e)
        return "Error interno en renderizado", 500


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
