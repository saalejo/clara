"""Cliente del puente de telefonía, desde dentro del contenedor del agente.

Es deliberadamente tonto: habla HTTP por un socket unix y no sabe nada de
Bluetooth, de D-Bus ni de oFono. Toda esa complejidad vive en el puente
(`voice_agent_telefonia`), que corre nativo en la placa. Aquí no se importa
`dbus_fast` ni nada parecido, y `tests/test_telefonia_liviana.py` lo vigila:
meterlo aquí lo metería en la imagen del agente.

El puente puede no estar —parado, sin instalar, recién reiniciado— y eso **no
es un error**: es el estado normal de un agente que todavía no tiene teléfono.
Por eso el cliente distingue entre "no contesta" (que el llamador trata como
"no hay telefonía") y "contesta con un error" (que se le cuenta al usuario).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from voice_agent_core.telefonia import (
    Coincidencia,
    EstadoTelefonia,
    EventoTelefonia,
    Llamada,
)

#: Cualquier host vale: httpx necesita una URL con autoridad aunque el
#: transporte sea un socket unix. Este nombre solo aparece en los logs.
BASE = "http://telefonia"


class ErrorTelefonia(Exception):
    """El puente no está, no contesta, o ha contestado con un error."""

    def __init__(self, mensaje: str, *, sugerencia: str = "") -> None:
        """Guarda además una sugerencia que la herramienta le dará al modelo."""
        super().__init__(mensaje)
        self.sugerencia = sugerencia


class ClienteTelefonia:
    """Habla con el puente por su socket unix."""

    def __init__(self, socket: Path, timeout_s: float = 4.0) -> None:
        """Prepara el cliente sin conectar nada todavía.

        Args:
            socket: Ruta del socket. Dentro del contenedor es
                `/data/run/telefonia.sock`.
            timeout_s: Tope por petición. Corto a propósito: mientras una
                herramienta corre, el agente se queda callado.
        """
        self.socket = socket
        self.timeout_s = timeout_s

    def _cliente(self, timeout: httpx.Timeout | float) -> httpx.AsyncClient:
        """Abre un cliente contra el socket del puente.

        El tiempo de espera es **obligatorio** y no tiene valor por defecto a
        propósito. Cuando lo tenía y se admitía `None` para decir "el de
        siempre", la llamada del canal de eventos —que pasaba `timeout=None`
        queriendo decir "sin límite"— acababa con los cuatro segundos de las
        peticiones normales. El síntoma era desconcertante: el canal SSE se
        caía cada pocos segundos y el agente lo registraba como
        `canal de eventos caído ()`, sin mensaje, porque el `str()` de un
        `httpx.ReadTimeout` está vacío.
        """
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(self.socket)),
            base_url=BASE,
            timeout=timeout,
        )

    async def _pedir(self, metodo: str, ruta: str, **kwargs: Any) -> Any:
        """Hace una petición y devuelve el JSON, o lanza `ErrorTelefonia`."""
        try:
            async with self._cliente(self.timeout_s) as cliente:
                respuesta = await cliente.request(metodo, ruta, **kwargs)
        except (httpx.ConnectError, FileNotFoundError) as e:
            raise ErrorTelefonia(
                "el puente de telefonía no está en marcha",
                sugerencia="Dile a la persona que el teléfono no está disponible ahora mismo.",
            ) from e
        except httpx.TimeoutException as e:
            raise ErrorTelefonia(
                "el puente de telefonía no contesta",
                sugerencia="Dile que el teléfono tarda en responder y que lo intente de nuevo.",
            ) from e
        except httpx.HTTPError as e:
            raise ErrorTelefonia(f"fallo hablando con el puente: {e}") from e

        if respuesta.status_code >= 400:
            try:
                detalle = respuesta.json().get("error", respuesta.text)
            except ValueError:
                detalle = respuesta.text
            raise ErrorTelefonia(str(detalle))

        try:
            return respuesta.json()
        except ValueError as e:
            raise ErrorTelefonia("el puente ha devuelto algo que no es JSON") from e

    # --- Consultas -----------------------------------------------------------

    async def disponible(self) -> bool:
        """Sondea el puente. Es lo que decide si las herramientas se ofrecen.

        Nunca lanza: la ausencia de puente es una respuesta válida, no un fallo.
        """
        try:
            datos = await self._pedir("GET", "/salud")
        except ErrorTelefonia as e:
            logger.info(f"Telefonía no disponible: {e}")
            return False
        return bool(datos.get("ok"))

    async def estado(self) -> EstadoTelefonia:
        """Pregunta cómo está el teléfono."""
        return EstadoTelefonia.model_validate(await self._pedir("GET", "/estado"))

    async def buscar_contactos(self, nombre: str, limite: int = 5) -> dict[str, Any]:
        """Busca en la agenda.

        Returns:
            `{"coincidencias": [...], "ambiguo": bool, "pregunta": str}`. Quién
            gana y si hay duda lo decide **el puente**, para que esa regla viva
            en un solo sitio y se pueda probar.
        """
        datos = await self._pedir("GET", "/contactos", params={"nombre": nombre, "limite": limite})
        return {
            "coincidencias": [Coincidencia.model_validate(c) for c in datos["coincidencias"]],
            "ambiguo": bool(datos.get("ambiguo")),
            "pregunta": str(datos.get("pregunta", "")),
        }

    # --- Acciones ------------------------------------------------------------

    async def marcar(self, numero: str) -> Llamada:
        """Marca un número."""
        return Llamada.model_validate(
            await self._pedir("POST", "/llamadas/marcar", json={"numero": numero})
        )

    async def contestar(self, id_llamada: str | None = None) -> Llamada:
        """Contesta la llamada entrante."""
        destino = f"/llamadas/{id_llamada or 'actual'}/contestar"
        return Llamada.model_validate(await self._pedir("POST", destino))

    async def colgar(self, id_llamada: str | None = None) -> None:
        """Cuelga, o rechaza si está entrando."""
        if id_llamada:
            await self._pedir("POST", f"/llamadas/{id_llamada}/colgar")
        else:
            await self._pedir("POST", "/llamadas/colgar-todas")

    async def tonos(self, tonos: str) -> None:
        """Marca dígitos durante una llamada."""
        await self._pedir("POST", "/llamadas/actual/tonos", json={"tonos": tonos})

    # --- Eventos -------------------------------------------------------------

    async def eventos(self) -> AsyncIterator[EventoTelefonia]:
        """Sigue el canal de eventos del puente.

        No reintenta por su cuenta: de la reconexión se encarga quien llama
        (`telefonia_anuncios`), que es quien sabe con qué paciencia hacerlo y
        cómo registrarlo sin llenar el log.
        """
        # `read=None` es la clave: es un flujo que dura lo que dure el agente,
        # así que no puede tener tope de lectura. El de conexión sí lo lleva,
        # para que un puente que no está se detecte enseguida. Una conexión
        # muerta se nota igual por los `: ping` que manda el puente cada 15 s.
        espera = httpx.Timeout(self.timeout_s, read=None)
        async with (
            self._cliente(espera) as cliente,
            cliente.stream("GET", "/eventos") as respuesta,
        ):
            respuesta.raise_for_status()
            async for linea in respuesta.aiter_lines():
                if not linea.startswith("data: "):
                    continue  # comentarios de latido y líneas en blanco
                try:
                    yield EventoTelefonia.model_validate(json.loads(linea[6:]))
                except (ValueError, TypeError) as e:
                    logger.warning(f"Evento de telefonía ilegible: {e}")


__all__ = ["BASE", "ClienteTelefonia", "ErrorTelefonia"]
