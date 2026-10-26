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
    monkeypatch.delenv("ITEM_STATUS_WRITE_ENABLED", raising=False)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def ml_put(monkeypatch):
    """Registra cada PUT a ML; sin este mock los tests saldrian a la red real."""
    calls = []
    response = {"value": _Resp(payload={"id": "MLA123", "status": "paused"})}

    def fake_put(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        value = response["value"]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(app_module.requests, "put", fake_put)
    return calls, response


def _put(client, item_id="MLA123", body=None):
    return client.put(f"/api/items/{item_id}/status", json={"status": "paused"} if body is None else body)


def test_pausa_la_publicacion_con_un_solo_put_a_ml(client, ml_put):
    calls, _ = ml_put
    res = _put(client)
    assert res.status_code == 200
    assert json.loads(res.data) == {"id": "MLA123", "status": "paused"}
    assert calls == [{
        "url": "https://api.mercadolibre.com/items/MLA123",
        "headers": {"Authorization": "Bearer TOKEN"},
        "json": {"status": "paused"},
        "timeout": app_module.ITEM_STATUS_WRITE_TIMEOUT,
    }]


def test_activa_la_publicacion(client, ml_put):
    calls, _ = ml_put
    assert _put(client, body={"status": "active"}).status_code == 200
    assert calls[0]["json"] == {"status": "active"}


@pytest.mark.parametrize("body", [{}, {"status": "closed"}, {"status": "PAUSED"}, {"status": None}, ["paused"]])
def test_un_status_que_no_es_paused_ni_active_es_400_y_no_escribe(client, ml_put, body):
    calls, _ = ml_put
    res = _put(client, body=body)
    assert res.status_code == 400
    assert json.loads(res.data)["status_write"] == app_module.ITEM_STATUS_WRITE_NOT_ATTEMPTED
    assert calls == []


@pytest.mark.parametrize("item_id", ["123", "MLB123", "MLA12a", "mla123"])
def test_un_item_que_no_es_mla_es_400_y_no_escribe(client, ml_put, item_id):
    calls, _ = ml_put
    res = _put(client, item_id=item_id)
    assert res.status_code == 400
    assert json.loads(res.data)["status_write"] == app_module.ITEM_STATUS_WRITE_NOT_ATTEMPTED
    assert calls == []


def test_con_el_interruptor_apagado_es_503_y_no_escribe(client, ml_put, monkeypatch):
    calls, _ = ml_put
    monkeypatch.setenv("ITEM_STATUS_WRITE_ENABLED", "0")
    res = _put(client)
    assert res.status_code == 503
    assert json.loads(res.data)["status_write"] == app_module.ITEM_STATUS_WRITE_NOT_ATTEMPTED
    assert calls == []


def test_el_rechazo_de_ml_pasa_tal_cual_sin_status_write(client, ml_put):
    _, response = ml_put
    response["value"] = _Resp(status_code=400, payload={"message": "item under review", "error": "validation_error"})
    res = _put(client, body={"status": "active"})
    assert res.status_code == 400
    assert json.loads(res.data) == {"message": "item under review", "error": "validation_error"}


def test_un_5xx_de_ml_pasa_tal_cual_y_no_reintenta(client, ml_put):
    calls, response = ml_put
    response["value"] = _Resp(status_code=503, payload={"message": "unavailable"})
    res = _put(client)
    assert res.status_code == 503
    assert "status_write" not in json.loads(res.data)
    assert len(calls) == 1


def test_timeout_del_put_es_504_y_ambiguo(client, ml_put):
    _, response = ml_put
    response["value"] = app_module.requests.Timeout()
    res = _put(client)
    assert res.status_code == 504
    assert json.loads(res.data)["status_write"] == app_module.ITEM_STATUS_WRITE_AMBIGUOUS


def test_una_excepcion_cualquiera_es_500_y_ambigua(client, ml_put):
    _, response = ml_put
    response["value"] = RuntimeError("boom")
    res = _put(client)
    assert res.status_code == 500
    assert json.loads(res.data)["status_write"] == app_module.ITEM_STATUS_WRITE_AMBIGUOUS
