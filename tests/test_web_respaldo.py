"""El resumen de respaldo cuando la llamada termina sin despedida."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from pipecat.processors.aggregators.llm_context import LLMContext

from voice_agent.resources import AppResources
from voice_agent.respaldo import resumen_de_respaldo as _resumen_de_respaldo
from voice_agent.respaldo import transcripcion_de as _transcripcion_de
from voice_agent.traza import TrazaLlamada
from voice_agent_core.config import Settings
from voice_agent_core.evaluaciones import Alerta, NivelAlerta
from voice_agent_core.rutas import dir_resumenes


def _contexto(*mensajes: dict[str, Any]) -> LLMContext:
    lista: list[Any] = [{"role": "system", "content": "reglas"}, *mensajes]
    return LLMContext(messages=lista)


def _recursos(tmp_path: Path, **cambios: Any) -> AppResources:
    return AppResources(
        settings=Settings(_env_file=None, data_dir=tmp_path),  # type: ignore[call-arg]
        retriever=cast(Any, object()),
        traza=TrazaLlamada(tmp_path, id_llamada="llamada-x"),
        **cambios,
    )


def test_la_transcripcion_filtra_sistema_y_herramientas() -> None:
    contexto = _contexto(
        {"role": "user", "content": "Hola, soy Nora."},
        {"role": "assistant", "content": "Buenos días, Nora."},
        {"role": "assistant", "content": None},
        {"role": "tool", "content": "resultado interno"},
    )
    assert _transcripcion_de(contexto) == [
        "paciente: Hola, soy Nora.",
        "agente: Buenos días, Nora.",
    ]


def test_persiste_respaldo_con_alerta_y_traza(tmp_path: Path) -> None:
    recursos = _recursos(
        tmp_path,
        ultima_alerta=Alerta(
            id_llamada="llamada-x",
            momento="2026-08-10T09:34:21",
            nivel=NivelAlerta.ROJO,
            sintomas="Dolor 6/10 y fotofobia.",
            justificacion="Signos de alarma.",
        ),
    )
    contexto = _contexto({"role": "user", "content": "Me duele mucho."})

    _resumen_de_respaldo(recursos, contexto)

    (fichero,) = list(dir_resumenes(tmp_path).iterdir())
    datos = json.loads(fichero.read_text())
    assert "respaldo" in fichero.name
    assert "rojo" in datos["decision"]
    assert datos["sintomas"] == "Dolor 6/10 y fotofobia."
    assert datos["transcripcion"] == ["paciente: Me duele mucho."]


def test_no_escribe_si_el_modelo_ya_guardo(tmp_path: Path) -> None:
    recursos = _recursos(tmp_path, resumen_guardado=True)
    _resumen_de_respaldo(recursos, _contexto({"role": "user", "content": "Hola."}))
    assert not dir_resumenes(tmp_path).exists() or not list(dir_resumenes(tmp_path).iterdir())


def test_no_escribe_si_nadie_hablo(tmp_path: Path) -> None:
    recursos = _recursos(tmp_path)
    _resumen_de_respaldo(recursos, _contexto())
    assert not dir_resumenes(tmp_path).exists() or not list(dir_resumenes(tmp_path).iterdir())
