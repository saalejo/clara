"""Adaptador del control de systemd para el panel.

La implementación vive en `voice_agent_core.systemd`, porque la comparten el panel
y el mando físico de la tarjeta de sonido (`packages/botones`). Ahí están también
los dos porqués que costaron medirlos: por qué D-Bus y no la API de Podman, y por
qué la unidad del panel necesita `--userns=keep-id`.

Aquí solo queda lo que es del panel: **la política** —qué unidades se pueden
tocar, que sale de `settings.UNIDADES_PERMITIDAS`— y los valores por defecto, para
que una vista pueda seguir escribiendo `control.reiniciar()` sin nombrar la unidad
del agente. Se mantiene la interfaz de funciones de módulo a propósito: `views.py`
y sus tests hacen `monkeypatch.setattr(control, "estado", ...)`, y cambiarla a una
clase habría obligado a reescribirlos sin ganar nada.
"""

from __future__ import annotations

from functools import lru_cache

from django.conf import settings

from voice_agent_core.systemd import ControlSystemd, ErrorDeControl, EstadoUnidad

__all__ = [
    "ErrorDeControl",
    "EstadoUnidad",
    "arrancar",
    "estado",
    "lanzar_ingesta",
    "parar",
    "reiniciar",
]


@lru_cache(maxsize=1)
def _control() -> ControlSystemd:
    """El control, con la lista blanca del panel.

    Se cachea porque no tiene estado mutable: cada llamada abre y cierra su propia
    conexión al bus.
    """
    return ControlSystemd(frozenset(settings.UNIDADES_PERMITIDAS))


def estado(unidad: str | None = None) -> EstadoUnidad:
    """Consulta el estado de una unidad; por defecto, la del agente.

    Raises:
        ErrorDeControl: Si no se puede hablar con systemd.
    """
    return _control().estado(unidad or settings.UNIDAD_AGENTE)


def arrancar(unidad: str | None = None) -> str:
    """Arranca la unidad. Devuelve la ruta del trabajo en systemd."""
    return _control().arrancar(unidad or settings.UNIDAD_AGENTE)


def parar(unidad: str | None = None) -> str:
    """Para la unidad."""
    return _control().parar(unidad or settings.UNIDAD_AGENTE)


def reiniciar(unidad: str | None = None) -> str:
    """Reinicia la unidad, que es como se aplica un cambio de configuración."""
    return _control().reiniciar(unidad or settings.UNIDAD_AGENTE)


def lanzar_ingesta() -> str:
    """Lanza la reindexación del RAG como unidad oneshot.

    Corre en su propio contenedor, con la imagen del agente y su propio tope de
    memoria: fastembed se come unos 400 MB durante el proceso y no conviene que eso
    ocurra dentro del panel ni compitiendo por los 2 GB del agente.
    """
    return _control().arrancar(settings.UNIDAD_INGESTA)
