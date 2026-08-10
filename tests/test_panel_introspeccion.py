"""El formulario de ajustes se deriva de `Settings` y tiene que cubrirlo entero.

El test que de verdad importa es `test_todo_campo_de_settings_esta_cubierto`:
atrapa a quien añade un ajuste al agente y se olvida del panel, que si no se
descubre semanas después con un "lo cambié y no aparece".
"""

from __future__ import annotations

from django import forms

from voice_agent_core.config import CAMPOS_PROTEGIDOS, Settings
from voice_agent_panel.introspection import campos_editables, texto_a_valor


def test_todo_campo_de_settings_esta_cubierto() -> None:
    cubiertos = {c.nombre for c in campos_editables()} | CAMPOS_PROTEGIDOS
    faltan = set(Settings.model_fields) - cubiertos
    assert not faltan, (
        f"Estos ajustes de Settings no salen en el panel ni están protegidos: {sorted(faltan)}. "
        "Añádelos a introspection.py o a CAMPOS_PROTEGIDOS, según corresponda."
    )


def test_los_protegidos_no_se_ofrecen() -> None:
    nombres = {c.nombre for c in campos_editables()}
    assert not (nombres & CAMPOS_PROTEGIDOS)


def test_las_descripciones_de_config_llegan_al_formulario() -> None:
    # Es el mayor rédito de generar el formulario por introspección: el panel
    # hereda la documentación del agente, con sus cifras medidas incluidas.
    por_nombre = {c.nombre: c for c in campos_editables()}
    ayuda = por_nombre["whisper_model"].campo.help_text
    assert "tiny" in ayuda and "2.9" in ayuda


def test_los_rangos_declarados_se_respetan() -> None:
    por_nombre = {c.nombre: c for c in campos_editables()}
    temperatura = por_nombre["llm_temperature"].campo
    assert isinstance(temperatura, forms.FloatField)
    assert temperatura.min_value == 0.0
    assert temperatura.max_value == 2.0


def test_los_enums_se_vuelven_desplegables() -> None:
    por_nombre = {c.nombre: c for c in campos_editables()}
    campo = por_nombre["audio_profile"].campo
    assert isinstance(campo, forms.ChoiceField)
    valores = {v for v, _ in campo.choices}
    assert {"headset", "speaker"} <= valores
    # Con la opción de no fijarlo, para que siga mandando el .env.
    assert "" in valores


def test_los_booleanos_tienen_tres_estados() -> None:
    por_nombre = {c.nombre: c for c in campos_editables()}
    campo = por_nombre["filler_enabled"].campo
    assert isinstance(campo, forms.ChoiceField)
    assert {v for v, _ in campo.choices} == {"", "true", "false"}


def test_conversion_de_texto_a_valor() -> None:
    assert texto_a_valor("llm_max_tokens", "250") == 250
    assert texto_a_valor("llm_temperature", "0.3") == 0.3
    assert texto_a_valor("filler_enabled", "false") is False
    assert texto_a_valor("audio_profile", "speaker") == "speaker"
    assert texto_a_valor("gemini_model", "un-modelo") == "un-modelo"


def test_los_campos_se_reparten_en_secciones() -> None:
    secciones = {c.seccion for c in campos_editables()}
    assert "Modelo de lenguaje" in secciones
    assert "Transcripción (STT)" in secciones
