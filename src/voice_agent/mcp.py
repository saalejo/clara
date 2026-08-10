"""Conexión con servidores MCP para ampliar las herramientas del agente.

El Model Context Protocol es la forma estándar de que un programa externo le
ofrezca herramientas a un modelo. Aquí sirve para que el panel pueda añadirle
capacidades al agente —leer ficheros, consultar una API, encender algo— sin
tocar el código: se registra el servidor, se reinicia, y sus herramientas
aparecen junto a las locales en el mismo contexto.

## Un servidor roto no puede dejar muda la placa

Es la propiedad más importante de este módulo. Los servidores MCP se configuran
desde un navegador, escribiendo un comando o una URL a mano, así que
equivocarse es lo normal, no la excepción. Si un servidor que no responde
impidiera arrancar, un error de tecleo dejaría la placa sin agente y sin ninguna
pista de por qué.

Por eso cada conexión va envuelta en su propio `wait_for` y su propio
`try/except`: lo que falla se registra, se guarda para que el panel lo enseñe, y
el agente sigue arrancando con las herramientas que sí tenga.

## Dónde va esto en el montaje

El orden dentro de `construir_worker` no admite variación:

1. **Después de `build_llm`**, porque `register_tools` necesita el servicio LLM
   para colgarle los manejadores de cada herramienta remota.
2. **Antes de crear el `LLMContext`**, porque los esquemas que devuelve tienen
   que entrar en él junto a los de las herramientas locales.

## El cierre va en la misma tarea que la apertura

Los transportes de `mcp` se apoyan en grupos de tareas de anyio, y esos exigen
entrar y salir desde la **misma** tarea de asyncio. Cerrar los clientes desde un
manejador de evento del worker —que corre en una tarea interna suya— revienta
con un *"Attempted to exit cancel scope in a different task"*. Por eso `ejecutar`
los cierra en un `finally` alrededor del `runner.run()`, y no en un
`on_pipeline_finished`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from mcp import StdioServerParameters
from mcp.client.session_group import SseServerParameters, StreamableHttpParameters
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import LLMService
from pipecat.services.mcp_service import MCPClient

from voice_agent_core.estado import ServidorMCPEstado
from voice_agent_core.runtime import MCPServerConfig, RuntimeConfig, TransporteMCP

#: `${VARIABLE}` dentro de un valor de entorno o de una cabecera.
_REFERENCIA = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

ParametrosServidor = StdioServerParameters | SseServerParameters | StreamableHttpParameters


@dataclass
class ResultadoMCP:
    """Lo que quedó de intentar conectar con todos los servidores configurados."""

    clientes: list[MCPClient] = field(default_factory=list)
    esquemas: list[FunctionSchema] = field(default_factory=list)
    estados: list[ServidorMCPEstado] = field(default_factory=list)


def expandir(valor: str) -> str:
    """Sustituye `${VARIABLE}` por lo que valga en el entorno del agente.

    Es lo que permite darle una clave de API a un servidor MCP sin que el panel
    llegue a verla: en la base de datos queda escrito `${MI_CLAVE}` y quien la
    resuelve es este proceso, que sí tiene el `.env` montado.

    Una variable que no exista se deja tal cual, a propósito: sustituirla por la
    cadena vacía haría que el servidor fallara con un error de autenticación
    incomprensible en vez de con el literal a la vista.
    """

    def _sustituir(coincidencia: re.Match[str]) -> str:
        nombre = coincidencia.group(1)
        resuelto = os.environ.get(nombre)
        if resuelto is None:
            logger.warning(f"La variable '{nombre}' no está definida; se deja sin expandir.")
            return coincidencia.group(0)
        return resuelto

    return _REFERENCIA.sub(_sustituir, valor)


def construir_parametros(cfg: MCPServerConfig) -> ParametrosServidor:
    """Traduce la configuración del panel a los parámetros que espera `mcp`.

    Args:
        cfg: El servidor tal y como lo guardó el panel.

    Returns:
        Los parámetros de conexión del transporte correspondiente.

    Raises:
        ValueError: Si falta el comando o la url. El modelo ya lo valida al
            guardarse, así que llegar aquí significa un JSON editado a mano.
    """
    entorno = {clave: expandir(valor) for clave, valor in cfg.entorno.items()}
    cabeceras = {clave: expandir(valor) for clave, valor in cfg.cabeceras.items()}

    if cfg.transporte is TransporteMCP.STDIO:
        if not cfg.comando:
            raise ValueError(f"El servidor MCP '{cfg.nombre}' no tiene comando.")
        return StdioServerParameters(
            command=expandir(cfg.comando),
            args=[expandir(a) for a in cfg.argumentos],
            # `None` significa "hereda el entorno del agente", que aquí lleva las
            # claves del LLM y de Deepgram. Se pasa siempre un diccionario,
            # aunque esté vacío, para no regalárselas a un proceso ajeno.
            env=entorno,
        )

    if not cfg.url:
        raise ValueError(f"El servidor MCP '{cfg.nombre}' no tiene url.")
    url = expandir(cfg.url)

    if cfg.transporte is TransporteMCP.SSE:
        return SseServerParameters(url=url, headers=cabeceras, timeout=cfg.timeout_secs)

    return StreamableHttpParameters(url=url, headers=cabeceras)


async def _conectar(cliente: MCPClient, llm: LLMService[Any]) -> ToolsSchema:
    """Abre la sesión y registra las herramientas del servidor en el LLM.

    Vive en su propia función para poder lanzarla como tarea aislada; ver el
    comentario largo de `iniciar_clientes`.
    """
    await cliente.start()
    return await cliente.register_tools(llm)


async def iniciar_clientes(runtime: RuntimeConfig, llm: LLMService[Any]) -> ResultadoMCP:
    """Conecta con los servidores MCP habilitados y registra sus herramientas.

    Cada servidor se intenta por separado y con su propio tope de tiempo. Lo que
    falle se anota y se sigue: ver el docstring del módulo.

    Args:
        runtime: La configuración del panel.
        llm: El servicio LLM al que colgar los manejadores de las herramientas.

    Returns:
        Los clientes vivos —que hay que cerrar al terminar—, los esquemas para
        el contexto y el estado de cada servidor para que lo enseñe el panel.
    """
    resultado = ResultadoMCP()

    for cfg in runtime.servidores_mcp_activos:
        try:
            parametros = construir_parametros(cfg)
        except ValueError as e:
            logger.error(f"Servidor MCP '{cfg.nombre}' mal configurado: {e}")
            resultado.estados.append(
                ServidorMCPEstado(nombre=cfg.nombre, conectado=False, error=str(e))
            )
            continue

        cliente = MCPClient(
            server_params=parametros,
            # Lista vacía significa "todas"; `None` es lo que espera Pipecat.
            tools_filter=cfg.herramientas_permitidas or None,
        )
        # La conexión va en **su propia tarea**, y esto es lo menos evidente de
        # todo el módulo. Un servidor que falla sale por puertas muy distintas,
        # todas medidas en la placa y no deducidas:
        #
        #   * comando inexistente -> `FileNotFoundError`, limpio;
        #   * extremo HTTP caído  -> `RuntimeError: Attempted to exit cancel
        #     scope in a different task`, porque el transporte de `mcp` deshace
        #     su grupo de tareas de anyio desde otra tarea al fallar;
        #   * y, si se cruza con el plazo, un `CancelledError` lanzado por el
        #     ámbito de anyio. Ese es el peligroso: es **BaseException**, se
        #     cuela por debajo de `except Exception` y tumba el arranque del
        #     agente entero, que es justo lo que este módulo existe para
        #     impedir.
        #
        # No sirve distinguirlo por `plazo.expired()` ni por `task.cancelling()`:
        # anyio cancela la tarea actual de verdad, así que ambos indicadores
        # dicen lo mismo que ante un apagado real del agente. Lo que sí funciona
        # es aislar: si la conexión corre en otra tarea, la cancelación de anyio
        # se queda dentro de ella y aquí se lee su resultado con `cancelled()` y
        # `exception()`, sin que nada salga volando. Y si cancelan al agente de
        # verdad mientras esperamos, el `CancelledError` sale por su cuenta —que
        # es lo correcto.
        conexion = asyncio.create_task(_conectar(cliente, llm), name=f"mcp-{cfg.nombre}")
        terminadas, _ = await asyncio.wait({conexion}, timeout=cfg.timeout_secs)

        detalle: str | None = None
        esquema = None
        if not terminadas:
            conexion.cancel()
            with contextlib.suppress(BaseException):
                await conexion
            detalle = f"no respondió en {cfg.timeout_secs}s"
        elif conexion.cancelled():
            detalle = (
                "el transporte se canceló solo; lo normal es que el servidor no sea alcanzable"
            )
        elif (error := conexion.exception()) is not None:
            detalle = f"{type(error).__name__}: {error}"
        else:
            esquema = conexion.result()

        if detalle is not None or esquema is None:
            logger.error(
                f"El servidor MCP '{cfg.nombre}' no se pudo usar: {detalle}. "
                "El agente arranca sin sus herramientas."
            )
            await _cerrar_uno(cliente, cfg.nombre)
            resultado.estados.append(
                ServidorMCPEstado(
                    nombre=cfg.nombre, conectado=False, error=detalle or "desconocido"
                )
            )
            continue

        herramientas = [h for h in esquema.standard_tools if isinstance(h, FunctionSchema)]
        resultado.clientes.append(cliente)
        resultado.esquemas.extend(herramientas)
        resultado.estados.append(
            ServidorMCPEstado(
                nombre=cfg.nombre,
                conectado=True,
                herramientas=[h.name for h in herramientas],
            )
        )
        logger.info(
            f"Servidor MCP '{cfg.nombre}' conectado con "
            f"{len(herramientas)} herramienta(s): {', '.join(h.name for h in herramientas) or '—'}"
        )

    return resultado


async def _cerrar_uno(cliente: MCPClient, nombre: str) -> None:
    """Cierra un cliente sin dejar que su fallo tape al que lo provocó."""
    try:
        await cliente.close()
    except Exception as e:
        logger.debug(f"Al cerrar el servidor MCP '{nombre}': {e}")


async def cerrar_clientes(clientes: list[MCPClient]) -> None:
    """Cierra todos los clientes MCP al terminar el agente.

    Se llama desde el `finally` de `ejecutar`, en la misma tarea que los abrió;
    ver el docstring del módulo para el porqué.
    """
    for cliente in clientes:
        await _cerrar_uno(cliente, getattr(cliente, "name", "?"))
