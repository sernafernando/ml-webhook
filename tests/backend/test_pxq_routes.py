import json

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


class _Resp:
    def __init__(self, status_code=200, payload=None, content=b"{}", text="{}"):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


def _patch_pxq_read(monkeypatch, prices=None, status_code=200):
    """El POST lee /prices antes de escribir para reinyectar los nodos ajenos.
    Sin este mock los tests de escritura saldrian a la red real."""
    payload = {"prices": [] if prices is None else prices}
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _Resp(status_code=status_code, payload=payload),
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


# -------------------- Lectura --------------------

def test_get_pxq_devuelve_array_pelado_y_aplanado(client, monkeypatch):
    calls = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["url"] = url
        calls["timeout"] = timeout
        return _Resp(payload={"prices": [
            {"id": "0", "amount": 3000, "type": "standard"},
            {"id": "1", "amount": 2900, "conditions": {"min_purchase_unit": 5}},
            {"id": "2", "amount": 2850, "conditions": {"min_purchase_unit": 10}},
        ]})

    monkeypatch.setattr(app_module.requests, "get", fake_get)

    res = client.get("/api/pxq/item/MLA123")

    assert res.status_code == 200
    assert calls["url"] == "https://api.mercadolibre.com/items/MLA123/prices"
    assert calls["timeout"] == app_module.PXQ_READ_TIMEOUT
    assert json.loads(res.data) == [
        {"id": "1", "quantity": 5, "amount": 2900},
        {"id": "2", "quantity": 10, "amount": 2850},
    ]


def test_get_pxq_pide_show_all_prices(client, monkeypatch):
    """Sin este header ML responde 200 omitiendo los nodos con
    min_purchase_unit: los tramos vivos se leen como 'no hay tramos'."""
    calls = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["headers"] = headers or {}
        return _Resp(payload={"prices": []})

    monkeypatch.setattr(app_module.requests, "get", fake_get)

    client.get("/api/pxq/item/MLA123")

    enviados = {k.lower(): v for k, v in calls["headers"].items()}
    assert enviados.get("show-all-prices") == "true"
    assert enviados.get("authorization") == "Bearer TOKEN"


def test_get_pxq_sin_tramos_devuelve_lista_vacia(client, monkeypatch):
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _Resp(payload={"prices": [{"id": "0", "amount": 3000}]}),
    )

    res = client.get("/api/pxq/item/MLA123")

    assert res.status_code == 200
    assert json.loads(res.data) == []


def test_get_pxq_no_cachea(client, monkeypatch):
    monkeypatch.setattr(
        app_module.requests, "get", lambda *a, **k: _Resp(payload={"prices": []}),
    )

    res = client.get("/api/pxq/item/MLA123")

    assert res.headers["Cache-Control"] == "no-store"


def test_get_pxq_preserva_status_de_ml(client, monkeypatch):
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _Resp(status_code=404, payload={"error": "not_found"}),
    )

    res = client.get("/api/pxq/item/MLA123")

    assert res.status_code == 404


def test_get_pxq_forma_inesperada_es_502_y_no_lista_vacia(client, monkeypatch):
    """[] afirma 'ML no tiene tramos'. Si no entendimos el cuerpo no tenemos
    esa afirmacion: el consumidor debe leer 'desconocido', no 'cero'."""
    casos = [
        {},                       # 200 sin clave prices
        {"prices": None},         # prices nulo
        {"prices": {"1": {}}},    # prices no es lista
        [],                       # cuerpo no es objeto
    ]

    for payload in casos:
        monkeypatch.setattr(
            app_module.requests, "get", lambda *a, **k: _Resp(payload=payload),
        )
        res = client.get("/api/pxq/item/MLA123")
        assert res.status_code == 502, f"payload {payload!r} deberia ser 502"
        assert json.loads(res.data) != []


def test_get_pxq_200_no_json_es_502(client, monkeypatch):
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _Resp(payload=None, content=b"<html>", text="<html>"),
    )

    res = client.get("/api/pxq/item/MLA123")

    assert res.status_code == 502


def test_get_pxq_cero_tramos_legitimo_sigue_siendo_200(client, monkeypatch):
    """El 502 de forma inesperada no debe tapar el 'cero' verdadero."""
    monkeypatch.setattr(
        app_module.requests, "get", lambda *a, **k: _Resp(payload={"prices": []}),
    )

    res = client.get("/api/pxq/item/MLA123")

    assert res.status_code == 200
    assert json.loads(res.data) == []


# -------------------- Escritura --------------------

