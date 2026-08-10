"""Lo que el agente publica sobre sí mismo al terminar de arrancar.

Es el único canal en sentido agente → panel, y existe porque el panel vive en
otra imagen: no tiene Pipecat, así que no puede calcular el esquema JSON de una
herramienta, y no tiene los comandos de los servidores MCP, así que no puede
sondear uno de tipo stdio.

Resolverlo por fichero resulta además ser *mejor* que calcularlo en el panel: lo
que se enseña es lo que el agente tiene cargado de verdad en este momento
—herramientas de MCP incluidas, y los servidores que fallaron— en vez de lo que
debería tener según la base de datos. Cuando ambas cosas no coinciden, que es
justo cuando uno mira el panel, la diferencia es el diagnóstico.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from voice_agent_core.rutas import escribir_json_atomico, ruta_estado

ORIGEN_LOCAL = "local"


class HerramientaExpuesta(BaseModel):
    """Una herramienta tal y como la ve el modelo."""

    nombre: str
    origen: str = Field(default=ORIGEN_LOCAL, description="'local' o el nombre del servidor MCP.")
    descripcion: str = ""
    esquema: dict[str, Any] = Field(
        default_factory=dict,
        description="El esquema JSON exacto que se le manda al modelo, para poder enseñarlo tal cual.",
    )


class ServidorMCPEstado(BaseModel):
    """Cómo le fue a un servidor MCP en el último arranque."""

    nombre: str
    conectado: bool
    error: str = ""
    herramientas: list[str] = Field(default_factory=list)


class EstadoArranque(BaseModel):
    """Retrato del agente justo después de montar el pipeline."""

    generado_en: datetime
    version_agente: str = ""
    runtime_version: int = 1
    snapshot_settings_mtime: float | None = Field(
        default=None,
        description=(
            "Fecha de modificación de settings.json cuando se leyó. El panel la "
            "compara con la suya para saber si el agente ya está corriendo con "
            "lo último que se guardó o si falta reiniciarlo."
        ),
    )

    tiene_llm_api_key: bool = False
    tiene_deepgram_api_key: bool = Field(
        default=False,
        description=(
            "Solo si está configurada o no. El panel no monta el .env y no ve "
            "las claves; esto le permite avisar de que falta una sin haberla "
            "leído nunca."
        ),
    )

    herramientas: list[HerramientaExpuesta] = Field(default_factory=list)
    mcp: list[ServidorMCPEstado] = Field(default_factory=list)
    hooks_activos: list[str] = Field(default_factory=list)

    @classmethod
    def ahora(cls, **campos: Any) -> EstadoArranque:
        """Construye un estado fechado en este instante."""
        return cls(generado_en=datetime.now(UTC), **campos)


def escribir_estado(data_dir: Path, estado: EstadoArranque) -> None:
    """Publica el estado para el panel.

    Nunca lanza: no poder escribir este fichero es un problema de observabilidad,
    no una razón para que el agente no arranque.
    """
    try:
        escribir_json_atomico(ruta_estado(data_dir), estado.model_dump(mode="json"))
    except OSError as e:
        logger.warning(f"No se pudo publicar el estado de arranque para el panel: {e}")


def leer_estado(data_dir: Path) -> EstadoArranque | None:
    """Lee el estado publicado por el agente.

    Returns:
        El estado, o `None` si no existe o no se puede interpretar —lo que en el
        panel se traduce en "el agente no ha arrancado todavía".
    """
    ruta = ruta_estado(data_dir)
    if not ruta.is_file():
        return None
    try:
        return EstadoArranque.model_validate_json(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.warning(f"El estado de arranque en {ruta} no se pudo leer: {e}")
        return None
