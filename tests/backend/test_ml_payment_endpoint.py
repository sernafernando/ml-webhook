"""Neto liquidado y retenciones, desde la API de pagos de Mercado Pago.

Por que existe esta ruta y no alcanzaba lo que ya teniamos: el neto no esta del
lado de ML. La orden solo trae el bruto, y facturacion solo trae lo que ML cobra
(percepciones si, retenciones no). El neto y las retenciones discriminadas estan
en el pago, en api.mercadopago.com.

Por que se PROYECTA la respuesta en vez de devolverla cruda, a diferencia de
/api/ml/orders y /api/ml/billing: el pago trae la tarjeta del comprador (bin,
primeros seis y ultimos cuatro digitos, titular), su IP y sus datos de contacto.
La conciliacion necesita seis numeros. Devolver el resto seria repartir datos de
tarjeta a cambio de nada.
"""
import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover - entorno sin DB/Redis
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


PAGO_CRUDO = {
    "id": 176106034911,
    "status": "approved",
    "currency_id": "ARS",
    "date_approved": "2026-08-20T10:00:00.000-04:00",
    "order": {"id": 2000018265495500, "type": "mercadolibre"},
    "transaction_amount": 730000,
    "shipping_amount": 0,
    "coupon_amount": 0,
    "taxes_amount": 0,
    "transaction_amount_refunded": 0,
    "transaction_details": {
        "net_received_amount": 519170,
        "total_paid_amount": 730000,
        "installment_amount": 81111.11,
        "payable_deferral_period": None,
    },
    "charges_details": [
        {"name": "tax_withholding_collector-debitos_creditos", "type": "tax",
         "amounts": {"original": 4380, "refunded": 0}},
        {"name": "tax_withholding_sirtac-la_pampa", "type": "tax",
         "amounts": {"original": 2190, "refunded": 0}},
        {"name": "meli_percentage_fee", "type": "fee",
         "amounts": {"original": 91250, "refunded": 0}},
        {"name": "financing_add_on_fee", "type": "fee",
         "amounts": {"original": 97820, "refunded": 0}},
        {"name": "shp_cross_docking", "type": "shipping",
         "amounts": {"original": 15190, "refunded": 0}},
    ],
    # Todo lo de abajo NO debe salir del proxy.
    "payer": {"email": "comprador@ejemplo.com", "first_name": "Ana",
              "last_name": "Perez", "identification": {"type": "DNI", "number": "12345678"}},
    "card": {"bin": "450995", "first_six_digits": "450995", "last_four_digits": "1234",
             "cardholder": {"name": "ANA PEREZ"}},
    "additional_info": {"ip_address": "10.36.23.215", "items": [{"title": "Impresora"}]},
    "metadata": {"interno": "no exponer"},
}


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = PAGO_CRUDO if payload is None else payload
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN-DEL-VENDEDOR")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def mp_calls(monkeypatch):
    calls = []

    def _fake_get(url, headers=None, params=None, **kwargs):
        calls.append({"url": url, "headers": headers or {}})
        return _Resp()

    monkeypatch.setattr(app_module, "ml_api_get", _fake_get)
    return calls


# =====================================================================
# El destino es Mercado Pago, y se valida igual de duro que ML
# =====================================================================

def test_pega_a_mercadopago_no_a_mercadolibre(client, mp_calls):
    res = client.get("/api/ml/payment", query_string={"payment_id": "176106034911"})

    assert res.status_code == 200
    assert mp_calls[0]["url"] == "https://api.mercadopago.com/v1/payments/176106034911"
    assert mp_calls[0]["headers"]["Authorization"] == "Bearer TOKEN-DEL-VENDEDOR"


@pytest.mark.parametrize("payment_id", [
    "", "abc", "176106034911/refunds", "../users/123", "1761060349 11",
    "@atacante.tld", "176106034911?x=1",
])
def test_solo_acepta_un_id_numerico(client, monkeypatch, payment_id):
    """El id se interpola en la URL: si aceptara cualquier cosa, seria la misma
    via de escape que build_ml_api_url existe para cerrar."""
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    res = client.get("/api/ml/payment", query_string={"payment_id": payment_id})

    assert res.status_code == 400
    assert res.is_json


