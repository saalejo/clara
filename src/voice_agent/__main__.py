"""Punto de entrada del agente: `python -m voice_agent`.

Se limita a leer la configuración, preparar el logging y arrancar el pipeline,
traduciendo los errores previsibles —falta la clave de la API, no se ha
indexado el corpus, el dispositivo de audio no existe— en mensajes que digan
qué hacer, en lugar de una traza de Python.
"""

from __future__ import annotations

import asyncio
import sys

from loguru import logger
from pydantic import ValidationError

from voice_agent.audio_devices import AudioDeviceError
from voice_agent.bot import ejecutar
from voice_agent.logging import setup_logging
from voice_agent_core.config import get_settings
from voice_agent_core.rutas import ruta_log_agente


def main() -> int:
    """Arranca el agente de voz.

    Returns:
        Código de salida del proceso: 0 si terminó de forma ordenada, 1 si
        hubo un error de configuración o de entorno.
    """
    try:
        settings = get_settings()
    except ValidationError as e:
        # Todavía no hay logging configurado: se escribe directamente.
        print("Error en la configuración (revisa el fichero .env):\n", file=sys.stderr)
        print(e, file=sys.stderr)
        return 1

    # El log va también a fichero para que el panel de control pueda seguirlo
    # desde su propio contenedor. Ver setup_logging().
    setup_logging(settings.log_level, archivo=ruta_log_agente(settings.data_dir))

    try:
        asyncio.run(ejecutar(settings))
    except KeyboardInterrupt:
        logger.info("Interrumpido por el usuario. Hasta luego.")
    except (ValueError, FileNotFoundError, AudioDeviceError) as e:
        # Errores de entorno esperables, con mensaje ya redactado en su origen.
        logger.error(str(e))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
