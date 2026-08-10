"""Sondeo de un servidor MCP antes de guardarlo.

Sirve para saber qué herramientas ofrece un servidor sin tener que desplegar y
reiniciar el agente para averiguarlo.

**Solo funciona con http y sse.** Un servidor de tipo stdio se lanza como
proceso hijo, y su comando vive en la imagen del agente, no en la del panel:
intentarlo desde aquí daría un "no such file" que no significa nada. Para esos,
el panel enseña lo que el agente descubrió en su último arranque, con su fecha.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from voice_agent_core.runtime import TransporteMCP

if TYPE_CHECKING:
    from voice_agent_panel.models import ServidorMCP


class ErrorDeSondeo(Exception):
    """No se pudo preguntar al servidor."""


async def _preguntar(servidor: ServidorMCP) -> list[str]:
    """Abre una sesión, lista las herramientas y cierra."""
    cabeceras = dict(servidor.cabeceras or {})
    if servidor.transporte == TransporteMCP.SSE:
        contexto = sse_client(url=servidor.url, headers=cabeceras)
    else:
        contexto = streamablehttp_client(url=servidor.url, headers=cabeceras)

    async with contexto as flujos:
        lectura, escritura = flujos[0], flujos[1]
        async with ClientSession(lectura, escritura) as sesion:
            await sesion.initialize()
            listado = await sesion.list_tools()
            return [h.name for h in listado.tools]


def sondear(servidor: ServidorMCP) -> list[str]:
    """Devuelve los nombres de las herramientas que ofrece el servidor.

    Args:
        servidor: El servidor tal y como está guardado.

    Returns:
        Los nombres, en el orden en que los declara.

    Raises:
        ErrorDeSondeo: Si es de tipo stdio, si no responde o si falla.
    """
    if servidor.transporte == TransporteMCP.STDIO:
        raise ErrorDeSondeo(
            "Los servidores de tipo stdio no se pueden sondear desde el panel: su "
            "comando vive en la imagen del agente. Despliega y mira el resultado "
            "del arranque."
        )
    if not servidor.url:
        raise ErrorDeSondeo("Este servidor no tiene URL.")

    try:
        # La conexión vive en su propio bucle de eventos, aislada de todo lo
        # demás: los transportes de `mcp` usan grupos de tareas de anyio, y ya
        # sabemos por el agente lo mal que llevan que se los cruce de tarea.
        return asyncio.run(asyncio.wait_for(_preguntar(servidor), timeout=servidor.timeout_secs))
    except TimeoutError as e:
        raise ErrorDeSondeo(f"No respondió en {servidor.timeout_secs}s.") from e
    except BaseException as e:
        raise ErrorDeSondeo(f"{type(e).__name__}: {e}") from e
