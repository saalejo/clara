"""Agrega los JSONL de `data/metricas/` a la tabla que exige el README.

Uso: `make metricas` (o `python scripts/metricas.py [data/metricas]`).

Solo biblioteca estándar, a propósito: tiene que poder ejecutarse en cualquier
sitio sin el entorno del agente, incluso sobre una copia de los ficheros.

Los precios para el coste extrapolado se declaran aquí arriba, con fecha, para
que el cálculo sea auditable. En el nivel gratuito de AI Studio el coste real
es cero; por eso se extrapola a precios de producción y se explica la cuenta.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

#: Precios de gemini-2.5-flash por millón de tokens (nivel de pago de Google
#: AI Studio, consultados el 9 de agosto de 2026). Entrada / salida en USD.
PRECIO_ENTRADA_USD_MTOK = 0.30
PRECIO_SALIDA_USD_MTOK = 2.50


def _leer_eventos(carpeta: Path) -> list[dict[str, object]]:
    eventos: list[dict[str, object]] = []
    for fichero in sorted(carpeta.glob("*.jsonl")):
        for linea in fichero.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if linea:
                eventos.append(json.loads(linea))
    return eventos


def _percentil(valores: list[float], p: float) -> float:
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    indice = min(len(ordenados) - 1, max(0, round(p / 100 * (len(ordenados) - 1))))
    return ordenados[indice]


def main() -> int:
    """Imprime la tabla de métricas agregadas en Markdown."""
    carpeta = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/metricas")
    if not carpeta.is_dir():
        print(f"No existe {carpeta}; habla por el navegador primero para generar métricas.")
        return 1

    eventos = _leer_eventos(carpeta)
    voz = [float(e["segundos"]) for e in eventos if e["tipo"] == "voz_a_voz"]  # type: ignore[arg-type]
    usos = [e for e in eventos if e["tipo"] == "llm_uso"]
    llamadas = {e["id_llamada"] for e in eventos if e["tipo"] == "llamada_inicio"}

    entrada = sum(int(e["tokens_entrada"]) for e in usos)  # type: ignore[call-overload]
    salida = sum(int(e["tokens_salida"]) for e in usos)  # type: ignore[call-overload]
    turnos = len(voz)
    n_llamadas = len(llamadas) or 1

    # Consultas RAG por llamada, desde las trazas si están al lado.
    trazas = carpeta.parent / "evaluaciones" / "trazas"
    consultas_rag = 0
    if trazas.is_dir():
        consultas_rag = sum(
            len(f.read_text(encoding="utf-8").splitlines()) for f in trazas.glob("*.jsonl")
        )

    coste_llamada = (
        entrada / 1e6 * PRECIO_ENTRADA_USD_MTOK + salida / 1e6 * PRECIO_SALIDA_USD_MTOK
    ) / n_llamadas

    print(f"Eventos analizados: {len(eventos)} en {carpeta}\n")
    print("| Métrica | Valor |")
    print("|---|---|")
    print(f"| Latencia voz-a-voz P50 | {_percentil(voz, 50):.2f} s |")
    print(f"| Latencia voz-a-voz P95 | {_percentil(voz, 95):.2f} s |")
    print(f"| Turnos medidos | {turnos} |")
    print(f"| Llamadas | {len(llamadas)} |")
    if turnos:
        print(f"| Invocaciones del modelo por turno | {len(usos) / turnos:.1f} |")
        print(f"| Tokens de entrada por turno | {entrada / turnos:.0f} |")
        print(f"| Tokens de salida por turno | {salida / turnos:.0f} |")
    print(f"| Tokens de entrada por llamada | {entrada / n_llamadas:.0f} |")
    print(f"| Tokens de salida por llamada | {salida / n_llamadas:.0f} |")
    print(f"| Consultas RAG (total, de las trazas) | {consultas_rag} |")
    print("| Coste real por llamada (nivel gratuito) | $0.00 |")
    print(f"| Coste extrapolado por llamada (precios de pago) | ${coste_llamada:.4f} |")

    ttfb: dict[str, list[float]] = defaultdict(list)
    for e in eventos:
        if e["tipo"] == "ttfb":
            ttfb[str(e["procesador"])].append(float(e["segundos"]))  # type: ignore[arg-type]
    if ttfb:
        print("\nTTFB por servicio (mediana):\n")
        print("| Servicio | Mediana | Muestras |")
        print("|---|---|---|")
        for procesador, valores in sorted(ttfb.items()):
            print(f"| {procesador} | {statistics.median(valores):.2f} s | {len(valores)} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
