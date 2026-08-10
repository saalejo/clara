"""La herramienta que persiste las respuestas de un cuestionario.

Lo delicado es que el id lo teclea el modelo copiándolo de la misión: acaba
siendo una carpeta en disco, así que un id inventado o con rutas dentro tiene
que morir en la validación, no en el sistema de ficheros.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from pipecat.services.llm_service import FunctionCallParams

from voice_agent.resources import AppResources
from voice_agent.tools.tareas import guardar_respuestas
from voice_agent_core.config import Settings
from voice_agent_core.rutas import dir_resultados_tareas


@dataclass
class ParamsFalsos:
    """Sustituto de `FunctionCallParams` con lo único que la herramienta usa."""

    app_resources: Any
    resultado: Any = None

    async def result_callback(self, resultado: Any) -> None:
        self.resultado = resultado


def _params(tmp_path: Path) -> ParamsFalsos:
    recursos = AppResources(
        settings=Settings(_env_file=None, data_dir=tmp_path),  # type: ignore[call-arg]
        retriever=cast(Any, None),  # esta herramienta no consulta el RAG
    )
    return ParamsFalsos(app_resources=recursos)


async def test_guarda_un_fichero_por_ejecucion(tmp_path: Path) -> None:
    params = _params(tmp_path)
    await guardar_respuestas(
        cast(FunctionCallParams, params),
        id_tarea="revision-abuela",
        respuestas="Durmió bien. No le duele nada.",
        resumen="Todo en orden",
    )

    assert params.resultado["guardado"] is True
    carpeta = dir_resultados_tareas(tmp_path) / "revision-abuela"
    ficheros = list(carpeta.iterdir())
    assert len(ficheros) == 1
    datos = json.loads(ficheros[0].read_text(encoding="utf-8"))
    assert datos["id_tarea"] == "revision-abuela"
    assert datos["respuestas"] == "Durmió bien. No le duele nada."
    assert datos["resumen"] == "Todo en orden"
    assert datos["momento"]  # ISO legible; el panel lo enseña tal cual


async def test_dos_guardados_no_se_pisan(tmp_path: Path) -> None:
    # Mismo segundo o no, cada llamada tiene que dejar su propio fichero.
    params = _params(tmp_path)
    for i in range(2):
        await guardar_respuestas(
            cast(FunctionCallParams, params), id_tarea="revision-abuela", respuestas=f"toma {i}"
        )
    carpeta = dir_resultados_tareas(tmp_path) / "revision-abuela"
    assert len(list(carpeta.iterdir())) == 2


@pytest.mark.parametrize("malo", ["../fuera", "Con Espacios", "", "x/y"])
async def test_un_id_invalido_no_toca_el_disco(tmp_path: Path, malo: str) -> None:
    params = _params(tmp_path)
    await guardar_respuestas(cast(FunctionCallParams, params), id_tarea=malo, respuestas="da igual")

    assert "error" in params.resultado
    assert "sugerencia" in params.resultado
    assert not dir_resultados_tareas(tmp_path).exists()


async def test_el_id_se_normaliza_antes_de_validar(tmp_path: Path) -> None:
    # El modelo a veces devuelve el id con mayúsculas o espacios alrededor;
    # eso no es motivo para perder unas respuestas.
    params = _params(tmp_path)
    await guardar_respuestas(
        cast(FunctionCallParams, params), id_tarea="  Revision-Abuela ", respuestas="bien"
    )
    assert params.resultado["guardado"] is True
    assert (dir_resultados_tareas(tmp_path) / "revision-abuela").is_dir()


async def test_un_error_de_disco_no_lanza(tmp_path: Path) -> None:
    # La carpeta de resultados es un fichero: escribir dentro es imposible.
    dir_resultados_tareas(tmp_path).parent.mkdir(parents=True)
    dir_resultados_tareas(tmp_path).write_text("estorbo")
    params = _params(tmp_path)
    await guardar_respuestas(
        cast(FunctionCallParams, params), id_tarea="revision-abuela", respuestas="bien"
    )
    assert "error" in params.resultado
