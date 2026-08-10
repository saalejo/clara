"""El contrato de `tareas.json` entre el panel y el agente.

Como con `runtime.json`: lo que importa es que una tarea imposible falle en el
panel al guardarla, y que un fichero roto degrade a "sin tareas" en vez de
tumbar el arranque del agente.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from voice_agent_core.rutas import escribir_json_atomico, ruta_tareas
from voice_agent_core.tareas import TareaProgramada, TareasConfig, TipoTarea, cargar_tareas


def _tarea(**cambios: object) -> TareaProgramada:
    base: dict[str, object] = {
        "id": "pastillas-manana",
        "nombre": "Pastillas de la mañana",
        "cron": "0 8 * * 1-5",
        "mision": "Recuérdale a Nora la pastilla de la tensión.",
    }
    base.update(cambios)
    return TareaProgramada.model_validate(base)


def test_ida_y_vuelta_por_json(tmp_path: Path) -> None:
    original = TareasConfig(
        generado_en=datetime(2026, 8, 5, 10, 0),
        tareas=[
            _tarea(),
            _tarea(
                id="revision-abuela",
                tipo=TipoTarea.LLAMADA,
                contacto_nombre="Abuela",
                contacto_numero="+573001234567",
                guardar_respuestas=True,
                habilitada=False,
            ),
        ],
    )
    escribir_json_atomico(ruta_tareas(tmp_path), original.model_dump(mode="json"))
    assert cargar_tareas(tmp_path) == original


def test_sin_fichero_no_hay_tareas(tmp_path: Path) -> None:
    assert cargar_tareas(tmp_path) == TareasConfig()


def test_json_corrupto_degrada_a_vacio(tmp_path: Path) -> None:
    ruta = ruta_tareas(tmp_path)
    ruta.parent.mkdir(parents=True)
    ruta.write_text("{esto no es json", encoding="utf-8")
    assert cargar_tareas(tmp_path) == TareasConfig()


def test_modelo_invalido_degrada_a_vacio(tmp_path: Path) -> None:
    # JSON bien formado pero que no valida: mismo destino que el corrupto.
    ruta = ruta_tareas(tmp_path)
    ruta.parent.mkdir(parents=True)
    ruta.write_text('{"tareas": [{"id": "x", "cron": "no es cron", "mision": "y"}]}')
    assert cargar_tareas(tmp_path) == TareasConfig()


def test_cron_invalido_no_valida() -> None:
    with pytest.raises(ValidationError, match="5 campos"):
        _tarea(cron="cada mañana")


def test_llamada_sin_numero_no_valida() -> None:
    with pytest.raises(ValidationError, match="número"):
        _tarea(tipo=TipoTarea.LLAMADA, contacto_nombre="Abuela")


@pytest.mark.parametrize("malo", ["", "Con Mayúsculas", "con espacios", "../fuera", "ñu"])
def test_id_es_un_slug(malo: str) -> None:
    # El id acaba siendo carpeta de resultados: nada de rutas ni sorpresas.
    with pytest.raises(ValidationError):
        _tarea(id=malo)


def test_habilitadas_filtra() -> None:
    config = TareasConfig(tareas=[_tarea(), _tarea(id="apagada", habilitada=False)])
    assert [t.id for t in config.habilitadas] == ["pastillas-manana"]
