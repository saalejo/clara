"""Preferencias del puente que sobreviven a un reinicio.

Hoy solo hay una: si el puente descuelga solo las llamadas entrantes.

## Por qué un fichero propio y no `config/settings.json`

Ese es el contrato **panel → agente**, y lo escribe entero el exportador del
panel con `escribir_json_atomico`. Meter aquí un segundo escritor del mismo
fichero es pedir una carrera: los dos leen, los dos modifican su parte y el
último en escribir se lleva por delante lo del otro. Con un fichero por dueño no
hay nada que coordinar.

## Por qué persistirlo

Porque el interruptor se pone con un botón físico y se olvida. Si no sobreviviera
al reinicio, un corte de luz dejaría el autocontestar apagado sin que nadie se
enterase — y de las dos formas de equivocarse, la que sorprende es que deje de
contestar cuando lo habías dejado puesto.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from voice_agent_core.rutas import escribir_json_atomico

NOMBRE = "preferencias.json"
SUBDIR = "telefonia"


class Preferencias(BaseModel):
    """Lo que el puente recuerda entre arranques."""

    autocontestar: bool = Field(
        default=False,
        description="Descolgar solo las llamadas entrantes.",
    )
    autocontestar_segundos: float = Field(
        default=2.0,
        ge=0.0,
        le=15.0,
        description=(
            "Margen antes de descolgar. No es cero a propósito: da tiempo a "
            "rechazar la llamada a mano, y deja que la máquina de estados de oFono "
            "se asiente antes de mandarle otra orden."
        ),
    )


def ruta(directorio_datos: Path) -> Path:
    """Dónde vive el fichero de preferencias."""
    return directorio_datos / SUBDIR / NOMBRE


def cargar(ruta_fichero: Path) -> Preferencias:
    """Lee las preferencias. Nunca lanza.

    Un fichero que falta es lo normal la primera vez. Uno corrupto es raro, pero
    la respuesta correcta es la misma: arrancar con los valores por defecto y
    registrarlo. Negarse a arrancar el puente porque un JSON de dos campos esté
    roto dejaría al usuario sin telefonía por un problema que se arregla borrando
    un fichero.
    """
    try:
        return Preferencias.model_validate_json(ruta_fichero.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Preferencias()
    except (OSError, ValidationError, ValueError) as e:
        logger.warning(f"No he podido leer {ruta_fichero} ({e}); uso los valores por defecto")
        return Preferencias()


def guardar(ruta_fichero: Path, preferencias: Preferencias) -> None:
    """Escribe las preferencias sin que se puedan leer a medias.

    Reutiliza `escribir_json_atomico` de `core`, que es lo que ya usa el panel:
    temporal en el mismo directorio, `fsync` y `os.replace`.
    """
    escribir_json_atomico(ruta_fichero, preferencias.model_dump(mode="json"))


__all__ = ["NOMBRE", "SUBDIR", "Preferencias", "cargar", "guardar", "ruta"]
