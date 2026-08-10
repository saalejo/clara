r"""Micrófono y volumen, en el mezclador de ALSA.

Aquí está la decisión que hace pequeño todo el paquete: silenciar el micrófono y
mover el volumen se hacen con `amixer`, **no dentro del agente**. Así el agente no
necesita ni una línea de cambio, no hay que reconstruir su imagen de contenedor,
y —lo más importante— los botones funcionan con el agente parado, que es justo
cuando más falta hacen.

Ojo con no confundir esto con la compuerta de `src/voice_agent/audio_gate.py`, que
**sí existe** en esta placa porque el perfil es `speaker`. Son cosas distintas: la
compuerta silencia el micro mientras habla el propio agente, dentro del pipeline y
por milisegundos; esto es un interruptor de hardware que se queda puesto.

Está medido en la placa, con el agente en marcha y la captura abierta por
`dsnoop`: `amixer -c Device sset Mic nocap` lleva la grabación de `rms=92.2 /
pico=403` a **`rms=0 / pico=0`**, silencio digital absoluto.

Hay precedente en casa: `src/voice_agent/audio_devices.py` ya invoca `amixer` por
subprocess en su código de diagnóstico.

## La trampa del parseo, que costó dejar el volumen a cero

`amixer sget` imprime varias líneas y **más de una casa con el patrón obvio**:

    Simple mixer control 'Speaker',0
      Capabilities: pvolume pswitch pswitch-joined
      Playback channels: Front Left - Front Right
      Limits: Playback 0 - 151          <-- ESTA casa con `Playback (\\d+)`
      Mono:
      Front Left: Playback 113 [64%] [-7.19dB] [on]

Un `Playback (\\d+)` a secas captura el `0` de los límites, no el `113` real. Al
descubrirlo, un script de medición leyó volumen 0, «restauró» a 0 y dejó la
tarjeta muda. Por eso aquí siempre se ancla en la línea de un canal (`Mono:`,
`Front Left:`).

La segunda mitad de la trampa está en el micrófono, que trae los dos sentidos en
la MISMA línea, con dos interruptores distintos:

      Mono: Playback 14 [11%] [2.62dB] [off] Capture 14 [88%] [20.83dB] [on]

Un `\\[(on|off)\\]` a secas devuelve el `off` de la reproducción cuando lo que se
pregunta es por la captura. Hay que cortar la línea por `Capture` primero.

## Por qué `-M` en el volumen

`-M` pide volumen **mapeado**, una escala perceptual. El rango crudo de esta
tarjeta es 0-151 en pasos de decibelio, y sin `-M` los primeros pasos casi no se
oyen mientras los últimos pegan un salto. Medido con `-M` y pasos del 6 %: 16
pulsaciones de fondo a tope, 11 para bajar. Con la escala cruda el recorrido
útil se concentra en un extremo.

El `unmute` que acompaña a cada cambio evita el clásico «subo el volumen y no
pasa nada» cuando el interruptor de salida estaba apagado.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from loguru import logger

from voice_agent_botones.config import Ajustes, ModoMicrofono

# Una línea de canal: la única en la que los valores son de verdad. `Mono:` la
# usa el micrófono (y aparece vacía en el altavoz estéreo, de ahí el `.+`).
LINEA_DE_CANAL = re.compile(r"^\s*(?:Mono|Front Left|Front Right|Playback):\s*\S.*$", re.MULTILINE)

PORCENTAJE = re.compile(r"\[(\d+)%\]")
INTERRUPTOR = re.compile(r"\[(on|off)\]")

# Devuelve (código de salida, salida estándar).
Ejecutor = Callable[[Sequence[str]], Awaitable[tuple[int, str]]]


@dataclass(frozen=True)
class EstadoMezclador:
    """Lo que el mezclador dice ahora mismo.

    Attributes:
        micro_abierto: Si la captura está activa.
        volumen_pct: Volumen de salida en escala mapeada, 0-100.
        salida_activa: Si el interruptor de salida está encendido.
    """

    micro_abierto: bool
    volumen_pct: int
    salida_activa: bool


def _linea_de_canal(salida: str) -> str | None:
    """Devuelve la primera línea de canal con valores, o `None`.

    Es lo que evita capturar el `0` de `Limits: Playback 0 - 151`.
    """
    coincidencia = LINEA_DE_CANAL.search(salida)
    return coincidencia.group(0) if coincidencia else None


def _tramo_de_captura(linea: str) -> str | None:
    """Recorta la parte de captura de una línea que trae los dos sentidos.

    El micrófono imprime reproducción y captura seguidas, cada una con su propio
    interruptor. Sin este recorte se leería el de la reproducción.
    """
    if "Capture" not in linea:
        return None
    return linea.rsplit("Capture", 1)[1]


def _tramo_de_reproduccion(linea: str) -> str:
    """Recorta la parte de reproducción de una línea de canal."""
    if "Playback" in linea:
        linea = linea.split("Playback", 1)[1]
    return linea.split("Capture", 1)[0]


def interruptor(tramo: str) -> bool | None:
    """Lee un `[on]`/`[off]`, o `None` si el tramo no lo trae."""
    coincidencia = INTERRUPTOR.search(tramo)
    if coincidencia is None:
        return None
    return coincidencia.group(1) == "on"


def porcentaje(tramo: str) -> int | None:
    """Lee un `[NN%]`, o `None` si el tramo no lo trae."""
    coincidencia = PORCENTAJE.search(tramo)
    if coincidencia is None:
        return None
    return int(coincidencia.group(1))


async def _ejecutar(orden: Sequence[str]) -> tuple[int, str]:
    """Lanza una orden y devuelve su código y su salida estándar."""
    proceso = await asyncio.create_subprocess_exec(
        *orden,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    salida, error = await proceso.communicate()
    if proceso.returncode != 0:
        logger.warning(f"{' '.join(orden)} ha fallado: {error.decode(errors='replace').strip()}")
    return proceso.returncode or 0, salida.decode(errors="replace")


class Mezclador:
    """Gobierna el micrófono y el volumen de la tarjeta con `amixer`.

    Ningún método lanza por culpa de `amixer`: devuelven `None` cuando algo va
    mal, y quien llama lo traduce a un pitido de error. Un fallo del mezclador no
    puede tumbar el demonio.
    """

    def __init__(self, ajustes: Ajustes, *, ejecutor: Ejecutor = _ejecutar) -> None:
        """Prepara el mezclador.

        Args:
            ajustes: Configuración, de donde salen la tarjeta y los controles.
            ejecutor: Cómo se lanzan las órdenes. Inyectable para los tests.
        """
        self._ajustes = ajustes
        self._ejecutor = ejecutor

    # -- Órdenes --------------------------------------------------------------

    def _orden(self, *argumentos: str, mapeado: bool = False) -> list[str]:
        """Construye una invocación de `amixer` para la tarjeta configurada."""
        orden = ["amixer"]
        if mapeado:
            orden.append("-M")
        orden += ["-c", self._ajustes.tarjeta_alsa, *argumentos]
        return orden

    async def _lanzar(self, orden: Sequence[str]) -> str | None:
        """Lanza una orden con tope de tiempo. Devuelve la salida, o `None`."""
        try:
            codigo, salida = await asyncio.wait_for(
                self._ejecutor(orden), self._ajustes.timeout_amixer_segundos
            )
        except TimeoutError:
            logger.warning(f"{' '.join(orden)} no ha respondido a tiempo")
            return None
        except OSError as e:
            # `amixer` no instalado, o la tarjeta desaparecida del hub.
            logger.warning(f"No he podido ejecutar {' '.join(orden)}: {e}")
            return None
        return salida if codigo == 0 else None

    # -- Lectura --------------------------------------------------------------

    async def leer(self) -> EstadoMezclador | None:
        """Lee el estado completo del mezclador."""
        micro = await self.micro_abierto()
        salida = await self._leer_salida()
        if micro is None or salida is None:
            return None
        volumen, activa = salida
        return EstadoMezclador(micro_abierto=micro, volumen_pct=volumen, salida_activa=activa)

    async def micro_abierto(self) -> bool | None:
        """Indica si la captura está activa, según el modo configurado."""
        salida = await self._lanzar(self._orden("sget", self._ajustes.control_micro))
        if salida is None:
            return None
        return self._interpretar_micro(salida)

    def _interpretar_micro(self, salida: str) -> bool | None:
        """Saca el estado del micrófono de la salida de `amixer sget`."""
        linea = _linea_de_canal(salida)
        if linea is None:
            logger.warning("No encuentro la línea de canal del micrófono en la salida de amixer")
            return None
        tramo = _tramo_de_captura(linea)
        if tramo is None:
            logger.warning("El control del micrófono no informa de captura")
            return None
        if self._ajustes.modo_micro is ModoMicrofono.SWITCH:
            return interruptor(tramo)
        # En modo ganancia, «abierto» es tener ganancia por encima de cero.
        nivel = porcentaje(tramo)
        return None if nivel is None else nivel > 0

    async def _leer_salida(self) -> tuple[int, bool] | None:
        """Lee el volumen mapeado y el interruptor de salida."""
        salida = await self._lanzar(
            self._orden("sget", self._ajustes.control_altavoz, mapeado=True)
        )
        if salida is None:
            return None
        return self._interpretar_salida(salida)

    def _interpretar_salida(self, salida: str) -> tuple[int, bool] | None:
        """Saca volumen e interruptor de la salida de `amixer sget`."""
        linea = _linea_de_canal(salida)
        if linea is None:
            logger.warning("No encuentro la línea de canal del altavoz en la salida de amixer")
            return None
        tramo = _tramo_de_reproduccion(linea)
        volumen = porcentaje(tramo)
        activa = interruptor(tramo)
        if volumen is None:
            logger.warning(f"No encuentro el volumen en {linea!r}")
            return None
        # Un control sin interruptor se da por activo: no hay nada que apagar.
        return volumen, True if activa is None else activa

    # -- Micrófono ------------------------------------------------------------

    async def fijar_micro(self, abierto: bool) -> bool | None:
        """Abre o silencia el micrófono. Devuelve el estado resultante.

        Se usa también al reabrir el device tras una reenumeración del USB: la
        tarjeta vuelve con los valores por defecto del driver, así que un silencio
        puesto por el usuario se desharía solo sin esto.
        """
        control = self._ajustes.control_micro
        argumentos: tuple[str, ...]
        if self._ajustes.modo_micro is ModoMicrofono.SWITCH:
            argumentos = ("sset", control, "cap" if abierto else "nocap")
        elif abierto:
            argumentos = ("sset", control, str(self._ajustes.ganancia_micro_abierto), "cap")
        else:
            argumentos = ("sset", control, "0")

        salida = await self._lanzar(self._orden(*argumentos))
        if salida is None:
            return None
        # `sset` imprime el estado resultante, así que no hace falta releer.
        return self._interpretar_micro(salida)

    async def alternar_micro(self) -> bool | None:
        """Cambia el micrófono de estado. Devuelve el estado resultante."""
        actual = await self.micro_abierto()
        if actual is None:
            return None
        return await self.fijar_micro(not actual)

    # -- Volumen --------------------------------------------------------------

    async def subir(self) -> tuple[int, bool] | None:
        """Sube el volumen un paso. Devuelve (porcentaje, si ya estaba al tope)."""
        return await self._mover(f"{self._ajustes.paso_volumen}%+")

    async def bajar(self) -> tuple[int, bool] | None:
        """Baja el volumen un paso. Devuelve (porcentaje, si ya estaba al fondo)."""
        return await self._mover(f"{self._ajustes.paso_volumen}%-")

    async def _mover(self, delta: str) -> tuple[int, bool] | None:
        """Aplica un cambio relativo de volumen y detecta si no se movió.

        Detectar el límite importa: sin el pitido de tope, una pulsación que no
        cambia nada parece un botón que no funciona.
        """
        antes = await self._leer_salida()
        if antes is None:
            return None

        salida = await self._lanzar(
            self._orden("sset", self._ajustes.control_altavoz, delta, "unmute", mapeado=True)
        )
        if salida is None:
            return None
        despues = self._interpretar_salida(salida)
        if despues is None:
            return None

        # Si el interruptor estaba apagado, el `unmute` ya es un cambio audible
        # aunque el porcentaje no se mueva: eso no es un tope.
        sin_cambio = despues[0] == antes[0] and antes[1]
        return despues[0], sin_cambio


__all__ = [
    "Ejecutor",
    "EstadoMezclador",
    "Mezclador",
    "interruptor",
    "porcentaje",
]
