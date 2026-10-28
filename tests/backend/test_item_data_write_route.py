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
    monkeypatch.delenv("ITEM_DATA_WRITE_ENABLED", raising=False)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def ml_put(monkeypatch):
    """Registra cada PUT a ML; sin este mock los tests saldrian a la red real."""
    calls = []
    response = {"value": _Resp(payload={"id": "MLA123", "title": "Router"})}

    def fake_put(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        value = response["value"]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(app_module.requests, "put", fake_put)
    return calls, response


def _put(client, item_id="MLA123", body=None):
    return client.put(
        f"/api/items/{item_id}/data",
        json={"title": "Router"} if body is None else body,
    )


def test_escribe_el_titulo_con_un_solo_put_a_ml(client, ml_put):
    calls, _ = ml_put
    res = _put(client)
    assert res.status_code == 200
    assert json.loads(res.data) == {"id": "MLA123", "title": "Router"}
    assert calls == [{
        "url": "https://api.mercadolibre.com/items/MLA123",
        "headers": {"Authorization": "Bearer TOKEN"},
        "json": {"title": "Router"},
        "timeout": app_module.ITEM_DATA_WRITE_TIMEOUT,
    }]


def test_reenvia_las_tres_claves_tal_cual_y_en_orden(client, ml_put):
    calls, _ = ml_put
    cuerpo = {
        "title": "Router TP-Link Archer AX55 WiFi 6",
        "pictures": [{"id": "123-MLA456_789"}, {"id": "987-MLA654_321"}],
        "attributes": [{"id": "MODEL", "value_name": "Archer AX55 Pro"}],
    }
    assert _put(client, body=cuerpo).status_code == 200
    assert calls[0]["json"] == cuerpo
    assert [p["id"] for p in calls[0]["json"]["pictures"]] == ["123-MLA456_789", "987-MLA654_321"]


def test_descarta_las_claves_que_no_son_del_contrato(client, ml_put):
    calls, _ = ml_put
    res = _put(client, body={
        "title": "Router",
        "price": 99999,
        "status": "closed",
        "available_quantity": 0,
    })
    assert res.status_code == 200
    assert calls[0]["json"] == {"title": "Router"}


@pytest.mark.parametrize("body", [
    {},
    {"price": 99999},
    {"status": "active"},
    ["title"],
])
def test_sin_ninguna_de_las_tres_claves_es_422_y_no_escribe(client, ml_put, body):
    calls, _ = ml_put
    res = _put(client, body=body)
    assert res.status_code == 422
    assert json.loads(res.data)["data_write"] == app_module.ITEM_DATA_WRITE_NOT_ATTEMPTED
    assert calls == []


@pytest.mark.parametrize("item_id", ["123", "MLB123", "MLA12a", "mla123", "MLA"])
def test_un_item_que_no_es_mla_es_400_y_no_escribe(client, ml_put, item_id):
    calls, _ = ml_put
    res = _put(client, item_id=item_id)
    assert res.status_code == 400
    assert json.loads(res.data)["data_write"] == app_module.ITEM_DATA_WRITE_NOT_ATTEMPTED
    assert calls == []


def test_con_el_interruptor_apagado_es_503_y_no_escribe(client, ml_put, monkeypatch):
    calls, _ = ml_put
    monkeypatch.setenv("ITEM_DATA_WRITE_ENABLED", "0")
    res = _put(client)
    assert res.status_code == 503
    assert json.loads(res.data)["data_write"] == app_module.ITEM_DATA_WRITE_NOT_ATTEMPTED
    assert calls == []


def test_sin_token_es_503_not_attempted_y_no_escribe(client, ml_put, monkeypatch):
    calls, _ = ml_put
    monkeypatch.setattr(app_module, "get_token", lambda: None)
    res = _put(client)
    assert res.status_code == 503
    assert json.loads(res.data)["data_write"] == app_module.ITEM_DATA_WRITE_NOT_ATTEMPTED
    assert calls == []


def test_toda_respuesta_propia_del_proxy_trae_message(client, ml_put):
    for res in (
        _put(client, body={}),
        _put(client, item_id="MLB1"),
    ):
        cuerpo = json.loads(res.data)
        assert cuerpo["data_write"] == app_module.ITEM_DATA_WRITE_NOT_ATTEMPTED
        assert cuerpo["message"]


def test_el_rechazo_de_ml_pasa_tal_cual_sin_data_write(client, ml_put):
    _, response = ml_put
    response["value"] = _Resp(
        status_code=400,
        payload={"message": "Item has variations", "error": "validation_error"},
    )
    res = _put(client, body={"pictures": [{"id": "1-MLA2_3"}]})
    assert res.status_code == 400
    assert json.loads(res.data) == {"message": "Item has variations", "error": "validation_error"}


def test_un_5xx_de_ml_pasa_tal_cual_y_no_reintenta(client, ml_put):
    calls, response = ml_put
    response["value"] = _Resp(status_code=503, payload={"message": "unavailable"})
    res = _put(client)
    assert res.status_code == 503
    assert "data_write" not in json.loads(res.data)
    assert len(calls) == 1


def test_timeout_del_put_es_504_y_ambiguo(client, ml_put):
    _, response = ml_put
    response["value"] = app_module.requests.Timeout()
    res = _put(client)
    assert res.status_code == 504
    assert json.loads(res.data)["data_write"] == app_module.ITEM_DATA_WRITE_AMBIGUOUS
    assert json.loads(res.data)["message"]


def test_un_error_de_conexion_es_502_y_ambiguo(client, ml_put):
    _, response = ml_put
    response["value"] = app_module.requests.ConnectionError("reset")
    res = _put(client)
    assert res.status_code == 502
    assert json.loads(res.data)["data_write"] == app_module.ITEM_DATA_WRITE_AMBIGUOUS


def test_una_excepcion_cualquiera_es_502_y_ambigua(client, ml_put):
    _, response = ml_put
    response["value"] = RuntimeError("boom")
    res = _put(client)
    assert res.status_code == 502
    assert json.loads(res.data)["data_write"] == app_module.ITEM_DATA_WRITE_AMBIGUOUS


@pytest.mark.parametrize("raw", ["null", "[]", "no soy json"])
def test_un_cuerpo_que_no_es_un_objeto_json_es_422_y_no_escribe(client, ml_put, raw):
    calls, _ = ml_put
    res = client.put("/api/items/MLA123/data", data=raw, content_type="application/json")
    assert res.status_code == 422
    assert json.loads(res.data)["data_write"] == app_module.ITEM_DATA_WRITE_NOT_ATTEMPTED
    assert calls == []
