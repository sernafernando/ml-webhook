"""Puente de actividad: cada notificacion de ML marca "esta venta se movio".

La vista de ventas ordena por ultima actividad, y eso no sale de la orden:
date_last_updated se mueve cuando cambia la orden, no cuando llega un mensaje o
se abre un reclamo. Solo el webhook sabe que paso algo.

Lo que cruza el puente es el HECHO, no el contenido. Los recursos que resuelven
el vinculo traen la direccion del comprador (/shipments/<id>.receiver_address) y
el texto de las conversaciones (/messages/<uuid>), asi que el vinculo se resuelve
de este lado y afuera sale un id, un tipo y una fecha.
"""
from contextlib import contextmanager

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover - entorno sin DB/Redis
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


@pytest.fixture(autouse=True)
def token_fijo(monkeypatch):
    """get_token() va a la base a buscar el token. Estos tests miden la
    resolucion del vinculo, no el refresh."""
    if app_module is not None:
        monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN")


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


# =====================================================================
# Resolver el vinculo evento -> venta
# =====================================================================

def test_orders_v2_sale_del_propio_resource_sin_llamadas(monkeypatch):
    """Es el unico que no cuesta una llamada: el id esta en el path."""
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    assert app_module.resolver_vinculo_actividad(
        "orders_v2", "/orders/2000018323050910") == (2000018323050910, None)


def test_shipments_resuelve_la_orden(monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp({"id": 47963823105,
                                               "order_id": 2000018265495500}))

    assert app_module.resolver_vinculo_actividad(
        "shipments", "/shipments/47963823105") == (2000018265495500, None)


def test_post_purchase_resuelve_la_orden_del_reclamo(monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp({"id": 5573505702, "resource": "order",
                                               "resource_id": 2000018128435014}))

    assert app_module.resolver_vinculo_actividad(
        "post_purchase", "/post-purchase/v1/claims/5573505702") == (2000018128435014, None)


def test_un_reclamo_sobre_algo_que_no_es_una_orden_no_inventa_vinculo(monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp({"resource": "shipment", "resource_id": 47}))

    assert app_module.resolver_vinculo_actividad(
        "post_purchase", "/post-purchase/v1/claims/1") == (None, None)


def test_payments_va_a_mercadopago_pese_a_que_el_resource_dice_collections(monkeypatch):
    """ML manda /collections/<id>, pero el pago vive en /v1/payments/<id> de MP."""
    urls = []

    def _fake(url, **kwargs):
        urls.append(url)
        return _Resp({"id": 176997615635, "order": {"id": 2000018325540962}})

    monkeypatch.setattr(app_module, "ml_api_get", _fake)

    assert app_module.resolver_vinculo_actividad(
        "payments", "/collections/176997615635") == (2000018325540962, None)
    assert urls == ["https://api.mercadopago.com/v1/payments/176997615635"]


def test_messages_devuelve_el_pack_y_no_gasta_un_salto_mas(monkeypatch):
    """message_resources da el pack, no la orden. Alcanza: la vista agrupa por
    pack, asi que saltar de nuevo para llegar a la orden no compraria nada."""
    urls = []

    def _fake(url, **kwargs):
        urls.append(url)
        return _Resp({"messages": [{"message_resources": [
            {"id": "2000014880973757", "name": "packs"},
            {"id": "413658225", "name": "sellers"},
        ]}]})

    monkeypatch.setattr(app_module, "ml_api_get", _fake)

    assert app_module.resolver_vinculo_actividad(
        "messages", "01a08274528a7b8b9cea3cb6204b6c52") == (None, 2000014880973757)
    assert len(urls) == 1
    assert "tag=post_sale" in urls[0]


def test_questions_no_tiene_vinculo_con_ninguna_venta(monkeypatch):
    """Una pregunta es preventa: tiene item_id y ninguna orden. Pedirsela a ML
    seria gastar una llamada para no encontrar nada."""
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    assert app_module.resolver_vinculo_actividad("questions", "/questions/123") == (None, None)


def test_si_ml_falla_no_se_pierde_el_evento(monkeypatch):
    """La resolucion es best-effort: sin vinculo el evento igual se registra."""
    def _boom(*a, **k):
        raise RuntimeError("ML no responde")

    monkeypatch.setattr(app_module, "ml_api_get", _boom)

    assert app_module.resolver_vinculo_actividad(
        "shipments", "/shipments/47963823105") == (None, None)


