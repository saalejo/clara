"""Reconexión del móvil iniciada desde la placa.

Cuando el enlace Bluetooth se pierde porque el adaptador se reenumera —se
cambia de puerto, se pasa por un hub, la placa se reinicia—, el móvil **no
vuelve solo**: Android reintenta contra los dispositivos que él recuerda
cuando le apetece, y en la práctica el manos libres queda desconectado hasta
que alguien toca el teléfono. Medido en la placa: tres minutos de espera tras
reenumerar el UB500 y ni un intento del TECNO.

La solución es la de cualquier kit de coche: que sea el manos libres quien
llame a la puerta. `Device1.Connect` de BlueZ levanta todos los perfiles
—incluido HFP, con lo que oFono crea el módem y el puente lo ve por su
vigilancia normal— y si el móvil no está al alcance falla con un error
corriente que se ignora. Probado a mano antes de escribir esto:
`bluetoothctl connect <MAC>` reconectó el TECNO al instante, con el módem
en línea a los cinco segundos.

El candidato se elige con los mismos criterios que usaría una persona:
emparejado, de confianza, y que anuncie el perfil de pasarela de audio (HFP
AG), que es lo que distingue un teléfono de unos auriculares.
"""

from __future__ import annotations

import asyncio
from typing import Any

from dbus_fast.aio import MessageBus
from loguru import logger

from voice_agent_telefonia.bus import ErrorBus, llamar

#: El perfil que anuncia un teléfono: pasarela de audio de HFP (Audio Gateway).
#: Unos auriculares o un altavoz anuncian el lado contrario (0000111e) y no
#: sirven de teléfono por mucho que estén emparejados.
UUID_HFP_AG = "0000111f-0000-1000-8000-00805f9b34fb"

#: Tope para un intento de conexión. El timeout de paginación de la radio son
#: unos diez segundos; esto solo protege de que un BlueZ colgado congele la
#: vigilancia del puente.
TIMEOUT_CONEXION_SECS = 25.0


def elegir_candidatos(
    objetos: dict[str, dict[str, Any]], direccion_preferida: str = ""
) -> list[tuple[str, str]] | None:
    """Filtra qué dispositivos de BlueZ merecen un intento de conexión.

    Args:
        objetos: Lo que devuelve `GetManagedObjects` de BlueZ, ya desenvuelto.
        direccion_preferida: MAC del móvil, si hay más de uno emparejado.

    Returns:
        Pares `(ruta, nombre)` de los teléfonos emparejados y de confianza que
        están desconectados, o `None` si alguno ya está conectado — que
        significa que no hay nada que hacer, y distinguirlo evita que el puente
        llame a `Connect` sobre un enlace vivo.
    """
    candidatos: list[tuple[str, str]] = []
    for ruta, interfaces in objetos.items():
        dispositivo = interfaces.get("org.bluez.Device1")
        if not dispositivo:
            continue
        if not dispositivo.get("Paired") or not dispositivo.get("Trusted"):
            continue
        uuids = [u.lower() for u in dispositivo.get("UUIDs", [])]
        if UUID_HFP_AG not in uuids:
            continue
        direccion = str(dispositivo.get("Address", ""))
        if direccion_preferida and direccion.upper() != direccion_preferida.upper():
            continue
        if dispositivo.get("Connected"):
            return None
        candidatos.append((ruta, str(dispositivo.get("Alias") or direccion)))
    return candidatos


async def reconectar_movil(bus: MessageBus, direccion_preferida: str = "") -> str | None:
    """Intenta conectar el móvil emparejado, si no lo está ya.

    Args:
        bus: Conexión al bus del sistema.
        direccion_preferida: MAC del móvil, si hay más de uno emparejado.

    Returns:
        El nombre del móvil si la conexión se estableció, o `None` si ya estaba
        conectado, no hay candidato o ninguno contestó — todo lo cual es normal
        y no merece más que un log de depuración.
    """
    try:
        objetos = await llamar(
            bus,
            destino="org.bluez",
            ruta="/",
            interfaz="org.freedesktop.DBus.ObjectManager",
            metodo="GetManagedObjects",
        )
    except ErrorBus as e:
        logger.debug(f"BlueZ no contesta; sin reconexión este ciclo: {e}")
        return None

    candidatos = elegir_candidatos(dict(objetos or {}), direccion_preferida)
    if candidatos is None or not candidatos:
        return None

    for ruta, nombre in candidatos:
        try:
            await asyncio.wait_for(
                llamar(
                    bus,
                    destino="org.bluez",
                    ruta=ruta,
                    interfaz="org.bluez.Device1",
                    metodo="Connect",
                ),
                timeout=TIMEOUT_CONEXION_SECS,
            )
        except (ErrorBus, TimeoutError):
            # Fuera de alcance o con el Bluetooth apagado: el caso normal de
            # un reintento. Se prueba el siguiente candidato, si lo hay.
            logger.debug(f"{nombre} no contesta a la conexión")
            continue
        return nombre
    return None


__all__ = ["TIMEOUT_CONEXION_SECS", "UUID_HFP_AG", "elegir_candidatos", "reconectar_movil"]
