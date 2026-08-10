"""Bus de eventos interno, difundido a los suscriptores del canal SSE.

Cada suscriptor tiene **su propia cola**. Es lo que evita el fallo clásico de
implementar esto con una sola cola compartida: con una sola, el primero que lee
un evento se lo lleva y los demás no lo ven, de modo que con dos clientes
conectados el anuncio de una llamada aparecería en uno u otro al azar.

Las colas están **acotadas**. Un suscriptor que no lee —un agente colgado, un
`curl` que alguien dejó abierto— no puede hacer crecer la memoria del puente sin
límite. Cuando su cola se llena se descarta el evento más viejo: para anuncios
de llamadas, lo reciente es lo que importa.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator

from loguru import logger

from voice_agent_core.telefonia import EventoTelefonia

#: Cuántos eventos se le guardan a un suscriptor lento antes de tirar los viejos.
#: Diez sobran: los eventos de telefonía llegan de uno en uno y a ritmo humano.
TAMANO_COLA = 10


class BusDeEventos:
    """Reparte eventos a todos los que estén escuchando."""

    def __init__(self) -> None:
        """Crea un bus sin suscriptores."""
        self._suscriptores: set[asyncio.Queue[EventoTelefonia]] = set()

    @property
    def suscriptores(self) -> int:
        """Cuántos están escuchando ahora mismo."""
        return len(self._suscriptores)

    def publicar(self, evento: EventoTelefonia) -> None:
        """Entrega un evento a todos los suscriptores.

        No es una corrutina a propósito: así se puede llamar desde un manejador
        de señal de D-Bus, que es código síncrono llamado por dbus-fast, sin
        tener que crear una tarea por evento.
        """
        logger.info(
            f"[telefonía] {evento.tipo.value}" + (f" — {evento.motivo}" if evento.motivo else "")
        )
        for cola in self._suscriptores:
            if cola.full():
                # Tirar el más viejo. Con un suscriptor atascado preferimos
                # perder historia antigua que quedarnos sin memoria.
                with contextlib.suppress(asyncio.QueueEmpty):
                    cola.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                cola.put_nowait(evento)

    @contextlib.contextmanager
    def suscripcion(self) -> Iterator[asyncio.Queue[EventoTelefonia]]:
        """Da de alta una cola de eventos y la retira al salir.

        Devuelve **la cola**, no un generador asíncrono, y esa es la parte que
        importa. La primera versión sí era un generador, del que había que tirar
        con `asyncio.wait_for(anext(gen), timeout=...)` para poder intercalar
        latidos. Ahí está la trampa: cuando `wait_for` agota el tiempo
        **cancela** lo que estaba esperando, y cancelar un `__anext__` finaliza
        el generador. El siguiente `anext` lanzaba `StopAsyncIteration`, así que
        el canal SSE se cortaba a los quince segundos exactos y el agente se
        pasaba la vida reconectando —"canal de eventos caído ()", una vez por
        segundo, sin mensaje de error porque la excepción no lleva ninguno—.

        Con una `asyncio.Queue` no ocurre: cancelar un `get()` es inocuo y la
        cola sigue viva y suscrita.

        La cola se da de baja al salir pase lo que pase; si no, un cliente que
        se desconecta la dejaría creciendo para siempre.
        """
        cola: asyncio.Queue[EventoTelefonia] = asyncio.Queue(maxsize=TAMANO_COLA)
        self._suscriptores.add(cola)
        try:
            yield cola
        finally:
            self._suscriptores.discard(cola)


__all__ = ["TAMANO_COLA", "BusDeEventos"]
