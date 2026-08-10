"""Sonda de HFP: le habla al móvil a pelo, sin oFono de por medio.

    sudo systemctl stop ofono
    make telefonia-sonda
    sudo systemctl start ofono

## Por qué existe

Contestar una llamada de WhatsApp desde la placa no funciona, y lo que hay
medido es que **oFono se niega**: su `src/voicecall.c` rechaza `Answer()` con
cualquier estado que no sea `incoming`, y este móvil entrega esas llamadas como
`dialing` o `alerting` y a los ~140 ms como `active`.

Pero esa es la política de **oFono**, no una limitación de HFP. En el protocolo,
descolgar es que la unidad de manos libres mande `ATA` por RFCOMM y el móvil
decida. Así que quedan dos preguntas sin responder, y las dos se contestan
mirando el cable:

1. **¿Qué manda el móvil de verdad?** oFono nos da su traducción, no el original.
   Aquí se ven los `+CIEV`, los `RING` y los `+CLIP` tal cual llegan.
2. **¿Aceptaría un `ATA`?** Se manda a mano y se mira la respuesta.

El dato que más pesa es `AT+CLCC`, que este móvil soporta (anuncia
`enhanced call status`). Devuelve por cada llamada:

    +CLCC: <idx>,<dir>,<stat>,<mode>,<mpty>[,<number>,<type>]

con `dir` 0=saliente 1=entrante, y `stat` 0=activa 1=retenida 2=marcando
3=sonando 4=**entrante** 5=en espera. Si con una llamada de WhatsApp dice
`dir=1,stat=4` mientras oFono la traduce como `alerting`, el móvil está
presentándola bien y el problema es enteramente de oFono.

## Es intrusiva: hay que parar oFono

El móvil acepta **una sola** conexión HFP. Mientras `ofonod` tenga la suya, esta
sonda no puede conectar. Por eso no para el servicio ella sola: pararlo es cosa
de `sudo` y del sistema, y una herramienta de diagnóstico que toquetea servicios
por su cuenta es peor que una que te pide que lo hagas tú.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
from datetime import UTC, datetime

from loguru import logger

#: Canal RFCOMM del `Handsfree Audio Gateway` del móvil. Se lee del SDP con
#: `sdptool browse --uuid 0x111f <MAC>`; en el TECNO POVA 5 Pro es el 3.
CANAL_POR_DEFECTO = 3

#: Lo que declaramos saber hacer, en el bitmap de `AT+BRSF`.
#:
#: * bit 2 (4)  — presentación del identificador de llamada, para que mande `+CLIP`
#: * bit 5 (32) — estado de llamada extendido, que es lo que habilita `AT+CLCC`
#:
#: Deliberadamente **no** se declara negociación de códec (bit 7): obligaría a
#: responder a `+BCS`, y aquí no se va a abrir ningún audio.
FUNCIONES_HF = 4 | 32

#: El saludo mínimo para que el móvil empiece a contar cosas. El orden importa:
#: hasta que no se manda `AT+CMER`, el móvil no emite un solo `+CIEV`.
SALUDO = (
    f"AT+BRSF={FUNCIONES_HF}",
    "AT+CIND=?",
    "AT+CIND?",
    "AT+CMER=3,0,0,1",
    "AT+CLIP=1",
)

#: Cada cuánto se pide la lista de llamadas mientras haya uno en marcha.
INTERVALO_CLCC = 1.0


def marca() -> str:
    """Devuelve la hora actual con milisegundos, para poder medir latencias."""
    return datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]


def pintar(direccion: str, texto: str) -> None:
    """Imprime una línea del diálogo, con la flecha en el sentido correcto."""
    print(f"{marca()}  {direccion} {texto}", flush=True)


#: Los `dir` y `stat` de `+CLCC`, según la especificación de HFP.
_DIRECCIONES = {"0": "SALIENTE", "1": "ENTRANTE"}
_ESTADOS = {
    "0": "activa",
    "1": "retenida",
    "2": "marcando",
    "3": "sonando",
    "4": "ENTRANTE (incoming)",
    "5": "en espera",
}


def explicar_clcc(linea: str) -> list[str]:
    """Traduce un `+CLCC` a algo legible. Es la respuesta que se viene a buscar.

    Args:
        linea: Una línea recibida del móvil, sea o no un `+CLCC`.

    Returns:
        Las explicaciones a pintar, vacío si la línea no es un `+CLCC` legible.
    """
    if not linea.startswith("+CLCC:"):
        return []
    campos = [c.strip() for c in linea.removeprefix("+CLCC:").strip().split(",")]
    if len(campos) < 3:
        return []
    direccion = _DIRECCIONES.get(campos[1], f"?{campos[1]}")
    estado = _ESTADOS.get(campos[2], f"?{campos[2]}")
    lineas = [f"llamada {campos[0]}: {direccion}, {estado}"]
    if campos[2] == "4":
        # El hallazgo que justifica toda la sonda: si el móvil la da por
        # `incoming`, oFono la está leyendo mal y un `ATA` debería descolgarla.
        lineas.append("*** el móvil la da por 'incoming': un ATA debería valer ***")
    return lineas


class Sonda:
    """La conversación AT con el móvil."""

    def __init__(self, mac: str, canal: int) -> None:
        """Prepara la sonda; no conecta hasta `arrancar`."""
        self.mac = mac
        self.canal = canal
        self._sock: socket.socket | None = None
        #: Se enciende con el primer indicio de llamada y apaga el sondeo de
        #: `AT+CLCC` cuando ya no hay ninguna, para no llenar la traza de ruido.
        self.hay_llamada = False

    async def conectar(self) -> None:
        """Abre el RFCOMM contra el AG del móvil.

        Raises:
            OSError: Si el móvil no acepta la conexión.
        """
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, (self.mac, self.canal))
        self._sock = sock
        pintar("--", f"conectado a {self.mac} canal {self.canal}")

    async def mandar(self, orden: str) -> None:
        r"""Manda una orden AT, terminada como manda el estándar (`\r`)."""
        if self._sock is None:
            return
        pintar("-->", orden)
        await asyncio.get_running_loop().sock_sendall(self._sock, f"{orden}\r".encode())

    async def leer(self) -> None:
        """Lee del socket hasta que se corte, pintando cada línea que llega."""
        loop = asyncio.get_running_loop()
        resto = ""
        while self._sock is not None:
            datos = await loop.sock_recv(self._sock, 1024)
            if not datos:
                pintar("--", "el móvil ha cerrado la conexión")
                return
            resto += datos.decode(errors="replace")
            # El AG separa con \r\n, pero no siempre manda las dos mitades de
            # golpe: hay que acumular y partir, no confiar en que cada recv
            # traiga líneas completas.
            partes = resto.replace("\r", "\n").split("\n")
            resto = partes.pop()
            for linea in (p.strip() for p in partes):
                if linea:
                    self._interpretar(linea)

    def _interpretar(self, linea: str) -> None:
        """Pinta una línea del móvil y anota si conviene sondear con `AT+CLCC`."""
        pintar("<--", linea)
        if linea.startswith(("RING", "+CLIP:", "+CIEV:")):
            self.hay_llamada = True
        for explicacion in explicar_clcc(linea):
            pintar("==", explicacion)

    async def sondear_llamadas(self) -> None:
        """Pide `AT+CLCC` mientras haya llamada, que es lo que revela la verdad."""
        while True:
            await asyncio.sleep(INTERVALO_CLCC)
            if self.hay_llamada:
                await self.mandar("AT+CLCC")

    async def leer_teclado(self) -> None:
        """Deja mandar órdenes a mano: `ATA` para descolgar, `AT+CHUP` para colgar."""
        loop = asyncio.get_running_loop()
        lector = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(lector), sys.stdin)
        while True:
            linea = (await lector.readline()).decode().strip()
            if not linea:
                continue
            if linea in ("q", "salir"):
                raise KeyboardInterrupt
            await self.mandar(linea)

    def cerrar(self) -> None:
        """Cierra el socket si sigue abierto."""
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None


async def sondear(mac: str, canal: int) -> int:
    """Conecta, saluda y se queda escuchando hasta Ctrl+C.

    Returns:
        El código de salida del proceso.
    """
    sonda = Sonda(mac, canal)
    try:
        await sonda.conectar()
    except OSError as e:
        logger.error(
            f"No he podido conectar con {mac} canal {canal}: {e}. "
            "¿Está oFono parado (`sudo systemctl stop ofono`) y el móvil cerca?"
        )
        return 1

    tareas = [
        asyncio.create_task(sonda.leer()),
        asyncio.create_task(sonda.sondear_llamadas()),
        asyncio.create_task(sonda.leer_teclado()),
    ]
    try:
        for orden in SALUDO:
            await sonda.mandar(orden)
            # Un respiro entre órdenes: encadenarlas sin esperar hace que algunos
            # AG contesten con ERROR a la segunda.
            await asyncio.sleep(0.3)
        print(
            "\nListo. Pide una llamada de WhatsApp.\n"
            "Escribe ATA para descolgar, AT+CHUP para colgar, q para salir.\n",
            flush=True,
        )
        await asyncio.gather(*tareas)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for tarea in tareas:
            tarea.cancel()
        sonda.cerrar()
    return 0


def main() -> int:
    """Punto de entrada de `python -m voice_agent_telefonia.sonda`."""
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{message}")

    mac = os.environ.get("TELEFONIA_BLUETOOTH_ADDRESS", "")
    if not mac:
        logger.error(
            "Hace falta la MAC del móvil en TELEFONIA_BLUETOOTH_ADDRESS. "
            "La lista `bluetoothctl devices Paired`."
        )
        return 2
    canal = int(os.environ.get("TELEFONIA_CANAL_HFP", str(CANAL_POR_DEFECTO)))
    try:
        return asyncio.run(sondear(mac, canal))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CANAL_POR_DEFECTO",
    "FUNCIONES_HF",
    "SALUDO",
    "Sonda",
    "explicar_clcc",
    "main",
    "sondear",
]