# =====================================================================
# El handler registra la actividad sin dejar de responder 200
# =====================================================================

class _Cursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._result = []

    def execute(self, query, params=None):
        q = " ".join(query.split())
        if "INSERT INTO webhooks" in q:
            self.rowcount = 1
        elif "INSERT INTO ml_activity" in q:
            self.db["actividad"].append(params)
            self.rowcount = 1
        elif "FROM ml_activity" in q:
            self._result = self.db["filas"]
        else:
            self.rowcount = 1

    def fetchone(self):
        return self._result[0] if self._result else (0,)

    def fetchall(self):
        return self._result


@pytest.fixture
def entorno(monkeypatch):
    db = {"actividad": [], "filas": []}

    @contextmanager
    def fake_db_cursor():
        yield _Cursor(db)

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    monkeypatch.setattr(app_module, "WEBHOOK_PREVIEW_ASYNC", True)
    monkeypatch.setattr(app_module, "_enqueue_preview_job", lambda resource: (True, None))
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c, db


def _evento(topic, resource, _id="abc-1"):
    return {"_id": _id, "topic": topic, "resource": resource, "user_id": 413658225,
            "sent": "2026-09-08T19:23:47.571Z", "received": "2026-09-08T19:23:45.349Z",
            "attempts": 1}


def test_el_webhook_registra_actividad_de_un_topic_del_puente(entorno, monkeypatch):
    client, db = entorno

    res = client.post("/webhook", json=_evento("orders_v2", "/orders/2000018323050910"))

    assert res.status_code == 200
    assert len(db["actividad"]) == 1


def test_un_topic_fuera_del_puente_no_registra_nada(entorno, monkeypatch):
    """items son 4,3 millones de eventos y no son actividad de una venta."""
    client, db = entorno

    res = client.post("/webhook", json=_evento("items", "/items/MLA123"))

    assert res.status_code == 200
    assert db["actividad"] == []


