"""Completa el vinculo evento -> venta de los eventos de actividad que quedaron sin el.

Por que hace falta: hasta el commit 21a4625, la resolucion de reclamos fallaba en
dos casos. Los eventos que llegaron como /claims/<id>/actions-history no daban el
id, y los reclamos cuyo resource es un shipment se descartaban. Esos ultimos son
cancel_purchase: ventas canceladas. Un consumidor que no las ve muestra como
activa una venta que ML ya cancelo, y eso no es un hueco de completitud sino un
dato incorrecto en pantalla.

Que hace y que NO hace: es un UPDATE en su lugar. Mismo id, mismo occurred_at,
mismo orden en el feed. No inserta, no borra y no reordena, asi que los cursores
que los consumidores ya guardaron siguen siendo validos. Reinsertar un evento por
delante del cursor de alguien se lo haria perder; reordenar le haria re-drenar.

Ojo con el volumen: el simulacro TAMBIEN consulta a ML, porque resolver el
vinculo es justamente lo que hay que probar. Con mil eventos pendientes son
varios minutos. Muestra progreso para que se note que avanza.

No todo pendiente es un error: los pagos de tipo bonificacion (money_transfer,
"bonificaciones_flex_fc") no tienen orden y nunca la van a tener. Por eso
conviene acotar con --topics a lo que si puede resolver.

Uso:
    python backfill_activity_links.py --topics post_purchase             # simulacro
    python backfill_activity_links.py --topics post_purchase --aplicar   # escribe
    python backfill_activity_links.py --aplicar --limite 50

Idempotente: correrlo dos veces no cambia nada, porque solo mira las filas que
siguen sin vinculo.
"""

import argparse

from dotenv import load_dotenv

load_dotenv()

from app import backfill_vinculos_actividad


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true",
                        help="escribe los vinculos; sin esto es un simulacro")
    parser.add_argument("--limite", type=int, default=None,
                        help="cuantos eventos revisar como maximo")
    parser.add_argument("--topics", default=None,
                        help="acota a estos topics, separados por coma")
    args = parser.parse_args()

    topics = [t.strip() for t in args.topics.split(",")] if args.topics else None

    if not args.aplicar:
        print("🔍 SIMULACRO: no se escribe nada. Usá --aplicar para hacerlo.")

    def mostrar(estado):
        if estado["revisados"] % 25 == 0 or estado["revisados"] == estado["pendientes"]:
            print(f"  {estado['revisados']}/{estado['pendientes']} "
                  f"(resueltos {estado['resueltos']}, sin resolver {estado['sin_resolver']})",
                  flush=True)

    resumen = backfill_vinculos_actividad(
        aplicar=args.aplicar, limite=args.limite, topics=topics, progreso=mostrar)

    print()
    print(f"pendientes:   {resumen['pendientes']}")
    print(f"revisados:    {resumen['revisados']}")
    print(f"resueltos:    {resumen['resueltos']}")
    print(f"sin resolver: {resumen['sin_resolver']}")

    if resumen["sin_resolver"]:
        print("\nLos que siguen sin resolver no son necesariamente un error: un pago de")
        print("bonificacion no tiene orden, y un reclamo puede colgar de algo que no es")
        print("una orden ni un envio. En esos casos no hay venta que marcar.")


if __name__ == "__main__":
    main()