def test_falta_payment_id(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    assert client.get("/api/ml/payment").status_code == 400


# =====================================================================
# La proyeccion: lo que sale y, sobre todo, lo que NO sale
# =====================================================================

def test_devuelve_el_neto_y_las_retenciones(client, mp_calls):
    d = client.get("/api/ml/payment",
                   query_string={"payment_id": "176106034911"}).get_json()

    assert d["net_received_amount"] == 519170
    assert d["transaction_amount"] == 730000
    assert d["order_id"] == 2000018265495500
    assert d["payment_id"] == 176106034911

    porNombre = {c["name"]: c for c in d["charges_details"]}
    assert porNombre["tax_withholding_sirtac-la_pampa"]["amount"] == 2190
    assert porNombre["tax_withholding_sirtac-la_pampa"]["type"] == "tax"


def test_el_neto_cierra_contra_los_cargos(client, mp_calls):
    """Si esta identidad se rompe, el consumidor no puede confiar en el desglose."""
    d = client.get("/api/ml/payment",
                   query_string={"payment_id": "176106034911"}).get_json()

    total_cargos = sum(c["amount"] for c in d["charges_details"])
    assert d["transaction_amount"] - total_cargos == d["net_received_amount"]


def test_no_filtra_datos_del_comprador(client, mp_calls):
    """El test que importa: la tarjeta, la IP y los datos personales no salen."""
    crudo = client.get("/api/ml/payment",
                       query_string={"payment_id": "176106034911"}).get_data(as_text=True)

    for prohibido in ["comprador@ejemplo.com", "Ana", "Perez", "12345678", "450995",
                      "1234", "ANA PEREZ", "10.36.23.215", "Impresora", "no exponer"]:
        assert prohibido not in crudo, f"se filtro {prohibido!r}"

    d = client.get("/api/ml/payment",
                   query_string={"payment_id": "176106034911"}).get_json()
    for clave in ("payer", "card", "additional_info", "metadata"):
        assert clave not in d


def test_un_pago_sin_desglose_no_rompe(client, monkeypatch):
    """MP puede devolver el pago sin transaction_details ni charges_details."""
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(payload={"id": 1, "status": "pending"}))

    res = client.get("/api/ml/payment", query_string={"payment_id": "1"})

    assert res.status_code == 200
    d = res.get_json()
    assert d["net_received_amount"] is None
    assert d["charges_details"] == []


def test_error_de_mp_se_traduce_a_json(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(status_code=404, payload={"message": "not found"}))

    res = client.get("/api/ml/payment", query_string={"payment_id": "1"})

    assert res.status_code == 404
    assert res.is_json
    assert res.get_json()["error"]


def test_no_acepta_post(client):
    assert client.post("/api/ml/payment",
                       query_string={"payment_id": "1"}).status_code == 405



# =====================================================================
# shipping_amount: sin el, la identidad del neto no cierra sola
# =====================================================================
# La base del neto no es transaction_amount sino lo efectivamente pagado, que
# incluye el envio que puso el comprador. Sin shipping_amount el consumidor
# tiene que ir a buscar paid_amount a la orden, y ahi se rompe: cuando una orden
# tiene mas de un pago, su paid_amount es el total de la ORDEN, no el de ese
# pago. Con shipping_amount la identidad queda entera adentro del pago.

PAGO_CON_ENVIO = {
    "id": 176726663717,
    "status": "approved",
    "order": {"id": 2000018322969636},
    "transaction_amount": 7371.11,
    "shipping_amount": 6990,
    "coupon_amount": 0,
    "transaction_details": {"net_received_amount": 14231.86, "total_paid_amount": 7371.11},
    "charges_details": [
        {"name": "tax_withholding_sirtac-buenos_aires", "type": "tax",
         "amounts": {"original": 43.08, "refunded": 0}},
        {"name": "tax_withholding_collector-debitos_creditos", "type": "tax",
         "amounts": {"original": 86.17, "refunded": 0}},
    ],
}


