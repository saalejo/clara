"""Los modelos que el panel escribe y el agente lee.

Es un contrato entre dos procesos que viven en imágenes distintas, así que lo
que se vigila aquí es sobre todo que siga siendo compatible: los valores por
defecto tienen que reproducir el comportamiento de siempre, y una configuración
imposible tiene que fallar en el panel y no cuarenta segundos después, en el
arranque del agente y en otro contenedor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_agent_core.prompts import MULETILLAS, PROMPT_SISTEMA, SALUDO_INICIAL
from voice_agent_core.runtime import (
    AccionHook,
    EventoHook,
    HookConfig,
    MCPServerConfig,
    PromptConfig,
    RuntimeConfig,
    ToolConfig,
    TransporteMCP,
    cargar_runtime,
)
from voice_agent_core.rutas import ruta_runtime


def test_por_defecto_se_comporta_como_antes_del_panel() -> None:
    # Un clon recién bajado, sin panel y sin ficheros, tiene que sonar igual.
    prompt = RuntimeConfig().prompt
    assert prompt.prompt_sistema == PROMPT_SISTEMA
    assert prompt.saludo_inicial == SALUDO_INICIAL
    assert prompt.muletillas == MULETILLAS
    assert prompt.prompt_sistema_efectivo == PROMPT_SISTEMA


def test_el_saludo_comercial_lleva_el_aviso_de_privacidad() -> None:
    """El saludo es también el aviso de la Ley 1581 (arts. 9 y 12).

    Quien lo reescriba tiene que conservar las tres piezas: que Clara es una
    IA, que la conversación se graba/transcribe, y dónde está la política.
    Sin ellas, la autorización por conducta inequívoca que registra
    `cierre_de_prospecto` deja de estar informada.
    """
    from voice_agent_core.prompts import SALUDO_MARKETING

    assert "inteligencia artificial" in SALUDO_MARKETING
    assert "se graba" in SALUDO_MARKETING and "transcribe" in SALUDO_MARKETING
    assert "privacidad" in SALUDO_MARKETING


def test_las_muletillas_por_defecto_no_se_comparten() -> None:
    # Si el default no fuese una copia, editar las muletillas de una instancia
    # mutaría la constante del módulo para todo el proceso.
    uno = PromptConfig()
    uno.muletillas["consulta"].append("una frase nueva")
    assert "una frase nueva" not in PromptConfig().muletillas["consulta"]
    assert "una frase nueva" not in MULETILLAS["consulta"]


def test_el_alma_se_anade_al_final_del_prompt() -> None:
    efectivo = PromptConfig(alma="Eres irónico pero nunca borde.").prompt_sistema_efectivo
    assert efectivo.startswith(PROMPT_SISTEMA.rstrip())
    assert efectivo.rstrip().endswith("Eres irónico pero nunca borde.")


def test_un_alma_en_blanco_no_toca_el_prompt() -> None:
    assert PromptConfig(alma="   \n  ").prompt_sistema_efectivo == PROMPT_SISTEMA


def test_ida_y_vuelta_por_json() -> None:
    original = RuntimeConfig(
        prompt=PromptConfig(alma="Hablas despacio."),
        herramientas=[ToolConfig(nombre="obtener_fecha_hora", habilitada=False)],
        mcp=[MCPServerConfig(nombre="ficheros", transporte=TransporteMCP.STDIO, comando="mcp-fs")],
        hooks=[
            HookConfig(
                nombre="corrige-nanopi",
                evento=EventoHook.TRANSCRIPCION_LISTA,
                accion=AccionHook.REESCRIBIR,
                patron="nanopi",
                reemplazo="NanoPi",
            )
        ],
    )
    copia = RuntimeConfig.model_validate_json(original.model_dump_json())
    assert copia == original


def test_herramientas_desactivadas() -> None:
    runtime = RuntimeConfig(
        herramientas=[
            ToolConfig(nombre="buscar_en_documentos", habilitada=True),
            ToolConfig(nombre="estado_del_sistema", habilitada=False),
        ]
    )
    assert runtime.herramientas_desactivadas == frozenset({"estado_del_sistema"})


def test_los_hooks_salen_ordenados_y_solo_los_activos() -> None:
    def hook(nombre: str, orden: int, habilitado: bool) -> HookConfig:
        return HookConfig(
            nombre=nombre,
            orden=orden,
            habilitado=habilitado,
            evento=EventoHook.TRANSCRIPCION_LISTA,
            accion=AccionHook.REESCRIBIR,
            patron="x",
        )

    runtime = RuntimeConfig(
        hooks=[hook("tarde", 90, True), hook("apagado", 1, False), hook("pronto", 10, True)]
    )
    assert [h.nombre for h in runtime.hooks_de(EventoHook.TRANSCRIPCION_LISTA)] == [
        "pronto",
        "tarde",
    ]


# --- Configuraciones imposibles, que deben fallar al guardarse ---------------


def test_un_mcp_stdio_sin_comando_no_valida() -> None:
    with pytest.raises(ValueError, match="necesita un comando"):
        MCPServerConfig(nombre="roto", transporte=TransporteMCP.STDIO)


def test_un_mcp_http_sin_url_no_valida() -> None:
    with pytest.raises(ValueError, match="necesita una url"):
        MCPServerConfig(nombre="roto", transporte=TransporteMCP.HTTP)


def test_un_hook_de_comando_sin_comando_no_valida() -> None:
    with pytest.raises(ValueError, match="no tiene ninguno"):
        HookConfig(
            nombre="roto", evento=EventoHook.ERROR, accion=AccionHook.EJECUTAR_COMANDO, comando=[]
        )


def test_un_hook_bloqueante_no_puede_tener_un_timeout_largo() -> None:
    # Se sumaría a cada turno de la conversación.
    with pytest.raises(ValueError, match="se sumaría a cada turno"):
        HookConfig(
            nombre="lento",
            evento=EventoHook.ERROR,
            accion=AccionHook.EJECUTAR_COMANDO,
            comando=["true"],
            bloqueante=True,
            timeout_secs=30.0,
        )


def test_no_se_puede_reescribir_un_evento_que_no_lleva_texto() -> None:
    # Vetar o reescribir un frame de control cuelga el pipeline entero, así que
    # ni siquiera se ofrece como opción.
    with pytest.raises(ValueError, match="no lleva texto"):
        HookConfig(
            nombre="imposible",
            evento=EventoHook.USUARIO_TERMINO,
            accion=AccionHook.REESCRIBIR,
            patron="x",
        )


# --- Carga desde disco -------------------------------------------------------


def test_cargar_sin_fichero_da_los_valores_por_defecto(tmp_path: Path) -> None:
    assert cargar_runtime(tmp_path) == RuntimeConfig()


def test_cargar_un_json_roto_no_lanza(tmp_path: Path) -> None:
    ruta = ruta_runtime(tmp_path)
    ruta.parent.mkdir(parents=True)
    ruta.write_text("{{{ roto", encoding="utf-8")

    assert cargar_runtime(tmp_path) == RuntimeConfig()


def test_cargar_algo_que_no_valida_no_lanza(tmp_path: Path) -> None:
    # JSON correcto pero semánticamente imposible: un MCP stdio sin comando.
    ruta = ruta_runtime(tmp_path)
    ruta.parent.mkdir(parents=True)
    ruta.write_text('{"mcp": [{"nombre": "roto", "transporte": "stdio"}]}', encoding="utf-8")

    assert cargar_runtime(tmp_path) == RuntimeConfig()


def test_cargar_lo_que_el_panel_escribio(tmp_path: Path) -> None:
    escrito = RuntimeConfig(prompt=PromptConfig(alma="Eres conciso."))
    ruta = ruta_runtime(tmp_path)
    ruta.parent.mkdir(parents=True)
    ruta.write_text(escrito.model_dump_json(), encoding="utf-8")

    assert cargar_runtime(tmp_path).prompt.alma == "Eres conciso."
