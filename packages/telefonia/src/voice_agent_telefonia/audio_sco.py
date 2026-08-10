"""El audio de la llamada: recibir el socket SCO de oFono y no perderlo.

Primera etapa de la fase 2 de `docs/telefonia.md`. oFono ofrece
`org.ofono.HandsfreeAudioManager.Register(o, ay)`: quien registra un
`HandsfreeAudioAgent` recibe en `NewConnection(o card, h fd, y codec)` el
socket SCO **ya conectado** de cada llamada, como descriptor de fichero. Sin
agente registrado oFono rechaza el canal de audio y la llamada suena por el
móvil; con él, el audio es nuestro — y también nuestra responsabilidad, porque
el móvil deja de sonar.

Por eso el modo por defecto es `off` y en él **ni siquiera se registra el
agente**: el comportamiento del sistema queda idéntico al de la fase 1. El
modo `eco` existe para validar el camino con una llamada de verdad: devuelve a
quien llama su propia voz, que demuestra los dos sentidos del canal de una
sola vez.

Dos decisiones de alcance, deliberadas:

* **Solo CVSD.** Registrar únicamente el códec 0x01 hace que la negociación
  nunca elija mSBC, aunque el dongle lo soporte: CVSD es PCM crudo de 8 kHz y
  mSBC habría que descodificarlo. El ancho de banda llegará cuando el
  contestador funcione, no antes.
* **Leer y escribir por paquetes del MTU.** El SCO es `SOCK_SEQPACKET`: cada
  `recv` entrega un paquete completo, y el kernel descarta en silencio lo que
  no encaje en bloques del MTU al escribir. Devolver exactamente lo que se
  recibió respeta eso sin aritmética.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING

from dbus_fast import Message, MessageType
from loguru import logger

from voice_agent_telefonia.bus import ErrorBus, llamar

if TYPE_CHECKING:
    from dbus_fast.aio import MessageBus

#: Ruta con la que el agente de audio se exporta en nuestro lado del bus.
RUTA_AGENTE = "/voice_agent/audio_sco"

INTERFAZ_AGENTE = "org.ofono.HandsfreeAudioAgent"
INTERFAZ_MANAGER = "org.ofono.HandsfreeAudioManager"

#: Códecs de `src/hfp.h` de oFono.
CODEC_CVSD = 0x01
CODEC_MSBC = 0x02
NOMBRES_CODEC = {CODEC_CVSD: "CVSD", CODEC_MSBC: "mSBC"}

#: `SOL_SCO` y `SCO_OPTIONS` de la cabecera `bluetooth/sco.h`; el módulo
#: `socket` de Python no los exporta.
SOL_SCO = 17
SCO_OPTIONS = 1

#: MTU típico de SCO por USB con CVSD, si el kernel no contesta al getsockopt.
MTU_POR_DEFECTO = 48

#: Tamaño de los paquetes de silencio del cebador: el clásico de SCO por USB
#: con CVSD. 48 bytes son 24 muestras de PCM de 16 bits — 3 ms a 8 kHz.
BLOQUE_CEBADOR = 48
SEGUNDOS_ENTRE_CEBOS = 0.003

#: Cada cuántos segundos deja el eco una línea de estadísticas en el log.
CADA_CUANTO_ESTADISTICAS = 5.0

MODO_OFF = "off"
MODO_ECO = "eco"
MODO_AGENTE = "agente"


class AudioSCO:
    """Registra el agente de audio en oFono y atiende lo que este le entregue.

    El registro no es un acto único: muere con oFono y hay que rehacerlo cada
    vez que el demonio se reinicia. En vez de vigilar señales, `asegurar_registro`
    compara el dueño actual del nombre `org.ofono` con el que había al
    registrarse — si cambió, el registro anterior ya no existe. Está pensada
    para llamarse desde el bucle de vigilancia del servicio, que ya corre cada
    pocos segundos y ya tolera errores.
    """

    def __init__(self, modo: str = MODO_OFF, con_msbc: bool = False) -> None:
        """Prepara el agente sin tocar el bus todavía.

        Args:
            modo: `off` para no registrar nada, `eco` para la prueba de audio,
                `agente` para entregarle el audio al agente de voz.
            con_msbc: Anunciar también mSBC en el registro. Va aparte porque
                el códec se pacta al abrir la sesión HFP y no hay vuelta atrás
                por llamada: hasta validar mSBC con una llamada real, mejor
                que la producción no lo ofrezca.
        """
        self.modo = modo
        self.con_msbc = con_msbc
        self._bus: MessageBus | None = None
        self._dueno_ofono: str | None = None
        self._tareas: set[asyncio.Task[None]] = set()
        self._escucha: socket.socket | None = None
        self._aceptador: asyncio.Task[None] | None = None
        self._consumidor: socket.socket | None = None

    @property
    def activo(self) -> bool:
        """Si este agente debe registrarse en oFono, AHORA.

        En modo `agente` la respuesta depende de si hay un consumidor
        conectado: registrarse sin nadie que se haga cargo del audio dejaría
        las llamadas sin sonido en el móvil a cambio de nada. Mientras el
        agente de voz no esté, mejor ni figurar.
        """
        if self.modo == MODO_AGENTE:
            return self._consumidor is not None
        return self.modo != MODO_OFF

    @property
    def registrado(self) -> bool:
        """Si hay un registro vigente con el oFono actual."""
        return self._dueno_ofono is not None

    # --- El consumidor (modo agente) -----------------------------------------

    async def arrancar(self, ruta_socket: Path) -> None:
        """Abre el socket por el que el agente de voz recogerá el audio.

        Solo en modo `agente`. El socket vive junto al de la API
        (`<DATA_DIR>/run/`), que el contenedor ya monta; por él viaja, para
        cada llamada, una línea de JSON con los metadatos y el descriptor del
        SCO como datos auxiliares (`SCM_RIGHTS`).
        """
        if self.modo != MODO_AGENTE:
            return
        ruta_socket.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            ruta_socket.unlink()
        escucha = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        escucha.bind(str(ruta_socket))
        escucha.listen(1)
        escucha.setblocking(False)
        self._escucha = escucha
        self._aceptador = asyncio.create_task(self._aceptar())
        logger.info(f"Audio SCO en modo agente; esperando consumidor en {ruta_socket}")

    async def _aceptar(self) -> None:
        """Acepta consumidores; el último en llegar es el que vale.

        Un solo consumidor tiene el audio. Si el agente se reinicia y vuelve a
        conectar, la conexión nueva sustituye a la vieja sin drama: la vieja
        se cierra y su dueño, si sigue vivo, verá el EOF.
        """
        assert self._escucha is not None
        bucle = asyncio.get_running_loop()
        while True:
            try:
                conexion, _ = await bucle.sock_accept(self._escucha)
            except OSError:
                return
            if self._consumidor is not None:
                with contextlib.suppress(OSError):
                    self._consumidor.close()
            conexion.setblocking(False)
            self._consumidor = conexion
            logger.info("Consumidor de audio SCO conectado")

    def _entregar(self, fd: int, tarjeta: str, codec: int) -> None:
        """Le pasa el SCO al consumidor, o lo rechaza si no hay nadie.

        Cerrar el descriptor **sin confirmar el defer setup** es un rechazo
        del eSCO: el móvil se queda el audio de la llamada y esta sigue
        sonando por él, que es la degradación correcta cuando el agente de
        voz no está para hacerse cargo. Lo que NO se puede hacer es
        confirmarlo y abandonarlo, o la llamada se queda 20 s sin audio para
        nadie.
        """
        if self._consumidor is None:
            logger.warning(f"Audio SCO de {tarjeta} rechazado: no hay consumidor")
            os.close(fd)
            return
        # La aceptación diferida se confirma AQUÍ, donde vive el conocimiento
        # de la trampa; el consumidor recibe un socket ya en camino de
        # conectarse. Ver el comentario largo en `_eco`.
        sock = socket.socket(fileno=fd)
        try:
            sock.setblocking(False)
            with contextlib.suppress(OSError):
                sock.recv(1, socket.MSG_PEEK)
            metadatos = (
                json.dumps({"tarjeta": tarjeta, "codec": codec, "mtu": mtu_de(sock)}).encode()
                + b"\n"
            )
            socket.send_fds(self._consumidor, [metadatos], [sock.fileno()])
            logger.info(f"Audio SCO de {tarjeta} entregado al consumidor")
        except OSError as e:
            logger.warning(f"El consumidor no aceptó el audio ({e}); rechazando el SCO")
            with contextlib.suppress(OSError):
                self._consumidor.close()
            self._consumidor = None
        finally:
            # El descriptor viajó (o murió con el rechazo): esta copia sobra.
            sock.close()

    # --- Registro ------------------------------------------------------------

    async def asegurar_registro(self, bus: MessageBus | None) -> None:
        """Registra el agente si hace falta, incluida la vuelta de un oFono nuevo.

        No propaga errores del bus: sin oFono no hay registro posible y el
        bucle de vigilancia volverá a intentarlo en el siguiente tique.
        """
        if not self.activo or bus is None:
            return
        try:
            dueno = await llamar(
                bus,
                destino="org.freedesktop.DBus",
                ruta="/org/freedesktop/DBus",
                interfaz="org.freedesktop.DBus",
                metodo="GetNameOwner",
                firma="s",
                cuerpo=["org.ofono"],
            )
        except ErrorBus:
            # oFono no está en el bus; el registro anterior, si lo hubo, murió
            # con él.
            self._dueno_ofono = None
            return

        if self._dueno_ofono == dueno:
            return

        if self._bus is not bus:
            bus.add_message_handler(self._manejar)
            self._bus = bus

        try:
            await llamar(
                bus,
                destino="org.ofono",
                ruta="/",
                interfaz=INTERFAZ_MANAGER,
                metodo="Register",
                firma="oay",
                cuerpo=[
                    RUTA_AGENTE,
                    bytes([CODEC_CVSD, CODEC_MSBC]) if self.con_msbc else bytes([CODEC_CVSD]),
                ],
            )
        except ErrorBus as e:
            # `InUse` significa que ESTE oFono ya nos tiene registrados y el
            # dueño simplemente no se había anotado; cualquier otra cosa es un
            # fallo de verdad.
            if "InUse" not in str(e):
                logger.warning(f"No he podido registrar el agente de audio: {e}")
                return
        self._dueno_ofono = str(dueno)
        codecs = "CVSD+mSBC" if self.con_msbc else "solo CVSD"
        logger.info(f"Agente de audio SCO registrado en oFono (modo {self.modo}, {codecs})")

    # --- El agente propiamente dicho -----------------------------------------

    def _manejar(self, mensaje: Message) -> Message | None:
        """Atiende las llamadas de oFono a nuestro `HandsfreeAudioAgent`.

        Es un manejador crudo de dbus-fast y no un `ServiceInterface` por la
        misma razón que el resto del paquete habla con mensajes en crudo: el
        descriptor llega como índice en `unix_fds` y así se recoge sin
        depender de la traducción del modo de alto nivel.
        """
        if (
            mensaje.message_type is not MessageType.METHOD_CALL
            or mensaje.path != RUTA_AGENTE
            or mensaje.interface != INTERFAZ_AGENTE
        ):
            return None

        if mensaje.member == "NewConnection":
            tarjeta, indice_fd, codec = mensaje.body
            try:
                # dbus-fast cierra los descriptores del mensaje al terminar de
                # procesarlo; se duplica para que el eco tenga el suyo propio.
                fd = os.dup(mensaje.unix_fds[indice_fd])
            except (IndexError, OSError) as e:
                logger.error(f"NewConnection sin descriptor utilizable: {e}")
                return Message.new_error(
                    mensaje, "org.ofono.Error.Failed", "descriptor no utilizable"
                )
            nombre_codec = NOMBRES_CODEC.get(codec, f"?{codec}")
            logger.info(f"Audio SCO de {tarjeta}: códec {nombre_codec}, fd {fd}")
            if self.modo == MODO_AGENTE:
                self._entregar(fd, tarjeta, codec)
            else:
                tarea = asyncio.create_task(self._eco(fd))
                self._tareas.add(tarea)
                tarea.add_done_callback(self._tareas.discard)
            return Message.new_method_return(mensaje)

        if mensaje.member == "Release":
            # oFono se despide (se apaga o lo desregistra); el siguiente tique
            # de vigilancia decidirá si hay que registrarse otra vez.
            logger.info("oFono ha liberado el agente de audio")
            self._dueno_ofono = None
            return Message.new_method_return(mensaje)

        return None

    # --- El eco ---------------------------------------------------------------

    async def _eco(self, fd: int) -> None:
        """Devuelve por el SCO lo mismo que llega, hasta que el canal se cierre.

        La parte que no es obvia es el **cebador**. Medido en la placa con una
        llamada real: con el eco puro —leer primero, contestar después— el
        canal se quedó 20 segundos abierto sin entregar un solo byte. Los
        dongles USB no arrancan la recepción SCO hasta que el anfitrión
        **transmite**; es la razón por la que PulseAudio escribe silencio de
        forma continua. Leer sin haber escrito es un interbloqueo: nosotros
        esperamos su audio, el controlador espera el nuestro.

        Así que hasta que llegue el primer paquete se manda silencio a ritmo
        de radio, y desde ese momento el eco se marca con el reloj de la
        recepción: cada paquete que entra sale de vuelta, y esa misma
        transmisión mantiene viva la recepción.

        No propaga excepciones: corre en una tarea suelta y el canal muere por
        causas normales — la llamada termina, el móvil se aleja.
        """
        sock = socket.socket(fileno=fd)
        sock.setblocking(False)
        # El descriptor llega en «defer setup»: la conexión SCO está PENDIENTE
        # y es el agente quien la confirma con un recv — es lo que hace
        # PulseAudio en su backend de oFono con `recv(fd, NULL, 0, 0)`. Medido
        # aquí sin esto: ENOTCONN al transmitir, ni un "Accept Synchronous
        # Connection Request" en btmon, y el móvil rindiéndose a los 20 s
        # exactos con "Connection Accept Timeout Exceeded".
        #
        # Con MSG_PEEK y longitud 1, no 0: un recv de longitud cero puede no
        # llegar a emitir el syscall, y sin syscall no hay aceptación. El PEEK
        # garantiza que si ya hubiera audio no se pierde ni un byte.
        try:
            sock.recv(1, socket.MSG_PEEK)
            logger.debug("Defer setup del SCO confirmado")
        except BlockingIOError:
            logger.debug("Defer setup del SCO confirmado (sin datos aún)")
        except OSError as e:
            logger.warning(f"El recv de aceptación del SCO falló: {e}")
        mtu = mtu_de(sock)
        bucle = asyncio.get_running_loop()
        empezado = bucle.time()
        ultimo_informe = empezado
        paquetes = 0
        octetos = 0
        cebador = asyncio.create_task(self._cebar(sock))
        logger.info(f"Eco SCO en marcha: MTU {mtu}, cebando con silencio")
        try:
            while True:
                datos = await bucle.sock_recv(sock, mtu)
                if not datos:
                    break
                if not cebador.done():
                    cebador.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cebador
                    espera = bucle.time() - empezado
                    logger.info(f"Primer paquete SCO recibido a los {espera:.2f} s: {len(datos)} B")
                await bucle.sock_sendall(sock, datos)
                paquetes += 1
                octetos += len(datos)
                ahora = bucle.time()
                if ahora - ultimo_informe >= CADA_CUANTO_ESTADISTICAS:
                    tasa = octetos / (ahora - empezado)
                    logger.info(f"Eco SCO: {paquetes} paquetes, {octetos} B, {tasa:.0f} B/s")
                    ultimo_informe = ahora
        except OSError as e:
            logger.info(f"El canal SCO se ha cerrado: {e}")
        finally:
            if not cebador.done():
                cebador.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cebador
            with contextlib.suppress(OSError):
                sock.close()
            duracion = bucle.time() - empezado
            logger.info(f"Eco SCO terminado: {paquetes} paquetes, {octetos} B en {duracion:.1f} s")

    @staticmethod
    async def _cebar(sock: socket.socket) -> None:
        """Manda silencio hasta que lo cancelen, para despertar la recepción.

        Bloques de 48 bytes cada 3 ms: el paquete clásico de CVSD por USB, al
        ritmo que le corresponde. Deja en el log lo bastante para distinguir
        «transmito y nadie contesta» de «ni siquiera puedo transmitir», que
        piden arreglos distintos.
        """
        bucle = asyncio.get_running_loop()
        silencio = bytes(BLOQUE_CEBADOR)
        enviados = 0
        espera = 0.0
        try:
            while True:
                try:
                    await bucle.sock_sendall(sock, silencio)
                except OSError as e:
                    # Tras confirmar el «defer setup», la conexión tarda unas
                    # décimas en completarse en el aire; mientras tanto el
                    # kernel contesta ENOTCONN. Es arranque, no avería.
                    if e.errno == errno.ENOTCONN and espera < 5.0:
                        espera += 0.02
                        await asyncio.sleep(0.02)
                        continue
                    raise
                enviados += 1
                if enviados == 1:
                    logger.info(
                        f"Cebador: transporte SCO arriba tras {espera:.2f} s; mandando silencio"
                    )
                elif enviados % 1000 == 0:
                    logger.info(f"Cebador: {enviados} paquetes de silencio enviados")
                await asyncio.sleep(SEGUNDOS_ENTRE_CEBOS)
        except OSError as e:
            logger.warning(f"Cebador muerto tras {enviados} paquetes: {e}")

    async def parar(self) -> None:
        """Cancela ecos, aceptador y consumidor, para un apagado limpio."""
        for tarea in list(self._tareas):
            tarea.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tarea
        self._tareas.clear()
        if self._aceptador is not None:
            self._aceptador.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._aceptador
            self._aceptador = None
        for sock in (self._consumidor, self._escucha):
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
        self._consumidor = self._escucha = None


def mtu_de(sock: socket.socket) -> int:
    """Pregunta al kernel el MTU del SCO, con un valor de respaldo razonable.

    Escribir en bloques que no sean exactamente el MTU hace que el kernel
    descarte paquetes **en silencio**, así que este número no es decorativo.
    El respaldo cubre los tests (un socketpair unix no entiende `SOL_SCO`) y
    cualquier kernel raro; 48 es el MTU clásico de SCO por USB con CVSD.
    """
    try:
        opciones = sock.getsockopt(SOL_SCO, SCO_OPTIONS, 2)
        mtu = int.from_bytes(opciones[:2], "little")
        if mtu > 0:
            return mtu
    except OSError:
        pass
    return MTU_POR_DEFECTO


__all__ = [
    "CODEC_CVSD",
    "CODEC_MSBC",
    "INTERFAZ_AGENTE",
    "INTERFAZ_MANAGER",
    "MODO_AGENTE",
    "MODO_ECO",
    "MODO_OFF",
    "MTU_POR_DEFECTO",
    "RUTA_AGENTE",
    "AudioSCO",
    "mtu_de",
]
