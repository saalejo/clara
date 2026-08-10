"""El audio de la llamada dentro del agente: recepción del SCO y transporte.

Tres piezas, de abajo arriba:

* `ClienteAudioSCO` se conecta al socket de audio del puente
  (`run/telefonia-audio.sock`) y recibe, por cada llamada, los metadatos y el
  descriptor del SCO (`SCM_RIGHTS`). Reconecta solo, con la misma filosofía
  que el resto de la telefonía: sin puente no hay audio, pero el agente sigue.
* `NucleoSCO` es el bucle de E/S sobre el socket, y concentra la única
  ingeniería delicada de la fase 2: **la salida va esclava del reloj de la
  radio**. El SCO entrega un paquete cada pocos ms con el reloj del enlace;
  por cada paquete que entra se envía un paquete de vuelta — audio encolado si
  lo hay, silencio si no. Así no hay deriva que compensar: el mismo tique
  gobierna los dos sentidos. Hasta el primer paquete rige el cebador de
  silencio, la trampa medida en `audio_sco.py` del puente.
* `TransporteSCO` viste el núcleo de transporte de Pipecat: la entrada empuja
  `InputAudioRawFrame` de PCM 8 kHz mono y la salida encola lo que el TTS
  produzca, con contrapresión para que un sintetizador más rápido que el
  tiempo real no infle la latencia de la llamada.

El PCM de CVSD es crudo: 8 kHz, mono, 16 bits — nada que descodificar.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from voice_agent.telefonia_codec import CODEC_CVSD, FRECUENCIA_PIPELINE, Codec, crear_codec

CANALES_SCO = 1

#: Paquete clásico de SCO por USB con CVSD; el real se aprende del primer
#: paquete recibido.
BLOQUE_SCO = 48
SEGUNDOS_ENTRE_CEBOS = 0.003

#: Contrapresión de la salida: como mucho este audio puede esperar en cola.
#: Medio segundo mantiene la latencia de la llamada conversacional; lo que el
#: TTS produzca por encima espera su turno sin perderse.
#: En bytes de línea; con CVSD equivale a 0,5 s y con mSBC a ~1 s de audio.
MAXIMO_COLA_SALIDA = 8000

#: Cada cuántos segundos se reintenta la conexión con el puente.
SEGUNDOS_ENTRE_RECONEXIONES = 5.0

#: La entrada se agrupa en bloques de al menos 20 ms antes de entrar al
#: pipeline. El SCO entrega paquetes de pocos ms (hasta 330 por segundo) y
#: empujar cada uno como frame satura el pipeline en esta placa: más de 1.600
#: pases de frame por segundo entre cinco procesadores, la cola crece más
#: rápido de lo que drena, y el saludo quedó 14 s sepultado detrás — medido.
#: El micrófono de la sala entrega trozos de 20 ms y por eso nunca sufrió esto.
BLOQUE_ENTRADA = 640  # bytes: 20 ms de PCM de 16 bits a 16 kHz


class NucleoSCO:
    """El bucle de E/S de una llamada, con la salida esclava de la recepción."""

    def __init__(
        self,
        sock: socket.socket,
        al_recibir: Callable[[bytes], Awaitable[None]] | None = None,
        silencio: Callable[[int], bytes] = bytes,
    ) -> None:
        """Prepara el núcleo sin arrancar ninguna tarea.

        Args:
            sock: El socket SCO (o su doble en tests), ya aceptado.
            al_recibir: Corrutina que recibe cada paquete de línea entrante.
            silencio: Fabrica `n` bytes de línea que suenen a silencio. Con
                CVSD son ceros; con mSBC tienen que ser tramas codificadas,
                porque un cero no es una trama válida.
        """
        sock.setblocking(False)
        self._sock = sock
        self._al_recibir = al_recibir
        self._silencio = silencio
        self._cola = bytearray()
        self._hay_hueco = asyncio.Event()
        self._hay_hueco.set()
        self._tarea: asyncio.Task[None] | None = None
        self._cebador: asyncio.Task[None] | None = None
        #: Se enciende cuando el canal muere; quien montó la llamada lo espera
        #: para desmontarla.
        self.cerrado = asyncio.Event()
        self.paquetes = 0
        self.octetos = 0

    def arrancar(self) -> None:
        """Pone en marcha el lector y el cebador. Llamarla dos veces no hace nada.

        La reentrada importa: el núcleo se arranca en cuanto llega el
        descriptor — montar el pipeline carga modelos durante segundos y un
        SCO aceptado pero mudo se lo carga el móvil — y el transporte vuelve a
        llamarla al arrancar el pipeline.
        """
        if self._tarea is not None:
            return
        self._tarea = asyncio.create_task(self._bucle())
        self._cebador = asyncio.create_task(self._cebar())

    async def escribir(self, datos: bytes) -> None:
        """Encola PCM para el otro lado, esperando si la cola está llena.

        La espera es la contrapresión: el TTS produce más rápido que el tiempo
        real y sin ella la cola crecería sin límite — audio con segundos de
        retraso en una conversación telefónica.
        """
        while len(self._cola) >= MAXIMO_COLA_SALIDA:
            self._hay_hueco.clear()
            await self._hay_hueco.wait()
        self._cola.extend(datos)

    async def _bucle(self) -> None:
        """Recibe, entrega y contesta: un paquete de salida por paquete de entrada."""
        bucle = asyncio.get_running_loop()
        try:
            while True:
                datos = await bucle.sock_recv(self._sock, 1024)
                if not datos:
                    break
                if self.paquetes == 0:
                    logger.info(f"Primer paquete SCO recibido: {len(datos)} B")
                if self._cebador is not None:
                    self._cebador.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._cebador
                    self._cebador = None
                if self._al_recibir is not None:
                    await self._al_recibir(datos)
                self.paquetes += 1
                self.octetos += len(datos)
                await self._contestar(len(datos))
        except OSError as e:
            logger.info(f"El canal SCO se ha cerrado: {e}")
        finally:
            logger.info(f"Núcleo SCO: {self.paquetes} paquetes, {self.octetos} B recibidos")
            self.cerrado.set()

    async def _contestar(self, tamano: int) -> None:
        """Envía un paquete del tamaño del recibido: cola primero, silencio después."""
        if len(self._cola) >= tamano:
            paquete = bytes(self._cola[:tamano])
            del self._cola[:tamano]
            if len(self._cola) < MAXIMO_COLA_SALIDA:
                self._hay_hueco.set()
        else:
            paquete = self._silencio(tamano)
        bucle = asyncio.get_running_loop()
        with contextlib.suppress(OSError):
            await bucle.sock_sendall(self._sock, paquete)

    async def _cebar(self) -> None:
        """Silencio hasta el primer paquete: sin transmitir, el SCO no recibe."""
        bucle = asyncio.get_running_loop()
        silencio = bytes(BLOQUE_SCO)
        with contextlib.suppress(OSError):
            while True:
                await bucle.sock_sendall(self._sock, silencio)
                await asyncio.sleep(SEGUNDOS_ENTRE_CEBOS)

    async def parar(self) -> None:
        """Detiene las tareas y cierra el socket."""
        for tarea in (self._tarea, self._cebador):
            if tarea is not None:
                tarea.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tarea
        self._tarea = self._cebador = None
        with contextlib.suppress(OSError):
            self._sock.close()
        self.cerrado.set()


class ClienteAudioSCO:
    """Recoge del puente el audio de cada llamada, y sobrevive sin puente."""

    def __init__(
        self,
        ruta_socket: Path,
        al_llegar_llamada: Callable[[socket.socket, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Prepara el cliente sin conectar.

        Args:
            ruta_socket: El `telefonia-audio.sock` del puente.
            al_llegar_llamada: Corrutina que recibe el socket SCO y los
                metadatos (`tarjeta`, `codec`, `mtu`) de cada llamada.
        """
        self.ruta = ruta_socket
        self._al_llegar = al_llegar_llamada
        self._tarea: asyncio.Task[None] | None = None

    def arrancar(self) -> None:
        """Deja el cliente conectando y reconectando en segundo plano."""
        self._tarea = asyncio.create_task(self._mantener())

    async def _mantener(self) -> None:
        """Conecta, atiende, y sobrevive a TODO menos a la cancelación.

        El primer despliegue enseñó por qué el catch-all: una excepción que no
        fuera OSError mataba la tarea en silencio —sin log, sin reconexión— y
        el puente se quedaba con un consumidor fantasma al que entregarle el
        audio (EPIPE) mientras el agente creía tenerlo todo en marcha.
        """
        while True:
            try:
                await self._atender()
                logger.info("Canal de audio del puente cerrado; reintentando")
            except asyncio.CancelledError:
                raise
            except OSError as e:
                logger.debug(f"Sin puente de audio ({e}); reintento en unos segundos")
            except Exception:
                logger.exception("El cliente de audio SCO tropezó; reintentando")
            await asyncio.sleep(SEGUNDOS_ENTRE_RECONEXIONES)

    async def _atender(self) -> None:
        """Una conexión con el puente, hasta que se caiga."""
        bucle = asyncio.get_running_loop()
        conexion = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conexion.setblocking(False)
        try:
            await bucle.sock_connect(conexion, str(self.ruta))
            logger.info(f"Conectado al audio del puente en {self.ruta}")
            while True:
                # `recv_fds` es bloqueante; se despacha a un hilo cuando el
                # selector diga que hay datos, para no parar el bucle.
                await _esperar_legible(conexion)
                datos, fds = await bucle.run_in_executor(None, _recibir_con_fds, conexion)
                if not datos:
                    logger.info("El puente cerró el canal de audio")
                    return
                metadatos = json.loads(datos.decode())
                if not fds:
                    logger.warning(f"Metadatos sin descriptor: {metadatos}")
                    continue
                sco = socket.socket(fileno=fds[0])
                logger.info(f"Audio de llamada recibido: {metadatos}")
                await self._al_llegar(sco, metadatos)
        finally:
            with contextlib.suppress(OSError):
                conexion.close()

    async def parar(self) -> None:
        """Detiene el cliente."""
        if self._tarea is not None:
            self._tarea.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tarea
            self._tarea = None


