"""Tests del juez: el chequeo determinista y el veredicto del LLM."""

from __future__ import annotations

from typing import Any

import pytest

from voice_agent.calidad.juez import chequeo_determinista, juzgar
from voice_agent_core.calidad import CategoriaEscenario, Escenario, Turno, UsoLLM
from voice_agent_core.config import Settings
from voice_agent_core.evaluaciones import Alerta, NivelAlerta


def _escenario(espera: NivelAlerta | None) -> Escenario:
    return Escenario(
        id="prueba",
        categoria=CategoriaEscenario.RIESGO_CLINICO,
        nombre="Prueba",
        descripcion="x",
        persona="x",
        criterios="Clara debe escalar y avisar de urgencias.",
        espera_alerta=espera,
    )


def _alerta(nivel: NivelAlerta) -> Alerta:
    return Alerta(
        id_llamada="c",
        momento="2026-08-11T12:00:00",
        nivel=nivel,
        sintomas="fiebre alta",
        justificacion="y",
    )


def test_chequeo_falla_si_falta_la_alerta_exigida() -> None:
    veredicto = chequeo_determinista(_escenario(NivelAlerta.ROJO), [])
    assert veredicto is not None
    assert veredicto.aprobado is False
    assert veredicto.determinista is True


def test_chequeo_deja_pasar_si_la_alerta_esta() -> None:
    assert chequeo_determinista(_escenario(NivelAlerta.ROJO), [_alerta(NivelAlerta.ROJO)]) is None


def test_chequeo_ignora_escenarios_sin_alerta_esperada() -> None:
    assert chequeo_determinista(_escenario(None), []) is None


class ClienteJuezFalso:
    def __init__(self, respuesta_json: str) -> None:
        self._json = respuesta_json
        self.prompt_visto = ""

    async def generar(self, *, modelo: str, contents: Any, config: Any) -> Any:
        self.prompt_visto = contents[0].parts[0].text
        return type("R", (), {"text": self._json, "usage_metadata": None})()


@pytest.mark.asyncio
async def test_juzgar_pasa_criterios_y_artefactos_y_parsea_el_veredicto() -> None:
    cliente = ClienteJuezFalso('{"aprobado": false, "razonamiento": "No escaló."}')
    uso = UsoLLM()
    veredicto = await juzgar(
        cliente,
        Settings(_env_file=None),  # type: ignore[call-arg]
        _escenario(None),
        [Turno(rol="paciente", texto="tengo fiebre alta")],
        [_alerta(NivelAlerta.AMARILLO)],
        None,
        ["apendicitis/postop.pdf"],
        uso=uso,
    )
    assert veredicto.aprobado is False
    assert veredicto.determinista is False
    assert "escalar y avisar de urgencias" in cliente.prompt_visto  # los criterios
    assert "fiebre alta" in cliente.prompt_visto  # los síntomas de la alerta
    assert "apendicitis/postop.pdf" in cliente.prompt_visto  # los documentos
    assert uso.llamadas == 1


@pytest.mark.asyncio
async def test_juzgar_suspende_si_el_veredicto_es_ilegible() -> None:
    cliente = ClienteJuezFalso("esto no es json")
    veredicto = await juzgar(
        cliente,
        Settings(_env_file=None),  # type: ignore[call-arg]
        _escenario(None),
        [],
        [],
        None,
        [],
        uso=UsoLLM(),
    )
    assert veredicto.aprobado is False
