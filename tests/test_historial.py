"""El historial de pacientes: la memoria entre llamadas por número.

Lo delicado no es el SQL, sino los contratos: los números de relleno no abren
ficha (mezclarían pacientes), la ficha del prompt habla solo de llamadas
ANTERIORES, y nada de aquí puede lanzar hacia una llamada en curso — una base
corrupta degrada a "sin historial".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from pipecat.services.llm_service import FunctionCallParams

from voice_agent.resources import AppResources
from voice_agent.tareas_programadas import MisionPendiente
from voice_agent.telefonia_llamada import _prompt_de_llamada
from voice_agent.tools.evaluacion import finalizar_llamada, registrar_alerta
from voice_agent.tools.historial import historial_paciente
from voice_agent.traza import TrazaLlamada
from voice_agent_core.config import Settings
from voice_agent_core.historial import HistorialPacientes, numero_identificable
from voice_agent_core.runtime import RuntimeConfig
from voice_agent_core.tareas import TareaProgramada, TipoTarea

NUMERO = "3046411802"


def historial_en(tmp_path: Path) -> HistorialPacientes:
    return HistorialPacientes(tmp_path / "historial.sqlite3")


def registrar(
    historial: HistorialPacientes,
    id_llamada: str = "llamada-1",
    numero: str = NUMERO,
    **extra: Any,
) -> None:
    historial.registrar_llamada(id_llamada, numero, "entrante", **extra)


class TestElRegistro:
    def test_una_llamada_abre_ficha(self, tmp_path: Path) -> None:
        historial = historial_en(tmp_path)
        registrar(historial, nombre="Nora Pérez", momento=datetime(2026, 8, 10, 14, 0))

        ficha = historial.ficha(NUMERO)
        assert ficha is not None
        assert ficha.nombre == "Nora Pérez"
        assert ficha.total_llamadas == 1
        assert ficha.ultima.momento == "2026-08-10T14:00:00"
        assert ficha.ultima.direccion == "entrante"

    def test_quien_nunca_llamo_no_tiene_ficha(self, tmp_path: Path) -> None:
        assert historial_en(tmp_path).ficha("3000000000") is None

    def test_los_numeros_de_relleno_no_abren_ficha(self, tmp_path: Path) -> None:
        # El 10000000 de WhatsApp identificaría a TODAS las llamadas de app
        # como el mismo paciente; el número oculto, igual.
        historial = historial_en(tmp_path)
        registrar(historial, numero="10000000")
        registrar(historial, id_llamada="llamada-2", numero="número oculto")
        registrar(historial, id_llamada="llamada-3", numero="  ")

        assert historial.ficha("10000000") is None
        assert historial.pacientes() == []
        assert not numero_identificable("10000000")
        assert numero_identificable(NUMERO)

    def test_el_nombre_no_se_borra_si_luego_llega_vacio(self, tmp_path: Path) -> None:
        # La agenda PBAP puede no estar descargada en la segunda llamada.
        historial = historial_en(tmp_path)
        registrar(historial, nombre="Nora Pérez")
        registrar(historial, id_llamada="llamada-2", nombre="")

        ficha = historial.ficha(NUMERO)
        assert ficha is not None
        assert ficha.nombre == "Nora Pérez"
        assert ficha.total_llamadas == 2

    def test_las_anotaciones_caen_en_su_llamada(self, tmp_path: Path) -> None:
        historial = historial_en(tmp_path)
        registrar(historial, momento=datetime(2026, 8, 10, 14, 0))
        registrar(historial, id_llamada="llamada-2", momento=datetime(2026, 8, 10, 15, 0))

        historial.anotar_alerta("llamada-2", "amarillo")
        historial.anotar_resumen(
            "llamada-2",
            paciente_y_procedimiento="Nora, colecistectomía hace 5 días",
            decision="Triaje amarillo; el equipo la contacta mañana",
            proximos_pasos="Vigilar la fiebre",
        )

        ficha = historial.ficha(NUMERO)
        assert ficha is not None
        assert ficha.ultima.id_llamada == "llamada-2"
        assert ficha.ultima.nivel == "amarillo"
        assert "colecistectomía" in ficha.ultima.paciente_y_procedimiento
        primera = historial.llamadas(NUMERO)[-1]
        assert primera.nivel == ""  # la anotación no se desparramó

    def test_anotar_sin_ficha_no_hace_nada_ni_lanza(self, tmp_path: Path) -> None:
        # El caso del navegador: alertas con id pero sin llamada registrada.
        historial = historial_en(tmp_path)
        historial.anotar_alerta("llamada-web", "rojo")
        assert historial.llamadas() == []

    def test_una_base_corrupta_degrada_a_sin_historial(self, tmp_path: Path) -> None:
        ruta = tmp_path / "historial.sqlite3"
        ruta.write_bytes(b"esto no es una base sqlite, pero pesa mas de cien bytes" * 3)
        historial = HistorialPacientes(ruta)

        registrar(historial)  # no lanza
        assert historial.ficha(NUMERO) is None
        assert historial.pacientes() == []
        assert historial.llamadas() == []


class TestElPromptConFicha:
    def _ficha(self, tmp_path: Path) -> Any:
        historial = historial_en(tmp_path)
        registrar(historial, momento=datetime(2026, 8, 10, 14, 0))
        historial.anotar_alerta("llamada-1", "verde")
        historial.anotar_resumen(
            "llamada-1",
            paciente_y_procedimiento="Nora, apendicectomía hace 3 días",
            decision="Evolución normal",
            proximos_pasos="Llamar si hay fiebre",
        )
        return historial.ficha(NUMERO)

    def test_una_entrante_con_ficha_lleva_el_historial(self, tmp_path: Path) -> None:
        texto = _prompt_de_llamada(RuntimeConfig(), None, None, self._ficha(tmp_path))
        assert "Historial de este número" in texto
        assert "2026-08-10" in texto
        assert "apendicectomía" in texto
        assert "triaje verde" in texto
        assert "confirma" in texto  # el historial es del teléfono, no de la voz

    def test_una_mision_tambien_lleva_el_historial(self, tmp_path: Path) -> None:
        tarea = TareaProgramada(
            id="seguimiento",
            tipo=TipoTarea.LLAMADA,
            cron="0 17 * * *",
            mision="Pregunta cómo sigue.",
            contacto_numero=NUMERO,
        )
        mision = MisionPendiente(encargo=tarea, id_llamada="voicecall01")
        texto = _prompt_de_llamada(RuntimeConfig(), mision, None, self._ficha(tmp_path))
        assert "Historial de este número" in texto
        assert "Llamar si hay fiebre" in texto

    def test_sin_ficha_el_prompt_no_menciona_historial(self) -> None:
        assert "Historial" not in _prompt_de_llamada(RuntimeConfig(), None)

    def test_una_ficha_sin_resumen_lo_dice(self, tmp_path: Path) -> None:
        historial = historial_en(tmp_path)
        registrar(historial)
        texto = _prompt_de_llamada(RuntimeConfig(), None, None, historial.ficha(NUMERO))
        assert "sin resumen" in texto


@dataclass
class ParamsFalsos:
    app_resources: Any
    resultado: Any = None

    async def result_callback(self, resultado: Any) -> None:
        self.resultado = resultado


def _params(tmp_path: Path, *, numero: str = NUMERO) -> ParamsFalsos:
    recursos = AppResources(
        settings=Settings(_env_file=None, data_dir=tmp_path),  # type: ignore[call-arg]
        retriever=cast(Any, object()),
        traza=TrazaLlamada(tmp_path, id_llamada="llamada-actual"),
        historial=historial_en(tmp_path),
        numero_llamada=numero,
    )
    return ParamsFalsos(app_resources=recursos)


class TestLasHerramientasAnotan:
    async def test_la_alerta_anota_el_nivel_en_la_ficha(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        recursos: AppResources = params.app_resources
        assert recursos.historial is not None
        recursos.historial.registrar_llamada("llamada-actual", NUMERO, "entrante")

        await registrar_alerta(
            cast(FunctionCallParams, params), nivel="rojo", sintomas="x", justificacion="y"
        )

        ficha = recursos.historial.ficha(NUMERO)
        assert ficha is not None and ficha.ultima.nivel == "rojo"

    async def test_el_resumen_anota_sus_campos(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        recursos: AppResources = params.app_resources
        assert recursos.historial is not None
        recursos.historial.registrar_llamada("llamada-actual", NUMERO, "entrante")

        await finalizar_llamada(
            cast(FunctionCallParams, params),
            paciente_y_procedimiento="Nora, apendicectomía",
            sintomas="Sin fiebre",
            decision="Verde",
            proximos_pasos="Control en una semana",
        )

        ficha = recursos.historial.ficha(NUMERO)
        assert ficha is not None
        assert ficha.ultima.decision == "Verde"
        assert ficha.ultima.proximos_pasos == "Control en una semana"


class TestElRespaldoAnota:
    def test_una_llamada_caida_deja_huella_en_la_ficha(self, tmp_path: Path) -> None:
        # El paciente cuelga (o la llamada se cae) sin que el modelo llegara a
        # `finalizar_llamada`: el respaldo del desmontaje tiene que anotar la
        # ficha igual, o la próxima llamada no sabría de esta.
        from pipecat.processors.aggregators.llm_context import LLMContext

        from voice_agent.respaldo import resumen_de_respaldo

        params = _params(tmp_path)
        recursos: AppResources = params.app_resources
        assert recursos.historial is not None
        recursos.historial.registrar_llamada("llamada-actual", NUMERO, "entrante")

        contexto = LLMContext(messages=[{"role": "user", "content": "Me duele mucho."}])
        resumen_de_respaldo(recursos, contexto)

        ficha = recursos.historial.ficha(NUMERO)
        assert ficha is not None
        assert "sin despedida" in ficha.ultima.paciente_y_procedimiento
        assert "antes de registrar un triaje" in ficha.ultima.decision


class TestLaHerramientaDeConsulta:
    async def test_sin_numero_identificado_dice_ninguno(self, tmp_path: Path) -> None:
        params = _params(tmp_path, numero="")
        await historial_paciente(cast(FunctionCallParams, params))
        assert params.resultado["historial"] == "ninguno"

    async def test_la_llamada_en_curso_no_cuenta_como_anterior(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        recursos: AppResources = params.app_resources
        assert recursos.historial is not None
        recursos.historial.registrar_llamada("llamada-actual", NUMERO, "entrante")

        await historial_paciente(cast(FunctionCallParams, params))
        assert params.resultado["historial"] == "ninguno"

    async def test_devuelve_las_anteriores_con_su_triaje(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        recursos: AppResources = params.app_resources
        assert recursos.historial is not None
        recursos.historial.registrar_llamada(
            "llamada-vieja", NUMERO, "mision", momento=datetime(2026, 8, 9, 10, 0)
        )
        recursos.historial.anotar_alerta("llamada-vieja", "amarillo")
        recursos.historial.registrar_llamada("llamada-actual", NUMERO, "entrante")

        await historial_paciente(cast(FunctionCallParams, params))

        assert params.resultado["total_llamadas_anteriores"] == 1
        (anterior,) = params.resultado["llamadas"]
        assert anterior["triaje"] == "amarillo"
        assert anterior["tipo"] == "llamada que hicimos nosotros"
