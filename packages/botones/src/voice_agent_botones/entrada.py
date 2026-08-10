"""Lectura del device HID de la tarjeta, sin dependencias externas.

Un `/dev/input/eventN` entrega una secuencia de `struct input_event` de tamaño
fijo. Leerlo son veinte líneas de `struct` y `loop.add_reader`, así que no se usa
`python-evdev`: es una extensión en C sin rueda para aarch64 y entraría en el
`uv.lock` del workspace a cambio de tres constantes y un `ioctl`.

## Por qué `leer(timeout)` y no un iterador asíncrono

Un `async for` es más bonito pero no compone con el detector de gestos, que
necesita ser despertado cuando **no** llega nada: el cruce de la frontera de
nivel 2 ocurre a los 700 ms de tener la tecla pulsada, sin evento alguno de por
medio. Con `leer(timeout)` el bucle del demonio queda en tres líneas honestas.

## Trampas

- **`loop.remove_reader` va ANTES del `os.close`.** Al revés, asyncio se queda
  con un descriptor cerrado registrado en el selector y el siguiente fd que reuse
  ese número hereda la basura.
- **`value == 2` es autorrepetición** y se descarta. Este device no la manda —no
  declara `EV_REP`— pero el código no debe romperse si otro la manda.
- **Una lectura puede traer varios eventos.** Se trocea por `TAMANO_EVENTO`, y se
  guarda el resto por si alguna vez llega un evento partido.
- **El grab no sobrevive a la reapertura.** Cuando el USB se reenumera hay que
  volver a hacer `EVIOCGRAB`, y avisar a quien lleve un gesto a medias.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import os
import struct
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from loguru import logger

from voice_agent_botones.gestos import (
    NOMBRES,
    TECLAS,
    DetectorDeGestos,
    Gesto,
    Pulsacion,
)

# `struct input_event` en un sistema de 64 bits: dos `time_t` para el timeval,
# dos `__u16` (tipo y código) y un `__s32` (valor).
FORMATO = "llHHi"
TAMANO_EVENTO = struct.calcsize(FORMATO)

# Tipos de evento de `linux/input-event-codes.h`. Solo interesa EV_KEY; EV_SYN y
# EV_MSC (con su MSC_SCAN, el scancode crudo) llegan y se descartan.
EV_KEY = 0x01

# `EVIOCGRAB` = _IOW('E', 0x90, int): pedir el device en exclusiva. Necesario
# porque tiene handler `kbd` y sin esto las teclas se inyectan además en la
# consola virtual del anfitrión.
EVIOCGRAB = 0x40044590

# Cuántos eventos se intentan leer de una vez. Sobra de largo: el device manda
# tres o cuatro por pulsación.
EVENTOS_POR_LECTURA = 64


def _abrir_device(ruta: Path) -> int:
    """Abre el device en solo lectura y sin bloqueo."""
    return os.open(ruta, os.O_RDONLY | os.O_NONBLOCK)


class LectorDeBotones:
    """Entrega las pulsaciones del device, reabriéndolo si desaparece.

    Nunca lanza por culpa del hardware: si el device no está, espera; si se va a
    mitad, lo reabre. El demonio no debería morir porque alguien haya tocado el
    hub USB.
    """

    def __init__(
        self,
        ruta: Path,
        *,
        acaparar: bool = True,
        espera_segundos: float = 2.0,
        reloj: Callable[[], float] = time.monotonic,
        abridor: Callable[[Path], int] = _abrir_device,
        al_reconectar: Callable[[], None] | None = None,
    ) -> None:
        """Prepara el lector sin abrir nada todavía.

        Args:
            ruta: Device a leer. Usa siempre un enlace de `/dev/input/by-id/`.
            acaparar: Pedir el device en exclusiva con `EVIOCGRAB`.
            espera_segundos: Cada cuánto reintentar cuando el device no está.
            reloj: Reloj monótono. Inyectable para los tests.
            abridor: Cómo abrir el device. Inyectable para los tests, que le
                pasan el extremo de lectura de un `os.pipe()`.
            al_reconectar: Se llama tras reabrir el device. Sirve para que el
                detector olvide un gesto a medias.
        """
        self._ruta = ruta
        self._acaparar = acaparar
        self._espera = espera_segundos
        self._reloj = reloj
        self._abridor = abridor
        self._al_reconectar = al_reconectar

        self._fd: int | None = None
        self._resto = b""
        self._cola: asyncio.Queue[Pulsacion] = asyncio.Queue()
        self._ausencia_registrada = False

    # -- Ciclo de vida --------------------------------------------------------

    async def abrir(self) -> None:
        """Abre el device, esperando lo que haga falta a que aparezca.

        En el arranque de la placa es normal que el USB tarde en enumerar, así
        que no encontrarlo no es un error: se espera. Solo se registra el primer
        intento fallido de cada racha, o el journal se llenaría a un mensaje cada
        dos segundos para siempre.
        """
        while self._fd is None:
            try:
                fd = self._abridor(self._ruta)
            except OSError as e:
                if not self._ausencia_registrada:
                    logger.warning(f"No puedo abrir {self._ruta} ({e.strerror}); esperando")
                    self._ausencia_registrada = True
                await asyncio.sleep(self._espera)
                continue

            self._fd = fd
            self._resto = b""
            self._ausencia_registrada = False
            if self._acaparar:
                self._tomar_en_exclusiva(fd)
            asyncio.get_running_loop().add_reader(fd, self._al_haber_datos)
            logger.info(f"Escuchando botones en {self._ruta}")

    async def cerrar(self) -> None:
        """Suelta el device. Es idempotente."""
        self._soltar()

    async def __aenter__(self) -> LectorDeBotones:
        """Abre el device al entrar en el contexto."""
        await self.abrir()
        return self

    async def __aexit__(self, *_: object) -> None:
        """Suelta el device al salir del contexto."""
        await self.cerrar()

    # -- Lectura --------------------------------------------------------------

    async def leer(self, timeout: float | None = None) -> Pulsacion | None:
        """Devuelve la siguiente pulsación, o `None` si vence el timeout.

        Args:
            timeout: Segundos a esperar como máximo. `None` espera lo que haga
                falta. Un valor menor o igual que cero se atiende sin esperar,
                para que el detector pueda cerrar un vencimiento ya cumplido.
        """
        if timeout is not None and timeout <= 0:
            try:
                return self._cola.get_nowait()
            except asyncio.QueueEmpty:
                return None
        try:
            return await asyncio.wait_for(self._cola.get(), timeout)
        except TimeoutError:
            return None

    def ahora(self) -> float:
        """Devuelve el instante monótono actual según el reloj inyectado.

        Existe para que el bucle de gestos no tenga que conocer el reloj: el
        lector es quien lo tiene, porque es quien marca las pulsaciones.
        """
        return self._reloj()

    # -- Interno --------------------------------------------------------------

    def _tomar_en_exclusiva(self, fd: int) -> None:
        """Hace `EVIOCGRAB`, tolerando que otro lo tenga ya.

        Si falla se sigue adelante a propósito: un demonio que lee los botones
        pero deja que las teclas lleguen también a la consola es molesto; uno que
        no arranca es inútil.
        """
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 1)
        except OSError as e:
            logger.warning(
                f"No he podido tomar {self._ruta} en exclusiva ({e.strerror}). "
                "Las teclas llegarán también a la consola. ¿Hay otra instancia en marcha?"
            )

    def _soltar(self) -> None:
        """Quita el lector del selector y cierra el descriptor, en ese orden."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        # Sin bucle de eventos (en pleno apagado) no hay selector del que quitarlo,
        # pero el descriptor sí hay que cerrarlo.
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().remove_reader(fd)
        os.close(fd)

    def _al_haber_datos(self) -> None:
        """Vacía el device y encola las pulsaciones. Lo llama el bucle de eventos."""
        fd = self._fd
        if fd is None:  # pragma: no cover — carrera con el cierre
            return
        try:
            datos = os.read(fd, TAMANO_EVENTO * EVENTOS_POR_LECTURA)
        except BlockingIOError:  # pragma: no cover — lectura espuria
            return
        except OSError as e:
            # ENODEV es la tarjeta desapareciendo del hub, que está documentado
            # que pasa: comparte hub con el dongle Bluetooth.
            # `OSError.errno` es opcional en las anotaciones, aunque aquí siempre
            # venga puesto: el `or 0` es para el tipador, no para el caso real.
            nombre = errno.errorcode.get(e.errno or 0, str(e.errno))
            logger.warning(f"{self._ruta} ha dejado de responder ({nombre}); voy a reabrirlo")
            self._soltar()
            asyncio.get_running_loop().create_task(self._reconectar())
            return

        if not datos:  # pragma: no cover — fin de fichero
            self._soltar()
            asyncio.get_running_loop().create_task(self._reconectar())
            return

        for pulsacion in self._interpretar(datos):
            self._cola.put_nowait(pulsacion)

    def _interpretar(self, datos: bytes) -> list[Pulsacion]:
        """Trocea el búfer en eventos y se queda con las teclas que interesan."""
        crudo = self._resto + datos
        sobrantes = len(crudo) % TAMANO_EVENTO
        if sobrantes:
            # Un evento partido. No debería ocurrir en un device de eventos, pero
            # el coste de contemplarlo es una línea.
            self._resto = crudo[len(crudo) - sobrantes :]
            crudo = crudo[: len(crudo) - sobrantes]
        else:
            self._resto = b""

        ahora = self._reloj()
        pulsaciones: list[Pulsacion] = []
        for inicio in range(0, len(crudo), TAMANO_EVENTO):
            _s, _us, tipo, codigo, valor = struct.unpack(
                FORMATO, crudo[inicio : inicio + TAMANO_EVENTO]
            )
            if tipo != EV_KEY or codigo not in TECLAS or valor == 2:
                continue
            pulsaciones.append(Pulsacion(tecla=codigo, pulsada=valor == 1, momento=ahora))
            logger.debug(f"{NOMBRES[codigo]} {'pulsa' if valor == 1 else 'suelta'}")
        return pulsaciones

    async def _reconectar(self) -> None:
        """Vuelve a abrir el device y avisa de que el estado anterior ya no vale."""
        await self.abrir()
        if self._al_reconectar is not None:
            self._al_reconectar()


async def gestos_de(lector: LectorDeBotones, detector: DetectorDeGestos) -> AsyncIterator[Gesto]:
    """Empareja el lector con el detector y va entregando gestos completos.

    Es el bucle que comparten la sonda de diagnóstico y el demonio, y toda su
    gracia está en el timeout: se espera lo que el detector diga que falta para su
    próximo vencimiento, de modo que el cruce de una frontera de nivel se avise a
    tiempo aunque no llegue ningún evento. Con `None` se espera indefinidamente,
    que es lo correcto cuando no hay ninguna tecla pulsada.
    """
    while True:
        pulsacion = await lector.leer(detector.proximo_despertar)
        gesto = (
            detector.alimentar(pulsacion)
            if pulsacion is not None
            else detector.vencimientos(lector.ahora())
        )
        if gesto is None and pulsacion is not None:
            # Una pulsación puede dejar pendiente un vencimiento inmediato (una
            # soltada con ventana de agrupación cero, por ejemplo).
            gesto = detector.vencimientos(lector.ahora())
        if gesto is not None:
            yield gesto


__all__ = [
    "EVIOCGRAB",
    "EV_KEY",
    "FORMATO",
    "TAMANO_EVENTO",
    "LectorDeBotones",
    "gestos_de",
]
