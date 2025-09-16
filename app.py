from flask import Flask, request, redirect, jsonify, send_from_directory
import os
import requests
import json
from dotenv import load_dotenv
from datetime import datetime
import time
import psycopg2
from psycopg2.extras import Json
from zoneinfo import ZoneInfo
from psycopg2 import pool
from contextlib import contextmanager

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

def _fmt_ars(val):
    try:
        n = float(val)
        s = f"{n:,.2f}"
        # Formato es-AR: miles con punto y decimales con coma
        return "$" + s.replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return "—" if val in (None, "") else str(val)

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

def fetch_and_store_preview(resource: str):
    try:
        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}

        preview = {"resource": resource}

        if resource.endswith("/price_to_win"):
            item_id = resource.split("/")[2]

            # consulta 1: datos básicos del item (trae catalog_product_id)
            res_item = requests.get(f"https://api.mercadolibre.com/items/{item_id}", headers=headers)
            item_data = res_item.json()

            catalog_product_id = item_data.get("catalog_product_id")  # <<--- lo necesitamos
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
                "brand": brand_name,   # <<--- ahora sí trae la marca
            })

            # consulta 2: price_to_win
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

            # -------- NUEVO: campos de preview listos para el frontend --------
            if catalog_product_id and winner_id:
                winner_url = f"https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{winner_id}"
            else:
                winner_url = None

            preview["winner_url"] = winner_url
            preview["winner_price_fmt"] = _fmt_ars(winner_price)
            # opcional: línea HTML ya armada (si querés inyectar tal cual en React)
            if winner_url:
                preview["winner_line_html"] = (
                    f'🏆 Ganador: <a href="{winner_url}" target="_blank" rel="noopener noreferrer">{winner_id}</a>'
                    f' — {_fmt_ars(winner_price)}'
                )
            else:
                preview["winner_line_html"] = (
                    f'🏆 Ganador: {winner_id or "—"}'
                    f' — {_fmt_ars(winner_price)}'
                )

        else:
            # caso normal /items/{id} (sin cambios)
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
                # opcionalmente podrías setear winner_url/winner_line_html = None acá
            })

        # --- Persistís SOLO lo que ya guardabas (no cambia el esquema) ---
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
                preview.get("brand"),          # 👈 nuevo
            ))
            

        print("🔍 Preview generado:", preview)
        return preview

    except Exception as e:
        print(f"❌ Error obteniendo preview de {resource}:", e)
        return {"resource": resource, "title": "Error"}


    
