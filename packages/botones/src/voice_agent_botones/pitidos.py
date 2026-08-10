"""Feedback sonoro: el único que hay, porque no hay pantalla.

Los WAV se generan con `wave` y `math` de la biblioteca estándar la primera vez
que hacen falta, en `<DATA_DIR>/pitidos/`, y se reproducen con `aplay`. No hay
binarios en el repositorio y no hay dependencia de audio.

## Por qué pitidos y no la voz del agente

Porque el momento en que más falta hace el feedback es cuando el agente está
parado —justo al pulsar el botón que lo arranca—, y entonces no hay quien hable.
Un pitido funciona siempre.

## Los tres detalles que no son cosméticos

**Rampa de 5 ms a la entrada y a la salida** de cada tono. Sin ella la onda se
corta a mitad de ciclo y se oye un chasquido más fuerte que el propio pitido.

**Amplitud máxima 0,25** del fondo de escala. La tarjeta se comparte con el
agente a través del `dmix` de `deploy/asound.conf`, que suma los flujos: al 100 %
un pitido durante una frase del agente saturaría.

**48 kHz estéreo**, el formato nativo de la tarjeta (`docs/audio.md`: la
reproducción exige estéreo y solo admite 44,1/48 kHz), para que la capa `plug` no
tenga que convertir nada.

## Y una propiedad afortunada

`docs/audio.md` documenta que **Silero está entrenado para ignorar tonos puros**
—el hallazgo que en su día invalidó una medición de eco con un tono de 440 Hz—.
Aquí juega a favor: los pitidos no disparan el VAD del agente, así que se pueden
emitir mientras conversa sin provocar una interrupción falsa.

## La cola de un solo hueco

`sonar()` no es una corrutina: encola y vuelve, para que pulsar un botón nunca
espere a que suene nada. Una tarea aparte reproduce de uno en uno, porque dos
`aplay` simultáneos sobre `dmix` se mezclarían y sonarían a estruendo. La cola
está acotada: si se llena —un `aplay` colgado, o alguien aporreando botones— se
descartan los pitidos nuevos y se registra, en lugar de acumular trabajo que ya
no significa nada.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import struct
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

FRECUENCIA_MUESTREO = 48_000
CANALES = 2
BYTES_POR_MUESTRA = 2
AMPLITUD = 0.25
RAMPA_MS = 5

# Tope de la cola. Cuatro pitidos pendientes ya son medio segundo de retraso: más
# allá de eso el feedback dejaría de significar nada.
TAMANO_COLA = 4

# Tope de un `aplay`. Un pitido dura menos de un segundo; si tarda más de esto es
# que la tarjeta se ha ido, y hay que soltar la cola en vez de bloquearla.
TIMEOUT_REPRODUCCION = 5.0


@dataclass(frozen=True)
class Tono:
    """Un tramo de sonido, con barrido lineal de frecuencia si se quiere.

    Attributes:
        hz_inicio: Frecuencia al empezar. Cero significa silencio.
        hz_fin: Frecuencia al terminar. Igual a `hz_inicio` para un tono plano.
        ms: Duración en milisegundos.
    """

    hz_inicio: float
    hz_fin: float
    ms: int

    @property
    def es_silencio(self) -> bool:
        """Indica si el tramo es un silencio."""
        return self.hz_inicio <= 0 and self.hz_fin <= 0


def _silencio(ms: int) -> Tono:
    """Construye un tramo de silencio."""
    return Tono(0, 0, ms)


# El catálogo. Los nombres describen el SIGNIFICADO, no el sonido, para que se
# puedan afinar de oído con `make botones-pitidos` sin tocar quien los usa.
#
# La lógica de los timbres: lo afirmativo es agudo y corto, lo negativo grave y
# algo más largo, lo que pregunta sube, lo que se cancela baja, y el error es
# inconfundible por repetición.
CATALOGO: dict[str, tuple[Tono, ...]] = {
    # Confirmaciones de una acción instantánea.
    "si": (Tono(1200, 1200, 90),),
    "no": (Tono(500, 500, 130),),
    # Avisos al cruzar una frontera de nivel, con el botón aún pulsado. Muy
    # cortos: son un "vas por aquí", no un resultado.
    "nivel2": (Tono(900, 900, 55),),
    "nivel3": (Tono(1400, 1400, 55),),
    # Un verbo destructivo pide confirmación.
    "pregunta": (Tono(700, 1100, 200),),
    "cancelado": (Tono(900, 500, 180),),
    # Algo no se pudo hacer. Dos pulsos graves, que no se confunden con nada.
    "error": (Tono(330, 330, 120), _silencio(70), Tono(330, 330, 120)),
    # La unidad está lista de verdad, que en el agente son unos 25 segundos
    # después de haber pulsado. Un arpegio para que se distinga a lo lejos.
    "listo": (Tono(523, 523, 90), Tono(659, 659, 90), Tono(784, 784, 140)),
    # El volumen ya estaba en el límite: la pulsación se registró pero no cambió
    # nada, y sin esto parecería que el botón no funciona.
    "tope": (Tono(1000, 1000, 60), _silencio(50), Tono(1000, 1000, 60)),
}

Reproductor = Callable[[Path], Awaitable[None]]


async def _reproducir_con_aplay(ruta: Path) -> None:
    """Reproduce un WAV con `aplay`, sin dejar que se cuelgue la cola."""
    proceso = await asyncio.create_subprocess_exec(
        "aplay",
        "-q",
        "-D",
        "default",
        str(ruta),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proceso.communicate(), TIMEOUT_REPRODUCCION)
    except TimeoutError:
        logger.warning(f"aplay se ha colgado con {ruta.name}; lo mato")
        proceso.kill()
        await proceso.wait()
        return
    if proceso.returncode != 0:
        logger.warning(f"aplay ha fallado con {ruta.name}: {err.decode(errors='replace').strip()}")


class Pitidos:
    """Genera y reproduce el catálogo de pitidos."""

    def __init__(
        self,
        directorio: Path,
        *,
        reproductor: Reproductor = _reproducir_con_aplay,
        catalogo: dict[str, tuple[Tono, ...]] | None = None,
    ) -> None:
        """Prepara el reproductor sin generar ni sonar nada todavía.

        Args:
            directorio: Dónde viven los WAV. Se crea si no está.
            reproductor: Cómo se reproduce un fichero. Inyectable para los tests.
            catalogo: Sonidos disponibles. Por defecto, `CATALOGO`.
        """
        self._directorio = directorio
        self._reproductor = reproductor
        self._catalogo = catalogo if catalogo is not None else CATALOGO
        self._cola: asyncio.Queue[Path] = asyncio.Queue(maxsize=TAMANO_COLA)
        self._tarea: asyncio.Task[None] | None = None

    # -- Generación -----------------------------------------------------------

    def preparar(self) -> None:
        """Genera los WAV que falten. Es idempotente y rápido si ya están.

        El nombre de cada fichero lleva un resumen de los parámetros del tono, así
        que cambiar una frecuencia en el catálogo genera un fichero nuevo en el
        siguiente arranque en lugar de reutilizar el viejo. Sin eso habría que
        acordarse de borrar a mano, que es exactamente lo que no se recuerda.
        """
        self._directorio.mkdir(parents=True, exist_ok=True)
        for nombre in self._catalogo:
            ruta = self.ruta_de(nombre)
            if not ruta.exists():
                self._escribir(ruta, self._catalogo[nombre])
                logger.debug(f"Pitido generado: {ruta.name}")

    def ruta_de(self, nombre: str) -> Path:
        """Devuelve la ruta del WAV de un pitido, con el resumen de sus tonos."""
        tonos = self._catalogo[nombre]
        firma = ";".join(f"{t.hz_inicio:.1f},{t.hz_fin:.1f},{t.ms}" for t in tonos)
        resumen = hashlib.sha256(firma.encode()).hexdigest()[:8]
        return self._directorio / f"{nombre}-{resumen}.wav"

    def _escribir(self, ruta: Path, tonos: tuple[Tono, ...]) -> None:
        """Sintetiza los tonos y los escribe como WAV."""
        muestras = bytearray()
        for tono in tonos:
            muestras += self._sintetizar(tono)

        # Se escribe en un temporal del mismo directorio y se renombra, para que
        # nadie pueda leer un WAV a medias si dos procesos arrancan a la vez.
        temporal = ruta.with_suffix(".parcial")
        with wave.open(str(temporal), "wb") as w:
            w.setnchannels(CANALES)
            w.setsampwidth(BYTES_POR_MUESTRA)
            w.setframerate(FRECUENCIA_MUESTREO)
            w.writeframes(bytes(muestras))
        temporal.replace(ruta)

    @staticmethod
    def _sintetizar(tono: Tono) -> bytes:
        """Genera un tramo de audio, con barrido de frecuencia y rampas."""
        total = int(FRECUENCIA_MUESTREO * tono.ms / 1000)
        if total <= 0:
            return b""
        if tono.es_silencio:
            return b"\x00" * (total * CANALES * BYTES_POR_MUESTRA)

        rampa = min(int(FRECUENCIA_MUESTREO * RAMPA_MS / 1000), total // 2)
        marco = bytearray()
        fase = 0.0
        for i in range(total):
            # Barrido lineal por acumulación de fase. Calcular sin(2*pi*f(t)*t)
            # directamente daría un salto de fase en cuanto f cambia, y ese salto
            # se oye igual que un corte.
            progreso = i / total
            hz = tono.hz_inicio + (tono.hz_fin - tono.hz_inicio) * progreso
            fase += 2 * math.pi * hz / FRECUENCIA_MUESTREO

            ganancia = 1.0
            if rampa > 0:
                if i < rampa:
                    ganancia = i / rampa
                elif i >= total - rampa:
                    ganancia = (total - 1 - i) / rampa

            valor = int(math.sin(fase) * AMPLITUD * ganancia * 32767)
            marco += struct.pack("<h", valor) * CANALES
        return bytes(marco)

    # -- Reproducción ---------------------------------------------------------

    def sonar(self, nombre: str) -> None:
        """Encola un pitido y vuelve al instante.

        No es una corrutina a propósito: pulsar un botón no debe esperar a que
        suene nada. Si la cola está llena se descarta, porque un pitido que llega
        tarde ya no informa de nada.
        """
        try:
            ruta = self.ruta_de(nombre)
        except KeyError:
            logger.error(f"Pitido desconocido: {nombre!r}")
            return
        try:
            self._cola.put_nowait(ruta)
        except asyncio.QueueFull:
            logger.debug(f"Cola de pitidos llena; descarto {nombre!r}")

    async def servir(self) -> None:
        """Reproduce lo encolado, de uno en uno, hasta que se la cancele.

        Un fallo al reproducir no puede tumbar la tarea: si el `aplay` de un
        pitido falla, los siguientes tienen que seguir sonando.
        """
        while True:
            ruta = await self._cola.get()
            try:
                await self._reproductor(ruta)
            except asyncio.CancelledError:
                raise
            # Se captura todo a propósito: la tarea que reproduce no puede morir.
            except Exception as e:
                logger.warning(f"No he podido reproducir {ruta.name}: {e}")
            finally:
                self._cola.task_done()

    async def __aenter__(self) -> Pitidos:
        """Genera lo que falte y arranca la tarea que reproduce."""
        self.preparar()
        self._tarea = asyncio.create_task(self.servir())
        return self

    async def __aexit__(self, *_: object) -> None:
        """Para la tarea que reproduce."""
        if self._tarea is None:
            return
        self._tarea.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._tarea
        self._tarea = None


__all__ = [
    "AMPLITUD",
    "CANALES",
    "CATALOGO",
    "FRECUENCIA_MUESTREO",
    "RAMPA_MS",
    "TAMANO_COLA",
    "Pitidos",
    "Reproductor",
    "Tono",
]
