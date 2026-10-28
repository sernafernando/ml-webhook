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
    monkeypatch.delenv("ITEM_DESCRIPTION_WRITE_ENABLED", raising=False)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def ml_put(monkeypatch):
    """Registra cada PUT a ML; sin este mock los tests saldrian a la red real."""
    calls = []
    response = {"value": _Resp(payload={"plain_text": "Descripcion nueva"})}

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
        f"/api/items/{item_id}/description",
        json={"plain_text": "Descripcion nueva"} if body is None else body,
    )


def test_escribe_la_descripcion_con_un_solo_put_a_ml(client, ml_put):
    calls, _ = ml_put
    res = _put(client)
    assert res.status_code == 200
    assert json.loads(res.data) == {"plain_text": "Descripcion nueva"}
    assert calls == [{
        "url": "https://api.mercadolibre.com/items/MLA123/description?api_version=2",
        "headers": {"Authorization": "Bearer TOKEN"},
        "json": {"plain_text": "Descripcion nueva"},
        "timeout": app_module.ITEM_DESCRIPTION_WRITE_TIMEOUT,
    }]


def test_reenvia_solo_plain_text_y_descarta_el_resto(client, ml_put):
    calls, _ = ml_put
    res = _put(client, body={"plain_text": "Texto", "text": "<p>html</p>", "title": "Router"})
    assert res.status_code == 200
    assert calls[0]["json"] == {"plain_text": "Texto"}


def test_conserva_los_saltos_de_linea_del_texto(client, ml_put):
    calls, _ = ml_put
    texto = "Linea uno\nLinea dos\n\n  Linea tres con sangria  "
    assert _put(client, body={"plain_text": texto}).status_code == 200
    assert calls[0]["json"] == {"plain_text": texto}


@pytest.mark.parametrize("body", [
    {},
    {"plain_text": ""},
    {"plain_text": "   "},
    {"plain_text": "\n\t "},
    {"plain_text": None},
    {"plain_text": 123},
    {"text": "<p>html</p>"},
    ["Descripcion"],
])
def test_sin_plain_text_util_es_422_y_no_escribe(client, ml_put, body):
    calls, _ = ml_put
    res = _put(client, body=body)
    assert res.status_code == 422
    assert json.loads(res.data)["description_write"] == app_module.ITEM_DESCRIPTION_WRITE_NOT_ATTEMPTED
    assert json.loads(res.data)["message"]
    assert calls == []


@pytest.mark.parametrize("item_id", ["123", "MLB123", "MLA12a", "mla123", "MLA"])
def test_un_item_que_no_es_mla_es_400_y_no_escribe(client, ml_put, item_id):
    calls, _ = ml_put
    res = _put(client, item_id=item_id)
    assert res.status_code == 400
    assert json.loads(res.data)["description_write"] == app_module.ITEM_DESCRIPTION_WRITE_NOT_ATTEMPTED
    assert calls == []


def test_con_el_interruptor_apagado_es_503_y_no_escribe(client, ml_put, monkeypatch):
    calls, _ = ml_put
    monkeypatch.setenv("ITEM_DESCRIPTION_WRITE_ENABLED", "0")
    res = _put(client)
    assert res.status_code == 503
    assert json.loads(res.data)["description_write"] == app_module.ITEM_DESCRIPTION_WRITE_NOT_ATTEMPTED
    assert calls == []


def test_sin_token_es_503_not_attempted_y_no_escribe(client, ml_put, monkeypatch):
    calls, _ = ml_put
    monkeypatch.setattr(app_module, "get_token", lambda: None)
    res = _put(client)
    assert res.status_code == 503
    assert json.loads(res.data)["description_write"] == app_module.ITEM_DESCRIPTION_WRITE_NOT_ATTEMPTED
    assert calls == []


def test_el_rechazo_de_ml_pasa_tal_cual_sin_description_write(client, ml_put):
    _, response = ml_put
    response["value"] = _Resp(
        status_code=400,
        payload={"message": "Invalid description", "error": "validation_error"},
    )
    res = _put(client)
    assert res.status_code == 400
    assert json.loads(res.data) == {"message": "Invalid description", "error": "validation_error"}


def test_un_5xx_de_ml_pasa_tal_cual_y_no_reintenta(client, ml_put):
    calls, response = ml_put
    response["value"] = _Resp(status_code=502, payload={"message": "bad gateway"})
    res = _put(client)
    assert res.status_code == 502
    assert "description_write" not in json.loads(res.data)
    assert len(calls) == 1


def test_timeout_del_put_es_504_y_ambiguo(client, ml_put):
    _, response = ml_put
    response["value"] = app_module.requests.Timeout()
    res = _put(client)
    assert res.status_code == 504
    assert json.loads(res.data)["description_write"] == app_module.ITEM_DESCRIPTION_WRITE_AMBIGUOUS


def test_un_error_de_conexion_es_502_y_ambiguo(client, ml_put):
    _, response = ml_put
    response["value"] = app_module.requests.ConnectionError("reset")
    res = _put(client)
    assert res.status_code == 502
    assert json.loads(res.data)["description_write"] == app_module.ITEM_DESCRIPTION_WRITE_AMBIGUOUS


def test_una_excepcion_cualquiera_es_502_y_ambigua(client, ml_put):
    _, response = ml_put
    response["value"] = RuntimeError("boom")
    res = _put(client)
    assert res.status_code == 502
    assert json.loads(res.data)["description_write"] == app_module.ITEM_DESCRIPTION_WRITE_AMBIGUOUS


@pytest.mark.parametrize("raw", ["null", "[]", "no soy json"])
def test_un_cuerpo_que_no_es_un_objeto_json_es_422_y_no_escribe(client, ml_put, raw):
    calls, _ = ml_put
    res = client.put("/api/items/MLA123/description", data=raw, content_type="application/json")
    assert res.status_code == 422
    assert json.loads(res.data)["description_write"] == app_module.ITEM_DESCRIPTION_WRITE_NOT_ATTEMPTED
    assert calls == []
