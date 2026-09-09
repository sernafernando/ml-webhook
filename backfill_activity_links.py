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

Uso:
    python backfill_activity_links.py            # simulacro, no escribe
    python backfill_activity_links.py --aplicar  # escribe
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
    args = parser.parse_args()

    if not args.aplicar:
        print("🔍 SIMULACRO: no se escribe nada. Usá --aplicar para hacerlo.")

    resumen = backfill_vinculos_actividad(aplicar=args.aplicar, limite=args.limite)

    print(f"revisados:    {resumen['revisados']}")
    print(f"resueltos:    {resumen['resueltos']}")
    print(f"sin resolver: {resumen['sin_resolver']}")

    if resumen["sin_resolver"]:
        print("\nLos que siguen sin resolver no son un error: un reclamo puede colgar")
        print("de algo que no es una orden ni un envio, y ahi no hay venta que marcar.")


if __name__ == "__main__":
    main()
