"""El avance de la reindexación, que la ingesta publica y el panel enseña.

Mismo canal y mismo motivo que `calidad.EstadoLote`: quien reindexa es otro
proceso —la unidad `clara-ingest`, que sí tiene chromadb y fastembed— y el
panel no puede preguntarle nada, así que el progreso viaja por un fichero JSON
en `DATA_DIR`. La página de Conocimiento lo consulta una vez por segundo
mientras la unidad está viva.

Vive en `core` y no en `voice_agent.rag` porque lo importan los dos lados, y el
panel no puede importar `voice_agent` (ver `tests/test_core_liviano.py`).

## Qué mide la barra

El reparto de la barra es deliberado y está aquí porque es lo que hace que no
mienta:

- **Explorar** se lleva el primer 10 %. Es recorrer el corpus, calcular la
  huella de cada fichero y preguntarle a cada colección qué tiene ya. Cuesta
  segundos, no minutos, pero no es instantáneo con 106 PDF.
- **Indexar** se lleva del 10 al 95 %, repartido entre los documentos que
  *hay que* procesar —no entre todos—. Es donde se va el tiempo de verdad:
  extraer el texto de un PDF, trocearlo y calcular sus embeddings.
- **Limpiar** es el 95 % restante: olvidar fragmentos sobrantes y borrar las
  colecciones de temas que ya no existen.

De ahí que una reindexación en la que no ha cambiado nada llegue al 100 % en un
par de segundos: no es que la barra vaya rápido, es que no hay trabajo.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

from voice_agent_core.rutas import escribir_json_atomico, ruta_progreso_ingesta

#: Peso de cada fase en la barra. Ver el porqué en el docstring del módulo.
_PESO_EXPLORAR = 10
_PESO_INDEXAR = 85


class FaseIngesta(StrEnum):
    """En qué anda la reindexación.

    Attributes:
        EXPLORANDO: Recorriendo el corpus y decidiendo qué hace falta reprocesar.
        INDEXANDO: Leyendo, troceando y vectorizando los documentos pendientes.
        LIMPIANDO: Olvidando lo que sobra y borrando colecciones huérfanas.
        TERMINADO: Acabó bien.
        ERROR: Acabó mal; el motivo está en `error`.
    """

    EXPLORANDO = "explorando"
    INDEXANDO = "indexando"
    LIMPIANDO = "limpiando"
    TERMINADO = "terminado"
    ERROR = "error"


class ProgresoIngesta(BaseModel):
    """Retrato de la reindexación en un instante.

    Los contadores de documentos son tres y no dos a propósito: `sin_cambios`
    —los que la exploración ha reconocido ya indexados— es la cifra que explica
    por qué reindexar 106 PDF puede tardar diez segundos, y esconderla dejaría
    la barra pareciendo rota.
    """

    iniciada_en: datetime
    actualizada_en: datetime
    fase: FaseIngesta = FaseIngesta.EXPLORANDO

    temas_total: int = 0
    temas_hechos: int = 0

    documentos_total: int = Field(default=0, description="Documentos indexables del corpus.")
    documentos_sin_cambios: int = Field(
        default=0, description="Los que ya estaban indexados con la misma huella: no se tocan."
    )
    documentos_pendientes: int = Field(
        default=0, description="Los que hay que leer, trocear y vectorizar en esta pasada."
    )
    documentos_hechos: int = Field(default=0, description="De los pendientes, cuántos van.")

    tema_actual: str = ""
    documento_actual: str = ""

    fragmentos_total: int = Field(default=0, description="Fragmentos que quedan en el índice.")
    fragmentos_nuevos: int = Field(
        default=0, description="Los que ha habido que vectorizar en esta pasada."
    )
    fragmentos_olvidados: int = Field(
        default=0, description="Los que se han borrado por sobrar (documentos editados o borrados)."
    )

    error: str = ""

    @property
    def terminada(self) -> bool:
        """Si ya no va a cambiar más."""
        return self.fase in (FaseIngesta.TERMINADO, FaseIngesta.ERROR)

    @property
    def porcentaje(self) -> int:
        """Avance de 0 a 100, repartido entre las fases."""
        if self.terminada:
            return 100
        if self.fase is FaseIngesta.LIMPIANDO:
            return _PESO_EXPLORAR + _PESO_INDEXAR
        if self.fase is FaseIngesta.EXPLORANDO:
            if self.temas_total <= 0:
                return 0
            return int(_PESO_EXPLORAR * self.temas_hechos / self.temas_total)
        if self.documentos_pendientes <= 0:
            return _PESO_EXPLORAR + _PESO_INDEXAR
        avance = self.documentos_hechos / self.documentos_pendientes
        return _PESO_EXPLORAR + int(_PESO_INDEXAR * avance)

    @property
    def duracion_s(self) -> float:
        """Cuánto lleva —o cuánto duró, si ya terminó."""
        return max(0.0, (self.actualizada_en - self.iniciada_en).total_seconds())


def escribir_progreso(data_dir: Path, progreso: ProgresoIngesta) -> None:
    """Publica el avance para el panel.

    Nunca lanza: quedarse sin barra de progreso no es motivo para abortar una
    reindexación que va bien.
    """
    try:
        escribir_json_atomico(ruta_progreso_ingesta(data_dir), progreso.model_dump(mode="json"))
    except OSError as e:
        logger.warning(f"No se pudo publicar el avance de la reindexación: {e}")


def leer_progreso(data_dir: Path) -> ProgresoIngesta | None:
    """Lee el avance publicado por la última reindexación.

    Returns:
        El progreso, o `None` si no existe o no se puede interpretar —lo que en
        el panel significa "aquí no se ha reindexado todavía".
    """
    ruta = ruta_progreso_ingesta(data_dir)
    if not ruta.is_file():
        return None
    try:
        return ProgresoIngesta.model_validate_json(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.warning(f"El avance de la reindexación en {ruta} no se pudo leer: {e}")
        return None
