"""Herramienta de estado de la placa.

Sirve de ejemplo de una herramienta que consulta el mundo real —no una base de
datos ni una API, sino el propio hardware— y de paso es genuinamente útil:
permite preguntarle a la placa por su temperatura o su carga sin abrir una
sesión por SSH.

La lectura en sí vive en `voice_agent_core.board`, no aquí. El panel de control
enseña esas mismas cifras en su portada, y este módulo importa Pipecat: dejar
las funciones aquí obligaría al proceso web a cargar todo el framework de voz
para leer cuatro ficheros de texto de `/proc`. Aquí queda solo la envoltura que
convierte esa lectura en una herramienta que el modelo puede llamar.
"""

from __future__ import annotations

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from voice_agent_core.board import estado_placa


async def estado_del_sistema(params: FunctionCallParams) -> None:
    """Consulta el estado actual del hardware de la placa.

    Devuelve la temperatura del procesador y de la GPU, el uso de memoria, la
    carga del sistema y cuánto tiempo lleva encendida. Úsala cuando te
    pregunten cómo está la placa, si se está calentando, cuánta memoria queda
    libre o cuánto lleva funcionando.
    """
    estado = estado_placa()
    logger.info(f"[herramienta] estado_del_sistema() -> {estado}")

    await params.result_callback(estado)
