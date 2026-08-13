"""Tareas programadas: misiones que el agente ejecuta a una hora dada.

Una tarea es un prompt con horario: a la hora que marca su expresión cron, el
agente recibe la misión y la ejecuta él mismo — anuncia algo en la sala, hace
un cuestionario, o llama por teléfono a alguien y conversa con un propósito.

El panel las edita en su base de datos y las exporta a
`<DATA_DIR>/config/tareas.json`; el agente las **recarga en caliente**
vigilando el mtime del fichero. Esto no contradice la doctrina de "la
configuración se lee una vez" de `bot.py`: aquella existe porque el prompt y
las muletillas invalidan el historial y el banco de audio a mitad de turno, y
una lista de tareas no toca ni lo uno ni lo otro (mismo argumento por el que
el índice RAG ya se recoge en caliente).

Como `runtime.py`, este módulo lo importan los dos lados: el panel para
validar y escribir, el agente para leer y planificar.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field, field_validator, model_validator

from voice_agent_core.cron import ExpresionCron
from voice_agent_core.rutas import ruta_tareas

#: El id viaja en JSON y acaba siendo carpeta de resultados: solo minúsculas,
#: dígitos y guiones, empezando por letra o dígito.
PATRON_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class TipoTarea(StrEnum):
    """Dónde se ejecuta la misión.

    Attributes:
        SALA: El agente habla por el altavoz de la habitación.
        LLAMADA: El agente llama por teléfono al contacto de la tarea.
    """

    SALA = "sala"
    LLAMADA = "llamada"


class TareaProgramada(BaseModel):
    """Una misión con horario."""

    id: str = Field(description="Identificador estable; nombra la carpeta de resultados.")
    nombre: str = Field(default="", description="Título legible para el panel.")
    habilitada: bool = True
    tipo: TipoTarea = TipoTarea.SALA
    cron: str = Field(description="Expresión de 5 campos, p. ej. '0 8 * * 1-5'.")
    mision: str = Field(description="El encargo, redactado para el modelo.")
    guardar_respuestas: bool = Field(
        default=False,
        description=(
            "Si la misión es un cuestionario: pide al modelo que al terminar "
            "guarde lo respondido con la herramienta `guardar_respuestas`."
        ),
    )
    contacto_nombre: str = Field(
        default="", description="Solo tipo=llamada: a quién se llama, para el prompt."
    )
    contacto_numero: str = Field(
        default="",
        description=(
            "Solo tipo=llamada. Se congela al crear la tarea en el panel: a la "
            "hora del disparo no hay nadie con quien desambiguar la agenda."
        ),
    )
    procedimiento: str = Field(
        default="",
        description=(
            "De qué se operó el paciente. Es el dato que arma la puerta de "
            "cobertura **antes del primer turno**: el agente solo consulta los "
            "protocolos de esa cirugía, y si el corpus no la cubre no consulta "
            "ninguno en vez de contestar con los de otra. Vacío deja que sea el "
            "agente quien lo pregunte durante la llamada."
        ),
    )

    @field_validator("id")
    @classmethod
    def _id_valido(cls, v: str) -> str:
        if not PATRON_ID.fullmatch(v):
            raise ValueError(
                f"El id '{v}' no vale: solo minúsculas, dígitos y guiones (es una ruta)."
            )
        return v

    @field_validator("cron")
    @classmethod
    def _cron_valido(cls, v: str) -> str:
        ExpresionCron.parse(v)  # ErrorDeCron es un ValueError; pydantic lo recoge
        return v

    @model_validator(mode="after")
    def _llamada_con_numero(self) -> TareaProgramada:
        if self.tipo is TipoTarea.LLAMADA and not self.contacto_numero.strip():
            raise ValueError(f"La tarea '{self.id}' es una llamada pero no tiene número congelado.")
        return self

    @property
    def expresion(self) -> ExpresionCron:
        """La expresión ya interpretada; el validador garantiza que parsea."""
        return ExpresionCron.parse(self.cron)

    @property
    def id_resultados(self) -> str:
        """Bajo qué id se guardan las respuestas y se anota la bitácora.

        Una tarea del panel guarda bajo su propio id. Existe para cumplir
        `misiones.EncargoLlamada`, donde una misión puntual puede querer
        guardar bajo el de la tarea que la originó (ver `MisionPuntual`).
        """
        return self.id

    @property
    def intento(self) -> int:
        """Una tarea del panel es siempre el primer intento.

        Los reintentos no se guardan aquí: son misiones puntuales del agente,
        y este fichero lo reescribe el panel entero en cada guardado.
        """
        return 0


class TareasConfig(BaseModel):
    """El fichero `tareas.json` completo."""

    version: int = 1
    generado_en: datetime | None = None
    tareas: list[TareaProgramada] = Field(default_factory=list)

    @property
    def habilitadas(self) -> list[TareaProgramada]:
        """Las tareas que el planificador debe tener en el calendario."""
        return [t for t in self.tareas if t.habilitada]


def cargar_tareas(data_dir: Path) -> TareasConfig:
    """Lee las tareas exportadas por el panel, o devuelve una lista vacía.

    Nunca lanza, por la misma razón que `cargar_runtime`: un JSON corrupto
    degrada a "sin tareas" con un aviso en el log, no a una placa muda.

    Args:
        data_dir: La raíz de datos, normalmente `Settings.data_dir`.

    Returns:
        La configuración leída, o una vacía.
    """
    ruta = ruta_tareas(data_dir)
    if not ruta.is_file():
        return TareasConfig()
    try:
        return TareasConfig.model_validate_json(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.error(f"No se pudieron leer las tareas de {ruta}: {e}. Se sigue sin tareas.")
        return TareasConfig()
