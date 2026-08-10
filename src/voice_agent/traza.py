"""La traza documental de una llamada: qué respaldó cada respuesta clínica.

La rúbrica del reto exige que cada respuesta clínica pueda rastrearse hasta el
documento que la sustenta, y que esa referencia "resista una verificación
contra la fuente real". Fiarse de que el modelo cite bien de memoria es
exactamente lo que no resiste esa verificación; lo que sí la resiste es
registrar **lo que el RAG devolvió de verdad** en cada consulta.

Cada llamada tiene su traza: un JSONL en `data/evaluaciones/trazas/`, una
línea por consulta con los pasajes recuperados (origen, tema y distancia). El
resumen de la llamada adjunta la lista de documentos consultados desde aquí, y
el panel —o el jurado— puede abrir el fichero y comparar con los PDF.

Se escribe con `append` y no con la escritura atómica de `rutas.py`: es un log
incremental de una sola escritora, y perder media línea en un corte de luz es
aceptable; perder las líneas anteriores por reescribir el fichero entero, no.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from loguru import logger

from voice_agent.rag.retriever import Pasaje
from voice_agent_core.rutas import dir_trazas


class TrazaLlamada:
    """Acumula y persiste las consultas al RAG de una llamada concreta."""

    def __init__(self, data_dir: Path, id_llamada: str | None = None) -> None:
        """Prepara la traza de una llamada.

        Args:
            data_dir: El directorio de datos del agente.
            id_llamada: Identificador de la llamada. Si no se da, se deriva
                del momento de creación, que para una llamada de navegador es
                único de sobra.
        """
        self.id_llamada = id_llamada or f"llamada-{datetime.now():%Y%m%d-%H%M%S}"
        self._ruta = dir_trazas(data_dir) / f"{self.id_llamada}.jsonl"
        self._origenes: list[str] = []

    @property
    def ruta(self) -> Path:
        """El fichero JSONL de esta traza, exista ya o no."""
        return self._ruta

    @property
    def documentos_consultados(self) -> list[str]:
        """Los orígenes distintos que respaldaron respuestas, en orden de uso."""
        return list(dict.fromkeys(self._origenes))

    def registrar(self, consulta: str, pasajes: Sequence[Pasaje]) -> None:
        """Anota una consulta al RAG y lo que devolvió.

        Nunca lanza: la traza es una obligación del sistema, no del paciente.
        Un disco lleno no puede tumbar la conversación; se registra el fallo y
        la llamada sigue.

        Args:
            consulta: La consulta tal y como la formuló el modelo.
            pasajes: Los pasajes que el RAG devolvió (puede ser vacío: una
                consulta sin resultados también es traza, porque respalda un
                "no lo sé").
        """
        self._origenes.extend(p.origen for p in pasajes)
        linea = {
            "momento": datetime.now().isoformat(timespec="seconds"),
            "consulta": consulta,
            "pasajes": [
                {"origen": p.origen, "tema": p.tema, "distancia": round(p.distancia, 4)}
                for p in pasajes
            ],
        }
        try:
            self._ruta.parent.mkdir(parents=True, exist_ok=True)
            with self._ruta.open("a", encoding="utf-8") as f:
                f.write(json.dumps(linea, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.error(f"No se pudo escribir la traza {self._ruta}: {e}")


__all__ = ["TrazaLlamada"]
