"""El registro de métricas del agente: latencia, tokens y consultas.

El README publica números concretos —latencia P50 y P95 desde que
el paciente deja de hablar hasta que suena el agente, tokens por turno y por
llamada, invocaciones del modelo por turno—, y reportar números que no se
sostienen es peor que no reportarlos. La forma de que se sostengan
es medirlos donde ocurren y dejarlos en disco: este módulo escribe una línea
JSON por evento en `data/metricas/<fecha>.jsonl`, y `scripts/metricas.py` los
agrega a la tabla del README.

El `MetricsRecorder` se inserta al final del pipeline web, tras la salida de
audio (el mismo hueco donde el pipeline de sala pone su controlador de
compuerta, por donde ya se sabe que pasan los frames de habla del bot):

* `UserStoppedSpeakingFrame` marca el inicio del silencio del paciente.
* El primer `BotStartedSpeakingFrame` posterior cierra la medición: esa
  diferencia es la latencia voz-a-voz percibida, la que se publica.
* Los `MetricsFrame` que emiten los servicios (con `enable_metrics=True`)
  traen el TTFB por servicio y el uso de tokens del LLM; se vuelcan tal cual.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    MetricsFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    ProcessingMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


def dir_metricas(data_dir: Path) -> Path:
    """Carpeta de los JSONL de métricas, un fichero por día."""
    return data_dir / "metricas"


def anotar_evento(data_dir: Path, tipo: str, **datos: Any) -> None:
    """Añade una línea de evento al JSONL del día.

    Nunca lanza: las métricas son contabilidad, no funcionalidad. Un disco
    lleno no puede tumbar una llamada.

    Args:
        data_dir: El directorio de datos del agente.
        tipo: El tipo de evento (`llamada_inicio`, `voz_a_voz`, `llm_uso`...).
        **datos: Los campos del evento.
    """
    linea = {"momento": datetime.now().isoformat(timespec="seconds"), "tipo": tipo, **datos}
    try:
        carpeta = dir_metricas(data_dir)
        carpeta.mkdir(parents=True, exist_ok=True)
        ruta = carpeta / f"{datetime.now():%Y%m%d}.jsonl"
        with ruta.open("a", encoding="utf-8") as f:
            f.write(json.dumps(linea, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning(f"No se pudo anotar la métrica {tipo}: {e}")


class MetricsRecorder(FrameProcessor):
    """Anota la latencia voz-a-voz y las métricas de los servicios en disco."""

    def __init__(self, data_dir: Path, id_llamada: str) -> None:
        """Prepara el registrador.

        Args:
            data_dir: El directorio de datos del agente.
            id_llamada: La llamada a la que pertenecen los eventos, para poder
                agrupar por llamada al agregar.
        """
        super().__init__()
        self._data_dir = data_dir
        self._id_llamada = id_llamada
        self._fin_de_habla: float | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Observa los frames que le interesan y deja pasar todos."""
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStoppedSpeakingFrame):
            self._fin_de_habla = time.monotonic()
        elif isinstance(frame, BotStartedSpeakingFrame):
            if self._fin_de_habla is not None:
                segundos = time.monotonic() - self._fin_de_habla
                self._fin_de_habla = None
                anotar_evento(
                    self._data_dir,
                    "voz_a_voz",
                    id_llamada=self._id_llamada,
                    segundos=round(segundos, 3),
                )
        elif isinstance(frame, MetricsFrame):
            self._anotar_metricas(frame)

        await self.push_frame(frame, direction)

    def _anotar_metricas(self, frame: MetricsFrame) -> None:
        """Vuelca las métricas de los servicios (TTFB, proceso, tokens)."""
        for dato in frame.data:
            if isinstance(dato, LLMUsageMetricsData):
                anotar_evento(
                    self._data_dir,
                    "llm_uso",
                    id_llamada=self._id_llamada,
                    modelo=dato.model,
                    tokens_entrada=dato.value.prompt_tokens,
                    tokens_salida=dato.value.completion_tokens,
                )
            elif isinstance(dato, TTFBMetricsData):
                anotar_evento(
                    self._data_dir,
                    "ttfb",
                    id_llamada=self._id_llamada,
                    procesador=dato.processor,
                    segundos=round(dato.value, 3),
                )
            elif isinstance(dato, ProcessingMetricsData):
                anotar_evento(
                    self._data_dir,
                    "proceso",
                    id_llamada=self._id_llamada,
                    procesador=dato.processor,
                    segundos=round(dato.value, 3),
                )
            elif isinstance(dato, TTSUsageMetricsData):
                anotar_evento(
                    self._data_dir,
                    "tts_uso",
                    id_llamada=self._id_llamada,
                    caracteres=dato.value,
                )


__all__ = ["MetricsRecorder", "anotar_evento", "dir_metricas"]
