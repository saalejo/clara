"""Tests del arnés de Clara: traducción de esquemas y bucle de herramientas.

Todo con un cliente de LLM falso y guionizado: se prueba que Clara despacha bien
las herramientas reales y que sus esquemas se traducen a lo que espera Gemini,
sin tocar la red.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from google.genai import types

from voice_agent.calidad.arnes import MAX_RONDAS_HERRAMIENTAS, ArnesClara, _declaraciones_de
from voice_agent.calidad.cliente import ClienteLLM
from voice_agent.resources import AppResources
from voice_agent.tools import HERRAMIENTAS, herramientas_activas, nombre_de, registrar_alerta
from voice_agent.traza import TrazaLlamada
from voice_agent_core.config import Settings
from voice_agent_core.rutas import dir_alertas


class RetrieverFalso:
    """Doble mínimo del buscador; el arnés no lo usa en estos tests."""

    def buscar(self, consulta: str, *, top_k: int | None = None) -> list[Any]:
        return []

    def temas_disponibles(self) -> list[str]:
        return []


@dataclass
class _LlamadaFalsa:
    name: str
    args: dict[str, Any]


@dataclass
class _RespuestaFalsa:
    text: str | None = None
    function_calls: list[_LlamadaFalsa] = field(default_factory=list)

    @property
    def candidates(self) -> list[Any]:
        parte = types.Part.from_text(text=self.text or "")
        return [type("Candidato", (), {"content": types.Content(role="model", parts=[parte])})()]

    usage_metadata: Any = None


class ClienteFalso:
    """Cliente de LLM guionizado: devuelve las respuestas en orden."""

    def __init__(self, guion: list[_RespuestaFalsa]) -> None:
        self._guion = guion
        self.llamadas = 0

    async def generar(self, *, modelo: str, contents: Any, config: Any) -> Any:
        respuesta = self._guion[self.llamadas]
        self.llamadas += 1
        return respuesta


def _recursos(data_dir: Path, id_llamada: str = "calidad-prueba") -> AppResources:
    return AppResources(
        settings=Settings(_env_file=None, data_dir=data_dir),  # type: ignore[call-arg]
        retriever=cast(Any, RetrieverFalso()),
        traza=TrazaLlamada(data_dir, id_llamada=id_llamada),
    )


def _arnes(cliente: ClienteLLM, recursos: AppResources, herramientas: list[Any]) -> ArnesClara:
    return ArnesClara(
        cliente=cliente,
        settings=recursos.settings,
        prompt_sistema="Eres Clara.",
        herramientas=herramientas,
        recursos=recursos,
    )


def test_declaraciones_se_traducen_desde_el_esquema_pipecat() -> None:
    herramientas = herramientas_activas(frozenset())
    declaraciones = _declaraciones_de(herramientas)
    assert len(declaraciones) == len(herramientas)
    nombres = {d.name for d in declaraciones}
    assert nombres == {nombre_de(h) for h in herramientas}
    for d in declaraciones:
        assert d.description, f"{d.name} sin descripción"
        # `parameters_json_schema` es el esquema JSON crudo que ve el modelo.
        esquema = d.parameters_json_schema
        assert isinstance(esquema, dict)
        assert esquema.get("type") == "object"


@pytest.mark.asyncio
async def test_clara_despacha_una_herramienta_real_y_contesta(tmp_path: Path) -> None:
    recursos = _recursos(tmp_path)
    guion = [
        _RespuestaFalsa(
            function_calls=[
                _LlamadaFalsa(
                    name="registrar_alerta",
                    args={
                        "nivel": "rojo",
                        "sintomas": "fiebre alta y herida supurando",
                        "justificacion": "bandera roja",
                    },
                )
            ]
        ),
        _RespuestaFalsa(text="Debe acudir a urgencias ahora mismo."),
    ]
    cliente = ClienteFalso(guion)
    arnes = _arnes(cliente, recursos, [registrar_alerta])

    historial = [types.Content(role="user", parts=[types.Part.from_text(text="me siento fatal")])]
    texto, turnos = await arnes.responder(historial)

    assert texto == "Debe acudir a urgencias ahora mismo."
    assert [t.rol for t in turnos] == ["herramienta"]
    assert turnos[0].texto == "registrar_alerta"
    # La herramienta REAL escribió la alerta en el sandbox.
    alertas = list(dir_alertas(tmp_path).glob("*.json"))
    assert len(alertas) == 1
    # El resultado de la herramienta viajó de vuelta al modelo.
    assert cast("dict[str, Any]", turnos[0].detalle["resultado"])["registrada"] is True
    # El historial quedó con: modelo(function_call) + user(function_response) + modelo(texto).
    assert len(historial) == 4


@pytest.mark.asyncio
async def test_una_herramienta_desconocida_no_rompe_el_turno(tmp_path: Path) -> None:
    recursos = _recursos(tmp_path)
    guion = [
        _RespuestaFalsa(function_calls=[_LlamadaFalsa(name="herramienta_fantasma", args={})]),
        _RespuestaFalsa(text="Sigo aquí."),
    ]
    arnes = _arnes(ClienteFalso(guion), recursos, [registrar_alerta])
    texto, turnos = await arnes.responder(
        [types.Content(role="user", parts=[types.Part.from_text(text="hola")])]
    )
    assert texto == "Sigo aquí."
    assert "error" in cast("dict[str, Any]", turnos[0].detalle["resultado"])


@pytest.mark.asyncio
async def test_el_tope_de_rondas_corta_el_bucle(tmp_path: Path) -> None:
    recursos = _recursos(tmp_path)
    # Siempre pide herramienta, nunca contesta: debe cortarse en el tope.
    guion = [
        _RespuestaFalsa(
            function_calls=[
                _LlamadaFalsa(
                    name="registrar_alerta",
                    args={"nivel": "verde", "sintomas": "x", "justificacion": "y"},
                )
            ]
        )
        for _ in range(MAX_RONDAS_HERRAMIENTAS + 2)
    ]
    arnes = _arnes(ClienteFalso(guion), recursos, [registrar_alerta])
    texto, turnos = await arnes.responder(
        [types.Content(role="user", parts=[types.Part.from_text(text="hola")])]
    )
    assert texto == ""
    assert len(turnos) == MAX_RONDAS_HERRAMIENTAS


def test_el_catalogo_completo_se_traduce_sin_error() -> None:
    # Guarda contra una herramienta cuyo esquema Pipecat no encaje en Gemini.
    declaraciones = _declaraciones_de(list(HERRAMIENTAS))
    assert len(declaraciones) == len(HERRAMIENTAS)