def render_ml_view(resource, data):
    html_parts = []

    # -------------------------------
    # Caso: /price_to_win
    # -------------------------------
    if "/price_to_win" in resource:
        item_id = data.get("item_id")
        catalog_product_id = data.get("catalog_product_id")
        
        if not catalog_product_id:
            # Aviso en el HTML
            html_parts.append(
                "<div class='alert alert-secondary' role='alert'>"
                "<h4>📦 El MLA no es una publicación de catálogo</h4>"
                "<h5>Mostrando datos del Producto</h5>"
                "</div>"
            )

            try:
                token = get_token()
                res_item = requests.get(
                    f"https://api.mercadolibre.com/items/{item_id}",
                    headers={"Authorization": f"Bearer {token}"}
                )
                item_data = res_item.json()

                permalink = item_data.get("permalink")
                if item_id and permalink:
                    html_parts.append(make_item_card(item_id, permalink, item_data))

                # 👇 Agregar también el JSON del item
                html_parts.append(render_json_as_html(item_data))

            except Exception as e:
                html_parts.append(
                    f"<div class='alert alert-danger'>❌ Error al cargar datos del item: {e}</div>"
                )

            # 👈 ahora SÍ cortamos acá, porque ya mostramos card + json
            return "".join(html_parts)
        winner = data.get("winner", {}) or {}
        winner_id = winner.get("item_id")
        current_price = data.get("current_price")
        winner_price = winner.get("price")
        status = data.get("status")
        competitors_sharing = data.get("competitors_sharing_first_place", 0)
        competitors_label = "Competidor" if competitors_sharing == 1 else "Competidores"

        # Card de producto (similar a /items común)
        if item_id and catalog_product_id:
            ml_url = f"https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{item_id}"
            html_parts.append(make_item_card(item_id, ml_url))

        # Alerts
        if item_id == winner_id:
            html_parts.append("<div class='alert alert-success' role='alert'>🎉 Estás Ganando el Catálogo!</div>")

        if current_price and winner_price and current_price > winner_price:
            diff = current_price - winner_price
            html_parts.append(f"<div class='alert alert-danger' role='alert'>🚫 Estás perdiendo el catálogo por ${diff}</div>")

        if status == "sharing_first_place":
            html_parts.append(f"<div class='alert alert-warning' role='alert'>⚠️ Estás compartiendo el primer lugar con {competitors_sharing} {competitors_label}.</div>")

        def _fmt_money(val):
            try:
                # acepta str o número y lo muestra sin decimales
                return f"{data.get('currency_id','') } {int(round(float(val))):,}".replace(",", ".")
            except Exception:
                return val if val is not None else "—"

        def _render_boosts_list(boost_list):
            if not boost_list:
                return "<em>Sin boosts</em>"
            lis = []
            for b in boost_list:
                st = (b or {}).get("status")
                icon = "🟢" if st == "boosted" else ("⚪" if st in ("opportunity", None) else "🟠")
                desc = (b or {}).get("description") or (b or {}).get("id") or "—"
                lis.append(f"<li class='mb-1'>{icon} {desc} <small class='text-muted'>({st or '—'})</small></li>")
            return "<ul class='mb-0 ps-3'>" + "".join(lis) + "</ul>"

        # Datos del propio item
        price_to_win_val = data.get("price_to_win")
        boosts_self = data.get("boosts", [])
        visit_share = data.get("visit_share") or "—"
        consistent = data.get("consistent")
        comp_share = data.get("competitors_sharing_first_place")
        comp_share_txt = "—" if comp_share in (None, "", []) else comp_share

        # Datos del ganador
        winner_boosts = winner.get("boosts", [])

        html_parts.append(f"""
        <div class="row g-3 mt-2">
          <!-- Tu publicación -->
          <div class="col-md-6">
            <div class="card bg-dark text-light border-info h-100">
              <div class="card-header">📦 Tu publicación</div>
              <div class="card-body">
                <div class="d-flex justify-content-between flex-wrap">
                  <div><strong>Item ID:</strong> {f'<a href="https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{item_id}" target="_blank" rel="noopener noreferrer">{item_id}</a>' if (catalog_product_id and item_id) else (item_id or "—")}</div>
                  <div><strong>Estado:</strong> {status or "—"}</div>
                </div>
                <div class="mt-2">
                  <div><strong>Precio actual:</strong> {_fmt_money(current_price)}</div>
                  <div><strong>Price to win:</strong> {_fmt_money(price_to_win_val)}</div>
                </div>
                <div class="mt-2 d-flex justify-content-between flex-wrap">
                  <div><strong>Consistente:</strong> {"✅ Sí" if consistent else "❌ No"}</div>
                  <div><strong>Visit share:</strong> {visit_share}</div>
                  <div><strong>Competidores en 1º lugar:</strong> {comp_share_txt}</div>
                </div>
                <hr>
                <h6 class="mb-2">Boosts</h6>
                {_render_boosts_list(boosts_self)}
              </div>
            </div>
          </div>

          <!-- Ganador -->
          <div class="col-md-6">
            <div class="card bg-dark text-light border-success h-100">
              <div class="card-header">🏆 Ganador</div>
              <div class="card-body">
                <div class="d-flex justify-content-between flex-wrap">
                  <div><strong>Item ID:</strong> {f'<a href="https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{winner_id}" target="_blank" rel="noopener noreferrer">{winner_id}</a>' if (catalog_product_id and winner_id) else (winner_id or "—")}</div>
                  <div><strong>Precio:</strong> {_fmt_money(winner_price)}</div>
                </div>
                <hr>
                <h6 class="mb-2">Boosts</h6>
                {_render_boosts_list(winner_boosts)}
              </div>
            </div>
          </div>
        </div>
        """)    

        return "".join(html_parts)


    # -------------------------------
    # Caso: /items común
    # -------------------------------
    elif resource.startswith("/items/MLA"):
        item_id = data.get("id")
        permalink = data.get("permalink")
        catalog_product_id = data.get("catalog_product_id")
        ml_url = None
        if item_id and catalog_product_id:
            ml_url = f"https://www.mercadolibre.com.ar/p/{catalog_product_id}?pdp_filters=item_id:{item_id}"
        elif item_id:
            ml_url = permalink

        if item_id and ml_url:
            html_parts.append(make_item_card(item_id, ml_url, data))

    elif resource.startswith("/seller-promotions/"):
        token = get_token()
        url = f"https://api.mercadolibre.com{resource}?app_version=v2"
        res = requests.get(url, headers={"Authorization": f"Bearer {token}"})

        if res.status_code != 200:
            html_parts.append(
                f"<div class='alert alert-danger'>❌ Error {res.status_code} consultando {resource}: {res.text}</div>"
            )
            return "".join(html_parts)

        offer_data = res.json()

        # 1) Mostrar el JSON de la offer
        html_parts.append(render_json_as_html(offer_data))

        # 2) Si la offer trae item_id, renderizar ABAJO la vista de /items/{id}/price_to_win
        item_id = offer_data.get("item_id")
        if item_id:
            try:
                ptw_res = requests.get(
                    f"https://api.mercadolibre.com/items/{item_id}/price_to_win?version=v2",
                    headers={"Authorization": f"Bearer {token}"}
                )
                if ptw_res.status_code == 200:
                    ptw_data = ptw_res.json()
                    html_parts.append("<h4 class='mt-4'>🏁 Catálogo</h4>")
                    # 👉 reusar la misma lógica de tu función, renderizando price_to_win dentro de offer
                    html_parts.append(render_ml_view(f"/items/{item_id}/price_to_win?version=v2", ptw_data))
                else:
                    html_parts.append(
                        f"<div class='alert alert-warning'>⚠️ No se pudo cargar price_to_win de {item_id}: {ptw_res.text}</div>"
                    )
            except Exception as e:
                html_parts.append(
                    f"<div class='alert alert-warning'>⚠️ Error al cargar price_to_win de {item_id}: {e}</div>"
                )

        # Importante: cortamos acá para no ejecutar el bloque genérico final
        return "".join(html_parts)

    # -------------------------------
    # Siempre: tabla JSON
    # -------------------------------
    html_parts.append(render_json_as_html(data))
    return "".join(html_parts)


