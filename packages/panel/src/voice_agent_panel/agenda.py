"""Consulta de la agenda del móvil desde el panel.

El formulario de una tarea de llamada necesita buscar un contacto y **congelar
su número**: a la hora del disparo no habrá nadie delante para desambiguar un
"llama a Luis". El puente de telefonía ya sabe buscar (con la misma regla de
ganador claro que usan las herramientas del agente), así que el panel le
pregunta por su socket unix en el volumen de datos — la primera vez que el
panel lo usa, pero el socket siempre estuvo ahí, en `DATA_DIR/run/`.

Todo degrada a "sin resultados": el puente puede estar parado o la placa sin
móvil emparejado, y eso no puede tumbar el formulario con un 500.
"""

from __future__ import annotations

from typing import Any

import httpx
from django.conf import settings
from loguru import logger
from pydantic import ValidationError

from voice_agent_core.telefonia import Coincidencia

TIMEOUT_SECS = 4.0


def buscar_contactos(nombre: str, limite: int = 5) -> list[dict[str, Any]]:
    """Busca en la agenda del móvil a través del puente.

    Args:
        nombre: Lo tecleado en el formulario.
        limite: Máximo de candidatos.

    Returns:
        Una lista de `{"nombre", "numero", "puntuacion"}` ordenada por
        puntuación, ya reducida al número preferido de cada contacto (móvil si
        lo hay). Vacía si el puente no está o no hay agenda.
    """
    if not nombre.strip():
        return []
    socket = settings.DATA_DIR / "run" / "telefonia.sock"
    try:
        transporte = httpx.HTTPTransport(uds=str(socket))
        with httpx.Client(transport=transporte, base_url="http://telefonia") as cliente:
            respuesta = cliente.get(
                "/contactos",
                params={"nombre": nombre, "limite": limite},
                timeout=TIMEOUT_SECS,
            )
            respuesta.raise_for_status()
            datos = respuesta.json()
    except (httpx.HTTPError, OSError, ValueError) as e:
        logger.info(f"No se pudo consultar la agenda por {socket}: {e}")
        return []

    candidatos: list[dict[str, Any]] = []
    for cruda in datos.get("coincidencias", []):
        try:
            coincidencia = Coincidencia.model_validate(cruda)
        except ValidationError as e:
            logger.warning(f"El puente devolvió una coincidencia que no valida: {e}")
            continue
        numero = coincidencia.contacto.numero_preferido
        if not numero:
            continue
        candidatos.append(
            {
                "nombre": coincidencia.contacto.nombre,
                "numero": numero,
                "puntuacion": coincidencia.puntuacion,
            }
        )
    return candidatos
