"""El agente de audio SCO: registro prudente, descriptores y eco fiel.

Lo que se fija aquí es el contrato de la primera etapa de la fase 2: que en
modo `off` no se toque nada — ni un registro, ni un manejador —, que el
descriptor de `NewConnection` se recoja del mensaje como manda dbus-fast (el
cuerpo trae el ÍNDICE, el descriptor va en `unix_fds`), y que el eco devuelva
paquete a paquete exactamente lo que entró.

El SCO se sustituye por un `socketpair` de tipo `SOCK_SEQPACKET`, que conserva
la propiedad que importa —un `recv` por paquete— sin necesitar Bluetooth.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

from dbus_fast import Message, MessageType

from voice_agent_telefonia.audio_sco import (
    CODEC_CVSD,
    INTERFAZ_AGENTE,
    MODO_ECO,
    MTU_POR_DEFECTO,
    RUTA_AGENTE,
    AudioSCO,
    mtu_de,
)

SILENCIO = bytes(48)


async def _recibir_audio(sock: socket.socket) -> bytes:
    """El primer paquete que NO sea el silencio del cebador.

    Hasta que el eco recibe algo, el puente manda silencio para despertar la
    recepción del SCO; en un socketpair ese silencio llega igual y hay que
    saltárselo para ver el eco de verdad.
    """
    bucle = asyncio.get_running_loop()
    for _ in range(1000):
        paquete = await asyncio.wait_for(bucle.sock_recv(sock, 4096), timeout=2)
        if paquete != SILENCIO:
            return paquete
    raise AssertionError("solo llegó silencio")


def _mensaje(member: str, body: list[object], unix_fds: list[int] | None = None) -> Message:
    """Un método de oFono hacia nuestro agente, como llegaría por el bus."""
    firmas = {"NewConnection": "ohy", "Release": ""}
    mensaje = Message(
        message_type=MessageType.METHOD_CALL,
        destination="unused.destino",
        path=RUTA_AGENTE,
        interface=INTERFAZ_AGENTE,
        member=member,
        signature=firmas.get(member, ""),
        body=body,
        unix_fds=unix_fds or [],
    )
    # El serial lo pone el bus al enviar; aquí no hay bus y la respuesta lo
    # necesita para `reply_serial`.
    mensaje.serial = 1
    return mensaje


class TestModoOff:
    def test_no_esta_activo(self) -> None:
        assert not AudioSCO().activo

    async def test_no_registra_nada(self) -> None:
        """En `off` no debe tocarse el bus: pasarle `None` no puede fallar."""
        agente = AudioSCO()
        await agente.asegurar_registro(None)
        assert not agente.registrado


class TestNewConnection:
    async def test_recoge_el_descriptor_por_su_indice(self) -> None:
        """El cuerpo trae `0`; el descriptor de verdad viaja en `unix_fds`."""
        agente = AudioSCO(MODO_ECO)
        nuestro, suyo = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            respuesta = agente._manejar(
                _mensaje("NewConnection", ["/card_0", 0, CODEC_CVSD], [suyo.fileno()])
            )

            assert respuesta is not None
            assert respuesta.message_type is MessageType.METHOD_RETURN
            # El eco quedó en marcha sobre un DUPLICADO del descriptor: lo que
            # se mande por nuestro extremo tiene que volver tal cual.
            nuestro.setblocking(False)
            bucle = asyncio.get_running_loop()
            paquete = bytes(range(1, 49))
            await bucle.sock_sendall(nuestro, paquete)
            assert await _recibir_audio(nuestro) == paquete
        finally:
            nuestro.close()
            suyo.close()
            await agente.parar()

    async def test_el_eco_devuelve_paquete_a_paquete(self) -> None:
        """Dos paquetes distintos vuelven como dos paquetes, no pegados."""
        agente = AudioSCO(MODO_ECO)
        nuestro, suyo = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            agente._manejar(_mensaje("NewConnection", ["/card_0", 0, CODEC_CVSD], [suyo.fileno()]))
            nuestro.setblocking(False)
            bucle = asyncio.get_running_loop()

            await bucle.sock_sendall(nuestro, b"a" * 48)
            await bucle.sock_sendall(nuestro, b"b" * 48)

            # Tras el primer paquete de verdad el cebador ya está cancelado,
            # así que el segundo tiene que llegar limpio, sin silencio entre
            # medias.
            assert await _recibir_audio(nuestro) == b"a" * 48
            assert await asyncio.wait_for(bucle.sock_recv(nuestro, 4096), timeout=2) == b"b" * 48
        finally:
            nuestro.close()
            suyo.close()
            await agente.parar()

    async def test_el_eco_muere_cuando_el_canal_se_cierra(self) -> None:
        """La llamada termina, el canal se cierra, y la tarea no queda huérfana."""
        agente = AudioSCO(MODO_ECO)
        nuestro, suyo = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        agente._manejar(_mensaje("NewConnection", ["/card_0", 0, CODEC_CVSD], [suyo.fileno()]))
        suyo.close()
        nuestro.close()

        for _ in range(200):
            if not agente._tareas:
                break
            await asyncio.sleep(0.01)
        assert not agente._tareas

    async def test_antes_del_primer_paquete_llega_silencio(self) -> None:
        """El cebador existe por una medida: sin transmitir, el SCO por USB no
        entrega nada. Hasta el primer paquete recibido tiene que salir
        silencio."""
        agente = AudioSCO(MODO_ECO)
        nuestro, suyo = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            agente._manejar(_mensaje("NewConnection", ["/card_0", 0, CODEC_CVSD], [suyo.fileno()]))
            nuestro.setblocking(False)
            bucle = asyncio.get_running_loop()

            assert await asyncio.wait_for(bucle.sock_recv(nuestro, 4096), timeout=2) == SILENCIO
        finally:
            nuestro.close()
            suyo.close()
            await agente.parar()

    async def test_un_indice_sin_descriptor_es_un_error_y_no_un_crash(self) -> None:
        """Si el descriptor no llegó, se contesta con un error de D-Bus."""
        agente = AudioSCO(MODO_ECO)
        respuesta = agente._manejar(_mensaje("NewConnection", ["/card_0", 0, CODEC_CVSD], []))

        assert respuesta is not None
        assert respuesta.message_type is MessageType.ERROR


class TestModoAgente:
    async def test_entrega_el_descriptor_con_metadatos(self, tmp_path: Path) -> None:
        """El consumidor recibe una línea de JSON y el SCO por `SCM_RIGHTS`."""
        import json

        from voice_agent_telefonia.audio_sco import MODO_AGENTE

        agente = AudioSCO(MODO_AGENTE)
        ruta = tmp_path / "audio.sock"
        await agente.arrancar(ruta)
        consumidor = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        nuestro, suyo = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            consumidor.connect(str(ruta))
            await asyncio.sleep(0.05)  # que el aceptador registre la conexión
            assert agente.activo  # con consumidor, el modo agente quiere registrarse

            agente._manejar(_mensaje("NewConnection", ["/card_0", 0, CODEC_CVSD], [suyo.fileno()]))

            datos, fds, _flags, _addr = socket.recv_fds(consumidor, 4096, 4)
            metadatos = json.loads(datos.decode())
            assert metadatos["tarjeta"] == "/card_0"
            assert metadatos["codec"] == CODEC_CVSD
            assert len(fds) == 1
            # El descriptor recibido es el MISMO canal: lo que se escriba por
            # nuestro extremo sale por él.
            nuestro.sendall(b"z" * 48)
            recibido = socket.socket(fileno=fds[0])
            recibido.settimeout(2)
            assert recibido.recv(4096) == b"z" * 48
            recibido.close()
        finally:
            consumidor.close()
            nuestro.close()
            suyo.close()
            await agente.parar()

    async def test_sin_consumidor_rechaza_y_no_se_registra(self, tmp_path: Path) -> None:
        """Sin agente de voz: ni registro en oFono, y el SCO que llegara se
        cierra sin confirmar — el móvil se queda el audio."""
        from voice_agent_telefonia.audio_sco import MODO_AGENTE

        agente = AudioSCO(MODO_AGENTE)
        await agente.arrancar(tmp_path / "audio.sock")
        nuestro, suyo = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            assert not agente.activo

            fd = suyo.fileno()
            respuesta = agente._manejar(_mensaje("NewConnection", ["/card_0", 0, CODEC_CVSD], [fd]))

            assert respuesta is not None
            assert respuesta.message_type is MessageType.METHOD_RETURN
            # El duplicado se cerró: nuestro extremo ve el EOF en cuanto
            # cierre el original.
            suyo.close()
            nuestro.settimeout(2)
            assert nuestro.recv(16) == b""
        finally:
            nuestro.close()
            await agente.parar()


class TestRelease:
    async def test_marca_el_registro_como_muerto(self) -> None:
        agente = AudioSCO(MODO_ECO)
        agente._dueno_ofono = ":1.7"

        respuesta = agente._manejar(_mensaje("Release", []))

        assert respuesta is not None
        assert respuesta.message_type is MessageType.METHOD_RETURN
        assert not agente.registrado


class TestAjenos:
    def test_ignora_mensajes_que_no_son_para_el(self) -> None:
        """El manejador ve TODO el tráfico del bus; solo puede tocar lo suyo."""
        agente = AudioSCO(MODO_ECO)
        ajeno = Message(
            message_type=MessageType.METHOD_CALL,
            destination="unused.destino",
            path="/otra/cosa",
            interface="org.otra.Interfaz",
            member="NewConnection",
        )
        assert agente._manejar(ajeno) is None


class TestMTU:
    def test_un_socket_sin_sol_sco_usa_el_respaldo(self) -> None:
        """Un socketpair unix no entiende `SOL_SCO`: el respaldo es la salida."""
        nuestro, suyo = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            assert mtu_de(nuestro) == MTU_POR_DEFECTO
        finally:
            nuestro.close()
            suyo.close()
