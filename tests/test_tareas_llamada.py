"""Las misiones de llamada: marcar, correlar el SCO y el prompt del pipeline.

La pieza delicada es la correlación: el handoff SCO no dice de qué llamada
viene, así que `MisionesLlamada` solo entrega la misión si SU llamada está en
curso en ese instante. Una entrante que se cruce con la ventana no puede
llevarse el prompt de la misión.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

from voice_agent import tareas_programadas
from voice_agent.tareas_programadas import (
    MisionesLlamada,
    MisionPendiente,
    ProgramadorTareas,
    SalaActual,
)
from voice_agent.telefonia import ErrorTelefonia
from voice_agent.telefonia_llamada import (
    PROMPT_LLAMADA,
    _prompt_de_llamada,
)
from voice_agent_core.config import Settings
from voice_agent_core.runtime import RuntimeConfig
from voice_agent_core.tareas import TareaProgramada, TipoTarea
from voice_agent_core.telefonia import EstadoLlamada, EstadoTelefonia, Llamada


def tarea_llamada(**cambios: Any) -> TareaProgramada:
    base: dict[str, Any] = {
        "id": "revision-abuela",
        "tipo": TipoTarea.LLAMADA,
        "cron": "0 17 * * *",
        "mision": "Pregúntale cómo se encuentra y si necesita algo.",
        "contacto_nombre": "Abuela",
        "contacto_numero": "+573001234567",
    }
    base.update(cambios)
    return TareaProgramada.model_validate(base)


def llamada(id_: str = "voicecall01", estado: EstadoLlamada = EstadoLlamada.EN_CURSO) -> Llamada:
    return Llamada(id=id_, estado=estado, numero="+573001234567", entrante=False)


class ClienteFalso:
    """Un puente de mentira: se le programa la secuencia de estados."""

    def __init__(self, estados: list[list[Llamada]] | None = None) -> None:
        self.estados = estados or []
        self.marcadas: list[str] = []
        self.colgadas: list[str | None] = []

    async def marcar(self, numero: str) -> Llamada:
        self.marcadas.append(numero)
        return llamada(estado=EstadoLlamada.SONANDO)

    async def colgar(self, id_llamada: str | None = None) -> None:
        self.colgadas.append(id_llamada)

    async def estado(self) -> EstadoTelefonia:
        llamadas = self.estados.pop(0) if self.estados else []
        return EstadoTelefonia(disponible=True, llamadas=llamadas)


class TestElPrompt:
    def test_una_entrante_lleva_el_prompt_de_siempre(self) -> None:
        texto = _prompt_de_llamada(RuntimeConfig(), None)
        assert texto.endswith(PROMPT_LLAMADA)

    def test_una_mision_sustituye_el_guion_de_entrante(self) -> None:
        mision = MisionPendiente(tarea=tarea_llamada(), id_llamada="voicecall01")
        texto = _prompt_de_llamada(RuntimeConfig(), mision)
        assert "Estás atendiendo una llamada" not in texto  # el guion de entrante
        assert "acabas de llamar a Abuela" in texto
        assert "Pregúntale cómo se encuentra" in texto
        assert "guardar_respuestas" not in texto  # no es un cuestionario

    def test_un_cuestionario_pide_guardar(self) -> None:
        mision = MisionPendiente(
            tarea=tarea_llamada(guardar_respuestas=True), id_llamada="voicecall01"
        )
        texto = _prompt_de_llamada(RuntimeConfig(), mision)
        assert "id_tarea='revision-abuela'" in texto


class TestLaCorrelacion:
    async def test_consume_solo_si_su_llamada_esta_en_curso(self) -> None:
        misiones = MisionesLlamada()
        misiones.registrar(tarea_llamada(), "voicecall01")
        cliente = ClienteFalso(estados=[[llamada()]])

        mision = await misiones.tomar_si_en_curso(cast(Any, cliente))
        assert mision is not None and mision.tarea.id == "revision-abuela"
        # Consumida una vez, no se entrega dos veces.
        assert await misiones.tomar_si_en_curso(cast(Any, cliente)) is None

    async def test_reintenta_hasta_que_confirma_en_curso(self) -> None:
        # El caso real que motivó los reintentos: el SCO llega con la llamada
        # todavía en SONANDO —oFono y el audio no cambian en el mismo
        # instante— y la confirmación EN_CURSO tarda un par de sondeos.
        misiones = MisionesLlamada()
        misiones.registrar(tarea_llamada(), "voicecall01")
        cliente = ClienteFalso(
            estados=[
                [llamada(estado=EstadoLlamada.SONANDO)],
                [llamada(estado=EstadoLlamada.SONANDO)],
                [llamada()],
            ]
        )
        mision = await misiones.tomar_si_en_curso(cast(Any, cliente))
        assert mision is not None and mision.tarea.id == "revision-abuela"

    async def test_sonando_todavia_no_es_en_curso(self) -> None:
        # Si nunca llega a EN_CURSO, se agotan los reintentos y se rinde.
        misiones = MisionesLlamada()
        misiones.registrar(tarea_llamada(), "voicecall01")
        cliente = ClienteFalso(estados=[[llamada(estado=EstadoLlamada.SONANDO)]])
        assert await misiones.tomar_si_en_curso(cast(Any, cliente)) is None

    async def test_una_entrante_no_se_lleva_la_mision(self) -> None:
        # Suena el SCO de una ENTRANTE contestada mientras nuestra saliente
        # sigue sonando: el id no casa y la misión se queda quieta.
        misiones = MisionesLlamada()
        misiones.registrar(tarea_llamada(), "voicecall01")
        cliente = ClienteFalso(estados=[[llamada(id_="voicecall02")]])
        assert await misiones.tomar_si_en_curso(cast(Any, cliente)) is None

    async def test_caducada_se_descarta(self) -> None:
        misiones = MisionesLlamada()
        pendiente = misiones.registrar(tarea_llamada(), "voicecall01")
        pendiente.creada_en = time.monotonic() - tareas_programadas.CADUCIDAD_MISION_SECS - 1
        cliente = ClienteFalso(estados=[[llamada()]])
        assert await misiones.tomar_si_en_curso(cast(Any, cliente)) is None

    async def test_sin_puente_no_hay_mision(self) -> None:
        misiones = MisionesLlamada()
        misiones.registrar(tarea_llamada(), "voicecall01")
        assert await misiones.tomar_si_en_curso(None) is None

    async def test_el_puente_caido_no_consume(self) -> None:
        class ClienteRoto:
            async def estado(self) -> EstadoTelefonia:
                raise ErrorTelefonia("sin puente")

        misiones = MisionesLlamada()
        misiones.registrar(tarea_llamada(), "voicecall01")
        assert await misiones.tomar_si_en_curso(cast(Any, ClienteRoto())) is None


def preparar_programador(
    tmp_path: Path, cliente: Any, misiones: MisionesLlamada | None = None
) -> ProgramadorTareas:
    settings = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
    return ProgramadorTareas(
        settings,
        SalaActual(),
        cast(Any, cliente),
        misiones or MisionesLlamada(),
        ahora=lambda: datetime(2026, 8, 5, 17, 0, 10),
    )


def resultados(tmp_path: Path) -> list[str]:
    import json

    from voice_agent_core.rutas import ruta_bitacora_tareas

    ruta = ruta_bitacora_tareas(tmp_path)
    if not ruta.is_file():
        return []
    return [json.loads(x)["resultado"] for x in ruta.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(autouse=True)
def _tiempos_cortos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los sondeos y timeouts reales son de segundos; aquí, de milisegundos."""
    monkeypatch.setattr(tareas_programadas, "SONDEO_LLAMADA_SECS", 0.01)
    # Holgado frente a los 0.01 del sondeo: en una placa cargada, un timeout
    # demasiado justo colgaría la llamada del test antes de "descolgarla".
    monkeypatch.setattr(tareas_programadas, "TIMEOUT_SALIENTE_SECS", 0.3)
    monkeypatch.setattr(tareas_programadas, "ESPERA_ENTRE_REINTENTOS_SECS", 0.001)


