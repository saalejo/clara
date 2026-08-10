"""El núcleo SCO del agente: reloj de recepción, contrapresión y cliente.

La propiedad central que se fija aquí es la que resuelve la deriva de reloj:
**la salida va esclava de la recepción** — por cada paquete que entra sale
exactamente uno, del mismo tamaño, con audio encolado si lo hay y silencio si
no. Un TTS más rápido que el tiempo real no adelanta nada: espera en la cola,
y la cola tiene tope.

Como en el resto de la telefonía, el SCO se sustituye por un `socketpair` de
tipo `SOCK_SEQPACKET`: un `recv` por paquete, sin Bluetooth.
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

from voice_agent.telefonia_audio import (
    MAXIMO_COLA_SALIDA,
    ClienteAudioSCO,
    NucleoSCO,
)

SILENCIO = bytes(48)


async def _paquete_util(sock: socket.socket) -> bytes:
    """El primer paquete que no sea silencio (del cebador o de la cola vacía)."""
    bucle = asyncio.get_running_loop()
    for _ in range(1000):
        paquete = await asyncio.wait_for(bucle.sock_recv(sock, 4096), timeout=2)
        if paquete != SILENCIO:
            return paquete
    raise AssertionError("solo llegó silencio")


class TestNucleo:
    async def test_la_salida_va_esclava_de_la_recepcion(self) -> None:
        """El audio encolado no sale hasta que entra un paquete, y sale en
        paquetes del tamaño del recibido."""
        nuestro, suyo = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        nuestro.setblocking(False)
        recibidos: list[bytes] = []

        async def apuntar(datos: bytes) -> None:
            recibidos.append(datos)

        nucleo = NucleoSCO(suyo, apuntar)
        nucleo.arrancar()
        bucle = asyncio.get_running_loop()
        try:
            await nucleo.escribir(b"x" * 96)

            await bucle.sock_sendall(nuestro, b"a" * 48)
            assert await _paquete_util(nuestro) == b"x" * 48

            await bucle.sock_sendall(nuestro, b"b" * 48)
            assert await _paquete_util(nuestro) == b"x" * 48

            # Cola vacía: el siguiente tique contesta silencio, no se calla.
            await bucle.sock_sendall(nuestro, b"c" * 48)
            assert await asyncio.wait_for(bucle.sock_recv(nuestro, 4096), timeout=2) == SILENCIO

            assert recibidos[:3] == [b"a" * 48, b"b" * 48, b"c" * 48]
        finally:
            await nucleo.parar()
            nuestro.close()

    async def test_escribir_espera_cuando_la_cola_esta_llena(self) -> None:
        """La contrapresión: con la cola al tope, `escribir` no vuelve hasta
        que la recepción drene."""
        nuestro, suyo = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        nuestro.setblocking(False)
        nucleo = NucleoSCO(suyo)
        nucleo.arrancar()
        bucle = asyncio.get_running_loop()
        try:
            await nucleo.escribir(bytes(MAXIMO_COLA_SALIDA))
            atascado = asyncio.create_task(nucleo.escribir(b"y" * 48))
            await asyncio.sleep(0.05)
            assert not atascado.done()

            # Un paquete entrante drena 48 B y abre hueco.
            await bucle.sock_sendall(nuestro, b"a" * 48)
            await asyncio.wait_for(atascado, timeout=2)
        finally:
            await nucleo.parar()
            nuestro.close()

    async def test_el_cierre_del_canal_enciende_la_senal(self) -> None:
        nuestro, suyo = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        nucleo = NucleoSCO(suyo)
        nucleo.arrancar()
        nuestro.close()

        await asyncio.wait_for(nucleo.cerrado.wait(), timeout=2)
        await nucleo.parar()


class TestCliente:
    async def test_recibe_el_descriptor_y_los_metadatos(self, tmp_path: Path) -> None:
        """Contra un puente falso: llega el JSON y un socket que funciona."""
        ruta = tmp_path / "audio.sock"
        puente = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        puente.bind(str(ruta))
        puente.listen(1)
        puente.setblocking(False)

        llegadas: list[tuple[socket.socket, dict[str, Any]]] = []
        llego = asyncio.Event()

        async def apuntar(sock: socket.socket, metadatos: dict[str, Any]) -> None:
            llegadas.append((sock, metadatos))
            llego.set()

        cliente = ClienteAudioSCO(ruta, apuntar)
        cliente.arrancar()
        bucle = asyncio.get_running_loop()
        nuestro, suyo = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            conexion, _ = await asyncio.wait_for(bucle.sock_accept(puente), timeout=2)
            metadatos = json.dumps({"tarjeta": "/card_9", "codec": 1, "mtu": 48}).encode() + b"\n"
            socket.send_fds(conexion, [metadatos], [suyo.fileno()])

            await asyncio.wait_for(llego.wait(), timeout=2)
            sco, recibidos = llegadas[0]
            assert recibidos["tarjeta"] == "/card_9"
            # El descriptor es el mismo canal: lo escrito por un extremo sale
            # por el otro.
            nuestro.sendall(b"hola")
            sco.settimeout(2)
            assert sco.recv(16) == b"hola"
            sco.close()
            conexion.close()
        finally:
            await cliente.parar()
            puente.close()
            nuestro.close()
            suyo.close()
