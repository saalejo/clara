"""Arranque del puente de telefonía sobre su socket unix.

    uv run --package voice-agent-telefonia python -m voice_agent_telefonia

Se sirve por un socket unix bajo `<DATA_DIR>/run/`, que es lo que el contenedor
del agente ya tiene montado. Ver `api.py` para por qué un socket y no un puerto.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import uvicorn
from loguru import logger

from voice_agent_telefonia.api import crear_app
from voice_agent_telefonia.servicio import Servicio


def _directorio_datos() -> Path:
    """Resuelve DATA_DIR igual que lo hace el resto del proyecto."""
    return Path(os.environ.get("DATA_DIR", "data")).resolve()


def _ruta_socket(directorio: Path) -> Path:
    """Devuelve la ruta del socket, derivada de DATA_DIR.

    Se deriva y no se configura por dos motivos. Dentro del contenedor DATA_DIR
    es `/data` y en nativo es `<proyecto>/data`, así que la ruta correcta es
    distinta en cada sitio; y un valor por defecto fijo reproduciría la trampa
    que este proyecto ya pagó con DATA_DIR, cuyo síntoma es un servicio en
    marcha, logs limpios y nadie capaz de hablar con él.
    """
    return directorio / "run" / "telefonia.sock"


def _preparar_socket(ruta: Path) -> None:
    """Deja el sitio listo para que uvicorn pueda enlazar.

    Un socket huérfano de un arranque anterior impide enlazar con "Address
    already in use", que en un fichero despista bastante. Se borra si no hay
    nadie escuchando detrás; si lo hubiera, el propio bind fallará y es lo
    correcto.
    """
    ruta.parent.mkdir(parents=True, exist_ok=True)
    if ruta.exists():
        logger.debug(f"Borrando el socket huérfano {ruta}")
        ruta.unlink()


def main() -> int:
    """Arranca el puente y lo mantiene en marcha hasta que lo paren."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="{time:HH:mm:ss.SSS} {level: <8} {name}:{function} - {message}",
    )

    directorio = _directorio_datos()
    socket = _ruta_socket(directorio)
    _preparar_socket(socket)

    servicio = Servicio(
        directorio_datos=directorio,
        direccion_preferida=os.environ.get("TELEFONIA_BLUETOOTH_ADDRESS", ""),
        ttl_agenda_horas=int(os.environ.get("TELEFONIA_CONTACTOS_TTL_HORAS", "12")),
        # `off` por defecto, y con motivo: registrar el agente de audio le
        # quita el sonido de la llamada al móvil. Ver audio_sco.py.
        modo_audio=os.environ.get("TELEFONIA_AUDIO_MODO", "off"),
        # mSBC se anuncia solo si se pide: el codec se pacta por sesion HFP y
        # no conviene ofrecerlo en produccion sin haberlo validado en el aire.
        msbc_audio=os.environ.get("TELEFONIA_AUDIO_MSBC", "") == "si",
    )
    app = crear_app(servicio)

    logger.info(f"Puente de telefonía escuchando en {socket}")
    uvicorn.run(app, uds=str(socket), log_level="warning", access_log=False)

    with contextlib.suppress(FileNotFoundError):
        socket.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
