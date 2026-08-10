"""Cliente del puente de telefonía, para el mando físico.

Habla con `packages/telefonia` por su socket unix. Es una copia adelgazada de
`src/voice_agent/telefonia.py`: mismo transporte, mismos motivos, mucha menos
superficie. El mando solo necesita cuatro verbos y el canal de eventos; ni marcar,
ni buscar en la agenda, ni mandar tonos.

## El canal de eventos no puede tener tope de lectura

Es la trampa que ya costó una sesión en el cliente del agente, y se repite aquí
igual. Un `timeout` único para todo hace que el flujo SSE —que dura lo que dure el
demonio— muera a los cuatro segundos, y el síntoma es desconcertante: el canal se
cae en bucle y se registra como `canal caído ()`, sin mensaje, porque el `str()` de
un `httpx.ReadTimeout` está vacío.

De ahí `httpx.Timeout(self.timeout_s, read=None)`: tope para conectar, ninguno
para leer. Una conexión muerta se nota igual, por los `: ping` que el puente manda
cada quince segundos.

## Suscribirse no le roba nada al agente

El bus de eventos del puente reparte a **todos** los suscriptores, cada uno con su
propia cola (`voice_agent_telefonia.eventos.BusDeEventos`). El demonio puede
escuchar en paralelo con el agente sin que ninguno se pierda un evento.

## Nunca lanza

Los métodos devuelven `bool` o `None`, y quien llama lo traduce a un pitido. Que el
puente no esté es el estado **normal** la mitad del tiempo: el modo «solo tarjeta de
sonido» lo para a propósito.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from voice_agent_core.telefonia import EventoTelefonia

# El host da igual: el transporte va por socket unix y nadie resuelve este nombre.
BASE = "http://telefonia"

# La llamada «de ahora». El puente entiende este id literal y evita tener que
# saber cuál es la llamada en curso antes de contestarla o colgarla.
ACTUAL = "actual"


class ClienteTelefonia:
    """Lo que el mando físico necesita del puente."""

    def __init__(self, socket: Path, *, timeout_s: float = 4.0) -> None:
        """Prepara el cliente sin abrir nada.

        Args:
            socket: Ruta del socket unix del puente.
            timeout_s: Tope por petición. Corto: detrás hay un botón esperando.
        """
        self.socket = socket
        self.timeout_s = timeout_s

    def _cliente(self, timeout: httpx.Timeout | float) -> httpx.AsyncClient:
        """Abre un cliente contra el socket. El tope es obligatorio, sin defecto."""
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(self.socket)),
            base_url=BASE,
            timeout=timeout,
        )

    async def _pedir(self, metodo: str, ruta: str, **kwargs: Any) -> Any:
        """Hace una petición. Devuelve el JSON, o `None` si algo va mal.

        Devuelve `Any` y no algo más estrecho porque lo que llega es JSON
        arbitrario: quien llama es el que sabe qué forma espera y lo comprueba.
        """
        try:
            async with self._cliente(self.timeout_s) as cliente:
                respuesta = await cliente.request(metodo, ruta, **kwargs)
        except (httpx.ConnectError, FileNotFoundError):
            logger.info("El puente de telefonía no está en marcha")
            return None
        except httpx.HTTPError as e:
            logger.warning(f"Fallo hablando con el puente: {e}")
            return None

        if respuesta.status_code >= 400:
            logger.warning(f"El puente ha rechazado {metodo} {ruta}: {respuesta.status_code}")
            return None
        try:
            return respuesta.json()
        except ValueError:
            logger.warning("El puente ha devuelto algo que no es JSON")
            return None

    # --- Acciones ------------------------------------------------------------

    async def contestar(self) -> bool:
        """Descuelga la llamada en curso."""
        return await self._pedir("POST", f"/llamadas/{ACTUAL}/contestar") is not None

    async def colgar(self) -> bool:
        """Cuelga la llamada en curso.

        Sirve tanto para rechazar una entrante como para terminar una en curso: para
        oFono es la misma orden, y distinguirlas en el mando solo añadiría un modo
        que no cambia nada.
        """
        return await self._pedir("POST", f"/llamadas/{ACTUAL}/colgar") is not None

    async def alternar_autocontestar(self) -> bool | None:
        """Invierte el autocontestar. Devuelve el valor nuevo, o `None` si falló.

        Se usa `alternar` y no leer-luego-escribir para que el panel no pueda
        colarse entre las dos operaciones, y para gastar un solo viaje.
        """
        datos = await self._pedir("POST", "/autocontestar", json={"alternar": True})
        if not isinstance(datos, dict):
            return None
        activo = datos.get("activo")
        return activo if isinstance(activo, bool) else None

    # --- Eventos -------------------------------------------------------------

    async def eventos(
        self, *, al_conectar: Callable[[], None] | None = None
    ) -> AsyncIterator[EventoTelefonia]:
        """Sigue el canal SSE del puente.

        No reintenta: de eso se encarga quien llama, que es quien sabe con qué
        paciencia hacerlo y cómo registrarlo sin llenar el journal.

        Args:
            al_conectar: Se llama en cuanto el flujo queda abierto, **antes** del
                primer evento. Hace falta porque un canal sano puede estar callado
                horas: sin este aviso, quien escucha no puede distinguir «conectado
                y tranquilo» de «caído», ni reiniciar su retroceso a tiempo.
        """
        espera = httpx.Timeout(self.timeout_s, read=None)
        async with (
            self._cliente(espera) as cliente,
            cliente.stream("GET", "/eventos") as respuesta,
        ):
            respuesta.raise_for_status()
            if al_conectar is not None:
                al_conectar()
            async for linea in respuesta.aiter_lines():
                if not linea.startswith("data: "):
                    continue  # latidos y líneas en blanco
                try:
                    yield EventoTelefonia.model_validate(json.loads(linea[6:]))
                except (ValueError, TypeError) as e:
                    # Un `tipo` que este enum no conoce entra por aquí: el puente
                    # es nativo y se actualiza antes que la imagen del agente.
                    logger.warning(f"Evento de telefonía ilegible: {e}")


__all__ = ["ACTUAL", "BASE", "ClienteTelefonia"]
