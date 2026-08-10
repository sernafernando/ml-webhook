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

    res = client.post("/api/pxq/item/MLA123", json={"prices": [{"id": "1"}]})

    assert res.status_code == 500


def test_post_pxq_no_reintenta(client, monkeypatch):
    intentos = {"n": 0}

    def fake_request(*a, **k):
        intentos["n"] += 1
        return _Resp(status_code=502, payload={"error": "bad gateway"})

    monkeypatch.setattr(app_module.requests, "request", fake_request)

    res = client.post("/api/pxq/item/MLA123", json={"prices": [{"id": "1"}]})

    assert res.status_code == 502
    assert intentos["n"] == 1


def test_post_pxq_body_invalido_es_400(client, monkeypatch):
    def fake_request(*a, **k):  # pragma: no cover - no debe llamarse
        raise AssertionError("no debe salir tráfico a ML con body inválido")

    monkeypatch.setattr(app_module.requests, "request", fake_request)

    assert client.post("/api/pxq/item/MLA123", json={}).status_code == 400
    assert client.post("/api/pxq/item/MLA123", json={"prices": "x"}).status_code == 400
    assert client.post(
        "/api/pxq/item/MLA123", json={"prices": [{"amount": 100}]},
    ).status_code == 400