def test_post_pxq_traduce_forma_simplificada(client, monkeypatch):
    sent = {}

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        sent["method"] = method
        sent["url"] = url
        sent["json"] = json
        sent["timeout"] = timeout
        return _Resp(payload={"prices": []})

    monkeypatch.setattr(app_module.requests, "request", fake_request)
    _patch_pxq_read(monkeypatch)

    res = client.post(
        "/api/pxq/item/MLA123",
        json={"prices": [{"id": "2"}, {"quantity": 10, "amount": 2850}]},
    )

    assert res.status_code == 200
    assert sent["method"] == "POST"
    assert sent["url"] == "https://api.mercadolibre.com/items/MLA123/prices/standard/quantity"
    assert sent["timeout"] == app_module.PXQ_WRITE_TIMEOUT
    assert sent["json"] == {"prices": [
        {"id": "2"},
        {
            "amount": 2850,
            "currency_id": "ARS",
            "conditions": {
                "context_restrictions": ["channel_marketplace", "user_type_business"],
                "min_purchase_unit": 10,
            },
        },
    ]}


def test_post_pxq_no_reordena_ni_deduplica(client, monkeypatch):
    sent = {}

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        sent["json"] = json
        return _Resp(payload={})

    monkeypatch.setattr(app_module.requests, "request", fake_request)
    _patch_pxq_read(monkeypatch)

    client.post("/api/pxq/item/MLA123", json={"prices": [
        {"quantity": 20, "amount": 2800},
        {"id": "1"},
        {"quantity": 5, "amount": 2900},
    ]})

    enviado = sent["json"]["prices"]
    assert len(enviado) == 3
    assert enviado[0]["conditions"]["min_purchase_unit"] == 20
    assert enviado[1] == {"id": "1"}
    assert enviado[2]["conditions"]["min_purchase_unit"] == 5


def test_post_pxq_preserva_5xx_de_ml(client, monkeypatch):
    monkeypatch.setattr(
        app_module.requests, "request",
        lambda *a, **k: _Resp(status_code=500, payload={"error": "internal"}),
    )
    _patch_pxq_read(monkeypatch)

    res = client.post("/api/pxq/item/MLA123", json={"prices": [{"id": "1"}]})

    assert res.status_code == 500


def test_post_pxq_no_reintenta(client, monkeypatch):
    intentos = {"n": 0}

    def fake_request(*a, **k):
        intentos["n"] += 1
        return _Resp(status_code=502, payload={"error": "bad gateway"})

    monkeypatch.setattr(app_module.requests, "request", fake_request)
    _patch_pxq_read(monkeypatch)

    res = client.post("/api/pxq/item/MLA123", json={"prices": [{"id": "1"}]})

    assert res.status_code == 502
    assert intentos["n"] == 1


def test_post_pxq_body_invalido_es_400(client, monkeypatch):
    def sin_trafico(*a, **k):  # pragma: no cover - no debe llamarse
        raise AssertionError("no debe salir tráfico a ML con body inválido")

    monkeypatch.setattr(app_module.requests, "request", sin_trafico)
    monkeypatch.setattr(app_module.requests, "get", sin_trafico)

    assert client.post("/api/pxq/item/MLA123", json={}).status_code == 400
    assert client.post("/api/pxq/item/MLA123", json={"prices": "x"}).status_code == 400
    assert client.post(
        "/api/pxq/item/MLA123", json={"prices": [{"amount": 100}]},
    ).status_code == 400


# -------------------- Escritura: reinyeccion de nodos ajenos --------------------

# Nodos que NO son tramos PxQ: precio estandar de cada canal + promocion viva.
# El cliente nunca los ve (el GET los filtra) y por lo tanto nunca los manda.
NODOS_AJENOS = [
    {"id": "3388", "amount": 3000, "type": "standard"},
    {"id": "3117", "amount": 3000, "type": "standard"},
    {"id": "3386", "amount": 2700, "type": "promotion"},
]


def test_post_pxq_reinyecta_nodos_sin_min_purchase_unit(client, monkeypatch):
    """Sin esto la primera escritura real borra el precio estandar y las promos:
    el POST de ML reemplaza el array completo."""
    sent = {}

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        sent["json"] = json
        return _Resp(payload={})

    monkeypatch.setattr(app_module.requests, "request", fake_request)
    _patch_pxq_read(monkeypatch, prices=NODOS_AJENOS + [
        {"id": "3390", "amount": 2900, "conditions": {"min_purchase_unit": 5}},
    ])

    client.post("/api/pxq/item/MLA123", json={"prices": [
        {"quantity": 2, "amount": 2950},
        {"quantity": 5, "amount": 2900},
    ]})

    enviado = sent["json"]["prices"]
    assert len(enviado) == 5
    # Los tramos del cliente van primero, los preservados detras.
    assert [p["conditions"]["min_purchase_unit"] for p in enviado[:2]] == [2, 5]
    assert enviado[2:] == [{"id": "3388"}, {"id": "3117"}, {"id": "3386"}]
    # El tramo PxQ vivo NO se reinyecta: es responsabilidad del cliente.
    assert {"id": "3390"} not in enviado


