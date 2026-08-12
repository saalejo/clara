"""Que una llamada nueva no le corte la palabra a la que está en curso.

Antes, cualquier oferta sin `pc_id` desconectaba la sesión viva. Eso arreglaba
un fallo real —una pestaña recargada dejaba un zombi que bloqueaba la interfaz
hasta reiniciar el servicio— pero dejaba que un tercero echara al que estuviera
hablando, en bucle. La regla nueva distingue el zombi de la llamada viva y hay
que probar las dos caras: que se desaloja al zombi, y que NO se desaloja a
quien habla.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from voice_agent.web import _esperar_hueco, _hay_llamada_viva, _vigilar_duracion
from voice_agent_core.config import Settings


class ConexionFalsa:
    """Lo justo de `SmallWebRTCConnection` para esta decisión."""

    def __init__(self, viva: bool, pc_id: str = "pc") -> None:
        self.viva = viva
        self.pc_id = pc_id
        self.desconectada = False

    def is_connected(self) -> bool:
        return self.viva

    async def disconnect(self) -> None:
        self.desconectada = True


class HandlerFalso:
    def __init__(self, *conexiones: ConexionFalsa) -> None:
        self._pcs_map = {c.pc_id: c for c in conexiones}


def test_sin_nadie_dentro_hay_hueco() -> None:
    assert not _hay_llamada_viva(HandlerFalso())  # type: ignore[arg-type]


def test_una_sesion_zombi_no_cuenta_como_llamada_viva() -> None:
    # `is_connected()` de pipecat mira la hora del último ping, no el estado de
    # aiortc: una pestaña recargada se delata en unos tres segundos.
    assert not _hay_llamada_viva(HandlerFalso(ConexionFalsa(viva=False)))  # type: ignore[arg-type]


def test_quien_esta_hablando_sí_cuenta() -> None:
    assert _hay_llamada_viva(HandlerFalso(ConexionFalsa(viva=True)))  # type: ignore[arg-type]


async def test_con_la_sesion_libre_se_entra_sin_esperar() -> None:
    handler = HandlerFalso(ConexionFalsa(viva=False))
    assert await _esperar_hueco(handler, asyncio.Lock(), 30.0)  # type: ignore[arg-type]


async def test_a_quien_habla_no_se_le_desaloja() -> None:
    handler = HandlerFalso(ConexionFalsa(viva=True))
    assert not await _esperar_hueco(handler, asyncio.Lock(), 0.3)  # type: ignore[arg-type]


async def test_la_pestaña_recargada_solo_espera_un_poco() -> None:
    # A los pocos segundos de recargar, la sesión previa deja de dar señales y
    # el recién llegado entra: sin esta espera se comería un 409.
    conexion = ConexionFalsa(viva=True)
    handler = HandlerFalso(conexion)

    async def morir_a_media_espera() -> None:
        await asyncio.sleep(0.6)
        conexion.viva = False

    tarea = asyncio.create_task(morir_a_media_espera())
    assert await _esperar_hueco(handler, asyncio.Lock(), 5.0)  # type: ignore[arg-type]
    await tarea


async def test_solo_espera_uno_a_la_vez() -> None:
    # Si no, veinte peticiones a la vez serían veinte tareas dormidas.
    handler = HandlerFalso(ConexionFalsa(viva=True))
    cerrojo = asyncio.Lock()
    await cerrojo.acquire()
    try:
        assert not await _esperar_hueco(handler, cerrojo, 30.0)  # type: ignore[arg-type]
    finally:
        cerrojo.release()


async def test_con_espera_cero_se_rechaza_en_seco() -> None:
    # Es la salida de emergencia: `WEB_ESPERA_SESION_SECS=0` en el `.env`.
    handler = HandlerFalso(ConexionFalsa(viva=True))
    assert not await _esperar_hueco(handler, asyncio.Lock(), 0.0)  # type: ignore[arg-type]


# --- El corte por duración -----------------------------------------------------


class WorkerFalso:
    def __init__(self) -> None:
        self.dicho: list[str] = []

    async def queue_frames(self, frames: list[Any]) -> None:
        self.dicho.extend(getattr(f, "text", "") for f in frames)


def _ajustes(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path,
        # El aviso va a `max - previo`: con los dos iguales no habría hueco
        # para avisar y el test estaría comprobando media función.
        web_llamada_max_secs=2,
        web_aviso_previo_secs=1,
        web_cierre_gracia_secs=0.05,
    )


async def test_avisa_se_despide_y_cuelga(tmp_path: Path) -> None:
    worker, conexion = WorkerFalso(), ConexionFalsa(viva=True)
    ajustes = _ajustes(tmp_path)
    await asyncio.wait_for(
        _vigilar_duracion(worker, conexion, ajustes, "id-1"),  # type: ignore[arg-type]
        timeout=5,
    )
    assert worker.dicho == [ajustes.web_aviso_cierre, ajustes.web_despedida_cierre]
    assert conexion.desconectada


async def test_colgar_a_tiempo_cancela_al_vigilante(tmp_path: Path) -> None:
    worker, conexion = WorkerFalso(), ConexionFalsa(viva=True)
    tarea = asyncio.create_task(
        _vigilar_duracion(worker, conexion, _ajustes(tmp_path), "id-1")  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.05)
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    assert worker.dicho == []
    assert not conexion.desconectada