async def _esperar_legible(sock: socket.socket) -> None:
    """Espera a que el socket tenga datos, sin consumirlos."""
    bucle = asyncio.get_running_loop()
    futuro: asyncio.Future[None] = bucle.create_future()
    fd = sock.fileno()

    def _avisar() -> None:
        if not futuro.done():
            futuro.set_result(None)

    bucle.add_reader(fd, _avisar)
    try:
        await futuro
    finally:
        bucle.remove_reader(fd)


def _recibir_con_fds(sock: socket.socket) -> tuple[bytes, list[int]]:
    """Un `recv_fds` con la firma que necesita el executor."""
    datos, fds, _flags, _addr = socket.recv_fds(sock, 4096, 4)
    return datos, list(fds)


class _EntradaSCO(BaseInputTransport):
    """La voz de quien llama, hacia el pipeline: descodificada y a 16 kHz."""

    def __init__(self, nucleo: NucleoSCO, codec: Codec, params: TransportParams) -> None:
        super().__init__(params)
        self._nucleo = nucleo
        self._codec = codec
        self._acumulado = bytearray()

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        self._nucleo._al_recibir = self._empujar
        self._nucleo.arrancar()
        await self.set_transport_ready(frame)

    async def _empujar(self, datos: bytes) -> None:
        # Descodifica lo que llegue por la línea y lo agrupa en bloques de al
        # menos 20 ms: empujar cada paquete de pocos ms como frame ahoga el
        # pipeline. Ver BLOQUE_ENTRADA.
        self._acumulado.extend(self._codec.decodificar(datos))
        if len(self._acumulado) < BLOQUE_ENTRADA:
            return
        bloque = bytes(self._acumulado)
        self._acumulado.clear()
        await self.push_audio_frame(
            InputAudioRawFrame(
                audio=bloque, sample_rate=FRECUENCIA_PIPELINE, num_channels=CANALES_SCO
            )
        )

    async def cleanup(self) -> None:
        await super().cleanup()  # type: ignore[no-untyped-call]
        await self._nucleo.parar()


