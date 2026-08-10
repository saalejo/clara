"""Envoltorio fino sobre dbus-fast: llamadas, señales y desempaquetado.

Se habla con D-Bus a base de `Message` en crudo y no con los proxies
introspectados de dbus-fast. Es una decisión deliberada: los proxies obligan a
introspeccionar cada objeto al crearlos, lo que añade una ida y vuelta por
llamada y, peor, falla de formas raras cuando el objeto aparece y desaparece —
que es exactamente lo que hace un módem HFP cada vez que el móvil se aleja.
Con mensajes crudos, un objeto que ya no está devuelve un error normal que se
puede tratar.

Los dos buses no son intercambiables:

* **Sistema** (`org.ofono`, `org.bluez`): las llamadas y el estado del móvil.
  Su política deniega por defecto; ver el drop-in de `/etc/dbus-1/system.d/`.
* **Sesión** (`org.bluez.obex`): la agenda por PBAP. obexd es un servicio de
  usuario y se activa por D-Bus.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from loguru import logger

PROPIEDADES = "org.freedesktop.DBus.Properties"


class ErrorBus(Exception):
    """Una llamada a D-Bus falló, o el servicio no está disponible."""


def desenvolver(valor: Any) -> Any:
    """Quita las `Variant` de una respuesta de D-Bus, recursivamente.

    D-Bus envuelve en `Variant` todo lo que va dentro de un diccionario `a{sv}`,
    que es la forma en que oFono y BlueZ devuelven casi todas sus propiedades.
    Trabajar con eso a mano llena el código de `.value` y de comprobaciones; se
    hace una vez aquí y el resto del paquete ve diccionarios normales de Python.
    """
    if isinstance(valor, Variant):
        return desenvolver(valor.value)
    if isinstance(valor, dict):
        return {k: desenvolver(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [desenvolver(v) for v in valor]
    return valor


async def conectar(tipo: BusType, *, con_fds: bool = False) -> MessageBus:
    """Abre una conexión al bus indicado.

    Args:
        tipo: Bus del sistema o de sesión.
        con_fds: Negociar el paso de descriptores de fichero. Hace falta en el
            bus del sistema para que oFono pueda entregar el socket SCO del
            audio de una llamada; sin negociarlo, dbus-fast descarta el
            descriptor sin avisar.

    Raises:
        ErrorBus: Si no se puede conectar.
    """
    try:
        return await MessageBus(bus_type=tipo, negotiate_unix_fd=con_fds).connect()
    except Exception as e:
        raise ErrorBus(f"no se pudo conectar al bus {tipo.name.lower()}: {e}") from e


async def llamar(
    bus: MessageBus,
    *,
    destino: str,
    ruta: str,
    interfaz: str,
    metodo: str,
    firma: str = "",
    cuerpo: list[Any] | None = None,
) -> Any:
    """Hace una llamada a un método y devuelve su resultado ya desenvuelto.

    Returns:
        El primer valor devuelto, o `None` si el método no devuelve nada.

    Raises:
        ErrorBus: Si D-Bus responde con un error.
    """
    respuesta = await bus.call(
        Message(
            destination=destino,
            path=ruta,
            interface=interfaz,
            member=metodo,
            signature=firma,
            body=cuerpo or [],
        )
    )
    if respuesta is None:
        return None
    if respuesta.message_type is MessageType.ERROR:
        detalle = respuesta.body[0] if respuesta.body else ""
        raise ErrorBus(f"{interfaz}.{metodo} falló: {respuesta.error_name}: {detalle}".strip())
    return desenvolver(respuesta.body[0]) if respuesta.body else None


async def escuchar(
    bus: MessageBus,
    manejador: Callable[[Message], None],
    *,
    interfaces: Sequence[str],
) -> None:
    """Se suscribe a varias señales y registra **un solo** manejador.

    Las dos mitades de esto son independientes y conviene no confundirlas:

    * `AddMatch` le dice al bus qué señales queremos que nos llegue. Va una por
      interfaz.
    * `add_message_handler` registra una función a la que dbus-fast le pasa
      **todos** los mensajes que lleguen, sin filtrar. El manejador tiene que
      comprobar `interface` y `member` por su cuenta.

    Por eso el manejador se registra **una vez** aunque haya varias reglas.
    Registrarlo una vez por interfaz —que es lo natural si esta función acepta
    una sola— hace que dbus-fast lo llame N veces por cada mensaje. Medido en la
    placa con cuatro suscripciones: cada señal de oFono se procesaba cuatro
    veces y se creaban cuatro tareas para releer las llamadas. No daba error,
    solo trabajo de más y logs cuadruplicados.
    """
    for interfaz in interfaces:
        regla = f"type='signal',interface='{interfaz}'"
        await llamar(
            bus,
            destino="org.freedesktop.DBus",
            ruta="/org/freedesktop/DBus",
            interfaz="org.freedesktop.DBus",
            metodo="AddMatch",
            firma="s",
            cuerpo=[regla],
        )
        logger.debug(f"Escuchando señales: {regla}")
    bus.add_message_handler(manejador)


async def propiedades(bus: MessageBus, destino: str, ruta: str, interfaz: str) -> dict[str, Any]:
    """Lee todas las propiedades de una interfaz."""
    resultado = await llamar(
        bus,
        destino=destino,
        ruta=ruta,
        interfaz=PROPIEDADES,
        metodo="GetAll",
        firma="s",
        cuerpo=[interfaz],
    )
    return dict(resultado or {})


__all__ = [
    "PROPIEDADES",
    "ErrorBus",
    "conectar",
    "desenvolver",
    "escuchar",
    "llamar",
    "propiedades",
]
