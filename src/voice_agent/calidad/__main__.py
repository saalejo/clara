"""Punto de entrada del runner de calidad: `python -m voice_agent.calidad`.

Sin argumentos, lee la `SolicitudCalidad` que el panel dejó en disco y ejecuta
ese lote; si no hay solicitud, sale limpio (así el oneshot de systemd, que se
descarga al terminar bien, no cuenta como error). Con `--todos` o `--escenario`
se ejecuta a mano por SSH sin pasar por el panel.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

from loguru import logger

from voice_agent.calidad.ejecutor import ejecutar_lote
from voice_agent.logging import setup_logging
from voice_agent_core.calidad import CATALOGO, SolicitudCalidad
from voice_agent_core.config import get_settings
from voice_agent_core.rutas import ruta_log_agente, ruta_solicitud_calidad


def _ids_desde_argumentos(args: argparse.Namespace) -> tuple[list[str], str]:
    """Resuelve qué escenarios ejecutar y con qué id de lote, según los flags CLI."""
    momento = datetime.now()
    id_lote = f"cli-{momento:%Y%m%d-%H%M%S}"
    if args.todos:
        return [e.id for e in CATALOGO], id_lote
    return list(args.escenario), id_lote


def main() -> int:
    """Ejecuta un lote de escenarios de calidad."""
    parser = argparse.ArgumentParser(
        description="Ensaya escenarios de calidad adversarios contra Clara."
    )
    parser.add_argument("--todos", action="store_true", help="Ejecuta todo el catálogo.")
    parser.add_argument(
        "--escenario",
        action="append",
        default=[],
        metavar="ID",
        help="Id de un escenario (repetible).",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level, archivo=ruta_log_agente(settings.data_dir))

    if args.todos or args.escenario:
        ids, id_lote = _ids_desde_argumentos(args)
    else:
        ruta = ruta_solicitud_calidad(settings.data_dir)
        if not ruta.is_file():
            logger.info("No hay solicitud de calidad pendiente; nada que hacer.")
            return 0
        try:
            solicitud = SolicitudCalidad.model_validate_json(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.error(f"No se pudo leer la solicitud de calidad {ruta}: {e}")
            return 1
        ids, id_lote = solicitud.escenarios, solicitud.id_lote

    if not ids:
        logger.info("El lote de calidad no tiene escenarios; nada que hacer.")
        return 0

    logger.info(f"Lanzando lote de calidad '{id_lote}' con {len(ids)} escenario(s).")
    asyncio.run(ejecutar_lote(settings, ids, id_lote=id_lote))
    logger.info("Lote de calidad terminado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
