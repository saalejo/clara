"""La exportación es la frontera entre el panel y el agente.

Lo que se comprueba aquí es que nada inválido llegue a cruzarla, que los campos
protegidos no se cuelen, y que un fallo no deje ficheros a medias.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from voice_agent_core.config import Settings
from voice_agent_core.runtime import RuntimeConfig
from voice_agent_core.rutas import ruta_runtime, ruta_snapshot_settings
from voice_agent_panel.exporter import (
    ErrorDeExportacion,
    construir_runtime,
    construir_snapshot_settings,
    exportar,
    herramientas_citadas_en_el_prompt,
)
from voice_agent_panel.models import (
    AjusteAgente,
    Despliegue,
    Herramienta,
    Hook,
    Perfil,
    ServidorMCP,
    VersionPrompt,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def perfil() -> Perfil:
    """El perfil "Por defecto" que siembra la migración, ya activo."""
    return cast(Perfil, Perfil.objects.get(activo=True))


def test_sin_nada_configurado_exporta_los_valores_por_defecto(tmp_path: Path) -> None:
    exportar(tmp_path)

    assert json.loads(ruta_snapshot_settings(tmp_path).read_text()) == {}
    runtime = RuntimeConfig.model_validate_json(ruta_runtime(tmp_path).read_text())
    assert runtime.prompt.prompt_sistema == RuntimeConfig().prompt.prompt_sistema
    assert runtime.perfil == "Por defecto"


def test_un_despliegue_completo_refresca_tambien_las_tareas(tmp_path: Path) -> None:
    # Así un tareas.json borrado a mano se regenera por el camino de siempre.
    from voice_agent_core.tareas import cargar_tareas
    from voice_agent_panel.models import TareaProgramada

    TareaProgramada.objects.create(
        nombre="pastillas", cron="0 8 * * *", mision="Recuerda la pastilla.", habilitada=True
    )
    exportar(tmp_path)

    config = cargar_tareas(tmp_path)
    assert [t.id for t in config.tareas] == ["pastillas"]
    assert config.tareas[0].habilitada


def test_los_ajustes_guardados_llegan_al_snapshot(tmp_path: Path, perfil: Perfil) -> None:
    AjusteAgente.objects.create(perfil=perfil, clave="llm_temperature", valor="0.25")
    AjusteAgente.objects.create(perfil=perfil, clave="gemini_model", valor='"otro-modelo"')

    exportar(tmp_path)

    assert json.loads(ruta_snapshot_settings(tmp_path).read_text()) == {
        "llm_temperature": 0.25,
        "gemini_model": "otro-modelo",
    }


def test_un_campo_protegido_no_se_exporta(perfil: Perfil) -> None:
    # Aunque alguien meta la fila a mano en la base de datos.
    AjusteAgente.objects.create(perfil=perfil, clave="gemini_api_key", valor='"secreta"')
    AjusteAgente.objects.create(perfil=perfil, clave="data_dir", valor='"/otra/ruta"')

    assert construir_snapshot_settings() == {}


def test_un_valor_fuera_de_rango_se_rechaza_aqui(perfil: Perfil) -> None:
    # Y no cuarenta segundos después, en el arranque del agente y en otro
    # contenedor. Ése es el motivo de validar con Settings antes de escribir.
    AjusteAgente.objects.create(perfil=perfil, clave="llm_temperature", valor="9.0")

    with pytest.raises(ErrorDeExportacion, match="no son válidos"):
        construir_snapshot_settings()


def test_un_json_corrupto_en_la_base_de_datos_se_rechaza(perfil: Perfil) -> None:
    AjusteAgente.objects.create(perfil=perfil, clave="llm_temperature", valor="{{{")

    with pytest.raises(ErrorDeExportacion, match="no es JSON válido"):
        construir_snapshot_settings()


def test_los_ajustes_de_un_perfil_inactivo_no_se_exportan(perfil: Perfil) -> None:
    otro = Perfil.objects.create(nombre="Otro")
    AjusteAgente.objects.create(perfil=otro, clave="llm_temperature", valor="0.9")

    assert construir_snapshot_settings() == {}


def test_si_la_validacion_falla_no_se_escribe_nada(tmp_path: Path, perfil: Perfil) -> None:
    AjusteAgente.objects.create(perfil=perfil, clave="llm_temperature", valor="9.0")

    with pytest.raises(ErrorDeExportacion):
        exportar(tmp_path)

    assert not ruta_snapshot_settings(tmp_path).exists()
    assert not ruta_runtime(tmp_path).exists()


def test_no_quedan_temporales(tmp_path: Path) -> None:
    exportar(tmp_path)
    assert list((tmp_path / "config").glob("*.tmp")) == []


def test_el_prompt_activo_es_el_que_se_exporta(tmp_path: Path, perfil: Perfil) -> None:
    VersionPrompt.objects.create(
        perfil=perfil, prompt_sistema="viejo", saludo_inicial="hola", muletillas={}, activa=False
    )
    VersionPrompt.objects.create(
        perfil=perfil,
        prompt_sistema="nuevo",
        alma="Eres directo.",
        saludo_inicial="buenas",
        muletillas={"consulta": ["un momento"]},
        activa=True,
    )

    runtime = construir_runtime()

    assert runtime.prompt.prompt_sistema == "nuevo"
    assert runtime.prompt.alma == "Eres directo."
    assert "Eres directo." in runtime.prompt.prompt_sistema_efectivo


def test_herramientas_hooks_y_mcp_se_exportan(tmp_path: Path, perfil: Perfil) -> None:
    Herramienta.objects.create(perfil=perfil, nombre="estado_del_sistema", habilitada=False)
    servidor = ServidorMCP.objects.create(nombre="ficheros", transporte="stdio", comando="mcp-fs")
    hook = Hook.objects.create(
        nombre="corrige",
        evento="transcripcion_lista",
        accion="reescribir",
        patron="nanopi",
        reemplazo="NanoPi",
    )
    perfil.mcp_habilitados.add(servidor)
    perfil.hooks_habilitados.add(hook)

    runtime = construir_runtime()

    assert runtime.herramientas_desactivadas == frozenset({"estado_del_sistema"})
    assert [s.nombre for s in runtime.servidores_mcp_activos] == ["ficheros"]
    assert runtime.hay_hooks


def test_la_seleccion_es_del_perfil_activo(tmp_path: Path, perfil: Perfil) -> None:
    # El mismo catálogo, dos perfiles: solo cuenta la selección del activo.
    servidor = ServidorMCP.objects.create(nombre="ficheros", transporte="stdio", comando="mcp-fs")
    otro = Perfil.objects.create(nombre="Otro")
    otro.mcp_habilitados.add(servidor)

    runtime = construir_runtime()

    assert [s.nombre for s in runtime.mcp] == ["ficheros"]
    assert runtime.servidores_mcp_activos == []

    otro.activar()
    runtime = construir_runtime()
    assert [s.nombre for s in runtime.servidores_mcp_activos] == ["ficheros"]
    assert runtime.perfil == "Otro"


def test_una_configuracion_imposible_de_mcp_se_rechaza() -> None:
    # Un stdio sin comando: el modelo del agente lo rechaza y aquí sale el aviso.
    ServidorMCP.objects.create(nombre="roto", transporte="stdio", comando="")

    with pytest.raises(ErrorDeExportacion, match="necesita un comando"):
        construir_runtime()


def test_el_despliegue_guarda_lo_que_se_envio(tmp_path: Path, perfil: Perfil) -> None:
    AjusteAgente.objects.create(perfil=perfil, clave="llm_temperature", valor="0.4")

    despliegue = exportar(tmp_path)

    assert despliegue.resultado == Despliegue.Resultado.EXPORTADO
    assert despliegue.instantanea_settings == {"llm_temperature": 0.4}
    assert despliegue.instantanea_runtime["prompt"]["saludo_inicial"]


def test_lo_exportado_lo_lee_settings_de_verdad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, perfil: Perfil
) -> None:
    # La prueba de que el contrato entre los dos procesos cierra: se exporta
    # desde el panel y se construye un Settings leyendo ese fichero.
    from voice_agent_core.rutas import VAR_ENTORNO_SNAPSHOT

    AjusteAgente.objects.create(perfil=perfil, clave="llm_max_tokens", valor="321")
    exportar(tmp_path)

    monkeypatch.setenv(VAR_ENTORNO_SNAPSHOT, str(ruta_snapshot_settings(tmp_path)))
    assert Settings(_env_file=None).llm_max_tokens == 321  # type: ignore[call-arg]


def test_avisa_si_el_prompt_menciona_una_herramienta_apagada(perfil: Perfil) -> None:
    VersionPrompt.objects.create(
        perfil=perfil,
        prompt_sistema="Tienes buscar_en_documentos para consultar.",
        saludo_inicial="hola",
        muletillas={},
        activa=True,
    )

    assert herramientas_citadas_en_el_prompt({"buscar_en_documentos"}) == ["buscar_en_documentos"]


def test_no_avisa_si_el_prompt_no_la_menciona(perfil: Perfil) -> None:
    VersionPrompt.objects.create(
        perfil=perfil,
        prompt_sistema="Habla claro.",
        saludo_inicial="hola",
        muletillas={},
        activa=True,
    )

    assert herramientas_citadas_en_el_prompt({"buscar_en_documentos"}) == []
