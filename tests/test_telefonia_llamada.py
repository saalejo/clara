"""Tests de la precarga de servicios de llamada: el banco de muletillas.

Lo que se fija aquí es el contrato del preloader: el banco se prepara junto a
los modelos —nunca durante la llamada—, a la frecuencia del pipeline de
llamada, una sola vez, y solo si las muletillas están activadas. Los
constructores de servicios se sustituyen porque cargan modelos reales y la
suite corre sin red.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from voice_agent import telefonia_llamada
from voice_agent.fillers import FillerBank
from voice_agent.telefonia_codec import FRECUENCIA_PIPELINE
from voice_agent.telefonia_llamada import ServiciosDeLlamada
from voice_agent_core.config import Settings
from voice_agent_core.runtime import RuntimeConfig


def _settings(tmp_path: Path, **kwargs: object) -> Settings:
    ajustes: dict[str, object] = {"data_dir": tmp_path}
    ajustes.update(kwargs)
    return Settings(_env_file=None, **ajustes)  # type: ignore[arg-type, call-arg]


class _CompletionsFalsas:
    """Imita `AsyncOpenAI().chat.completions`, lo único que toca `_calentar_llm`."""

    def __init__(self, registro: list[dict[str, Any]], *, fallar: bool) -> None:
        self._registro = registro
        self._fallar = fallar

    async def create(self, **kwargs: Any) -> None:
        self._registro.append(kwargs)
        if self._fallar:
            raise RuntimeError("sin red")


class _ChatFalso:
    def __init__(self, registro: list[dict[str, Any]], *, fallar: bool) -> None:
        self.completions = _CompletionsFalsas(registro, fallar=fallar)


class _ClienteFalso:
    def __init__(self, registro: list[dict[str, Any]], *, fallar: bool) -> None:
        self.chat = _ChatFalso(registro, fallar=fallar)


class _LLMFalso:
    """Imita un `OpenRouterLLMService`: solo hace falta `._client.chat.completions`."""

    def __init__(self, registro: list[dict[str, Any]], *, fallar: bool = False) -> None:
        self._client = _ClienteFalso(registro, fallar=fallar)


@pytest.fixture
def preparadas(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, list[str]]]:
    """Sustituye los constructores de servicios y la síntesis del banco.

    Devuelve la lista de diccionarios de frases con los que se llamó a
    `FillerBank.preparar`, para poder afirmar cuántas veces y con qué.
    """
    monkeypatch.setattr(telefonia_llamada, "build_stt", lambda s, sample_rate=None: "stt")
    monkeypatch.setattr(telefonia_llamada, "build_llm", lambda s: "llm")
    monkeypatch.setattr(telefonia_llamada, "build_tts", lambda s, sample_rate=None: "tts")
    monkeypatch.setattr(telefonia_llamada, "Retriever", lambda s: "retriever")
    llamadas: list[dict[str, list[str]]] = []
    monkeypatch.setattr(FillerBank, "preparar", lambda self, frases: llamadas.append(frases))
    return llamadas


async def test_la_precarga_prepara_el_banco(
    tmp_path: Path, preparadas: list[dict[str, list[str]]]
) -> None:
    runtime = RuntimeConfig()
    servicios = ServiciosDeLlamada(_settings(tmp_path), runtime)
    servicios.precargar()
    assert await servicios.tomar() == ("stt", "llm", "tts")
    banco = servicios.banco
    assert banco is not None
    # A la frecuencia del pipeline de llamada, no a la del micrófono de la sala.
    assert banco._sample_rate == FRECUENCIA_PIPELINE
    assert preparadas == [runtime.prompt.muletillas]
    # El RAG también queda precargado, listo para las herramientas de la llamada.
    assert servicios.recursos is not None
    # El fixture sustituye Retriever por un centinela de texto.
    assert servicios.recursos.retriever == "retriever"  # type: ignore[comparison-overlap]


async def test_sin_indice_se_atiende_sin_herramientas(
    tmp_path: Path,
    preparadas: list[dict[str, list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un índice ausente no puede dejar el contestador mudo: se atiende sin RAG."""

    def revienta(s: object) -> object:
        raise FileNotFoundError("sin índice")

    monkeypatch.setattr(telefonia_llamada, "Retriever", revienta)
    servicios = ServiciosDeLlamada(_settings(tmp_path), RuntimeConfig())
    servicios.precargar()
    assert await servicios.tomar() == ("stt", "llm", "tts")
    assert servicios.recursos is None


async def test_el_banco_se_prepara_una_sola_vez(
    tmp_path: Path, preparadas: list[dict[str, list[str]]]
) -> None:
    """Los bytes cacheados sirven para todas las llamadas; solo el trío se repone."""
    servicios = ServiciosDeLlamada(_settings(tmp_path), RuntimeConfig())
    servicios.precargar()
    await servicios.tomar()
    banco = servicios.banco
    servicios.precargar()
    await servicios.tomar()
    assert servicios.banco is banco
    assert len(preparadas) == 1


async def test_con_muletillas_desactivadas_no_hay_banco(
    tmp_path: Path, preparadas: list[dict[str, list[str]]]
) -> None:
    servicios = ServiciosDeLlamada(_settings(tmp_path, filler_enabled=False), RuntimeConfig())
    servicios.precargar()
    assert await servicios.tomar() == ("stt", "llm", "tts")
    assert servicios.banco is None
    assert preparadas == []


class TestElCalentamientoDelLLM:
    """La precarga abre la conexión al LLM antes de que suene el teléfono.

    Medido con una llamada real: la primera respuesta de una conexión nueva
    tarda ~2 s más que la siguiente, con la misma conexión ya abierta. Una
    petición mínima aquí evita que ese coste lo pague quien está al teléfono.
    """

    async def test_precargar_manda_una_peticion_minima(
        self,
        tmp_path: Path,
        preparadas: list[dict[str, list[str]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        peticiones: list[dict[str, Any]] = []
        monkeypatch.setattr(telefonia_llamada, "build_llm", lambda s: _LLMFalso(peticiones))
        settings = _settings(tmp_path)

        servicios = ServiciosDeLlamada(settings, RuntimeConfig())
        servicios.precargar()
        await servicios.tomar()

        (peticion,) = peticiones
        assert peticion["model"] == settings.llm_model_efectivo
        assert peticion["max_tokens"] == 1

    async def test_un_calentamiento_fallido_no_impide_precargar(
        self,
        tmp_path: Path,
        preparadas: list[dict[str, list[str]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Sin red durante la precarga: la llamada de verdad, minutos después,
        # tiene que poder atenderse igual — simplemente sin haber calentado.
        peticiones: list[dict[str, Any]] = []
        monkeypatch.setattr(
            telefonia_llamada, "build_llm", lambda s: _LLMFalso(peticiones, fallar=True)
        )
        servicios = ServiciosDeLlamada(_settings(tmp_path), RuntimeConfig())
        servicios.precargar()

        stt, llm, tts = await servicios.tomar()
        assert stt == "stt"
        assert tts == "tts"
        assert isinstance(llm, _LLMFalso)
