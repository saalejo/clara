"""Descarga de la agenda del móvil por PBAP, sobre obexd.

PBAP (*Phone Book Access Profile*) es el perfil con el que un manos libres pide
la agenda al móvil. Quien lo implementa aquí es **obexd**, que vive en el bus de
**sesión** con el nombre `org.bluez.obex`; este módulo solo lo conduce.

## La trampa que cuesta una tarde

obexd ata la vida de una sesión PBAP **al dueño del nombre de D-Bus que la
creó**. En cuanto esa conexión se cierra, la sesión se destruye. Con `busctl`
desde el shell cada comando es una conexión distinta, así que la sesión muere
antes del comando siguiente y `Select` falla con:

    Method "Select" ... doesn't exist

que suena a versión equivocada de la interfaz y no lo es. En el log de
`obexd -d` se ve el verdadero motivo:

    session.c:owner_disconnected()
    session.c:obc_session_shutdown()

Por eso todo esto ocurre sobre **una sola conexión persistente**, la del puente,
y por eso PBAP no se puede depurar a base de `busctl`.

## La otra trampa: `PullAll` vuelve antes de tiempo

`PullAll` devuelve inmediatamente una ruta de transferencia; el fichero no está
completo hasta que la propiedad `Status` de `org.bluez.obex.Transfer1` llega a
`complete`. Y hay carrera: en una agenda pequeña la transferencia puede terminar
antes de que dé tiempo a suscribirse, así que **hay que suscribirse a
`PropertiesChanged` antes de llamar** y, aun así, comprobar el estado actual
después de suscribirse.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from loguru import logger

from voice_agent_core.telefonia import Contacto, ResultadoSincronizacion
from voice_agent_telefonia.vcard import analizar_vcards

SERVICIO_OBEX = "org.bluez.obex"
RUTA_CLIENTE = "/org/bluez/obex"
IFAZ_CLIENTE = "org.bluez.obex.Client1"
IFAZ_AGENDA = "org.bluez.obex.PhonebookAccess1"
IFAZ_TRANSFERENCIA = "org.bluez.obex.Transfer1"
IFAZ_PROPIEDADES = "org.freedesktop.DBus.Properties"

#: Solo estos campos. No es cosmético: sin `Fields` el móvil manda también las
#: FOTOS de los contactos, que son el 95 % del peso, y una agenda de 300
#: personas pasa de unos segundos a varios minutos por RFCOMM.
CAMPOS = ["N", "FN", "TEL"]

#: Tope de espera de una descarga. Una agenda grande por RFCOMM es lenta, pero
#: si a los dos minutos no ha terminado es que algo se ha quedado colgado.
TIMEOUT_DESCARGA = 120.0


class ErrorPBAP(Exception):
    """No se pudo descargar la agenda."""


def _comprobar(respuesta: Message | None, que: str) -> Message:
    """Convierte un error de D-Bus en una excepción con un mensaje legible.

    `MessageBus.call` está tipado como `Message | None` porque devuelve `None`
    para los mensajes que no esperan respuesta. Aquí todas la esperan, así que
    un `None` es un fallo de verdad y se trata como tal.
    """
    if respuesta is None:
        raise ErrorPBAP(f"{que}: D-Bus no ha contestado")
    if respuesta.message_type is MessageType.ERROR:
        detalle = respuesta.body[0] if respuesta.body else respuesta.error_name
        raise ErrorPBAP(f"{que}: {detalle}")
    return respuesta


class DescargaPBAP:
    """Una descarga de la agenda, de principio a fin, sobre una conexión viva.

    Se usa como gestor de contexto asíncrono para que la sesión se cierre
    siempre, incluso si algo falla a mitad: una sesión abierta mantiene ocupado
    un canal RFCOMM contra el móvil.
    """

    def __init__(self, bus: MessageBus, direccion: str) -> None:
        """Prepara una descarga contra el móvil indicado.

        Args:
            bus: Conexión **persistente** al bus de sesión. Ver el docstring
                del módulo: si se cierra, obexd destruye la sesión.
            direccion: MAC del móvil, en formato AA:BB:CC:DD:EE:FF.
        """
        self.bus = bus
        self.direccion = direccion
        self.sesion: str | None = None

    async def _esperar_a_obexd(self, intentos: int = 10) -> None:
        """Espera a que obexd esté activado y con su interfaz ya registrada.

        obexd se activa por D-Bus: la primera llamada lo arranca. Pero el
        nombre `org.bluez.obex` aparece en el bus **antes** de que el objeto
        `/org/bluez/obex` tenga registrada la interfaz `Client1`, así que esa
        primera llamada se contesta con:

            Method "CreateSession" ... doesn't exist

        que parece un problema de versión de la interfaz y es solo una carrera:
        repetida un segundo después, funciona. Medido en la placa.
        """
        for intento in range(intentos):
            respuesta = await self.bus.call(
                Message(
                    destination=SERVICIO_OBEX,
                    path=RUTA_CLIENTE,
                    interface="org.freedesktop.DBus.Introspectable",
                    member="Introspect",
                )
            )
            if (
                respuesta is not None
                and respuesta.message_type is not MessageType.ERROR
                and IFAZ_CLIENTE in respuesta.body[0]
            ):
                return
            if intento == 0:
                logger.debug("obexd aún no está listo; esperando a que registre Client1...")
            await asyncio.sleep(0.3)
        raise ErrorPBAP("obexd no llegó a registrar org.bluez.obex.Client1")

    async def __aenter__(self) -> DescargaPBAP:
        """Abre la sesión PBAP contra el móvil."""
        await self._esperar_a_obexd()
        respuesta = _comprobar(
            await self.bus.call(
                Message(
                    destination=SERVICIO_OBEX,
                    path=RUTA_CLIENTE,
                    interface=IFAZ_CLIENTE,
                    member="CreateSession",
                    signature="sa{sv}",
                    body=[self.direccion, {"Target": Variant("s", "pbap")}],
                )
            ),
            "no se pudo abrir la sesión PBAP",
        )
        self.sesion = respuesta.body[0]
        logger.debug(f"Sesión PBAP abierta: {self.sesion}")
        return self

    async def __aexit__(self, *_: object) -> None:
        """Cierra la sesión, pase lo que pase."""
        if self.sesion is None:
            return
        with contextlib.suppress(Exception):
            await self.bus.call(
                Message(
                    destination=SERVICIO_OBEX,
                    path=RUTA_CLIENTE,
                    interface=IFAZ_CLIENTE,
                    member="RemoveSession",
                    signature="o",
                    body=[self.sesion],
                )
            )
        logger.debug("Sesión PBAP cerrada")
        self.sesion = None

    async def _llamar_agenda(self, metodo: str, firma: str, cuerpo: list[Any]) -> Message | None:
        assert self.sesion is not None
        return await self.bus.call(
            Message(
                destination=SERVICIO_OBEX,
                path=self.sesion,
                interface=IFAZ_AGENDA,
                member=metodo,
                signature=firma,
                body=cuerpo,
            )
        )

    async def seleccionar(self, almacen: str = "int", agenda: str = "pb") -> None:
        """Elige qué libreta se va a leer.

        Args:
            almacen: `int` (memoria del móvil) o `sim1`.
            agenda: `pb` contactos, `ich` entrantes, `och` salientes,
                `mch` perdidas, `cch` combinado, `fav` favoritos.
        """
        _comprobar(await self._llamar_agenda("Select", "ss", [almacen, agenda]), "Select falló")

    async def cuantos(self) -> int:
        """Pregunta cuántas entradas tiene la libreta seleccionada.

        Es barato y sirve de diagnóstico: un cero aquí significa casi siempre
        que el Android tiene apagado el permiso de compartir contactos, no que
        la agenda esté vacía.
        """
        respuesta = _comprobar(await self._llamar_agenda("GetSize", "", []), "GetSize falló")
        return int(respuesta.body[0])

    async def descargar(self, destino: Path) -> None:
        """Trae la libreta entera a un fichero y espera a que termine de verdad.

        Args:
            destino: Fichero donde obexd escribirá las vCard.

        Raises:
            ErrorPBAP: Si la transferencia falla o no termina a tiempo.
        """
        assert self.sesion is not None
        destino.parent.mkdir(parents=True, exist_ok=True)

        terminada: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        ruta_transferencia: str | None = None

        def al_cambiar(mensaje: Message) -> None:
            # Llegan cambios de todos los objetos de obexd; solo interesa el de
            # nuestra transferencia, y puede que aún no sepamos cuál es.
            if mensaje.interface != IFAZ_PROPIEDADES or mensaje.member != "PropertiesChanged":
                return
            if ruta_transferencia is None or mensaje.path != ruta_transferencia:
                return
            cambios = mensaje.body[1]
            estado = cambios.get("Status")
            if estado is None or terminada.done():
                return
            valor = estado.value if isinstance(estado, Variant) else estado
            if valor in ("complete", "error"):
                terminada.set_result(str(valor))

        # Suscribirse ANTES de PullAll: en una agenda pequeña la transferencia
        # puede acabar antes de que diera tiempo a hacerlo después.
        self.bus.add_message_handler(al_cambiar)
        try:
            _comprobar(
                await self.bus.call(
                    Message(
                        destination="org.freedesktop.DBus",
                        path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus",
                        member="AddMatch",
                        signature="s",
                        body=[
                            f"type='signal',interface='{IFAZ_PROPIEDADES}',member='PropertiesChanged'"
                        ],
                    )
                ),
                "no se pudo escuchar el progreso de la transferencia",
            )

            respuesta = _comprobar(
                await self._llamar_agenda(
                    "PullAll",
                    "sa{sv}",
                    [
                        str(destino),
                        {
                            "Format": Variant("s", "vcard30"),
                            "Fields": Variant("as", CAMPOS),
                        },
                    ],
                ),
                "PullAll falló",
            )
            ruta_transferencia = respuesta.body[0]

            # Y aun así, comprobar el estado actual: puede haber terminado entre
            # el PullAll y el momento en que supimos qué ruta vigilar.
            estado_actual = await self._estado_de(ruta_transferencia)
            if estado_actual in ("complete", "error") and not terminada.done():
                terminada.set_result(estado_actual)

            try:
                final = await asyncio.wait_for(terminada, timeout=TIMEOUT_DESCARGA)
            except TimeoutError as e:
                raise ErrorPBAP(
                    f"la descarga de la agenda no terminó en {TIMEOUT_DESCARGA:.0f} s"
                ) from e

            if final != "complete":
                raise ErrorPBAP("el móvil abortó la transferencia de la agenda")
        finally:
            self.bus.remove_message_handler(al_cambiar)

    async def _estado_de(self, ruta: str) -> str:
        """Lee la propiedad `Status` de una transferencia.

        Si la transferencia ya no existe es porque obexd la ha retirado al
        terminar, que es un final feliz y no un error.
        """
        respuesta = await self.bus.call(
            Message(
                destination=SERVICIO_OBEX,
                path=ruta,
                interface=IFAZ_PROPIEDADES,
                member="Get",
                signature="ss",
                body=[IFAZ_TRANSFERENCIA, "Status"],
            )
        )
        if respuesta is None or respuesta.message_type is MessageType.ERROR:
            # Que la transferencia ya no exista es un final feliz: obexd la
            # retira en cuanto termina.
            return "complete"
        valor = respuesta.body[0]
        return str(valor.value if isinstance(valor, Variant) else valor)


async def descargar_agenda(
    bus: MessageBus, direccion: str, destino: Path
) -> tuple[list[Contacto], ResultadoSincronizacion]:
    """Descarga la agenda del móvil y la convierte en contactos.

    Args:
        bus: Conexión persistente al bus de sesión.
        direccion: MAC del móvil emparejado.
        destino: Dónde dejar el .vcf descargado. Se conserva a propósito: es lo
            primero que hay que mirar cuando un contacto no aparece.

    Returns:
        Los contactos y un resumen de cómo fue.
    """
    empezado = time.perf_counter()
    try:
        async with DescargaPBAP(bus, direccion) as descarga:
            await descarga.seleccionar()
            total = await descarga.cuantos()
            logger.info(f"El móvil declara {total} contactos; descargando...")
            if total == 0:
                # Casi siempre es el permiso de Android, no una agenda vacía.
                return [], ResultadoSincronizacion(
                    ok=True,
                    contactos=0,
                    segundos=time.perf_counter() - empezado,
                    detalle=(
                        "El móvil dice tener 0 contactos. Normalmente significa que está "
                        "apagado el permiso de compartir contactos y registro de llamadas "
                        "en los ajustes Bluetooth del teléfono."
                    ),
                )
            await descarga.descargar(destino)
    except ErrorPBAP as e:
        logger.warning(f"No se pudo descargar la agenda: {e}")
        return [], ResultadoSincronizacion(
            ok=False, segundos=time.perf_counter() - empezado, detalle=str(e)
        )

    contactos = analizar_vcards(destino.read_text(encoding="utf-8", errors="replace"))
    segundos = time.perf_counter() - empezado
    logger.info(f"Agenda descargada: {len(contactos)} contactos en {segundos:.1f} s")
    return contactos, ResultadoSincronizacion(
        ok=True,
        contactos=len(contactos),
        segundos=segundos,
        detalle=f"{len(contactos)} contactos",
    )


__all__ = ["CAMPOS", "DescargaPBAP", "ErrorPBAP", "descargar_agenda"]
