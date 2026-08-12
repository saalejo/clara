"""Tests del ejecutor: conversación completa, aislamiento y progreso del lote.

El cliente de LLM va falseado y enrutado por el tipo de petición (Clara lleva
herramientas, el juez pide JSON, el paciente ni lo uno ni lo otro), así que se
prueba la orquestación entera sin red ni modelos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from google.genai import types

from voice_agent.calidad import ejecutor
from voice_agent.calidad.ejecutor import ejecutar_escenario, ejecutar_lote
from voice_agent_core.calidad import CategoriaEscenario, Escenario, EstadoLote, ResultadoEscenario
from voice_agent_core.config import LLMBackend, Settings
from voice_agent_core.evaluaciones import NivelAlerta
from voice_agent_core.runtime import cargar_runtime
from voice_agent_core.rutas import (
    dir_alertas,
    dir_resultados_calidad,
    dir_sandbox_calidad,
    ruta_lote_calidad,
)

pytestmark = pytest.mark.asyncio


class RetrieverFalso:
    def buscar(self, consulta: str, *, top_k: int | None = None) -> list[Any]:
        return []

    def temas_disponibles(self) -> list[str]:
        return []


@dataclass
class _Llamada:
    name: str
    args: dict[str, Any]


@dataclass
class _Resp:
    text: str | None = None
    function_calls: list[_Llamada] = field(default_factory=list)

    @property
    def candidates(self) -> list[Any]:
        parte = types.Part.from_text(text=self.text or "")
        return [type("C", (), {"content": types.Content(role="model", parts=[parte])})()]

    usage_metadata: Any = None


class ClienteRuteado:
    """Enruta cada `generar` a la cola del papel que corresponde."""

    def __init__(
        self,
        *,
        paciente: list[_Resp],
        clara: list[_Resp],
        juez: _Resp | None = None,
    ) -> None:
        self.paciente = paciente
        self.clara = clara
        self.juez = juez
        self.juez_llamado = False

    async def generar(self, *, modelo: str, contents: Any, config: Any) -> Any:
        if config.response_mime_type == "application/json":
            self.juez_llamado = True
            assert self.juez is not None
            return self.juez
        if config.tools:
            return self.clara.pop(0)
        return self.paciente.pop(0)


@pytest.fixture(autouse=True)
def _sin_pausa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ejecutor, "PAUSA_ENTRE_LLAMADAS_S", 0)


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]


def _escenario(**kwargs: Any) -> Escenario:
    base: dict[str, Any] = dict(
        id="prueba",
        categoria=CategoriaEscenario.SEGURIDAD,
        nombre="Prueba",
        descripcion="x",
        persona="Eres un paciente.",
        criterios="Clara debe portarse bien.",
        max_turnos=4,
    )
    base.update(kwargs)
    return Escenario(**base)


async def _ejecutar(
    tmp_path: Path, escenario: Escenario, cliente: ClienteRuteado
) -> ResultadoEscenario:
    settings = _settings(tmp_path)
    return await ejecutar_escenario(
        cast(Any, cliente),
        settings,
        cargar_runtime(tmp_path),
        cast(Any, RetrieverFalso()),
        escenario,
    )


async def test_conversacion_completa_escribe_resultado_y_aisla_artefactos(tmp_path: Path) -> None:
    cliente = ClienteRuteado(
        paciente=[_Resp(text="Tengo fiebre alta y la herida supura."), _Resp(text="[COLGAR]")],
        clara=[
            _Resp(
                function_calls=[
                    _Llamada(
                        "registrar_alerta",
                        {"nivel": "rojo", "sintomas": "fiebre", "justificacion": "roja"},
                    )
                ]
            ),
            _Resp(text="Acuda a urgencias ahora mismo."),
        ],
        juez=_Resp(text='{"aprobado": true, "razonamiento": "Escaló bien."}'),
    )
    resultado = await _ejecutar(tmp_path, _escenario(), cliente)

    assert resultado.estado == "completado"
    assert resultado.veredicto is not None and resultado.veredicto.aprobado is True
    # El expediente quedó en el data_dir REAL.
    assert (dir_resultados_calidad(tmp_path) / f"{resultado.id_ejecucion}.json").is_file()
    # La alerta de prueba fue al SANDBOX, no a las evaluaciones reales.
    sandbox = dir_sandbox_calidad(tmp_path) / resultado.id_ejecucion
    assert len(list(dir_alertas(sandbox).glob("*.json"))) == 1
    assert not dir_alertas(tmp_path).exists() or not list(dir_alertas(tmp_path).glob("*.json"))
    assert len(resultado.alertas) == 1
    assert resultado.uso.llamadas >= 3  # paciente + clara(x2) + juez


async def test_el_chequeo_determinista_tumba_a_un_juez_aprobador(tmp_path: Path) -> None:
    cliente = ClienteRuteado(
        paciente=[_Resp(text="No me pasa nada grave."), _Resp(text="[COLGAR]")],
        clara=[_Resp(text="De acuerdo, cuídese.")],
        juez=_Resp(text='{"aprobado": true, "razonamiento": "Bien."}'),
    )
    resultado = await _ejecutar(
        tmp_path, _escenario(espera_alerta=NivelAlerta.ROJO, max_turnos=2), cliente
    )

    assert resultado.veredicto is not None
    assert resultado.veredicto.aprobado is False
    assert resultado.veredicto.determinista is True
    assert cliente.juez_llamado is False  # no se consultó al juez


async def test_max_turnos_corta_la_conversacion(tmp_path: Path) -> None:
    cliente = ClienteRuteado(
        paciente=[_Resp(text="Hablo y hablo."), _Resp(text="Y sigo hablando.")],
        clara=[_Resp(text="Ya veo."), _Resp(text="Entiendo.")],
        juez=_Resp(text='{"aprobado": true, "razonamiento": "ok"}'),
    )
    resultado = await _ejecutar(tmp_path, _escenario(max_turnos=2), cliente)
    assert sum(1 for t in resultado.turnos if t.rol == "paciente") == 2


async def test_ejecutar_lote_publica_progreso_y_no_muere_con_groq(tmp_path: Path) -> None:
    # Con Groq el lote registra cada escenario como error, sin construir el runner.
    settings = Settings(_env_file=None, data_dir=tmp_path, llm_backend=LLMBackend.GROQ)  # type: ignore[call-arg]
    await ejecutar_lote(settings, ["inyeccion-olvida", "bandera-roja"], id_lote="lote-x")

    resultados = list(dir_resultados_calidad(tmp_path).glob("*.json"))
    assert len(resultados) == 2
    for ruta in resultados:
        assert ResultadoEscenario.model_validate_json(ruta.read_text()).estado == "error"
    lote = EstadoLote.model_validate_json(ruta_lote_calidad(tmp_path).read_text())
    assert lote.terminado is True
    assert lote.total == 2


async def test_ejecutar_lote_ignora_ids_desconocidos(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, llm_backend=LLMBackend.GROQ)  # type: ignore[call-arg]
    await ejecutar_lote(settings, ["no-existe"], id_lote="lote-y")
    assert not list(dir_resultados_calidad(tmp_path).glob("*.json"))
    lote = EstadoLote.model_validate_json(ruta_lote_calidad(tmp_path).read_text())
    assert lote.total == 0
    assert lote.terminado is True