def test_si_registrar_la_actividad_falla_el_webhook_igual_responde_200(entorno, monkeypatch):
    """Perder una fila de actividad es recuperable; que ML reintente porque no
    le contestamos 200 es peor, y tiene limite."""
    client, db = entorno
    monkeypatch.setattr(app_module, "registrar_actividad",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db caida")))

    assert client.post("/webhook",
                       json=_evento("orders_v2", "/orders/1")).status_code == 200


# =====================================================================
# El endpoint de lectura
# =====================================================================

FILAS = [
    # (id, topic, resource, order_id, pack_id, sent, occurred_at)
    (101, "orders_v2", "/orders/2000018323050910", 2000018323050910, None,
     "2026-09-08T19:23:47.571Z", "2026-09-08T19:23:48"),
    (102, "messages", "01a08274528a7b8b9cea3cb6204b6c52", None, 2000014880973757,
     "2026-09-08T19:25:01.000Z", "2026-09-08T19:25:02"),
]


@pytest.fixture
def lector(monkeypatch):
    db = {"actividad": [], "filas": FILAS, "consultas": []}

    class _C(_Cursor):
        def execute(self, query, params=None):
            self.db["consultas"].append((" ".join(query.split()), params))
            super().execute(query, params)

    @contextmanager
    def fake_db_cursor():
        yield _C(db)

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c, db


def test_devuelve_los_eventos_con_su_vinculo_y_el_sent_de_ml(lector):
    client, _ = lector

    d = client.get("/api/ml/activity").get_json()

    assert len(d["events"]) == 2
    primero = d["events"][0]
    assert primero["topic"] == "orders_v2"
    assert primero["order_id"] == 2000018323050910
    assert primero["pack_id"] is None
    assert primero["resource"] == "/orders/2000018323050910"
    # sent es de ML: es contra lo que el consumidor ordena, no contra occurred_at.
    assert primero["sent"] == "2026-09-08T19:23:47.571Z"
    assert primero["occurred_at"]


def test_no_devuelve_contenido_del_comprador(lector):
    """El puente manda el hecho, no el mensaje ni la direccion."""
    client, _ = lector

    crudo = client.get("/api/ml/activity").get_data(as_text=True)
    d = client.get("/api/ml/activity").get_json()

    for prohibido in ("text", "subject", "receiver_address", "buyer", "payload", "from"):
        assert prohibido not in crudo
    assert set(d["events"][0]) == {
        "topic", "order_id", "pack_id", "resource", "occurred_at", "sent"}


def test_el_cursor_es_opaco_y_avanza(lector):
    client, _ = lector

    d = client.get("/api/ml/activity").get_json()

    assert d["next_cursor"]
    # Opaco: el consumidor lo guarda y lo devuelve, no lo interpreta.
    assert "101" not in d["next_cursor"] and "102" not in d["next_cursor"]


def test_el_cursor_filtra_por_lo_ya_entregado(lector):
    client, db = lector
    cursor = client.get("/api/ml/activity").get_json()["next_cursor"]

    client.get("/api/ml/activity", query_string={"since": cursor})

    ultima, params = db["consultas"][-1]
    assert "id > %s" in ultima
    assert params[0] == 102


def test_un_cursor_invalido_no_devuelve_todo_de_nuevo(lector):
    """Devolver todo desde cero ante un cursor roto es peor que fallar: el
    consumidor reprocesa el historico creyendo que son novedades."""
    client, _ = lector

    res = client.get("/api/ml/activity", query_string={"since": "no-es-un-cursor"})

    assert res.status_code == 400
    assert res.is_json


def test_filtra_por_topics(lector):
    client, db = lector

    client.get("/api/ml/activity", query_string={"topics": "orders_v2,payments"})

    ultima, params = db["consultas"][-1]
    assert "topic = ANY" in ultima
    # params: (ultimo_id, topics, limit)
    assert params[1] == ["orders_v2", "payments"]


def test_un_topic_desconocido_se_rechaza(lector):
    client, _ = lector

    res = client.get("/api/ml/activity", query_string={"topics": "items"})

    assert res.status_code == 400


def test_no_acepta_post(lector):
    client, _ = lector
    assert client.post("/api/ml/activity").status_code == 405


# =====================================================================
# El ping: avisa que hay novedades, no las manda
# =====================================================================
# El dato viaja por el canal confiable (el pull con cursor); la urgencia, por el
# canal barato. Si el ping se pierde, no se pierde nada: el proximo pull lo
# levanta igual. En cuanto alguien dependa de que el ping llegue siempre,
# volvimos al push con sus problemas y sin sus garantias.

def test_sin_destino_configurado_no_intenta_nada(monkeypatch):
    """Por defecto el ping esta inerte: nadie tiene que apagarlo."""
    monkeypatch.setattr(app_module, "ACTIVITY_PING_URL", None)
    monkeypatch.setattr(app_module.requests, "post",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    app_module.notificar_actividad("orders_v2")


def test_avisa_al_destino_configurado(monkeypatch):
    llamadas = []
    monkeypatch.setattr(app_module, "ACTIVITY_PING_URL", "https://pricing/interno/ping")
    monkeypatch.setattr(app_module.requests, "post",
                        lambda url, **kw: llamadas.append((url, kw)) or _Resp({}))

    app_module.notificar_actividad("orders_v2")

    assert llamadas[0][0] == "https://pricing/interno/ping"
    # Timeout obligatorio: un destino colgado no puede quedarse con el hilo.
    assert llamadas[0][1].get("timeout") is not None


def test_el_ping_no_lleva_datos(monkeypatch):
    """Solo dice 'vení a buscar'. Si llevara el evento, seria un push y habria
    que garantizar la entrega."""
    llamadas = []
    monkeypatch.setattr(app_module, "ACTIVITY_PING_URL", "https://pricing/interno/ping")
    monkeypatch.setattr(app_module.requests, "post",
                        lambda url, **kw: llamadas.append(kw) or _Resp({}))

    app_module.notificar_actividad("messages")

    kw = llamadas[0]
    assert not kw.get("json") and not kw.get("data")


def test_si_el_destino_esta_caido_no_pasa_nada(monkeypatch):
    """Best-effort de verdad: la excepcion no sube."""
    monkeypatch.setattr(app_module, "ACTIVITY_PING_URL", "https://pricing/interno/ping")

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(app_module.requests, "post", _boom)

    app_module.notificar_actividad("orders_v2")


@pytest.mark.parametrize("topic", ["items", "price_suggestion", "public_offers"])
def test_los_topics_de_catalogo_no_disparan_el_ping(monkeypatch, topic):
    """items y price_suggestion son el grueso del volumen y no son actividad de
    una venta: despertarian el pull para nada."""
    monkeypatch.setattr(app_module, "ACTIVITY_PING_URL", "https://pricing/interno/ping")
    monkeypatch.setattr(app_module.requests, "post",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    app_module.notificar_actividad(topic)


def test_el_ping_sale_al_registrar_actividad(entorno, monkeypatch):
    avisados = []
    monkeypatch.setattr(app_module, "notificar_actividad", lambda t: avisados.append(t))
    client, _ = entorno

    client.post("/webhook", json=_evento("orders_v2", "/orders/1"))

    assert avisados == ["orders_v2"]


# =====================================================================
# El ping se autentica contra el receptor
# =====================================================================
# El receptor exige Bearer y responde 401 sin el. Y requests NO levanta
# excepcion ante un 401, asi que un ping sin token no falla: no pasa nada, en
# silencio absoluto. Por eso ademas se mira el status de la respuesta.

def test_manda_el_token_como_bearer(monkeypatch):
    llamadas = []
    monkeypatch.setattr(app_module, "ACTIVITY_PING_URL", "https://pricing/interno/ping")
    monkeypatch.setattr(app_module, "ACTIVITY_PING_TOKEN", "TOKEN-DEL-PUENTE")
    monkeypatch.setattr(app_module.requests, "post",
                        lambda url, **kw: llamadas.append(kw) or _Resp({}))

    app_module.notificar_actividad("orders_v2")

    assert llamadas[0]["headers"]["Authorization"] == "Bearer TOKEN-DEL-PUENTE"


def test_sin_token_no_manda_header_vacio(monkeypatch):
    """Un 'Bearer ' pelado es peor que no mandar nada: parece configurado."""
    llamadas = []
    monkeypatch.setattr(app_module, "ACTIVITY_PING_URL", "https://pricing/interno/ping")
    monkeypatch.setattr(app_module, "ACTIVITY_PING_TOKEN", None)
    monkeypatch.setattr(app_module.requests, "post",
                        lambda url, **kw: llamadas.append(kw) or _Resp({}))

    app_module.notificar_actividad("orders_v2")

    assert "Authorization" not in (llamadas[0].get("headers") or {})


def test_el_token_no_se_loguea(monkeypatch, capsys):
    """Un 401 se avisa, pero el secreto no va al log."""
    monkeypatch.setattr(app_module, "ACTIVITY_PING_URL", "https://pricing/interno/ping")
    monkeypatch.setattr(app_module, "ACTIVITY_PING_TOKEN", "TOKEN-SECRETO-DEL-PUENTE")
    monkeypatch.setattr(app_module.requests, "post",
                        lambda url, **kw: _Resp({}, status_code=401))

    app_module.notificar_actividad("orders_v2")

    salida = capsys.readouterr().out
    assert "TOKEN-SECRETO-DEL-PUENTE" not in salida


def test_un_rechazo_del_receptor_se_avisa(monkeypatch, capsys):
    """requests no levanta excepcion ante un 401: sin este chequeo, un ping
    rechazado falla en silencio y nadie se entera hasta que alguien mira por que
    la vista no se actualiza."""
    monkeypatch.setattr(app_module, "ACTIVITY_PING_URL", "https://pricing/interno/ping")
    monkeypatch.setattr(app_module, "ACTIVITY_PING_TOKEN", "T")
    monkeypatch.setattr(app_module.requests, "post",
                        lambda url, **kw: _Resp({}, status_code=401))

    app_module.notificar_actividad("orders_v2")

    assert "401" in capsys.readouterr().out


def test_un_ping_aceptado_no_ensucia_el_log(monkeypatch, capsys):
    monkeypatch.setattr(app_module, "ACTIVITY_PING_URL", "https://pricing/interno/ping")
    monkeypatch.setattr(app_module, "ACTIVITY_PING_TOKEN", "T")
    monkeypatch.setattr(app_module.requests, "post",
                        lambda url, **kw: _Resp({}, status_code=202))

    app_module.notificar_actividad("orders_v2")

    assert capsys.readouterr().out == ""
