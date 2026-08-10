"""Arranque del demonio de botones.

    uv run --package voice-agent-botones python -m voice_agent_botones
    uv run --package voice-agent-botones python -m voice_agent_botones --sonda

El modo `--sonda` imprime los gestos detectados y **no ejecuta ninguna acción**.
Es la herramienta de diagnóstico: sirve para comprobar el cableado, para ajustar
los umbrales de duración y para descubrir qué emite un botón nuevo.

**La sonda no puede correr con el servicio en marcha**, porque el servicio tiene
el device en exclusiva por `EVIOCGRAB` y la sonda vería silencio. El síntoma
parece una avería del hardware, así que `make botones-sonda` para la unidad y la
restaura al terminar. Si prefieres convivir, arranca el servicio con
`BOTONES_ACAPARAR=0`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger
from pydantic import ValidationError

from voice_agent_botones.config import Ajustes
from voice_agent_botones.demonio import Demonio
from voice_agent_botones.entrada import LectorDeBotones, gestos_de
from voice_agent_botones.gestos import NOMBRES, DetectorDeGestos, Nivel
from voice_agent_botones.pitidos import CATALOGO, Pitidos


def _decir(mensaje: str) -> None:
    """Escribe en la salida estándar vaciando el búfer.

    El `flush` no es cosmético. La sonda se mata con Ctrl-C, y la salida estándar
    va con búfer de bloque en cuanto no es un terminal —al canalizarla por `tee`, o
    al lanzarla por SSH—, así que sin vaciar cada línea se pierde TODO lo impreso
    al morir el proceso. En una herramienta cuya única razón de ser es el feedback
    en vivo, eso la deja inservible, y el síntoma —"la sonda no detecta nada"— se
    confunde con el del `EVIOCGRAB`.
    """
    print(mensaje, flush=True)


def _configurar_registro(nivel: str) -> None:
    """Deja el log como el del resto del proyecto."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=nivel,
        format="{time:HH:mm:ss.SSS} {level: <8} {name}:{function} - {message}",
    )


def _detector(ajustes: Ajustes, *, avisar: bool) -> DetectorDeGestos:
    """Construye el detector con los umbrales configurados.

    Args:
        ajustes: Configuración del demonio.
        avisar: Si se anuncian por consola los cruces de frontera. En la sonda
            interesa verlos; en el demonio los anuncia el catálogo de pitidos.
    """

    def al_cruzar(tecla: int, nivel: Nivel) -> None:
        _decir(f"    ...{NOMBRES.get(tecla, tecla)} ha entrado en el nivel {int(nivel)}")

    def al_cancelar() -> None:
        _decir("    (dos teclas a la vez: gesto cancelado)")

    return DetectorDeGestos(
        ventana_ms=ajustes.ventana_agrupacion_ms,
        umbral_largo_ms=ajustes.umbral_largo_ms,
        umbral_muy_largo_ms=ajustes.umbral_muy_largo_ms,
        al_cruzar_nivel=al_cruzar if avisar else None,
        al_cancelar=al_cancelar if avisar else None,
    )


async def atender(ajustes: Ajustes) -> int:
    """Arranca el demonio y atiende botones hasta que lo paren."""
    await Demonio(ajustes).ejecutar()
    return 0


async def reproducir_catalogo(ajustes: Ajustes) -> int:
    """Genera el catálogo de pitidos y lo reproduce entero, para elegir de oído.

    Es una decisión estética y hay que tomarla escuchando: sobre el papel no se
    sabe si un tono se confunde con otro ni si se oye con la diadema puesta.
    """
    pitidos = Pitidos(ajustes.dir_pitidos)
    _decir(f"Catálogo de pitidos en {ajustes.dir_pitidos}\n")
    async with pitidos:
        for nombre, tonos in CATALOGO.items():
            duracion = sum(t.ms for t in tonos)
            _decir(f"  {nombre:10} {duracion:4} ms   {pitidos.ruta_de(nombre).name}")
            pitidos.sonar(nombre)
            # Se espera algo más que la duración para que no se solapen y se
            # puedan distinguir de uno en uno.
            await asyncio.sleep(duracion / 1000 + 0.6)
    return 0


async def sondear(ajustes: Ajustes) -> int:
    """Imprime los gestos que llegan, sin ejecutar nada."""
    _decir(f"Sonda de botones sobre {ajustes.dispositivo}")
    _decir(
        f"  niveles del rocker: 1 hasta {ajustes.umbral_largo_ms} ms, "
        f"2 hasta {ajustes.umbral_muy_largo_ms} ms, 3 por encima"
    )
    _decir("  MUTE no tiene niveles: el hardware manda pulsar y soltar pegados")
    _decir("  Ctrl+C para salir\n")

    detector = _detector(ajustes, avisar=True)
    lector = LectorDeBotones(
        ajustes.dispositivo,
        acaparar=ajustes.acaparar,
        espera_segundos=ajustes.espera_dispositivo_segundos,
        al_reconectar=detector.olvidar,
    )
    async with lector:
        async for gesto in gestos_de(lector, detector):
            _decir(f"  -> {gesto}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada del paquete."""
    analizador = argparse.ArgumentParser(
        prog="python -m voice_agent_botones",
        description="Mando físico del agente de voz sobre los botones de la tarjeta USB.",
    )
    analizador.add_argument(
        "--sonda",
        action="store_true",
        help="imprime los gestos detectados sin ejecutar ninguna acción",
    )
    analizador.add_argument(
        "--pitidos",
        action="store_true",
        help="genera y reproduce el catálogo de pitidos, para afinarlos de oído",
    )
    args = analizador.parse_args(argv)

    try:
        ajustes = Ajustes()
    except ValidationError as e:
        print(f"Configuración inválida:\n{e}", file=sys.stderr)
        return 1

    _configurar_registro(ajustes.log_level)

    if args.pitidos:
        tarea = reproducir_catalogo(ajustes)
    elif args.sonda:
        tarea = sondear(ajustes)
    else:
        tarea = atender(ajustes)

    try:
        return asyncio.run(tarea)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["atender", "main", "reproducir_catalogo", "sondear"]