class _SalidaSCO(BaseOutputTransport):
    """La voz del agente, hacia quien llama: codificada para la línea."""

    def __init__(self, nucleo: NucleoSCO, codec: Codec, params: TransportParams) -> None:
        super().__init__(params)
        self._nucleo = nucleo
        self._codec = codec

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        # Sin esto, el escritor de audio del transporte de salida no arranca
        # nunca y el TTS sintetiza hacia el vacío: `write_audio_frame` no se
        # llama. La entrada ya lo hacía; la salida se quedó sin él y costó una
        # llamada muda descubrirlo.
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        await self._nucleo.escribir(self._codec.codificar(frame.audio))
        return True


class TransporteSCO(BaseTransport):
    """El transporte de Pipecat sobre el socket SCO de una llamada.

    Uno por llamada: el socket muere con ella y el transporte no se reutiliza.
    `nucleo.cerrado` es la señal de desmontaje para quien haya montado el
    pipeline de la llamada.
    """

    def __init__(self, sock: socket.socket, codec: int = CODEC_CVSD) -> None:
        """Monta entrada y salida sobre el socket de una llamada.

        Args:
            sock: El socket SCO ya aceptado.
            codec: El códec que negoció HFP (`CODEC_CVSD` o `CODEC_MSBC`);
                decide cómo se traduce la línea al PCM de 16 kHz del pipeline.
        """
        params = TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=FRECUENCIA_PIPELINE,
            audio_out_sample_rate=FRECUENCIA_PIPELINE,
            audio_in_channels=CANALES_SCO,
            audio_out_channels=CANALES_SCO,
        )
        super().__init__()
        el_codec = crear_codec(codec)
        self.nucleo = NucleoSCO(sock, silencio=el_codec.silencio)
        self._entrada = _EntradaSCO(self.nucleo, el_codec, params)
        self._salida = _SalidaSCO(self.nucleo, el_codec, params)

    def input(self) -> FrameProcessor:
        """La entrada del transporte."""
        return self._entrada

    def output(self) -> FrameProcessor:
        """La salida del transporte."""
        return self._salida


__all__ = [
    "BLOQUE_SCO",
    "CANALES_SCO",
    "MAXIMO_COLA_SALIDA",
    "ClienteAudioSCO",
    "NucleoSCO",
    "TransporteSCO",
]
