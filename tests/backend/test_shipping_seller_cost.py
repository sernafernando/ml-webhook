import json

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


ITEM = {"id": "MLA1563835240", "seller_id": 111, "listing_type_id": "gold_special"}


def _router(monkeypatch, item=ITEM, unit=None, bulk=None, calls=None):
    """Rutea los tres GET reales: item, billable unitario, costo del bulto."""
    unit = unit if unit is not None else {"coverage": {"all_country": {"billable_weight": 3230}}}
    bulk = bulk if bulk is not None else {
        "coverage": {"all_country": {"list_cost": 33570, "currency_id": "ARS"}}
    }

    def fake_get(url, headers=None, params=None, timeout=None):
        if calls is not None:
            calls.append((url, dict(params or {})))
        if "/shipping_options/free" in url:
            if "item_id" in (params or {}):
                return _Resp(payload=unit) if not isinstance(unit, _Resp) else unit
            return _Resp(payload=bulk) if not isinstance(bulk, _Resp) else bulk
        return _Resp(payload=item) if not isinstance(item, _Resp) else item

    monkeypatch.setattr(app_module.requests, "get", fake_get)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN")
    monkeypatch.setattr(app_module, "_ml_pxq_throttle", lambda: None)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_devuelve_amount_del_bulto(client, monkeypatch):
    calls = []
    _router(monkeypatch, calls=calls)

    res = client.get("/api/shipping/seller-cost?item_id=MLA1563835240&quantity=10&tier_price=2850")

    assert res.status_code == 200
    body = json.loads(res.data)
    assert body["amount"] == 33570
    assert body["billable_weight"] == 32300


def test_la_llamada_del_bulto_usa_caja_chica_peso_lineal_y_descuento(client, monkeypatch):
    calls = []
    _router(monkeypatch, calls=calls)

    client.get("/api/shipping/seller-cost?item_id=MLA1563835240&quantity=5&tier_price=2900")

    _, bulk_params = calls[-1]
    # caja chica a proposito: manda el peso que pasamos, no un volumetrico inventado
    assert bulk_params["dimensions"] == "10x10x10,16150"
    # sin item_price ML devuelve list_cost 0; sin listing_type_id no aplica descuento
    assert bulk_params["item_price"] == 2900.0
    assert bulk_params["listing_type_id"] == "gold_special"
    assert bulk_params["mode"] == "me2"


def test_no_multiplica_el_costo_unitario_por_n(client, monkeypatch):
    """El peso escala lineal, el costo NO: el costo sale de la segunda llamada."""
    _router(monkeypatch, unit={"coverage": {"all_country": {
        "billable_weight": 3230, "list_cost": 9860}}})

    res = client.get("/api/shipping/seller-cost?item_id=MLA1563835240&quantity=10&tier_price=2850")

    assert json.loads(res.data)["amount"] == 33570  # no 98600


@pytest.mark.parametrize("qs", [
    "item_id=NOPE&quantity=2&tier_price=100",
    "item_id=MLA1&quantity=0&tier_price=100",
    "item_id=MLA1&quantity=x&tier_price=100",
    "item_id=MLA1&quantity=2&tier_price=0",
    "item_id=MLA1&quantity=2",
])
def test_parametros_invalidos_dan_400(client, monkeypatch, qs):
    _router(monkeypatch)
    res = client.get(f"/api/shipping/seller-cost?{qs}")
    assert res.status_code == 400
    assert "amount" not in json.loads(res.data)


def test_ml_no_200_falla_con_status_y_sin_amount(client, monkeypatch):
    _router(monkeypatch, bulk=_Resp(status_code=500))
    res = client.get("/api/shipping/seller-cost?item_id=MLA1&quantity=2&tier_price=100")
    assert res.status_code == 502
    assert "amount" not in json.loads(res.data)


def test_list_cost_ausente_no_se_colapsa_a_cero(client, monkeypatch):
    """0 es un costo valido: fabricarlo inventaria un markup."""
    _router(monkeypatch, bulk={"coverage": {"all_country": {"currency_id": "ARS"}}})
    res = client.get("/api/shipping/seller-cost?item_id=MLA1&quantity=2&tier_price=100")
    assert res.status_code == 502
    assert "amount" not in json.loads(res.data)


def test_billable_weight_ausente_falla(client, monkeypatch):
    _router(monkeypatch, unit={"coverage": {"all_country": {}}})
    res = client.get("/api/shipping/seller-cost?item_id=MLA1&quantity=2&tier_price=100")
    assert res.status_code == 502


def test_timeout_de_ml_da_504(client, monkeypatch):
    def fake_get(*a, **k):
        raise app_module.requests.Timeout()
    monkeypatch.setattr(app_module.requests, "get", fake_get)
    res = client.get("/api/shipping/seller-cost?item_id=MLA1&quantity=2&tier_price=100")
    assert res.status_code == 504
    assert "amount" not in json.loads(res.data)


def test_item_sin_listing_type_id_falla(client, monkeypatch):
    _router(monkeypatch, item={"id": "MLA1", "seller_id": 111})
    res = client.get("/api/shipping/seller-cost?item_id=MLA1&quantity=2&tier_price=100")
    assert res.status_code == 502
