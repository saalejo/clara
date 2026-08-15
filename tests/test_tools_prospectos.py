"""Las herramientas comerciales y el cierre del pipeline en modo prospectos.

Lo delicado son los contratos: la adopción de identidad muda la conversación a
la ficha vieja, la conversación en curso no cuenta como anterior, sin almacén
todo degrada sin lanzar, y el cierre comercial NO escribe el respaldo clínico
— sembraría la página Pacientes de falsos pacientes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams

from voice_agent.resources import AppResources
from voice_agent.respaldo import cierre_de_prospecto
from voice_agent.tools.prospectos import (
    guardar_brief,
    historial_prospecto,
    identificar_prospecto,
)
from voice_agent.traza import TrazaLlamada
from voice_agent.web import galleta_de_prospecto, modo_prospectos
from voice_agent_core.config import Settings
from voice_agent_core.prospectos import AlmacenProspectos
from voice_agent_core.runtime import RuntimeConfig, ToolConfig
from voice_agent_core.rutas import dir_resumenes

ID = "a3f9c2d1e8b7460fa1b2c3d4e5f60718"


@dataclass
class ParamsFalsos:
    app_resources: Any
    resultado: Any = None

    async def result_callback(self, resultado: Any) -> None:
        self.resultado = resultado


def _params(tmp_path: Path, *, id_prospecto: str = ID, con_almacen: bool = True) -> ParamsFalsos:
    recursos = AppResources(
        settings=Settings(_env_file=None, data_dir=tmp_path),  # type: ignore[call-arg]
        retriever=cast(Any, object()),
        traza=TrazaLlamada(tmp_path, id_llamada="conv-actual"),
        prospectos=AlmacenProspectos(tmp_path / "prospectos.sqlite3") if con_almacen else None,
        id_prospecto=id_prospecto,
    )
    return ParamsFalsos(app_resources=recursos)


class TestIdentificarProspecto:
    async def test_registra_la_identidad(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        recursos: AppResources = params.app_resources
        assert recursos.prospectos is not None
        recursos.prospectos.registrar_conversacion("conv-actual", ID)

        await identificar_prospecto(
            cast(FunctionCallParams, params), nombre="Marta Ruiz", empresa="Óptica Andina"
        )

        assert params.resultado["registrado"] is True
        ficha = recursos.prospectos.ficha(ID)
        assert ficha is not None and ficha.prospecto.empresa == "Óptica Andina"

    async def test_adopta_la_ficha_de_otro_navegador(self, tmp_path: Path) -> None:
        # El mismo prospecto habló antes desde otro navegador (otro id).
        params = _params(tmp_path, id_prospecto="id-navegador-nuevo00000000000000")
        recursos: AppResources = params.app_resources
        almacen = recursos.prospectos
        assert almacen is not None
        almacen.registrar_conversacion("conv-vieja", ID, momento=datetime(2026, 8, 10, 10, 0))
        almacen.identificar(ID, nombre="Marta Ruiz", empresa="Óptica Andina")
        almacen.guardar_brief("conv-vieja", ID, necesidad="citas por voz")
        almacen.registrar_conversacion("conv-actual", "id-navegador-nuevo00000000000000")

        await identificar_prospecto(
            cast(FunctionCallParams, params), nombre="Marta Ruiz", empresa="Óptica Andina"
        )

        assert recursos.id_prospecto == ID, "la herramienta adopta la ficha vieja"
        assert params.resultado["necesidad_registrada"] == "citas por voz"
        ficha = almacen.ficha(ID)
        assert ficha is not None and ficha.total_conversaciones == 2

    async def test_sin_almacen_degrada_sin_lanzar(self, tmp_path: Path) -> None:
        params = _params(tmp_path, con_almacen=False)
        await identificar_prospecto(cast(FunctionCallParams, params), nombre="Marta")
        assert params.resultado["registrado"] is False


class TestGuardarBrief:
    async def test_guarda_y_marca_la_bandera(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        recursos: AppResources = params.app_resources
        assert recursos.prospectos is not None
        recursos.prospectos.registrar_conversacion("conv-actual", ID)

        await guardar_brief(
            cast(FunctionCallParams, params),
            empresa_y_contacto="Óptica Andina, Marta Ruiz, 3001234567",
            necesidad="Perder menos citas por llamadas sin contestar",
            caso_de_uso="Agente que agenda citas por teléfono",
            proximos_pasos="El equipo la llama el lunes",
        )

        assert params.resultado["guardado"] is True
        assert recursos.brief_guardado is True
        brief = recursos.prospectos.brief("conv-actual")
        assert brief is not None
        assert brief.caso_de_uso == "Agente que agenda citas por teléfono"

    async def test_sin_almacen_degrada_sin_lanzar(self, tmp_path: Path) -> None:
        params = _params(tmp_path, con_almacen=False)
        await guardar_brief(
            cast(FunctionCallParams, params),
            empresa_y_contacto="x",
            necesidad="x",
            caso_de_uso="x",
            proximos_pasos="x",
        )
        assert params.resultado["guardado"] is False
        recursos: AppResources = params.app_resources
        assert recursos.brief_guardado is False


class TestHistorialProspecto:
    async def test_la_conversacion_en_curso_no_cuenta_como_anterior(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        recursos: AppResources = params.app_resources
        assert recursos.prospectos is not None
        recursos.prospectos.registrar_conversacion("conv-actual", ID)

        await historial_prospecto(cast(FunctionCallParams, params))
        assert params.resultado["historial"] == "ninguno"

    async def test_devuelve_las_anteriores_con_su_brief(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        recursos: AppResources = params.app_resources
        almacen = recursos.prospectos
        assert almacen is not None
        almacen.registrar_conversacion("conv-vieja", ID, momento=datetime(2026, 8, 10, 10, 0))
        almacen.anotar_resumen("conv-vieja", "Preguntó por un agente de citas")
        almacen.guardar_brief("conv-vieja", ID, necesidad="citas por voz")
        almacen.identificar(ID, nombre="Marta", empresa="Óptica Andina")
        almacen.registrar_conversacion("conv-actual", ID)

        await historial_prospecto(cast(FunctionCallParams, params))

        assert params.resultado["total_conversaciones_anteriores"] == 1
        assert params.resultado["empresa_conocida"] == "Óptica Andina"
        (anterior,) = params.resultado["conversaciones"]
        assert "citas" in anterior["resumen"]
        assert params.resultado["ultimo_brief"]["necesidad"] == "citas por voz"

    async def test_sin_almacen_dice_ninguno(self, tmp_path: Path) -> None:
        params = _params(tmp_path, con_almacen=False)
        await historial_prospecto(cast(FunctionCallParams, params))
        assert params.resultado["historial"] == "ninguno"


class TestElCierreComercial:
    def test_anota_la_transcripcion_y_la_nota_sin_brief(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        recursos: AppResources = params.app_resources
        assert recursos.prospectos is not None
        recursos.prospectos.registrar_conversacion("conv-actual", ID)

        contexto = LLMContext(messages=[{"role": "user", "content": "Tengo una óptica."}])
        cierre_de_prospecto(recursos, contexto)

        conversacion = recursos.prospectos.conversacion("conv-actual")
        assert conversacion is not None
        assert "visitante: Tengo una óptica." in conversacion.transcripcion
        assert "sin brief" in conversacion.resumen

    def test_con_brief_no_deja_la_nota(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        recursos: AppResources = params.app_resources
        assert recursos.prospectos is not None
        recursos.prospectos.registrar_conversacion("conv-actual", ID)
        recursos.brief_guardado = True

        contexto = LLMContext(messages=[{"role": "user", "content": "Gracias, hasta luego."}])
        cierre_de_prospecto(recursos, contexto)

        conversacion = recursos.prospectos.conversacion("conv-actual")
        assert conversacion is not None
        assert conversacion.resumen == ""
        assert conversacion.transcripcion

    def test_no_escribe_el_respaldo_clinico(self, tmp_path: Path) -> None:
        # El respaldo clínico alimenta la página Pacientes; una conversación
        # comercial no puede aparecer allí como un falso paciente.
        params = _params(tmp_path)
        recursos: AppResources = params.app_resources

        contexto = LLMContext(messages=[{"role": "user", "content": "Tengo una óptica."}])
        cierre_de_prospecto(recursos, contexto)

        carpeta = dir_resumenes(recursos.settings.data_dir)
        assert not carpeta.exists() or not list(carpeta.iterdir())

    def test_sin_habla_no_anota_nada(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        recursos: AppResources = params.app_resources
        assert recursos.prospectos is not None
        recursos.prospectos.registrar_conversacion("conv-actual", ID)

        cierre_de_prospecto(recursos, LLMContext(messages=[]))

        conversacion = recursos.prospectos.conversacion("conv-actual")
        assert conversacion is not None and conversacion.transcripcion == ""


class TestLaGalleta:
    def test_una_galleta_legitima_se_respeta(self) -> None:
        id_prospecto, emitir = galleta_de_prospecto({"vd_prospecto": ID})
        assert id_prospecto == ID
        assert emitir is False

    def test_sin_galleta_se_emite_una_nueva(self) -> None:
        id_prospecto, emitir = galleta_de_prospecto({})
        assert emitir is True
        assert len(id_prospecto) == 32

    def test_una_galleta_inventada_no_abre_ficha_ajena(self) -> None:
        # La galleta no va firmada: lo que la protege es que un valor con otra
        # pinta (basura, un intento de inyección) se descarta y se emite otra.
        for basura in ("", "  ", "x" * 32, "a" * 31, "A3F9C2D1E8B7460FA1B2C3D4E5F60718", "../../x"):
            id_prospecto, emitir = galleta_de_prospecto({"vd_prospecto": basura})
            assert emitir is True
            assert id_prospecto != basura


class TestElModo:
    def test_de_fabrica_cae_en_clinico(self) -> None:
        # Sin runtime.json (la degradación de cargar_runtime) todas las
        # herramientas están encendidas: el agente es el clínico de siempre.
        assert modo_prospectos(RuntimeConfig()) is False

    def test_el_perfil_comercial_lo_enciende(self) -> None:
        runtime = RuntimeConfig(
            herramientas=[
                ToolConfig(nombre="finalizar_llamada", habilitada=False),
                ToolConfig(nombre="registrar_alerta", habilitada=False),
                ToolConfig(nombre="buscar_en_documentos", habilitada=False),
            ]
        )
        assert modo_prospectos(runtime) is True

    def test_apagar_tambien_el_brief_lo_apaga(self) -> None:
        runtime = RuntimeConfig(
            herramientas=[
                ToolConfig(nombre="finalizar_llamada", habilitada=False),
                ToolConfig(nombre="guardar_brief", habilitada=False),
            ]
        )
        assert modo_prospectos(runtime) is False