def test_proyecta_shipping_amount_y_coupon_amount(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(payload=PAGO_CON_ENVIO))

    d = client.get("/api/ml/payment",
                   query_string={"payment_id": "176726663717"}).get_json()

    assert d["shipping_amount"] == 6990
    assert d["coupon_amount"] == 0


def test_la_identidad_del_neto_cierra_dentro_del_pago(client, monkeypatch):
    """Verificado contra el pago real 176726663717, de una orden con dos pagos
    aprobados: es el caso donde usar el paid_amount de la orden no cerraba."""
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(payload=PAGO_CON_ENVIO))

    d = client.get("/api/ml/payment",
                   query_string={"payment_id": "176726663717"}).get_json()

    propios = sum(c["amount"] for c in d["charges_details"])
    calculado = d["transaction_amount"] + d["shipping_amount"] - propios

    assert abs(calculado - d["net_received_amount"]) < 0.01


def test_un_pago_sin_esos_campos_no_rompe(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(payload={"id": 1, "status": "pending"}))

    d = client.get("/api/ml/payment", query_string={"payment_id": "1"}).get_json()

    assert d["shipping_amount"] is None
    assert d["coupon_amount"] is None



# =====================================================================
# Devoluciones: el neto de un pago devuelto sigue siendo positivo
# =====================================================================
# Verificado contra el pago real 176756836953 (orden 2000018325540962): status
# refunded, transaction_amount_refunded 47000 (todo), y net_received_amount
# igual 27614. Sin el monto reembolsado a nivel del pago, una venta cancelada
# aparece como si hubiera dejado plata.

PAGO_DEVUELTO = {
    "id": 176756836953,
    "status": "refunded",
    "order": {"id": 2000018325540962},
    "transaction_amount": 47000,
    "shipping_amount": 0,
    "coupon_amount": 0,
    "taxes_amount": 0,
    "transaction_amount_refunded": 47000,
    "transaction_details": {"net_received_amount": 27614, "total_paid_amount": 47000},
    "charges_details": [
        {"name": "tax_withholding_collector-debitos_creditos", "type": "tax",
         "amounts": {"original": 282, "refunded": 282}},
        {"name": "tax_withholding_sirtac-catamarca", "type": "tax",
         "amounts": {"original": 846, "refunded": 846}},
        {"name": "meli_percentage_fee", "type": "fee",
         "amounts": {"original": 18258, "refunded": 18258}},
    ],
}


def test_proyecta_el_monto_reembolsado(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(payload=PAGO_DEVUELTO))

    d = client.get("/api/ml/payment",
                   query_string={"payment_id": "176756836953"}).get_json()

    assert d["transaction_amount_refunded"] == 47000
    assert d["taxes_amount"] == 0
    # El neto sigue positivo: es el dato que hace falta para no mostrar una
    # venta cancelada como si hubiera dejado plata.
    assert d["net_received_amount"] == 27614


def test_el_refunded_por_cargo_sigue_disponible_como_contraste(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(payload=PAGO_DEVUELTO))

    d = client.get("/api/ml/payment",
                   query_string={"payment_id": "176756836953"}).get_json()

    assert sum(c["refunded"] for c in d["charges_details"]) == 19386


def test_los_campos_nuevos_no_rompen_un_pago_incompleto(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(payload={"id": 1, "status": "pending"}))

    d = client.get("/api/ml/payment", query_string={"payment_id": "1"}).get_json()

    assert d["taxes_amount"] is None
    assert d["transaction_amount_refunded"] is None


def test_la_proyeccion_sigue_sin_filtrar_datos_del_comprador(client, mp_calls):
    """Se agregaron campos: este test vuelve a fijar que no entro nada mas."""
    d = client.get("/api/ml/payment",
                   query_string={"payment_id": "176106034911"}).get_json()

    assert set(d) == {
        "payment_id", "order_id", "status", "currency_id", "date_approved",
        "transaction_amount", "shipping_amount", "coupon_amount",
        "taxes_amount", "transaction_amount_refunded",
        "total_paid_amount", "net_received_amount", "charges_details",
    }
