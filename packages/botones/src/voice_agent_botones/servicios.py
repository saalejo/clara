"""Gobierno de unidades de systemd, en asíncrono y sin lanzar.

Envoltorio fino sobre `voice_agent_core.systemd.ControlSystemd`, que es el mismo
código que usa el panel. Aquí se le añaden las dos cosas que el demonio necesita y
el panel no:

## 1. No bloquear el bucle de eventos

`jeepney.io.blocking` es síncrono, y `estado()` hace cuatro viajes de ida y vuelta
al bus. En una vista de Django eso da igual; en el bucle que está leyendo botones,
**cada consulta congelaría la lectura** y se perderían pulsaciones. Por eso todas
las llamadas van dentro de `asyncio.to_thread`.

## 2. No lanzar nunca

Los métodos devuelven `None` o `False` cuando algo va mal, y quien llama lo
traduce a un pitido de error. Un mando físico que muere porque systemd tardó en
contestar no sirve de nada.

## La asimetría que obliga a esperar

`StartUnit` **vuelve en centésimas de segundo**: devuelve la ruta del trabajo que
systemd acaba de encolar, no el resultado. Y el agente tarda unos 25 segundos en
estar LISTO —carga Whisper, Piper y el modelo de embeddings—, de los cuales los
primeros son solo el contenedor arrancando: ver `Demonio._testigo_de`.

Sin `esperar_estado`, el pitido de «hecho» mentiría: sonaría cuando la orden se
aceptó, no cuando el agente está escuchando. Y en una interfaz sin pantalla, un
feedback que miente es peor que no tener feedback.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from voice_agent_botones.config import Ajustes
from voice_agent_core.systemd import ControlSystemd, ErrorDeControl, EstadoUnidad

# Cada cuánto se pregunta si la unidad ya llegó a donde tenía que llegar. Un
# segundo es cómodo: son unas veinticinco preguntas para el arranque del agente,
# y cada una son cuatro viajes al bus en un hilo aparte.
INTERVALO_SONDEO = 1.0


def _mtime(ruta: Path | None) -> float:
    """Fecha de modificación de un fichero, o `-1` si no está o no se pide."""
    if ruta is None:
        return -1.0
    try:
        return ruta.stat().st_mtime
    except OSError:
        return -1.0


class Servicios:
    """Arranca, para y consulta las unidades que el mando puede gobernar."""

    def __init__(
        self,
        ajustes: Ajustes,
        *,
        control: ControlSystemd | None = None,
        reloj: Callable[[], float] = time.monotonic,
        intervalo: float = INTERVALO_SONDEO,
    ) -> None:
        """Prepara el gobierno con la lista blanca de los ajustes.

        Args:
            ajustes: Configuración; de ahí sale qué unidades son gobernables.
            control: Control de systemd. Inyectable para los tests.
            reloj: Reloj monótono, para la espera. Inyectable para los tests.
            intervalo: Cada cuánto se sondea el estado. Los tests lo ponen a cero
                para no dormir de verdad; con el reloj inyectado, el tiempo
                simulado avanza igual y la lógica de la espera es la misma.
        """
        self._ajustes = ajustes
        self._control = (
            control if control is not None else ControlSystemd(ajustes.unidades_gobernadas)
        )
        self._reloj = reloj
        self._intervalo = intervalo

    # -- Consulta -------------------------------------------------------------

    async def estado(self, unidad: str) -> EstadoUnidad | None:
        """Consulta el estado de una unidad. `None` si no se pudo."""
        return await self._intentar(self._control.estado, unidad)

    # -- Acciones -------------------------------------------------------------

    async def arrancar(self, unidad: str) -> bool:
        """Encola el arranque. `True` si systemd aceptó la orden."""
        return await self._intentar(self._control.arrancar, unidad) is not None

    async def parar(self, unidad: str) -> bool:
        """Encola la parada. `True` si systemd aceptó la orden."""
        return await self._intentar(self._control.parar, unidad) is not None

    async def reiniciar(self, unidad: str) -> bool:
        """Encola el reinicio. `True` si systemd aceptó la orden."""
        return await self._intentar(self._control.reiniciar, unidad) is not None

    # -- Espera ---------------------------------------------------------------

    async def esperar_estado(
        self, unidad: str, *, activa: bool, testigo: Path | None = None
    ) -> bool:
        """Espera a que la unidad llegue a activa o parada. `True` si llegó.

        Args:
            unidad: La unidad a vigilar.
            activa: `True` para esperar a que arranque, `False` a que pare.
            testigo: Fichero que la unidad reescribe cuando de verdad está lista.
                Si se da, no basta con que systemd la dé por activa: hay que ver
                el fichero más nuevo que el instante en que se pidió la orden. Ver
                `Demonio._testigo_de` para por qué hace falta.

        Se rinde al agotar `espera_unidad_segundos` y devuelve `False`, que quien
        llama traduce a un pitido de error. Rendirse es lo correcto: si el agente no
        ha arrancado en treinta segundos, algo va mal y hay que decirlo.
        """
        limite = self._reloj() + self._ajustes.espera_unidad_segundos
        desde = _mtime(testigo)
        while True:
            estado = await self.estado(unidad)
            if estado is not None:
                if activa:
                    if estado.activo and _mtime(testigo) > desde:
                        return True
                    # Arrancando, un `failed` no se va a arreglar esperando más.
                    if estado.fallido:
                        logger.warning(f"{unidad} ha quedado en fallido: {estado.resultado}")
                        return False
                # `parado` y `not activo` NO son lo mismo: en medio hay un
                # `deactivating` que no es ninguna de las dos cosas, y darlo por
                # parado ahí anunciaba el final cuarenta milisegundos después de
                # pedirlo.
                #
                # Y `fallido` también cuenta como parado, porque es el desenlace
                # NORMAL de parar el agente: su unidad es `Type=notify` con
                # `KillMode=mixed` y el agente no maneja SIGTERM, así que el
                # contenedor sale con código distinto de cero y systemd marca la
                # unidad `failed`. Medido en la placa: once segundos de parada
                # limpia que terminan en `failed: exit-code`. La unidad no está
                # corriendo, que es lo que se pidió; pitar error ahí sería mentir
                # en el otro sentido.
                elif estado.parado or estado.fallido:
                    return True
            if self._reloj() >= limite:
                logger.warning(
                    f"{unidad} no ha llegado a "
                    f"{'activa' if activa else 'parada'} en "
                    f"{self._ajustes.espera_unidad_segundos:.0f} s"
                )
                return False
            await asyncio.sleep(self._intervalo)

    # -- Interno --------------------------------------------------------------

    async def _intentar[T](self, funcion: Callable[[str], T], unidad: str) -> T | None:
        """Ejecuta una llamada bloqueante en un hilo, tragándose los errores."""
        try:
            return await asyncio.to_thread(funcion, unidad)
        except ErrorDeControl as e:
            logger.warning(f"No he podido gobernar {unidad}: {e}")
            return None


__all__ = ["INTERVALO_SONDEO", "Servicios"]