class TestElFlujoDeLlamada:
    async def test_nadie_contesta_cuelga_y_anota(self, tmp_path: Path) -> None:
        # La llamada suena y suena: pasado el timeout, se cuelga y se anota.
        cliente = ClienteFalso(estados=[[llamada(estado=EstadoLlamada.SONANDO)]] * 50)
        programador = preparar_programador(tmp_path, cliente)

        await programador._ejecutar_llamada(tarea_llamada(), datetime(2026, 8, 5, 17, 0))

        assert cliente.marcadas == ["+573001234567"]
        assert cliente.colgadas == ["voicecall01"]
        assert resultados(tmp_path) == ["sin_respuesta"]

    async def test_contestada_espera_el_final_y_anota(self, tmp_path: Path) -> None:
        # El papel del callback del SCO, condensado en el propio sondeo: en
        # cuanto la llamada aparece EN_CURSO, la misión se consume — que es lo
        # que haría `_al_llegar_audio` al recibir el socket en ese momento.
        misiones = MisionesLlamada()

        class ClienteQueDescuelga(ClienteFalso):
            async def estado(self) -> EstadoTelefonia:
                est = await super().estado()
                if any(c.estado is EstadoLlamada.EN_CURSO for c in est.llamadas):
                    await misiones.tomar_si_en_curso(cast(Any, ClienteFalso([est.llamadas])))
                return est

        cliente = ClienteQueDescuelga(
            estados=[
                [llamada(estado=EstadoLlamada.SONANDO)],
                [llamada()],  # descolgada: aquí llegaría el SCO
                [llamada()],
                [],  # colgaron
            ]
        )
        programador = preparar_programador(tmp_path, cliente, misiones)
        await programador._ejecutar_llamada(tarea_llamada(), datetime(2026, 8, 5, 17, 0))

        assert cliente.colgadas == []  # colgó el otro lado, no el timeout
        assert resultados(tmp_path) == ["llamada_contestada"]

    async def test_desaparece_sin_consumirse_es_sin_respuesta(self, tmp_path: Path) -> None:
        # Rechazada al segundo timbre: la llamada se esfuma sin SCO.
        cliente = ClienteFalso(estados=[[llamada(estado=EstadoLlamada.SONANDO)], []])
        programador = preparar_programador(tmp_path, cliente)
        await programador._ejecutar_llamada(tarea_llamada(), datetime(2026, 8, 5, 17, 0))
        assert resultados(tmp_path) == ["sin_respuesta"]

    async def test_marcar_falla_y_queda_anotado(self, tmp_path: Path) -> None:
        class ClienteQueNoMarca:
            async def marcar(self, numero: str) -> Llamada:
                raise ErrorTelefonia("el puente no está")

        programador = preparar_programador(tmp_path, ClienteQueNoMarca())
        await programador._ejecutar_llamada(tarea_llamada(), datetime(2026, 8, 5, 17, 0))
        assert resultados(tmp_path) == ["error"]

    async def test_sin_puente_es_error(self, tmp_path: Path) -> None:
        programador = preparar_programador(tmp_path, None)
        programador._telefonia = None
        await programador._ejecutar_llamada(tarea_llamada(), datetime(2026, 8, 5, 17, 0))
        assert resultados(tmp_path) == ["error"]