def test_post_pxq_no_duplica_id_ya_enviado_por_el_cliente(client, monkeypatch):
    """ML devuelve los ids como int o str indistintamente: la comparacion es
    por str, no por identidad de tipo."""
    sent = {}

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        sent["json"] = json
        return _Resp(payload={})

    monkeypatch.setattr(app_module.requests, "request", fake_request)
    _patch_pxq_read(monkeypatch, prices=[
        {"id": 3388, "amount": 3000},
        {"id": "3386", "amount": 2700},
    ])

    client.post("/api/pxq/item/MLA123", json={"prices": [
        {"id": "3388"},
        {"quantity": 10, "amount": 2850},
    ]})

    enviado = sent["json"]["prices"]
    ids = [str(p["id"]) for p in enviado if "id" in p]
    assert ids.count("3388") == 1
    assert ids == ["3388", "3386"]
    assert len(enviado) == 3


def test_post_pxq_lectura_previa_fallida_es_502_y_no_escribe(client, monkeypatch):
    """FALLA CERRADO. Si no sabemos que hay del otro lado, escribir es borrar."""
    escrituras = {"n": 0}

    def fake_request(*a, **k):
        escrituras["n"] += 1
        return _Resp(payload={})

    monkeypatch.setattr(app_module.requests, "request", fake_request)
    _patch_pxq_read(monkeypatch, status_code=500)

    res = client.post("/api/pxq/item/MLA123", json={"prices": [
        {"quantity": 10, "amount": 2850},
    ]})

    assert res.status_code == 502
    assert escrituras["n"] == 0, "no debe escribirse a ML si la lectura previa fallo"


def test_post_pxq_lectura_previa_con_timeout_es_504_y_no_escribe(client, monkeypatch):
    escrituras = {"n": 0}

    def fake_request(*a, **k):
        escrituras["n"] += 1
        return _Resp(payload={})

    def fake_get(*a, **k):
        raise app_module.requests.Timeout("read timeout")

    monkeypatch.setattr(app_module.requests, "request", fake_request)
    monkeypatch.setattr(app_module.requests, "get", fake_get)

    res = client.post("/api/pxq/item/MLA123", json={"prices": [
        {"quantity": 10, "amount": 2850},
    ]})

    assert res.status_code == 504
    assert escrituras["n"] == 0


def test_post_pxq_tramos_nuevos_conservan_context_restrictions(client, monkeypatch):
    """La reinyeccion no debe contaminar la forma de los tramos nuevos."""
    sent = {}

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        sent["json"] = json
        return _Resp(payload={})

    monkeypatch.setattr(app_module.requests, "request", fake_request)
    _patch_pxq_read(monkeypatch, prices=NODOS_AJENOS)

    client.post("/api/pxq/item/MLA123", json={"prices": [
        {"quantity": 2, "amount": 2950},
    ]})

    tramo = sent["json"]["prices"][0]
    assert tramo == {
        "amount": 2950,
        "currency_id": "ARS",
        "conditions": {
            "context_restrictions": ["channel_marketplace", "user_type_business"],
            "min_purchase_unit": 2,
        },
    }


def test_post_pxq_sin_nodos_ajenos_no_cambia_el_payload(client, monkeypatch):
    """Sin regresion: si ML no tiene nodos ajenos, sale exactamente lo de antes."""
    sent = {}

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        sent["json"] = json
        return _Resp(payload={})

    monkeypatch.setattr(app_module.requests, "request", fake_request)
    _patch_pxq_read(monkeypatch, prices=[
        {"id": "3390", "amount": 2900, "conditions": {"min_purchase_unit": 5}},
    ])

    res = client.post("/api/pxq/item/MLA123", json={
        "prices": [{"id": "2"}, {"quantity": 10, "amount": 2850}],
    })

    assert res.status_code == 200
    assert sent["json"] == {"prices": [
        {"id": "2"},
        {
            "amount": 2850,
            "currency_id": "ARS",
            "conditions": {
                "context_restrictions": ["channel_marketplace", "user_type_business"],
                "min_purchase_unit": 10,
            },
        },
    ]}