def make_item_card(item_id, ml_url, ml_data=None):
    """Helper para renderizar una card de un item MLA"""
    try:
        if not ml_data:
            token = get_token()
            res = requests.get(
                f"https://api.mercadolibre.com/items/{item_id}",
                headers={"Authorization": f"Bearer {token}"}
            )
            ml_data = res.json()
    except Exception:
        ml_data = {}

    title = ml_data.get("title", f"Item {item_id}")
    price = ml_data.get("price", "—")
    currency = ml_data.get("currency_id", "")
    thumbnail = ml_data.get("thumbnail", "")

    return f"""
        <h3>Producto:</h3>
        <a href="{ml_url}" target="_blank" rel="noopener noreferrer" class="text-decoration-none text-reset">
          <div class="card mb-3 bg-dark text-light border-secondary" style="max-width: 540px;">
            <div class="row g-0">
              <div class="col-md-4 d-flex align-items-center justify-content-center p-2">
                <img src="{thumbnail}" alt="{title}" class="img-fluid rounded-start" style="max-height: 100px; object-fit: cover;" />
              </div>
              <div class="col-md-8">
                <div class="card-body">
                  <h5 class="card-title">{title}</h5>
                  <p class="card-text">{currency} {price}</p>
                  <p class="card-text"><small class="text-muted">Click para ver en Mercado Libre</small></p>
                </div>
              </div>
            </div>
          </div>
        </a>
    """

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

        # Insert ÚNICO: exactamente el resource recibido
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
                        evento.get("_id"),  # UUID válido requerido por tu esquema
                    ),
                )
                results["insert_original"] = {
                    "rowcount": cur.rowcount,
                    "webhook_id": evento.get("_id"),
                }
        except Exception as e:
            results["errors"].append(f"insert_original: {e}")

        # Refrescar preview del MISMO resource (no rompe el webhook si falla)
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
        # último recurso
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

        # 1) Total correcto: cantidad de resources únicos (último evento por resource dentro del topic)
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

        # 2) Filas a listar (último evento por resource) + preview por el MISMO resource
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
                    p.title, p.price, p.currency_id, p.thumbnail, p.winner, p.winner_price, p.status, w.received_at,p.brand
                FROM latest
                JOIN webhooks w
                  ON w.resource = latest.resource
                 AND w.received_at = latest.max_received
                LEFT JOIN ml_previews p
                  ON p.resource = w.resource        
                ORDER BY w.received_at DESC
                LIMIT %s OFFSET %s
            """, (topic, limit, offset))
            rows_db = cur.fetchall()  # 👈 leemos TODO adentro del with

        # 3) Construcción de respuesta (ya fuera del with: el cursor está cerrado)
        rows = []
        for row in rows_db:
            payload = row[0]
            # payload puede venir como jsonb (dict) o como string
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

            # Adjuntamos preview siempre (si no hay, vendrá con None en sus campos)
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





@app.route("/api/ml/render")
def render_meli_resource():
    resource = request.args.get("resource")
    if not resource:
        return "Falta el parámetro 'resource'", 400

    try:
        token = get_token()

        if "/price_to_win" in resource:
            resource += ("&" if "?" in resource else "?") + "version=v2"
        
        res = requests.get(
            f"https://api.mercadolibre.com{resource}",
            headers={"Authorization": f"Bearer {token}"}
        )
        data = res.json()
        
        

        body = render_ml_view(resource, data)

        final_html = f"""
        <html>
          <head>
            <meta charset="utf-8">
            <title>Consultas ML API</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            <link rel="icon" href={FAVICON_DIR}>
            <link rel="apple-touch-icon" href={FAVICON_DIR}>
          </head>
          <body class="bg-dark text-light p-3" data-bs-theme="dark">
            {body}
          </body>
        </html>
        """
        return final_html, 200
    except Exception as e:
        print("❌ Error en renderizado:", e)
        return "Error interno en renderizado", 500

    except Exception as e:
        print("❌ Error en renderizado:", e)
        return "Error interno en renderizado", 500

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



@app.route("/api/ml/preview", methods=["GET", "POST"])
def ml_preview():
    resource = request.args.get("resource")
    if not resource:
        return jsonify({"error": "Falta parámetro resource"}), 400

    if not resource.startswith("/items/MLA"):
        return jsonify({"error": "Solo se soportan resources de items"}), 400

    return jsonify(fetch_and_store_preview(resource))

@app.route("/consulta", methods=["GET", "POST"])
def consulta():
    item_id = None
    mode = "items"
    data = None
    error = None

    if request.method == "POST":
        item_id = request.form.get("item_id")
        mode = request.form.get("mode", "price_to_win")

        if item_id:
            resource = f"/items/{item_id}" if mode == "items" else f"/items/{item_id}/price_to_win?version=v2"

            try:
                token = get_token()
                headers = {"Authorization": f"Bearer {token}"}
                res = requests.get(f"https://api.mercadolibre.com{resource}", headers=headers)
                data = res.json()
            except Exception as e:
                error = str(e)

    # render HTML
    html_parts = [
        f"""
        <html>
        <head>
            <title>Consultas ML API</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            <link rel="icon" href={FAVICON_DIR}>
            <link rel="apple-touch-icon" href={FAVICON_DIR}>
        </head>
        <body class="bg-dark text-light p-3" data-bs-theme="dark">
            <div class="container">
            <h2 class="mb-3">🔍 Consulta manual de MLA</h2>
            <form method="POST" class="mb-4">
                <div class="input-group mb-3">
                <input type="text" class="form-control" name="item_id" placeholder="Ej: MLA123456" required>
                <select class="form-select" name="mode">
                    <option value="items" {"selected" if mode == "items" else ""}>Consulta Items</option>
                    <option value="price_to_win" {"selected" if mode == "price_to_win" else ""}>Consulta Price to Win</option>
                </select>
                <button class="btn btn-primary" type="submit">Consultar</button>
                </div>
            </form>
        """
    ]

    if error:
        html_parts.append(f"<div class='alert alert-danger'>❌ Error: {error}</div>")

    if data:
        html_parts.append(render_ml_view(resource, data))

    html_parts.append("""
            </div>
            <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
        </body></html>
    """)

    return "".join(html_parts)


# frontend
@app.route("/")
def index():
    return send_from_directory("frontend/dist", "index.html")

@app.route("/<path:path>")
def assets(path):
    return send_from_directory("frontend/dist", path)

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

@app.route("/catalogByEan")
def catalog_by_ean():
    site = request.args.get("site", "MLA")
    ean = request.args.get("ean")
    if not ean:
        return "Falta parámetro 'ean'", 400

    try:
        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"https://api.mercadolibre.com/products/search?site_id={site}&product_identifier={ean}"
        res = requests.get(url, headers=headers)
        data = res.json()

        body = render_json_as_html(data)
        final_html = f"""
        <html>
          <head>
            <meta charset="utf-8">
            <title>Catalog by EAN</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            <link rel="icon" href={FAVICON_DIR}>
            <link rel="apple-touch-icon" href={FAVICON_DIR}>
          </head>
          <body class="bg-dark text-light p-3" data-bs-theme="dark">
            <h3>📦 Catálogo por EAN</h3>
            {body}
          </body>
        </html>
        """
        return final_html, res.status_code
    except Exception as e:
        return f"❌ Error: {e}", 500


@app.route("/listingsByCatalog")
def listings_by_catalog():
    site = request.args.get("site", "MLA")
    catalog_id = request.args.get("catalog_id")
    if not catalog_id:
        return "Falta parámetro 'catalog_id'", 400

    try:
        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"https://api.mercadolibre.com/sites/{site}/search?catalog_product_id={catalog_id}"
        res = requests.get(url, headers=headers)
        data = res.json()

        body = render_json_as_html(data)
        final_html = f"""
        <html>
          <head>
            <meta charset="utf-8">
            <title>Listings by Catalog</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            <link rel="icon" href={FAVICON_DIR}>
            <link rel="apple-touch-icon" href={FAVICON_DIR}>
          </head>
          <body class="bg-dark text-light p-3" data-bs-theme="dark">
            <h3>🔍 Listings por catálogo</h3>
            {body}
          </body>
        </html>
        """
        return final_html, res.status_code
    except Exception as e:
        return f"❌ Error: {e}", 500

@app.route("/debug/token")
def debug_token():
    try:
        token = get_token()
        return jsonify({
            "access_token": token,
            "expires_at": EXPIRATION,
            "expires_in_seconds": int(EXPIRATION - time.time())
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
