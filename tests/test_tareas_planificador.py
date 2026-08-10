"""El planificador de tareas: el reloj, la recarga en caliente y las misiones de sala.

Lo que se fija es la mecánica que no se ve en una demo: que un disparo no se
repite en el mismo minuto, que editar el fichero jamás dispara hacia atrás,
que sin sala la misión se aplaza y queda anotada, y que el frame que entra al
pipeline es un frame de datos con turno — la regla de `telefonia_anuncios`
más el `run_llm` que este módulo estrena.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pipecat.frames.frames import DataFrame, LLMMessagesAppendFrame, SystemFrame

from voice_agent import tareas_programadas
from voice_agent.tareas_programadas import (
    ESPERA_OCUPADO_MAX_SECS,
    MisionesLlamada,
    ProgramadorTareas,
    SalaActual,
    instruccion_mision_sala,
)
from voice_agent_core.config import Settings
from voice_agent_core.rutas import escribir_json_atomico, ruta_bitacora_tareas, ruta_tareas
from voice_agent_core.tareas import TareaProgramada, TareasConfig


class WorkerFalso:
    """Solo apunta lo que le encolan."""

    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def queue_frames(self, frames: list[Any]) -> None:
        self.frames.extend(frames)


class RelojFalso:
    """Un `datetime.now` que se mueve a mano."""

    def __init__(self, momento: datetime) -> None:
        self.momento = momento

    def __call__(self) -> datetime:
        return self.momento


def tarea(**cambios: Any) -> TareaProgramada:
    base: dict[str, Any] = {
        "id": "pastillas",
        "cron": "0 8 * * *",
        "mision": "Recuérdale a Nora la pastilla.",
    }
    base.update(cambios)
    return TareaProgramada.model_validate(base)


def escribir_tareas(data_dir: Path, *tareas: TareaProgramada) -> None:
    config = TareasConfig(tareas=list(tareas))
    escribir_json_atomico(ruta_tareas(data_dir), config.model_dump(mode="json"))
    # El planificador vigila el mtime; dos escrituras en el mismo test pueden
    # caer tan juntas que el reloj del sistema de ficheros no las distinga.
    marca = os.stat(ruta_tareas(data_dir)).st_mtime
    os.utime(ruta_tareas(data_dir), (marca + 1, marca + 1))


def bitacora(data_dir: Path) -> list[dict[str, Any]]:
    ruta = ruta_bitacora_tareas(data_dir)
    if not ruta.is_file():
        return []
    return [json.loads(linea) for linea in ruta.read_text(encoding="utf-8").splitlines()]


def preparar(
    tmp_path: Path,
    *tareas_: TareaProgramada,
    momento: datetime = datetime(2026, 8, 5, 7, 59),
    con_worker: bool = True,
) -> tuple[ProgramadorTareas, WorkerFalso, RelojFalso, SalaActual]:
    escribir_tareas(tmp_path, *tareas_)
    settings = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
    worker = WorkerFalso()
    sala = SalaActual(worker=cast(Any, worker) if con_worker else None)
    reloj = RelojFalso(momento)
    programador = ProgramadorTareas(settings, sala, None, MisionesLlamada(), ahora=reloj)
    return programador, worker, reloj, sala


async def tick(programador: ProgramadorTareas) -> None:
    """Una vuelta del bucle, sin el sleep."""
    programador._recargar_si_cambio()
    await programador._disparar_vencidas()


class TestElReloj:
    async def test_no_dispara_antes_de_la_hora(self, tmp_path: Path) -> None:
        programador, worker, _, _ = preparar(tmp_path, tarea())
        await tick(programador)
        assert worker.frames == []

    async def test_dispara_a_la_hora(self, tmp_path: Path) -> None:
        programador, worker, reloj, _ = preparar(tmp_path, tarea())
        await tick(programador)
        reloj.momento = datetime(2026, 8, 5, 8, 0, 10)
        await tick(programador)
        assert len(worker.frames) == 1
        assert [e["resultado"] for e in bitacora(tmp_path)] == ["hablado"]

    async def test_no_dispara_dos_veces_el_mismo_minuto(self, tmp_path: Path) -> None:
        programador, worker, reloj, _ = preparar(tmp_path, tarea())
        await tick(programador)
        reloj.momento = datetime(2026, 8, 5, 8, 0, 10)
        await tick(programador)
        reloj.momento = datetime(2026, 8, 5, 8, 0, 40)
        await tick(programador)
        assert len(worker.frames) == 1

    async def test_arrancar_tarde_no_recupera_el_disparo(self, tmp_path: Path) -> None:
        # El agente estuvo apagado a las 8: esa ejecución se pierde, por diseño.
        programador, worker, _, _ = preparar(tmp_path, tarea(), momento=datetime(2026, 8, 5, 9, 0))
        await tick(programador)
        assert worker.frames == []
        assert bitacora(tmp_path) == []

    async def test_una_edicion_no_dispara_al_pasado(self, tmp_path: Path) -> None:
        programador, worker, reloj, _ = preparar(tmp_path, tarea())
        await tick(programador)
        reloj.momento = datetime(2026, 8, 5, 9, 0)  # las 8 ya pasaron sin agente "encendido"...
        escribir_tareas(tmp_path, tarea(mision="Editada."))  # ...y alguien edita la tarea
        await tick(programador)
        assert worker.frames == []

    async def test_deshabilitada_no_corre(self, tmp_path: Path) -> None:
        programador, worker, reloj, _ = preparar(tmp_path, tarea(habilitada=False))
        await tick(programador)
        reloj.momento = datetime(2026, 8, 5, 8, 0, 10)
        await tick(programador)
        assert worker.frames == []

    async def test_habilitarla_desde_el_panel_entra_en_caliente(self, tmp_path: Path) -> None:
        programador, worker, reloj, _ = preparar(tmp_path, tarea(habilitada=False))
        await tick(programador)
        escribir_tareas(tmp_path, tarea())  # el panel la habilita y reexporta
        reloj.momento = datetime(2026, 8, 5, 7, 59, 30)
        await tick(programador)  # la recarga entra antes de la hora
        reloj.momento = datetime(2026, 8, 5, 8, 0, 10)
        await tick(programador)
        assert len(worker.frames) == 1


class TestLaFormaDelFrame:
    async def test_un_solo_frame_de_datos_con_turno(self, tmp_path: Path) -> None:
        programador, worker, reloj, _ = preparar(tmp_path, tarea(guardar_respuestas=True))
        await tick(programador)
        reloj.momento = datetime(2026, 8, 5, 8, 0, 10)
        await tick(programador)

        (frame,) = worker.frames
        assert isinstance(frame, LLMMessagesAppendFrame)
        # La lección de telefonia_anuncios: desde fuera del pipeline, solo
        # frames de datos. Y aquí, a diferencia de allí, con turno: la misión
        # es hablar sin que nadie te haya hablado.
        assert isinstance(frame, DataFrame)
        assert not isinstance(frame, SystemFrame)
        assert frame.run_llm is True

        mensaje = cast(dict[str, str], frame.messages[0])
        assert len(frame.messages) == 1
        assert mensaje["role"] == "system"
        assert "Recuérdale a Nora la pastilla." in mensaje["content"]
        assert "id_tarea='pastillas'" in mensaje["content"]

    def test_sin_cuestionario_no_se_menciona_la_herramienta(self) -> None:
        texto = instruccion_mision_sala(tarea())
        assert "guardar_respuestas" not in texto


class TestLaSalaOcupada:
    async def test_sin_worker_se_aplaza(self, tmp_path: Path) -> None:
        programador, worker, reloj, _ = preparar(tmp_path, tarea(), con_worker=False)
        await tick(programador)
        reloj.momento = datetime(2026, 8, 5, 8, 0, 10)
        await tick(programador)
        assert worker.frames == []
        assert bitacora(tmp_path) == []  # aplazada, no perdida

    async def test_el_aplazamiento_se_agota_y_queda_anotado(self, tmp_path: Path) -> None:
        programador, worker, reloj, _ = preparar(tmp_path, tarea(), con_worker=False)
        await tick(programador)
        reloj.momento = datetime(2026, 8, 5, 8, 0) + timedelta(seconds=ESPERA_OCUPADO_MAX_SECS + 30)
        await tick(programador)
        assert worker.frames == []
        assert [e["resultado"] for e in bitacora(tmp_path)] == ["sin_sala"]

    async def test_la_sala_vuelve_y_la_mision_aplazada_sale(self, tmp_path: Path) -> None:
        programador, worker, reloj, sala = preparar(tmp_path, tarea(), con_worker=False)
        await tick(programador)
        reloj.momento = datetime(2026, 8, 5, 8, 1)
        await tick(programador)  # aplazada: sin tarjeta
        sala.worker = cast(Any, worker)  # la tarjeta vuelve dentro del margen
        reloj.momento = datetime(2026, 8, 5, 8, 3)
        await tick(programador)
        assert len(worker.frames) == 1

    async def test_con_llamada_en_curso_se_aplaza(self, tmp_path: Path) -> None:
        class GateRetenida:
            retenida = True

        programador, worker, reloj, sala = preparar(tmp_path, tarea())
        sala.gate = cast(Any, GateRetenida())
        await tick(programador)
        reloj.momento = datetime(2026, 8, 5, 8, 0, 10)
        await tick(programador)
        assert worker.frames == []


class TestElBucle:
    async def test_una_excepcion_no_mata_el_planificador(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        programador, _, _, _ = preparar(tmp_path, tarea())
        fallos = {"restantes": 2}

        def recarga_rota() -> None:
            if fallos["restantes"]:
                fallos["restantes"] -= 1
                raise RuntimeError("disco en llamas")

        monkeypatch.setattr(programador, "_recargar_si_cambio", recarga_rota)
        monkeypatch.setattr(tareas_programadas, "INTERVALO_TICK_SECS", 0.01)

        corredor = asyncio.create_task(programador.correr())
        await asyncio.sleep(0.1)
        assert not corredor.done()  # sobrevivió a los dos fallos
        corredor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await corredor

    async def test_sin_fichero_no_pasa_nada(self, tmp_path: Path) -> None:
        settings = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
        programador = ProgramadorTareas(settings, SalaActual(), None, MisionesLlamada())
        await tick(programador)  # ni fichero ni tareas: la vuelta es un no-op
